from datetime import UTC, datetime

from dob.models import ChatMessage, IngestItem


def _item(**kw):
    return IngestItem(source="inbox", original_name="a.md", source_ref="inbox:a.md", **kw)


def test_ledger_transitions(tmp_state):
    it = _item()
    tmp_state.submit(it)
    assert tmp_state.get(it.id)["status"] == "queued"
    tmp_state.set_status(it.id, "fetching", sha256="abc", bump_attempts=True)
    tmp_state.set_status(it.id, "done", note_path="sources/documents/x.md")
    row = tmp_state.get(it.id)
    assert row["status"] == "done" and row["attempts"] == 1 and row["sha256"] == "abc"
    assert tmp_state.find_done_by_sha("abc")["id"] == it.id
    assert tmp_state.find_done_by_ref("inbox:a.md")["note_path"] == "sources/documents/x.md"
    assert tmp_state.counts() == {"done": 1}


def test_inflight_requeue(tmp_state):
    a, b, c = _item(), _item(), _item()
    for it in (a, b, c):
        tmp_state.submit(it)
    tmp_state.set_status(a.id, "structuring")
    tmp_state.set_status(b.id, "done")
    tmp_state.set_status(c.id, "failed", error="boom")
    ids = {i.id for i in tmp_state.inflight()}
    assert ids == {a.id}


def test_cursors(tmp_state):
    assert tmp_state.get_cursor(5) is None
    tmp_state.set_cursor(5, 100, parent_id=None, name="general")
    tmp_state.set_cursor(5, 200, parent_id=None, name="general")
    assert tmp_state.get_cursor(5) == 200
    tmp_state.set_cursor(6, 1, parent_id=5, name="thread")
    tmp_state.reset_cursors([5])
    assert tmp_state.get_cursor(5) is None and tmp_state.get_cursor(6) is None


def test_messages_roundtrip(tmp_state):
    m = ChatMessage(
        id=1,
        channel_id=20,
        author_id=100,
        author_name="jordan",
        created_at=datetime(2026, 9, 8, 13, 0, tzinfo=UTC),
        content="hi",
        jump_url="https://discord.com/channels/1/20/1",
    )
    tmp_state.upsert_messages([m], ["2026-09-08"])
    tmp_state.upsert_messages([m.model_copy(update={"content": "edited"})], ["2026-09-08"])
    got = tmp_state.messages_for_day(20, "2026-09-08")
    assert len(got) == 1 and got[0].content == "edited"
    assert tmp_state.day_counts(20, ["2026-09-08", "2026-09-09"]) == {"2026-09-08": 1}
    tmp_state.upsert_chat_day(20, "2026-09-08", 1, "chat/general/2026-09-08.md")
    tmp_state.set_digest(20, "2026-09-08", "sources/chat-digests/general/2026-09-08.md", 1)
    row = tmp_state.get_chat_day(20, "2026-09-08")
    assert row["digest_message_count"] == 1
