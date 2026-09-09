"""The pipeline: fetch -> dedupe -> extract -> structure -> write. Owns ledger transitions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Protocol

from .extract import ExtractorRegistry, UnsupportedTypeError
from .models import IngestItem, RunResult
from .state import State
from .structure import StructureError, Structurer
from .vault import VaultWriter

log = logging.getLogger(__name__)


class Progress(Protocol):
    async def update(self, stage: str, detail: str = "", fraction: float | None = None) -> None: ...


class NullProgress:
    async def update(self, stage: str, detail: str = "", fraction: float | None = None) -> None:
        log.info("%s %s", stage, detail)


class Fetcher(Protocol):
    async def fetch(self, item: IngestItem, progress: Progress) -> IngestItem: ...


class NoFetcher:
    """For local runs: the item must already have a local_path."""

    async def fetch(self, item: IngestItem, progress: Progress) -> IngestItem:
        if item.local_path is None:
            raise UnsupportedTypeError("no fetcher configured and item has no local_path")
        return item


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Pipeline:
    def __init__(
        self,
        *,
        state: State,
        fetcher: Fetcher,
        extractors: ExtractorRegistry,
        structurer: Structurer,
        vault: VaultWriter,
        cache_dir: Path,
    ):
        self.state = state
        self.fetcher = fetcher
        self.extractors = extractors
        self.structurer = structurer
        self.vault = vault
        self.cache_dir = cache_dir

    async def run(self, item: IngestItem, progress: Progress | None = None) -> RunResult:
        progress = progress or NullProgress()
        if self.state.get(item.id) is None:
            self.state.submit(item)
        try:
            result = await self._run(item, progress)
        except Exception as e:
            self.state.set_status(
                item.id, "failed", error=f"{type(e).__name__}: {e}", bump_attempts=True
            )
            raise
        finally:
            self._cleanup(item)
        return result

    async def _run(self, item: IngestItem, progress: Progress) -> RunResult:
        st = self.state
        # 1. fetch
        if item.source != "chat_day":
            st.set_status(item.id, "fetching", bump_attempts=True)
            if item.url and not item.force and (row := st.find_done_by_ref(item.source_ref)):
                return self._skipped(item, row["note_path"])
            await progress.update("fetching", item.original_name)
            item = await self.fetcher.fetch(item, progress)
            if item.local_path is None or not Path(item.local_path).exists():
                raise UnsupportedTypeError("fetch produced no file")
            if not item.sha256:
                item.sha256 = await asyncio.to_thread(sha256_of, Path(item.local_path))
            st.set_status(item.id, "fetching", sha256=item.sha256, item=item)
            # 2. dedupe
            if not item.force and (row := st.find_done_by_sha(item.sha256)):
                return self._skipped(item, row["note_path"])
        else:
            st.set_status(item.id, "extracting", bump_attempts=True)

        # 3. extract
        st.set_status(item.id, "extracting")
        await progress.update("extracting", item.original_name)
        extracted = await self.extractors.extract(item, progress)
        if item.source == "chat_day":
            # the day's rendered text is the identity of a digest
            item.sha256 = hashlib.sha256(extracted.text.encode("utf-8")).hexdigest()
            extracted.item.sha256 = item.sha256
            st.set_status(item.id, "extracting", sha256=item.sha256, item=item)
            if not item.force and (row := st.find_done_by_sha(item.sha256)):
                return self._skipped(item, row["note_path"])
        if not extracted.text.strip():
            raise UnsupportedTypeError("nothing could be extracted (empty text)")

        # 4. structure
        st.set_status(item.id, "structuring")
        await progress.update("structuring", f"{len(extracted.text):,} chars")
        structured = await self.structurer.structure(extracted, self.vault.entity_context())
        st.set_status(
            item.id,
            "structuring",
            input_tokens=structured.input_tokens,
            output_tokens=structured.output_tokens,
        )

        # 5. write
        st.set_status(item.id, "writing")
        await progress.update("writing", structured.output.suggested_title)
        note = await asyncio.to_thread(self.vault.write, extracted, structured)
        st.set_status(item.id, "done", note_path=note.note_path)
        log.info("done %s -> %s", item.original_name, note.note_path)
        return RunResult(
            status="done", note=note, summary=structured.output.summary, message="ingested"
        )

    def _skipped(self, item: IngestItem, note_path: str | None) -> RunResult:
        self.state.set_status(item.id, "skipped", note_path=note_path)
        log.info("skipped duplicate %s -> %s", item.original_name, note_path)
        link = VaultWriter.to_link(note_path) if note_path else ""
        return RunResult(
            status="skipped",
            message=f"already ingested → [[{link}]]" if link else "already ingested",
        )

    def _cleanup(self, item: IngestItem) -> None:
        d = self.cache_dir / item.id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


__all__ = [
    "Fetcher",
    "NoFetcher",
    "NullProgress",
    "Pipeline",
    "Progress",
    "StructureError",
    "UnsupportedTypeError",
    "sha256_of",
]
