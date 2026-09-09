"""Downloaders: Discord attachments, yt-dlp media/links, direct HTTP files, Google Docs exports.

Also `classify_url` / `canonical_ref`, used by the bot to decide what to react to before
anything is downloaded.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from .extract import UnsupportedTypeError
from .models import IngestItem

log = logging.getLogger(__name__)

MAX_BYTES = 4 * 1024**3
_YT_ID = re.compile(r"^[\w-]{11}$")
_GDOC = re.compile(r"^/(document|spreadsheets|presentation)/d/([\w-]+)")
_GDRIVE = re.compile(r"^/file/d/([\w-]+)")
_LOOM = re.compile(r"^/(?:share|embed)/([0-9a-f]{32})")
GDOC_EXPORT = {"document": "pdf", "spreadsheets": "csv", "presentation": "pdf"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")


def canonical_ref(url: str) -> str:
    """Stable identity for a URL without touching the network (used for pre-download dedupe)."""
    u = urlparse(url)
    host, path = _host(url), u.path
    if host == "youtu.be" and _YT_ID.match(path.strip("/")):
        return f"youtube:{path.strip('/')}"
    if host in ("youtube.com", "music.youtube.com"):
        vid = parse_qs(u.query).get("v", [None])[0]
        m = re.match(r"^/(?:shorts|live|embed)/([\w-]{11})", path)
        vid = vid or (m.group(1) if m else None)
        if vid:
            return f"youtube:{vid}"
    if host == "loom.com" and (m := _LOOM.match(path)):
        return f"loom:{m.group(1)}"
    if host == "drive.google.com" and (m := _GDRIVE.match(path)):
        return f"gdrive:{m.group(1)}"
    if host == "docs.google.com" and (m := _GDOC.match(path)):
        return f"gdoc:{m.group(1)}:{m.group(2)}"
    return url.split("#")[0]


def classify_url(url: str, url_hosts: list[str], supported_ext: set[str]) -> str | None:
    """'ytdlp' | 'gdoc' | 'http' | None (ignore)."""
    if not url.startswith(("http://", "https://")):
        return None
    host = _host(url)
    if host == "docs.google.com" and _GDOC.match(urlparse(url).path):
        return "gdoc"
    if host in url_hosts or any(host.endswith("." + h) for h in url_hosts):
        return "ytdlp"
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in supported_ext:
        return "http"
    return None


class Downloader:
    def __init__(
        self, cache_dir: Path, url_hosts: list[str] | None = None, max_bytes: int = MAX_BYTES
    ):
        self.cache_dir = Path(cache_dir)
        self.url_hosts = url_hosts or []
        self.max_bytes = max_bytes

    async def fetch(self, item: IngestItem, progress) -> IngestItem:
        if item.local_path and Path(item.local_path).exists():
            return item
        if not item.url:
            raise UnsupportedTypeError("nothing to fetch: no local_path and no url")
        dest = self.cache_dir / item.id
        dest.mkdir(parents=True, exist_ok=True)
        if item.source == "discord_attachment":
            item.local_path = await self._http(item.url, dest / _safe(item.original_name), progress)
            return item
        kind = classify_url(item.url, self.url_hosts, set()) or "http"
        if kind == "gdoc":
            m = _GDOC.match(urlparse(item.url).path)
            assert m
            fmt = GDOC_EXPORT[m.group(1)]
            export = f"https://docs.google.com/{m.group(1)}/d/{m.group(2)}/export?format={fmt}"
            item.local_path = await self._http(export, dest / f"{m.group(2)}.{fmt}", progress)
            item.original_name = item.local_path.name
        elif kind == "ytdlp":
            item.local_path, title, ref = await asyncio.to_thread(self._ytdlp, item.url, dest)
            item.original_name = title
            if ref:
                item.source_ref = ref
        else:
            name = _safe(Path(urlparse(item.url).path).name or "download")
            item.local_path = await self._http(item.url, dest / name, progress)
            item.original_name = item.local_path.name
        return item

    async def _http(self, url: str, dest: Path, progress) -> Path:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=600)) as client,
            client.stream("GET", url) as r,
        ):
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            if total > self.max_bytes:
                raise UnsupportedTypeError(f"file too large ({total / 1e9:.1f} GB)")
            if not dest.suffix:
                ctype = r.headers.get("content-type", "").split(";")[0].strip()
                ext = mimetypes.guess_extension(ctype) or ""
                dest = dest.with_suffix(ext)
            got = 0
            last = 0.0
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if got > self.max_bytes:
                        raise UnsupportedTypeError("file too large")
                    if total and progress and (got / total) - last >= 0.1:
                        last = got / total
                        await progress.update(
                            "downloading", f"{got / 1e6:.0f}/{total / 1e6:.0f} MB", last
                        )
        return dest

    def _ytdlp(self, url: str, dest: Path) -> tuple[Path, str, str | None]:
        import yt_dlp

        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(dest / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": self.max_bytes,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise UnsupportedTypeError("yt-dlp returned nothing")
                if "entries" in info:  # playlist despite noplaylist
                    info = next((e for e in info["entries"] if e), None) or info
                path = Path(ydl.prepare_filename(info))
                if not path.exists():  # postprocessing may change the extension
                    cands = list(dest.glob(f"{info.get('id', '*')}.*"))
                    if not cands:
                        raise UnsupportedTypeError("yt-dlp produced no file")
                    path = cands[0]
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "HTTP Error 5" in msg or "timed out" in msg.lower():
                from .jobs import TransientError

                raise TransientError(msg) from e
            raise UnsupportedTypeError(f"download failed: {msg[-300:]}") from e
        title = f"{info.get('title') or info.get('id') or 'media'}{path.suffix}"
        ref = (
            f"{(info.get('extractor_key') or 'url').lower()}:{info['id']}"
            if info.get("id")
            else None
        )
        return path, _safe(title), ref


def _safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip() or "file"
    return name[:150]
