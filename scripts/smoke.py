"""Run the pipeline on one local file or URL without Discord.

    python scripts/smoke.py path/or/url --vault ./tmp-vault [--fake-llm] [--fake-whisper] [--no-git]

Uses your .env for API keys unless --fake-* is given. Prints the note path and what was linked.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--vault", default="./tmp-vault")
    ap.add_argument("--data", default=None, help="state/cache dir (default: <vault>/.dob)")
    ap.add_argument("--fake-llm", action="store_true")
    ap.add_argument("--fake-whisper", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    os.environ["VAULT_PATH"] = args.vault
    os.environ["DATA_PATH"] = args.data or str(Path(args.vault) / ".dob")
    if args.no_git:
        os.environ["VAULT_GIT_COMMIT"] = "false"

    from dob.__main__ import build_providers
    from dob.config import load_bot_config, load_settings, setup_logging
    from dob.jobs import LogSink
    from dob.models import INTERACTIVE, IngestItem

    settings = load_settings()
    setup_logging(settings.log_level)
    cfg = load_bot_config(settings.config_path) if settings.config_path.exists() else None
    pipeline = build_providers(
        settings, cfg, fake_llm=args.fake_llm, fake_whisper=args.fake_whisper
    )

    src = args.source
    if src.startswith(("http://", "https://")):
        item = IngestItem(
            source="discord_url",
            original_name=src,
            source_ref=src,
            url=src,
            priority=INTERACTIVE,
            force=args.force,
        )
    else:
        p = Path(src).resolve()
        item = IngestItem(
            source="inbox",
            original_name=p.name,
            source_ref=f"inbox:{p.name}",
            local_path=p,
            priority=INTERACTIVE,
            force=args.force,
        )

    result = await pipeline.run(item, LogSink(item.original_name))
    print()
    print("status :", result.status)
    if result.note:
        n = result.note
        print("note   :", n.note_path)
        print("created:", n.entities_created, n.decisions_created)
        print("updated:", n.entities_updated)
        print("commit :", n.commit_sha)
    else:
        print("message:", result.message)
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
