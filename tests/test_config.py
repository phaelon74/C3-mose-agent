"""Tests for config loading and env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from mose.config import load_config


def test_llm_env_overrides_when_config_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "no_config.toml"
    monkeypatch.setenv("LLM_ENDPOINT", "http://llm.test:9/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.25")
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "4096")
    monkeypatch.setenv("LLM_API_KEY", "sekret")
    monkeypatch.setenv("LLM_PROVIDER", "vllm")

    cfg = load_config(missing)

    assert cfg.llm.endpoint == "http://llm.test:9/v1"
    assert cfg.llm.model == "test-model"
    assert cfg.llm.max_tokens == 2048
    assert cfg.llm.temperature == 0.25
    assert cfg.llm.context_window == 4096
    assert cfg.llm.api_key == "sekret"
    assert cfg.llm.provider == "vllm"


def test_llm_temperature_zero_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "no_config.toml"
    monkeypatch.setenv("LLM_TEMPERATURE", "0")

    cfg = load_config(missing)

    assert cfg.llm.temperature == 0.0


def test_llm_env_empty_string_skips_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Blank env must not crash int()/float() and must not override TOML."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[llm]\nendpoint = "http://from-toml:1/v1"\nmodel = "from-toml"\n'
        "max_tokens = 100\ntemperature = 0.5\ncontext_window = 200\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MAX_TOKENS", "")
    monkeypatch.setenv("LLM_TEMPERATURE", "   ")
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "")

    cfg = load_config(cfg_path)

    assert cfg.llm.max_tokens == 100
    assert cfg.llm.temperature == 0.5
    assert cfg.llm.context_window == 200
