# discord-obsidian-bot

Drop audio, video, documents, images or links into Discord, or just talk in your channels, and get a
linked Obsidian knowledge base: one note per source, plus auto-maintained pages for the people,
projects, topics and decisions they mention, all wired together with `[[wikilinks]]`.

- **Transcription runs locally** on your GPU with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (optional speaker diarization with pyannote). Audio never leaves your machine.
- **Structuring uses Claude** (Anthropic API) with strict JSON output: summary, key points, decisions,
  action items, entities.
- **Chat history is archived too**: verbatim daily logs per channel plus an LLM digest per day, feeding
  the same people/projects/topics graph.
- Writes straight into a vault folder on disk and can commit + push it to git after every ingest.

```
#drops  ─ file / link ──┐
inbox/  ─ big files ────┼─► fetch ─► extract ─► Claude ─► vault/sources/…  ─┐
#general chat history ──┘   (whisper, PyMuPDF, docx, vision)               ├─► people/ projects/ topics/ decisions/
                                                                            └─► Home.md, git commit
```

## What you get in the vault

```
company-kb/
├── Home.md                              generated: counts, recent sources, decisions, open action items
├── sources/recordings/2026-09-08 weekly-sync.md
├── sources/memos/…   sources/documents/…   sources/chat-digests/<channel>/<day>.md
├── chat/<channel>/2026-09-08.md         verbatim daily chat logs
├── people/Jordan Blake.md  projects/…  topics/…   one page per entity, append-only "Mentions"
├── decisions/2026-09-08 use-postgres-for-crm.md
└── attachments/                         small originals (PDFs, docs) kept next to their notes
```

Every source note has frontmatter (type, date, participants, duration, tags, Discord link, sha256), a
summary, key points, decisions, `- [ ]` action items with owners, entity links, and the full
transcript or extracted text. See [docs/vault-format.md](docs/vault-format.md).

## Requirements

- Docker Desktop with the WSL2 backend and a recent NVIDIA driver (GPU passthrough is built in), **or**
  Python 3.12+ with ffmpeg on PATH for a native run.
