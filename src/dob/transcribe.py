"""Transcriber protocol, faster-whisper implementation (lazy GPU singleton), optional pyannote.

faster-whisper / pyannote are imported inside methods so the package imports without the
`gpu` extra installed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import Segment

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]  # (fraction, detail)


@dataclass
class Transcript:
    segments: list[Segment]
    language: str | None = None
    duration_s: float = 0.0
    speakers: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()


class Transcriber(Protocol):
    async def transcribe(
        self, wav: Path, *, progress: ProgressFn | None = None, diarize: bool | None = None
    ) -> Transcript: ...


class FakeTranscriber:
    """Offline stand-in: returns three fixed segments with two speakers."""

    async def transcribe(
        self, wav: Path, *, progress: ProgressFn | None = None, diarize: bool | None = None
    ) -> Transcript:
        if progress:
            progress(1.0, "fake")
        segs = [
            Segment(
                start=0.0,
                end=2.0,
                text="Hello everyone, quick sync on pricing.",
                speaker="SPEAKER_00",
            ),
            Segment(
                start=2.0,
                end=4.0,
                text="We decided to use Postgres for the CRM.",
                speaker="SPEAKER_01",
            ),
            Segment(
                start=4.0,
                end=6.0,
                text="TODO: draft the pricing page by Friday.",
                speaker="SPEAKER_00",
            ),
        ]
        return Transcript(
            segments=segs, language="en", duration_s=6.0, speakers=["SPEAKER_00", "SPEAKER_01"]
        )


class FasterWhisperTranscriber:
    def __init__(
        self,
        *,
        model: str = "large-v3",
        device: str = "auto",
        compute_type: str = "int8_float16",
        models_path: Path | None = None,
        hf_token: str = "",
        diarize: bool = True,
        language: str | None = None,
    ):
        import faster_whisper  # noqa: F401 - fail fast at construction if not installed

        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.models_path = Path(models_path) if models_path else None
        self.hf_token = hf_token
        self.diarize = diarize and bool(hf_token)
        self.language = language
        self._model = None
        self._diar = None
        self._lock = threading.Lock()

    # --- lazy loading ------------------------------------------------------------------

    def _whisper(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    kw = {}
                    if self.models_path:
                        kw["download_root"] = str(self.models_path / "whisper")
                    log.info(
                        "loading whisper %s (%s, %s)",
                        self.model_name,
                        self.device,
                        self.compute_type,
                    )
                    try:
                        self._model = WhisperModel(
                            self.model_name,
                            device=self.device,
                            compute_type=self.compute_type,
                            **kw,
                        )
                    except (ValueError, RuntimeError) as e:
                        if self.device == "cpu":
                            raise
                        log.warning("GPU load failed (%s); falling back to CPU int8", e)
                        self._model = WhisperModel(
                            self.model_name, device="cpu", compute_type="int8", **kw
                        )
        return self._model

    def _diarizer(self):
        if self._diar is None and self.diarize:
            with self._lock:
                if self._diar is None:
                    try:
                        import torch
                        from pyannote.audio import Pipeline as PyannotePipeline

                        log.info("loading pyannote speaker-diarization-community-1")
                        pipe = PyannotePipeline.from_pretrained(
                            "pyannote/speaker-diarization-community-1", token=self.hf_token
                        )
                        if torch.cuda.is_available() and self.device != "cpu":
                            pipe.to(torch.device("cuda"))
                        self._diar = pipe
                    except Exception as e:  # noqa: BLE001
                        log.warning("diarization unavailable (%s); continuing without speakers", e)
                        self.diarize = False
        return self._diar

    # --- work --------------------------------------------------------------------------

    async def transcribe(
        self, wav: Path, *, progress: ProgressFn | None = None, diarize: bool | None = None
    ) -> Transcript:
        import asyncio

        return await asyncio.to_thread(self._transcribe_sync, wav, progress, diarize)

    def _transcribe_sync(
        self, wav: Path, progress: ProgressFn | None, diarize: bool | None = None
    ) -> Transcript:
        model = self._whisper()
        seg_iter, info = model.transcribe(
            str(wav),
            language=self.language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        segments: list[Segment] = []
        for s in seg_iter:
            segments.append(Segment(start=float(s.start), end=float(s.end), text=s.text.strip()))
            if progress and duration:
                progress(min(0.9, s.end / duration), f"{int(s.end) // 60}:{int(s.end) % 60:02d}")
        speakers: list[str] = []
        if segments and diarize is not False and self._diarizer() is not None:
            if progress:
                progress(0.9, "diarizing")
            speakers = self._assign_speakers(wav, segments)
        if progress:
            progress(1.0, "done")
        return Transcript(
            segments=segments,
            language=getattr(info, "language", None),
            duration_s=duration,
            speakers=speakers,
        )

    def _assign_speakers(self, wav: Path, segments: list[Segment]) -> list[str]:
        """Label each whisper segment with the pyannote speaker that overlaps it most."""
        try:
            out = self._diar(str(wav))
            annotation = getattr(out, "speaker_diarization", out)
            turns = [(t.start, t.end, spk) for t, _, spk in annotation.itertracks(yield_label=True)]
        except Exception as e:  # noqa: BLE001
            log.warning("diarization failed: %s", e)
            return []
        names: dict[str, str] = {}
        for seg in segments:
            best, best_ov = None, 0.0
            for start, end, spk in turns:
                ov = min(seg.end, end) - max(seg.start, start)
                if ov > best_ov:
                    best, best_ov = spk, ov
            if best is not None:
                names.setdefault(best, f"SPEAKER_{len(names):02d}")
                seg.speaker = names[best]
        return list(names.values())
