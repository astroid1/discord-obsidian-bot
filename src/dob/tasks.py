"""To-dos: due-date parsing and the text the bot posts for `/task`, the board, reminders and
the vault export. Storage lives in `state.py` (the `tasks` table); Discord wiring in `bot.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
STATUSES = ("suggested", "open", "done", "cancelled")
STATUS_ICON = {"suggested": "💡", "open": "☐", "done": "✅", "cancelled": "✖️"}
BOARD_TITLE = "**Open tasks**"
DUE_HELP = (
    "Use `YYYY-MM-DD`, `9/12`, `today`, `tomorrow`, a weekday like `fri`, `next week`, "
    "`in 3 days`, `2w`, or `none` to clear."
)
_REL = re.compile(r"(?:in\s+)?(\d+)\s*(d|day|days|w|wk|week|weeks)")
_MDY = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")


def parse_due(text: str | None, today: date) -> date | None:
    """Turn what a person types into a date. Raises ValueError with a hint when it cannot."""
    s = (text or "").strip().lower()
    if not s or s in ("none", "-", "clear", "no"):
        return None
    if s in ("today", "eod", "tonight"):
        return today
    if s in ("tomorrow", "tmrw", "tmr"):
        return today + timedelta(days=1)
    if s in ("next week", "nextweek"):
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    if s in ("end of week", "eow"):
        return today + timedelta(days=(4 - today.weekday()) % 7)
    if m := _REL.fullmatch(s):
        n = int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2).startswith("w") else 1))
    nxt = s.removeprefix("next ").strip()
    if len(nxt) >= 3:
        for i, name in enumerate(WEEKDAYS):
            if name.startswith(nxt):
                ahead = (i - today.weekday()) % 7 or 7
                if s.startswith("next ") and ahead < 7:
                    ahead += 7
                return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    if m := _MDY.fullmatch(s):
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = today.year if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
        try:
            return date(year, mo, d)
        except ValueError as e:
            raise ValueError(f"{text!r} is not a real date. {DUE_HELP}") from e
    raise ValueError(f"Could not read a date from {text!r}. {DUE_HELP}")


@dataclass
class Task:
    id: int
    title: str
    notes: str | None
    assignee_id: int | None
    assignee_name: str | None
    due: str | None
    status: str
    created_by_id: int | None
    created_by: str | None
    created_at: str
    updated_at: str
    done_at: str | None
    message_id: int | None
    source_url: str | None
    source_note: str | None = None

    @classmethod
    def from_row(cls, row) -> Task:
        keys = set(row.keys())
        return cls(**{k: row[k] for k in cls.__dataclass_fields__ if k in keys})

    @property
    def due_date(self) -> date | None:
        return date.fromisoformat(self.due) if self.due else None

    @property
    def is_open(self) -> bool:
        return self.status == "open"


def due_label(due: str | None, today: date) -> str:
    if not due:
        return "no due date"
    d = date.fromisoformat(due)
    delta = (d - today).days
    if delta < 0:
        return f"overdue by {-delta}d ({due})"
    if delta == 0:
        return "due today"
    if delta == 1:
        return "due tomorrow"
    if delta < 7:
        return f"due {d.strftime('%a')} ({due})"
    return f"due {due}"


def due_flag(t: Task, today: date) -> str:
    d = t.due_date
    if not d or not t.is_open:
        return ""
    if d < today:
        return "🔴 "
    if d == today:
        return "🟡 "
    return ""


def mention(t: Task) -> str:
    if t.assignee_id:
        return f"<@{t.assignee_id}>"
    return t.assignee_name or "unassigned"


def _render_suggestion(t: Task, today: date) -> str:
    lines = [
        f"💡 **Suggested #{t.id} · {t.title.strip()}**",
        f"{mention(t)} · {due_label(t.due, today)}",
    ]
    if t.notes and t.notes.strip():
        lines.append(t.notes.strip())
    lines.append("-# React ✅ to put it on the board or ❌ to dismiss")
    return "\n".join(lines)


def render_card(t: Task, today: date) -> str:
    if t.status == "suggested":
        return _render_suggestion(t, today)
    icon = STATUS_ICON.get(t.status, "☐")
    head = f"#{t.id} · {t.title.strip()}"
    if not t.is_open:
        head = f"~~{head}~~"
    lines = [f"{due_flag(t, today)}{icon} **{head}**"]
    meta = [mention(t), due_label(t.due, today) if t.is_open else t.status]
    if t.created_by:
        meta.append(f"added by {t.created_by}")
    if t.done_at and not t.is_open:
        meta.append(f"closed {t.done_at[:10]}")
    lines.append(" · ".join(meta))
    if t.notes and t.notes.strip():
        lines.append(t.notes.strip())
    if t.is_open:
        lines.append(f"-# React ✅ or `/task done id:{t.id}` when finished")
    return "\n".join(lines)


def _sort_key(t: Task) -> tuple:
    return (t.due is None, t.due or "", t.id)


def render_board(tasks: list[Task], today: date, *, title: str = BOARD_TITLE) -> str:
    open_tasks = sorted((t for t in tasks if t.is_open), key=_sort_key)
    if not open_tasks:
        return f"{title} · {today.isoformat()}\nNothing open. Nice."
    groups: dict[str, list[Task]] = {}
    for t in open_tasks:
        groups.setdefault(t.assignee_name or "Unassigned", []).append(t)
    lines = [f"{title} · {today.isoformat()} · {len(open_tasks)} open"]
    for who, ts in sorted(groups.items(), key=lambda kv: (kv[0] == "Unassigned", kv[0])):
        lines.append(f"\n**{who}**")
        for t in ts:
            lines.append(
                f"• {due_flag(t, today)}#{t.id} {t.title.strip()} — {due_label(t.due, today)}"
            )
    return "\n".join(lines)


def render_reminder(tasks: list[Task], today: date) -> str | None:
    """Morning nudge: overdue and due-today tasks with mentions. None when there is nothing."""
    overdue = sorted(
        (t for t in tasks if t.is_open and t.due and t.due < today.isoformat()), key=_sort_key
    )
    due_today = [t for t in tasks if t.is_open and t.due == today.isoformat()]
    if not overdue and not due_today:
        return None
    lines = [f"⏰ **Task reminder · {today.strftime('%a %Y-%m-%d')}**"]
    if overdue:
        lines.append("**Overdue**")
        lines += [
            f"• #{t.id} {t.title.strip()} — {mention(t)} · {due_label(t.due, today)}"
            for t in overdue
        ]
    if due_today:
        lines.append("**Due today**")
        lines += [f"• #{t.id} {t.title.strip()} — {mention(t)}" for t in due_today]
    return "\n".join(lines)


def render_vault_page(tasks: list[Task], today: date, *, recent_days: int = 30) -> str:
    """tasks/Tasks.md: generated, one table of open tasks and one of recently closed ones."""
    cutoff = (today - timedelta(days=recent_days)).isoformat()
    open_tasks = sorted((t for t in tasks if t.is_open), key=_sort_key)
    closed = sorted(
        (
            t
            for t in tasks
            if t.status in ("done", "cancelled") and (t.done_at or "")[:10] >= cutoff
        ),
        key=lambda t: t.done_at or "",
        reverse=True,
    )

    def row(t: Task, last: str) -> str:
        who = f"[[{t.assignee_name}]]" if t.assignee_name else ""
        title = t.title.strip().replace("|", "／")
        return f"| {t.id} | {title} | {who} | {t.due or ''} | {last} |"

    lines = [
        "<!-- generated by discord-obsidian-bot; edits to this file will be overwritten -->",
        "",
        "# Tasks",
        "",
        f"Updated {today.isoformat()} · {len(open_tasks)} open",
        "",
        "## Open",
        "",
        "| # | Task | Owner | Due | Added |",
        "|---|---|---|---|---|",
        *[row(t, t.created_at[:10]) for t in open_tasks],
        "",
        f"## Closed in the last {recent_days} days",
        "",
        "| # | Task | Owner | Due | Closed |",
        "|---|---|---|---|---|",
        *[row(t, f"{t.status} {(t.done_at or '')[:10]}") for t in closed],
        "",
    ]
    return "\n".join(lines)


def week_tasks_line(tasks: list[Task], today: date) -> str:
    """One paragraph for the weekly digest."""
    open_tasks = [t for t in tasks if t.is_open]
    if not open_tasks:
        return ""
    overdue = [t for t in open_tasks if t.due and t.due < today.isoformat()]
    soon = [
        t
        for t in open_tasks
        if t.due and today.isoformat() <= t.due <= (today + timedelta(days=7)).isoformat()
    ]
    parts = [f"**Tasks:** {len(open_tasks)} open"]
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if soon:
        parts.append(f"{len(soon)} due this week")
    lines = [", ".join(parts)]
    for t in sorted(overdue + soon, key=_sort_key)[:6]:
        lines.append(
            f"• #{t.id} {t.title.strip()} — {t.assignee_name or 'unassigned'} · {due_label(t.due, today)}"
        )
    return "\n".join(lines)


# --- suggestions from action items -------------------------------------------------------------


@dataclass
class Suggestion:
    title: str
    owner_id: int | None
    owner_name: str | None
    due: str | None


def resolve_owner(name: str | None, people: dict[int, str]) -> tuple[int | None, str | None]:
    """Map an action item's owner ("Jordan", "Sam") to a Discord user via config `people`.
    Full-name match first, then a unique first-name match; otherwise keep the name unlinked."""
    if not name or not name.strip():
        return None, None
    key = name.strip().casefold()
    exact = [uid for uid, n in people.items() if n.casefold() == key]
    if len(exact) == 1:
        return exact[0], people[exact[0]]
    first = key.split()[0]
    by_first = [uid for uid, n in people.items() if n.split() and n.split()[0].casefold() == first]
    if len(by_first) == 1:
        return by_first[0], people[by_first[0]]
    return None, name.strip()


def suggestion_due(due: str | None, today: date) -> str | None:
    if not due or not due.strip():
        return None
    try:
        return date.fromisoformat(due.strip()[:10]).isoformat()
    except ValueError:
        pass
    try:
        d = parse_due(due, today)
    except ValueError:
        return None
    return d.isoformat() if d else None


def suggestions_from(action_items, people: dict[int, str], today: date, *, limit: int = 8):
    """Turn a note's action items into task suggestions: tidy titles, owners, dates, no repeats."""
    out: list[Suggestion] = []
    seen: set[str] = set()
    for a in action_items:
        title = " ".join((getattr(a, "task", "") or "").split()).strip(" .")
        if not title or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        uid, name = resolve_owner(getattr(a, "owner", None), people)
        out.append(
            Suggestion(title[:200], uid, name, suggestion_due(getattr(a, "due", None), today))
        )
        if len(out) >= limit:
            break
    return out


__all__ = [
    "BOARD_TITLE",
    "DUE_HELP",
    "STATUSES",
    "Task",
    "due_label",
    "mention",
    "parse_due",
    "render_board",
    "render_card",
    "render_reminder",
    "render_vault_page",
    "resolve_owner",
    "suggestions_from",
    "week_tasks_line",
]
