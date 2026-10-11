#!/usr/bin/env python3
"""
PeerShop — product description via Gemini 3, value-oriented two-step.

Why PeerShop owns this instead of calling the foundation's describe_product:
the shared /root/pipeline function hard-pins a retired model (gemini-2.0-flash,
now 404) with no way to configure it. Rather than edit shared Hermes code, and
to implement the requested cheap-vs-quality model split, PeerShop does its own
description while still reusing the foundation's image processing (receive →
remove_background → enhance).

Model strategy (quality/value oriented):
  * STEP 1 — image identification: the CHEAPEST Gemini 3 multimodal model
    (gemini-3.1-flash-lite). The image (the token-expensive part) is sent ONLY
    here, and we ask for a short factual object statement — minimal output tokens.
  * STEP 2 — listing copy: a higher-quality Gemini 3 model
    (gemini-3.1-pro-preview), TEXT-ONLY (no image tokens), turns that factual
    statement into a catchy title + short description. (gemini-3.5-flash was
    evaluated but returns 504s consistently, so it's avoided.)

Requires the project venv (google-genai — the current SDK; the deprecated
google-generativeai has been migrated away). GEMINI_API_KEY is loaded from env
or /root/.hermes/.env.
"""

import json
import os

HERMES_ENV = "/root/.hermes/.env"

# Cheapest Gemini 3 multimodal for "what is this object"; quality pro for copy.
IMAGE_ID_MODEL = os.environ.get("PEERSHOP_IMAGE_MODEL", "gemini-3.1-flash-lite")
COPY_MODEL = os.environ.get("PEERSHOP_COPY_MODEL", "gemini-3.1-pro-preview")
_TIMEOUT = 110


def _ensure_key():
    if os.environ.get("GEMINI_API_KEY"):
        return
    if os.path.exists(HERMES_ENV):
        for line in open(HERMES_ENV):
            line = line.strip()
            if line.startswith("GEMINI_API_KEY=") and not line.startswith("#"):
                os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1]
                return
    raise RuntimeError("GEMINI_API_KEY not found in env or /root/.hermes/.env")


def _client():
    """A configured google.genai client (key loaded, per-request timeout set)."""
    from google import genai
    from google.genai import types
    _ensure_key()
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(timeout=_TIMEOUT * 1000),  # ms
    )


def identify_object(image_path: str) -> str:
    """STEP 1 (cheap, multimodal): a short factual statement of what the item is."""
    from google.genai import types
    with open(image_path, "rb") as f:
        image_data = f.read()
    client = _client()   # keep a reference: the Client closes its httpx client on GC
    resp = client.models.generate_content(
        model=IMAGE_ID_MODEL,
        contents=[
            "State plainly in 8-20 words what this product is and its key visible "
            "attributes (type, colour, material, notable features). Facts only, "
            "no marketing language.",
            types.Part.from_bytes(data=image_data, mime_type="image/png"),
        ],
    )
    return resp.text.strip()


def write_listing(object_statement: str) -> dict:
    """STEP 2 (quality, text-only): catchy title + short description as a dict."""
    client = _client()
    prompt = (
        f'A seller is listing this product: "{object_statement}".\n'
        'Write a marketplace listing as JSON with exactly two keys:\n'
        '  "title": a concise catchy title, 5-10 words.\n'
        '  "description": 1-2 sentences, 30-50 words, appealing to buyers.\n'
        'Output JSON only, no code fences.'
    )
    # Prefer the quality model; if it errors (e.g. a 504), fall back to the fast
    # cheap model so intake still gets real copy rather than failing outright.
    text = _generate_with_fallback(client, prompt)
    parsed = _parse_title_desc(text)
    if parsed:
        return parsed
    # Fallback: use the object statement itself rather than losing the info.
    return {"title": object_statement[:60] or "Product",
            "description": object_statement or "A quality product."}


def _generate_with_fallback(client, prompt: str) -> str:
    """Text-only generate; quality model first, cheap model as a 504/error fallback."""
    for model in (COPY_MODEL, IMAGE_ID_MODEL):
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            return resp.text.strip()
        except Exception:
            continue
    return ""


def _parse_title_desc(text: str) -> dict | None:
    """Extract {'title','description'} from a JSON-ish reply, or None if unparseable."""
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        if title or description:
            return {"title": title or "Product",
                    "description": description or "A quality product."}
    return None


def refine(title: str, description: str, change_request: str) -> dict:
    """
    ONE bounded, text-only regeneration of a listing incorporating the seller's
    plain-language change request. No image tokens (the item is already known).
    Returns {'title','description'}; on a failed/unparseable call it returns the
    CURRENT title/description unchanged rather than destroying the listing — the
    caller (intake) still counts the round, so this can't loop forever.
    """
    client = _client()
    prompt = (
        "You are refining a marketplace product listing based on the seller's "
        "request. Keep it accurate and appealing; change only what's asked.\n"
        f'Current title: "{title}"\n'
        f'Current description: "{description}"\n'
        f'Seller\'s requested change: "{change_request}"\n'
        'Rewrite as JSON with exactly two keys: "title" (5-10 words) and '
        '"description" (1-2 sentences, 30-50 words). Output JSON only, no code fences.'
    )
    parsed = _parse_title_desc(_generate_with_fallback(client, prompt))
    return parsed or {"title": title, "description": description}


def incorporate_hint(listing: dict, hint: str) -> dict:
    """
    Fold a caption/raw-text hint (e.g. a supplier's forwarded "Blue saree,
    cotton, 500rs") into an existing AI-drafted listing. Text-only, one call,
    no image tokens (the image was already described). On a failed/unparseable
    call, returns the ORIGINAL listing unchanged rather than losing it --
    same fail-open behaviour as refine().
    """
    client = _client()
    prompt = (
        "You are improving a marketplace product listing using an extra hint "
        "of real context from the seller (e.g. their own caption or supplier "
        "note). Keep it accurate; only use details confirmed by the hint, "
        "don't invent anything the hint doesn't support.\n"
        f'Current title: "{listing.get("title", "")}"\n'
        f'Current description: "{listing.get("description", "")}"\n'
        f'Seller\'s hint: "{hint}"\n'
        'Rewrite as JSON with exactly two keys: "title" (5-10 words) and '
        '"description" (1-2 sentences, 30-50 words). Output JSON only, no code fences.'
    )
    parsed = _parse_title_desc(_generate_with_fallback(client, prompt))
    if parsed:
        result = dict(listing)
        result["title"] = parsed["title"]
        result["description"] = parsed["description"]
        return result
    return listing


def describe(image_path: str) -> dict:
    """Full two-step describe. Returns {'title', 'description', 'object'}."""
    obj = identify_object(image_path)
    listing = write_listing(obj)
    listing["object"] = obj
    return listing


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: ./.venv/bin/python describe.py <image_path>")
        raise SystemExit(1)
    print(json.dumps(describe(sys.argv[1]), indent=2))