- An [Anthropic API key](https://console.anthropic.com/).
- Optional: a Hugging Face token with the terms accepted for
  [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
  if you want "who said what" on recordings.
- A Discord server you administer.

Without a GPU, faster-whisper falls back to CPU (slow but works). Without a Hugging Face token,
transcripts have no speaker labels.

## Setup

### 1. Create the Discord application

Follow [docs/setup-discord.md](docs/setup-discord.md). Short version:

1. https://discord.com/developers/applications → **New Application** → **Bot** → **Reset Token** (copy it).
2. Under **Privileged Gateway Intents** enable **Message Content**.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions *View Channels,
   Send Messages, Send Messages in Threads, Read Message History, Add Reactions, Embed Links*.
   Open the generated URL and add the bot to your server.
4. Turn on **Developer Mode** (User Settings → Advanced) so right-click → **Copy ID** works on the
   server, channels and users.

### 2. Configure

```bash
git clone https://github.com/astroid1/discord-obsidian-bot
cd discord-obsidian-bot
cp .env.example .env                # secrets + paths
cp config.example.yaml config.yaml  # server / channel / people IDs
```

`.env` (never committed):

| key | what |
|---|---|
| `DISCORD_TOKEN` | the bot token |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude key and model id (default `claude-sonnet-5`) |
| `HF_TOKEN` | optional, enables diarization |
| `VAULT_HOST_PATH` | your vault folder on the host, e.g. `C:/Users/you/Vaults/company-kb` |
| `VAULT_GIT_COMMIT`, `VAULT_GIT_PUSH`, `VAULT_GIT_REMOTE_URL` | commit after each ingest; push using an HTTPS remote with a token |
| `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` | `large-v3` / `auto` / `int8_float16` fit a 12 GB card with room to spare |

`config.yaml` (also not committed, it holds your IDs):

| key | what |
|---|---|
| `guild_id` | the one server the bot serves |
| `watched_channels` | channels where dropped files and links get ingested |
| `archived_channels` | channels whose chat history is archived and digested |
| `allowed_user_ids`, `allowed_role_ids` | who may run slash commands (admins always can) |
| `people` | Discord user id → canonical name, so chat authors become `[[Name]]` links |
| `url_hosts` | link hosts the bot downloads from (YouTube, Loom, Google Drive/Docs by default) |
| `timezone` | day boundaries for chat logs |
| `sync_interval_hours`, `digest_min_messages`, `digest_regen_delta` | chat archiving cadence |
| `memo_max_minutes` | audio shorter than this is a memo, longer is a recording |
| `llm.single_call_max_chars` | above this, long transcripts are chunked and merged |

### 3. Run

**Docker (recommended):**

```bash
mkdir -p "$VAULT_HOST_PATH"   # or create the folder in Obsidian and open it once
docker compose up -d --build   # first build downloads the CUDA image and torch (several GB)
docker compose logs -f bot     # wait for "logged in as …"
```

**Native (development):**

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                          # + ".[gpu]" for faster-whisper/pyannote
python -m dob run
```

Then drop a `.md` or `.pdf` into a watched channel. The bot reacts ⏳, replies with progress, and
swaps to ✅ with the title, a two-line summary and the note path. Re-dropping the same file is
skipped (🔁) because ingestion is idempotent by content hash.

### 4. Archive chat history

Run `/backfill` once (optionally `channel:` and `since:YYYY-MM-DD`) to walk the full history of every
archived channel, threads included. After that the bot syncs new messages every
`sync_interval_hours` and on startup; `/sync` forces one. Each closed day with at least
`digest_min_messages` messages gets an LLM digest note linked from its raw log.

## Using it

| way in | what happens |
|---|---|
| attachment in a watched channel | audio/video → transcript; pdf/docx/md/txt/csv/json → text; png/jpg/webp → Claude vision description + OCR |
| link in a watched channel | YouTube / Loom / Google Drive via yt-dlp, Google Docs/Sheets/Slides via export, or any direct link to a supported file type |
| file in `inbox/` | same pipeline, bypasses Discord's upload limit; moved to `inbox/done/` or `inbox/failed/` |
| `/ingest url:` | ingest a link from anywhere |
| `/retry link:` / `/reingest link:` | rerun a failed drop, or force a re-run of a done one |
| `/status` | queue, ledger counts, archive cursors |
| `/backfill`, `/sync` | chat archiving |

The pipeline runs one job at a time so the GPU is never shared; drops are processed before
digests. Failed jobs get ❌ with the reason; transient network/API errors retry twice.

Try it without Discord:

```bash
python scripts/smoke.py path/to/file.m4a --vault ./tmp-vault            # real providers from .env
python scripts/smoke.py tests/fixtures/sample.md --vault ./tmp-vault --fake-llm --fake-whisper
```

## How entity pages stay sane

- Claude is handed the list of known people/projects/topics/decisions and told to reuse exact names.
- Code is the final arbiter: a mention resolves to an existing page by canonical name or alias
  (case-insensitive) within the same type. Aliases are unioned into the page's `aliases:` frontmatter,
  which Obsidian uses natively for link resolution.
- An existing page is only ever *appended to* (a bullet under `## Mentions`, deduplicated by source
  link). Its body text is never rewritten, so your hand edits survive.
- Decisions that reaffirm a known decision are appended to it rather than duplicated.

## Security notes for running in the open

- Secrets live only in `.env`; server IDs only in `config.yaml`; both are gitignored and CI runs
  gitleaks. `pre-commit install` adds the same scan locally.
- The bot only reads the configured guild and channels and only accepts commands from the
  allowlist (or server admins).
- Attachment CDN links expire in ~24 h; the bot downloads immediately and never stores the URL.
- Files from Discord are treated as untrusted input: they are parsed by PyMuPDF/python-docx/ffmpeg,
  never executed.

## Development

```bash
make venv          # or the pip commands above
make test          # pytest, no GPU or network needed (fake providers)
make lint          # ruff
make smoke FILE=tests/fixtures/sample.md ARGS="--fake-llm --fake-whisper"
```

Layout and the provider interfaces are described in [docs/architecture.md](docs/architecture.md).
Adding a transcription or LLM backend means implementing one small protocol and wiring it in
`src/dob/__main__.py`.

## License

MIT.
