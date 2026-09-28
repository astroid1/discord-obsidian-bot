"""Extractors: turn a fetched file (or a chat day) into Extracted text.

Registry is keyed by file extension. Media and document extractors are added in later modules
(`extract_media.py`, `extract_docs.py`) and registered by `__main__`/smoke via `default_registry`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from .models import Extracted, IngestItem

log = logging.getLogger(__name__)

TEXT_EXT = {".md", ".markdown", ".txt", ".csv", ".json", ".log", ".rst", ".html", ".htm"}


class UnsupportedTypeError(ValueError):
    """Deterministic: the pipeline cannot handle this input."""


class Extractor(Protocol):
    async def extract(self, item: IngestItem, progress) -> Extracted: ...


class TextExtractor:
    async def extract(self, item: IngestItem, progress) -> Extracted:
        p = Path(item.local_path)  # type: ignore[arg-type]
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        if text.startswith("﻿"):
            text = text[1:]
        if p.suffix.lower() in (".html", ".htm"):
            text = _strip_html(text)
        return Extracted(
            item=item, kind="document", text=text, meta={"format": p.suffix.lstrip(".")}
        )


def _strip_html(html: str) -> str:
    import re

    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as _h

    return re.sub(r"[ \t]+", " ", _h.unescape(text)).strip()


class ExtractorRegistry:
    def __init__(self) -> None:
        self._by_ext: dict[str, Extractor] = {}
        self._by_source: dict[str, Extractor] = {}

    def register(self, exts: set[str] | list[str], extractor: Extractor) -> None:
        for e in exts:
            self._by_ext[e.lower()] = extractor

    def register_source(self, source: str, extractor: Extractor) -> None:
        self._by_source[source] = extractor

    def supported_extensions(self) -> set[str]:
        return set(self._by_ext)

    def supports(self, name: str) -> bool:
        return Path(name).suffix.lower() in self._by_ext

    def for_item(self, item: IngestItem) -> Extractor:
        if item.source in self._by_source:
            return self._by_source[item.source]
        ext = Path(item.local_path or item.original_name).suffix.lower()
        try:
            return self._by_ext[ext]
        except KeyError:
            raise UnsupportedTypeError(f"unsupported file type {ext or '(none)'}") from None

    async def extract(self, item: IngestItem, progress) -> Extracted:
        return await self.for_item(item).extract(item, progress)


def default_registry(
    *, transcriber=None, structurer=None, cfg=None, state=None, bot_cfg=None
) -> ExtractorRegistry:
    """Build the registry with everything that is available."""
    reg = ExtractorRegistry()
    reg.register(TEXT_EXT, TextExtractor())
    try:
        from .extract_docs import register_docs

        register_docs(reg, structurer=structurer)
    except ImportError:
        pass
    if transcriber is not None:
        from .extract_media import register_media

        register_media(reg, transcriber=transcriber, cfg=cfg)
        from .record import MeetingExtractor

        reg.register_source("meeting", MeetingExtractor(transcriber))
    if state is not None and bot_cfg is not None:
        from .chat import ChatDayExtractor

        reg.register_source("chat_day", ChatDayExtractor(state, bot_cfg))
    return reg
