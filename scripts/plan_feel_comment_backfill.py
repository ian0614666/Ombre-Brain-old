import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import jieba
except Exception:  # pragma: no cover
    jieba = None

from bucket_manager import BucketManager
from utils import load_config, strip_wikilinks


RELATIONSHIP_WEATHER_TAGS = {"relationship_weather", "daily_impression", "weekly_impression"}
STOPWORDS = {
    "我",
    "你",
    "她",
    "他",
    "它",
    "我们",
    "你们",
    "他们",
    "小雨",
    "haven",
    "哥哥",
    "老公",
    "老婆",
    "宝宝",
    "亲爱的",
    "今天",
    "时候",
    "感觉",
    "觉得",
    "记忆",
    "自己",
    "一个",
    "一些",
    "这个",
    "那个",
    "这里",
    "那里",
    "这样",
    "什么",
    "因为",
    "所以",
    "但是",
    "不是",
    "没有",
    "可以",
    "已经",
    "现在",
    "喜欢",
    "原因",
    "含义",
    "这种",
    "只是",
    "最后",
    "记录",
    "而是",
    "以后",
    "每次",
    "醒来",
    "第一次",
    "成为",
    "有点",
    "告诉",
    "一起",
    "之间",
    "关系",
    "事情",
    "片刻",
    "模板",
    "残留",
    "无关",
    "窗口",
    "继续",
    "bucket",
    "memory",
    "memories",
    "comment",
    "comments",
    "affect",
    "anchor",
    "affect_anchor",
    "favorite",
    "haven_favorite",
    "flavor",
    "haven喜欢它的原因",
    "fmaj9",
    "cmaj9",
    "am",
    "mp",
    "bpm",
}

CHORD_RE = re.compile(
    r"[a-g](?:#|b)?(?:maj|min|dim|aug|sus|add|m)?\d*(?:sus\d+|add\d+)?(?:/[a-g](?:#|b)?)?",
    re.IGNORECASE,
)


def candidate_text(bucket):
    meta = bucket.get("metadata", {})
    text = " ".join(
        [
            str(meta.get("name", "")),
            " ".join(str(tag) for tag in meta.get("tags", []) or []),
            bucket.get("content", ""),
        ]
    )
    text = strip_wikilinks(text or "")
    text = re.sub(r"(?im)^###\s*.*$", " ", text)
    text = re.sub(r"(?im)^>\s*.*$", " ", text)
    text = re.sub(r"(?i)\b(?:affect_anchor|haven_favorite|flavor_[\w-]+)\b", " ", text)
    return text


def is_backfill_source(bucket):
    meta = bucket.get("metadata", {})
    if meta.get("type") in {"feel", "permanent"}:
        return False
    if meta.get("pinned") or meta.get("protected"):
        return False
    return True


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def keywords(text):
    text = strip_wikilinks(text or "")
    if jieba:
        words = jieba.cut(text)
    else:
        words = re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", text)
    result = set()
    for word in words:
        word = str(word).strip().lower()
        if len(word) < 2:
            continue
        if word in STOPWORDS:
            continue
        if word.startswith("flavor_"):
            continue
        if not re.search(r"[a-z0-9\u4e00-\u9fff]", word):
            continue
        if re.fullmatch(r"#+", word):
            continue
        if re.fullmatch(r"[a-z0-9_]+", word) and len(word) < 3:
            continue
        if re.fullmatch(r"\d+", word):
            continue
        if re.fullmatch(r"\d+\s*bpm", word):
            continue
        if CHORD_RE.fullmatch(word):
            continue
        result.add(word)
    return result


def candidate_score(feel, source):
    source_meta = source.get("metadata", {})
    feel_meta = feel.get("metadata", {})
    feel_words = keywords(candidate_text(feel))
    source_words = keywords(candidate_text(source))
    common_keywords = sorted(feel_words & source_words)
    overlap = len(common_keywords)
    union = max(1, len(feel_words | source_words))
    keyword_score = overlap / union

    feel_time = parse_time(feel_meta.get("created") or feel_meta.get("last_active"))
    source_time = parse_time(source_meta.get("created") or source_meta.get("last_active"))
    if feel_time and source_time:
        delta_days = abs((feel_time - source_time).total_seconds()) / 86400
        date_score = max(0.0, 1.0 - delta_days / 30.0)
    else:
        date_score = 0.0

    score = round(keyword_score * 0.8 + date_score * 0.2, 4)
    confidence = "low"
    if overlap >= 5 and score >= 0.25:
        confidence = "high"
    elif overlap >= 2 and score >= 0.12:
        confidence = "medium"
    return {
        "score": score,
        "keyword_score": round(keyword_score, 4),
        "date_score": round(date_score, 4),
        "keyword_overlap": overlap,
        "common_keywords": common_keywords[:12],
        "confidence": confidence,
    }


def preview(bucket):
    return strip_wikilinks(bucket.get("content", "")).replace("\n", " ").strip()[:180]


