"""Audio/video extractor: ffmpeg -> 16 kHz mono wav -> Transcriber."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from .config import BotConfig
from .extract import ExtractorRegistry, UnsupportedTypeError
from .models import Extracted, IngestItem
from .transcribe import Transcriber

log = logging.getLogger(__name__)

AUDIO_EXT = {
    ".mp3",
    ".wav",
    ".m4a",
    ".ogg",
    ".oga",
    ".flac",
    ".opus",
    ".aac",
    ".wma",
    ".aiff",
    ".amr",
}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".3gp"}


class MediaExtractor:
    def __init__(self, transcriber: Transcriber, *, memo_max_minutes: float = 5.0):
        self.transcriber = transcriber
        self.memo_max_minutes = memo_max_minutes
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH; it is required for audio/video ingestion")

    async def extract(self, item: IngestItem, progress) -> Extracted:
        src = Path(item.local_path)  # type: ignore[arg-type]
        duration = await probe_duration(src)
        await progress.update("converting", f"{src.name} ({duration / 60:.1f} min)")
        # Never write next to the source: inbox/ and bind mounts may be read-only.
        with tempfile.TemporaryDirectory(prefix="dob-") as tmp:
            wav = Path(tmp) / "audio.16k.wav"
            await to_wav(src, wav)
            loop = asyncio.get_running_loop()

            def on_progress(fraction: float, detail: str) -> None:
                loop.call_soon_threadsafe(
                    asyncio.ensure_future, progress.update("transcribing", detail, fraction)
                )

            t = await self.transcriber.transcribe(wav, progress=on_progress)
        if not t.segments:
            raise UnsupportedTypeError("no speech detected")
        duration = t.duration_s or duration
        is_voice_msg = item.discord is not None and item.original_name.startswith("voice-message")
        kind = "memo" if is_voice_msg or duration < self.memo_max_minutes * 60 else "recording"
        meta = {"format": src.suffix.lstrip(".")}
        if t.speakers:
            meta["speakers"] = ", ".join(t.speakers)
        return Extracted(
            item=item,
            kind=kind,
            text=t.text,
            segments=t.segments,
            duration_s=duration,
            language=t.language,
            meta=meta,
        )


async def _run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    if shutil.which("ffprobe") is None:
        return 0.0
    rc, out, _ = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)
    )
    if rc != 0:
        return 0.0
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


async def to_wav(src: Path, dst: Path) -> None:
    rc, _, err = await _run(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dst),
    )
    if rc != 0 or not dst.exists():
        raise UnsupportedTypeError(f"ffmpeg could not decode {src.name}: {err.strip()[-300:]}")


def register_media(
    reg: ExtractorRegistry, *, transcriber: Transcriber, cfg: BotConfig | None
) -> None:
    ex = MediaExtractor(transcriber, memo_max_minutes=cfg.memo_max_minutes if cfg else 5.0)
    reg.register(AUDIO_EXT | VIDEO_EXT, ex)
