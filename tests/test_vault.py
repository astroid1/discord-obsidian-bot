from datetime import UTC, datetime

from dob.models import (
    ActionItemOut,
    DecisionOut,
    DiscordRef,
    EntityOut,
    Extracted,
    IngestItem,
    Segment,
    Structured,
    StructuredOutput,
)
from dob.vault import load_note, safe_name, slugify


def _extracted(kind="document", text="hello world", sha="deadbeef" * 8, **item_kw):
    item = IngestItem(
        source="inbox", original_name="x.md", source_ref="inbox:x.md", sha256=sha, **item_kw
    )
    return Extracted(item=item, kind=kind, text=text)


def _structured(**kw):
    base = dict(
        suggested_title="Weekly sync",
        summary="We talked about pricing.",
        key_points=["pricing", "vendor"],
        entities=[
            EntityOut(
                type="person",
                name="Jordan Blake",
                aliases=["Jordan"],
                role_in_source="led",
                is_new=True,
                description="Founder.",
            ),
            EntityOut(
                type="project",
                name="Acme Website",
                aliases=[],
                role_in_source="discussed",
                is_new=True,
            ),
        ],
        decisions=[
            DecisionOut(
                title="Use Postgres for CRM", statement="Postgres it is.", decided_by=["Jordan Blake"]
            )
        ],
        action_items=[
            ActionItemOut(task="Draft pricing page", owner="Jordan Blake", due="2026-09-15")
        ],
        participants=["Jordan Blake"],
        tags=["Meeting", "pricing"],
    )
    base.update(kw)
    return Structured(output=StructuredOutput(**base), model="fake")


def test_helpers():
    assert slugify("Hello, World! 2026") == "hello-world-2026"
    assert safe_name('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"


def test_write_creates_everything(tmp_vault):
    note = tmp_vault.write(_extracted(), _structured())
    root = tmp_vault.root
    assert (root / note.note_path).exists()
    assert note.note_path.startswith("sources/documents/") and note.note_path.endswith(
        " weekly-sync.md"
    )
    assert set(note.entities_created) == {"Jordan Blake", "Acme Website"}
    assert note.decisions_created == ["Use Postgres for CRM"]
    meta, body = load_note(root / note.note_path)
    assert meta["type"] == "document" and meta["sha256"] == "deadbeef" * 8
    assert meta["tags"] == ["meeting", "pricing"]
    assert meta["participants"] == ["[[Jordan Blake]]"]
    assert "- [ ] Draft pricing page — [[Jordan Blake]] (due 2026-09-15)" in body
    assert "- People: [[Jordan Blake]]" in body and "- Projects: [[Acme Website]]" in body
    assert "## Decisions\n- [[decisions/" in body
    pmeta, pbody = load_note(root / "people" / "Jordan Blake.md")
    assert pmeta["aliases"] == ["Jordan"] and "Founder." in pbody
    assert f"[[{note.link}|Weekly sync]] — led" in pbody
    dmeta, dbody = load_note(next((root / "decisions").glob("*.md")))
    assert dmeta["decided_by"] == ["[[Jordan Blake]]"] and "Postgres it is." in dbody
    assert dmeta["source"] == f"[[{note.link}|Weekly sync]]"
    assert note.commit_sha  # git commit happened
    home = (root / "Home.md").read_text(encoding="utf-8")
    assert "Weekly sync" in home and "Draft pricing page" in home and "Use Postgres for CRM" in home


def test_append_only_and_dedupe(tmp_vault):
    tmp_vault.write(_extracted(), _structured())
    person = tmp_vault.root / "people" / "Jordan Blake.md"
    person.write_text(
        person.read_text(encoding="utf-8").replace("Founder.", "Founder. Hand-edited."),
        encoding="utf-8",
    )
    # second source mentions "Jordan" (alias) with a new alias, and reaffirms the decision
    s2 = _structured(
        suggested_title="Follow-up",
        entities=[
            EntityOut(
                type="person",
                name="Jordan",
                aliases=["jordan_blake"],
                role_in_source="asked",
                is_new=False,
            )
        ],
        decisions=[
            DecisionOut(
                title="Postgres for the CRM",
                statement="still",
                matches_existing="Use Postgres for CRM",
            )
        ],
    )
    n2 = tmp_vault.write(_extracted(sha="ab" * 32), s2)
    assert n2.entities_updated == ["Jordan Blake"] and n2.entities_created == []
    assert n2.decisions_created == []
    meta, body = load_note(person)
    assert "Hand-edited." in body
    assert meta["aliases"] == ["Jordan", "jordan_blake"]
    assert body.count("## Mentions") == 1 and body.count("Follow-up") == 1
    assert len(list((tmp_vault.root / "decisions").glob("*.md"))) == 1
    _, dbody = load_note(next((tmp_vault.root / "decisions").glob("*.md")))
    assert "Reaffirmed here" in dbody
    # writing the same source again does not duplicate the mention
    tmp_vault.write(_extracted(sha="ab" * 32, force=True), s2)
    _, body = load_note(person)
    assert body.count("Follow-up") == 1


def test_alias_collision_guard(tmp_vault):
    tmp_vault.write(_extracted(), _structured())
    s = _structured(
        suggested_title="Other",
        entities=[
            EntityOut(
                type="person",
                name="Jordan Smith",
                aliases=["Jordan", "Smithy"],
                role_in_source="x",
                is_new=True,
            )
        ],
        decisions=[],
    )
    tmp_vault.write(_extracted(sha="cd" * 32), s)
    meta, _ = load_note(tmp_vault.root / "people" / "Jordan Smith.md")
    assert meta["aliases"] == ["Smithy"]  # "Jordan" already belongs to Jordan Blake


def test_filename_collision(tmp_vault):
    n1 = tmp_vault.write(_extracted(sha="11" * 32), _structured(decisions=[], entities=[]))
    n2 = tmp_vault.write(_extracted(sha="22" * 32), _structured(decisions=[], entities=[]))
    assert n1.note_path != n2.note_path and n2.note_path.endswith("-22222222.md")


def test_recording_note_and_discord_meta(tmp_vault):
    ref = DiscordRef(
        guild_id=1, channel_id=2, channel_name="meetings", message_id=3, author_id=4,
        author_name="jordan", jump_url="https://discord.com/channels/1/2/3",
        created_at=datetime(2026, 9, 8, 3, 0, tzinfo=UTC),
    )  # fmt: skip
    ex = _extracted(kind="recording", text="a b", discord=ref)
    ex.segments = [
        Segment(start=0, end=1.5, text="hello", speaker="SPEAKER_00"),
        Segment(start=61, end=62, text="bye"),
    ]
    ex.duration_s = 62
    note = tmp_vault.write(ex, _structured(decisions=[], entities=[]))
    meta, body = load_note(tmp_vault.root / note.note_path)
    assert note.note_path.startswith("sources/recordings/2026-09-08 ")
    assert (
        meta["discord_link"] == ref.jump_url
        and meta["channel"] == "#meetings"
        and meta["duration"] == "00:01:02"
    )
    assert "[00:00:00] SPEAKER_00: hello" in body and "[00:01:01] bye" in body
