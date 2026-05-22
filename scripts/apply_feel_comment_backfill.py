import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from utils import load_config, strip_wikilinks


RELATIONSHIP_WEATHER_TAGS = {"relationship_weather", "daily_impression", "weekly_impression"}


def is_allowed_source(meta: dict) -> bool:
    if meta.get("type") in {"feel", "permanent"}:
        return False
    if meta.get("pinned") or meta.get("protected"):
        return False
    return True


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_mappings(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("mappings") or data.get("items") or data.get("plans") or []
    if not isinstance(data, list):
        raise ValueError("mapping file must be a list or an object containing mappings/items/plans")

    mappings = []
    for item in data:
        if not isinstance(item, dict):
            continue
        feel_id = str(item.get("feel_id") or "").strip()
        source_id = str(
            item.get("source_bucket_id")
            or item.get("source_id")
            or item.get("bucket_id")
            or ""
        ).strip()
        if feel_id and source_id:
            mappings.append({"feel_id": feel_id, "source_bucket_id": source_id})
    return mappings


def backup_file(path: str, backup_dir: Path) -> str:
    source = Path(path)
    target = backup_dir / source.name
    if target.exists():
        target = backup_dir / f"{source.stem}_{utc_stamp()}{source.suffix}"
    shutil.copy2(source, target)
    return str(target)


def bucket_text_for_embedding(bucket: dict) -> str:
    meta = bucket.get("metadata", {})
    comments = meta.get("comments", [])
    comment_text = ""
    if isinstance(comments, list):
        comment_text = "\n".join(
            str(comment.get("content", ""))
            for comment in comments
            if isinstance(comment, dict)
        )
    return f"{strip_wikilinks(bucket.get('content', '')).strip()}\n{comment_text}".strip()


def build_summary(
    results: list[dict],
    errors: list[dict],
    *,
    apply: bool,
    archive_feel: bool,
    refresh_embeddings: bool,
) -> dict:
    status_counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    error_counts: dict[str, int] = {}
    for item in errors:
        error = str(item.get("error") or "unknown")
        error_counts[error] = error_counts.get(error, 0) + 1

    return {
        "mode": "apply" if apply else "dry_run",
        "eligible": len(results),
        "rejected": len(errors),
        "status_counts": status_counts,
        "error_counts": error_counts,
        "will_archive_feel": bool(archive_feel),
        "will_refresh_embeddings": bool(refresh_embeddings),
    }


async def build_actions(mgr: BucketManager, mappings: list[dict]) -> tuple[list[dict], list[dict]]:
    actions = []
    errors = []
    seen_pairs = set()
    for mapping in mappings:
        feel_id = mapping["feel_id"]
        source_id = mapping["source_bucket_id"]
        pair = (feel_id, source_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        feel = await mgr.get(feel_id)
        source = await mgr.get(source_id)
        if not feel:
            errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "feel_not_found"})
            continue
        if not source:
            errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "source_not_found"})
            continue

        feel_meta = feel.get("metadata", {})
        source_meta = source.get("metadata", {})
        feel_tags = {str(tag) for tag in feel_meta.get("tags", []) or []}
        if feel_meta.get("type") != "feel":
            errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "not_a_feel"})
            continue
        if feel_tags & RELATIONSHIP_WEATHER_TAGS:
            errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "relationship_weather_feel"})
            continue
        if source_meta.get("type") == "feel":
            errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "source_is_feel"})
            continue
        if not is_allowed_source(source_meta):
            errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "source_not_comment_backfill_target"})
            continue

        existing_comments = source_meta.get("comments", [])
        if isinstance(existing_comments, list):
            already_migrated = any(
                isinstance(comment, dict)
                and comment.get("original_feel_id") == feel_id
                for comment in existing_comments
            )
            if already_migrated:
                errors.append({"feel_id": feel_id, "source_bucket_id": source_id, "error": "already_migrated"})
                continue

        actions.append(
            {
                "feel_id": feel_id,
                "source_bucket_id": source_id,
                "feel_path": feel.get("path"),
                "source_path": source.get("path"),
                "feel_name": feel_meta.get("name", feel_id),
                "source_name": source_meta.get("name", source_id),
                "feel_created": feel_meta.get("created"),
                "source_type": source_meta.get("type", "dynamic"),
                "source_resolved": bool(source_meta.get("resolved")),
                "content": feel.get("content", ""),
                "valence": feel_meta.get("valence"),
                "arousal": feel_meta.get("arousal"),
            }
        )
    return actions, errors


