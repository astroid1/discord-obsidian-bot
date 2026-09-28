"""Discord surface: intents, watched channels, reactions and replies, slash commands, schedulers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands

from .ask import collect_week, next_run, render_week, split_message
from .chat import ChatArchiver
from .config import BotConfig, Settings
from .fetch import canonical_ref, classify_url
from .jobs import JobQueue, LogSink
from .models import INTERACTIVE, DiscordRef, IngestItem, RunResult
from .pipeline import Pipeline
from .tasks import (
    DUE_HELP,
    Task,
    parse_due,
    render_board,
    render_card,
    render_reminder,
    render_vault_page,
    suggestions_from,
    week_tasks_line,
)

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()]+")
MSG_LINK_RE = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
EDIT_INTERVAL = 5.0
REPLY_MAX = 1900


def _trim(s: str, n: int = REPLY_MAX) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class DiscordSink:
    """Progress sink that reacts on the source message and edits one reply."""

    def __init__(self, message: discord.Message, label: str):
        self.message = message
        self.label = label
        self.reply: discord.Message | None = None
        self._last = 0.0

    async def _edit(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < EDIT_INTERVAL:
            return
        self._last = now
        with contextlib.suppress(discord.HTTPException):
            if self.reply is None:
                self.reply = await self.message.reply(_trim(text), mention_author=False)
            else:
                await self.reply.edit(content=_trim(text))

    async def _react(self, emoji: str, remove: str | None = None) -> None:
        with contextlib.suppress(discord.HTTPException):
            if remove and self.message.guild and self.message.guild.me:
                await self.message.remove_reaction(remove, self.message.guild.me)
            await self.message.add_reaction(emoji)

    async def queued(self, position: int) -> None:
        await self._react("⏳")
        await self._edit(f"⏳ Queued (#{position}): **{self.label}**", force=True)

    async def update(self, stage: str, detail: str = "", fraction: float | None = None) -> None:
        pct = f" {fraction * 100:.0f}%" if fraction is not None else ""
        await self._edit(f"⏳ {stage}{pct} · {detail} — **{self.label}**")

    async def done(self, result: RunResult) -> None:
        if result.status == "skipped":
            await self._react("🔁", remove="⏳")
            await self._edit(f"🔁 **{self.label}** — {result.message}", force=True)
            return
        n = result.note
        assert n is not None
        await self._react("✅", remove="⏳")
        counts = []
        if n.entities_created or n.entities_updated:
            counts.append(f"{len(n.entities_created) + len(n.entities_updated)} entities")
        if n.decisions_created:
            counts.append(f"{len(n.decisions_created)} decisions")
        tail = f" · {' · '.join(counts)}" if counts else ""
        await self._edit(f"✅ **{n.title}**\n{result.summary}\n`{n.note_path}`{tail}", force=True)

    async def failed(self, error: str, will_retry: bool) -> None:
        if will_retry:
            await self._edit(f"⚠️ **{self.label}** — {error}\nRetrying…", force=True)
            return
        await self._react("❌", remove="⏳")
        await self._edit(
            f"❌ **{self.label}** — {error}\nFix the input and use `/retry` with the message link.",
            force=True,
        )


class InboxSink(LogSink):
    """Moves inbox files to done/ or failed/ after processing."""

    def __init__(self, path: Path, inbox: Path):
        super().__init__(path.name)
        self.path = path
        self.inbox = inbox

    def _move(self, sub: str) -> None:
        dest = self.inbox / sub
        dest.mkdir(exist_ok=True)
        with contextlib.suppress(OSError):
            shutil.move(str(self.path), str(dest / self.path.name))

    async def done(self, result: RunResult) -> None:
        await super().done(result)
        self._move("done")

    async def failed(self, error: str, will_retry: bool) -> None:
        await super().failed(error, will_retry)
        if not will_retry:
            self._move("failed")


class Bot(discord.Client):
    def __init__(self, settings: Settings, cfg: BotConfig, pipeline: Pipeline):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.guild_reactions = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.settings = settings
        self.cfg = cfg
        self.pipeline = pipeline
        self.queue = JobQueue(pipeline)
        self.archiver = ChatArchiver(
            client=self, state=pipeline.state, cfg=cfg, vault=pipeline.vault, queue=self.queue
        )
        self.tree = app_commands.CommandTree(self)
        self._started = False
        self._tasks: list[asyncio.Task] = []
        self._inbox_seen: dict[Path, int] = {}
        self._rec: SimpleNamespace | None = None
        self.queue.on_result = self._on_result
        self.answerer = None
        if settings.anthropic_api_key:
            from .ask import Answerer

            self.answerer = Answerer(
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
                vault_root=pipeline.vault.root,
                tz=cfg.tz,
                max_notes=cfg.ask.max_notes,
                budget_chars=cfg.ask.budget_chars,
                note_chars=cfg.ask.note_chars,
            )
        self._register_commands()

    # --- lifecycle -----------------------------------------------------------------------

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        log.info("logged in as %s (guild %s)", self.user, self.cfg.guild_id)
        if self._started:
            return
        self._started = True
        self.queue.start()
        await self._requeue()
        self._tasks.append(asyncio.create_task(self._inbox_loop(), name="inbox"))
        self._tasks.append(asyncio.create_task(self._sync_loop(), name="sync"))
        self._tasks.append(asyncio.create_task(self._weekly_loop(), name="weekly"))
        self._tasks.append(asyncio.create_task(self._task_reminder_loop(), name="tasks"))

    async def close(self) -> None:
        if self._rec is not None:
            with contextlib.suppress(Exception):
                await self._stop_recording("bot shutting down")
        for t in self._tasks:
            t.cancel()
        await self.queue.stop()
        await super().close()

    async def _requeue(self) -> None:
        for item in self.pipeline.state.inflight():
            sink = None
            if item.discord:
                with contextlib.suppress(discord.HTTPException, AttributeError):
                    ch = self.get_channel(item.discord.channel_id) or await self.fetch_channel(
                        item.discord.channel_id
                    )
                    msg = await ch.fetch_message(item.discord.message_id)  # type: ignore[union-attr]
                    sink = DiscordSink(msg, item.original_name)
            await self.queue.submit(item, sink, persist=False)

    # --- message handling --------------------------------------------------------------------

    def _watched(self, channel) -> bool:
        if channel.id in self.cfg.watched_channels:
            return True
        parent = getattr(channel, "parent", None)
        return parent is not None and parent.id in self.cfg.watched_channels

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or message.guild.id != self.cfg.guild_id:
            return
        if not self._watched(message.channel):
            return
        await self._ingest_message(message, force=False)

    async def _ingest_message(self, message: discord.Message, *, force: bool) -> int:
        ref = DiscordRef(
            guild_id=message.guild.id,  # type: ignore[union-attr]
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", str(message.channel.id)),
            message_id=message.id,
            author_id=message.author.id,
            author_name=self.cfg.people.get(message.author.id) or message.author.display_name,
            jump_url=message.jump_url,
            created_at=message.created_at,
        )
        supported = self.pipeline.extractors.supported_extensions()
        n = 0
        unsupported: list[str] = []
        for a in message.attachments:
            if not self.pipeline.extractors.supports(a.filename):
                unsupported.append(a.filename)
                continue
            item = IngestItem(
                source="discord_attachment",
                original_name=a.filename,
                source_ref=f"discord:{message.id}:{a.id}",
                url=a.url,
                mime=a.content_type,
                discord=ref,
                priority=INTERACTIVE,
                force=force,
            )
            await self.queue.submit(item, DiscordSink(message, a.filename))
            n += 1
        for url in URL_RE.findall(message.content or ""):
            url = url.rstrip(".,;:!?)")
            if classify_url(url, self.cfg.url_hosts, supported) is None:
                continue
            item = IngestItem(
                source="discord_url",
                original_name=url,
                source_ref=canonical_ref(url),
                url=url,
                discord=ref,
                priority=INTERACTIVE,
                force=force,
            )
            await self.queue.submit(item, DiscordSink(message, url))
            n += 1
        if unsupported and not n:
            with contextlib.suppress(discord.HTTPException):
                await message.reply(
                    f"Skipping {', '.join(unsupported[:5])} — unsupported type. I can read: "
                    + " ".join(sorted(supported)),
                    mention_author=False,
                )
        return n

    # --- background loops --------------------------------------------------------------------

    async def _inbox_loop(self) -> None:
        inbox = self.settings.inbox_path
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "done").mkdir(exist_ok=True)
        (inbox / "failed").mkdir(exist_ok=True)
        log.info("watching inbox %s every %ss", inbox, self.settings.inbox_poll_seconds)
        pending: dict[Path, int] = {}
        while True:
            try:
                for p in sorted(inbox.iterdir()):
                    if not p.is_file() or p.name.startswith(".") or p in self._inbox_seen:
                        continue
                    size = p.stat().st_size
                    if pending.get(p) == size and size > 0:
                        del pending[p]
                        self._inbox_seen[p] = size
                        if not self.pipeline.extractors.supports(p.name):
                            log.warning("inbox: unsupported file %s", p.name)
                            shutil.move(str(p), str(inbox / "failed" / p.name))
                            continue
                        item = IngestItem(
                            source="inbox",
                            original_name=p.name,
                            source_ref=f"inbox:{p.name}",
                            local_path=p,
                            priority=INTERACTIVE,
                        )
                        await self.queue.submit(item, InboxSink(p, inbox))
                    else:
                        pending[p] = size
                for p in list(self._inbox_seen):
                    if not p.exists():
                        del self._inbox_seen[p]
            except Exception:  # noqa: BLE001
                log.exception("inbox loop error")
            await asyncio.sleep(self.settings.inbox_poll_seconds)

    async def _sync_loop(self) -> None:
        if not self.cfg.archived_channels:
            return
        interval = max(0.25, self.cfg.sync_interval_hours) * 3600
        while True:
            try:
                totals = await self.archiver.walk()
                log.info(
                    "chat sync: %s", {k: v for k, v in totals.items() if v} or "no new messages"
                )
            except Exception:  # noqa: BLE001
                log.exception("chat sync failed")
            await asyncio.sleep(interval)

    async def _weekly_loop(self) -> None:
        wd = self.cfg.weekly_digest
        if not wd.channel_id:
            return
        while True:
            now = datetime.now(self.cfg.tz)
            nxt = next_run(now, wd.weekday, wd.hour)
            log.info("weekly digest scheduled for %s", nxt.isoformat(timespec="minutes"))
            await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
            key = nxt.date().isoformat()
            if self.pipeline.state.get_kv("weekly_digest_last") == key:
                continue
            try:
                text = await self.build_week(wd.days)
                await self._send_long(await self._channel(wd.channel_id), text)
                self.pipeline.state.set_kv("weekly_digest_last", key)
            except Exception:  # noqa: BLE001
                log.exception("weekly digest failed")
                await asyncio.sleep(3600)

    # --- helpers shared by commands and loops ------------------------------------------------

    async def _channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _send_long(self, channel, text: str) -> None:
        for chunk in split_message(text):
            await channel.send(chunk)

    def _person(self, user) -> str:
        return self.cfg.people.get(user.id) or user.display_name

    async def build_week(self, days: int) -> str:
        """Week-in-review text: Claude-written when configured, plain rendering otherwise."""
        today = datetime.now(self.cfg.tz).date()
        week = await asyncio.to_thread(
            collect_week, self.pipeline.vault.root, since=today - timedelta(days=days), until=today
        )
        if self.answerer is not None:
            text = await self.answerer.weekly(week)
        else:
            text = render_week(week)
        tasks_line = week_tasks_line(self._all_tasks(), today)
        return f"{text}\n\n{tasks_line}" if tasks_line else text

    # --- tasks -------------------------------------------------------------------------------

    def _today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    def _all_tasks(self) -> list[Task]:
        return [Task.from_row(r) for r in self.pipeline.state.list_tasks()]

    def _task(self, task_id: int) -> Task | None:
        row = self.pipeline.state.get_task(task_id)
        return Task.from_row(row) if row else None

    async def _task_channel(self, fallback: int | None = None):
        cid = self.cfg.tasks.channel_id or fallback
        return await self._channel(cid) if cid else None

    async def _post_card(self, task: Task, fallback_channel: int | None) -> Task:
        ch = await self._task_channel(fallback_channel)
        if ch is None:
            return task
        msg = await ch.send(render_card(task, self._today()))
        self.pipeline.state.update_task(task.id, message_id=msg.id, source_url=msg.jump_url)
        if task.status == "suggested":
            for emoji in ("✅", "❌"):
                with contextlib.suppress(discord.HTTPException):
                    await msg.add_reaction(emoji)
        return self._task(task.id) or task

    async def _refresh_card(self, task: Task) -> None:
        if not task.message_id:
            return
        ch = await self._task_channel()
        if ch is None:
            return
        with contextlib.suppress(discord.HTTPException):
            msg = await ch.fetch_message(task.message_id)
            await msg.edit(content=render_card(task, self._today()))

    async def _refresh_board(self) -> None:
        ch = await self._task_channel()
        if ch is None:
            return
        text = _trim(render_board(self._all_tasks(), self._today()))
        st = self.pipeline.state
        mid = st.get_kv("tasks_board_message_id")
        if mid:
            with contextlib.suppress(discord.HTTPException, ValueError):
                msg = await ch.fetch_message(int(mid))
                await msg.edit(content=text)
                return
        msg = await ch.send(text)
        st.set_kv("tasks_board_message_id", str(msg.id))
        with contextlib.suppress(discord.HTTPException):
            await msg.pin(reason="task board")

    async def _after_task_change(self, task: Task) -> None:
        await self._refresh_card(task)
        await self._refresh_board()
        page = render_vault_page(self._all_tasks(), self._today())
        try:
            await asyncio.to_thread(self.pipeline.vault.write_tasks_page, page)
        except Exception:  # noqa: BLE001 - the vault mirror is best-effort
            log.exception("could not write tasks page")

    async def _close_task(self, task: Task, status: str, by: str) -> Task:
        self.pipeline.state.update_task(
            task.id, status=status, done_at=datetime.now(self.cfg.tz).isoformat(timespec="seconds")
        )
        log.info("task #%d %s by %s", task.id, status, by)
        updated = self._task(task.id) or task
        await self._after_task_change(updated)
        return updated

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """✅ on an open card closes it; ✅/❌ on a suggested card accepts/dismisses it."""
        emoji = str(payload.emoji)
        if (
            emoji not in ("✅", "❌")
            or payload.guild_id != self.cfg.guild_id
            or payload.channel_id != self.cfg.tasks.channel_id
            or payload.member is None
            or payload.member.bot
        ):
            return
        row = self.pipeline.state.task_by_message(payload.message_id)
        if not row:
            return
        task = Task.from_row(row)
        if task.status not in ("open", "suggested"):
            return
        m = payload.member
        allowed = (
            m.id in self.cfg.allowed_user_ids
            or m.guild_permissions.administrator
            or any(r.id in self.cfg.allowed_role_ids for r in m.roles)
            or m.id == task.assignee_id
        )
        if not allowed:
            return
        if task.status == "suggested":
            if emoji == "✅":
                self.pipeline.state.update_task(task.id, status="open")
                log.info("suggested task #%d accepted by %s", task.id, self._person(m))
                await self._after_task_change(self._task(task.id) or task)
            else:
                await self._close_task(task, "cancelled", self._person(m))
            return
        if emoji == "✅":
            await self._close_task(task, "done", self._person(m))

    # --- suggested tasks from new notes ---------------------------------------------------

    async def _on_result(self, item: IngestItem, result: RunResult) -> None:
        if item.source == "meeting" and item.local_path:
            await asyncio.to_thread(shutil.rmtree, Path(item.local_path).parent, True)
        note = result.note
        tc = self.cfg.tasks
        if note is None or not tc.channel_id or not tc.suggest_from_notes:
            return
        if note.kind not in tc.suggest_kinds or not note.action_items:
            return
        st = self.pipeline.state
        fresh = [
            s
            for s in suggestions_from(
                note.action_items, self.cfg.people, self._today(), limit=tc.suggest_max_per_note
            )
            if not st.task_exists(note.note_path, s.title)
        ]
        if not fresh:
            return
        ch = await self._task_channel()
        if ch is None:
            return
        src = f"[{note.title}]({item.discord.jump_url})" if item.discord else f"**{note.title}**"
        n = len(fresh)
        await ch.send(
            f"💡 {n} suggested task{'s' if n != 1 else ''} from {src}. "
            "React ✅ to accept or ❌ to dismiss."
        )
        for s in fresh:
            tid = st.add_task(
                title=s.title,
                notes=f"From: {note.title}",
                assignee_id=s.owner_id,
                assignee_name=s.owner_name,
                due=s.due,
                created_by="Knowledge-base bot",
                status="suggested",
                source_note=note.note_path,
            )
            task = self._task(tid)
            if task is not None:
                await self._post_card(task, None)
        log.info("suggested %d task(s) from %s", n, note.note_path)

    # --- meeting capture ------------------------------------------------------------------

    async def _start_recording(self, channel, user, text_channel_id: int) -> None:
        from discord.ext import voice_recv

        from .record import TrackWriter, install_dave_decrypt, make_sink, new_recording_dir

        root = new_recording_dir(self.settings.data_path / "recordings")
        writer = TrackWriter(root, names=dict(self.cfg.people))
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, self_mute=True)
        try:
            vc.listen(make_sink(writer))
            install_dave_decrypt(vc)
        except Exception:
            await vc.disconnect(force=True)
            shutil.rmtree(root, ignore_errors=True)
            raise
        self._rec = SimpleNamespace(
            vc=vc,
            writer=writer,
            channel=channel,
            requester_id=user.id,
            requested_by=self._person(user),
            text_channel_id=text_channel_id,
            timer=None,
        )
        self._rec.timer = asyncio.create_task(self._recording_timeout(), name="record-timeout")
        log.info("recording #%s into %s", channel.name, root)
        if self.cfg.meetings.announce:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"🔴 **This voice channel is being recorded** by {self._person(user)} for "
                    "meeting notes. Leave the channel if you do not consent. `/record stop` ends it."
                )

    async def _recording_timeout(self) -> None:
        await asyncio.sleep(self.cfg.meetings.max_minutes * 60)
        await self._stop_recording(f"{self.cfg.meetings.max_minutes}-minute limit")

    async def _stop_recording(self, reason: str, status_channel_id: int | None = None) -> None:
        rec, self._rec = self._rec, None
        if rec is None:
            return
        if rec.timer is not None and rec.timer is not asyncio.current_task():
            rec.timer.cancel()
        with contextlib.suppress(Exception):
            rec.vc.stop_listening()
        with contextlib.suppress(Exception):
            await rec.vc.disconnect(force=True)
        manifest = await asyncio.to_thread(
            rec.writer.finish, channel=rec.channel.name, requested_by=rec.requested_by
        )
        minutes = rec.writer.elapsed() / 60
        ch = await self._channel(status_channel_id or rec.text_channel_id)
        if not rec.writer.tracks:
            await ch.send(f"⏹ Recording of <#{rec.channel.id}> stopped ({reason}). Nobody spoke.")
            await asyncio.to_thread(shutil.rmtree, manifest.parent, True)
            return
        names = ", ".join(t.name for t in rec.writer.tracks.values())
        msg = await ch.send(
            f"⏹ Recording of <#{rec.channel.id}> stopped ({reason}) · {minutes:.0f} min · {names}. "
            "Transcribing…"
        )
        ref = DiscordRef(
            guild_id=self.cfg.guild_id,
            channel_id=msg.channel.id,
            channel_name=getattr(msg.channel, "name", str(msg.channel.id)),
            message_id=msg.id,
            author_id=rec.requester_id,
            author_name=rec.requested_by,
            jump_url=msg.jump_url,
            created_at=msg.created_at,
        )
        item = IngestItem(
            source="meeting",
            original_name=f"Meeting in #{rec.channel.name}",
            source_ref=f"meeting:{manifest.parent.name}",
            local_path=manifest,
            discord=ref,
            priority=INTERACTIVE,
        )
        await self.queue.submit(item, DiscordSink(msg, f"meeting in #{rec.channel.name}"))

    async def on_voice_state_update(self, member, before, after) -> None:
        rec = self._rec
        if rec is None:
            return
        if self.user and member.id == self.user.id and after.channel is None:
            await self._stop_recording("bot was disconnected")
            return
        left_ours = before.channel is not None and before.channel.id == rec.channel.id
        if left_ours and not [m for m in rec.channel.members if not m.bot]:
            await self._stop_recording("everyone left")

    async def _task_reminder_loop(self) -> None:
        tc = self.cfg.tasks
        if not tc.channel_id:
            return
        while True:
            now = datetime.now(self.cfg.tz)
            nxt = now.replace(hour=tc.reminder_hour, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
            key = nxt.date().isoformat()
            if tc.reminder_weekdays_only and nxt.weekday() >= 5:
                continue
            if self.pipeline.state.get_kv("tasks_reminder_last") == key:
                continue
            try:
                text = render_reminder(self._all_tasks(), nxt.date())
                if text:
                    await self._send_long(await self._channel(tc.channel_id), text)
                    await self._refresh_board()
                self.pipeline.state.set_kv("tasks_reminder_last", key)
            except Exception:  # noqa: BLE001
                log.exception("task reminder failed")
                await asyncio.sleep(600)

    # --- slash commands ----------------------------------------------------------------------

    def _allowed(self, inter: discord.Interaction) -> bool:
        u = inter.user
        if u.id in self.cfg.allowed_user_ids:
            return True
        if isinstance(u, discord.Member):
            if u.guild_permissions.administrator:
                return True
            if any(r.id in self.cfg.allowed_role_ids for r in u.roles):
                return True
        return False

    async def _gate(self, inter: discord.Interaction) -> bool:
        if inter.guild_id != self.cfg.guild_id or not self._allowed(inter):
            await inter.response.send_message("You are not allowed to run this.", ephemeral=True)
            return False
        return True

    async def _message_from_link(self, link: str) -> discord.Message | None:
        m = MSG_LINK_RE.search(link)
        if not m:
            return None
        _, cid, mid = (int(x) for x in m.groups())
        ch = self.get_channel(cid) or await self.fetch_channel(cid)
        return await ch.fetch_message(mid)  # type: ignore[union-attr]

    def _register_commands(self) -> None:
        tree = self.tree

        @tree.command(name="status", description="Queue, ledger and archive status")
        async def status(inter: discord.Interaction) -> None:
            if not await self._gate(inter):
                return
            st = self.pipeline.state
            counts = st.counts()
            cur = self.queue.current.original_name if self.queue.current else "idle"
            lines = [
                f"**Queue:** {self.queue.size()} · current: {cur}",
                f"**Ledger:** {counts or 'empty'}",
            ]
            cursors = st.cursors()
            if cursors:
                lines.append(
                    f"**Archive:** {len(cursors)} channel/thread cursors, last sync {max(c['last_synced_at'] or '' for c in cursors)}"
                )
            for r in st.recent(5):
                lines.append(
                    f"· {r['status']} — {r['original_name']} {('→ ' + r['note_path']) if r['note_path'] else (r['error'] or '')}"
                )
            await inter.response.send_message(_trim("\n".join(lines)), ephemeral=True)

        @tree.command(
            name="backfill",
            description="Archive full history of the configured channels (or one channel)",
        )
        @app_commands.describe(channel="Only this channel", since="Start date YYYY-MM-DD")
        async def backfill(
            inter: discord.Interaction,
            channel: discord.TextChannel | None = None,
            since: str | None = None,
        ) -> None:
            if not await self._gate(inter):
                return
            since_d = None
            if since:
                try:
                    since_d = date.fromisoformat(since)
                except ValueError:
                    await inter.response.send_message("since must be YYYY-MM-DD", ephemeral=True)
                    return
            await inter.response.send_message(
                "⏳ Backfill started; I'll report here when done.", ephemeral=True
            )

            async def report(text: str) -> None:
                with contextlib.suppress(discord.HTTPException):
                    await inter.followup.send(text, ephemeral=True)

            ids = [channel.id] if channel else None
            try:
                totals = await self.archiver.walk(
                    channel_ids=ids, since=since_d, full=True, report=report
                )
            except Exception as e:  # noqa: BLE001
                log.exception("backfill failed")
                await report(f"❌ backfill failed: {e}")
                return
            await report(
                "✅ Backfill done: " + ", ".join(f"<#{k}>: {v:,}" for k, v in totals.items())
            )

        @tree.command(name="sync", description="Archive new messages since the last sync")
        async def sync(inter: discord.Interaction) -> None:
            if not await self._gate(inter):
                return
            await inter.response.defer(ephemeral=True)
            totals = await self.archiver.walk()
            await inter.followup.send(
                "✅ "
                + (
                    ", ".join(f"<#{k}>: {v:,}" for k, v in totals.items() if v) or "no new messages"
                ),
                ephemeral=True,
            )

        @tree.command(
            name="ingest", description="Ingest a URL (YouTube, Loom, Drive, direct file link)"
        )
        async def ingest(inter: discord.Interaction, url: str) -> None:
            if not await self._gate(inter):
                return
            if (
                classify_url(
                    url, self.cfg.url_hosts, self.pipeline.extractors.supported_extensions()
                )
                is None
            ):
                await inter.response.send_message(
                    "That URL is not on the allowed host list and has no supported file extension.",
                    ephemeral=True,
                )
                return
            await inter.response.send_message(f"⏳ Queued {url}", ephemeral=False)
            msg = await inter.original_response()
            ref = DiscordRef(
                guild_id=inter.guild_id or 0, channel_id=inter.channel_id or 0, channel_name=getattr(inter.channel, "name", ""),
                message_id=msg.id, author_id=inter.user.id, author_name=self.cfg.people.get(inter.user.id) or inter.user.display_name,
                jump_url=msg.jump_url, created_at=msg.created_at,
            )  # fmt: skip
            item = IngestItem(
                source="discord_url",
                original_name=url,
                source_ref=canonical_ref(url),
                url=url,
                discord=ref,
                priority=INTERACTIVE,
            )
            await self.queue.submit(item, DiscordSink(msg, url))

        async def _re(inter: discord.Interaction, link: str, force: bool) -> None:
            if not await self._gate(inter):
                return
            try:
                msg = await self._message_from_link(link)
            except discord.HTTPException:
                msg = None
            if msg is None:
                await inter.response.send_message(
                    "Could not find that message (paste a message link).", ephemeral=True
                )
                return
            n = await self._ingest_message(msg, force=force)
            await inter.response.send_message(
                f"Queued {n} item(s) from that message."
                if n
                else "Nothing ingestible on that message.",
                ephemeral=True,
            )

        @tree.command(name="ask", description="Ask the knowledge base a question")
        @app_commands.describe(question="What do you want to know?")
        async def ask(inter: discord.Interaction, question: str) -> None:
            if not await self._gate(inter):
                return
            if self.answerer is None:
                await inter.response.send_message(
                    "ANTHROPIC_API_KEY is not set, so /ask is disabled.", ephemeral=True
                )
                return
            await inter.response.defer(thinking=True)
            try:
                answer, notes = await self.answerer.ask(question, asked_by=self._person(inter.user))
            except Exception as e:  # noqa: BLE001
                log.exception("/ask failed")
                await inter.followup.send(_trim(f"❌ {type(e).__name__}: {e}"))
                return
            log.info("/ask by %s: %d note(s) used", inter.user, len(notes))
            for chunk in split_message(f"**Q:** {question.strip()}\n{answer}"):
                await inter.followup.send(chunk)

        @tree.command(
            name="decide", description="Record a decision in the decisions channel and the vault"
        )
        @app_commands.describe(
            title="Short imperative title, e.g. 'Price by quote above 50 seats'",
            what="What was decided",
            why="Why (rationale)",
            alternatives="Options considered and rejected",
            decided_by="Names, comma separated (default: you)",
        )
        async def decide(
            inter: discord.Interaction,
            title: str,
            what: str,
            why: str | None = None,
            alternatives: str | None = None,
            decided_by: str | None = None,
        ) -> None:
            if not await self._gate(inter):
                return
            await inter.response.defer(ephemeral=True)
            names = [n.strip() for n in (decided_by or "").split(",") if n.strip()] or [
                self._person(inter.user)
            ]
            when = datetime.now(self.cfg.tz).date()
            cid = self.cfg.decisions_channel_id or inter.channel_id or 0
            lines = [f"📌 **Decision: {title.strip()}**", f"**What:** {what.strip()}"]
            if why:
                lines.append(f"**Why:** {why.strip()}")
            if alternatives:
                lines.append(f"**Alternatives considered:** {alternatives.strip()}")
            lines.append(f"**Decided by:** {', '.join(names)} · {when.isoformat()}")
            text = "\n".join(lines)
            try:
                posted = await (await self._channel(cid)).send(_trim(text))
            except discord.HTTPException as e:
                await inter.followup.send(f"❌ Could not post in <#{cid}>: {e}", ephemeral=True)
                return
            rel, created = await asyncio.to_thread(
                self.pipeline.vault.write_decision,
                title=title,
                statement=what,
                rationale=why,
                alternatives=alternatives,
                decided_by=names,
                when=when,
                source_url=posted.jump_url,
            )
            with contextlib.suppress(discord.HTTPException):
                await posted.edit(content=_trim(f"{text}\n`{rel}`"))
            verb = "✅ Recorded" if created else "🔁 Already on file; linked this message to"
            await inter.followup.send(f"{verb} `{rel}` and posted in <#{cid}>.", ephemeral=True)

        @tree.command(name="digest", description="Build the week-in-review now (preview or post)")
        @app_commands.describe(
            days="Look back this many days (default 7)",
            post="Post to the announcements channel instead of a private preview",
        )
        async def digest(
            inter: discord.Interaction, days: app_commands.Range[int, 1, 60] = 7, post: bool = False
        ) -> None:
            if not await self._gate(inter):
                return
            await inter.response.defer(ephemeral=True, thinking=True)
            try:
                text = await self.build_week(days)
            except Exception as e:  # noqa: BLE001
                log.exception("/digest failed")
                await inter.followup.send(_trim(f"❌ {type(e).__name__}: {e}"), ephemeral=True)
                return
            if post:
                cid = self.cfg.weekly_digest.channel_id or inter.channel_id or 0
                await self._send_long(await self._channel(cid), text)
                await inter.followup.send(f"✅ Posted in <#{cid}>.", ephemeral=True)
                return
            for chunk in split_message(text):
                await inter.followup.send(chunk, ephemeral=True)

        rec_group = app_commands.Group(
            name="record", description="Record a voice channel into meeting notes"
        )
        tree.add_command(rec_group)

        @rec_group.command(name="start", description="Record the voice channel you are in")
        async def rec_start(inter: discord.Interaction) -> None:
            if not await self._gate(inter):
                return
            voice = getattr(inter.user, "voice", None)
            if voice is None or voice.channel is None:
                await inter.response.send_message("Join a voice channel first.", ephemeral=True)
                return
            if self._rec is not None:
                await inter.response.send_message(
                    f"Already recording <#{self._rec.channel.id}>.", ephemeral=True
                )
                return
            await inter.response.defer()
            try:
                await self._start_recording(voice.channel, inter.user, inter.channel_id or 0)
            except Exception as e:  # noqa: BLE001
                log.exception("could not start recording")
                await inter.followup.send(_trim(f"❌ Could not start recording: {e}"))
                return
            await inter.followup.send(
                f"🔴 Recording <#{voice.channel.id}>. `/record stop` when you are done; I also stop "
                f"when everyone leaves or after {self.cfg.meetings.max_minutes} minutes. Notes, "
                "decisions and suggested tasks follow once it is transcribed."
            )

        @rec_group.command(name="stop", description="Stop recording and write the meeting notes")
        async def rec_stop(inter: discord.Interaction) -> None:
            if not await self._gate(inter):
                return
            if self._rec is None:
                await inter.response.send_message("Not recording.", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True)
            await self._stop_recording(
                f"stopped by {self._person(inter.user)}", status_channel_id=inter.channel_id
            )
            await inter.followup.send("⏹ Stopped. Transcribing now.", ephemeral=True)

        @rec_group.command(name="status", description="Is anything being recorded?")
        async def rec_status(inter: discord.Interaction) -> None:
            rec = self._rec
            if rec is None:
                await inter.response.send_message("Not recording.", ephemeral=True)
                return
            speakers = ", ".join(t.name for t in rec.writer.tracks.values()) or "nobody yet"
            await inter.response.send_message(
                f"🔴 Recording <#{rec.channel.id}> for {rec.writer.elapsed() / 60:.0f} min, "
                f"started by {rec.requested_by}. Heard: {speakers}.",
                ephemeral=True,
            )

        task_group = app_commands.Group(name="task", description="Team to-dos")
        tree.add_command(task_group)

        @task_group.command(name="add", description="Add a task")
        @app_commands.describe(
            title="What needs doing",
            assignee="Who owns it (default: nobody)",
            due="When: 2026-09-12, 9/12, today, tomorrow, fri, next week, in 3 days",
            notes="Details or a link",
        )
        async def task_add(
            inter: discord.Interaction,
            title: str,
            assignee: discord.Member | None = None,
            due: str | None = None,
            notes: str | None = None,
        ) -> None:
            if not await self._gate(inter):
                return
            try:
                due_d = parse_due(due, self._today())
            except ValueError as e:
                await inter.response.send_message(f"❌ {e}", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True)
            tid = self.pipeline.state.add_task(
                title=title.strip(),
                notes=notes,
                assignee_id=assignee.id if assignee else None,
                assignee_name=self._person(assignee) if assignee else None,
                due=due_d.isoformat() if due_d else None,
                created_by_id=inter.user.id,
                created_by=self._person(inter.user),
            )
            task = self._task(tid)
            assert task is not None
            task = await self._post_card(task, inter.channel_id)
            await self._after_task_change(task)
            where = f" · <#{self.cfg.tasks.channel_id}>" if self.cfg.tasks.channel_id else ""
            await inter.followup.send(f"✅ Added task #{tid}{where}", ephemeral=True)

        @task_group.command(name="done", description="Mark a task done")
        @app_commands.describe(id="Task number", cancel="Cancel instead of completing")
        async def task_done(inter: discord.Interaction, id: int, cancel: bool = False) -> None:
            if not await self._gate(inter):
                return
            task = self._task(id)
            if task is None:
                await inter.response.send_message(f"No task #{id}.", ephemeral=True)
                return
            if not task.is_open:
                await inter.response.send_message(
                    f"#{id} is already {task.status}.", ephemeral=True
                )
                return
            await inter.response.defer(ephemeral=True)
            task = await self._close_task(
                task, "cancelled" if cancel else "done", self._person(inter.user)
            )
            await inter.followup.send(
                f"{'✖️' if cancel else '✅'} #{id} {task.title} — {task.status}.", ephemeral=True
            )

        @task_group.command(
            name="update", description="Change a task's title, owner, due date or notes"
        )
        @app_commands.describe(
            id="Task number",
            title="New title",
            assignee="New owner",
            due="New due date, or 'none' to clear",
            notes="New notes",
            reopen="Reopen a done or cancelled task",
        )
        async def task_update(
            inter: discord.Interaction,
            id: int,
            title: str | None = None,
            assignee: discord.Member | None = None,
            due: str | None = None,
            notes: str | None = None,
            reopen: bool = False,
        ) -> None:
            if not await self._gate(inter):
                return
            task = self._task(id)
            if task is None:
                await inter.response.send_message(f"No task #{id}.", ephemeral=True)
                return
            fields: dict = {}
            if title:
                fields["title"] = title.strip()
            if assignee is not None:
                fields["assignee_id"] = assignee.id
                fields["assignee_name"] = self._person(assignee)
            if due is not None:
                try:
                    d = parse_due(due, self._today())
                except ValueError as e:
                    await inter.response.send_message(f"❌ {e}", ephemeral=True)
                    return
                fields["due"] = d.isoformat() if d else None
            if notes is not None:
                fields["notes"] = notes
            if reopen:
                fields["status"] = "open"
                fields["done_at"] = None
            if not fields:
                await inter.response.send_message(f"Nothing to change. {DUE_HELP}", ephemeral=True)
                return
            await inter.response.defer(ephemeral=True)
            self.pipeline.state.update_task(id, **fields)
            task = self._task(id) or task
            await self._after_task_change(task)
            await inter.followup.send(f"✅ Updated #{id}: {', '.join(fields)}.", ephemeral=True)

        @task_group.command(name="list", description="Show open tasks")
        @app_commands.describe(assignee="Only this person's tasks", everyone="Post publicly")
        async def task_list(
            inter: discord.Interaction,
            assignee: discord.Member | None = None,
            everyone: bool = False,
        ) -> None:
            if not await self._gate(inter):
                return
            tasks = self._all_tasks()
            title = "**Open tasks**"
            if assignee is not None:
                tasks = [t for t in tasks if t.assignee_id == assignee.id]
                title = f"**Open tasks for {self._person(assignee)}**"
            text = render_board(tasks, self._today(), title=title)
            await inter.response.send_message(_trim(text), ephemeral=not everyone)

        @task_group.command(name="mine", description="Show my open tasks")
        async def task_mine(inter: discord.Interaction) -> None:
            if not await self._gate(inter):
                return
            tasks = [t for t in self._all_tasks() if t.assignee_id == inter.user.id]
            text = render_board(
                tasks, self._today(), title=f"**Open tasks for {self._person(inter.user)}**"
            )
            await inter.response.send_message(_trim(text), ephemeral=True)

        @tree.command(name="retry", description="Retry a failed drop (paste the message link)")
        async def retry(inter: discord.Interaction, link: str) -> None:
            await _re(inter, link, False)

        @tree.command(name="reingest", description="Re-run a drop even if it was already ingested")
        async def reingest(inter: discord.Interaction, link: str) -> None:
            await _re(inter, link, True)


# --- entry points ------------------------------------------------------------------------------


def _install_signal_handlers(client: discord.Client) -> None:
    if sys.platform == "win32":
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.close()))


async def run_bot(settings: Settings, cfg: BotConfig, pipeline: Pipeline) -> None:
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    bot = Bot(settings, cfg, pipeline)
    _install_signal_handlers(bot)
    async with bot:
        await bot.start(settings.discord_token)


async def run_oneshot(
    settings: Settings,
    cfg: BotConfig,
    pipeline: Pipeline,
    *,
    mode: str,
    channel: int | None,
    since: str | None,
) -> None:
    """`dob backfill` / `dob sync`: connect, walk history, drain digest jobs, exit."""
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")

    class OneShot(Bot):
        async def setup_hook(self) -> None:  # no command sync needed
            pass

        async def on_ready(self) -> None:
            if self._started:
                return
            self._started = True
            self.queue.start()
            try:
                totals = await self.archiver.walk(
                    channel_ids=[channel] if channel else None,
                    since=date.fromisoformat(since) if since else None,
                    full=(mode == "backfill"),
                )
                log.info("%s done: %s", mode, totals)
                while self.queue.size():
                    await asyncio.sleep(2)
            finally:
                await self.close()

    bot = OneShot(settings, cfg, pipeline)
    async with bot:
        await bot.start(settings.discord_token)
