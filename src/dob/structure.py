"""Structurer: turn extracted text into the StructuredOutput contract.

`FakeStructurer` is deterministic and offline (tests, smoke --fake-llm).
`ClaudeStructurer` lives in structure_claude.py and is imported lazily so the package works
without an API key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import (
    ActionItemOut,
    DecisionOut,
    EntityOut,
    Extracted,
    Structured,
    StructuredOutput,
)


@dataclass
class KnownEntity:
    type: str
    name: str
    aliases: list[str]
    path: str
    last_mention: str = ""


@dataclass
class KnownDecision:
    title: str
    path: str


@dataclass
class KnownContext:
    entities: list[KnownEntity] = field(default_factory=list)
    decisions: list[KnownDecision] = field(default_factory=list)


class StructureError(RuntimeError):
    """Deterministic structuring failure (refusal, truncated output, schema violation)."""


class Structurer(Protocol):
    async def structure(self, extracted: Extracted, context: KnownContext) -> Structured: ...

    async def describe_image(self, path: Path) -> str: ...

    async def read_pdf(self, path: Path) -> str: ...


_MENTION = re.compile(r"@([A-Z][\w-]+(?: [A-Z][\w-]+)?)")
_DECIDED = re.compile(r"(?im)^\s*[-*]?\s*(?:we\s+)?decided(?:\s+to)?\s*:?\s*(.+)$")
_TODO = re.compile(r"(?im)^\s*[-*]?\s*(?:TODO|ACTION)\s*:?\s*(.+)$")


class FakeStructurer:
    """Deterministic stand-in: entities from @Name, decisions from 'decided ...' lines."""

    model = "fake"
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    async def structure(self, extracted: Extracted, context: KnownContext) -> Structured:
        self.calls += 1
        text = extracted.text
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
        title = (first or extracted.item.original_name)[:60]
        known = {e.name.casefold(): e for e in context.entities}
        entities: list[EntityOut] = []
        seen: set[str] = set()
        for m in _MENTION.finditer(text):
            name = m.group(1)
            if name.casefold() in seen:
                continue
            seen.add(name.casefold())
            k = known.get(name.casefold())
            entities.append(
                EntityOut(
                    type=k.type if k else "person",  # type: ignore[arg-type]
                    name=k.name if k else name,
                    aliases=[],
                    role_in_source=f"mentioned in {title}",
                    is_new=k is None,
                    description=None if k else f"{name} (auto-created)",
                )
            )
        decisions = [
            DecisionOut(title=d.strip().rstrip(".")[:60], statement=d.strip())
            for d in _DECIDED.findall(text)
        ]
        actions = [ActionItemOut(task=t.strip()) for t in _TODO.findall(text)]
        lines = [ln.strip("-* ").strip() for ln in text.splitlines() if ln.strip()]
        out = StructuredOutput(
            suggested_title=title,
            summary=" ".join(lines[:2])[:400] or "(empty)",
            key_points=lines[1:4] or [title],
            decisions=decisions,
            action_items=actions,
            entities=entities,
            participants=[e.name for e in entities if e.type == "person"],
            tags=[extracted.kind],
        )
        return Structured(output=out, model=self.model, input_tokens=len(text) // 4)

    async def describe_image(self, path: Path) -> str:
        return f"(fake image description of {path.name})"

    async def read_pdf(self, path: Path) -> str:
        return f"{path.stem}\n(scanned pdf, fake transcription of {path.name})"
