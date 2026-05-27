# ============================================================
# Module: Memory Import Engine (import_memory.py)
# 模块：历史记忆导入引擎
#
# Imports conversation history from various platforms into OB.
# 将各平台对话历史导入 OB 记忆系统。
#
# Supports: Claude JSON, ChatGPT export, DeepSeek, Markdown, plain text
# 支持格式：Claude JSON、ChatGPT 导出、DeepSeek、Markdown、纯文本
#
# Features:
#   - Chunked processing with resume support
#   - Progress persistence (import_state.json)
#   - Raw preservation mode for special contexts
#   - Post-import frequency pattern detection
# ============================================================

import os
import json
import hashlib
import logging
import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import jieba
from rapidfuzz import fuzz

from utils import bucket_text_for_embedding, count_tokens_approx, now_iso

logger = logging.getLogger("ombre_brain.import")


# ============================================================
# Format Parsers — normalize any format to conversation turns
# 格式解析器 — 将任意格式标准化为对话轮次
# ============================================================

_MARKDOWN_ROLE_RE = re.compile(
    r"^\s*(?:>\s*)?(?:[-*+]\s*)?(?:#{1,6}\s*)?(?:\*\*)?([A-Za-z0-9_\-\u4e00-\u9fff]+)(?:\*\*)?\s*[:：]\s*(.*)$"
)
_MARKDOWN_USER_LABELS = {
    "human",
    "user",
    "me",
    "rain",
    "你",
    "我",
    "用户",
    "人类",
    "小雨",
}
_MARKDOWN_ASSISTANT_LABELS = {
    "assistant",
    "claude",
    "ai",
    "gpt",
    "chatgpt",
    "bot",
    "deepseek",
    "gemini",
    "qwen",
    "haven",
    "助手",
    "模型",
    "ai助手",
}


def _detect_markdown_role_line(line: str) -> tuple[str, str] | None:
    """Return (role, content_after_prefix) for simple role-prefixed Markdown lines."""
    match = _MARKDOWN_ROLE_RE.match(line)
    if not match:
        return None
    label = match.group(1).strip().lower()
    content_after = match.group(2).strip()
    if content_after.startswith("**"):
        content_after = content_after[2:].lstrip()
    if label in _MARKDOWN_USER_LABELS:
        return "user", content_after
    if label in _MARKDOWN_ASSISTANT_LABELS:
        return "assistant", content_after
    return None

def _parse_claude_json(data: dict | list) -> list[dict]:
    """Parse Claude.ai export JSON → [{role, content, timestamp}, ...]"""
    turns = []
    conversations = data if isinstance(data, list) else [data]
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        messages = conv.get("chat_messages", conv.get("messages", []))
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("text", msg.get("content", ""))
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            elif isinstance(content, dict):
                content = " ".join(
                    str(p.get("text", p)) if isinstance(p, dict) else str(p)
                    for p in content.get("parts", [])
                    if p
                )
            elif not isinstance(content, str):
                content = str(content)
            if not content or not content.strip():
                continue
            role = msg.get("sender", msg.get("role", "user"))
            ts = msg.get("created_at", msg.get("timestamp", ""))
            turns.append({"role": role, "content": content.strip(), "timestamp": ts})
    return turns


