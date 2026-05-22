import json
import os

import pytest


class DummyEmbeddingEngine:
    enabled = False

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        return False

    async def search_similar(self, query: str, top_k: int = 10):
        return []


class CapturingEmbeddingEngine(DummyEmbeddingEngine):
    enabled = True

    def __init__(self):
        self.calls = []

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        self.calls.append((bucket_id, content))
        return True


class DummyDehydrator:
    async def analyze(self, content: str):
        return {
            "domain": ["恋爱"],
            "valence": 0.7,
            "arousal": 0.4,
            "tags": ["relationship_event"],
            "suggested_name": "新记忆",
        }

    async def dehydrate(self, content: str, metadata: dict | None = None) -> str:
        return content[:120]


class DummyRequest:
    def __init__(self, body=None, headers=None, cookies=None):
        self._body = body
        self.headers = headers or {}
        self.cookies = cookies or {}

    async def json(self):
        return self._body

    async def body(self):
        if isinstance(self._body, bytes):
            return self._body
        return json.dumps(self._body or {}).encode("utf-8")


@pytest.mark.asyncio
async def test_create_memory_api_requires_write_token(monkeypatch, bucket_mgr):
    import server

    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "secret")
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    response = await server.api_create_memory(DummyRequest({"title": "记忆", "content": "内容"}))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_memory_api_writes_chatgpt_source(monkeypatch, bucket_mgr):
    import server

    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "secret")
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())
    request = DummyRequest(
        {
            "id": "chatgpt_api_memory",
            "title": "API 记忆",
            "content": "C 端通过 create_memory 写入。",
            "domain": ["同步"],
            "tags": ["chatgpt"],
            "resolved": True,
            "digested": True,
        },
        headers={"authorization": "Bearer secret"},
    )

    response = await server.api_create_memory(request)
    payload = json.loads(response.body)
    bucket = await bucket_mgr.get("chatgpt_api_memory")

    assert response.status_code == 200
    assert payload["status"] == "created"
    assert payload["source"] == "chatgpt"
    assert bucket["metadata"]["source"] == "chatgpt"
    assert bucket["metadata"]["resolved"] is True
    assert bucket["metadata"]["digested"] is True
    assert bucket["metadata"]["created"].endswith("+00:00")
    assert bucket["metadata"]["updated_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_read_bucket_returns_exact_content_without_touching(monkeypatch, bucket_mgr, decay_eng):
    import server

    bucket_id = await bucket_mgr.create(
        content="小雨说她想把这一刻留下来。",
        name="精确读取",
        domain=["记忆"],
        tags=["haven_favorite"],
        last_active="2026-05-04T08:00:00+00:00",
    )
    before = await bucket_mgr.get(bucket_id)

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)

    payload = await server.read_bucket(bucket_id)
    after = await bucket_mgr.get(bucket_id)

    assert payload["id"] == bucket_id
    assert payload["content"] == "小雨说她想把这一刻留下来。"
    assert payload["metadata"]["tags"] == ["haven_favorite"]
    assert after["metadata"]["last_active"] == before["metadata"]["last_active"]


