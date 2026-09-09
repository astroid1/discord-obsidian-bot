from types import SimpleNamespace

import pytest

from dob.config import LLMConfig
from dob.models import Extracted, IngestItem, StructuredOutput
from dob.structure import KnownContext, KnownEntity, StructureError
from dob.structure_claude import ClaudeStructurer, chunk_text


def test_chunk_text_boundaries():
    text = "".join(f"line {i:03d}\n" for i in range(100))  # 9 chars per line
    chunks = chunk_text(text, size=200, overlap=30)
    assert len(chunks) > 1
    assert "".join(c for c in chunks).count("line 000") == 1
    # every chunk ends on a line boundary and respects size
    for c in chunks:
        assert c.endswith("\n") and len(c) <= 200 + 9
    # consecutive chunks overlap by roughly `overlap`
    assert chunks[0][-27:] in chunks[1]
    assert chunk_text("short", 100, 10) == ["short"]


class _FakeMessages:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def parse(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0)
        return SimpleNamespace(
            stop_reason="end_turn",
            parsed_output=out,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def _out(title):
    return StructuredOutput(suggested_title=title, summary="s", key_points=["k"])


def _extracted(text):
    return Extracted(
        item=IngestItem(source="inbox", original_name="x.txt", source_ref="inbox:x.txt"),
        kind="document",
        text=text,
    )


@pytest.fixture
def structurer():
    s = ClaudeStructurer(
        api_key="test",
        model="test-model",
        llm_cfg=LLMConfig(single_call_max_chars=100, chunk_chars=60, chunk_overlap_chars=10),
    )
    return s


async def test_single_call_below_threshold(structurer):
    fake = _FakeMessages([_out("one")])
    structurer.client = SimpleNamespace(messages=fake)
    ctx = KnownContext(
        entities=[
            KnownEntity(
                type="person", name="Jordan Blake", aliases=["Jordan"], path="people/Jordan Blake.md"
            )
        ]
    )
    res = await structurer.structure(_extracted("short text"), ctx)
    assert res.output.suggested_title == "one" and res.input_tokens == 10
    assert len(fake.calls) == 1
    user = fake.calls[0]["messages"][0]["content"]
    assert (
        "- person: Jordan Blake (aliases: Jordan)" in user
        and "<content>\nshort text\n</content>" in user
    )
    assert fake.calls[0]["output_format"] is StructuredOutput


async def test_map_reduce_above_threshold(structurer):
    text = "".join(f"line {i:03d}\n" for i in range(30))  # 270 chars > 100
    n_chunks = len(chunk_text(text, 60, 10))
    fake = _FakeMessages([_out(f"part{i}") for i in range(n_chunks)] + [_out("merged")])
    structurer.client = SimpleNamespace(messages=fake)
    res = await structurer.structure(_extracted(text), KnownContext())
    assert res.output.suggested_title == "merged"
    assert len(fake.calls) == n_chunks + 1
    assert "part 1 of" in fake.calls[0]["messages"][0]["content"]
    assert '<partial index="1">' in fake.calls[-1]["messages"][0]["content"]
    assert res.input_tokens == 10 * (n_chunks + 1)


async def test_refusal_raises(structurer):
    class Refusing:
        async def parse(self, **kw):
            return SimpleNamespace(stop_reason="refusal", parsed_output=None, usage=None)

    structurer.client = SimpleNamespace(messages=Refusing())
    with pytest.raises(StructureError, match="refused"):
        await structurer.structure(_extracted("x"), KnownContext())
