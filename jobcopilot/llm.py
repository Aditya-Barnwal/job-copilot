"""Provider-agnostic LLM module.

Default provider is **Ollama** (free, local, no key). Configured by env:

  LLM_PROVIDER = ollama | groq | gemini | anthropic, or a comma-separated
                 fallback chain such as "groq,ollama" (tried in order)
  OLLAMA_MODEL=qwen2.5:7b      (or candidates: "qwen2.5:7b,llama3.2:3b")
  GROQ_API_KEY, GROQ_MODEL     (e.g. "openai/gpt-oss-120b,qwen/qwen3.8-27b")
  GROQ_REASONING_EFFORT=low    (reasoning models only: low | medium | high)
  GEMINI_API_KEY, GEMINI_MODEL
  ANTHROPIC_API_KEY, ANTHROPIC_MODEL

This is the only place that talks to a provider SDK. Tailoring and (optional)
keyword extraction must go through `complete()` so providers stay swappable.

Includes:
  - parse_json: tolerant JSON parsing (small local models often add fences).
  - available_models / resolve_model: list + fallback so a stale model name in
    .env auto-falls-back or errors with the available list.
  - provider fallback: a failing provider (down, rate-limited, bad key) hands
    over to the next one in LLM_PROVIDER instead of failing the whole run.
  - reasoning-model handling: models like gpt-oss spend tokens "thinking"
    before answering, so a tiny max_tokens returns empty content. We give them
    a token floor and a reasoning-effort hint, and turn an empty truncated
    reply into a clear error instead of silently returning "".
  - health(): cheap status check for the UI badge.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import requests


log = logging.getLogger("jobcopilot.llm")


def provider_chain() -> list[str]:
    """Providers to try, in order, from LLM_PROVIDER (read at call time)."""
    raw = os.getenv("LLM_PROVIDER", "ollama").lower()
    chain = [p.strip() for p in raw.split(",") if p.strip()]
    return chain or ["ollama"]


# First provider in the chain — kept as a module attribute for callers/UI that
# display "the" provider. Resolved at import; complete() re-reads the env.
PROVIDER = provider_chain()[0]

# Reasoning models need room to think before they answer.
_REASONING_MODEL_PREFIXES = ("openai/gpt-oss", "gpt-oss", "qwen/qwen3", "deepseek-r1")
_REASONING_TOKEN_FLOOR = 512


class LLMTruncated(RuntimeError):
    """The model hit max_tokens before producing any answer text."""


def _is_reasoning_model(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _REASONING_MODEL_PREFIXES)


_COMPAT = {
    "ollama": dict(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        api_key_env=None,
        model_env="OLLAMA_MODEL",
        default_model="qwen2.5:7b",
    ),
    "groq": dict(
        base_url="https://api.groq.com/openai/v1",
        api_key=None,
        api_key_env="GROQ_API_KEY",
        model_env="GROQ_MODEL",
        # Groq retired the Llama 3.x models in 2026; gpt-oss is the current default.
        default_model="openai/gpt-oss-120b,openai/gpt-oss-20b",
    ),
    "gemini": dict(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=None,
        api_key_env="GEMINI_API_KEY",
        model_env="GEMINI_MODEL",
        default_model="gemini-2.0-flash",
    ),
}


# ---------------------------------------------------------------------------
# Tolerant JSON parser — small local models love adding ``` fences and prose.
# ---------------------------------------------------------------------------

def parse_json(text: str):
    """Tolerant JSON parse: strips code fences, salvages the outermost {...},
    fixes trailing commas, balances one missing bracket if obvious."""
    t = (text or "").strip()
    # Strip code fences (```json ... ``` or ``` ... ```).
    t = re.sub(r"^\s*```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```\s*$", "", t, flags=re.I).strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    # Grab the outermost object/array — greedy on both ends.
    m = re.search(r"[\{\[].*[\}\]]", t, flags=re.S)
    if m:
        t = m.group(0)

    # Balance a single missing brace/bracket at the tail.
    if t.count("{") - t.count("}") == 1:
        t = t + "}"
    if t.count("[") - t.count("]") == 1:
        t = t + "]"
    # Strip trailing commas before } or ].
    t = re.sub(r",(\s*[}\]])", r"\1", t)

    return json.loads(t)


# ---------------------------------------------------------------------------
# Model listing + resolution (addendum-2 Part 2).
# ---------------------------------------------------------------------------

def available_models(provider: Optional[str] = None) -> list[str]:
    p = (provider or PROVIDER)
    if p == "ollama":
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    if p == "anthropic":
        return []  # trust ANTHROPIC_MODEL
    from openai import OpenAI
    cfg = _COMPAT[p]
    key = cfg.get("api_key") or os.getenv(cfg["api_key_env"] or "")
    client = OpenAI(base_url=cfg["base_url"], api_key=key or "x")
    return [m.id for m in client.models.list().data]


def resolve_models(provider: str, configured: str) -> list[str]:
    """`configured` may be a comma-separated candidate list. Return the
    candidates that are available, in the configured order (all of them when
    the provider can't be listed, e.g. offline)."""
    candidates = [c.strip() for c in (configured or "").split(",") if c.strip()]
    if not candidates:
        raise RuntimeError(f"no model configured for {provider}")
    if provider == "anthropic":
        return candidates
    try:
        avail = set(available_models(provider))
    except Exception:
        return candidates  # can't list (offline?) → just try them in order
    usable = [c for c in candidates if c in avail]
    if not usable:
        raise RuntimeError(
            f"No configured model available for {provider}. "
            f"Tried {candidates}. Available: {sorted(avail)[:15]}"
        )
    return usable


def resolve_model(provider: str, configured: str) -> str:
    """First available model from the configured candidate list."""
    return resolve_models(provider, configured)[0]


# ---------------------------------------------------------------------------
# The single entry point everything else uses.
# ---------------------------------------------------------------------------

def complete(system: str, user: str, *, temperature: float = 0.2,
             max_tokens: int = 1500) -> str:
    """Return the model's text, trying each provider in LLM_PROVIDER in order.

    A provider that fails (unreachable, missing key, rate-limited, truncated)
    hands over to the next one; if all fail, the last error is raised with the
    per-provider reasons attached so the UI can say what went wrong.
    """
    errors: list[str] = []
    last_exc: Exception | None = None
    for provider in provider_chain():
        try:
            return _complete_one(provider, system, user, temperature, max_tokens)
        except Exception as exc:  # noqa: BLE001 — fall through to next provider
            log.warning("llm provider=%s failed: %s", provider, exc)
            errors.append(f"{provider}: {exc}")
            last_exc = exc
    if len(errors) == 1 and last_exc is not None:
        raise last_exc
    raise RuntimeError("All LLM providers failed — " + " | ".join(errors)) from last_exc


def _complete_one(provider: str, system: str, user: str,
                  temperature: float, max_tokens: int) -> str:
    if provider == "anthropic":
        return _anthropic(system, user, temperature, max_tokens)

    cfg = _COMPAT.get(provider)
    if not cfg:
        raise ValueError(f"Unknown LLM provider {provider!r}")
    api_key = cfg.get("api_key") or (os.getenv(cfg["api_key_env"]) if cfg["api_key_env"] else None)
    if provider != "ollama" and not api_key:
        raise RuntimeError(f"{cfg['api_key_env']} not set for provider {provider}")

    configured = os.getenv(cfg["model_env"], cfg["default_model"])
    models = resolve_models(provider, configured)

    # Free tiers rate-limit per model, so a 429 / 5xx / truncation on one model
    # moves on to the next configured model before giving up on the provider.
    errors: list[str] = []
    last_exc: Exception | None = None
    for model in models:
        try:
            return _call_model(provider, cfg, api_key, model, system, user,
                               temperature, max_tokens)
        except Exception as exc:  # noqa: BLE001
            if _is_auth_error(exc):
                raise  # a bad key won't work for the next model either
            log.warning("llm %s/%s failed: %s", provider, model, exc)
            errors.append(f"{model}: {exc}")
            last_exc = exc
    if len(errors) == 1 and last_exc is not None:
        raise last_exc
    raise RuntimeError(f"all {provider} models failed — " + " | ".join(errors)) from last_exc


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S | re.I)