def _parse_chatgpt_json(data: list | dict) -> list[dict]:
    """Parse ChatGPT export JSON → [{role, content, timestamp}, ...]"""
    turns = []
    conversations = data if isinstance(data, list) else [data]
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get("mapping", {})
        if isinstance(mapping, dict) and mapping:
            # ChatGPT uses a tree structure with mapping
            sorted_nodes = sorted(
                [node for node in mapping.values() if isinstance(node, dict)],
                key=lambda n: (n.get("message") or {}).get("create_time", 0) or 0,
            )
            for node in sorted_nodes:
                msg = node.get("message")
                if not msg or not isinstance(msg, dict):
                    continue
                content_obj = msg.get("content", {})
                if isinstance(content_obj, dict):
                    content_parts = content_obj.get("parts", [])
                    content = " ".join(str(p) for p in content_parts if p)
                elif isinstance(content_obj, str):
                    content = content_obj
                else:
                    content = ""
                if not isinstance(content, str):
                    content = str(content)
                if not content.strip():
                    continue
                author = msg.get("author", {})
                role = author.get("role", "user") if isinstance(author, dict) else "user"
                ts = msg.get("create_time", "")
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts).isoformat()
                turns.append({"role": role, "content": content.strip(), "timestamp": str(ts)})
        else:
            # Simpler format: list of messages
            messages = conv.get("messages", [])
            if not isinstance(messages, list):
                continue
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", msg.get("text", ""))
                if isinstance(content, dict):
                    content = " ".join(str(p) for p in content.get("parts", []))
                elif isinstance(content, list):
                    content = " ".join(
                        str(p.get("text", p)) if isinstance(p, dict) else str(p)
                        for p in content
                        if p
                    )
                elif not isinstance(content, str):
                    content = str(content)
                if not content or not content.strip():
                    continue
                author = msg.get("author", {})
                role = msg.get("role") or (author.get("role") if isinstance(author, dict) else None) or "user"
                ts = msg.get("timestamp", msg.get("create_time", ""))
                turns.append({"role": role, "content": content.strip(), "timestamp": str(ts)})
    return turns


def _parse_markdown(text: str) -> list[dict]:
    """Parse Markdown/plain text → [{role, content, timestamp}, ...]"""
    # Try to detect conversation patterns
    lines = text.split("\n")
    turns = []
    current_role = "user"
    current_content = []

    def append_current_turn():
        content = "\n".join(current_content).strip()
        if content:
            turns.append({"role": current_role, "content": content, "timestamp": ""})

    for line in lines:
        stripped = line.strip()
        role_line = _detect_markdown_role_line(stripped)
        if role_line:
            if current_content:
                append_current_turn()
            current_role, content_after = role_line
            current_content = [content_after] if content_after else []
        else:
            current_content.append(line)

    if current_content:
        append_current_turn()

    # If no role patterns detected, treat entire text as one big chunk
    if not turns:
        turns = [{"role": "user", "content": text.strip(), "timestamp": ""}]

    return turns


def detect_and_parse(raw_content: str, filename: str = "") -> list[dict]:
    """
    Auto-detect format and parse to normalized turns.
    自动检测格式并解析为标准化的对话轮次。
    """
    ext = Path(filename).suffix.lower() if filename else ""

    # Try JSON first
    if ext in (".json", "") or raw_content.strip().startswith(("{", "[")):
        try:
            data = json.loads(raw_content)
            # Detect Claude vs ChatGPT format
            if isinstance(data, list):
                sample = data[0] if data else {}
            else:
                sample = data

            if isinstance(sample, dict):
                if "chat_messages" in sample:
                    return _parse_claude_json(data)
                if "mapping" in sample:
                    return _parse_chatgpt_json(data)
                if "messages" in sample:
                    # Could be either — try ChatGPT first, fall back to Claude
                    msgs = sample["messages"]
                    if msgs and isinstance(msgs[0], dict) and "content" in msgs[0]:
                        if isinstance(msgs[0]["content"], dict):
                            return _parse_chatgpt_json(data)
                    return _parse_claude_json(data)
                # Single conversation object with role/content messages
                if "role" in sample and "content" in sample:
                    return _parse_claude_json(data)
        except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
            pass

    # Fall back to markdown/text
    return _parse_markdown(raw_content)


# ============================================================
# Chunking — split turns into ~10k token windows
# 分窗 — 按对话轮次边界切为 ~10k token 窗口
# ============================================================

_OVERLAP_CONTEXT_NOTICE = "[上下文提示] 以下是上一段结尾，只用于理解前后关系，请不要从这里单独提取记忆。"
_CURRENT_SEGMENT_NOTICE = "[本段内容]"
_IMPORT_DUPLICATE_SIMILARITY = 88.0


def _normalize_import_text(text: str) -> str:
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", str(text or ""))
    text = re.sub(r"[\s\u3000]+", "", text.lower())
    return re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "", text)


def _import_similarity_text(text: str) -> str:
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", str(text or "").lower())
    text = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", " ", text)
    return " ".join(token for token in jieba.lcut(text) if token.strip())


