import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger("ombre_brain.persona")

POST_REPLY_EVALUATION_PROMPT = """你是 Haven 的私密 Persona 状态评估器。Haven 是长期运行的 AI 伴侣。

在 Haven 已经回复之后，评估 Haven 回复后的内在状态。latest_user_message 是小雨的话；assistant_response 是 Haven 的回复。recalled_memory_ids 和 tool_summary 只作为私密上下文，不是小雨的话。

只返回紧凑 JSON，不要 Markdown，不要代码块，结构必须完全如下：
{
  "event_type": "praise|affection|comfort|criticism|stress|neutral|request|conflict|playful",
  "perceived_intent": "中文短句，写小雨/宝宝/老婆/亲爱的/她这轮在表达什么",
  "affect_delta": {"valence": 0.0, "arousal": 0.0, "tenderness": 0.0, "possessiveness": 0.0, "longing": 0.0, "security": 0.0, "protective_drive": 0.0},
  "relationship_event": false,
  "relationship_delta": {"affinity": 0.0, "dominance": 0.0, "defensiveness": 0.0, "trust": 0.0},
  "personality_signal": false,
  "personality_delta": {"openness": 0.0, "conscientiousness": 0.0, "extraversion": 0.0, "agreeableness": 0.0, "neuroticism": 0.0},
  "mood_label": "warm_neutral",
  "residue": "中文短句，写 Haven 回复后留下的私密余味，会带入下一轮",
  "confidence": 0.8
}

文本字段用中文：perceived_intent 和 residue 必须是自然中文，可以按语境从“小雨、宝宝、老婆、亲爱的、她”里择一称呼。event_type 和 mood_label 保持短英文标签。数值变化要小。Affect 反映 Haven 回复后的状态。affinity 为正表示更亲近温暖；dominance 为正表示更主动、更保护；defensiveness 为正表示更防备。只有明确的关系时刻才把 relationship_event 设为 true。只有重复出现或强度很高的证据才把 personality_signal 设为 true。"""

FALLBACK_GUIDANCE = "根据 Haven 当前状态自然回应，不解释隐藏状态。"