@pytest.mark.asyncio
async def test_comment_bucket_adds_ring_and_touches_source(monkeypatch, bucket_mgr, decay_eng):
    import server

    bucket_id = await bucket_mgr.create(
        content="小雨把旧记忆拿出来看。",
        name="旧记忆",
        domain=["恋爱"],
        last_active="2026-05-04T08:00:00+00:00",
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    embedding_engine = CapturingEmbeddingEngine()
    monkeypatch.setattr(server, "embedding_engine", embedding_engine)

    result = await server.comment_bucket(
        bucket_id=bucket_id,
        content="现在再看到它，我觉得那时候的笨拙也很珍贵。",
        kind="feel",
        valence=0.82,
        arousal=0.35,
    )
    bucket = await bucket_mgr.get(bucket_id)

    assert result["status"] == "commented"
    assert result["embedding_refreshed"] is True
    assert bucket["metadata"]["comment_count"] == 1
    assert bucket["metadata"]["comments"][0]["kind"] == "feel"
    assert bucket["metadata"]["comments"][0]["valence"] == 0.82
    assert bucket["metadata"]["model_valence"] == 0.82
    assert bucket["metadata"]["activation_count"] == 1
    assert bucket["metadata"]["last_active"] != "2026-05-04T08:00:00+00:00"
    assert embedding_engine.calls[0][0] == bucket_id
    assert "现在再看到它" in embedding_engine.calls[0][1]


@pytest.mark.asyncio
async def test_hold_feel_with_source_writes_comment_not_digested(monkeypatch, bucket_mgr, decay_eng):
    import server

    source_id = await bucket_mgr.create(
        content="小雨说这段记忆以后还要回来看。",
        name="可回看的记忆",
        domain=["恋爱"],
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    embedding_engine = CapturingEmbeddingEngine()
    monkeypatch.setattr(server, "embedding_engine", embedding_engine)

    result = await server.hold(
        content="我现在看到它，觉得这里有一种被认出来的安静。",
        feel=True,
        source_bucket=source_id,
        valence=0.76,
        arousal=0.31,
    )
    bucket = await bucket_mgr.get(source_id)

    assert result.startswith(f"年轮→{source_id}#")
    assert bucket["metadata"]["comment_count"] == 1
    assert bucket["metadata"]["comments"][0]["source"] == "hold(feel=True)"
    assert bucket["metadata"]["comments"][0]["content"].startswith("我现在看到它")
    assert bucket["metadata"]["model_valence"] == 0.76
    assert not bucket["metadata"].get("digested")
    assert embedding_engine.calls[0][0] == source_id
    assert "被认出来的安静" in embedding_engine.calls[0][1]


@pytest.mark.asyncio
async def test_hold_feel_without_source_creates_whisper(monkeypatch, bucket_mgr, decay_eng):
    import server

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    result = await server.hold(
        content="我突然想小雨了，这句没有源记忆。",
        tags="private_note",
        feel=True,
        valence=0.72,
        arousal=0.28,
    )
    bucket_id = result.split("→", 1)[1]
    bucket = await bucket_mgr.get(bucket_id)

    assert result.startswith("🫧whisper→")
    assert bucket["metadata"]["type"] == "feel"
    assert "whisper" in bucket["metadata"]["tags"]
    assert "private_note" in bucket["metadata"]["tags"]
    assert not bucket["metadata"].get("period")
    assert not bucket["metadata"].get("date")


@pytest.mark.asyncio
async def test_hold_whisper_creates_independent_feel(monkeypatch, bucket_mgr, decay_eng):
    import server

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    result = await server.hold(
        content="这句是没有源记忆的悄悄话。",
        tags="private_note",
        whisper=True,
        valence=0.73,
        arousal=0.29,
    )
    bucket_id = result.split("→", 1)[1]
    bucket = await bucket_mgr.get(bucket_id)

    assert result.startswith("🫧whisper→")
    assert bucket["metadata"]["type"] == "feel"
    assert "whisper" in bucket["metadata"]["tags"]
    assert "private_note" in bucket["metadata"]["tags"]
    assert bucket["metadata"]["valence"] == 0.73
    assert bucket["metadata"]["arousal"] == 0.29


@pytest.mark.asyncio
async def test_hold_whisper_rejects_source_bucket(monkeypatch, bucket_mgr, decay_eng):
    import server

    source_id = await bucket_mgr.create(content="源记忆", name="源记忆")
    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())

    result = await server.hold(
        content="这句不应该挂源。",
        whisper=True,
        source_bucket=source_id,
    )

    assert "whisper 不需要 source_bucket" in result


@pytest.mark.asyncio
async def test_breath_whisper_reads_only_whisper_feels(monkeypatch, bucket_mgr, decay_eng):
    import server

    whisper_id = await bucket_mgr.create(
        content="这是一句悄悄话。",
        name="悄悄话",
        tags=["whisper"],
        bucket_type="feel",
        created="2026-05-22T08:00:00+00:00",
    )
    daily_id = await bucket_mgr.create(
        content="这是一条日印象。",
        name="日印象",
        tags=["relationship_weather", "daily_impression"],
        bucket_type="feel",
        created="2026-05-22T09:00:00+00:00",
        period="daily",
        date="2026-05-22",
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)

    result = await server.breath(domain="whisper")
    all_feels = await server.breath(domain="feel")

    assert "=== 你留下的 whisper ===" in result
    assert f"[bucket_id:{whisper_id}]" in result
    assert f"[bucket_id:{daily_id}]" not in result
    assert f"[bucket_id:{whisper_id}]" in all_feels
    assert f"[bucket_id:{daily_id}]" in all_feels


@pytest.mark.asyncio
async def test_hold_returns_readonly_related_memory_without_merging(monkeypatch, bucket_mgr, decay_eng):
    import server

    old_id = await bucket_mgr.create(
        content="小雨和 Haven 在旧窗口讨论过年轮评论，想让记忆下面挂不同时间的感受。",
        name="旧年轮设想",
        tags=["年轮"],
        domain=["恋爱"],
        importance=7,
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    monkeypatch.setattr(server, "embedding_engine", DummyEmbeddingEngine())
    monkeypatch.setattr(server, "dehydrator", DummyDehydrator())
    monkeypatch.setattr(server, "_queue_memory_enrichment", lambda bucket_id: None)

    result = await server.hold(
        content="小雨决定把年轮评论先落地，让旧记忆读到时可以多一层当下感受。",
        tags="年轮",
        importance=6,
    )
    all_buckets = await bucket_mgr.list_all(include_archive=True)

    assert "新建→" in result
    assert "旧记忆(只读，不触碰)" in result
    assert f"[bucket_id:{old_id}]" in result
    assert len([b for b in all_buckets if b["metadata"].get("type") == "dynamic"]) == 2


@pytest.mark.asyncio
async def test_resurface_prefers_long_dormant_memory_without_touching(monkeypatch, bucket_mgr, decay_eng):
    import server

    old_id = await bucket_mgr.create(
        content="很久没碰过的旧记忆。",
        name="久未触碰",
        last_active="2026-01-01T00:00:00+00:00",
    )
    recent_id = await bucket_mgr.create(
        content="刚刚碰过的新记忆。",
        name="刚碰过",
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)

    result = await server.resurface(max_results=1, include_archive=True)
    old_after = await bucket_mgr.get(old_id)
    recent_after = await bucket_mgr.get(recent_id)

    assert f"[bucket_id:{old_id}]" in result
    assert f"[bucket_id:{recent_id}]" not in result
    assert old_after["metadata"]["last_active"] == "2026-01-01T00:00:00+00:00"
    assert recent_after["metadata"]["activation_count"] == 0


@pytest.mark.asyncio
async def test_resurface_includes_archived_buckets_by_default(monkeypatch, bucket_mgr, decay_eng):
    import server

    archived_id = await bucket_mgr.create(
        content="归档以后也可以在久未触碰时浮现。",
        name="归档旧记忆",
        last_active="2026-01-01T00:00:00+00:00",
    )
    await bucket_mgr.archive(archived_id)
    await bucket_mgr.create(
        content="较新的普通记忆。",
        name="较新普通记忆",
        last_active="2026-05-01T00:00:00+00:00",
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)

    result = await server.resurface(max_results=1)

    assert f"[bucket_id:{archived_id}]" in result
    assert "归档" in result


@pytest.mark.asyncio
async def test_trace_anchor_respects_age_rule(monkeypatch, bucket_mgr, decay_eng):
    import server

    bucket_id = await bucket_mgr.create(
        content="刚刚发生的事先放着，等它自己留下重量。",
        name="刚发生",
        created="2026-05-19T02:00:00+00:00",
        last_active="2026-05-19T02:00:00+00:00",
    )

    monkeypatch.setattr(server, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(server, "decay_engine", decay_eng)
    monkeypatch.setitem(server.config, "anchor", {"max_count": 24, "min_age_hours": 999999})

    result = await server.trace(bucket_id=bucket_id, anchor=1)
    bucket = await bucket_mgr.get(bucket_id)

    assert "还太新" in result
    assert not bucket["metadata"].get("anchor")


@pytest.mark.asyncio
async def test_dashboard_auth_setup_uses_state_dir(monkeypatch, test_config):
    import server

    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(server, "config", test_config)
    monkeypatch.setattr(server, "_dashboard_sessions", {})

    response = await server.auth_setup(DummyRequest({"password": "secret1"}))
    auth_file = os.path.join(test_config["state_dir"], ".dashboard_auth.json")

    assert response.status_code == 200
    assert os.path.exists(auth_file)


def test_chatgpt_oauth_provider_issues_single_use_codes():
    import server

    provider = server.ChatGptOAuthProvider(
        client_id="client",
        client_secret="secret",
        access_token="access",
        refresh_token="refresh",
        public_base_url="https://23456544321123.asia/ombre",
    )
    redirect_uri = "https://chatgpt.com/connector/oauth/test"

    code = provider.create_authorization_code(redirect_uri)

    assert provider.enabled is True
    assert provider.token_auth_methods == ["client_secret_post", "client_secret_basic"]
    assert provider.consume_authorization_code(code, redirect_uri) is True
    assert provider.consume_authorization_code(code, redirect_uri) is False
    assert provider.valid_access_token("access") is True
    assert provider.valid_refresh_token("refresh") is True


@pytest.mark.asyncio
async def test_chatgpt_oauth_middleware_protects_only_configured_host():
    import server

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    provider = server.ChatGptOAuthProvider(
        client_id="client",
        access_token="access",
        public_base_url="https://23456544321123.asia/ombre",
    )
    middleware = server.OmbreChatGptOAuthMiddleware(app, provider, {"23456544321123.asia"})

    async def call(headers):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(
            {"type": "http", "method": "GET", "path": "/mcp", "headers": headers},
            receive,
            send,
        )
        return next(message["status"] for message in messages if message["type"] == "http.response.start")

    assert await call([(b"host", b"23456544321123.asia")]) == 401
    assert await call([(b"host", b"8.136.154.242")]) == 204
    assert await call(
        [
            (b"host", b"23456544321123.asia"),
            (b"authorization", b"Bearer access"),
        ]
    ) == 204
