# Changelog

## Unreleased

- Heartbeat loop plus `watchdog/`, a Cloudflare Worker dead-man's switch that alerts a Discord
  webhook when the bot goes silent and again when it recovers.
- Compose project name pinned so Docker volume names no longer depend on the folder name.
- `/record start|stop|status`: capture a voice channel with discord-ext-voice-recv, one PCM track
  per speaker on a shared timeline, DAVE (end-to-end encrypted voice) frames decrypted through the
  connection's davey session. Each track is transcribed alone and merged, so speaker labels are
  real names. Auto-stops when the channel empties or after `meetings.max_minutes`.
- Suggested tasks: action items from every new note become cards in the tasks channel that are
  accepted with ✅ or dismissed with ❌; owners map to Discord users via `people`, and a note never
  suggests the same task twice.
- New `voice` extra; the Docker image installs it plus libopus.
- `/task` group (`add`, `done`, `update`, `list`, `mine`): to-dos with owners and due dates in a
  `tasks` table. Cards post to `tasks.channel_id`, a pinned board stays current, a ✅ reaction
  closes a card, a weekday-morning reminder lists overdue and due-today items, and the vault gets
  a generated `tasks/Tasks.md`. Open tasks are appended to the weekly digest.
- `/ask`: keyword retrieval over the vault plus a Claude answer that cites the notes it used.
- `/decide`: records a decision as a card in the decisions channel and a page in `decisions/`;
  the same title again appends a "Reaffirmed" mention instead of a duplicate page.
- `/digest` and a scheduled week-in-review post (`weekly_digest` in config.yaml) built from
  frontmatter dates, written up by Claude when a key is set.
- New config: `decisions_channel_id`, `ask`, `weekly_digest`. New `kv` table in the state db.

## 0.1.0 — 2026-09-08

Initial release.

- Ingest Discord attachments (audio, video, pdf, docx, md/txt/csv/json, images) and links (YouTube,
  Loom, Google Drive/Docs, direct files) from watched channels, plus a local `inbox/` folder.
- Local transcription with faster-whisper; optional pyannote diarization.
- Claude structured outputs: summary, key points, decisions, action items, entities; map-reduce for
  long transcripts.
- Obsidian vault writer: source notes, append-only people/projects/topics/decision pages with alias
  resolution, generated Home.md, optional git commit + push.
- Chat archiving: `/backfill` and scheduled sync into verbatim daily logs and per-day LLM digests.
- Slash commands: `/status`, `/backfill`, `/sync`, `/ingest`, `/retry`, `/reingest`.
- Docker Compose with NVIDIA GPU passthrough; CI with ruff, pytest and gitleaks.
