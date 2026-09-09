"""Chat archiving: render daily logs from stored messages and feed digests into the pipeline.

The Discord history walker (`ChatArchiver`) lives here too; it needs a discord.py client and is
only imported by bot.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING

from .config import BotConfig
from .models import BACKGROUND, ChatAttachment, ChatMessage, Extracted, IngestItem
from .state import State
from .vault import VaultWriter, link, safe_name

if TYPE_CHECKING:
    import discord

log = logging.getLogger(__name__)


# --- rendering -------------------------------------------------------------------------------


def author_label(msg: ChatMessage, cfg: BotConfig) -> str:
    name = cfg.people.get(msg.author_id)
    return link(name) if name else f"@{msg.author_name}"


def render_day_log(
    msgs: list[ChatMessage],
    *,
    channel_name: str,
    day: str,
    cfg: BotConfig,
    note_for_message=None,
    digest_link: str | None = None,
) -> tuple[dict, str]:
    """Return (frontmatter, body) for chat/<channel>/<day>.md."""
    tz = cfg.tz
    by_id = {m.id: m for m in msgs}
    threads: dict[int, list[ChatMessage]] = {}
    top: list[ChatMessage] = []
    for m in msgs:
        if m.thread_id:
            threads.setdefault(m.thread_id, []).append(m)
        else:
            top.append(m)

    def fmt(m: ChatMessage, indent: str = "") -> list[str]:
        t = m.created_at.astimezone(tz).strftime("%H:%M")
        head = f"{indent}- **{t}** {author_label(m, cfg)}"
        if m.reply_to_id:
            target = by_id.get(m.reply_to_id)
            if target:
                head += f" ↩ {target.created_at.astimezone(tz).strftime('%H:%M')}"
            else:
                head += " ↩"
        content = m.content.strip().replace("\n", f"\n{indent}  ")
        lines = [
            f"{head}: {content} [↗]({m.jump_url})" if content else f"{head}: [↗]({m.jump_url})"
        ]
        for a in m.attachments:
            s = f"{indent}  - 📎 [{a.filename}]({a.url})"
            note = note_for_message(m.id) if note_for_message else None
            if note:
                s += f" → {link(VaultWriter.to_link(note))}"
            lines.append(s)
        return lines

    body: list[str] = [f"# #{channel_name} — {day}", ""]
    emitted_threads: set[int] = set()
    for m in top:
        body += fmt(m)
    for tid, tmsgs in threads.items():
        if tid in emitted_threads:
            continue
        emitted_threads.add(tid)
        first = tmsgs[0]
        t = first.created_at.astimezone(tz).strftime("%H:%M")
        body.append(f'- **{t}** 🧵 *thread: "{first.thread_name or tid}"*')
        for tm in tmsgs:
            body += fmt(tm, indent="  ")
    meta = {
        "type": "chat-log",
        "channel": f"#{channel_name}",
        "channel_id": msgs[0].channel_id if msgs else None,
        "date": day,
        "message_count": len(msgs),
    }
    if digest_link:
        meta["digest"] = link(digest_link)
    return meta, "\n".join(body)


def render_day_text(msgs: list[ChatMessage], *, channel_name: str, day: str, cfg: BotConfig) -> str:
    """Plain text the LLM sees for a chat-day digest."""
    tz = cfg.tz
    out = [f"Channel #{channel_name}, {day}", ""]
    cur_thread = None
    for m in msgs:
        if m.thread_id != cur_thread:
            cur_thread = m.thread_id
            if m.thread_id:
                out.append(f'--- thread "{m.thread_name}" ---')
            else:
                out.append("--- main channel ---")
        who = cfg.people.get(m.author_id, m.author_name)
        t = m.created_at.astimezone(tz).strftime("%H:%M")
        att = "".join(f" [attachment: {a.filename}]" for a in m.attachments)
        out.append(f"[{t}] {who}: {m.content.strip()}{att}")
    return "\n".join(out)


class ChatDayExtractor:
    """Extractor for `chat_day` items: renders the day's stored messages to text."""

    def __init__(self, state: State, cfg: BotConfig):
        self.state = state
        self.cfg = cfg

    async def extract(self, item: IngestItem, progress) -> Extracted:
        assert item.channel_id and item.day
        msgs = self.state.messages_for_day(item.channel_id, item.day)
        text = render_day_text(
            msgs, channel_name=item.channel_name or str(item.channel_id), day=item.day, cfg=self.cfg
        )
        threads = []
        for m in msgs:
            if m.thread_name and m.thread_name not in threads:
                threads.append(m.thread_name)
        return Extracted(
            item=item,
            kind="chat-digest",
            text=text,
            meta={"message_count": str(len(msgs)), "threads": "\n".join(threads)},
        )


