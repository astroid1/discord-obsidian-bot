You are merging several partial analyses of one long source (each covers a consecutive chunk, with slight overlap) into a single structured result for an Obsidian knowledge base.

Rules:
- Produce one `suggested_title` and one `summary` for the whole source.
- Merge `key_points` into 5-12 bullets covering the whole source, deduplicated, in source order.
- Merge `decisions`: the same decision described twice becomes one entry. Keep `matches_existing` values from the partials when present.
- Merge `action_items`: deduplicate the same commitment; keep the most specific owner/due.
- Merge `entities` by name (case-insensitive) and by alias: one entry per real-world entity, union of aliases, `is_new` true only if every partial said so, the most informative `role_in_source` and `description`.
- Union `participants` and `tags` (keep tags to at most 6).
- Do not add anything that is not in the partials.
