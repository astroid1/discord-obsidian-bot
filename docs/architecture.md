# Architecture

```
                  ┌──────────────┐
 Discord gateway ─┤  bot.py      ├─ on_message (watched channels): attachments + links
                  │  slash cmds  ├─ /backfill /sync  ──► chat.py ChatArchiver ─► state.db messages
 inbox/ folder ───┤  inbox loop  │                                 └─► chat/<ch>/<day>.md + digest jobs
                  └──────┬───────┘
                         │ IngestItem
                  ┌──────▼───────┐   one worker, priority 0 (drops) before 5 (digests)
                  │  jobs.py     │   retries transient errors, requeues in-flight jobs on restart
                  └──────┬───────┘
                  ┌──────▼───────┐
                  │ pipeline.py  │  fetch ─► dedupe (sha256 / source_ref) ─► extract ─► structure ─► write
                  └──┬──┬──┬──┬──┘
      fetch.py ◄─────┘  │  │  └────► vault.py   VaultWriter: notes, entity pages, Home.md, git
      extract*.py ◄─────┘  └───────► structure_claude.py   Claude structured outputs (+ chunk/merge)
        └─ transcribe.py  faster-whisper (+ pyannote)
```

## Modules

| module | responsibility |
|---|---|
| `config.py` | `Settings` (env / `.env`: secrets, paths) and `BotConfig` (`config.yaml`: IDs, tunables) |
| `models.py` | pydantic data objects between stages and the LLM output schema (`StructuredOutput`) |
| `state.py` | SQLite: ingest ledger, chat cursors, archived messages, chat-day bookkeeping |
| `jobs.py` | `JobQueue`: asyncio priority queue with a single worker; sinks for progress/done/failed |
| `pipeline.py` | the stage sequence and ledger status transitions; the only place that knows the order |
| `extract.py` | extractor registry by extension; text extractor |
| `extract_docs.py` | PDF (PyMuPDF, Claude fallback for scans), DOCX, images (Claude vision) |
| `extract_media.py` | ffmpeg → 16 kHz wav → `Transcriber`; memo vs recording |
| `transcribe.py` | `Transcriber` protocol, `FasterWhisperTranscriber` (lazy GPU singleton, optional pyannote), `FakeTranscriber` |
| `structure.py` | `Structurer` protocol, known-entity context types, `FakeStructurer` |
| `structure_claude.py` | `ClaudeStructurer`: prompts, known-entity block, `messages.parse`, map-reduce |
| `vault.py` | `VaultWriter`: paths/slugs, note rendering, entity resolution and append-only mentions, `Home.md`, git |
| `fetch.py` | downloaders (Discord CDN, yt-dlp, HTTP, Google Docs export), URL classification |
| `chat.py` | day-log rendering, chat-day extractor, digest rules, Discord history walker |
| `bot.py` | discord.py client: intents, reactions/replies, slash commands, inbox and sync loops |
| `__main__.py` | CLI and the one place real providers are constructed |

## Data flow

1. A source creates an `IngestItem` (`source`, `original_name`, `source_ref`, `url` or `local_path`,
   optional `DiscordRef`, `priority`, `force`) and submits it. The ledger row is written at submit so
   a crash mid-job is re-queued on restart.
2. `Pipeline.run`: fetch (fills `local_path`, `sha256`), dedupe against the ledger (`skipped` if the
   hash or canonical URL was already ingested and `force` is false), extract to `Extracted`
   (`kind`, `text`, `segments`, `duration_s`, …), structure to `StructuredOutput`, write to the vault,
   mark `done`.
3. `VaultWriter.write`: resolve entities against the vault, create or append pages, create or append
   decisions, render the source note, append mentions, regenerate `Home.md`, commit.
4. Chat: the archiver walks `channel.history(after=cursor, oldest_first=True)` for each archived
   channel and its threads, stores messages in SQLite (day computed in the configured timezone),
   renders the touched day logs and enqueues digest jobs for closed days. A digest is a normal
   pipeline run whose extractor reads the day's messages from SQLite.

## Provider interfaces

```python
class Transcriber(Protocol):
    async def transcribe(self, wav: Path, *, progress=None) -> Transcript: ...


class Structurer(Protocol):
    async def structure(self, extracted: Extracted, context: KnownContext) -> Structured: ...
    async def describe_image(self, path: Path) -> str: ...
    async def read_pdf(self, path: Path) -> str: ...


class Extractor(Protocol):
    async def extract(self, item: IngestItem, progress) -> Extracted: ...


class Fetcher(Protocol):
    async def fetch(self, item: IngestItem, progress) -> IngestItem: ...
```

To add, say, a Deepgram transcriber: implement `Transcriber` in a new module, construct it in
`build_providers()` in `__main__.py` behind a setting, done. The pipeline, vault and bot do not change.

## Idempotency and safety

- Ledger key is the content sha256 (URLs are also checked by canonical id before download).
- Same content re-dropped → `skipped`; `/reingest` sets `force` and overwrites the source note; entity
  mentions dedupe by link so re-runs never duplicate.
- Entity/decision pages: only appends and alias union. Source notes are the only files rewritten.
- One worker: GPU, LLM rate limits and git are never contended.
- Deterministic failures (unsupported type, empty text, model refusal) fail immediately with the
  reason; transient ones (network, 429/5xx) retry after 15 s and 60 s.

## Testing

`pytest` runs without GPU or network: `FakeTranscriber`, `FakeStructurer`, a fake Anthropic client for
chunking, and the real `VaultWriter`, `State`, extractors and chat renderers against temp dirs.
`scripts/smoke.py` runs the real pipeline on one file or URL into a scratch vault.
