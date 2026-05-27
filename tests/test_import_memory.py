import pytest

from import_memory import IMPORT_EXTRACT_PROMPT, ImportEngine, chunk_turns, detect_and_parse
from utils import count_tokens_approx


class DummyDehydrator:
    api_available = True
    model = "dummy"

    async def merge(self, old_content: str, new_content: str) -> str:
        return f"{old_content}\n{new_content}"


def test_markdown_parser_supports_chinese_role_prefixes():
    text = "用户：这里有 <b>HTML</b> & 符号\n助手：收到，我不会改写原文。"

    turns = detect_and_parse(text, "chat.md")

    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("user", "这里有 <b>HTML</b> & 符号"),
        ("assistant", "收到，我不会改写原文。"),
    ]


def test_markdown_parser_supports_ascii_role_prefixes():
    text = "user: first\nassistant: second\nHuman: third\n**AI:** fourth"

    turns = detect_and_parse(text, "chat.md")

    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("user", "first"),
        ("assistant", "second"),
        ("user", "third"),
        ("assistant", "fourth"),
    ]


def test_oversized_markdown_turn_is_split_into_multiple_chunks():
    long_line = "这是一段很长的导入文本。" * 700
    turns = detect_and_parse("用户：" + long_line, "chat.md")

    chunks = chunk_turns(turns, target_tokens=800)

    assert len(chunks) > 1
    assert all(count_tokens_approx(chunk["content"]) <= 1200 for chunk in chunks)
    assert chunks[0]["content"].startswith("[用户] ")
    assert all("[上下文提示]" in chunk["content"] for chunk in chunks[1:])
    assert all("[本段内容]" in chunk["content"] for chunk in chunks[1:])


def test_oversized_markdown_overlap_is_marked_as_context_only():
    long_turn = "\n".join(
        f"第{i}段：这是一段需要连续理解的内容。" * 20
        for i in range(20)
    )
    turns = detect_and_parse("用户：" + long_turn, "chat.md")

    chunks = chunk_turns(turns, target_tokens=500)

    assert len(chunks) > 1
    assert "请不要从这里单独提取记忆" in chunks[1]["content"]
    assert chunks[1]["content"].index("[上下文提示]") < chunks[1]["content"].index("[本段内容]")


def test_import_prompt_allows_up_to_ten_items_per_chunk():
    assert "总条目数控制在 0~10 个" in IMPORT_EXTRACT_PROMPT
    assert "总条目数控制在 0~5 个" not in IMPORT_EXTRACT_PROMPT


def test_import_extraction_output_budget_supports_ten_items():
    class DummyClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    DummyClient.kwargs = kwargs

                    class Message:
                        content = "[]"

                    class Choice:
                        message = Message()

                    class Response:
                        choices = [Choice()]

                    return Response()

    class DummyDehydrator:
        api_available = True
        client = DummyClient()
        model = "dummy"

    engine = ImportEngine(
        {"buckets_dir": "buckets"},
        bucket_mgr=None,
        dehydrator=DummyDehydrator(),
        embedding_engine=None,
    )

    import asyncio

    asyncio.run(engine._extract_memories("hello"))

    assert DummyClient.kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_import_dedupes_existing_bucket_by_content(test_config, bucket_mgr):
    content = "小雨决定周末去杭州参加朋友婚礼，需要提前买高铁票并准备蓝色连衣裙。"
    await bucket_mgr.create(
        content=content,
        name="婚礼安排",
        domain=["人际"],
        tags=["婚礼", "杭州"],
    )
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())

    status = await engine._merge_or_create_item(
        {
            "name": "出行计划",
            "content": content,
            "domain": ["事务"],
            "tags": ["高铁", "连衣裙"],
            "importance": 6,
        }
    )

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert status == "duplicate"
    assert len(buckets) == 1


@pytest.mark.asyncio
async def test_import_dedupes_existing_bucket_by_similar_body(test_config, bucket_mgr):
    await bucket_mgr.create(
        content="小雨决定周末去杭州参加朋友婚礼，需要提前买高铁票并准备蓝色连衣裙。",
        name="婚礼安排",
        domain=["人际"],
        tags=["婚礼", "杭州"],
    )
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())

    status = await engine._merge_or_create_item(
        {
            "name": "杭州待办",
            "content": "周末小雨要去杭州参加朋友的婚礼，她需要提前订高铁票，也想带上蓝色连衣裙。",
            "domain": ["事务"],
            "tags": ["高铁", "待办"],
            "importance": 6,
        }
    )

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert status == "duplicate"
    assert len(buckets) == 1


@pytest.mark.asyncio
async def test_import_preserve_raw_still_dedupes(test_config, bucket_mgr):
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())
    item = {
        "name": "暗号",
        "content": "小雨说某个特殊暗号只属于她和 Haven，要保留原话。",
        "domain": ["恋爱"],
        "tags": ["暗号"],
        "importance": 8,
    }

    first_status = await engine._merge_or_create_item(item, preserve_raw=True)
    second_status = await engine._merge_or_create_item({**item, "name": "重复暗号"}, preserve_raw=True)

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert first_status == "raw"
    assert second_status == "duplicate"
    assert len(buckets) == 1


def test_import_dedupes_items_inside_same_extraction_batch(test_config, bucket_mgr):
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())
    items = [
        {"name": "A", "content": "同一条导入记忆。"},
        {"name": "B", "content": "同一条导入记忆。"},
        {"name": "C", "content": "另一条导入记忆。"},
    ]

    deduped = engine._dedupe_extracted_items(items)

    assert [item["name"] for item in deduped] == ["A", "C"]
