from __future__ import annotations

import shutil
import wave
from pathlib import Path

import pytest

from dob.config import BotConfig
from dob.extract import default_registry
from dob.models import INTERACTIVE, IngestItem
from dob.pipeline import NoFetcher, Pipeline
from dob.state import State
from dob.structure import FakeStructurer
from dob.vault import VaultWriter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def bot_cfg() -> BotConfig:
    return BotConfig(
        guild_id=1,
        watched_channels=[10],
        archived_channels=[20],
        allowed_user_ids=[100],
        people={100: "Jordan Blake", 101: "Marko Horvat"},
        timezone="America/New_York",
    )


@pytest.fixture
def tmp_vault(tmp_path: Path) -> VaultWriter:
    v = VaultWriter(tmp_path / "vault", git_commit=True)
    v.ensure_layout()
    return v


@pytest.fixture
def tmp_state(tmp_path: Path) -> State:
    return State(tmp_path / "data" / "state.db")


@pytest.fixture
def fake_structurer() -> FakeStructurer:
    return FakeStructurer()


@pytest.fixture
def fake_pipeline(tmp_path, tmp_vault, tmp_state, fake_structurer, bot_cfg) -> Pipeline:
    from dob.transcribe import FakeTranscriber

    reg = default_registry(
        transcriber=FakeTranscriber(),
        structurer=fake_structurer,
        cfg=bot_cfg,
        state=tmp_state,
        bot_cfg=bot_cfg,
    )
    return Pipeline(
        state=tmp_state,
        fetcher=NoFetcher(),
        extractors=reg,
        structurer=fake_structurer,
        vault=tmp_vault,
        cache_dir=tmp_path / "data" / "cache",
    )


def local_item(path: Path, **kw) -> IngestItem:
    return IngestItem(
        source="inbox",
        original_name=path.name,
        source_ref=f"inbox:{path.name}",
        local_path=path,
        priority=INTERACTIVE,
        **kw,
    )


@pytest.fixture
def sample_md(tmp_path: Path) -> Path:
    dst = tmp_path / "sample.md"
    shutil.copy(FIXTURES / "sample.md", dst)
    return dst


@pytest.fixture
def tiny_wav(tmp_path: Path) -> Path:
    p = tmp_path / "tiny.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return p
