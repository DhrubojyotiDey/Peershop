#!/usr/bin/env python3
"""
PeerShop — shared LLM plumbing for the narrow, single-purpose AI calls.

Both Phase-2 categorization and Phase-4 per-product chat are ONE bounded call
each: small prompt in, small answer out, no history, no tools, no agent
framework. This module is the single place that knows how to talk to the
provider — so the free-tier config and the Cloudflare User-Agent fix live once.

Stdlib only (urllib). Groq by default; OpenRouter as a drop-in via
PEERSHOP_PROVIDER=openrouter.
"""

import json
import os
import urllib.request

HERMES_ENV = "/root/.hermes/.env"

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.1-8b-instant",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
    },
}
PROVIDER = os.environ.get("PEERSHOP_PROVIDER", "groq")


def _load_key(key_env: str) -> str:
    """Read an API key from the environment, falling back to /root/.hermes/.env."""
    if os.environ.get(key_env):
        return os.environ[key_env]
    if os.path.exists(HERMES_ENV):
        for line in open(HERMES_ENV):
            line = line.strip()
            if line.startswith(f"{key_env}=") and not line.startswith("#"):
                return line.split("=", 1)[1]
    raise RuntimeError(f"{key_env} not found in env or {HERMES_ENV}")


def complete(messages: list[dict], max_tokens: int, temperature: float = 0.0,
             timeout: int = 30) -> str:
    """
    One chat completion. Returns the assistant's text content.

    Callers pass their own tiny message list and a small max_tokens — this
    module intentionally offers no history/session state, keeping every call
    inside free-tier tokens-per-minute limits.
    """
    cfg = PROVIDERS[PROVIDER]
    key = _load_key(cfg["key_env"])
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Groq is behind Cloudflare, which 403s (err 1010) the default
            # Python-urllib signature. A plain UA is enough to pass.
            "User-Agent": "peershop/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]
