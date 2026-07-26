"""Provider-agnostic LLM module.

Default provider is **Ollama** (free, local, no key). Configured by env:

  LLM_PROVIDER = ollama | groq | gemini | anthropic
  OLLAMA_MODEL=qwen2.5:7b      (or candidates: "qwen2.5:7b,llama3.2:3b")
  GROQ_API_KEY, GROQ_MODEL
  GEMINI_API_KEY, GEMINI_MODEL
  ANTHROPIC_API_KEY, ANTHROPIC_MODEL

This is the only place that talks to a provider SDK. Tailoring and (optional)
keyword extraction must go through `complete()` so providers stay swappable.

Includes:
  - parse_json: tolerant JSON parsing (small local models often add fences).
  - available_models / resolve_model: list + fallback so a stale model name in
    .env auto-falls-back or errors with the available list.
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


PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()


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
        default_model="llama-3.1-8b-instant",
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


def resolve_model(provider: str, configured: str) -> str:
    """`configured` may be a comma-separated candidate list. Pick first available."""
    candidates = [c.strip() for c in (configured or "").split(",") if c.strip()]
    if not candidates:
        raise RuntimeError(f"no model configured for {provider}")
    if provider == "anthropic":
        return candidates[0]
    try:
        avail = set(available_models(provider))
    except Exception:
        return candidates[0]  # can't list (offline?) → just try the first
    for c in candidates:
        if c in avail:
            return c
    raise RuntimeError(
        f"No configured model available for {provider}. "
        f"Tried {candidates}. Available: {sorted(avail)[:15]}"
    )


# ---------------------------------------------------------------------------
# The single entry point everything else uses.
# ---------------------------------------------------------------------------

def complete(system: str, user: str, *, temperature: float = 0.2,
             max_tokens: int = 1500) -> str:
    """Return the model's text. Provider chosen by LLM_PROVIDER (default ollama)."""
    if PROVIDER == "anthropic":
        return _anthropic(system, user, temperature, max_tokens)

    cfg = _COMPAT.get(PROVIDER)
    if not cfg:
        raise ValueError(f"Unknown LLM_PROVIDER={PROVIDER!r}")
    api_key = cfg.get("api_key") or (os.getenv(cfg["api_key_env"]) if cfg["api_key_env"] else None)
    if PROVIDER != "ollama" and not api_key:
        raise RuntimeError(f"{cfg['api_key_env']} not set for LLM_PROVIDER={PROVIDER}")

    configured = os.getenv(cfg["model_env"], cfg["default_model"])
    model = resolve_model(PROVIDER, configured)

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
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        if PROVIDER == "ollama":
            raise RuntimeError(
                "Ollama call failed. Is it running and is the model pulled? "
                f"Run `ollama serve` and `ollama pull {model}`."
            ) from exc
        raise


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
    cfg = _COMPAT.get(PROVIDER) if PROVIDER != "anthropic" else None
    configured = (
        os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
        if PROVIDER == "anthropic"
        else os.getenv(cfg["model_env"], cfg["default_model"])
    )
    out = {
        "provider": PROVIDER,
        "configured": configured,
        "model": None,
        "reachable": False,
        "model_ok": False,
        "available_sample": [],
        "hint": None,
    }
    try:
        avail = available_models(PROVIDER)
        out["reachable"] = True
        out["available_sample"] = sorted(avail)[:8]
        try:
            out["model"] = resolve_model(PROVIDER, configured)
            out["model_ok"] = True
        except Exception as exc:
            out["hint"] = str(exc)
    except Exception as exc:
        if PROVIDER == "ollama":
            out["hint"] = "Start `ollama serve` and `ollama pull " + (configured.split(",")[0]) + "`."
        else:
            out["hint"] = f"{type(exc).__name__}: {exc}"
    return out
