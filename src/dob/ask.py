"""Ask the vault and summarise the week.

Two things live here because they share the vault reader:

- `VaultIndex` + `Answerer.ask()`: `/ask` picks the notes that best match a question with plain
  keyword scoring (the vault is small; no embeddings needed) and lets Claude answer from those
  notes only, citing them.
- `collect_week()` + `render_week()` + `Answerer.weekly()`: the weekly digest posted to the
  announcements channel. The facts are gathered deterministically from frontmatter; Claude only
  turns them into a readable note, and a plain rendering is used when no API key is set.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

from .vault import load_note

log = logging.getLogger(__name__)

SKIP_DIRS = {".obsidian", ".git", ".trash", "attachments"}
DISCORD_LIMIT = 1990
_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]+")
_STOPWORDS = """a an and are as at be but by can did do does for from had has have how i if in into is it
    its of on or our that the their them then there these they this to was we were what when where
    which who why will with would you your about after all also any because been before being
    could get got just like more most much need not now only other over same should so some such
    than too up us very want"""
_STOP = frozenset(_STOPWORDS.split())


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries so every chunk fits a Discord message."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    cur = ""
    for ln in text.splitlines(keepends=True):
        while len(ln) > limit:  # a single overlong line: hard split
            if cur:
                out.append(cur.rstrip())
                cur = ""
            out.append(ln[:limit])
            ln = ln[limit:]
        if len(cur) + len(ln) > limit:
            out.append(cur.rstrip())
            cur = ""
        cur += ln
    if cur.strip():
        out.append(cur.rstrip())
    return out


# --- vault reading -----------------------------------------------------------------------------


@dataclass
class Note:
    rel: str  # vault-relative path, with .md
    title: str
    kind: str
    date: str
    ingested_at: str
    body: str
    meta: dict

    @property
    def link(self) -> str:
        return self.rel.removesuffix(".md")


def read_notes(root: Path) -> list[Note]:
    notes: list[Note] = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if rel.parts[0] in SKIP_DIRS or p.name == "Home.md":
            continue
        try:
            meta, body = load_note(p)
        except Exception:  # noqa: BLE001 - one bad note must not break /ask
            log.warning("could not parse %s; skipping", p)
            continue
        notes.append(
            Note(
                rel=rel.as_posix(),
                title=str(meta.get("title") or meta.get("name") or p.stem),
                kind=str(meta.get("type") or rel.parts[0]),
                date=str(meta.get("date") or meta.get("decided_on") or meta.get("updated") or ""),
                ingested_at=str(meta.get("ingested_at") or ""),
                body=body,
                meta=meta,
            )
        )
    return notes


class VaultIndex:
    """Keyword search over the vault. Rebuilt on every query; a company vault is small."""

    KIND_WEIGHT = {"decision": 1.5, "person": 1.2, "project": 1.2, "topic": 1.2, "chat-log": 0.6}

    def __init__(self, root: Path):
        self.root = Path(root)

    def search(
        self, query: str, *, limit: int = 12, budget_chars: int = 120_000, note_chars: int = 12_000
    ) -> list[Note]:
        q = tokens(query)
        if not q:
            return []
        qset = set(q)
        scored: list[tuple[float, Note]] = []
        for n in read_notes(self.root):
            body_toks = tokens(n.body)
            if not body_toks and not n.title:
                continue
            title_toks = set(tokens(n.title))
            hits = 0.0
            for t in qset:
                tf = body_toks.count(t)
                if tf:
                    hits += 1 + min(tf, 8) / 4
                if t in title_toks:
                    hits += 4
            if not hits:
                continue
            score = hits * self.KIND_WEIGHT.get(n.kind, 1.0)
            score += min(len(qset & set(body_toks)) / len(qset), 1.0) * 3  # crenewal
            scored.append((score, n))
        scored.sort(key=lambda s: (s[0], s[1].date), reverse=True)
        out: list[Note] = []
        used = 0
        for _, n in scored:
            body = n.body[:note_chars]
            if used + len(body) > budget_chars and out:
                break
            out.append(Note(**{**n.__dict__, "body": body}))
            used += len(body)
            if len(out) >= limit:
                break
        return out


def notes_block(notes: list[Note]) -> str:
    parts = []
    for n in notes:
        parts.append(
            f'<note path="{n.link}" title="{n.title}" type="{n.kind}" date="{n.date}">\n'
            f"{n.body.strip()}\n</note>"
        )
    return "\n\n".join(parts) or "(no matching notes)"


# --- weekly digest ----------------------------------------------------------------------------


@dataclass
class WeekSummary:
    since: date
    until: date
    sources: list[Note] = field(default_factory=list)
    decisions: list[Note] = field(default_factory=list)
    action_items: list[tuple[str, str]] = field(default_factory=list)  # (task line, note title)
    new_entities: list[Note] = field(default_factory=list)
    chat_days: int = 0


def _first_section(body: str, heading: str) -> str:
    m = re.search(rf"## {re.escape(heading)}\n+(.+?)(?:\n## |\Z)", body, re.S)
    return m.group(1).strip() if m else ""


def collect_week(root: Path, *, since: date, until: date) -> WeekSummary:
    """Everything that entered the vault in [since, until]. Dates come from frontmatter."""
    w = WeekSummary(since=since, until=until)
    lo, hi = since.isoformat(), until.isoformat()
    for n in read_notes(root):
        if n.kind == "chat-log":
            if lo <= n.date <= hi:
                w.chat_days += 1
            continue
        if n.kind == "decision":
            if lo <= n.date <= hi:
                w.decisions.append(n)
            continue
        if n.kind in ("person", "project", "topic"):
            created = str(n.meta.get("created") or "")
            if lo <= created <= hi:
                w.new_entities.append(n)
            continue
        stamp = n.ingested_at[:10] or n.date
        if lo <= stamp <= hi:
            w.sources.append(n)
            for ln in n.body.splitlines():
                if ln.startswith("- [ ] "):
                    w.action_items.append((ln[6:].strip(), n.title))
    w.sources.sort(key=lambda n: (n.date, n.title))
    w.decisions.sort(key=lambda n: (n.date, n.title))
    return w


def render_week(w: WeekSummary) -> str:
    """Plain Discord markdown, no wikilinks. Also the fact sheet Claude rewrites from."""
    lines = [f"**Week in review · {w.since.isoformat()} → {w.until.isoformat()}**", ""]
    if w.decisions:
        lines.append("**Decisions**")
        for d in w.decisions:
            stmt = _first_section(d.body, "Statement").splitlines()[:1]
            lines.append(f"• {d.title}" + (f" — {stmt[0]}" if stmt else ""))
        lines.append("")
    if w.sources:
        lines.append(f"**New in the knowledge base** ({len(w.sources)})")
        for s in w.sources[:15]:
            summary = _first_section(s.body, "Summary").replace("\n", " ")
            lines.append(
                f"• {s.date} · {s.kind} · {s.title}" + (f" — {summary[:140]}" if summary else "")
            )
        if len(w.sources) > 15:
            lines.append(f"• …and {len(w.sources) - 15} more")
        lines.append("")
    if w.action_items:
        lines.append("**Open action items**")
        for task, title in w.action_items[:15]:
            lines.append(f"• {task}  ({title})")
        lines.append("")
    if w.new_entities:
        names = ", ".join(e.title for e in w.new_entities[:20])
        lines.append(f"**New pages:** {names}")
        lines.append("")
    if w.chat_days:
        lines.append(f"_{w.chat_days} channel-days of chat archived._")
    if not (w.decisions or w.sources or w.action_items or w.new_entities or w.chat_days):
        lines.append("_A quiet week: nothing new reached the knowledge base._")
    return "\n".join(lines).strip()


def next_run(now: datetime, weekday: int, hour: int) -> datetime:
    """Next `weekday` (0=Monday) at `hour`:00 strictly after `now`, in now's timezone."""
    cand = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    cand += timedelta(days=(weekday - now.weekday()) % 7)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