def digest_item(channel_id: int, channel_name: str, day: str, *, force: bool = False) -> IngestItem:
    return IngestItem(
        source="chat_day",
        original_name=f"#{channel_name} {day}",
        source_ref=f"chat:{channel_id}:{day}",
        channel_id=channel_id,
        channel_name=channel_name,
        day=day,
        priority=BACKGROUND,
        force=force,
    )


def should_digest(
    state: State, cfg: BotConfig, channel_id: int, day: str, count: int, today: str
) -> bool:
    if count < cfg.digest_min_messages:
        return False
    row = state.get_chat_day(channel_id, day)
    prev = (row["digest_message_count"] if row else None) or 0
    if prev == 0:
        return day < today
    return count - prev >= cfg.digest_regen_delta


# --- Discord history walker ------------------------------------------------------------------


def msg_to_chat(
    m: discord.Message, *, parent_id: int, thread: discord.Thread | None
) -> ChatMessage:
    return ChatMessage(
        id=m.id,
        channel_id=parent_id,
        thread_id=thread.id if thread else None,
        thread_name=thread.name if thread else None,
        author_id=m.author.id,
        author_name=getattr(m.author, "display_name", None) or m.author.name,
        created_at=m.created_at,
        content=m.system_content if m.type.name != "default" and not m.content else m.content,
        attachments=[
            ChatAttachment(id=a.id, filename=a.filename, url=a.url, size=a.size)
            for a in m.attachments
        ],
        reply_to_id=m.reference.message_id if m.reference and m.reference.message_id else None,
        jump_url=m.jump_url,
    )


