from __future__ import annotations

import os
from pathlib import Path

import pytest

from memesnipe.config import ARM_ENV, ARM_VALUE, Config, ConfigError, load_config
from memesnipe.models import Chain

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_loads():
    config = load_config(REPO_CONFIG)
    assert config.twitter.accounts
    assert config.execution.mode == "paper"


def test_chain_is_coerced_to_the_enum(tmp_path: Path):
    # `from __future__ import annotations` turns field types into strings, so
    # a loader that reflects over them silently leaves this as str - and then
    # every `is Chain.SOLANA` check downstream is wrong.
    config = load_config(write(tmp_path, '[discovery]\nchain = "solana"\n'))
    assert config.discovery.chain is Chain.SOLANA


def test_unsupported_chain_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="unsupported chain"):
        load_config(write(tmp_path, '[discovery]\nchain = "dogechain"\n'))


def test_typo_in_a_key_is_an_error_not_a_silent_default(tmp_path: Path):
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(write(tmp_path, "[risk]\nmin_liquidty_usd = 10.0\n"))


def test_unknown_section_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="unknown config section"):
        load_config(write(tmp_path, "[nonsense]\nx = 1\n"))


def test_ladder_is_parsed_as_pairs(tmp_path: Path):
    config = load_config(
        write(tmp_path, "[exits]\ntake_profit_ladder = [[3.0, 0.4], [8.0, 0.5]]\n")
    )
    assert config.exits.take_profit_ladder == [(3.0, 0.4), (8.0, 0.5)]


def test_paper_mode_never_needs_the_arm():
    Config().require_arm()  # must not raise


def test_live_mode_refuses_without_the_arm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ARM_ENV, raising=False)
    config = Config()
    config.execution.mode = "live"
    with pytest.raises(ConfigError, match=ARM_ENV):
        config.require_arm()


def test_live_mode_still_needs_an_rpc_and_a_wallet(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ARM_ENV, ARM_VALUE)
    monkeypatch.delenv("MEMESNIPE_WALLET_KEY", raising=False)
    config = Config()
    config.execution.mode = "live"
    with pytest.raises(ConfigError, match="rpc_url"):
        config.require_arm()

    config.execution.rpc_url = "https://rpc.example"
    with pytest.raises(ConfigError, match="MEMESNIPE_WALLET_KEY"):
        config.require_arm()

    monkeypatch.setenv("MEMESNIPE_WALLET_KEY", "x")
    config.require_arm()


def test_a_near_miss_arm_value_does_not_count(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ARM_ENV, "yes")
    config = Config()
    config.execution.mode = "live"
    with pytest.raises(ConfigError):
        config.require_arm()
    assert os.environ[ARM_ENV] == "yes"