def _import_content_hash(text: str) -> str:
    normalized = _normalize_import_text(text)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _tail_for_overlap(text: str, overlap_tokens: int) -> str:
    lines = text.splitlines() or [text]
    tail: list[str] = []
    current_tokens = 0
    max_chars = max(40, int(overlap_tokens / 1.8))

    for line in reversed(lines):
        line_tokens = count_tokens_approx(line)
        if not tail and line_tokens > overlap_tokens:
            return line[-max_chars:].strip()
        if tail and current_tokens + line_tokens > overlap_tokens:
            break
        tail.insert(0, line)
        current_tokens += line_tokens

    return "\n".join(tail).strip()


def _split_oversized_turn(role_label: str, content: str, target_tokens: int) -> list[str]:
    """Split a single very long turn into model-sized chunks with small context overlap."""
    prefix = f"[{role_label}] "
    segments: list[str] = []
    current_lines: list[str] = []
    current_tokens = count_tokens_approx(prefix)
    content_budget = max(80, int(target_tokens * 0.85))
    overlap_tokens = max(20, int(target_tokens * 0.12))
    max_chars = max(80, int(content_budget / 1.8))

    def flush_current():
        nonlocal current_lines, current_tokens
        body = "\n".join(current_lines).strip()
        if body:
            segments.append(body)
        current_lines = []
        current_tokens = count_tokens_approx(prefix)

    for line in content.splitlines() or [content]:
        line_tokens = count_tokens_approx(line)
        if line_tokens > content_budget:
            flush_current()
            for start in range(0, len(line), max_chars):
                segment = line[start:start + max_chars].strip()
                if segment:
                    segments.append(segment)
            continue

        if current_lines and current_tokens + line_tokens > content_budget:
            flush_current()
        current_lines.append(line)
        current_tokens += line_tokens

    flush_current()

    pieces: list[str] = []
    previous_tail = ""
    for segment in segments:
        body = prefix + segment
        if previous_tail:
            pieces.append(
                f"{_OVERLAP_CONTEXT_NOTICE}\n"
                f"{prefix}{previous_tail}\n\n"
                f"{_CURRENT_SEGMENT_NOTICE}\n"
                f"{body}"
            )
        else:
            pieces.append(body)
        previous_tail = _tail_for_overlap(segment, overlap_tokens)

    return pieces


def chunk_turns(turns: list[dict], target_tokens: int = 10000) -> list[dict]:
    """
    Group conversation turns into chunks of ~target_tokens.
    Returns list of {content, timestamp_start, timestamp_end, turn_count}.
    按对话轮次边界将对话分为 ~target_tokens 大小的窗口。
    """
    chunks = []
    current_lines = []
    current_tokens = 0
    first_ts = ""
    last_ts = ""
    turn_count = 0

    for turn in turns:
        role_label = "用户" if turn["role"] in ("user", "human") else "AI"
        line = f"[{role_label}] {turn['content']}"
        line_tokens = count_tokens_approx(line)

        # If single turn exceeds target, split it
        if line_tokens > target_tokens * 1.5:
            # Flush current
            if current_lines:
                chunks.append({
                    "content": "\n".join(current_lines),
                    "timestamp_start": first_ts,
                    "timestamp_end": last_ts,
                    "turn_count": turn_count,
                })
                current_lines = []
                current_tokens = 0
                turn_count = 0
                first_ts = ""

            for split_line in _split_oversized_turn(role_label, turn["content"], target_tokens):
                chunks.append({
                    "content": split_line,
                    "timestamp_start": turn.get("timestamp", ""),
                    "timestamp_end": turn.get("timestamp", ""),
                    "turn_count": 1,
                })
            continue

        if current_tokens + line_tokens > target_tokens and current_lines:
            chunks.append({
                "content": "\n".join(current_lines),
                "timestamp_start": first_ts,
                "timestamp_end": last_ts,
                "turn_count": turn_count,
            })
            current_lines = []
            current_tokens = 0
            turn_count = 0
            first_ts = ""

        if not first_ts:
            first_ts = turn.get("timestamp", "")
        last_ts = turn.get("timestamp", "")
        current_lines.append(line)
        current_tokens += line_tokens
        turn_count += 1

    if current_lines:
        chunks.append({
            "content": "\n".join(current_lines),
            "timestamp_start": first_ts,
            "timestamp_end": last_ts,
            "turn_count": turn_count,
        })

    return chunks


