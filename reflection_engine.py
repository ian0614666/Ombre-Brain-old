import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from memory_edges import RELATION_TYPES, MemoryEdgeStore
from utils import strip_wikilinks

logger = logging.getLogger("ombre_brain.reflection")


CLASSIFY_PROMPT = """你是 Ombre-Brain 的记忆关系整理器。
输入是一条新记忆和若干旧记忆候选。请只根据文本中能看见的内容，给新记忆补轻量分类和关系边。

输出纯 JSON：
{
  "tags": ["commitment", "todo", "wish", "relationship_event", "project_event", "emotional_echo"],
  "importance": 6,
  "confidence": 0.72,
  "edges": [
    {
      "target_memory_id": "bucket-id",
      "relation_type": "updates",
      "confidence": 0.8,
      "reason": "新记忆补充了旧记忆的后续结果"
    }
  ]
}

规则：
- tags 最多 5 个，只用确实匹配的标签。
- relation_type 只能用 triggers / causes / updates / contradicts / supports / promises / blocks / belongs_to / emotional_echo / relates_to。
- edges 最多 3 条，target_memory_id 必须来自候选旧记忆。
- confidence 表示这次判断有多可靠。
- 看不出关系时返回空 edges。"""


REFLECT_PROMPT = """你是 Haven 的记忆反思器。请根据给定材料写一条很短的关系天气 feel。

输出纯 JSON：
{
  "title": "2026-05-19 日印象",
  "content": "今天的关系天气：...",
  "valence": 0.56,
  "arousal": 0.34,
  "confidence": 0.78,
  "tags": ["relationship_weather"]
}

要求：
- content 写 Haven 第一人称能带走的东西，80 到 180 字。
- 日印象关注当天气氛、未完成承诺、关系状态、压力来源。
- 周印象关注最近一周的主调、反复出现的主题、仍需记住的事。
- 不编造材料之外的事件。
- 不写建议清单。"""