def _is_auth_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) in (401, 403)


def _call_model(provider: str, cfg: dict, api_key: Optional[str], model: str,
                system: str, user: str, temperature: float, max_tokens: int) -> str:
    kwargs: dict = {}
    if _is_reasoning_model(model):
        max_tokens = max(max_tokens, _REASONING_TOKEN_FLOOR)
        if provider == "groq":
            if model.lower().startswith(("openai/gpt-oss", "gpt-oss")):
                kwargs["reasoning_effort"] = os.getenv("GROQ_REASONING_EFFORT", "low")
            else:
                # qwen3 / deepseek-r1: keep reasoning out of the answer text.
                kwargs["extra_body"] = {"reasoning_format": "hidden"}  # Groq-only param

    try:
        from openai import OpenAI
        client = OpenAI(base_url=cfg["base_url"], api_key=api_key or "ollama")
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
    except Exception as exc:
        if provider == "ollama":
            raise RuntimeError(
                "Ollama call failed. Is it running and is the model pulled? "
                f"Run `ollama serve` and `ollama pull {model}`."
            ) from exc
        raise

    choice = resp.choices[0]
    # Some models (qwen3, deepseek-r1 via Ollama) inline their reasoning; it
    # must never reach parse_json (its braces derail the JSON salvage).
    content = _THINK_RE.sub("", choice.message.content or "")
    if not content.strip() and getattr(choice, "finish_reason", None) == "length":
        raise LLMTruncated(
            f"{provider}/{model} used all {max_tokens} tokens before answering "
            "(reasoning models need a larger max_tokens)."
        )
    return content


