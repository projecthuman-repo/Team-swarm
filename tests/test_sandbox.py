"""Sandbox runner unit behavior: token accounting and branch discipline."""

from agent.sandbox import _parse_tokens


def test_parse_tokens_plain():
    assert _parse_tokens("Tokens: 1200 sent, 300 received.") == 1500


def test_parse_tokens_k_suffix():
    assert _parse_tokens("Tokens: 4.2k sent, 1.1k received.") == 5300


def test_parse_tokens_multiple_turns():
    out = "Tokens: 1k sent, 1k received.\n...\nTokens: 2k sent, 500 received."
    assert _parse_tokens(out) == 4500


def test_parse_tokens_absent():
    assert _parse_tokens("no token line here") == 0
