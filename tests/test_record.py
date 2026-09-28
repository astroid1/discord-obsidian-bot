import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from dob.models import ActionItemOut, IngestItem, Segment
from dob.pipeline import NullProgress
from dob.record import (
    BYTES_PER_FRAME,
    SAMPLE_RATE,
    MeetingExtractor,
    TrackWriter,
    merge_segments,
)
from dob.state import State
from dob.tasks import Task, render_card, resolve_owner, suggestions_from
from dob.transcribe import FakeTranscriber

WED = date(2026, 9, 9)
PEOPLE = {1: "Jordan Blake", 2: "Sam", 3: "Marko Horvat"}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _chunk(ms=20, value=1):
    frames = SAMPLE_RATE * ms // 1000
    return bytes([value, 0]) * 2 * frames  # stereo s16le


def test_track_writer_pads_gaps_on_a_shared_timeline(tmp_path: Path):
    clock = Clock()
    w = TrackWriter(tmp_path / "rec", names={1: "Jordan Blake"}, clock=clock)
    clock.t += 0.02
    w.write_pcm(1, "jordanb", _chunk())
    clock.t += 1.0  # Sam starts talking a second later
    w.write_pcm(2, "samk", _chunk())
    clock.t += 0.03  # Jordan was silent for about a second: padded too
    w.write_pcm(1, "jordanb", _chunk())
    jordan, sam = w.tracks[1], w.tracks[2]
    assert jordan.name == "Jordan Blake" and sam.name == "samk"
    assert abs(jordan.frames - int(1.05 * SAMPLE_RATE)) <= 1
    # Sam's first chunk ends at 1.02 s, so ~1.0 s of silence is written before it
    assert abs(sam.frames - int(1.02 * SAMPLE_RATE)) <= 1

    manifest = w.finish(channel="General", requested_by="Jordan Blake")
    assert (tmp_path / "rec" / "2.pcm").stat().st_size == sam.frames * BYTES_PER_FRAME
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert m["channel"] == "General" and m["requested_by"] == "Jordan Blake"
    assert [t["name"] for t in m["tracks"]] == ["Jordan Blake", "samk"]
    before = w.tracks[1].frames
    w.write_pcm(1, "x", _chunk())  # writes after finish are ignored
    assert w.tracks[1].frames == before


def test_track_writer_ignores_jitter(tmp_path: Path):
    clock = Clock()
    w = TrackWriter(tmp_path / "rec", clock=clock)
    clock.t += 0.02
    w.write_pcm(1, "a", _chunk())
    clock.t += 0.07  # 50 ms late: below the 100 ms padding threshold
    w.write_pcm(1, "a", _chunk())
    assert w.tracks[1].frames == 2 * 960


