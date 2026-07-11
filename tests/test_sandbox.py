"""Sandbox runner unit behavior: token accounting and engine selection."""

import pytest

from agent import sandbox
from agent.sandbox import PoisonError, _parse_tokens, engine_cmd


def test_parse_tokens_plain():
    assert _parse_tokens("Tokens: 1200 sent, 300 received.") == 1500


def test_parse_tokens_k_suffix():
    assert _parse_tokens("Tokens: 4.2k sent, 1.1k received.") == 5300


def test_parse_tokens_multiple_turns():
    out = "Tokens: 1k sent, 1k received.\n...\nTokens: 2k sent, 500 received."
    assert _parse_tokens(out) == 4500


def test_parse_tokens_absent():
    assert _parse_tokens("no token line here") == 0


def test_engine_default_is_aider():
    cmd = engine_cmd("do the thing")
    assert cmd[0] == "aider"
    assert cmd[-1] == "do the thing"
    assert "--architect" in cmd


def test_engine_nano_claude_code(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "nano-claude-code")
    assert engine_cmd("fix it") == ["nano-claude-code", "--prompt", "fix it"]


def test_engine_custom_template(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE_CMD", "mytool --run {message} --json")
    assert engine_cmd("a b c") == ["mytool", "--run", "a b c", "--json"]


def test_engine_unknown_is_poison(monkeypatch):
    monkeypatch.setattr(sandbox, "ENGINE", "gpt-magic")
    with pytest.raises(PoisonError):
        engine_cmd("x")
