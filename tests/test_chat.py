import json
from datetime import UTC, datetime
from pathlib import Path

from dob.chat import ChatDayExtractor, digest_item, render_day_log, render_day_text, should_digest
from dob.models import ChatMessage

FIX = Path(__file__).parent / "fixtures" / "chat_messages.json"


def _msgs() -> list[ChatMessage]:
    raw = json.loads(FIX.read_text(encoding="utf-8"))
    return [ChatMessage.model_validate(m) for m in raw]


def test_day_log_render(bot_cfg):
    msgs = _msgs()
    meta, body = render_day_log(
        msgs,
        channel_name="general",
        day="2026-09-08",
        cfg=bot_cfg,
        note_for_message=lambda mid: (
            "sources/documents/2026-09-08 contract.md" if mid == 3 else None
        ),
        digest_link="sources/chat-digests/general/2026-09-08",
    )
    assert meta["type"] == "chat-log" and meta["message_count"] == 5
    assert meta["digest"] == "[[sources/chat-digests/general/2026-09-08]]"
    # 13:14 UTC -> 09:14 New York; configured people are wikilinked, others are @handles
    assert "- **09:14** [[Jordan Blake]]: Morning — did the vendor reply?" in body
    assert "- **09:16** [[Marko Horvat]] ↩ 09:14: Not yet, chasing today." in body
    assert "@guest: hi" in body
    assert (
        "📎 [contract-v2.pdf](https://cdn/contract-v2.pdf) → [[sources/documents/2026-09-08 contract]]"
        in body
    )
    assert '🧵 *thread: "Pricing page copy"*' in body
    assert "  - **11:02** [[Jordan Blake]]: first draft" in body
    assert body.index("🧵") > body.index("@guest")  # threads rendered after top-level messages


def test_day_text_and_extractor(bot_cfg, tmp_state):
    msgs = _msgs()
    tmp_state.upsert_messages(msgs, ["2026-09-08"] * len(msgs))
    text = render_day_text(msgs, channel_name="general", day="2026-09-08", cfg=bot_cfg)
    assert text.startswith("Channel #general, 2026-09-08")
    assert "[09:14] Jordan Blake: Morning" in text and '--- thread "Pricing page copy" ---' in text
    assert "[attachment: contract-v2.pdf]" in text

    import asyncio

    ex = asyncio.run(
        ChatDayExtractor(tmp_state, bot_cfg).extract(digest_item(20, "general", "2026-09-08"), None)
    )
    assert ex.kind == "chat-digest" and ex.meta["message_count"] == "5"
    assert ex.meta["threads"] == "Pricing page copy"


def test_should_digest_rules(bot_cfg, tmp_state):
    today = "2026-09-09"
    assert not should_digest(tmp_state, bot_cfg, 20, "2026-09-08", 3, today)  # below min
    assert should_digest(
        tmp_state, bot_cfg, 20, "2026-09-08", 5, today
    )  # closed day, no digest yet
    assert not should_digest(tmp_state, bot_cfg, 20, today, 50, today)  # today: wait for day close
    tmp_state.upsert_chat_day(20, "2026-09-08", 5, "chat/general/2026-09-08.md")
    tmp_state.set_digest(20, "2026-09-08", "sources/chat-digests/general/2026-09-08.md", 5)
    assert not should_digest(tmp_state, bot_cfg, 20, "2026-09-08", 9, today)  # grew by 4 < delta 10
    assert should_digest(tmp_state, bot_cfg, 20, "2026-09-08", 15, today)  # grew by 10


def test_timezone_day_boundary(bot_cfg):
    # 03:30 UTC on the 9th is still the 8th in New York
    dt = datetime(2026, 9, 9, 3, 30, tzinfo=UTC)
    assert dt.astimezone(bot_cfg.tz).date().isoformat() == "2026-09-08"
