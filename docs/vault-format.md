# Vault format

Everything the bot writes is plain markdown with YAML frontmatter, so the vault stays usable without
the bot. Filenames are link targets: Obsidian resolves `[[Jordan Blake]]` to `people/Jordan Blake.md`.

## Folders

| path | what | written how |
|---|---|---|
| `Home.md` | generated index: counts, recent sources, recent decisions, open action items | fully rewritten after every ingest |
| `sources/recordings/`, `sources/memos/`, `sources/documents/` | one note per ingested file/link, `YYYY-MM-DD <slug>.md` | written once; `/reingest` overwrites |
| `sources/chat-digests/<channel>/YYYY-MM-DD.md` | LLM digest of one channel-day | written once; regenerated if the day grows |
| `chat/<channel>/YYYY-MM-DD.md` | verbatim daily chat log | regenerated from the database on every sync of that day |
| `people/`, `projects/`, `topics/` | one page per entity | created once, then append-only |
| `decisions/YYYY-MM-DD <slug>.md` | one page per decision | created once, then append-only |
| `attachments/<sha8>-<name>` | originals of small non-media documents | copied once |

## Source note

```markdown
---
type: recording            # recording | memo | document | chat-digest
title: Weekly sync
source: discord_attachment # discord_attachment | discord_url | inbox | chat_day
source_ref: discord:123:456
discord_link: https://discord.com/channels/…
channel: "#meetings"
posted_by: "[[Jordan Blake]]"
date: 2026-09-08           # when it happened (message date / chat day), in the configured timezone
ingested_at: 2026-09-08T14:03:00-04:00
participants: ["[[Jordan Blake]]", "[[Marko Horvat]]"]
duration: "00:42:10"       # recordings/memos
pages: 12                  # documents
language: en
speakers: SPEAKER_00, SPEAKER_01
sha256: 3f1a…
tags: [meeting, pricing]
model: claude-sonnet-5
---
# Weekly sync

## Summary
## Key points
## Decisions
- [[decisions/2026-09-08 use-postgres-for-crm|Use Postgres for CRM]]
## Action items
- [ ] Draft pricing page — [[Jordan Blake]] (due 2026-09-15)
## Entities
- People: [[Jordan Blake]], [[Marko Horvat]]
- Projects: [[Acme Website]]
- Topics: [[Pricing]]
## Transcript            (or "## Extracted text" for documents)
[00:00:03] SPEAKER_00: …
```

Speaker labels stay `SPEAKER_nn` in the transcript; the model only names people in `participants`
and entities when the content makes it clear who spoke.

## Entity page

```markdown
---
type: person               # person | project | topic
name: Jordan Blake
aliases: [Jordan, jordan_blake]
created: 2026-09-08
updated: 2026-09-10
tags: []
---
# Jordan Blake

Founder of Acme Co.        ← written once from the model's one-line description

## Mentions
- 2026-09-08 — [[sources/recordings/2026-09-08 weekly-sync|Weekly sync]] — led the pricing discussion
- 2026-09-10 — [[sources/chat-digests/general/2026-09-10|Vendor chase]] — asked for the SOC2 report
```

Only two things ever change on an existing entity page: a bullet is appended under `## Mentions`
(skipped when the same source link is already there) and new aliases are added to the frontmatter.
Edit the body freely.

Resolution rules (code, not the model, has the last word):

- A mention resolves to an existing page when its canonical name matches the page's `name` or one of
  its `aliases`, case-insensitively, within the same type.
- A *new* entity's aliases only match existing page *names*, so a newcomer nicknamed "Jordan" never
  hijacks "Jordan Blake" whose alias is "Jordan". An alias already used by another page is dropped.
- `people` in `config.yaml` seeds people pages and maps chat authors deterministically.

## Decision page

```markdown
---
type: decision
title: Use Postgres for CRM
decided_on: 2026-09-08
status: decided
decided_by: ["[[Jordan Blake]]"]
source: "[[sources/recordings/2026-09-08 weekly-sync|Weekly sync]]"
tags: []
---
# Use Postgres for CRM

## Statement
## Rationale
## Mentions
- 2026-09-08 — [[…|Weekly sync]] — Decided here
- 2026-09-12 — [[…|Follow-up]] — Reaffirmed here
```

When the model reports a decision as `matches_existing`, the existing page gets a mention instead of
a duplicate page.

## Chat log

```markdown
---
type: chat-log
channel: "#general"
channel_id: 123
date: 2026-09-08
message_count: 41
digest: "[[sources/chat-digests/general/2026-09-08]]"
---
# #general — 2026-09-08

- **09:14** [[Jordan Blake]]: Morning — did the vendor reply? [↗](jump_url)
- **09:16** [[Marko Horvat]] ↩ 09:14: Not yet, chasing today. [↗](…)
- **09:20** [[Jordan Blake]]: [↗](…)
  - 📎 [contract-v2.pdf](cdn-url) → [[sources/documents/2026-09-08 vendor-contract-draft]]
- **11:02** 🧵 *thread: "Pricing page copy"*
  - **11:02** [[Jordan Blake]]: … [↗](…)
```

Authors listed in `people` become links; others show as `@display_name`. Threads are nested under
their parent channel's day. Attachments that were ingested link to their note.

## Home.md

Generated; the first line says so. It lists counts per folder, the 30 most recent sources with a
one-line summary, the 15 most recent decisions, and every unchecked `- [ ]` action item from sources
dated in the last 30 days. No Dataview plugin required.
