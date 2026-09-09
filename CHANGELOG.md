# Changelog

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
