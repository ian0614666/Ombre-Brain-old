import pytest

from memory_edges import MemoryEdgeStore


class DummyDecayEngine:
    async def ensure_started(self) -> None:
        return None

    def calculate_score(self, metadata: dict) -> float:
        return float(metadata.get("score", metadata.get("importance", 1)))


class DummyDehydrator:
    async def dehydrate(self, content: str, metadata: dict | None = None) -> str:
        return " ".join((content or "").split())


class DummyEmbeddingEngine:
    async def search_similar(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return []


class FakeBucketManager:
    def __init__(self, buckets: list[dict], search_ids: list[str] | None = None):
        self.buckets = {bucket["id"]: bucket for bucket in buckets}
        self.search_ids = search_ids or []
        self.touched: list[str] = []

    async def list_all(self, include_archive: bool = False) -> list[dict]:
        return list(self.buckets.values())

    async def search(
        self,
        query: str,
        limit: int = 20,
        domain_filter: list[str] | None = None,
        query_valence: float | None = None,
        query_arousal: float | None = None,
    ) -> list[dict]:
        return [self.buckets[bucket_id] for bucket_id in self.search_ids[:limit]]

    async def get(self, bucket_id: str) -> dict | None:
        return self.buckets.get(bucket_id)

    async def touch(self, bucket_id: str) -> None:
        self.touched.append(bucket_id)


def _bucket(
    bucket_id: str,
    content: str,
    *,
    score: float = 1.0,
    bucket_type: str = "dynamic",
    importance: int = 5,
    pinned: bool = False,
    protected: bool = False,
    resolved: bool = False,
) -> dict:
    metadata = {
        "id": bucket_id,
        "name": bucket_id,
        "tags": [],
        "domain": ["测试"],
        "type": bucket_type,
        "importance": importance,
        "score": score,
        "valence": 0.5,
        "arousal": 0.3,
        "created": "2026-05-19T00:00:00+00:00",
        "updated_at": "2026-05-19T00:00:00+00:00",
        "last_active": "2026-05-19T00:00:00+00:00",
    }
    if pinned:
        metadata["pinned"] = True
    if protected:
        metadata["protected"] = True
    if resolved:
        metadata["resolved"] = True
    return {"id": bucket_id, "content": content, "metadata": metadata}


def _edge_store(tmp_path, edges: list[dict] | None = None) -> MemoryEdgeStore:
    store = MemoryEdgeStore(
        {
            "state_dir": str(tmp_path / "state"),
            "buckets_dir": str(tmp_path / "buckets"),
        }
    )
    for edge in edges or []:
        store.add_edge(
            edge["source"],
            edge["target"],
            edge.get("relation_type", "relates_to"),
            confidence=edge.get("confidence", 0.8),
            reason=edge.get("reason", "related in test"),
        )
    return store


@pytest.fixture
def patch_breath(monkeypatch, tmp_path):
    import server

    def _patch(
        buckets: list[dict],
        *,
        search_ids: list[str] | None = None,
        edges: list[dict] | None = None,
        token_counter=None,
    ) -> FakeBucketManager:
        bucket_mgr = FakeBucketManager(buckets, search_ids=search_ids)
        monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
        monkeypatch.setattr(server, "decay_engine", DummyDecayEngine())
        monkeypatch.setattr(server, "dehydrator", DummyDehydrator())
        monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())
        monkeypatch.setattr(server, "memory_edge_store", _edge_store(tmp_path, edges))
        monkeypatch.setattr(server.random, "random", lambda: 1.0)
        monkeypatch.setattr(server.random, "shuffle", lambda items: None)
        monkeypatch.setattr(server, "count_tokens_approx", token_counter or (lambda text: 1))
        return bucket_mgr

    return _patch


@pytest.mark.asyncio
async def test_surfacing_appends_related_memory_for_returned_dynamic_bucket(patch_breath):
    import server

    patch_breath(
        [
            _bucket("A", "A actual surface", score=9.0),
            _bucket("B", "B related target", resolved=True),
        ],
        edges=[{"source": "A", "target": "B", "relation_type": "supports", "confidence": 0.9}],
    )

    result = await server.breath(max_tokens=50, include_core=False)

    assert "=== 浮现记忆 ===" in result
    assert "[bucket_id:A]" in result
    assert "=== 关联记忆 ===" in result
    assert "[bucket_id:B]" in result


@pytest.mark.asyncio
async def test_budget_skipped_dynamic_bucket_does_not_emit_related(patch_breath):
    import server

    patch_breath(
        [
            _bucket("A", "A too expensive to surface", score=9.0),
            _bucket("B", "B should stay hidden", resolved=True),
        ],
        edges=[{"source": "A", "target": "B", "confidence": 0.9}],
        token_counter=lambda text: 10,
    )

    result = await server.breath(max_tokens=5, include_core=False)

    assert "[bucket_id:A]" not in result
    assert "=== 关联记忆 ===" not in result
    assert "[bucket_id:B]" not in result