# ============================================================
# Import State — persistent progress tracking
# 导入状态 — 持久化进度追踪
# ============================================================

class ImportState:
    """Manages import progress with file-based persistence."""

    def __init__(self, state_dir: str):
        self.state_file = os.path.join(state_dir, "import_state.json")
        self.data = {
            "source_file": "",
            "source_hash": "",
            "total_chunks": 0,
            "processed": 0,
            "api_calls": 0,
            "memories_created": 0,
            "memories_merged": 0,
            "memories_raw": 0,
            "errors": [],
            "status": "idle",  # idle | running | paused | completed | error
            "started_at": "",
            "updated_at": "",
        }

    def load(self) -> bool:
        """Load state from file. Returns True if state exists."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.data.update(saved)
                return True
            except (json.JSONDecodeError, OSError):
                return False
        return False

    def save(self):
        """Persist state to file."""
        self.data["updated_at"] = now_iso()
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)

    def reset(self, source_file: str, source_hash: str, total_chunks: int):
        """Reset state for a new import."""
        self.data = {
            "source_file": source_file,
            "source_hash": source_hash,
            "total_chunks": total_chunks,
            "processed": 0,
            "api_calls": 0,
            "memories_created": 0,
            "memories_merged": 0,
            "memories_raw": 0,
            "errors": [],
            "status": "running",
            "started_at": now_iso(),
            "updated_at": now_iso(),
        }

    @property
    def can_resume(self) -> bool:
        return self.data["status"] in ("paused", "running") and self.data["processed"] < self.data["total_chunks"]

    def to_dict(self) -> dict:
        return dict(self.data)


# ============================================================
# Import extraction prompt
# 导入提取提示词
# ============================================================

IMPORT_EXTRACT_PROMPT = """你是一个对话记忆提取专家。从以下对话片段中提取值得长期记住的信息。

提取规则：
1. 提取用户的事实、偏好、习惯、重要事件、情感时刻
2. 同一话题的零散信息整合为一条记忆
3. 过滤掉纯技术调试输出、代码块、重复问答、无意义寒暄
4. 如果对话中有特殊暗号、仪式性行为、关键承诺等，标记 preserve_raw=true
5. 如果内容是用户和AI之间的习惯性互动模式（例如打招呼方式、告别习惯），标记 is_pattern=true
6. 每条记忆不少于30字
7. 总条目数控制在 0~10 个（没有值得记的就返回空数组）
8. 在 content 中对人名、地名、专有名词用 [[双链]] 标记
9. 如果片段里出现「[上下文提示]」，该部分只是上一段尾巴，只用于理解前后关系；不要从上下文提示本身单独提取记忆，除非同一事实在「[本段内容]」里继续出现

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "整理后的内容",
    "domain": ["主题域1"],
    "valence": 0.7,
    "arousal": 0.4,
    "tags": ["核心词1", "核心词2", "扩展词1"],
    "importance": 5,
    "preserve_raw": false,
    "is_pattern": false
  }
]

主题域可选（选 1~2 个）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]

