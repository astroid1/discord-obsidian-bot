"""SQLite state: ingest ledger, chat cursors, archived messages, chat-day bookkeeping.

Plain synchronous sqlite3 behind a lock. Every call is a handful of small statements, so
calling it from the event loop is fine; heavy callers (the history walker) batch per page.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .models import ChatAttachment, ChatMessage, IngestItem

INFLIGHT = ("queued", "fetching", "extracting", "structuring", "writing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingests (
  id TEXT PRIMARY KEY,
  sha256 TEXT,
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  original_name TEXT,
  note_path TEXT,
  status TEXT NOT NULL,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  discord_message_id INTEGER,
  item_json TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ingests_sha ON ingests(sha256);
CREATE INDEX IF NOT EXISTS ix_ingests_ref ON ingests(source_ref);
CREATE INDEX IF NOT EXISTS ix_ingests_msg ON ingests(discord_message_id);
CREATE INDEX IF NOT EXISTS ix_ingests_status ON ingests(status);

CREATE TABLE IF NOT EXISTS cursors (
  channel_id INTEGER PRIMARY KEY,
  parent_id INTEGER,
  name TEXT,
  last_message_id INTEGER,
  last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL,
  thread_id INTEGER,
  thread_name TEXT,
  author_id INTEGER NOT NULL,
  author_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  day TEXT NOT NULL,
  content TEXT NOT NULL,
  attachments_json TEXT NOT NULL,
  reply_to_id INTEGER,
  jump_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_day ON messages(channel_id, day, created_at);

CREATE TABLE IF NOT EXISTS chat_days (
  channel_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  message_count INTEGER NOT NULL,
  log_path TEXT,
  digest_path TEXT,
  digest_message_count INTEGER,
  PRIMARY KEY (channel_id, day)
);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- ingest ledger ---------------------------------------------------------

    def submit(self, item: IngestItem) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO ingests(id, sha256, source, source_ref, original_name, status,
                       discord_message_id, item_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,'queued',?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET status='queued', updated_at=excluded.updated_at""",
                (
                    item.id,
                    item.sha256,
                    item.source,
                    item.source_ref,
                    item.original_name,
                    item.discord.message_id if item.discord else None,
                    item.model_dump_json(),
                    _now(),
                    _now(),
                ),
            )

    def set_status(
        self,
        item_id: str,
        status: str,
        *,
        error: str | None = None,
        note_path: str | None = None,
        sha256: str | None = None,
        item: IngestItem | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        bump_attempts: bool = False,
    ) -> None:
        sets = ["status=?", "updated_at=?"]
        args: list[object] = [status, _now()]
        if error is not None:
            sets.append("error=?")
            args.append(error[:1000])
        if note_path is not None:
            sets.append("note_path=?")
            args.append(note_path)
        if sha256 is not None:
            sets.append("sha256=?")
            args.append(sha256)
        if item is not None:
            sets.append("item_json=?")
            args.append(item.model_dump_json())
        if input_tokens is not None:
            sets.append("input_tokens=?")
            args.append(input_tokens)
        if output_tokens is not None:
            sets.append("output_tokens=?")
            args.append(output_tokens)
        if bump_attempts:
            sets.append("attempts=attempts+1")
        args.append(item_id)
        with self._lock:
            self._db.execute(f"UPDATE ingests SET {', '.join(sets)} WHERE id=?", args)

    def get(self, item_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM ingests WHERE id=?", (item_id,)).fetchone()

    def find_done_by_sha(self, sha256: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM ingests WHERE sha256=? AND status='done' ORDER BY updated_at DESC",
                (sha256,),
            ).fetchone()

    def find_done_by_ref(self, source_ref: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM ingests WHERE source_ref=? AND status='done' ORDER BY updated_at DESC",
                (source_ref,),
            ).fetchone()

    def find_by_message(self, message_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM ingests WHERE discord_message_id=? ORDER BY created_at",
                (message_id,),
            ).fetchall()

    def note_for_message(self, message_id: int) -> str | None:
        for row in self.find_by_message(message_id):
            if row["status"] == "done" and row["note_path"]:
                return row["note_path"]
        return None

    def inflight(self) -> list[IngestItem]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT item_json FROM ingests WHERE status IN ({','.join('?' * len(INFLIGHT))})"
                " ORDER BY created_at",
                INFLIGHT,
            ).fetchall()
        return [IngestItem.model_validate_json(r["item_json"]) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM ingests GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def recent(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM ingests ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()

    # --- chat cursors -------------------------------------------------------------

    def get_cursor(self, channel_id: int) -> int | None:
        with self._lock:
            row = self._db.execute(
                "SELECT last_message_id FROM cursors WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return row["last_message_id"] if row else None

    def set_cursor(
        self, channel_id: int, last_message_id: int, *, parent_id: int | None, name: str
    ) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO cursors(channel_id, parent_id, name, last_message_id, last_synced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET parent_id=excluded.parent_id,
                     name=excluded.name, last_message_id=excluded.last_message_id,
                     last_synced_at=excluded.last_synced_at""",
                (channel_id, parent_id, name, last_message_id, _now()),
            )

    def cursors(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM cursors ORDER BY parent_id, name").fetchall()

    def reset_cursors(self, channel_ids: list[int] | None = None) -> None:
        with self._lock:
            if channel_ids is None:
                self._db.execute("DELETE FROM cursors")
            else:
                for cid in channel_ids:
                    self._db.execute(
                        "DELETE FROM cursors WHERE channel_id=? OR parent_id=?", (cid, cid)
                    )

    # --- messages ------------------------------------------------------------------

    def upsert_messages(self, msgs: list[ChatMessage], days: list[str]) -> None:
        """Store a page of messages. `days` = local day string per message (same order)."""
        rows = [
            (
                m.id,
                m.channel_id,
                m.thread_id,
                m.thread_name,
                m.author_id,
                m.author_name,
                m.created_at.isoformat(),
                day,
                m.content,
                json.dumps([a.model_dump() for a in m.attachments]),
                m.reply_to_id,
                m.jump_url,
            )
            for m, day in zip(msgs, days, strict=True)
        ]
        with self._lock:
            self._db.executemany(
                """INSERT INTO messages(id, channel_id, thread_id, thread_name, author_id, author_name,
                       created_at, day, content, attachments_json, reply_to_id, jump_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET content=excluded.content,
                     attachments_json=excluded.attachments_json, thread_name=excluded.thread_name""",
                rows,
            )

    def messages_for_day(self, channel_id: int, day: str) -> list[ChatMessage]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE channel_id=? AND day=? ORDER BY created_at, id",
                (channel_id, day),
            ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def message(self, message_id: int) -> ChatMessage | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._row_to_msg(row) if row else None

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> ChatMessage:
        return ChatMessage(
            id=r["id"],
            channel_id=r["channel_id"],
            thread_id=r["thread_id"],
            thread_name=r["thread_name"],
            author_id=r["author_id"],
            author_name=r["author_name"],
            created_at=datetime.fromisoformat(r["created_at"]),
            content=r["content"],
            attachments=[
                ChatAttachment.model_validate(a) for a in json.loads(r["attachments_json"])
            ],
            reply_to_id=r["reply_to_id"],
            jump_url=r["jump_url"],
        )

    def day_counts(self, channel_id: int, days: list[str]) -> dict[str, int]:
        if not days:
            return {}
        with self._lock:
            rows = self._db.execute(
                "SELECT day, COUNT(*) AS n FROM messages WHERE channel_id=? AND day IN "
                f"({','.join('?' * len(days))}) GROUP BY day",
                (channel_id, *days),
            ).fetchall()
        return {r["day"]: r["n"] for r in rows}

    # --- chat days ------------------------------------------------------------------

    def get_chat_day(self, channel_id: int, day: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM chat_days WHERE channel_id=? AND day=?", (channel_id, day)
            ).fetchone()

    def upsert_chat_day(self, channel_id: int, day: str, message_count: int, log_path: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO chat_days(channel_id, day, message_count, log_path)
                   VALUES (?,?,?,?)
                   ON CONFLICT(channel_id, day) DO UPDATE SET message_count=excluded.message_count,
                     log_path=excluded.log_path""",
                (channel_id, day, message_count, log_path),
            )

    def set_digest(self, channel_id: int, day: str, digest_path: str, message_count: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE chat_days SET digest_path=?, digest_message_count=? WHERE channel_id=? AND day=?",
                (digest_path, message_count, channel_id, day),
            )

    # --- key/value ----------------------------------------------------------------

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
