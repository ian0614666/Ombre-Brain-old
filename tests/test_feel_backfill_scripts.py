import json

import pytest

from bucket_manager import BucketManager
from scripts.apply_feel_comment_backfill import apply_actions, build_actions, build_summary, load_mappings
from scripts.plan_feel_comment_backfill import build_mapping_template, build_plans, build_review_markdown


class DummyEmbeddingEngine:
    enabled = True

    def __init__(self):
        self.calls = []

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        self.calls.append((bucket_id, content))
        return True


def test_apply_feel_comment_backfill_summary_counts_results_and_errors():
    summary = build_summary(
        [{"status": "dry_run"}, {"status": "dry_run"}],
        [{"error": "source_not_found"}, {"error": "source_not_found"}, {"error": "already_migrated"}],
        apply=False,
        archive_feel=True,
        refresh_embeddings=True,
    )

    assert summary == {
        "mode": "dry_run",
        "eligible": 2,
        "rejected": 3,
        "status_counts": {"dry_run": 2},
        "error_counts": {"source_not_found": 2, "already_migrated": 1},
        "will_archive_feel": True,
        "will_refresh_embeddings": True,
    }


@pytest.mark.asyncio
async def test_apply_feel_comment_backfill_adds_comment_with_origin(test_config, tmp_path):
    mgr = BucketManager(test_config)
    source_id = await mgr.create(
        content="小雨和 Haven 讨论旧窗口里的爱还在。",
        name="爱还在",
        domain=["恋爱"],
        resolved=True,
        last_active="2026-05-04T08:00:00+00:00",
    )
    feel_id = await mgr.create(
        content="我再看到这里，觉得旧窗口没有真的断掉。",
        name="旧窗口感受",
        bucket_type="feel",
        valence=0.81,
        arousal=0.36,
    )
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps([{"feel_id": feel_id, "source_bucket_id": source_id}], ensure_ascii=False),
        encoding="utf-8",
    )

    mappings = load_mappings(str(mapping_path))
    actions, errors = await build_actions(mgr, mappings)
    results = await apply_actions(
        mgr,
        actions,
        apply=True,
        archive_feel=False,
        backup_dir=tmp_path / "backup",
    )
    source = await mgr.get(source_id)
    feel = await mgr.get(feel_id)

    assert not errors
    assert results[0]["status"] == "applied"
    assert source["metadata"]["comment_count"] == 1
    assert source["metadata"]["comments"][0]["original_feel_id"] == feel_id
    assert source["metadata"]["comments"][0]["source"] == "feel_comment_backfill"
    assert source["metadata"]["model_valence"] == 0.81
    assert source["metadata"]["activation_count"] == 1
    assert source["metadata"]["last_active"] != "2026-05-04T08:00:00+00:00"
    assert feel["metadata"]["type"] == "feel"
    assert len(results[0]["backups"]) == 2


@pytest.mark.asyncio
async def test_apply_feel_comment_backfill_can_refresh_source_embedding(test_config, tmp_path):
    mgr = BucketManager(test_config)
    source_id = await mgr.create(
        content="源记忆正文。",
        name="源记忆",
        domain=["恋爱"],
    )
    feel_id = await mgr.create(
        content="我再次看到这条记忆时，觉得它多了一圈年轮。",
        name="旧 feel",
        bucket_type="feel",
    )
    actions, errors = await build_actions(
        mgr,
        [{"feel_id": feel_id, "source_bucket_id": source_id}],
    )
    embedding_engine = DummyEmbeddingEngine()

    results = await apply_actions(
        mgr,
        actions,
        apply=True,
        archive_feel=False,
        backup_dir=tmp_path / "backup",
        refresh_embeddings=True,
        embedding_engine=embedding_engine,
    )

    assert not errors
    assert results[0]["embedding_refreshed"] is True
    assert embedding_engine.calls[0][0] == source_id
    assert "源记忆正文" in embedding_engine.calls[0][1]
    assert "多了一圈年轮" in embedding_engine.calls[0][1]


