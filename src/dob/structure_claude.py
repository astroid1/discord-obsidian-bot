"""ClaudeStructurer: Anthropic SDK structured outputs, known-entity context, chunk + merge."""

from __future__ import annotations

import base64
import logging
from importlib import resources
from pathlib import Path

from anthropic import AsyncAnthropic

from .config import LLMConfig
from .models import Extracted, Structured, StructuredOutput
from .structure import KnownContext, StructureError

log = logging.getLogger(__name__)

MAX_KNOWN_ENTITIES = 400
MAX_KNOWN_DECISIONS = 200
IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _prompt(name: str) -> str:
    return resources.files("dob.prompts").joinpath(name).read_text(encoding="utf-8")


class ClaudeStructurer:
    def __init__(
        self, *, api_key: str, model: str, llm_cfg: LLMConfig | None = None, max_tokens: int = 16000
    ):
        self.client = AsyncAnthropic(api_key=api_key, max_retries=3, timeout=600)
        self.model = model
        self.cfg = llm_cfg or LLMConfig()
        self.max_tokens = max_tokens
        self.calls = 0

    # --- public ---------------------------------------------------------------------------

    async def structure(self, extracted: Extracted, context: KnownContext) -> Structured:
        system = _prompt(
            "structure_chat.md" if extracted.kind == "chat-digest" else "structure_source.md"
        )
        header = self._header(extracted)
        known = self._known_block(context)
        text = extracted.text
        if len(text) <= self.cfg.single_call_max_chars:
            out, usage = await self._call(
                system, f"{header}\n{known}\n<content>\n{text}\n</content>"
            )
            return Structured(output=out, model=self.model, **usage)

        chunks = chunk_text(text, self.cfg.chunk_chars, self.cfg.chunk_overlap_chars)
        log.info("long input (%d chars): %d chunks", len(text), len(chunks))
        partials: list[StructuredOutput] = []
        tot = {"input_tokens": 0, "output_tokens": 0}
        for i, c in enumerate(chunks, 1):
            note = f"<note>This is part {i} of {len(chunks)} of a longer source.</note>"
            out, usage = await self._call(
                system, f"{header}\n{known}\n{note}\n<content>\n{c}\n</content>"
            )
            partials.append(out)
            for k in tot:
                tot[k] += usage[k]
        parts_json = "\n".join(
            f'<partial index="{i}">\n{p.model_dump_json()}\n</partial>'
            for i, p in enumerate(partials, 1)
        )
        out, usage = await self._call(
            _prompt("merge_partials.md"), f"{header}\n{known}\n{parts_json}"
        )
        for k in tot:
            tot[k] += usage[k]
        return Structured(output=out, model=self.model, **tot)

    async def describe_image(self, path: Path) -> str:
        media = IMAGE_MEDIA.get(path.suffix.lower())
        if not media:
            raise StructureError(f"unsupported image type {path.suffix}")
        data = base64.b64encode(path.read_bytes()).decode()
        return await self._text_call(
            [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
                {
                    "type": "text",
                    "text": "Describe this image for a knowledge base. First a one-line title, then a factual description, then, under 'Text:', every piece of visible text transcribed exactly (tables as markdown).",
                },
            ]
        )

    async def read_pdf(self, path: Path) -> str:
        data = base64.b64encode(path.read_bytes()).decode()
        return await self._text_call(
            [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                },
                {
                    "type": "text",
                    "text": "Transcribe this document to clean markdown, preserving headings, lists and tables. Mark each page with '--- page N ---'. Do not summarize; reproduce the text.",
                },
            ]
        )

    # --- internals ----------------------------------------------------------------------------

    async def _call(self, system: str, user: str) -> tuple[StructuredOutput, dict]:
        self.calls += 1
        msg = await self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=StructuredOutput,
        )
        if msg.stop_reason == "refusal":
            raise StructureError("model refused to process this source")
        if msg.stop_reason == "max_tokens":
            raise StructureError("structured output truncated (max_tokens)")
        if msg.parsed_output is None:
            raise StructureError("model returned no structured output")
        usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
        return msg.parsed_output, usage

    async def _text_call(self, content: list[dict]) -> str:
        self.calls += 1
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        if msg.stop_reason == "refusal":
            raise StructureError("model refused to read this file")
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    @staticmethod
    def _header(extracted: Extracted) -> str:
        item = extracted.item
        lines = [f"kind: {extracted.kind}", f"source: {item.original_name}"]
        if item.discord:
            lines.append(f"channel: #{item.discord.channel_name}")
            lines.append(f"posted_by: {item.discord.author_name}")
            lines.append(f"posted_at: {item.discord.created_at.isoformat()}")
        if item.day:
            lines.append(f"day: {item.day}")
        if extracted.duration_s:
            lines.append(f"duration_minutes: {extracted.duration_s / 60:.1f}")
        if extracted.meta.get("speakers"):
            lines.append(f"diarized_speakers: {extracted.meta['speakers']}")
        return "<metadata>\n" + "\n".join(lines) + "\n</metadata>"

    @staticmethod
    def _known_block(ctx: KnownContext) -> str:
        ents = ctx.entities[:MAX_KNOWN_ENTITIES]
        e_lines = [
            f"- {e.type}: {e.name}" + (f" (aliases: {', '.join(e.aliases)})" if e.aliases else "")
            for e in ents
        ]
        d_lines = [f"- {d.title}" for d in ctx.decisions[:MAX_KNOWN_DECISIONS]]
        return (
            "<known_entities>\n" + ("\n".join(e_lines) or "(none yet)") + "\n</known_entities>\n"
            "<known_decisions>\n" + ("\n".join(d_lines) or "(none yet)") + "\n</known_decisions>"
        )


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split on line boundaries into chunks of about `size` chars with `overlap` chars carried over."""
    if len(text) <= size:
        return [text]
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        if cur_len + len(ln) > size and cur:
            chunks.append("".join(cur))
            # carry the tail as overlap
            tail: list[str] = []
            tlen = 0
            for prev in reversed(cur):
                if tlen + len(prev) > overlap:
                    break
                tail.insert(0, prev)
                tlen += len(prev)
            cur, cur_len = tail, tlen
        cur.append(ln)
        cur_len += len(ln)
    if cur:
        chunks.append("".join(cur))
    return chunks