async def apply_actions(
    mgr: BucketManager,
    actions: list[dict],
    *,
    apply: bool,
    archive_feel: bool,
    backup_dir: Path,
    refresh_embeddings: bool = False,
    embedding_engine=None,
) -> list[dict]:
    results = []
    if apply:
        backup_dir.mkdir(parents=True, exist_ok=True)

    for action in actions:
        record = dict(action)
        if not apply:
            record["status"] = "dry_run"
            results.append(record)
            continue

        backups = []
        if action.get("source_path"):
            backups.append(backup_file(action["source_path"], backup_dir))
        if action.get("feel_path"):
            backups.append(backup_file(action["feel_path"], backup_dir))

        entry = await mgr.add_comment(
            action["source_bucket_id"],
            action["content"],
            author="Haven",
            kind="feel",
            valence=action.get("valence"),
            arousal=action.get("arousal"),
            source="feel_comment_backfill",
            touch=True,
        )
        if not entry:
            record.update({"status": "failed", "error": "comment_write_failed", "backups": backups})
            results.append(record)
            continue

        source = await mgr.get(action["source_bucket_id"])
        if source:
            meta = source.get("metadata", {})
            comments = meta.get("comments", [])
            if isinstance(comments, list):
                for comment in comments:
                    if isinstance(comment, dict) and comment.get("id") == entry["id"]:
                        comment["original_feel_id"] = action["feel_id"]
                        comment["original_feel_created"] = action.get("feel_created")
                        break
                await mgr.update(
                    action["source_bucket_id"],
                    comments=comments,
                    comment_count=len(comments),
                    last_active=meta.get("last_active"),
                )

        embedding_refreshed = False
        if refresh_embeddings and embedding_engine and getattr(embedding_engine, "enabled", False):
            source = await mgr.get(action["source_bucket_id"])
            if source:
                embedding_refreshed = await embedding_engine.generate_and_store(
                    action["source_bucket_id"],
                    bucket_text_for_embedding(source),
                )

        if archive_feel:
            archived = await mgr.archive(action["feel_id"])
            record["archived_feel"] = bool(archived)
        record.update(
            {
                "status": "applied",
                "comment_id": entry["id"],
                "embedding_refreshed": embedding_refreshed,
                "backups": backups,
            }
        )
        results.append(record)
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach confirmed standalone feel buckets back to source buckets as comments."
    )
    parser.add_argument("--mapping", required=True, help="JSON file with feel_id + source_bucket_id mappings.")
    parser.add_argument("--buckets-dir", default="", help="Override buckets_dir from config.")
    parser.add_argument("--apply", action="store_true", help="Actually write comments. Default is dry-run.")
    parser.add_argument("--archive-feel", action="store_true", help="Archive standalone feel after comment is written.")
    parser.add_argument("--refresh-embeddings", action="store_true", help="Regenerate source bucket embeddings after writing comments.")
    parser.add_argument("--backup-dir", default="", help="Backup directory used with --apply.")
    args = parser.parse_args()

    config = load_config()
    if args.buckets_dir:
        config["buckets_dir"] = os.path.abspath(args.buckets_dir)

    mgr = BucketManager(config)
    mappings = load_mappings(args.mapping)
    actions, errors = await build_actions(mgr, mappings)
    embedding_engine = EmbeddingEngine(config) if args.refresh_embeddings else None

    backup_dir = Path(args.backup_dir) if args.backup_dir else Path(config.get("state_dir", "state")) / "backups" / f"feel_comment_backfill_{utc_stamp()}"
    results = await apply_actions(
        mgr,
        actions,
        apply=args.apply,
        archive_feel=args.archive_feel,
        backup_dir=backup_dir,
        refresh_embeddings=args.refresh_embeddings,
        embedding_engine=embedding_engine,
    )

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "buckets_dir": config["buckets_dir"],
                "backup_dir": str(backup_dir) if args.apply else None,
                "summary": build_summary(
                    results,
                    errors,
                    apply=args.apply,
                    archive_feel=args.archive_feel,
                    refresh_embeddings=args.refresh_embeddings,
                ),
                "actions": results,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