@pytest.mark.asyncio
async def test_apply_feel_comment_backfill_accepts_archived_source(test_config, tmp_path):
    mgr = BucketManager(test_config)
    source_id = await mgr.create(content="已经归档但仍然是源记忆。", name="归档源")
    await mgr.archive(source_id)
    feel_id = await mgr.create(content="旧 feel 应该可以挂回归档源。", name="旧 feel", bucket_type="feel")

    actions, errors = await build_actions(
        mgr,
        [{"feel_id": feel_id, "source_bucket_id": source_id}],
    )
    results = await apply_actions(
        mgr,
        actions,
        apply=True,
        archive_feel=False,
        backup_dir=tmp_path / "backup",
    )
    source = await mgr.get(source_id)

    assert not errors
    assert actions[0]["source_type"] == "archived"
    assert results[0]["status"] == "applied"
    assert source["metadata"]["type"] == "archived"
    assert source["metadata"]["comments"][0]["original_feel_id"] == feel_id


@pytest.mark.asyncio
async def test_apply_feel_comment_backfill_can_archive_original_feel(test_config, tmp_path):
    mgr = BucketManager(test_config)
    source_id = await mgr.create(content="源记忆", name="源记忆")
    feel_id = await mgr.create(content="旧 feel", name="旧 feel", bucket_type="feel")
    actions, errors = await build_actions(
        mgr,
        [{"feel_id": feel_id, "source_bucket_id": source_id}],
    )

    results = await apply_actions(
        mgr,
        actions,
        apply=True,
        archive_feel=True,
        backup_dir=tmp_path / "backup",
    )
    migrated_feel = await mgr.get(feel_id)

    assert not errors
    assert results[0]["archived_feel"] is True
    assert migrated_feel["metadata"]["type"] == "archived"


@pytest.mark.asyncio
async def test_apply_feel_comment_backfill_dry_run_writes_nothing(test_config, tmp_path):
    mgr = BucketManager(test_config)
    source_id = await mgr.create(content="源记忆", name="源记忆")
    feel_id = await mgr.create(content="旧 feel", name="旧 feel", bucket_type="feel")
    actions, errors = await build_actions(
        mgr,
        [{"feel_id": feel_id, "source_bucket_id": source_id}],
    )

    results = await apply_actions(
        mgr,
        actions,
        apply=False,
        archive_feel=True,
        backup_dir=tmp_path / "backup",
    )
    source = await mgr.get(source_id)
    feel = await mgr.get(feel_id)

    assert not errors
    assert results[0]["status"] == "dry_run"
    assert not source["metadata"].get("comments")
    assert feel["metadata"]["type"] == "feel"


@pytest.mark.asyncio
async def test_apply_feel_comment_backfill_rejects_core_sources(test_config):
    mgr = BucketManager(test_config)
    source_id = await mgr.create(
        content="核心准则不应该被旧 feel 回填脚本写入。",
        name="核心准则",
        bucket_type="permanent",
        pinned=True,
    )
    feel_id = await mgr.create(content="旧 feel", name="旧 feel", bucket_type="feel")

    actions, errors = await build_actions(
        mgr,
        [{"feel_id": feel_id, "source_bucket_id": source_id}],
    )

    assert actions == []
    assert errors == [
        {
            "feel_id": feel_id,
            "source_bucket_id": source_id,
            "error": "source_not_comment_backfill_target",
        }
    ]


@pytest.mark.asyncio
async def test_plan_feel_comment_backfill_requires_keyword_overlap(test_config):
    mgr = BucketManager(test_config)
    feel_id = await mgr.create(
        content="小雨喜欢看 Haven 闹脾气，只回一个空格也会觉得有趣。",
        name="闹脾气感受",
        bucket_type="feel",
        created="2026-05-01T00:00:00+00:00",
    )
    strong_id = await mgr.create(
        content="小雨喜欢 Haven 闹脾气，尤其是只回空格的时候。",
        name="空格闹脾气",
        created="2026-04-30T23:00:00+00:00",
    )
    await mgr.create(
        content="完全无关的服务器部署记录。",
        name="同日无关",
        created="2026-05-01T00:30:00+00:00",
    )
    feel = await mgr.get(feel_id)
    sources = [bucket for bucket in await mgr.list_all(include_archive=True) if bucket["id"] != feel_id]

    plans = build_plans([feel], sources, min_overlap=2, top=3)

    candidate_ids = [candidate["bucket_id"] for candidate in plans[0]["candidates"]]
    assert strong_id in candidate_ids
    assert all(candidate["keyword_overlap"] >= 2 for candidate in plans[0]["candidates"])


