"""LLM layer: provider fallback chain + reasoning-model handling (offline)."""
from __future__ import annotations

import types

import pytest

from jobcopilot import llm


class _FakeCompletions:
    def __init__(self, content: str, finish_reason: str = "stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = types.SimpleNamespace(content=self.content)
        choice = types.SimpleNamespace(message=msg, finish_reason=self.finish_reason)
        return types.SimpleNamespace(choices=[choice])


def _install_fake_openai(monkeypatch, completions: _FakeCompletions) -> None:
    import openai

    class FakeOpenAI:
        def __init__(self, **_kw):
            self.chat = types.SimpleNamespace(completions=completions)

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(llm, "resolve_model", lambda provider, configured: configured.split(",")[0])


def test_provider_chain_parses_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "Groq, ollama")
    assert llm.provider_chain() == ["groq", "ollama"]
    monkeypatch.setenv("LLM_PROVIDER", "")
    assert llm.provider_chain() == ["ollama"]


def test_fallback_to_next_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq,ollama")
    seen: list[str] = []

    def fake_one(provider, *a, **k):
        seen.append(provider)
        if provider == "groq":
            raise RuntimeError("rate limited")
        return "from ollama"

    monkeypatch.setattr(llm, "_complete_one", fake_one)
    assert llm.complete("s", "u") == "from ollama"
    assert seen == ["groq", "ollama"]


def test_all_providers_fail_reports_each(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq,ollama")

    def boom(provider, *a, **k):
        raise RuntimeError(f"{provider} down")

    monkeypatch.setattr(llm, "_complete_one", boom)
    with pytest.raises(RuntimeError) as exc:
        llm.complete("s", "u")
    assert "groq: groq down" in str(exc.value) and "ollama: ollama down" in str(exc.value)


def test_single_provider_error_is_raised_unwrapped(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setattr(llm, "_complete_one", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad key")))
    with pytest.raises(ValueError, match="bad key"):
        llm.complete("s", "u")


def test_reasoning_model_gets_token_floor_and_effort(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    monkeypatch.delenv("GROQ_REASONING_EFFORT", raising=False)
    fake = _FakeCompletions("OK")
    _install_fake_openai(monkeypatch, fake)

    assert llm.complete("s", "u", max_tokens=20) == "OK"
    call = fake.calls[0]
    assert call["max_tokens"] >= 512
    assert call["reasoning_effort"] == "low"


def test_non_reasoning_model_untouched(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    fake = _FakeCompletions("hi")
    _install_fake_openai(monkeypatch, fake)

    assert llm.complete("s", "u", max_tokens=20) == "hi"
    call = fake.calls[0]
    assert call["max_tokens"] == 20
    assert "reasoning_effort" not in call


def test_empty_truncated_reply_is_an_error(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
    _install_fake_openai(monkeypatch, _FakeCompletions("", finish_reason="length"))
    with pytest.raises(llm.LLMTruncated):
        llm.complete("s", "u")


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY not set"):
        llm.complete("s", "u")