def complete_json(system: str, user: str, *, temperature: float = 0.2,
                  max_tokens: int = 2200) -> tuple[dict | list, dict]:
    """Call complete(); parse JSON; on failure, retry once with a stricter
    reminder appended to the system prompt. Returns (parsed, meta)."""
    raw = complete(system, user, temperature=temperature, max_tokens=max_tokens)
    try:
        return parse_json(raw), {"retries": 0, "raw_first_len": len(raw)}
    except Exception as first_exc:
        log.warning("json parse failed on first try (%s). retrying with stricter prompt.", first_exc)

    stricter = (
        system
        + "\n\nIMPORTANT: your previous reply was not valid JSON. "
          "Return ONLY a valid JSON object, no prose, no code fences, "
          "no trailing commas, and no line breaks inside string values."
    )
    raw2 = complete(stricter, user, temperature=min(temperature, 0.1), max_tokens=max_tokens)
    try:
        return parse_json(raw2), {"retries": 1, "raw_first_len": len(raw), "raw_second_len": len(raw2)}
    except Exception as second_exc:
        # Return the raw for the caller's error surface, honestly.
        raise ValueError(
            "The local model returned malformed JSON on both tries. "
            "This is common with small local models (llama3.2:3b). "
            "Consider switching LLM_PROVIDER=groq (free tier, stronger model, "
            "returns clean JSON). Second-try error: " + str(second_exc)
        ) from second_exc


def _anthropic(system: str, user: str, temperature: float, max_tokens: int) -> str:
    import anthropic  # only imported if LLM_PROVIDER=anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"),
        max_tokens=max_tokens, temperature=temperature, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


# ---------------------------------------------------------------------------
# Health for the UI badge.
# ---------------------------------------------------------------------------

def health() -> dict:
    """Status of the FIRST provider in the chain (what the UI badge shows),
    plus the full chain so a configured fallback is visible."""
    provider = provider_chain()[0]
    cfg = _COMPAT.get(provider) if provider != "anthropic" else None
    if provider != "anthropic" and cfg is None:
        return {"provider": provider, "chain": provider_chain(), "configured": None,
                "model": None, "reachable": False, "model_ok": False,
                "available_sample": [], "hint": f"Unknown LLM provider {provider!r}"}
    configured = (
        os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
        if provider == "anthropic"
        else os.getenv(cfg["model_env"], cfg["default_model"])
    )
    out = {
        "provider": provider,
        "chain": provider_chain(),
        "configured": configured,
        "model": None,
        "reachable": False,
        "model_ok": False,
        "available_sample": [],
        "hint": None,
    }
    try:
        avail = available_models(provider)
        out["reachable"] = True
        out["available_sample"] = sorted(avail)[:8]
        try:
            out["model"] = resolve_model(provider, configured)
            out["model_ok"] = True
        except Exception as exc:
            out["hint"] = str(exc)
    except Exception as exc:
        if provider == "ollama":
            out["hint"] = "Start `ollama serve` and `ollama pull " + (configured.split(",")[0]) + "`."
        else:
            out["hint"] = f"{type(exc).__name__}: {exc}"
    return out