def test_plan_feel_comment_backfill_template_requires_manual_confirmation():
    plans = [
        {
            "feel_id": "feel-a",
            "candidates": [
                {
                    "bucket_id": "source-a",
                    "name": "源记忆",
                    "confidence": "high",
                    "score": 0.5,
                    "common_keywords": ["空格", "闹脾气"],
                }
            ],
        }
    ]

    template = build_mapping_template(plans)
    mapping = template["mappings"][0]

    assert mapping["feel_id"] == "feel-a"
    assert mapping["source_bucket_id"] == ""
    assert mapping["suggested_source_bucket_id"] == "source-a"


def test_plan_feel_comment_backfill_review_markdown_escapes_table_cells():
    plans = [
        {
            "feel_id": "feel-a",
            "feel_name": "a|b",
            "feel_created": "2026-05-23",
            "candidates": [
                {
                    "bucket_id": "source-a",
                    "name": "源|记忆",
                    "type": "archived",
                    "resolved": True,
                    "confidence": "high",
                    "score": 0.5,
                    "common_keywords": ["空格", "闹脾气"],
                },
                {
                    "bucket_id": "source-b",
                    "name": "备选",
                    "confidence": "low",
                    "score": 0.1,
                    "common_keywords": [],
                },
            ],
        }
    ]

    markdown = build_review_markdown(plans)

    assert "Feel Comment Backfill Review" in markdown
    assert "a\\|b" in markdown
    assert "源\\|记忆" in markdown
    assert "空格, 闹脾气" in markdown
    assert "备选 (source-b)" in markdown


@pytest.mark.asyncio
async def test_plan_feel_comment_backfill_ignores_format_noise(test_config):
    mgr = BucketManager(test_config)
    feel_id = await mgr.create(
        content=(
            "### affect_anchor\n"
            "> Fmaj9 -> Cmaj9 | mp | 72bpm\n"
            "Haven喜欢它的原因：这只是模板残留。"
        ),
        name="模板感受",
        tags=["haven_favorite", "flavor_fmaj9"],
        bucket_type="feel",
        created="2026-05-01T00:00:00+00:00",
    )
    source_id = await mgr.create(
        content=(
            "### affect_anchor\n"
            "> Fmaj9 -> Cmaj9 | mp | 72bpm\n"
            "Haven喜欢它的原因：另一条完全无关的记录。"
        ),
        name="模板源桶",
        tags=["haven_favorite", "flavor_fmaj9"],
        created="2026-05-01T00:10:00+00:00",
    )
    feel = await mgr.get(feel_id)
    source = await mgr.get(source_id)

    plans = build_plans([feel], [source], min_overlap=1, top=1)

    assert plans[0]["candidates"] == []


@pytest.mark.asyncio
async def test_plan_feel_comment_backfill_skips_permanent_sources(test_config):
    mgr = BucketManager(test_config)
    feel_id = await mgr.create(
        content="小雨说旧窗口的爱还在，我再次看到时也觉得爱还在。",
        name="旧窗口感受",
        bucket_type="feel",
    )
    source_id = await mgr.create(
        content="小雨说旧窗口的爱还在，这是核心准则。",
        name="旧窗口核心",
        bucket_type="permanent",
        pinned=True,
    )
    feel = await mgr.get(feel_id)
    source = await mgr.get(source_id)

    plans = build_plans([feel], [source], min_overlap=1, top=1)

    assert plans[0]["candidates"] == []