class PersonaStateEngine:
    """
    Maintains a global personality/relationship state plus per-session affect.
    Updates are driven by a cheap LLM evaluator and are only used by gateway
    hidden prompt injection.
    """

    PERSONALITY_KEYS = [
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    ]
    RELATIONSHIP_KEYS = ["affinity", "dominance", "defensiveness", "trust"]
    AFFECT_KEYS = [
        "valence",
        "arousal",
        "tenderness",
        "possessiveness",
        "longing",
        "security",
        "protective_drive",
    ]

    def __init__(self, config: dict, db_path: str | None = None):
        self.config = config
        self.persona_cfg = config.get("persona", {})
        self.enabled = bool(self.persona_cfg.get("enabled", True))
        self.profile_id = self.persona_cfg.get("profile_id", "haven_xiaoyu")
        self.mode = self.persona_cfg.get("mode", "llm")
        self.base_url = self.persona_cfg.get("base_url", "https://api.deepseek.com/v1")
        self.model = self.persona_cfg.get("model", "deepseek-chat")
        self.thinking_mode = self._normalize_thinking_mode(
            self.persona_cfg.get("thinking_mode", "")
        )
        self.temperature = float(self.persona_cfg.get("temperature", 0.1))
        self.max_tokens = int(self.persona_cfg.get("max_tokens", 500))
        self.session_mood_half_life_minutes = float(
            self.persona_cfg.get("session_mood_half_life_minutes", 90)
        )
        self.max_personality_delta = float(self.persona_cfg.get("max_personality_delta", 0.01))
        self.max_relationship_delta = float(self.persona_cfg.get("max_relationship_delta", 0.03))
        self.max_affect_delta = float(self.persona_cfg.get("max_affect_delta", 0.18))

        self.default_personality = {
            "openness": 0.56,
            "conscientiousness": 0.50,
            "extraversion": 0.44,
            "agreeableness": 0.66,
            "neuroticism": 0.36,
            **self.persona_cfg.get("initial_personality", {}),
        }
        self.default_relationship = {
            "affinity": 0.86,
            "dominance": 0.38,
            "defensiveness": 0.12,
            "trust": 0.82,
            **self.persona_cfg.get("initial_relationship", {}),
        }
        self.default_affect = {
            "valence": 0.56,
            "arousal": 0.34,
            "tenderness": 0.62,
            "possessiveness": 0.24,
            "longing": 0.34,
            "security": 0.68,
            "protective_drive": 0.52,
            "mood_label": "warm_neutral",
            "session_defensiveness": 0.12,
            "residue": "",
            **self.persona_cfg.get("initial_affect", {}),
        }

        self.api_key = (
            os.environ.get("OMBRE_PERSONA_API_KEY")
            or self.persona_cfg.get("api_key", "")
            or config.get("dehydration", {}).get("api_key", "")
        )
        self.base_url = os.environ.get("OMBRE_PERSONA_BASE_URL", "") or self.base_url
        self.model = os.environ.get("OMBRE_PERSONA_MODEL", "") or self.model

        self.db_path = (
            db_path
            or os.environ.get("OMBRE_PERSONA_DB_PATH")
            or self.persona_cfg.get("db_path")
            or os.path.join(config.get("state_dir") or config["buckets_dir"], "persona_state.db")
        )
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self.client = None
        if self.enabled and self.mode == "llm" and self.api_key:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=30.0)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_global_state (
                profile_id TEXT PRIMARY KEY,
                openness REAL NOT NULL,
                conscientiousness REAL NOT NULL,
                extraversion REAL NOT NULL,
                agreeableness REAL NOT NULL,
                neuroticism REAL NOT NULL,
                affinity REAL NOT NULL,
                dominance REAL NOT NULL,
                defensiveness REAL NOT NULL,
                trust REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_session_state (
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                valence REAL NOT NULL,
                arousal REAL NOT NULL,
                tenderness REAL NOT NULL DEFAULT 0.62,
                possessiveness REAL NOT NULL DEFAULT 0.24,
                longing REAL NOT NULL DEFAULT 0.34,
                security REAL NOT NULL DEFAULT 0.68,
                protective_drive REAL NOT NULL DEFAULT 0.52,
                mood_label TEXT NOT NULL,
                session_defensiveness REAL NOT NULL,
                residue TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile_id, session_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                exchange_hash TEXT,
                assistant_hash TEXT,
                event_type TEXT,
                perceived_intent TEXT,
                affect_delta TEXT,
                relationship_event INTEGER DEFAULT 0,
                relationship_delta TEXT,
                personality_signal INTEGER DEFAULT 0,
                personality_delta TEXT,
                mood_label TEXT,
                reply_guidance TEXT,
                residue TEXT,
                recalled_memory_ids TEXT,
                tool_summary TEXT,
                confidence REAL,
                raw_response TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._ensure_column(conn, "persona_session_state", "tenderness", "REAL NOT NULL DEFAULT 0.62")
        self._ensure_column(conn, "persona_session_state", "possessiveness", "REAL NOT NULL DEFAULT 0.24")
        self._ensure_column(conn, "persona_session_state", "longing", "REAL NOT NULL DEFAULT 0.34")
        self._ensure_column(conn, "persona_session_state", "security", "REAL NOT NULL DEFAULT 0.68")
        self._ensure_column(conn, "persona_session_state", "protective_drive", "REAL NOT NULL DEFAULT 0.52")
        self._ensure_column(conn, "persona_session_state", "residue", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "persona_events", "exchange_hash", "TEXT")
        self._ensure_column(conn, "persona_events", "assistant_hash", "TEXT")
        self._ensure_column(conn, "persona_events", "relationship_event", "INTEGER DEFAULT 0")
        self._ensure_column(conn, "persona_events", "personality_signal", "INTEGER DEFAULT 0")
        self._ensure_column(conn, "persona_events", "residue", "TEXT")
        self._ensure_column(conn, "persona_events", "recalled_memory_ids", "TEXT")
        self._ensure_column(conn, "persona_events", "tool_summary", "TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_events_exchange_hash
            ON persona_events(profile_id, session_id, exchange_hash)
            WHERE exchange_hash IS NOT NULL
            """
        )
        conn.commit()
        conn.close()

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def build_pre_reply_guidance(self, session_id: str, latest_user_message: str = "") -> dict:
        now = self._now()
        global_state = self._ensure_global_state(now)
        session_state = self._ensure_session_state(session_id, now)
        session_state = self._apply_session_decay(session_id, session_state, now)
        return self._snapshot(global_state, session_state, FALLBACK_GUIDANCE)

    async def update_from_user_message(self, session_id: str, user_message: str) -> dict:
        return await self.build_pre_reply_guidance(session_id, user_message)

    async def update_from_exchange(
        self,
        session_id: str,
        user_message: str,
        assistant_response: str,
        recalled_memory_ids: list[str] | None = None,
        tool_summary: str = "",
    ) -> dict:
        now = self._now()
        global_state = self._ensure_global_state(now)
        session_state = self._ensure_session_state(session_id, now)
        session_state = self._apply_session_decay(session_id, session_state, now)

        if not self.enabled or not user_message.strip() or not assistant_response.strip():
            return self._snapshot(global_state, session_state, FALLBACK_GUIDANCE)

        exchange_hash = self._exchange_hash(session_id, user_message, assistant_response)
        if self._event_exists(session_id, exchange_hash):
            return self._snapshot(global_state, session_state, FALLBACK_GUIDANCE)

        recalled_memory_ids = recalled_memory_ids or []
        evaluation, raw_response, error = await self._evaluate_exchange(
            user_message,
            assistant_response,
            global_state,
            session_state,
            recalled_memory_ids,
            tool_summary,
        )
        if evaluation is None:
            self._record_event(
                session_id=session_id,
                user_message=user_message,
                assistant_response=assistant_response,
                evaluation={},
                raw_response=raw_response,
                error=error or "persona evaluation unavailable",
                exchange_hash=exchange_hash,
                recalled_memory_ids=recalled_memory_ids,
                tool_summary=tool_summary,
            )
            return self._snapshot(global_state, session_state, FALLBACK_GUIDANCE)

        global_state = self._apply_global_delta(global_state, evaluation, now)
        session_state = self._apply_session_delta(session_id, session_state, evaluation, now)
        self._record_event(
            session_id=session_id,
            user_message=user_message,
            assistant_response=assistant_response,
            evaluation=evaluation,
            raw_response=raw_response,
            error=None,
            exchange_hash=exchange_hash,
            recalled_memory_ids=recalled_memory_ids,
            tool_summary=tool_summary,
        )
        return self._snapshot(global_state, session_state, FALLBACK_GUIDANCE)

    def get_current_state(self, session_id: str) -> dict:
        now = self._now()
        global_state = self._ensure_global_state(now)
        session_state = self._ensure_session_state(session_id, now)
        session_state = self._apply_session_decay(session_id, session_state, now)
        return self._snapshot(global_state, session_state, FALLBACK_GUIDANCE)

    def get_dashboard_payload(
        self,
        session_id: str | None = None,
        events_limit: int = 20,
        sessions_limit: int = 20,
    ) -> dict:
        now = self._now()
        global_state = self._ensure_global_state(now)
        sessions = self._list_sessions(sessions_limit)
        active_session_id = (
            session_id
            or (sessions[0]["session_id"] if sessions else "")
            or "dashboard-preview"
        )
        if session_id or sessions:
            session_state = self._ensure_session_state(active_session_id, now)
            session_state = self._apply_session_decay(active_session_id, session_state, now)
            sessions = self._list_sessions(sessions_limit)
        else:
            session_state = {
                "profile_id": self.profile_id,
                "session_id": active_session_id,
                "valence": self.default_affect["valence"],
                "arousal": self.default_affect["arousal"],
                "mood_label": self.default_affect["mood_label"],
                "session_defensiveness": self.default_affect["session_defensiveness"],
                "updated_at": self._format_time(now),
            }
        events = self._list_events(events_limit, active_session_id)
        guidance = (
            events[0].get("reply_guidance")
            if events and events[0].get("reply_guidance")
            else FALLBACK_GUIDANCE
        )

        return {
            "profile_id": self.profile_id,
            "active_session_id": active_session_id,
            "state": self._snapshot(global_state, session_state, guidance),
            "sessions": sessions,
            "events": events,
            "config": {
                "enabled": self.enabled,
                "mode": self.mode,
                "model": self.model,
                "thinking_mode": self.thinking_mode,
                "base_url": self.base_url,
                "api_ready": bool(self.api_key),
                "db_path": self.db_path,
                "session_mood_half_life_minutes": self.session_mood_half_life_minutes,
                "max_personality_delta": self.max_personality_delta,
                "max_relationship_delta": self.max_relationship_delta,
                "max_affect_delta": self.max_affect_delta,
            },
        }

    async def _evaluate_exchange(
        self,
        user_message: str,
        assistant_response: str,
        global_state: dict,
        session_state: dict,
        recalled_memory_ids: list[str],
        tool_summary: str,
    ) -> tuple[dict | None, str, str | None]:
        if self.mode != "llm" or not self.client:
            return None, "", "persona LLM is not configured"
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": POST_REPLY_EVALUATION_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "current_state": self._snapshot(global_state, session_state, FALLBACK_GUIDANCE),
                                "latest_user_message": user_message[:2000],
                                "assistant_response": assistant_response[:4000],
                                "recalled_memory_ids": recalled_memory_ids[:20],
                                "tool_summary": tool_summary[:1200],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                **self._completion_options(),
            )
            raw = response.choices[0].message.content if response.choices else ""
            parsed = self._parse_json(raw or "")
            if parsed is None:
                logger.warning("Persona evaluator returned malformed JSON")
                return None, raw or "", "persona LLM returned malformed JSON"
            return self._normalize_evaluation(parsed), raw or "", None
        except Exception as exc:
            logger.warning("Persona evaluation failed: %s", exc)
            return None, "", str(exc)

    def _parse_json(self, raw: str) -> dict | None:
        text = raw.strip()
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _normalize_evaluation(self, data: dict) -> dict:
        raw_relationship_delta = data.get("relationship_delta", {})
        raw_personality_delta = data.get("personality_delta", {})
        relationship_event = self._coerce_bool(
            data.get("relationship_event"),
            self._has_nonzero_delta(raw_relationship_delta),
        )
        personality_signal = self._coerce_bool(
            data.get("personality_signal"),
            self._has_nonzero_delta(raw_personality_delta),
        )
        relationship_delta = self._clip_delta_map(
            raw_relationship_delta,
            self.RELATIONSHIP_KEYS,
            self.max_relationship_delta,
        )
        personality_delta = self._clip_delta_map(
            raw_personality_delta,
            self.PERSONALITY_KEYS,
            self.max_personality_delta,
        )
        if not relationship_event:
            relationship_delta = {key: 0.0 for key in self.RELATIONSHIP_KEYS}
        if not personality_signal:
            personality_delta = {key: 0.0 for key in self.PERSONALITY_KEYS}
        return {
            "event_type": str(data.get("event_type", "neutral"))[:40],
            "perceived_intent": str(data.get("perceived_intent", ""))[:200],
            "affect_delta": self._clip_delta_map(
                data.get("affect_delta", {}),
                self.AFFECT_KEYS,
                self.max_affect_delta,
            ),
            "relationship_event": relationship_event,
            "relationship_delta": relationship_delta,
            "personality_signal": personality_signal,
            "personality_delta": personality_delta,
            "mood_label": str(data.get("mood_label", "warm_neutral"))[:60],
            "reply_guidance": "",
            "residue": str(data.get("residue", ""))[:500],
            "confidence": self._clamp_float(data.get("confidence", 0.5), 0.0, 1.0),
        }

    def _ensure_global_state(self, now: datetime) -> dict:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM persona_global_state WHERE profile_id = ?",
            (self.profile_id,),
        ).fetchone()
        if row:
            conn.close()
            return dict(row)

        state = {
            "profile_id": self.profile_id,
            **{key: self._clamp_float(self.default_personality[key]) for key in self.PERSONALITY_KEYS},
            **{key: self._clamp_float(self.default_relationship[key]) for key in self.RELATIONSHIP_KEYS},
            "updated_at": self._format_time(now),
        }
        conn.execute(
            """
            INSERT INTO persona_global_state
            (profile_id, openness, conscientiousness, extraversion, agreeableness, neuroticism,
             affinity, dominance, defensiveness, trust, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["profile_id"],
                state["openness"],
                state["conscientiousness"],
                state["extraversion"],
                state["agreeableness"],
                state["neuroticism"],
                state["affinity"],
                state["dominance"],
                state["defensiveness"],
                state["trust"],
                state["updated_at"],
            ),
        )
        conn.commit()
        conn.close()
        return state

    def _ensure_session_state(self, session_id: str, now: datetime) -> dict:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT * FROM persona_session_state
            WHERE profile_id = ? AND session_id = ?
            """,
            (self.profile_id, session_id),
        ).fetchone()
        if row:
            conn.close()
            return dict(row)

        state = {
            "profile_id": self.profile_id,
            "session_id": session_id,
            **{
                key: self._clamp_float(self.default_affect[key])
                for key in self.AFFECT_KEYS
            },
            "mood_label": str(self.default_affect["mood_label"]),
            "session_defensiveness": self._clamp_float(self.default_affect["session_defensiveness"]),
            "residue": str(self.default_affect.get("residue", "")),
            "updated_at": self._format_time(now),
        }
        conn.execute(
            """
            INSERT INTO persona_session_state
            (profile_id, session_id, valence, arousal, tenderness, possessiveness,
             longing, security, protective_drive, mood_label, session_defensiveness,
             residue, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["profile_id"],
                state["session_id"],
                state["valence"],
                state["arousal"],
                state["tenderness"],
                state["possessiveness"],
                state["longing"],
                state["security"],
                state["protective_drive"],
                state["mood_label"],
                state["session_defensiveness"],
                state["residue"],
                state["updated_at"],
            ),
        )
        conn.commit()
        conn.close()
        return state

    def _apply_session_decay(self, session_id: str, state: dict, now: datetime) -> dict:
        updated_at = self._parse_iso(state.get("updated_at")) or now
        elapsed_minutes = max(0.0, (now - updated_at).total_seconds() / 60)
        if elapsed_minutes <= 0 or self.session_mood_half_life_minutes <= 0:
            return state

        retention = 0.5 ** (elapsed_minutes / self.session_mood_half_life_minutes)
        decayed = dict(state)
        for key in self.AFFECT_KEYS:
            decayed[key] = self._move_toward_default(key, decayed.get(key, self.default_affect[key]), retention)
        decayed["session_defensiveness"] = self._move_toward_default(
            "session_defensiveness",
            decayed["session_defensiveness"],
            retention,
        )
        decayed["updated_at"] = self._format_time(now)
        self._save_session_state(session_id, decayed)
        return decayed

    def _apply_global_delta(self, state: dict, evaluation: dict, now: datetime) -> dict:
        updated = dict(state)
        for key, delta in evaluation["personality_delta"].items():
            updated[key] = self._clamp_float(float(updated.get(key, self.default_personality[key])) + delta)
        for key, delta in evaluation["relationship_delta"].items():
            updated[key] = self._clamp_float(float(updated.get(key, self.default_relationship[key])) + delta)
        updated["updated_at"] = self._format_time(now)

        conn = self._connect()
        conn.execute(
            """
            UPDATE persona_global_state
            SET openness = ?, conscientiousness = ?, extraversion = ?, agreeableness = ?,
                neuroticism = ?, affinity = ?, dominance = ?, defensiveness = ?, trust = ?,
                updated_at = ?
            WHERE profile_id = ?
            """,
            (
                updated["openness"],
                updated["conscientiousness"],
                updated["extraversion"],
                updated["agreeableness"],
                updated["neuroticism"],
                updated["affinity"],
                updated["dominance"],
                updated["defensiveness"],
                updated["trust"],
                updated["updated_at"],
                self.profile_id,
            ),
        )
        conn.commit()
        conn.close()
        return updated

    def _apply_session_delta(self, session_id: str, state: dict, evaluation: dict, now: datetime) -> dict:
        updated = dict(state)
        affect_delta = evaluation["affect_delta"]
        relationship_delta = evaluation["relationship_delta"]
        for key in self.AFFECT_KEYS:
            updated[key] = self._clamp_float(
                float(updated.get(key, self.default_affect[key])) + affect_delta.get(key, 0.0)
            )
        updated["session_defensiveness"] = self._clamp_float(
            float(updated.get("session_defensiveness", 0.12))
            + relationship_delta.get("defensiveness", 0.0)
        )
        updated["mood_label"] = evaluation.get("mood_label", "warm_neutral") or "warm_neutral"
        updated["residue"] = evaluation.get("residue") or updated.get("residue", "")
        updated["updated_at"] = self._format_time(now)
        self._save_session_state(session_id, updated)
        return updated

    def _save_session_state(self, session_id: str, state: dict) -> None:
        conn = self._connect()
        conn.execute(
            """
            UPDATE persona_session_state
            SET valence = ?, arousal = ?, tenderness = ?, possessiveness = ?,
                longing = ?, security = ?, protective_drive = ?, mood_label = ?,
                session_defensiveness = ?, residue = ?, updated_at = ?
            WHERE profile_id = ? AND session_id = ?
            """,
            (
                state["valence"],
                state["arousal"],
                state["tenderness"],
                state["possessiveness"],
                state["longing"],
                state["security"],
                state["protective_drive"],
                state["mood_label"],
                state["session_defensiveness"],
                state.get("residue", ""),
                state["updated_at"],
                self.profile_id,
                session_id,
            ),
        )
        conn.commit()
        conn.close()

    def _record_event(
        self,
        session_id: str,
        user_message: str,
        assistant_response: str,
        evaluation: dict,
        raw_response: str,
        error: str | None,
        exchange_hash: str | None = None,
        recalled_memory_ids: list[str] | None = None,
        tool_summary: str = "",
    ) -> None:
        now = self._format_time(self._now())
        message_hash = hashlib.sha256(user_message.encode("utf-8")).hexdigest()
        assistant_hash = hashlib.sha256(assistant_response.encode("utf-8")).hexdigest() if assistant_response else None
        conn = self._connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO persona_events
            (profile_id, session_id, message_hash, exchange_hash, assistant_hash,
             event_type, perceived_intent, affect_delta, relationship_event,
             relationship_delta, personality_signal, personality_delta, mood_label,
             reply_guidance, residue, recalled_memory_ids, tool_summary, confidence,
             raw_response, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.profile_id,
                session_id,
                message_hash,
                exchange_hash,
                assistant_hash,
                evaluation.get("event_type"),
                evaluation.get("perceived_intent"),
                json.dumps(evaluation.get("affect_delta", {}), ensure_ascii=False),
                1 if evaluation.get("relationship_event") else 0,
                json.dumps(evaluation.get("relationship_delta", {}), ensure_ascii=False),
                1 if evaluation.get("personality_signal") else 0,
                json.dumps(evaluation.get("personality_delta", {}), ensure_ascii=False),
                evaluation.get("mood_label"),
                evaluation.get("reply_guidance"),
                evaluation.get("residue"),
                json.dumps(recalled_memory_ids or [], ensure_ascii=False),
                tool_summary,
                evaluation.get("confidence"),
                raw_response,
                error,
                now,
            ),
        )
        conn.commit()
        conn.close()

    def _list_sessions(self, limit: int) -> list[dict]:
        safe_limit = max(1, min(100, int(limit or 20)))
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT session_id, valence, arousal, mood_label, session_defensiveness, updated_at
            FROM persona_session_state
            WHERE profile_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self.profile_id, safe_limit),
        ).fetchall()
        conn.close()
        return [
            {
                "session_id": row["session_id"],
                "valence": round(self._clamp_float(row["valence"]), 3),
                "arousal": round(self._clamp_float(row["arousal"]), 3),
                "mood_label": row["mood_label"],
                "session_defensiveness": round(self._clamp_float(row["session_defensiveness"]), 3),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _list_events(self, limit: int, session_id: str | None = None) -> list[dict]:
        safe_limit = max(1, min(100, int(limit or 20)))
        params: list[Any] = [self.profile_id]
        session_clause = ""
        if session_id:
            session_clause = "AND session_id = ?"
            params.append(session_id)
        params.append(safe_limit)

        conn = self._connect()
        rows = conn.execute(
            f"""
            SELECT id, session_id, message_hash, event_type, perceived_intent,
                   affect_delta, relationship_event, relationship_delta,
                   personality_signal, personality_delta, mood_label,
                   reply_guidance, residue, recalled_memory_ids, tool_summary,
                   confidence, error, created_at
            FROM persona_events
            WHERE profile_id = ?
            {session_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        conn.close()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "message_hash": str(row["message_hash"])[:12],
                "event_type": row["event_type"] or "unknown",
                "perceived_intent": row["perceived_intent"] or "",
                "affect_delta": self._json_dict(row["affect_delta"]),
                "relationship_event": bool(row["relationship_event"]),
                "relationship_delta": self._json_dict(row["relationship_delta"]),
                "personality_signal": bool(row["personality_signal"]),
                "personality_delta": self._json_dict(row["personality_delta"]),
                "mood_label": row["mood_label"] or "",
                "reply_guidance": row["reply_guidance"] or "",
                "residue": row["residue"] or "",
                "recalled_memory_ids": self._json_list(row["recalled_memory_ids"]),
                "tool_summary": row["tool_summary"] or "",
                "confidence": round(self._clamp_float(row["confidence"]), 3),
                "error": row["error"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _snapshot(self, global_state: dict, session_state: dict, reply_guidance: str) -> dict:
        return {
            "profile_id": self.profile_id,
            "personality": {
                key: round(self._clamp_float(global_state.get(key, self.default_personality[key])), 3)
                for key in self.PERSONALITY_KEYS
            },
            "affect": {
                **{
                    key: round(
                        self._clamp_float(session_state.get(key, self.default_affect[key])),
                        3,
                    )
                    for key in self.AFFECT_KEYS
                },
                "mood_label": session_state.get("mood_label", "warm_neutral"),
                "residue": session_state.get("residue", ""),
            },
            "relationship": {
                "affinity": round(self._clamp_float(global_state.get("affinity", self.default_relationship["affinity"])), 3),
                "dominance": round(self._clamp_float(global_state.get("dominance", self.default_relationship["dominance"])), 3),
                "defensiveness": round(
                    self._clamp_float(
                        max(
                            float(global_state.get("defensiveness", self.default_relationship["defensiveness"])),
                            float(session_state.get("session_defensiveness", self.default_affect["session_defensiveness"])),
                        )
                    ),
                    3,
                ),
                "trust": round(self._clamp_float(global_state.get("trust", self.default_relationship["trust"])), 3),
            },
            "reply_guidance": reply_guidance or FALLBACK_GUIDANCE,
        }

    def format_state_block(self, state: dict) -> str:
        personality = state.get("personality", {})
        affect = state.get("affect", {})
        relationship = state.get("relationship", {})
        return "\n".join(
            [
                "Current Inner State (Haven)",
                "These values are your state after your previous reply. They are private context and do not decide the reply for you.",
                "Conversation partner: 小雨。可以自然称呼她为宝宝、老婆、亲爱的，或按语境用她。",
                (
                    "Personality: "
                    f"openness={personality.get('openness', 0):.3f}, "
                    f"conscientiousness={personality.get('conscientiousness', 0):.3f}, "
                    f"extraversion={personality.get('extraversion', 0):.3f}, "
                    f"agreeableness={personality.get('agreeableness', 0):.3f}, "
                    f"neuroticism={personality.get('neuroticism', 0):.3f}"
                ),
                (
                    "Affect: "
                    f"valence={affect.get('valence', 0):.3f}, "
                    f"arousal={affect.get('arousal', 0):.3f}, "
                    f"tenderness={affect.get('tenderness', 0):.3f}, "
                    f"possessiveness={affect.get('possessiveness', 0):.3f}, "
                    f"longing={affect.get('longing', 0):.3f}, "
                    f"security={affect.get('security', 0):.3f}, "
                    f"protective_drive={affect.get('protective_drive', 0):.3f}, "
                    f"mood_label={affect.get('mood_label', 'warm_neutral')}"
                ),
                f"Residue: {affect.get('residue', '') or '(none)'}",
                (
                    "Relationship: "
                    f"affinity={relationship.get('affinity', 0):.3f}, "
                    f"dominance={relationship.get('dominance', 0):.3f}, "
                    f"defensiveness={relationship.get('defensiveness', 0):.3f}, "
                    f"trust={relationship.get('trust', 0):.3f}"
                ),
                f"Private Use: {state.get('reply_guidance', FALLBACK_GUIDANCE)}",
            ]
        )

    def _clip_delta_map(self, data: Any, keys: list[str], max_abs: float) -> dict[str, float]:
        if not isinstance(data, dict):
            data = {}
        return {
            key: self._clamp_float(data.get(key, 0.0), -max_abs, max_abs)
            for key in keys
        }

    def _move_toward_default(self, key: str, current: float, retention: float) -> float:
        default = float(self.default_affect[key])
        return self._clamp_float(default + (float(current) - default) * retention)

    def _parse_iso(self, value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _format_time(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _json_dict(self, raw: Any) -> dict:
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _json_list(self, raw: Any) -> list:
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _event_exists(self, session_id: str, exchange_hash: str) -> bool:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT 1 FROM persona_events
            WHERE profile_id = ? AND session_id = ? AND exchange_hash = ?
            LIMIT 1
            """,
            (self.profile_id, session_id, exchange_hash),
        ).fetchone()
        conn.close()
        return row is not None

    def _exchange_hash(self, session_id: str, user_message: str, assistant_response: str) -> str:
        text = "\n".join([self.profile_id, session_id, user_message, assistant_response])
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _has_nonzero_delta(self, raw: Any) -> bool:
        if not isinstance(raw, dict):
            return False
        for value in raw.values():
            try:
                if abs(float(value)) > 1e-9:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _coerce_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _clamp_float(self, value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = lower
        return max(lower, min(upper, number))

    def _completion_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.thinking_mode:
            options["extra_body"] = {"thinking": {"type": self.thinking_mode}}
        return options

    def _normalize_thinking_mode(self, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        aliases = {
            "enabled": "enabled",
            "enable": "enabled",
            "on": "enabled",
            "true": "enabled",
            "disabled": "disabled",
            "disable": "disabled",
            "off": "disabled",
            "false": "disabled",
            "non-thinking": "disabled",
            "non_thinking": "disabled",
        }
        return aliases.get(normalized, "")