def test_merge_segments_orders_by_time_and_names_speakers():
    merged = merge_segments(
        [
            (
                "Jordan Blake",
                [Segment(start=0, end=2, text="Hi"), Segment(start=5, end=6, text="Bye")],
            ),
            ("Sam", [Segment(start=2.5, end=4, text=" Hey "), Segment(start=7, end=8, text="  ")]),
        ]
    )
    assert [(s.speaker, s.text) for s in merged] == [
        ("Jordan Blake", "Hi"),
        ("Sam", "Hey"),
        ("Jordan Blake", "Bye"),
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_meeting_extractor_end_to_end(tmp_path: Path):
    clock = Clock()
    w = TrackWriter(tmp_path / "rec", names=PEOPLE, clock=clock)
    for _ in range(50):  # one second each
        clock.t += 0.02
        w.write_pcm(1, "a", _chunk())
        w.write_pcm(2, "e", _chunk())
    manifest = w.finish(channel="General", requested_by="Jordan Blake")
    item = IngestItem(
        source="meeting", original_name="Meeting", source_ref="meeting:x", local_path=manifest
    )
    ex = await MeetingExtractor(FakeTranscriber()).extract(item, NullProgress())
    assert ex.kind == "recording"
    assert {s.speaker for s in ex.segments} == {"Jordan Blake", "Sam"}
    assert [s.start for s in ex.segments] == sorted(s.start for s in ex.segments)
    assert ex.meta["speakers"] == "Jordan Blake, Sam" and ex.meta["voice_channel"] == "General"
    assert ex.text.splitlines()[0].startswith(("Jordan Blake: ", "Sam: "))


async def test_meeting_extractor_rejects_silence(tmp_path: Path):
    w = TrackWriter(tmp_path / "rec", clock=Clock())
    manifest = w.finish(channel="General", requested_by="Jordan")
    item = IngestItem(source="meeting", original_name="M", source_ref="m", local_path=manifest)
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    with pytest.raises(ValueError, match="nobody spoke"):
        await MeetingExtractor(FakeTranscriber()).extract(item, NullProgress())


def test_resolve_owner():
    assert resolve_owner("Sam", PEOPLE) == (2, "Sam")
    assert resolve_owner("jordan", PEOPLE) == (1, "Jordan Blake")
    assert resolve_owner("Jordan Blake", PEOPLE) == (1, "Jordan Blake")
    assert resolve_owner("Riley", PEOPLE) == (None, "Riley")
    assert resolve_owner(None, PEOPLE) == (None, None)
    assert resolve_owner("Jordan", {1: "Jordan Blake", 4: "Jordan Smith"}) == (None, "Jordan")


def test_suggestions_from_cleans_dedupes_and_limits():
    items = [
        ActionItemOut(task="  Send Sam the renewal clause. ", owner="Jordan", due="2026-09-12"),
        ActionItemOut(task="send sam the renewal clause", owner="Sam"),
        ActionItemOut(task="Call Northside Plumbing", owner="Sam", due="friday"),
        ActionItemOut(task="Pick a CRM", owner=None, due="someday"),
        ActionItemOut(task="   "),
    ]
    out = suggestions_from(items, PEOPLE, WED)
    assert [(s.title, s.owner_id, s.owner_name, s.due) for s in out] == [
        ("Send Sam the renewal clause", 1, "Jordan Blake", "2026-09-12"),
        ("Call Northside Plumbing", 2, "Sam", "2026-09-11"),
        ("Pick a CRM", None, None, None),
    ]
    assert len(suggestions_from(items, PEOPLE, WED, limit=1)) == 1


def test_suggested_card_and_state(tmp_path: Path):
    st = State(tmp_path / "state.db")
    tid = st.add_task(
        title="Call Northside Plumbing",
        notes="From: Partner sync",
        assignee_name="Riley",
        due="2026-09-11",
        status="suggested",
        source_note="sources/recordings/x.md",
    )
    t = Task.from_row(st.get_task(tid))
    assert t.status == "suggested" and t.source_note == "sources/recordings/x.md"
    card = render_card(t, WED)
    assert card.startswith("💡 **Suggested #1 · Call Northside Plumbing**")
    assert "Riley · due Fri (2026-09-11)" in card and "React ✅" in card
    assert st.task_exists("sources/recordings/x.md", "call northside plumbing")
    assert not st.task_exists("sources/recordings/y.md", "Call Northside Plumbing")
    assert [r["id"] for r in st.list_tasks(status="open")] == []


def test_state_migrates_old_tasks_table(tmp_path: Path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        notes TEXT, assignee_id INTEGER, assignee_name TEXT, due TEXT,
        status TEXT NOT NULL DEFAULT 'open', created_by_id INTEGER, created_by TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_at TEXT, message_id INTEGER,
        source_url TEXT);
        INSERT INTO tasks(title, created_at, updated_at) VALUES ('old', 'x', 'x');"""
    )
    con.close()
    st = State(db)
    assert Task.from_row(st.get_task(1)).source_note is None
    st.add_task(title="new", source_note="n.md")
    assert st.task_exists("n.md", "NEW")


async def test_pipeline_result_carries_kind_and_action_items(tmp_path, fake_pipeline):
    p = tmp_path / "notes.md"
    p.write_text("Meeting notes\nTODO: draft the pricing page\n", encoding="utf-8")
    item = IngestItem(
        source="inbox", original_name=p.name, source_ref="inbox:notes.md", local_path=p
    )
    result = await fake_pipeline.run(item)
    assert result.note is not None and result.note.kind == "document"
    assert [a.task for a in result.note.action_items] == ["draft the pricing page"]