# --- Claude ----------------------------------------------------------------------------------


def _prompt(name: str) -> str:
    return resources.files("dob.prompts").joinpath(name).read_text(encoding="utf-8")


class Answerer:
    """Claude on top of the vault. Constructed only when an API key is configured."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        vault_root: Path,
        tz: ZoneInfo,
        max_notes: int = 12,
        budget_chars: int = 120_000,
        note_chars: int = 12_000,
        max_tokens: int = 2000,
    ):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, max_retries=2, timeout=120)
        self.model = model
        self.index = VaultIndex(vault_root)
        self.root = Path(vault_root)
        self.tz = tz
        self.max_notes = max_notes
        self.budget_chars = budget_chars
        self.note_chars = note_chars
        self.max_tokens = max_tokens

    async def _text(self, system: str, user: str) -> str:
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    async def ask(self, question: str, *, asked_by: str = "") -> tuple[str, list[Note]]:
        notes = self.index.search(
            question,
            limit=self.max_notes,
            budget_chars=self.budget_chars,
            note_chars=self.note_chars,
        )
        today = datetime.now(self.tz).date().isoformat()
        user = (
            f"<today>{today}</today>\n<asked_by>{asked_by}</asked_by>\n"
            f"<question>{question.strip()}</question>\n\n{notes_block(notes)}"
        )
        answer = await self._text(_prompt("ask.md"), user)
        return answer, notes

    async def weekly(self, w: WeekSummary) -> str:
        facts = render_week(w)
        if "quiet week" in facts:
            return facts
        try:
            return await self._text(_prompt("weekly_digest.md"), facts)
        except Exception:  # noqa: BLE001 - the plain rendering is always good enough
            log.exception("weekly digest LLM call failed; posting the plain rendering")
            return facts


__all__ = [
    "Answerer",
    "Note",
    "VaultIndex",
    "WeekSummary",
    "collect_week",
    "next_run",
    "notes_block",
    "read_notes",
    "render_week",
    "split_message",
    "tokens",
]
