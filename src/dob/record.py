"""Meeting capture: record a Discord voice channel one track per speaker, then transcribe.

Recording (needs the optional `voice` extra: PyNaCl, davey, discord-ext-voice-recv):

- `TrackWriter` appends each speaker's 48 kHz stereo PCM to its own raw file and pads silence
  from the wall clock, so every track shares one timeline and segment times line up.
- `install_dave_decrypt()` adds the step discord-ext-voice-recv lacks: Discord voice is end-to-end
  encrypted (DAVE), so after transport decryption each frame still has to go through the
  connection's DAVE session before it is Opus again.
- `finish()` writes `manifest.json`; that file is what enters the pipeline.

Transcription (no Discord needed, so it is unit-testable):

- `MeetingExtractor` converts every track to 16 kHz mono, transcribes it alone with diarization
  off, labels its segments with the speaker's name, and merges all segments by start time.
  One track per person makes speaker labels exact and free.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .extract import UnsupportedTypeError
from .extract_media import _run, probe_duration
from .models import Extracted, IngestItem, Segment

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS  # s16le stereo
GAP_PAD_FRAMES = SAMPLE_RATE // 10  # only pad gaps longer than 100 ms; smaller ones are jitter
MANIFEST = "manifest.json"


# --- recording ---------------------------------------------------------------------------------


@dataclass
class Track:
    user_id: int
    name: str
    file: str
    frames: int = 0


@dataclass
class TrackWriter:
    """Thread-safe per-speaker PCM writer. `write()` is called from the voice reader thread."""

    root: Path
    names: dict[int, str] = field(default_factory=dict)
    clock: object = time.monotonic
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.t0 = self.clock()
        self.tracks: dict[int, Track] = {}
        self._fh: dict[int, object] = {}
        self._lock = threading.Lock()
        self.closed = False

    def write_pcm(self, user_id: int, display_name: str, pcm: bytes) -> None:
        if not pcm or self.closed:
            return
        n = len(pcm) // BYTES_PER_FRAME
        with self._lock:
            tr = self.tracks.get(user_id)
            if tr is None:
                name = self.names.get(user_id) or display_name or str(user_id)
                tr = Track(user_id=user_id, name=name, file=f"{user_id}.pcm")
                self.tracks[user_id] = tr
                self._fh[user_id] = (self.root / tr.file).open("ab")
            fh = self._fh[user_id]
            start = int((self.clock() - self.t0) * SAMPLE_RATE) - n
            gap = start - tr.frames
            if gap > GAP_PAD_FRAMES:
                fh.write(b"\x00" * (gap * BYTES_PER_FRAME))
                tr.frames += gap
            fh.write(pcm[: n * BYTES_PER_FRAME])
            tr.frames += n

    def elapsed(self) -> float:
        return self.clock() - self.t0

    def finish(self, *, channel: str, requested_by: str) -> Path:
        with self._lock:
            self.closed = True
            for fh in self._fh.values():
                fh.close()
            self._fh.clear()
            manifest = {
                "channel": channel,
                "requested_by": requested_by,
                "started_at": self.started_at,
                "duration_s": round(self.elapsed(), 1),
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "tracks": [
                    {"user_id": t.user_id, "name": t.name, "file": t.file, "frames": t.frames}
                    for t in self.tracks.values()
                    if t.frames > 0
                ],
            }
        path = self.root / MANIFEST
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return path


def new_recording_dir(base: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return base / f"{stamp}-{uuid.uuid4().hex[:6]}"


def make_sink(writer: TrackWriter):
    """An AudioSink for discord-ext-voice-recv that feeds `writer`. Imported lazily."""
    from discord.ext import voice_recv

    class _Sink(voice_recv.AudioSink):
        def wants_opus(self) -> bool:
            return False

        def write(self, user, data) -> None:
            if user is None or getattr(user, "bot", False) or not data.pcm:
                return
            writer.write_pcm(user.id, getattr(user, "display_name", "") or user.name, data.pcm)

        def cleanup(self) -> None:
            pass

    return _Sink()


def install_dave_decrypt(vc) -> None:
    """Run every received Opus frame through the connection's DAVE session.

    discord-ext-voice-recv strips transport encryption only. With DAVE (Discord's E2EE voice)
    the payload is still encrypted per sender; without this step the Opus decoder gets noise.
    """
    import davey
    from discord.ext.voice_recv.rtp import OPUS_SILENCE

    decryptor = vc._reader.decryptor
    transport_decrypt = decryptor.decrypt_rtp
    conn = vc._connection
    warned: set[int] = set()

    def decrypt_rtp(packet) -> bytes:
        data = transport_decrypt(packet)
        session = getattr(conn, "dave_session", None)
        if not getattr(conn, "dave_protocol_version", 0) or session is None or not session.ready:
            return data
        if data == OPUS_SILENCE:
            return data
        uid = vc._get_id_from_ssrc(packet.ssrc)
        if uid is None:
            return OPUS_SILENCE
        try:
            return session.decrypt(uid, davey.MediaType.audio, data)
        except Exception as e:  # noqa: BLE001 - one bad frame must not stop the recording
            if session.can_passthrough(uid):
                return data
            if uid not in warned:
                warned.add(uid)
                log.warning("DAVE decrypt failed for user %s: %s", uid, e)
            return OPUS_SILENCE

    decryptor.decrypt_rtp = decrypt_rtp


# --- transcription -----------------------------------------------------------------------------


def merge_segments(per_speaker: list[tuple[str, list[Segment]]]) -> list[Segment]:
    out: list[Segment] = []
    for name, segs in per_speaker:
        for s in segs:
            out.append(Segment(start=s.start, end=s.end, text=s.text.strip(), speaker=name))
    out.sort(key=lambda s: (s.start, s.end))
    return [s for s in out if s.text]


async def pcm_to_wav(src: Path, dst: Path, *, sample_rate: int, channels: int) -> None:
    rc, _, err = await _run(
        "ffmpeg", "-y", "-v", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels), "-i", str(src),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst),
    )  # fmt: skip
    if rc != 0 or not dst.exists():
        raise UnsupportedTypeError(f"ffmpeg could not convert {src.name}: {err.strip()[-300:]}")


class MeetingExtractor:
    """Extractor for `source="meeting"` items whose local_path is a recording's manifest.json."""

    def __init__(self, transcriber):
        self.transcriber = transcriber
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH; it is required for meeting capture")

    async def extract(self, item: IngestItem, progress) -> Extracted:
        manifest_path = Path(item.local_path)  # type: ignore[arg-type]
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        tracks = m.get("tracks") or []
        if not tracks:
            raise UnsupportedTypeError("nobody spoke during the recording")
        per_speaker: list[tuple[str, list[Segment]]] = []
        language = None
        duration = float(m.get("duration_s") or 0)
        with tempfile.TemporaryDirectory(prefix="dob-meet-") as tmp:
            for i, tr in enumerate(tracks, 1):
                src = manifest_path.parent / tr["file"]
                if not src.exists():
                    log.warning("meeting track missing: %s", src)
                    continue
                wav = Path(tmp) / f"{tr['user_id']}.wav"
                await progress.update("converting", f"{tr['name']} ({i}/{len(tracks)})")
                await pcm_to_wav(
                    src, wav, sample_rate=int(m.get("sample_rate") or SAMPLE_RATE),
                    channels=int(m.get("channels") or CHANNELS),
                )  # fmt: skip
                duration = max(duration, await probe_duration(wav))
                await progress.update("transcribing", f"{tr['name']} ({i}/{len(tracks)})")
                t = await self.transcriber.transcribe(wav, diarize=False)
                language = language or t.language
                per_speaker.append((tr["name"], t.segments))
        segments = merge_segments(per_speaker)
        if not segments:
            raise UnsupportedTypeError("no speech detected in the recording")
        speakers = [name for name, segs in per_speaker if segs]
        text = "\n".join(f"{s.speaker}: {s.text}" for s in segments)
        return Extracted(
            item=item,
            kind="recording",
            text=text,
            segments=segments,
            duration_s=duration or segments[-1].end,
            language=language,
            meta={
                "format": "discord-voice",
                "speakers": ", ".join(speakers),
                "voice_channel": str(m.get("channel") or ""),
                "recorded_by": str(m.get("requested_by") or ""),
            },
        )


__all__ = [
    "MANIFEST",
    "MeetingExtractor",
    "Track",
    "TrackWriter",
    "install_dave_decrypt",
    "make_sink",
    "merge_segments",
    "new_recording_dir",
]
