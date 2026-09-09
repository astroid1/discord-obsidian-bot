import pytest

from conftest import local_item
from dob.extract import UnsupportedTypeError
from dob.vault import load_note


async def test_text_end_to_end(fake_pipeline, sample_md):
    r = await fake_pipeline.run(local_item(sample_md))
    assert r.status == "done" and r.note is not None
    root = fake_pipeline.vault.root
    meta, body = load_note(root / r.note.note_path)
    assert meta["title"] == "Weekly sync notes"
    assert (root / "people" / "Jordan Blake.md").exists()
    assert (root / "people" / "Marko Horvat.md").exists()
    assert len(list((root / "decisions").glob("*.md"))) == 1
    assert "- [ ] draft the pricing page copy by Friday." in body
    row = fake_pipeline.state.get(r.note and fake_pipeline.state.recent(1)[0]["id"])
    assert row["status"] == "done" and row["note_path"] == r.note.note_path


async def test_duplicate_skipped_and_force(fake_pipeline, sample_md, fake_structurer):
    r1 = await fake_pipeline.run(local_item(sample_md))
    r2 = await fake_pipeline.run(local_item(sample_md))
    assert r2.status == "skipped" and r1.note.link in r2.message
    assert fake_structurer.calls == 1
    r3 = await fake_pipeline.run(local_item(sample_md, force=True))
    assert r3.status == "done" and r3.note.note_path == r1.note.note_path
    assert fake_structurer.calls == 2


async def test_unsupported_type_fails_ledger(fake_pipeline, tmp_path):
    p = tmp_path / "blob.xyz"
    p.write_bytes(b"\x00\x01")
    item = local_item(p)
    with pytest.raises(UnsupportedTypeError):
        await fake_pipeline.run(item)
    row = fake_pipeline.state.get(item.id)
    assert row["status"] == "failed" and "unsupported file type .xyz" in row["error"]


async def test_empty_file_fails(fake_pipeline, tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n")
    with pytest.raises(UnsupportedTypeError, match="empty"):
        await fake_pipeline.run(local_item(p))
