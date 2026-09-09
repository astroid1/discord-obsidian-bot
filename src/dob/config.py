"""Settings (secrets and paths from the environment / .env) and BotConfig (config.yaml)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_URL_HOSTS = ["youtube.com", "youtu.be", "loom.com", "drive.google.com", "docs.google.com"]


class Settings(BaseSettings):
    """Secrets and machine-specific paths. Loaded from the environment and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    hf_token: str = ""

    vault_path: Path = Path("./tmp-vault")
    data_path: Path = Path("./data")
    inbox_path: Path = Path("./inbox")
    models_path: Path = Path("./models")
    config_path: Path = Path("./config.yaml")

    vault_git_commit: bool = True
    vault_git_push: bool = False
    vault_git_remote_url: str = ""

    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8_float16"

    log_level: str = "INFO"
    inbox_poll_seconds: int = 10
    inbox_force_polling: bool = True


class LLMConfig(BaseModel):
    single_call_max_chars: int = 400_000
    chunk_chars: int = 200_000
    chunk_overlap_chars: int = 2_000


class TranscriptionConfig(BaseModel):
    language: str | None = None
    diarize: bool = True


class VaultConfig(BaseModel):
    keep_originals_max_mb: int = 25


class BotConfig(BaseModel):
    """Server-specific configuration from config.yaml. Contains IDs, never secrets."""

    guild_id: int
    watched_channels: list[int] = Field(default_factory=list)
    archived_channels: list[int] = Field(default_factory=list)
    allowed_role_ids: list[int] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    people: dict[int, str] = Field(default_factory=dict)
    url_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_URL_HOSTS))
    timezone: str = "UTC"
    sync_interval_hours: float = 6
    digest_min_messages: int = 5
    digest_regen_delta: int = 10
    memo_max_minutes: float = 5
    llm: LLMConfig = Field(default_factory=LLMConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("url_hosts")
    @classmethod
    def _norm_hosts(cls, v: list[str]) -> list[str]:
        return [h.lower().removeprefix("www.") for h in v]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_bot_config(path: Path) -> BotConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to config.yaml and fill in your IDs."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return BotConfig.model_validate(raw)


def load_settings() -> Settings:
    return Settings()


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy codepage
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), format=fmt, handlers=handlers, force=True)
    # Third-party chatter
    for noisy in ("httpx", "httpcore", "discord.gateway", "discord.client", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