@pytest.mark.asyncio
async def test_search_appends_related_memory_and_touches_only_matched_bucket(patch_breath):
    import server

    bucket_mgr = patch_breath(
        [
            _bucket("A", "A search hit", score=9.0),
            _bucket("B", "B related target", resolved=True),
        ],
        search_ids=["A"],
        edges=[{"source": "A", "target": "B", "relation_type": "updates", "confidence": 0.9}],
    )

    result = await server.breath(query="A", max_tokens=50)

    assert "[bucket_id:A]" in result
    assert "=== 关联记忆 ===" in result
    assert "[bucket_id:B]" in result
    assert bucket_mgr.touched == ["A"]


@pytest.mark.asyncio
async def test_search_skips_feel_hits_without_touching(patch_breath):
    import server

    bucket_mgr = patch_breath(
        [
            _bucket("F", "F feel hit", bucket_type="feel", score=10.0),
            _bucket("A", "A ordinary hit", score=9.0),
        ],
        search_ids=["F", "A"],
    )

    result = await server.breath(query="hit", max_tokens=50, include_related=False)

    assert "[bucket_id:F]" not in result
    assert "[bucket_id:A]" in result
    assert bucket_mgr.touched == ["A"]


@pytest.mark.asyncio
async def test_incoming_edge_renders_left_arrow_from_search_source(patch_breath):
    import server

    patch_breath(
        [
            _bucket("A", "A search hit", score=9.0),
            _bucket("B", "B incoming source", resolved=True),
        ],
        search_ids=["A"],
        edges=[{"source": "B", "target": "A", "relation_type": "blocks", "confidence": 0.9}],
    )

    result = await server.breath(query="A", max_tokens=50)

    assert "[A <- B]" in result


@pytest.mark.asyncio
async def test_include_related_false_suppresses_related_block(patch_breath):
    import server

    patch_breath(
        [
            _bucket("A", "A search hit", score=9.0),
            _bucket("B", "B related target", resolved=True),
        ],
        search_ids=["A"],
        edges=[{"source": "A", "target": "B", "confidence": 0.9}],
    )

    result = await server.breath(query="A", max_tokens=50, include_related=False)

    assert "[bucket_id:A]" in result
    assert "=== 关联记忆 ===" not in result
    assert "[bucket_id:B]" not in result


@pytest.mark.asyncio
async def test_core_limit_keeps_pinned_from_full_surfacing(patch_breath):
    import server

    patch_breath(
        [
            _bucket(
                f"P{index}",
                f"pinned memory {index}",
                bucket_type="permanent",
                pinned=True,
                importance=10 - index,
                score=10 - index,
            )
            for index in range(5)
        ]
    )

    result = await server.breath(max_tokens=500, core_limit=2)

    assert result.count("[核心准则]") == 2
    assert "[bucket_id:P0]" in result
    assert "[bucket_id:P1]" in result
    assert "[bucket_id:P2]" not in result


@pytest.mark.asyncio
async def test_core_memory_does_not_pull_related_memory_without_dynamic_source(patch_breath):
    import server

    patch_breath(
        [
            _bucket("A", "pinned A", bucket_type="permanent", pinned=True, importance=10),
            _bucket("B", "B related to core only", resolved=True),
        ],
        edges=[{"source": "A", "target": "B", "confidence": 0.9}],
    )

    result = await server.breath(max_tokens=500, core_limit=3)

    assert "[bucket_id:A]" in result
    assert "=== 关联记忆 ===" not in result
    assert "[bucket_id:B]" not in result


@pytest.mark.asyncio
async def test_random_drift_does_not_exceed_remaining_budget(patch_breath, monkeypatch):
    import server

    def token_counter(text: str) -> int:
        text = str(text)
        if text.startswith("[bucket_id:A]"):
            return 9
        if text.startswith("--- 久未碰过"):
            return 2
        return 5

    patch_breath(
        [
            _bucket("A", "A search hit", score=9.0),
            _bucket("B", "B low score drift candidate", score=0.5),
        ],
        search_ids=["A"],
        token_counter=token_counter,
    )
    monkeypatch.setattr(server.random, "random", lambda: 0.0)
    monkeypatch.setattr(server.random, "randint", lambda start, end: 1)

    result = await server.breath(query="A", max_tokens=10, include_related=False)

    assert "[bucket_id:A]" in result
    assert "--- 久未碰过 ---" not in result
    assert "B low score drift candidate" not in result
