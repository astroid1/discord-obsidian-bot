import shutil

import pytest

from conftest import local_item


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_wav_transcribed_without_touching_source_dir(fake_pipeline, tiny_wav):
    before = sorted(p.name for p in tiny_wav.parent.iterdir())
    r = await fake_pipeline.run(local_item(tiny_wav))
    assert r.status == "done" and r.note.note_path.startswith("sources/memos/")
    assert sorted(p.name for p in tiny_wav.parent.iterdir()) == before  # no *.16k.wav left behind
    body = (fake_pipeline.vault.root / r.note.note_path).read_text(encoding="utf-8")
    assert "[00:00:02] SPEAKER_01: We decided to use Postgres" in body
    assert "duration: 00:00:06" in body
