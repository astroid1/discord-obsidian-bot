"""Data objects passed between pipeline stages, plus the LLM output contract.

Everything here is a pydantic model so jobs can be serialised into the ledger and
re-queued after a restart.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SourceKind = Literal["discord_attachment", "discord_url", "inbox", "chat_day"]
NoteKind = Literal["recording", "memo", "document", "chat-digest"]
EntityKind = Literal["person", "project", "topic"]

INTERACTIVE = 0
BACKGROUND = 5


class DiscordRef(BaseModel):
    """Where an item came from on Discord."""

    guild_id: int
    channel_id: int
    channel_name: str
    message_id: int
    author_id: int
    author_name: str
    jump_url: str
    created_at: datetime


class IngestItem(BaseModel):
    """A unit of work entering the pipeline."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: SourceKind
    original_name: str
    # Canonical identity used for pre-download dedupe: "youtube:<id>", "inbox:<name>",
    # "chat:<channel_id>:<day>", "discord:<message_id>:<attachment_id>".
    source_ref: str
    url: str | None = None
    local_path: Path | None = None
    mime: str | None = None
    sha256: str | None = None
    discord: DiscordRef | None = None
    priority: int = BACKGROUND
    force: bool = False
    # chat_day only
    channel_id: int | None = None
    channel_name: str | None = None
    day: str | None = None


class Segment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class Extracted(BaseModel):
    """Text (and for media, timed segments) pulled out of an item."""

    item: IngestItem
    kind: NoteKind
    text: str
    segments: list[Segment] | None = None
    duration_s: float | None = None
    language: str | None = None
    page_count: int | None = None
    meta: dict[str, str] = Field(default_factory=dict)


# --- LLM contract -----------------------------------------------------------------


class EntityOut(BaseModel):
    type: EntityKind
    name: str = Field(
        description="Canonical name. MUST equal a known entity's name if one applies."
    )
    aliases: list[str] = Field(default_factory=list, description="Other surface forms seen here.")
    role_in_source: str = Field(description="One line: what this entity did or was in this source.")
    is_new: bool = Field(description="True if this entity is not in the known-entity list.")
    description: str | None = Field(
        default=None, description="One-line description; only used when is_new is true."
    )


class DecisionOut(BaseModel):
    title: str = Field(description="Short imperative title, e.g. 'Use Postgres for the CRM'.")
    statement: str
    rationale: str | None = None
    decided_by: list[str] = Field(default_factory=list, description="Person names.")
    matches_existing: str | None = Field(
        default=None,
        description="Exact title of a known decision this reaffirms or revisits, else null.",
    )


class ActionItemOut(BaseModel):
    task: str
    owner: str | None = Field(default=None, description="Person name or null.")
    due: str | None = Field(default=None, description="ISO date if explicitly stated, else null.")


class StructuredOutput(BaseModel):
    """What the structuring LLM returns for any source."""

    suggested_title: str = Field(description="At most 60 characters, no date.")
    summary: str = Field(description="2-4 sentences.")
    key_points: list[str] = Field(description="3-10 bullets.")
    decisions: list[DecisionOut] = Field(default_factory=list)
    action_items: list[ActionItemOut] = Field(default_factory=list)
    entities: list[EntityOut] = Field(default_factory=list)
    participants: list[str] = Field(
        default_factory=list, description="Names of people present or speaking."
    )
    tags: list[str] = Field(default_factory=list, description="2-6 lowercase kebab-case tags.")


class Structured(BaseModel):
    output: StructuredOutput
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


# --- Output ---------------------------------------------------------------------------


class WrittenNote(BaseModel):
    note_path: str  # vault-relative, with .md
    link: str  # vault-relative without .md, usable as [[link]]
    title: str
    entities_created: list[str] = Field(default_factory=list)
    entities_updated: list[str] = Field(default_factory=list)
    decisions_created: list[str] = Field(default_factory=list)
    commit_sha: str | None = None


class RunResult(BaseModel):
    status: Literal["done", "skipped", "failed"]
    note: WrittenNote | None = None
    message: str = ""
    summary: str = ""


# --- Chat --------------------------------------------------------------------------------


class ChatAttachment(BaseModel):
    id: int
    filename: str
    url: str
    size: int = 0


class ChatMessage(BaseModel):
    id: int
    channel_id: int  # the archived (parent) channel
    thread_id: int | None = None
    thread_name: str | None = None
    author_id: int
    author_name: str
    created_at: datetime
    content: str
    attachments: list[ChatAttachment] = Field(default_factory=list)
    reply_to_id: int | None = None
    jump_url: str
