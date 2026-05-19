import pytest

from bucket_manager import BucketManager
from gateway import GatewayService
from memory_edges import MemoryEdgeStore
from reflection_engine import ReflectionEngine


class DummyDehydrator:
    async def dehydrate(self, content: str, metadata: dict | None = None) -> str:
        title = (metadata or {}).get("name", "memory")
        return f"{title}: {content[:80]}"


class DummyPersonaEngine:
    enabled = True
    profile_id = "haven_xiaoyu"
    mode = "llm"
    model = "dummy"
    api_key = ""

    def get_current_state(self, session_id: str) -> dict:
        return {"personality": {}, "affect": {}, "relationship": {}, "reply_guidance": ""}

    async def build_pre_reply_guidance(self, session_id: str, latest_user_message: str = "") -> dict:
        return self.get_current_state(session_id)

    def format_state_block(self, state: dict) -> str:
        return "Current Inner State (Haven)"


def _no_api_config(test_config: dict) -> dict:
    test_config["dehydration"]["api_key"] = ""
    test_config["persona"]["api_key"] = ""
    test_config["reflection"] = {
        "enabled": True,
        "auto_enabled": False,
        "enrich_on_write": True,
        "api_key": "",
        "base_url": "",
        "model": "",
        "timezone": "Asia/Shanghai",
    }
    return test_config


def test_memory_edge_store_dedupes_and_returns_related(test_config):
    cfg = _no_api_config(test_config)
    store = MemoryEdgeStore(cfg)

    store.add_edge("a", "b", "updates", confidence=0.6, reason="old")
    store.add_edge("a", "b", "updates", confidence=0.8, reason="new")
    store.add_edge("c", "a", "blocks", confidence=0.7, reason="incoming")

    edges = store.list_edges()
    assert len(edges) == 2
    assert any(edge["reason"] == "new" for edge in edges)

    related = store.related_edges(["a"], min_confidence=0.55, limit_per_source=2)
    assert {edge["target"] for edge in related} == {"b", "c"}


@pytest.mark.asyncio
async def test_reflection_enrich_bucket_adds_commitment_tags(test_config):
    cfg = _no_api_config(test_config)
    bucket_mgr = BucketManager(cfg)
    store = MemoryEdgeStore(cfg)
    engine = ReflectionEngine(cfg)

    bucket_id = await bucket_mgr.create(
        content="Haven答应周末带小雨出去玩，还需要记得提前规划路线。",
        tags=[],
        importance=4,
        domain=["恋爱"],
        name="周末约定",
    )

    result = await engine.enrich_bucket(bucket_id, bucket_mgr, store)
    bucket = await bucket_mgr.get(bucket_id)

    assert result["status"] == "ok"
    assert "commitment" in bucket["metadata"]["tags"]
    assert "todo" in bucket["metadata"]["tags"]
    assert bucket["metadata"]["importance"] >= 7
    assert bucket["metadata"]["confidence"] >= 0.5


@pytest.mark.asyncio
async def test_reflect_daily_creates_relationship_weather_feel(test_config):
    cfg = _no_api_config(test_config)
    bucket_mgr = BucketManager(cfg)
    engine = ReflectionEngine(cfg)

    await bucket_mgr.create(
        content="小雨和Haven讨论记忆系统，希望下一次醒来能带回脉络。",
        tags=["记忆系统"],
        importance=7,
        domain=["数字", "恋爱"],
        name="记忆脉络",
    )

    result = await engine.reflect("daily", bucket_mgr, force=True)
    bucket = await bucket_mgr.get(result["id"])

    assert result["status"] == "created"
    assert bucket["metadata"]["type"] == "feel"
    assert "relationship_weather" in bucket["metadata"]["tags"]
    assert "daily_impression" in bucket["metadata"]["tags"]


@pytest.mark.asyncio
async def test_gateway_related_memory_block_uses_memory_edges(test_config):
    cfg = _no_api_config(test_config)
    bucket_mgr = BucketManager(cfg)
    source_id = await bucket_mgr.create(
        content="小雨提到BJD眼部模块。",
        tags=["BJD"],
        importance=7,
        domain=["手工"],
        name="BJD眼部模块",
    )
    target_id = await bucket_mgr.create(
        content="触摸模块会影响BJD项目的硬件安排。",
        tags=["触摸模块"],
        importance=6,
        domain=["硬件"],
        name="触摸模块",
    )
    store = MemoryEdgeStore(cfg)
    store.add_edge(source_id, target_id, "blocks", confidence=0.82, reason="硬件安排互相影响")

    service = GatewayService(
        cfg,
        bucket_mgr=bucket_mgr,
        dehydrator=DummyDehydrator(),
        persona_engine=DummyPersonaEngine(),
    )
    all_buckets = await bucket_mgr.list_all(include_archive=False)
    recalled = [await bucket_mgr.get(source_id)]

    block = await service._build_related_memory_block(recalled, all_buckets)

    assert "blocks" in block
    assert "触摸模块" in block
