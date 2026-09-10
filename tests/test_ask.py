from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dob.ask import (
    VaultIndex,
    collect_week,
    next_run,
    read_notes,
    render_week,
    split_message,
    tokens,
)
from dob.vault import VaultWriter, dump_note, load_note

TZ = ZoneInfo("America/New_York")


def _note(root: Path, rel: str, meta: dict, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_note(meta, body), encoding="utf-8")


def _seed(root: Path) -> None:
    _note(
        root,
        "sources/documents/2026-09-08 pricing-guide.md",
        {
            "type": "document",
            "title": "Pricing guide",
            "date": "2026-09-08",
            "ingested_at": "2026-09-09T10:00:00",
        },
        "# Pricing guide\n\n## Summary\nBase plan $49 per month with renewal.\n\n## Action items\n- [ ] Send Sam the renewal clause — [[Jordan Blake]]\n",
    )
    _note(
        root,
        "decisions/2026-09-06 fixed-price-with-renewal.md",
        {"type": "decision", "title": "Fixed price with renewal", "decided_on": "2026-09-06"},
        "# Fixed price with renewal\n\n## Statement\nCharge a fixed base with renewal fees.\n\n## Mentions\n",
    )
    _note(
        root,
        "people/Sam.md",
        {"type": "person", "name": "Sam", "created": "2026-08-31", "updated": "2026-09-08"},
        "# Sam\n\nCo-founder.\n\n## Mentions\n",
    )
    _note(
        root,
        "chat/general/2026-09-08.md",
        {"type": "chat-log", "channel": "#general", "date": "2026-09-08", "message_count": 3},
        "# #general — 2026-09-08\n\n- **10:00** [[Sam]]: hello\n",
    )
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "workspace.md").write_text("ignored", encoding="utf-8")


def test_tokens_drop_stopwords():
    assert tokens("What is the price of the base plan?") == ["price", "base", "plan"]


def test_split_message_respects_lines_and_limit():
    text = "\n".join(f"line {i} " + "x" * 40 for i in range(100))
    chunks = split_message(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks).count("line") == 100
    assert split_message("x" * 1200, limit=500) == ["x" * 500, "x" * 500, "x" * 200]
    assert split_message("   ") == []


def test_read_notes_skips_obsidian_and_home(tmp_path: Path):
    _seed(tmp_path)
    (tmp_path / "Home.md").write_text("# Home", encoding="utf-8")
    rels = {n.rel for n in read_notes(tmp_path)}
    assert "people/Sam.md" in rels
    assert not any(r.startswith(".obsidian") for r in rels)
    assert "Home.md" not in rels


def test_search_prefers_title_and_decision_pages(tmp_path: Path):
    _seed(tmp_path)
    hits = VaultIndex(tmp_path).search("what did we decide about renewal pricing?")
    assert hits, "expected matches"
    assert hits[0].kind == "decision"
    assert {h.rel for h in hits} >= {
        "decisions/2026-09-06 fixed-price-with-renewal.md",
        "sources/documents/2026-09-08 pricing-guide.md",
    }
    assert VaultIndex(tmp_path).search("the of and") == []


def test_search_respects_budget(tmp_path: Path):
    _seed(tmp_path)
    hits = VaultIndex(tmp_path).search("pricing renewal", budget_chars=8, note_chars=5)
    assert len(hits) == 1 and len(hits[0].body) <= 5


def test_collect_and_render_week(tmp_path: Path):
    _seed(tmp_path)
    w = collect_week(tmp_path, since=date(2026, 9, 5), until=date(2026, 9, 11))
    assert [n.title for n in w.sources] == ["Pricing guide"]
    assert [n.title for n in w.decisions] == ["Fixed price with renewal"]
    assert w.action_items == [("Send Sam the renewal clause — [[Jordan Blake]]", "Pricing guide")]
    assert w.new_entities == [] and w.chat_days == 1
    text = render_week(w)
    assert text.startswith("**Week in review · 2026-09-05 → 2026-09-11**")
    assert "• Fixed price with renewal — Charge a fixed base with renewal fees." in text
    assert "Base plan $49" in text and "Send Sam the renewal clause" in text

    quiet = collect_week(tmp_path, since=date(2020, 1, 1), until=date(2020, 1, 7))
    assert "quiet week" in render_week(quiet)


def test_next_run():
    wed = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)  # a Wednesday
    assert next_run(wed, 0, 8) == datetime(2026, 9, 14, 8, 0, tzinfo=TZ)
    assert next_run(wed, 2, 13) == datetime(2026, 9, 9, 13, 0, tzinfo=TZ)
    assert next_run(wed, 2, 12) == datetime(2026, 9, 16, 12, 0, tzinfo=TZ)


def test_write_decision_creates_then_reaffirms(tmp_path: Path):
    v = VaultWriter(tmp_path / "vault", git_commit=False)
    v.ensure_layout()
    _note(
        v.root,
        "people/Jordan Blake.md",
        {"type": "person", "name": "Jordan Blake"},
        "# Jordan Blake\n\n## Mentions\n",
    )
    rel, created = v.write_decision(
        title="Price by quote above 50 seats",
        statement="Large accounts get a custom quote.",
        rationale="Avoid underpricing.",
        alternatives="Public enterprise tier.",
        decided_by=["Jordan Blake", "Sam"],
        when=date(2026, 9, 10),
        source_url="https://discord.com/channels/1/2/3",
    )
    assert created and rel == "decisions/2026-09-10 price-by-quote-above-50-seats.md"
    meta, body = load_note(v.root / rel)
    assert meta["decided_by"] == ["[[Jordan Blake]]", "Sam"]
    assert meta["source"] == "https://discord.com/channels/1/2/3"
    assert "## Alternatives considered\nPublic enterprise tier." in body
    assert "https://discord.com/channels/1/2/3" in body
    assert "Price by quote" in (v.root / "Home.md").read_text(encoding="utf-8")

    rel2, created2 = v.write_decision(
        title="price by quote above 50 seats",
        statement="again",
        decided_by=["Sam"],
        when=date(2026, 9, 12),
        source_url="https://discord.com/channels/1/2/4",
    )
    assert rel2 == rel and not created2
    _, body2 = load_note(v.root / rel)
    assert "2026-09-12 — Reaffirmed" in body2 and body2.count("Reaffirmed") == 1
    # same message again is a no-op
    v.write_decision(
        title="Price by quote above 50 seats",
        statement="x",
        when=date(2026, 9, 12),
        source_url="https://discord.com/channels/1/2/4",
    )
    assert load_note(v.root / rel)[1].count("Reaffirmed") == 1