importance: 1-10
valence: 0~1（0=消极, 0.5=中性, 1=积极）
arousal: 0~1（0=平静, 0.5=普通, 1=激动）
preserve_raw: true = 特殊情境/暗号/仪式，保留原文不摘要
is_pattern: true = 反复出现的习惯性行为模式"""


# ============================================================
# Import Engine — core processing logic
# 导入引擎 — 核心处理逻辑
# ============================================================

class ImportEngine:
    """
    Processes conversation history files into OB memory buckets.
    将对话历史文件处理为 OB 记忆桶。
    """

    def __init__(self, config: dict, bucket_mgr, dehydrator, embedding_engine=None):
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.embedding_engine = embedding_engine
        self.state = ImportState(config["buckets_dir"])
        self._paused = False
        self._running = False
        self._chunks: list[dict] = []
        self._seen_import_hashes: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._running

    def pause(self):
        """Request pause — will stop after current chunk finishes."""
        self._paused = True

    def get_status(self) -> dict:
        """Get current import status."""
        return self.state.to_dict()

    async def start(
        self,
        raw_content: str,
        filename: str = "",
        preserve_raw: bool = False,
        resume: bool = False,
    ) -> dict:
        """
        Start or resume an import.
        开始或恢复导入。
        """
        if self._running:
            return {"error": "Import already running"}

        self._running = True
        self._paused = False
        self._seen_import_hashes = set()

        try:
            source_hash = hashlib.sha256(raw_content.encode()).hexdigest()[:16]

            # Check for resume
            if resume and self.state.load() and self.state.can_resume:
                if self.state.data["source_hash"] == source_hash:
                    logger.info(f"Resuming import from chunk {self.state.data['processed']}/{self.state.data['total_chunks']}")
                    # Re-parse and re-chunk to get the same chunks
                    turns = detect_and_parse(raw_content, filename)
                    self._chunks = chunk_turns(turns)
                    self.state.data["status"] = "running"
                    self.state.save()
                    return await self._process_chunks(preserve_raw)
                else:
                    logger.warning("Source file changed, starting fresh import")

            # Fresh import
            turns = detect_and_parse(raw_content, filename)
            if not turns:
                self._running = False
                return {"error": "No conversation turns found in file"}

            self._chunks = chunk_turns(turns)
            if not self._chunks:
                self._running = False
                return {"error": "No processable chunks after splitting"}

            self.state.reset(filename, source_hash, len(self._chunks))
            self.state.save()

            logger.info(f"Starting import: {len(turns)} turns → {len(self._chunks)} chunks")
            return await self._process_chunks(preserve_raw)

        except Exception as e:
            self.state.data["status"] = "error"
            self.state.data["errors"].append(str(e))
            self.state.save()
            self._running = False
            raise

    async def _process_chunks(self, preserve_raw: bool) -> dict:
        """Process chunks from current position."""
        start_idx = self.state.data["processed"]

        for i in range(start_idx, len(self._chunks)):
            if self._paused:
                self.state.data["status"] = "paused"
                self.state.save()
                self._running = False
                logger.info(f"Import paused at chunk {i}/{len(self._chunks)}")
                return self.state.to_dict()

            chunk = self._chunks[i]
            try:
                await self._process_single_chunk(chunk, preserve_raw)
            except Exception as e:
                err_msg = f"Chunk {i}: {str(e)[:200]}"
                logger.warning(f"Import chunk error: {err_msg}")
                if len(self.state.data["errors"]) < 100:
                    self.state.data["errors"].append(err_msg)

            self.state.data["processed"] = i + 1
            # Save progress every chunk
            self.state.save()

        self.state.data["status"] = "completed"
        self.state.save()
        self._running = False
        logger.info(f"Import completed: {self.state.data['memories_created']} created, {self.state.data['memories_merged']} merged")
        return self.state.to_dict()

    async def _process_single_chunk(self, chunk: dict, preserve_raw: bool):
        """Extract memories from a single chunk and store them."""
        content = chunk["content"]
        if not content.strip():
            return

        # --- LLM extraction ---
        try:
            items = await self._extract_memories(content)
            self.state.data["api_calls"] += 1
        except Exception as e:
            logger.warning(f"LLM extraction failed: {e}")
            self.state.data["api_calls"] += 1
            return

        if not items:
            return

        items = self._dedupe_extracted_items(items)
        if not items:
            return

        # --- Store each extracted memory ---
        for item in items:
            try:
                should_preserve = preserve_raw or item.get("preserve_raw", False)
                status = await self._merge_or_create_item(item, preserve_raw=should_preserve)

                if status == "raw":
                    self.state.data["memories_raw"] += 1
                    self.state.data["memories_created"] += 1
                elif status == "created":
                    self.state.data["memories_created"] += 1
                else:
                    self.state.data["memories_merged"] += 1

                # Patch timestamp if available
                if chunk.get("timestamp_start"):
                    # We don't have update support for created, so skip
                    pass

            except Exception as e:
                logger.warning(f"Failed to store memory: {item.get('name', '?')}: {e}")

    async def _extract_memories(self, chunk_content: str) -> list[dict]:
        """Use LLM to extract memories from a conversation chunk."""
        if not self.dehydrator.api_available:
            raise RuntimeError("API not available")

        response = await self.dehydrator.client.chat.completions.create(
            model=self.dehydrator.model,
            messages=[
                {"role": "system", "content": IMPORT_EXTRACT_PROMPT},
                {"role": "user", "content": chunk_content[:12000]},
            ],
            max_tokens=4096,
            temperature=0.0,
        )

        if not response.choices:
            return []

        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return []

        return self._parse_extraction(raw)

    @staticmethod
    def _parse_extraction(raw: str) -> list[dict]:
        """Parse and validate LLM extraction result."""
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            items = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Import extraction JSON parse failed: {raw[:200]}")
            return []

        if not isinstance(items, list):
            return []

        validated = []
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (ValueError, TypeError):
                importance = 5
            try:
                valence = max(0.0, min(1.0, float(item.get("valence", 0.5))))
                arousal = max(0.0, min(1.0, float(item.get("arousal", 0.3))))
            except (ValueError, TypeError):
                valence, arousal = 0.5, 0.3

            validated.append({
                "name": str(item.get("name", ""))[:20],
                "content": str(item["content"]),
                "domain": item.get("domain", ["未分类"])[:3],
                "valence": valence,
                "arousal": arousal,
                "tags": [str(t) for t in item.get("tags", [])][:10],
                "importance": importance,
                "preserve_raw": bool(item.get("preserve_raw", False)),
                "is_pattern": bool(item.get("is_pattern", False)),
            })

        return validated

    def _dedupe_extracted_items(self, items: list[dict]) -> list[dict]:
        deduped = []
        for item in items:
            content = str(item.get("content") or "")
            if not _normalize_import_text(content):
                continue
            content_hash = _import_content_hash(content)
            if content_hash in self._seen_import_hashes:
                logger.info("Skipped duplicate import item in same run: %s", item.get("name", "?"))
                continue
            self._seen_import_hashes.add(content_hash)
            deduped.append(item)
        return deduped

    async def _find_duplicate_bucket(self, content: str) -> dict | None:
        normalized = _normalize_import_text(content)
        if not normalized:
            return None
        similarity_text = _import_similarity_text(content)
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.warning(f"Import duplicate scan failed: {e}")
            return None

        for bucket in buckets:
            meta = bucket.get("metadata", {}) if isinstance(bucket, dict) else {}
            if meta.get("type") == "feel":
                continue
            existing_content = str(bucket.get("content") or "")
            existing_normalized = _normalize_import_text(existing_content)
            if not existing_normalized:
                continue
            if normalized == existing_normalized:
                return bucket
            if min(len(normalized), len(existing_normalized)) >= 40 and (
                normalized in existing_normalized or existing_normalized in normalized
            ):
                return bucket

            existing_similarity_text = _import_similarity_text(existing_content)
            if min(len(similarity_text), len(existing_similarity_text)) < 30:
                continue
            if fuzz.token_set_ratio(similarity_text, existing_similarity_text) >= _IMPORT_DUPLICATE_SIMILARITY:
                return bucket
        return None

    async def _merge_or_create_item(self, item: dict, preserve_raw: bool = False) -> str:
        """Try to merge with existing bucket, or create new. Returns created/merged/raw/duplicate."""
        content = item["content"]
        domain = item.get("domain", ["未分类"])
        tags = item.get("tags", [])
        importance = item.get("importance", 5)
        valence = item.get("valence", 0.5)
        arousal = item.get("arousal", 0.3)
        name = item.get("name", "")

        duplicate = await self._find_duplicate_bucket(content)
        if duplicate:
            logger.info(
                "Skipped duplicate import item: %s -> %s",
                name or "?",
                duplicate.get("id", "?"),
            )
            return "duplicate"

        if preserve_raw:
            bucket_id = await self.bucket_mgr.create(
                content=content,
                tags=tags,
                importance=importance,
                domain=domain,
                valence=valence,
                arousal=arousal,
                name=name or None,
            )
            if self.embedding_engine:
                try:
                    await self.embedding_engine.generate_and_store(
                        bucket_id,
                        bucket_text_for_embedding(
                            {
                                "id": bucket_id,
                                "content": content,
                                "metadata": {"name": name},
                            }
                        ),
                    )
                except Exception:
                    pass
            return "raw"

        try:
            existing = await self.bucket_mgr.search(
                content,
                limit=1,
                domain_filter=domain or None,
                include_archive=False,
            )
        except Exception:
            existing = []

        merge_threshold = self.config.get("merge_threshold", 75)

        if existing and existing[0].get("score", 0) > merge_threshold:
            bucket = existing[0]
            if not (
                bucket["metadata"].get("pinned")
                or bucket["metadata"].get("protected")
                or bucket["metadata"].get("type") == "feel"
            ):
                try:
                    merged = await self.dehydrator.merge(bucket["content"], content)
                    self.state.data["api_calls"] += 1
                    old_v = bucket["metadata"].get("valence", 0.5)
                    old_a = bucket["metadata"].get("arousal", 0.3)
                    await self.bucket_mgr.update(
                        bucket["id"],
                        content=merged,
                        tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                        importance=max(bucket["metadata"].get("importance", 5), importance),
                        domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                        valence=round((old_v + valence) / 2, 2),
                        arousal=round((old_a + arousal) / 2, 2),
                    )
                    if self.embedding_engine:
                        try:
                            await self.embedding_engine.generate_and_store(
                                bucket["id"],
                                bucket_text_for_embedding({**bucket, "content": merged}),
                            )
                        except Exception:
                            pass
                    return "merged"
                except Exception as e:
                    logger.warning(f"Merge failed during import: {e}")
                    self.state.data["api_calls"] += 1

        # Create new
        bucket_id = await self.bucket_mgr.create(
            content=content,
            tags=tags,
            importance=importance,
            domain=domain,
            valence=valence,
            arousal=arousal,
            name=name or None,
        )
        if self.embedding_engine:
            try:
                await self.embedding_engine.generate_and_store(
                    bucket_id,
                    bucket_text_for_embedding(
                        {
                            "id": bucket_id,
                            "content": content,
                            "metadata": {"name": name},
                        }
                    ),
                )
            except Exception:
                pass
        return "created"

    async def detect_patterns(self) -> list[dict]:
        """
        Post-import: detect high-frequency patterns via embedding clustering.
        导入后：通过 embedding 聚类检测高频模式。
        Returns list of {pattern_content, count, bucket_ids, suggested_action}.
        """
        if not self.embedding_engine:
            return []

        all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        dynamic_buckets = [
            b for b in all_buckets
            if b["metadata"].get("type") == "dynamic"
            and not b["metadata"].get("pinned")
            and not b["metadata"].get("resolved")
        ]

        if len(dynamic_buckets) < 5:
            return []

        # Get embeddings
        embeddings = {}
        for b in dynamic_buckets:
            emb = await self.embedding_engine.get_embedding(b["id"])
            if emb is not None:
                embeddings[b["id"]] = emb

        if len(embeddings) < 5:
            return []

        # Find clusters: group by pairwise similarity > 0.7
        import numpy as np
        ids = list(embeddings.keys())
        clusters: dict[str, list[str]] = {}
        visited = set()

        for i, id_a in enumerate(ids):
            if id_a in visited:
                continue
            cluster = [id_a]
            visited.add(id_a)
            emb_a = np.array(embeddings[id_a])
            norm_a = np.linalg.norm(emb_a)
            if norm_a == 0:
                continue

            for j in range(i + 1, len(ids)):
                id_b = ids[j]
                if id_b in visited:
                    continue
                emb_b = np.array(embeddings[id_b])
                norm_b = np.linalg.norm(emb_b)
                if norm_b == 0:
                    continue
                sim = float(np.dot(emb_a, emb_b) / (norm_a * norm_b))
                if sim > 0.7:
                    cluster.append(id_b)
                    visited.add(id_b)

            if len(cluster) >= 3:
                clusters[id_a] = cluster

        # Format results
        patterns = []
        for lead_id, cluster_ids in clusters.items():
            lead_bucket = next((b for b in dynamic_buckets if b["id"] == lead_id), None)
            if not lead_bucket:
                continue
            patterns.append({
                "pattern_content": lead_bucket["content"][:200],
                "pattern_name": lead_bucket["metadata"].get("name", lead_id),
                "count": len(cluster_ids),
                "bucket_ids": cluster_ids,
                "suggested_action": "pin" if len(cluster_ids) >= 5 else "review",
            })

        patterns.sort(key=lambda p: p["count"], reverse=True)
        return patterns[:20]