class ChatArchiver:
    def __init__(
        self, *, client: discord.Client, state: State, cfg: BotConfig, vault: VaultWriter, queue
    ):
        self.client = client
        self.state = state
        self.cfg = cfg
        self.vault = vault
        self.queue = queue
        self._lock = asyncio.Lock()

    def _day(self, dt: datetime) -> str:
        return dt.astimezone(self.cfg.tz).date().isoformat()

    async def walk(
        self,
        *,
        channel_ids: list[int] | None = None,
        since: date | None = None,
        full: bool = False,
        report=None,
    ) -> dict[int, int]:
        """Walk configured channels (+ threads). Returns {channel_id: new_message_count}."""
        import discord

        async with self._lock:
            ids = channel_ids or self.cfg.archived_channels
            if full:
                self.state.reset_cursors(ids)
            totals: dict[int, int] = {}
            touched: dict[tuple[int, str], str] = {}
            for cid in ids:
                ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
                if not isinstance(ch, discord.TextChannel | discord.ForumChannel):
                    log.warning("channel %s is not a text/forum channel; skipping", cid)
                    continue
                n = 0
                if isinstance(ch, discord.TextChannel):
                    n += await self._walk_one(
                        ch, parent=ch, thread=None, since=since, touched=touched, report=report
                    )
                for th in await self._threads(ch):
                    n += await self._walk_one(
                        th, parent=ch, thread=th, since=since, touched=touched, report=report
                    )
                totals[cid] = n
            await self._render_days(touched)
            return totals

    async def _threads(self, ch) -> list[discord.Thread]:
        threads = list(ch.threads)
        try:
            async for th in ch.archived_threads(limit=None):
                threads.append(th)
        except Exception as e:  # noqa: BLE001 - missing perms on some servers
            log.warning("could not list archived threads of #%s: %s", ch.name, e)
        return threads

    async def _walk_one(self, source, *, parent, thread, since, touched, report) -> int:
        import discord

        cursor = self.state.get_cursor(source.id)
        after = (
            discord.Object(id=cursor)
            if cursor
            else (since and datetime.combine(since, datetime.min.time(), tzinfo=self.cfg.tz))
        )
        page: list[ChatMessage] = []
        n = 0
        last_id = cursor

        async def flush() -> None:
            nonlocal page, last_id
            if not page:
                return
            days = [self._day(m.created_at) for m in page]
            self.state.upsert_messages(page, days)
            for d in days:
                touched[(parent.id, d)] = parent.name
            last_id = page[-1].id
            self.state.set_cursor(
                source.id, last_id, parent_id=parent.id if thread else None, name=source.name
            )
            page = []

        try:
            async for m in source.history(limit=None, after=after, oldest_first=True):
                page.append(msg_to_chat(m, parent_id=parent.id, thread=thread))
                n += 1
                if len(page) >= 100:
                    await flush()
                    if report and n % 500 == 0:
                        await report(
                            f"#{parent.name}{' / ' + thread.name if thread else ''}: {n:,} messages"
                        )
                    await asyncio.sleep(1.0)
        except discord.Forbidden:
            log.warning("no permission to read history of %s; skipping", source)
        finally:
            await flush()
        if n:
            log.info("archived %d new message(s) from %s", n, source)
        return n

    async def _render_days(self, touched: dict[tuple[int, str], str]) -> None:
        today = datetime.now(self.cfg.tz).date().isoformat()
        for (cid, day), cname in sorted(touched.items()):
            msgs = self.state.messages_for_day(cid, day)
            digest_link = self.vault.chat_digest_link(cname, day)
            row = self.state.get_chat_day(cid, day)
            meta, body = render_day_log(
                msgs,
                channel_name=cname,
                day=day,
                cfg=self.cfg,
                note_for_message=self.state.note_for_message,
                digest_link=digest_link if row and row["digest_path"] else None,
            )
            rel = self.vault.write_chat_log(cname, day, meta, body)
            self.state.upsert_chat_day(cid, day, len(msgs), rel)
            if should_digest(self.state, self.cfg, cid, day, len(msgs), today):
                await self.queue.submit(
                    digest_item(cid, cname, day, force=bool(row and row["digest_path"]))
                )
        if touched:
            self.vault.commit(f"chat: {len(touched)} day log(s)")

    def note_digest_written(
        self, channel_id: int, channel_name: str, day: str, note_path: str
    ) -> None:
        row = self.state.get_chat_day(channel_id, day)
        self.state.set_digest(channel_id, day, note_path, row["message_count"] if row else 0)
        msgs = self.state.messages_for_day(channel_id, day)
        meta, body = render_day_log(
            msgs,
            channel_name=channel_name,
            day=day,
            cfg=self.cfg,
            note_for_message=self.state.note_for_message,
            digest_link=VaultWriter.to_link(note_path),
        )
        self.vault.write_chat_log(channel_name, day, meta, body)


class DigestSink:
    """Job sink for chat-day digests: records the digest and links it from the day log."""

    def __init__(self, archiver: ChatArchiver, channel_id: int, channel_name: str, day: str):
        self.archiver = archiver
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.day = day
        self.name = f"#{channel_name} {day}"

    async def queued(self, position: int) -> None:
        log.info("[%s] digest queued #%d", self.name, position)

    async def update(self, stage: str, detail: str = "", fraction: float | None = None) -> None:
        log.debug("[%s] %s %s", self.name, stage, detail)

    async def done(self, result) -> None:
        if result.note:
            self.archiver.note_digest_written(
                self.channel_id, self.channel_name, self.day, result.note.note_path
            )
            log.info("[%s] digest -> %s", self.name, result.note.note_path)
        else:
            log.info("[%s] digest %s", self.name, result.message)

    async def failed(self, error: str, will_retry: bool) -> None:
        log.error(
            "[%s] digest failed (%s): %s", self.name, "retrying" if will_retry else "gave up", error
        )


__all__ = [
    "ChatArchiver",
    "ChatDayExtractor",
    "DigestSink",
    "digest_item",
    "render_day_log",
    "render_day_text",
    "safe_name",
    "should_digest",
]
