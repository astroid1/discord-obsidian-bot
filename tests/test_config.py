from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from dob.config import BotConfig, load_bot_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_parses():
    cfg = load_bot_config(ROOT / "config.example.yaml")
    assert cfg.guild_id > 0
    assert 123456789012345678 in cfg.people
    assert cfg.people[123456789012345678] == "Jordan Blake"
    assert cfg.tz.key == "America/New_York"


def test_example_env_has_no_secrets():
    text = (ROOT / ".env.example").read_text()
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            if key.strip() in (
                "DISCORD_TOKEN",
                "ANTHROPIC_API_KEY",
                "HF_TOKEN",
                "VAULT_GIT_REMOTE_URL",
            ):
                assert val.strip() == "", f"{key} must be empty in .env.example"


def test_missing_guild_fails():
    with pytest.raises(ValidationError):
        BotConfig.model_validate({"watched_channels": [1]})


def test_bad_timezone_fails():
    with pytest.raises(ValidationError):
        BotConfig(guild_id=1, timezone="Mars/Olympus")


def test_url_hosts_normalised(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"guild_id": 1, "url_hosts": ["WWW.YouTube.com"]}))
    assert load_bot_config(p).url_hosts == ["youtube.com"]


def test_missing_file_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        load_bot_config(tmp_path / "nope.yaml")
