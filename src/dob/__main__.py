"""CLI entry point: `dob run | ingest | backfill | sync | status`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .config import BotConfig, Settings, load_bot_config, load_settings, setup_logging

log = logging.getLogger("dob")


def build_providers(
    settings: Settings, cfg: BotConfig | None, *, fake_llm=False, fake_whisper=False
):
    """Construct the real (or fake) providers and the pipeline. The only place that does so."""
    from .extract import default_registry
    from .pipeline import Pipeline
    from .state import State
    from .structure import FakeStructurer
    from .vault import VaultWriter

    tz = cfg.tz if cfg else None
    state = State(settings.data_path / "state.db")
    vault = VaultWriter(
        settings.vault_path,
        cfg=cfg.vault if cfg else None,
        tz=tz,
        git_commit=settings.vault_git_commit,
        git_push=settings.vault_git_push,
        remote_url=settings.vault_git_remote_url,
    )
    vault.ensure_layout()

    if fake_llm or not settings.anthropic_api_key:
        if not fake_llm:
            log.warning("ANTHROPIC_API_KEY not set; using the fake structurer")
        structurer = FakeStructurer()
    else:
        from .structure_claude import ClaudeStructurer

        structurer = ClaudeStructurer(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            llm_cfg=cfg.llm if cfg else None,
        )

    transcriber = None
    if fake_whisper:
        from .transcribe import FakeTranscriber

        transcriber = FakeTranscriber()
    else:
        try:
            from .transcribe import FasterWhisperTranscriber

            transcriber = FasterWhisperTranscriber(
                model=settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                models_path=settings.models_path,
                hf_token=settings.hf_token,
                diarize=cfg.transcription.diarize if cfg else True,
                language=cfg.transcription.language if cfg else None,
            )
        except ImportError:
            log.warning("faster-whisper not installed; audio/video ingestion disabled")

    registry = default_registry(
        transcriber=transcriber, structurer=structurer, cfg=cfg, state=state, bot_cfg=cfg
    )
    from .fetch import Downloader

    fetcher = Downloader(
        cache_dir=settings.data_path / "cache", url_hosts=cfg.url_hosts if cfg else None
    )
    pipeline = Pipeline(
        state=state,
        fetcher=fetcher,
        extractors=registry,
        structurer=structurer,
        vault=vault,
        cache_dir=settings.data_path / "cache",
    )
    return pipeline


def _load(args) -> tuple[Settings, BotConfig | None]:
    settings = load_settings()
    setup_logging(settings.log_level, settings.data_path / "logs" / "bot.log")
    cfg = None
    if getattr(args, "config", None) is not False:
        try:
            cfg = load_bot_config(settings.config_path)
        except FileNotFoundError as e:
            if args.cmd == "run":
                raise SystemExit(str(e)) from None
            log.warning("%s (continuing without config.yaml)", e)
    return settings, cfg


async def cmd_ingest(args) -> int:
    from .jobs import LogSink
    from .models import INTERACTIVE, IngestItem

    settings, cfg = _load(args)
    pipeline = build_providers(
        settings, cfg, fake_llm=args.fake_llm, fake_whisper=args.fake_whisper
    )
    src = args.source
    if src.startswith(("http://", "https://")):
        item = IngestItem(
            source="discord_url", original_name=src, source_ref=src, url=src, priority=INTERACTIVE
        )
    else:
        p = Path(src).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"not found: {p}")
        item = IngestItem(
            source="inbox",
            original_name=p.name,
            source_ref=f"inbox:{p.name}",
            local_path=p,
            priority=INTERACTIVE,
            force=args.force,
        )
    result = await pipeline.run(item, LogSink(item.original_name))
    print(result.status, result.note.note_path if result.note else result.message)
    return 0 if result.status != "failed" else 1


async def cmd_run(args) -> int:
    from .bot import run_bot

    settings, cfg = _load(args)
    assert cfg is not None
    pipeline = build_providers(settings, cfg)
    await run_bot(settings, cfg, pipeline)
    return 0


async def cmd_chat(args) -> int:
    from .bot import run_oneshot

    settings, cfg = _load(args)
    assert cfg is not None
    pipeline = build_providers(settings, cfg)
    await run_oneshot(
        settings, cfg, pipeline, mode=args.cmd, channel=args.channel, since=args.since
    )
    return 0


def cmd_status(args) -> int:
    from .state import State

    settings, _ = _load(args)
    st = State(settings.data_path / "state.db")
    print("ingests:", st.counts())
    for r in st.recent(10):
        print(
            f"  {r['updated_at']}  {r['status']:<10} {r['original_name']}  {r['note_path'] or r['error'] or ''}"
        )
    print("cursors:")
    for c in st.cursors():
        print(f"  {c['name']}  last={c['last_message_id']}  synced={c['last_synced_at']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dob", description="discord-obsidian-bot")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="run the Discord bot")

    p = sub.add_parser("ingest", help="ingest a local file or URL without Discord")
    p.add_argument("source")
    p.add_argument("--force", action="store_true", help="re-ingest even if already done")
    p.add_argument("--fake-llm", action="store_true")
    p.add_argument("--fake-whisper", action="store_true")

    for name, help_ in (
        ("backfill", "walk full history of archived channels"),
        ("sync", "incremental history sync"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--channel", type=int, default=None)
        p.add_argument("--since", default=None, help="YYYY-MM-DD (backfill only)")

    sub.add_parser("status", help="show ledger and cursors")

    args = ap.parse_args(argv)
    if args.cmd == "ingest":
        return asyncio.run(cmd_ingest(args))
    if args.cmd == "run":
        return asyncio.run(cmd_run(args))
    if args.cmd in ("backfill", "sync"):
        return asyncio.run(cmd_chat(args))
    if args.cmd == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