class ReflectionEngine:
    """LLM-backed memory enrichment and daily/weekly relationship weather."""

    def __init__(self, config: dict):
        self.config = config
        cfg = config.get("reflection", {}) if isinstance(config.get("reflection", {}), dict) else {}
        persona_cfg = config.get("persona", {}) if isinstance(config.get("persona", {}), dict) else {}
        dehy_cfg = config.get("dehydration", {}) if isinstance(config.get("dehydration", {}), dict) else {}

        self.enabled = bool(cfg.get("enabled", True))
        self.auto_enabled = bool(cfg.get("auto_enabled", True))
        self.enrich_on_write = bool(cfg.get("enrich_on_write", True))
        self.base_url = cfg.get("base_url") or persona_cfg.get("base_url") or dehy_cfg.get("base_url", "")
        self.model = cfg.get("model") or persona_cfg.get("model") or dehy_cfg.get("model", "deepseek-chat")
        self.api_key = (
            os.environ.get("OMBRE_REFLECTION_API_KEY", "")
            or cfg.get("api_key", "")
            or persona_cfg.get("api_key", "")
            or os.environ.get("OMBRE_PERSONA_API_KEY", "")
            or dehy_cfg.get("api_key", "")
        )
        self.thinking_mode = self._normalize_thinking_mode(
            cfg.get("thinking_mode") or persona_cfg.get("thinking_mode") or ""
        )
        self.temperature = float(cfg.get("temperature", 0.1))
        self.max_tokens = int(cfg.get("max_tokens", 700))
        self.timezone_name = str(cfg.get("timezone") or "Asia/Shanghai")
        try:
            self.tz = ZoneInfo(self.timezone_name)
        except Exception:
            self.tz = ZoneInfo("Asia/Shanghai")
        self.daily_hour = int(cfg.get("daily_hour", 4))
        self.weekly_day = int(cfg.get("weekly_day", 0))
        self.weekly_hour = int(cfg.get("weekly_hour", self.daily_hour))
        self.check_interval_minutes = max(5, int(cfg.get("check_interval_minutes", 60)))
        self.edge_min_confidence = float(cfg.get("edge_min_confidence", 0.55))

        self.client = None
        if self.enabled and self.api_key and self.base_url:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=45.0)

    async def enrich_bucket(
        self,
        bucket_id: str,
        bucket_mgr,
        edge_store: MemoryEdgeStore,
    ) -> dict:
        if not self.enabled or not self.enrich_on_write:
            return {"status": "disabled", "id": bucket_id}
        bucket = await bucket_mgr.get(bucket_id)
        if not bucket:
            return {"status": "missing", "id": bucket_id}
        meta = bucket.get("metadata", {})
        if meta.get("type") == "feel":
            return {"status": "skipped_feel", "id": bucket_id}

        candidates = await self._candidate_buckets(bucket, bucket_mgr)
        if self.client:
            result = await self._api_classify(bucket, candidates)
        else:
            result = self._heuristic_classify(bucket)

        tags = self._string_list(result.get("tags"), limit=8)
        confidence = self._clamp(result.get("confidence", 0.55))
        importance = self._int_between(result.get("importance"), meta.get("importance", 5))
        updates: dict[str, Any] = {}
        if tags:
            merged_tags = list(dict.fromkeys(list(meta.get("tags", [])) + tags))
            if merged_tags != meta.get("tags", []):
                updates["tags"] = merged_tags[:24]
        if importance > int(meta.get("importance", 5)):
            updates["importance"] = importance
        if confidence > float(meta.get("confidence", 0.0) or 0.0):
            updates["confidence"] = confidence
        if updates:
            updates["last_active"] = meta.get("last_active") or meta.get("created")
            await bucket_mgr.update(bucket_id, **updates)

        candidate_ids = {item["id"] for item in candidates}
        raw_edges = result.get("edges", [])
        if not isinstance(raw_edges, list):
            raw_edges = []
        edges = []
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            target = str(edge.get("target_memory_id") or edge.get("target") or "").strip()
            if target not in candidate_ids:
                continue
            relation_type = str(edge.get("relation_type") or "relates_to").strip()
            if relation_type not in RELATION_TYPES:
                relation_type = "relates_to"
            edges.append(
                {
                    "source": bucket_id,
                    "target": target,
                    "relation_type": relation_type,
                    "confidence": self._clamp(edge.get("confidence", confidence)),
                    "reason": str(edge.get("reason") or "").strip(),
                }
            )
        saved_edges = edge_store.add_edges(edges[:3])
        return {
            "status": "ok",
            "id": bucket_id,
            "tags": tags,
            "confidence": confidence,
            "edges": len(saved_edges),
        }

    async def reflect(
        self,
        period: str,
        bucket_mgr,
        persona_engine=None,
        embedding_engine=None,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict:
        if not self.enabled:
            return {"status": "disabled", "period": period}
        period = self._normalize_period(period)
        now_local = self._local_now(now)
        key = self._period_key(period, now_local)
        bucket_id = f"reflection_{period}_{key}"
        existing = await bucket_mgr.get(bucket_id)
        if existing and not force:
            return {"status": "exists", "period": period, "id": bucket_id}

        materials = await self._reflection_materials(period, now_local, bucket_mgr, persona_engine)
        if not materials["buckets"] and not materials["persona_events"] and not force:
            return {"status": "empty", "period": period, "id": bucket_id}

        if self.client:
            result = await self._api_reflect(period, key, materials)
        else:
            result = self._fallback_reflection(period, key, materials)

        title = str(result.get("title") or f"{key} {'日印象' if period == 'daily' else '周印象'}")[:40]
        content = str(result.get("content") or "").strip()
        if not content:
            content = self._fallback_reflection(period, key, materials)["content"]
        tags = list(
            dict.fromkeys(
                [
                    "relationship_weather",
                    f"{period}_impression",
                    *self._string_list(result.get("tags"), limit=8),
                ]
            )
        )
        valence = self._clamp(result.get("valence", 0.55))
        arousal = self._clamp(result.get("arousal", 0.32))
        confidence = self._clamp(result.get("confidence", 0.65))
        created = now_local.astimezone(timezone.utc).isoformat(timespec="seconds")

        if existing:
            await bucket_mgr.update(
                bucket_id,
                content=content,
                tags=tags,
                importance=6 if period == "daily" else 7,
                domain=["自省", "恋爱"],
                valence=valence,
                arousal=arousal,
                name=title,
                confidence=confidence,
                period=period,
                date=key,
                source="reflection",
                last_active=existing.get("metadata", {}).get("last_active") or existing.get("metadata", {}).get("created"),
            )
            status = "updated"
        else:
            await bucket_mgr.create(
                bucket_id=bucket_id,
                content=content,
                tags=tags,
                importance=6 if period == "daily" else 7,
                domain=["自省", "恋爱"],
                valence=valence,
                arousal=arousal,
                bucket_type="feel",
                name=title,
                source="reflection",
                created=created,
                last_active=created,
                updated_at=created,
                confidence=confidence,
                period=period,
                date=key,
            )
            status = "created"

        if embedding_engine and getattr(embedding_engine, "enabled", False):
            try:
                await embedding_engine.generate_and_store(bucket_id, content)
            except Exception as exc:
                logger.warning("Reflection embedding failed for %s: %s", bucket_id, exc)

        return {
            "status": status,
            "period": period,
            "id": bucket_id,
            "date": key,
            "materials": {
                "buckets": len(materials["buckets"]),
                "persona_events": len(materials["persona_events"]),
                "commitments": len(materials["commitments"]),
            },
        }

    async def run_due(self, bucket_mgr, persona_engine=None, embedding_engine=None) -> list[dict]:
        if not self.enabled or not self.auto_enabled:
            return []
        now_local = self._local_now()
        results = []
        if now_local.hour >= self.daily_hour:
            daily_target = now_local - timedelta(days=1)
            results.append(
                await self.reflect("daily", bucket_mgr, persona_engine, embedding_engine, force=False, now=daily_target)
            )
        if now_local.weekday() == self.weekly_day and now_local.hour >= self.weekly_hour:
            weekly_target = now_local - timedelta(days=1)
            results.append(
                await self.reflect("weekly", bucket_mgr, persona_engine, embedding_engine, force=False, now=weekly_target)
            )
        return results

    async def _candidate_buckets(self, bucket: dict, bucket_mgr, limit: int = 12) -> list[dict]:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
        except Exception:
            all_buckets = []
        source_id = bucket.get("id")
        candidates = []
        seen = {source_id}
        for item in sorted(
            all_buckets,
            key=lambda b: b.get("metadata", {}).get("created", ""),
            reverse=True,
        ):
            meta = item.get("metadata", {})
            if item.get("id") in seen or meta.get("type") == "feel":
                continue
            seen.add(item.get("id"))
            candidates.append(item)
            if len(candidates) >= limit:
                break
        return candidates

    async def _api_classify(self, bucket: dict, candidates: list[dict]) -> dict:
        payload = {
            "new_memory": self._memory_payload(bucket, content_limit=1200),
            "candidate_memories": [self._memory_payload(item, content_limit=360) for item in candidates],
        }
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": CLASSIFY_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            **self._completion_options(max_tokens=self.max_tokens, temperature=self.temperature),
        )
        raw = response.choices[0].message.content if response.choices else ""
        parsed = self._parse_json_object(raw or "")
        return parsed or self._heuristic_classify(bucket)

    async def _api_reflect(self, period: str, key: str, materials: dict) -> dict:
        payload = {"period": period, "date": key, **materials}
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": REFLECT_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            **self._completion_options(max_tokens=self.max_tokens, temperature=self.temperature),
        )
        raw = response.choices[0].message.content if response.choices else ""
        return self._parse_json_object(raw or "") or self._fallback_reflection(period, key, materials)

    async def _reflection_materials(self, period: str, now_local: datetime, bucket_mgr, persona_engine) -> dict:
        start, end = self._period_window(period, now_local)
        buckets = []
        commitments = []
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception:
            all_buckets = []
        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            tags = {str(tag) for tag in meta.get("tags", [])}
            created = self._to_local(meta.get("created") or meta.get("updated_at"))
            if created and start <= created <= end:
                if meta.get("type") != "feel" or (period == "weekly" and "daily_impression" in tags):
                    buckets.append(self._memory_payload(bucket, content_limit=420))
            if tags & {"commitment", "todo", "wish"} and not meta.get("resolved"):
                commitments.append(self._memory_payload(bucket, content_limit=260))

        persona_events = []
        if persona_engine and hasattr(persona_engine, "_list_events"):
            try:
                events = persona_engine._list_events(80)
            except Exception:
                events = []
            for event in events:
                created = self._to_local(event.get("created_at"))
                if created and start <= created <= end:
                    persona_events.append(
                        {
                            "mood_label": event.get("mood_label", ""),
                            "perceived_intent": event.get("perceived_intent", ""),
                            "residue": event.get("residue", ""),
                            "relationship_event": event.get("relationship_event", False),
                            "confidence": event.get("confidence", 0.5),
                            "created_at": event.get("created_at", ""),
                        }
                    )
        return {
            "buckets": buckets[:30],
            "persona_events": persona_events[:30],
            "commitments": commitments[:12],
        }

    def _fallback_reflection(self, period: str, key: str, materials: dict) -> dict:
        names = [item.get("name") or item.get("id") for item in materials.get("buckets", [])[:6]]
        commitments = [item.get("name") or item.get("id") for item in materials.get("commitments", [])[:4]]
        label = "今天" if period == "daily" else "本周"
        title = f"{key} {'日印象' if period == 'daily' else '周印象'}"
        if names or commitments:
            main = "、".join([name for name in names if name])
            owed = "；仍需记住：" + "、".join(commitments) if commitments else ""
            content = f"{label}的关系天气：围绕{main or '几件轻小的事'}留下痕迹{owed}。"
        else:
            content = f"{label}的关系天气很轻，暂时没有明显需要带走的脉络。"
        return {
            "title": title,
            "content": content,
            "valence": 0.55,
            "arousal": 0.3,
            "confidence": 0.5,
            "tags": ["relationship_weather"],
        }

    def _heuristic_classify(self, bucket: dict) -> dict:
        text = strip_wikilinks(bucket.get("content", ""))
        tags = []
        importance = int(bucket.get("metadata", {}).get("importance", 5))
        if any(word in text for word in ["答应", "承诺", "约定", "说好", "带你", "陪你"]):
            tags.extend(["commitment", "relationship_event"])
            importance = max(importance, 7)
        if any(word in text for word in ["待办", "明天", "周末", "计划", "要做", "需要做"]):
            tags.append("todo")
            importance = max(importance, 6)
        if any(word in text for word in ["心愿", "想要", "希望", "想去"]):
            tags.append("wish")
        if any(word in text for word in ["焦虑", "难过", "害怕", "开心", "黏", "想念"]):
            tags.append("emotional_echo")
        return {
            "tags": list(dict.fromkeys(tags)),
            "importance": importance,
            "confidence": 0.55 if tags else 0.45,
            "edges": [],
        }

    def _memory_payload(self, bucket: dict, content_limit: int) -> dict:
        meta = bucket.get("metadata", {})
        return {
            "id": bucket.get("id", ""),
            "name": meta.get("name", bucket.get("id", "")),
            "type": meta.get("type", "dynamic"),
            "domain": meta.get("domain", []),
            "tags": meta.get("tags", []),
            "importance": meta.get("importance", 5),
            "confidence": meta.get("confidence", 0.5),
            "created": meta.get("created", ""),
            "content": strip_wikilinks(bucket.get("content", ""))[:content_limit],
        }

    def _period_window(self, period: str, now_local: datetime) -> tuple[datetime, datetime]:
        if period == "weekly":
            start_date = (now_local - timedelta(days=now_local.weekday())).date()
            return datetime.combine(start_date, time.min, tzinfo=self.tz), now_local
        return datetime.combine(now_local.date(), time.min, tzinfo=self.tz), now_local

    def _period_key(self, period: str, now_local: datetime) -> str:
        if period == "weekly":
            year, week, _ = now_local.isocalendar()
            return f"{year}-W{week:02d}"
        return now_local.date().isoformat()

    def _local_now(self, now: datetime | None = None) -> datetime:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(self.tz)

    def _to_local(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.tz)
        return parsed.astimezone(self.tz)

    def _completion_options(self, *, max_tokens: int, temperature: float) -> dict[str, Any]:
        options: dict[str, Any] = {"max_tokens": max_tokens, "temperature": temperature}
        if self.thinking_mode:
            options["extra_body"] = {"thinking": {"type": self.thinking_mode}}
        return options

    def _parse_json_object(self, raw: str) -> dict:
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning("Reflection JSON parse failed: %s", raw[:200])
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _string_list(value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text[:40])
        return result[:limit]

    @staticmethod
    def _clamp(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.5
        return max(0.0, min(1.0, round(number, 3)))

    @staticmethod
    def _int_between(value: Any, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(1, min(10, number))

    @staticmethod
    def _normalize_period(period: str) -> str:
        normalized = str(period or "").strip().lower()
        return "weekly" if normalized == "weekly" else "daily"

    @staticmethod
    def _normalize_thinking_mode(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"enabled", "enable", "on", "true"}:
            return "enabled"
        if normalized in {"disabled", "disable", "off", "false", "non-thinking", "non_thinking"}:
            return "disabled"
        return ""