def build_plans(feels, sources, *, limit: int = 50, top: int = 3, min_overlap: int = 2, min_score: float = 0.0):
    plans = []
    for feel in feels[: max(1, limit)]:
        scored = []
        for source in sources:
            if not is_backfill_source(source):
                continue
            metrics = candidate_score(feel, source)
            if metrics["score"] < min_score:
                continue
            if metrics["keyword_overlap"] < min_overlap:
                continue
            scored.append((metrics["score"], metrics, source))
        scored.sort(key=lambda item: item[0], reverse=True)
        plans.append(
            {
                "feel_id": feel["id"],
                "feel_name": feel.get("metadata", {}).get("name", feel["id"]),
                "feel_created": feel.get("metadata", {}).get("created"),
                "feel_preview": preview(feel),
                "candidates": [
                    {
                        **metrics,
                        "bucket_id": source["id"],
                        "name": source.get("metadata", {}).get("name", source["id"]),
                        "type": source.get("metadata", {}).get("type", "dynamic"),
                        "resolved": bool(source.get("metadata", {}).get("resolved")),
                        "created": source.get("metadata", {}).get("created"),
                        "preview": preview(source),
                    }
                    for _, metrics, source in scored[: max(1, top)]
                ],
            }
        )
    return plans


def build_mapping_template(plans: list[dict]) -> dict:
    mappings = []
    for plan in plans:
        candidate = plan.get("candidates", [{}])[0] if plan.get("candidates") else {}
        mappings.append(
            {
                "feel_id": plan.get("feel_id", ""),
                "source_bucket_id": "",
                "suggested_source_bucket_id": candidate.get("bucket_id", ""),
                "suggested_source_name": candidate.get("name", ""),
                "confidence": candidate.get("confidence", "none"),
                "score": candidate.get("score", 0),
                "common_keywords": candidate.get("common_keywords", []),
                "note": "确认后把 suggested_source_bucket_id 复制到 source_bucket_id；不确认就留空。",
            }
        )
    return {"mappings": mappings}


def markdown_cell(value) -> str:
    text = str(value if value is not None else "")
    text = text.replace("|", "\\|").replace("\n", " ")
    return text.strip()


def build_review_markdown(plans: list[dict]) -> str:
    lines = [
        "# Feel Comment Backfill Review",
        "",
        "只读审阅表。确认后，把 JSON 模板里的 `suggested_source_bucket_id` 复制到 `source_bucket_id`；不确认就留空。",
        "",
        "| feel_id | feel_name | created | suggested_source | confidence | score | keywords | alt_sources |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for plan in plans:
        candidates = plan.get("candidates", [])
        first = candidates[0] if candidates else {}
        alt_sources = ", ".join(
            f"{candidate.get('name', '')} ({candidate.get('bucket_id', '')})"
            for candidate in candidates[1:]
        )
        keywords_text = ", ".join(first.get("common_keywords", [])) if first else ""
        suggested = ""
        if first:
            state = []
            if first.get("type"):
                state.append(str(first.get("type")))
            if first.get("resolved"):
                state.append("resolved")
            state_text = f" [{' / '.join(state)}]" if state else ""
            suggested = f"{first.get('name', '')} ({first.get('bucket_id', '')}){state_text}"
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(plan.get("feel_id", "")),
                    markdown_cell(plan.get("feel_name", "")),
                    markdown_cell(plan.get("feel_created", "")),
                    markdown_cell(suggested),
                    markdown_cell(first.get("confidence", "none") if first else "none"),
                    markdown_cell(first.get("score", 0) if first else 0),
                    markdown_cell(keywords_text),
                    markdown_cell(alt_sources),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


async def main():
    parser = argparse.ArgumentParser(
        description="Plan old standalone feel -> source bucket comment backfill. Dry-run only."
    )
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--min-overlap", type=int, default=2)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--mapping-template", default="", help="Write an editable mapping template JSON.")
    parser.add_argument("--review-markdown", default="", help="Write a concise human review table.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    all_buckets = await mgr.list_all(include_archive=True)
    feels = []
    sources = []
    for bucket in all_buckets:
        meta = bucket.get("metadata", {})
        tags = {str(tag) for tag in meta.get("tags", []) or []}
        if meta.get("type") == "feel":
            if tags & RELATIONSHIP_WEATHER_TAGS:
                continue
            feels.append(bucket)
        elif is_backfill_source(bucket):
            sources.append(bucket)

    plans = build_plans(
        feels,
        sources,
        limit=args.limit,
        top=args.top,
        min_overlap=max(0, args.min_overlap),
        min_score=max(0.0, args.min_score),
    )

    if args.mapping_template:
        with open(args.mapping_template, "w", encoding="utf-8") as f:
            json.dump(build_mapping_template(plans), f, ensure_ascii=False, indent=2)
    if args.review_markdown:
        with open(args.review_markdown, "w", encoding="utf-8") as f:
            f.write(build_review_markdown(plans))

    print(json.dumps({"buckets_dir": config["buckets_dir"], "plans": plans}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
