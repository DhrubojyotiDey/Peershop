#!/usr/bin/env python3
"""
PeerShop — Phase 3: the shared listing page.

ONE catalog page across ALL sellers. Lean and native: Python stdlib
http.server + sqlite3, plain HTML. No Flask, no template engine, no new deps.

What it shows:
  * Only products with status = 'live'. Pending/uncategorized are hidden
    (per the agreed policy — no admin review UI yet).
  * All sellers' live products together, grouped by category.
  * Optional category filter:  /?category=saree
  * Each product's image, served from its on-disk image_path.
  * Seller name + an OPTIONAL outbound Meesho link (plain link only — no sync).

Routes:
  GET /                 listing (all live products, grouped by category)
  GET /?category=NAME   listing filtered to one category
  GET /image/<id>       the product image bytes (live products only)
  GET /health           "ok"

Usage:
    python3 seed_demo.py        # populate some live products to look at
    python3 serve.py            # serve on http://localhost:8000
    PEERSHOP_PORT=9000 python3 serve.py
"""

import hmac
import html
import http.cookies
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import attributes
import auth
import chat
import telegram_link
import onboarding
import seller_actions

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")
PORT = int(os.environ.get("PEERSHOP_PORT", "8000"))
HOST = os.environ.get("PEERSHOP_HOST", "127.0.0.1")   # dev sets 0.0.0.0 for LAN access
WA_COUNTRY_CODE = "91"          # default country code for wa.me links (India)

# Web product-photo uploads (Phase H2).
PRODUCT_MEDIA = os.path.join(HERE, "media", "products")
def _hermes_val(key: str) -> str:
    """Read a config value from env, else /root/.hermes/.env (same idiom as llm.py)."""
    if os.environ.get(key):
        return os.environ[key]
    p = "/root/.hermes/.env"
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    return ""


TG_BOT_USERNAME = _hermes_val("PEERSHOP_TELEGRAM_BOT_USERNAME")  # for /start deep links
MAX_UPLOAD = 34 * 1024 * 1024                   # total multipart body cap (~4 × 8 MB photos)
MAX_FORM_BODY = 256 * 1024                       # cap for non-upload POST bodies (form/JSON) — DoS guard
MAX_QUESTION = 1000                              # max chars of a customer chat question (prompt-bloat guard)
MAX_PHOTOS = 4                                  # per product (matches post_wizard)
_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_CTYPE_EXT = {"image/jpeg": ".jpg", "image/png": ".png",
              "image/gif": ".gif", "image/webp": ".webp"}


# ── Per-IP rate limit for the public chat endpoint (in-memory) ────────────────
# /product/<id>/chat makes a billed LLM call per request, so cap it per client
# IP with an in-memory sliding window. Fine at current single-process scale;
# swap for a shared store (Redis/etc.) only if serve.py is ever run multi-process.
RATE_LIMIT_MAX = 5                       # requests allowed per window, per IP
RATE_LIMIT_WINDOW = 60                   # window length, seconds
_rate_limit_hits: dict[str, list[float]] = {}   # ip -> recent hit timestamps
_rate_limit_lock = threading.Lock()      # ThreadingHTTPServer → one thread/request


def rate_limit_ok(ip: str) -> bool:
    """True if this IP may make another chat request (and record the hit); False
    once it has made RATE_LIMIT_MAX within the trailing RATE_LIMIT_WINDOW seconds."""
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_limit_lock:
        hits = [t for t in _rate_limit_hits.get(ip, ()) if t > cutoff]
        if len(hits) >= RATE_LIMIT_MAX:
            _rate_limit_hits[ip] = hits          # keep the pruned window; reject
            return False
        hits.append(now)
        _rate_limit_hits[ip] = hits
        # Opportunistic sweep so idle IPs don't accumulate unbounded.
        if len(_rate_limit_hits) > 4096:
            for k in [k for k, v in _rate_limit_hits.items()
                      if not v or v[-1] <= cutoff]:
                del _rate_limit_hits[k]
        return True


# ── Data access ──────────────────────────────────────────────────────────────
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def live_products(category: str | None = None) -> list[sqlite3.Row]:
    """All live products across every seller, optionally filtered by category."""
    sql = (
        "SELECT p.id, p.title, p.description, p.image_path, p.price, "
        "       c.name AS category, s.id AS seller_id, s.name AS seller, "
        "       s.meesho_store_url "
        "FROM products p "
        "JOIN sellers s ON p.seller_id = s.id "
        "LEFT JOIN categories c ON p.category_id = c.id "
        "WHERE p.status = 'live' "
    )
    params: tuple = ()
    if category:
        sql += "AND c.name = ? "
        params = (category,)
    sql += "ORDER BY c.name, p.created_at DESC"
    with _conn() as conn:
        return conn.execute(sql, params).fetchall()


def live_categories() -> list[str]:
    """Category names that currently have at least one live product."""
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT c.name FROM products p "
            "JOIN categories c ON p.category_id = c.id "
            "WHERE p.status = 'live' AND c.name IS NOT NULL "
            "ORDER BY c.name")]


def image_path_for(product_id: int) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT image_path FROM products WHERE id = ? AND status = 'live'",
            (product_id,)).fetchone()
    return row[0] if row else None


def gallery_paths_for(product_id: int) -> list[str]:
    """All photo paths for a LIVE product, primary first. Falls back to the single
    products.image_path when a product has no product_photos rows (web-added/old)."""
    with _conn() as conn:
        live = conn.execute(
            "SELECT image_path FROM products WHERE id = ? AND status = 'live'",
            (product_id,)).fetchone()
        if not live:
            return []
        rows = conn.execute(
            "SELECT image_path FROM product_photos WHERE product_id = ? "
            "ORDER BY position, id", (product_id,)).fetchall()
    return [r[0] for r in rows] or [live[0]]


def live_product(product_id: int) -> sqlite3.Row | None:
    """One live product with seller + category, for the detail page."""
    with _conn() as conn:
        return conn.execute(
            "SELECT p.id, p.title, p.description, p.price, c.name AS category, "
            "       s.id AS seller_id, s.name AS seller, s.owner_name, s.contact, "
            "       s.meesho_store_url "
            "FROM products p "
            "JOIN sellers s ON p.seller_id = s.id "
            "LEFT JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = ? AND p.status = 'live'",
            (product_id,)).fetchone()


# ── HTML rendering (plain strings, everything escaped) ────────────────────────
PAGE_CSS = """
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:#faf7f5; color:#241f1c; }
  header { padding:20px 24px; background:#fff; border-bottom:1px solid #eee; }
  header h1 { margin:0; font-size:20px; }
  header h1 a { color:inherit; text-decoration:none; }
  header p { margin:4px 0 0; color:#8a807a; font-size:13px; }
  nav { padding:12px 24px; display:flex; gap:10px; flex-wrap:wrap; }
  nav a { text-decoration:none; font-size:13px; padding:6px 12px; border-radius:999px;
          background:#fff; border:1px solid #e5ddd8; color:#5a504a; }
  nav a.active { background:#241f1c; color:#fff; border-color:#241f1c; }
  .cat { padding:8px 24px 4px; font-size:14px; font-weight:600; color:#8a807a;
         text-transform:capitalize; }
  .grid { display:grid; gap:16px; padding:8px 24px 32px;
          grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); }
  .card { background:#fff; border:1px solid #eee; border-radius:12px; overflow:hidden; }
  .card img { width:100%; height:200px; object-fit:cover; background:#f0ebe8; display:block; }
  .card .body { padding:12px 14px; }
  .card h3 { margin:0 0 6px; font-size:15px; }
  .card h3 a, .card a { color:inherit; text-decoration:none; }
  .card p { margin:0 0 8px; font-size:13px; color:#6a605a; line-height:1.4; }
  .card .price { margin:0 0 6px; font-size:15px; font-weight:700; color:#241f1c; }
  .meta { font-size:12px; color:#9a908a; display:flex; justify-content:space-between;
          align-items:center; }
  .meta a { color:#a3573b; text-decoration:none; }
  .meta a.sellerlink { color:#5a504a; }
  .meta a.sellerlink:hover { text-decoration:underline; }
  .empty { padding:60px 24px; text-align:center; color:#9a908a; }
  form.search { margin-top:12px; display:flex; gap:8px; max-width:520px; align-items:center; }
  form.search input { flex:1; padding:9px 12px; border:1px solid #e5ddd8;
                      border-radius:10px; font-size:14px; }
  form.search input.pin { flex:0 0 130px; }
  .shopcard .near { display:inline-block; margin-top:6px; font-size:11px; font-weight:600;
                    color:#2f6b4f; background:#e8f3ec; padding:2px 8px; border-radius:999px; }
  form.search button { padding:9px 16px; border:0; border-radius:10px;
                       background:#241f1c; color:#fff; font-size:13px; cursor:pointer; }
  form.search a.clear { font-size:12px; color:#8a807a; text-decoration:none; white-space:nowrap; }
  .shops { display:grid; gap:12px; padding:8px 24px 32px;
           grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }
  .shopcard { background:#fff; border:1px solid #eee; border-radius:12px; padding:16px;
              text-decoration:none; color:inherit; display:block; }
  .shopcard:hover { border-color:#d8ccc4; }
  .shopcard .shopname { font-size:16px; font-weight:600; }
  .shopcard .owner { font-size:13px; color:#6a605a; margin-top:4px; }
  .shopcard .nlive { font-size:12px; color:#9a908a; margin-top:8px; }
"""


def _inr(price) -> str:
    """Format a price as INR (₹1,499), or '' when unset."""
    return f"₹{price:,.0f}" if price is not None else ""


def wa_digits(contact) -> str | None:
    """
    Normalize a freeform seller `contact` into a wa.me phone (digits only), or
    None if it isn't a plausible phone. Strips a single leading 0; a bare 10-digit
    number gets WA_COUNTRY_CODE prepended; 11-15 digits are trusted as-is.
    None → the 'not a usable phone' signal that hides the WhatsApp button.
    """
    digits = re.sub(r"\D", "", contact or "")
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = WA_COUNTRY_CODE + digits
    return digits if 10 <= len(digits) <= 15 else None


def _card(p: sqlite3.Row) -> str:
    title = html.escape(p["title"] or "Untitled")
    desc = html.escape((p["description"] or "").strip())
    if len(desc) > 140:
        desc = desc[:137] + "…"
    seller = html.escape(p["seller"] or "")
    meesho = ""
    if p["meesho_store_url"]:
        url = html.escape(p["meesho_store_url"], quote=True)
        meesho = f'<a href="{url}" target="_blank" rel="noopener nofollow">Meesho ↗</a>'
    return (
        f'<div class="card">'
        f'<a href="/product/{p["id"]}"><img src="/image/{p["id"]}" '
        f'alt="{title}" loading="lazy"></a>'
        f'<div class="body">'
        f'<h3><a href="/product/{p["id"]}">{title}</a></h3>'
        + (f'<div class="price">{_inr(p["price"])}</div>' if p["price"] is not None else "")
        + f'<p>{desc}</p>'
        f'<div class="meta"><a class="sellerlink" href="/seller/{p["seller_id"]}">'
        f'{seller}</a>{meesho}</div>'
        f'</div></div>'
    )


def search_sellers(term: str) -> list[sqlite3.Row]:
    """Shops whose name OR owner_name matches `term` (case-insensitive substring),
    limited to sellers with at least one live product so every result links to a
    non-empty profile. Parameterized + LIKE-escaped — safe from injection and
    stray wildcard input."""
    safe = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{safe}%"
    sql = (
        "SELECT s.id, s.name, s.owner_name, s.pincode, COUNT(p.id) AS n_live "
        "FROM sellers s "
        "JOIN products p ON p.seller_id = s.id AND p.status = 'live' "
        "WHERE s.name LIKE ? ESCAPE '\\' OR s.owner_name LIKE ? ESCAPE '\\' "
        "GROUP BY s.id, s.name, s.owner_name, s.pincode "
        "ORDER BY s.name"
    )
    with _conn() as conn:
        return conn.execute(sql, (like, like)).fetchall()


def search_products(term: str) -> list[sqlite3.Row]:
    """Live products across ALL sellers whose title or description matches `term`
    (case-insensitive substring). Returns the same columns as live_products so
    _card() renders them unchanged. Parameterized + LIKE-escaped."""
    safe = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{safe}%"
    sql = (
        "SELECT p.id, p.title, p.description, p.image_path, p.price, "
        "       c.name AS category, s.id AS seller_id, s.name AS seller, "
        "       s.meesho_store_url, s.pincode AS seller_pincode "
        "FROM products p "
        "JOIN sellers s ON p.seller_id = s.id "
        "LEFT JOIN categories c ON p.category_id = c.id "
        "WHERE p.status = 'live' "
        "  AND (p.title LIKE ? ESCAPE '\\' OR p.description LIKE ? ESCAPE '\\') "
        "ORDER BY p.created_at DESC"
    )
    with _conn() as conn:
        return conn.execute(sql, (like, like)).fetchall()


def _pin_distance(seller_pin: str | None, buyer_pin: str) -> int:
    """Nearness rank, lower = nearer. `buyer_pin` is a validated 6-digit PIN.
    Exact match first (0), then by shared leading-digit prefix (India PINs get
    more specific left-to-right), then sellers with no PIN last (7)."""
    if not seller_pin:
        return 7
    if seller_pin == buyer_pin:
        return 0
    common = 0
    for a, b in zip(seller_pin, buyer_pin):
        if a == b:
            common += 1
        else:
            break
    return 6 - common          # share 5 → 1 … share 0 → 6


def render_listing(category: str | None, query: str | None = None,
                   loc: str | None = None) -> str:
    q = (query or "").strip()
    loc = (loc or "").strip()
    # A valid buyer PIN turns on nearest-first ranking of search results.
    try:
        buyer_pin = onboarding.normalize_pincode(loc)
    except ValueError:
        buyer_pin = None
    search_box = (
        '<form class="search" method="get" action="/" role="search">'
        f'<input type="text" name="q" value="{html.escape(q, quote=True)}" '
        'placeholder="Search products, shops, or owners…" '
        'aria-label="Search products, shops, or owners">'
        f'<input class="pin" type="text" name="loc" value="{html.escape(loc, quote=True)}" '
        'inputmode="numeric" pattern="[1-9][0-9]{5}" placeholder="Your pincode" '
        'aria-label="Your pincode for nearest-first results">'
        '<button type="submit">Search</button>'
        + ('<a class="clear" href="/">Clear</a>' if (q or loc) else '')
        + '</form>')

    if q:
        # Search takes over the body; the category nav isn't relevant here.
        shops = search_sellers(q)
        prods = search_products(q)
        if buyer_pin:                       # nearest-first
            shops = sorted(shops, key=lambda s: _pin_distance(s["pincode"], buyer_pin))
            prods = sorted(prods, key=lambda p: _pin_distance(p["seller_pincode"], buyer_pin))
        qh = html.escape(q)
        near_note = (f'<div class="cat">📍 Nearest to {html.escape(buyer_pin)} first</div>'
                     if buyer_pin else "")
        nav = ""
        parts = []
        if shops:
            cards = []
            for s in shops:
                name = html.escape(s["name"] or "Shop")
                owner = (f'<div class="owner">Run by {html.escape(s["owner_name"])}</div>'
                         if s["owner_name"] else "")
                badge = ""
                if buyer_pin:
                    d = _pin_distance(s["pincode"], buyer_pin)
                    if d == 0:
                        badge = '<div class="near">📍 same pincode</div>'
                    elif d <= 3:
                        badge = '<div class="near">📍 nearby</div>'
                cards.append(
                    f'<a class="shopcard" href="/seller/{s["id"]}">'
                    f'<div class="shopname">{name}</div>{owner}{badge}'
                    f'<div class="nlive">{s["n_live"]} live item(s)</div></a>')
            parts.append(f'<div class="cat">Shops matching “{qh}”</div>')
            parts.append(f'<div class="shops">{"".join(cards)}</div>')
        if prods:
            parts.append(f'<div class="cat">Products matching “{qh}”</div>')
            parts.append(f'<div class="grid">{"".join(_card(p) for p in prods)}</div>')
        if shops or prods:
            body = near_note + "".join(parts)
        else:
            body = (f'<div class="empty">Nothing matches “{qh}”.<br>'
                    'Try a product, a shop name, or an owner name.</div>')
    else:
        rows = live_products(category)
        cats = live_categories()
        nav_links = ['<a href="/" class="%s">All</a>' % ("" if category else "active")]
        for c in cats:
            active = "active" if c == category else ""
            nav_links.append(
                f'<a href="/?category={urllib.parse.quote(c)}" class="{active}">'
                f'{html.escape(c).capitalize()}</a>')
        nav = f"<nav>{''.join(nav_links)}</nav>"
        if not rows:
            body = '<div class="empty">No live products yet.</div>'
        elif category:
            body = f'<div class="grid">{"".join(_card(p) for p in rows)}</div>'
        else:
            # Group by category for the unfiltered view.
            groups: dict[str, list] = {}
            for p in rows:
                groups.setdefault(p["category"] or "uncategorized", []).append(p)
            parts = []
            for cat, items in groups.items():
                parts.append(f'<div class="cat">{html.escape(cat)}</div>')
                parts.append(f'<div class="grid">{"".join(_card(p) for p in items)}</div>')
            body = "".join(parts)

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>PeerShop</title><style>{PAGE_CSS}</style></head><body>"
        "<header><h1><a href='/'>PeerShop</a></h1>"
        "<p>Products from your neighbors</p>"
        f"{search_box}"
        '<p style="margin-top:6px"><a href="/seller/login" '
        'style="color:#a3573b;text-decoration:none">Seller? Log in or create an '
        'account →</a></p></header>'
        f"{nav}{body}</body></html>"
    )


DETAIL_CSS = """
  .detail { max-width:900px; margin:0 auto; padding:16px 24px 40px;
            display:grid; grid-template-columns:1fr 1fr; gap:28px; }
  .detail .gallery img.main { width:100%; border-radius:12px; background:#f0ebe8; }
  .detail img { width:100%; border-radius:12px; background:#f0ebe8; }
  .thumbs { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
  .thumbs img.thumb { width:64px; height:64px; object-fit:cover; border-radius:8px;
                      cursor:pointer; border:2px solid transparent; background:#f0ebe8; }
  .thumbs img.thumb.active { border-color:#a3573b; }
  .detail h2 { margin:0 0 8px; font-size:22px; }
  .detail .cat { padding:0; font-size:12px; text-transform:uppercase;
                 letter-spacing:.05em; }
  .detail .desc { font-size:15px; line-height:1.6; color:#463f3a; margin:12px 0; }
  .detail .seller { font-size:13px; color:#8a807a; }
  .detail .seller a { color:#a3573b; text-decoration:none; }
  .back { display:inline-block; margin:12px 24px; color:#8a807a;
          text-decoration:none; font-size:13px; }
  .chat { margin-top:20px; border-top:1px solid #eee; padding-top:16px; }
  .chat h4 { margin:0 0 4px; font-size:14px; }
  .chat .hint { margin:0 0 10px; font-size:12px; color:#9a908a; }
  .thread { display:flex; flex-direction:column; gap:8px; margin-bottom:12px; }
  .bubble { padding:8px 12px; border-radius:12px; font-size:13px; max-width:85%;
            line-height:1.45; }
  .bubble.q { align-self:flex-end; background:#241f1c; color:#fff; }
  .bubble.a { align-self:flex-start; background:#f0ebe8; color:#241f1c; }
  .ask { display:flex; gap:8px; }
  .ask input { flex:1; padding:9px 12px; border:1px solid #e5ddd8; border-radius:10px;
               font-size:13px; }
  .ask button { padding:9px 16px; border:0; border-radius:10px; background:#241f1c;
                color:#fff; font-size:13px; cursor:pointer; }
  .ask button:disabled { opacity:.5; cursor:default; }
  .wa-btn { margin-top:12px; width:100%; padding:11px 16px; border:0; border-radius:10px;
            background:#25D366; color:#fff; font-size:14px; font-weight:600; cursor:pointer; }
  .wa-btn:hover { background:#1ebe5b; }
  @media (max-width:640px){ .detail{ grid-template-columns:1fr; } }
"""

# JS is kept tiny and history-free: each request posts ONLY the new question.
DETAIL_JS = """
  function selectPhoto(el){
    document.getElementById('main-img').src = el.src;
    document.querySelectorAll('.thumbs .thumb').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
  }
  const pid = CTX.pid;
  const thread = document.getElementById('thread');
  const input = document.getElementById('q');
  const btn = document.getElementById('send');
  function bubble(cls, text){
    const d = document.createElement('div');
    d.className = 'bubble ' + cls; d.textContent = text;
    thread.appendChild(d); thread.scrollTop = thread.scrollHeight;
    return d;
  }
  async function ask(){
    const q = input.value.trim(); if(!q) return;
    input.value=''; btn.disabled=true;
    bubble('q', q);
    const thinking = bubble('a', '…');
    try {
      const r = await fetch('/product/'+pid+'/chat', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({question: q})   // no history — just this question
      });
      const data = await r.json();
      thinking.textContent = data.answer || data.error || 'Sorry, something went wrong.';
    } catch(e){ thinking.textContent = 'Sorry, something went wrong.'; }
    btn.disabled=false; input.focus();
  }
  btn.addEventListener('click', ask);
  input.addEventListener('keydown', e => { if(e.key==='Enter') ask(); });

  // Click-to-chat: open WhatsApp toward the seller, prefilled with the product
  // link + the customer's typed question (Instagram-ad style). Client-side only.
  function sellerMsg(){
    const q = input.value.trim();
    let text = 'Hi ' + CTX.greet + ', I\\'m interested in "' + CTX.title + '"'
             + (CTX.priceStr ? ' (' + CTX.priceStr + ')' : '')
             + ' on PeerShop.\\n' + CTX.productUrl
             + (q ? '\\n\\nMy question: ' + q : '');
    window.open('https://wa.me/' + CTX.waDigits + '?text=' + encodeURIComponent(text), '_blank');
  }
  const waBtn = document.getElementById('msg-seller');
  if (waBtn) waBtn.addEventListener('click', sellerMsg);
"""


def _gallery_html(product_id: int, title: str) -> str:
    """Primary photo + clickable thumbnails (thumbnails shown only when >1 photo)."""
    n = len(gallery_paths_for(product_id))
    main = f'<img id="main-img" class="main" src="/image/{product_id}/0" alt="{title}">'
    if n <= 1:
        return f'<div class="gallery">{main}</div>'
    thumbs = "".join(
        f'<img class="thumb{" active" if i == 0 else ""}" '
        f'src="/image/{product_id}/{i}" alt="{title} photo {i + 1}" '
        'onclick="selectPhoto(this)">'
        for i in range(n))
    return f'<div class="gallery">{main}<div class="thumbs">{thumbs}</div></div>'


def render_detail(p: sqlite3.Row, base_url: str) -> str:
    title = html.escape(p["title"] or "Untitled")
    desc = html.escape((p["description"] or "").strip())
    seller = html.escape(p["seller"] or "")
    category = html.escape(p["category"] or "uncategorized")
    meesho = ""
    if p["meesho_store_url"]:
        url = html.escape(p["meesho_store_url"], quote=True)
        meesho = (f' · <a href="{url}" target="_blank" rel="noopener nofollow">'
                  f'Visit Meesho store ↗</a>')

    # Click-to-chat: build a wa.me link client-side from these values + the typed
    # question. Button shown only when the seller's contact is a usable phone.
    digits = wa_digits(p["contact"])
    wa_button = ('<button id="msg-seller" class="wa-btn" type="button">'
                 '💬 Message the seller on WhatsApp</button>') if digits else ""
    ctx = json.dumps({
        "pid": p["id"],
        "waDigits": digits,
        "productUrl": f"{base_url}/product/{p['id']}",
        "title": p["title"] or "this product",
        "priceStr": _inr(p["price"]),
        "greet": p["owner_name"] or p["seller"] or "there",
    })

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title} — PeerShop</title>"
        f"<style>{PAGE_CSS}{DETAIL_CSS}</style></head><body>"
        "<header><h1><a href='/'>PeerShop</a></h1><p>Products from your neighbors</p></header>"
        '<a class="back" href="/">← Back to all products</a>'
        '<div class="detail">'
        f'<div>{_gallery_html(p["id"], title)}</div>'
        '<div>'
        f'<div class="cat">{category}</div>'
        f'<h2>{title}</h2>'
        + (f'<div style="font-size:20px;font-weight:700;color:#241f1c;margin:4px 0">'
           f'{_inr(p["price"])}</div>' if p["price"] is not None else "")
        + f'<div class="desc">{desc or "No description provided."}</div>'
        f'<div class="seller">Sold by <a href="/seller/{p["seller_id"]}">{seller}</a>{meesho}</div>'
        '<div class="chat">'
        '<h4>Ask about this product</h4>'
        '<p class="hint">Answers come only from this listing’s description. '
        'For anything else, message the seller.</p>'
        '<div class="thread" id="thread"></div>'
        '<div class="ask">'
        '<input id="q" placeholder="e.g. What material is it?" autocomplete="off">'
        '<button id="send">Ask</button>'
        '</div>'
        f'{wa_button}'
        '</div></div>'
        '</div>'
        f'<script>const CTX={ctx};{DETAIL_JS}</script>'
        "</body></html>"
    )


# ── Seller profile pages (Phase E) ───────────────────────────────────────────
def seller_profile(seller_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT id, name, owner_name, contact, meesho_store_url "
            "FROM sellers WHERE id = ?", (seller_id,)).fetchone()


def seller_tagged_departments(seller_id: int) -> list[str]:
    """The departments a seller is tagged with (their profile identity)."""
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT d.name FROM seller_departments sd "
            "JOIN departments d ON d.id = sd.department_id "
            "WHERE sd.seller_id = ? ORDER BY d.id", (seller_id,))]


def seller_photo_rows(seller_id: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT id, image_path FROM seller_photos WHERE seller_id = ? ORDER BY id",
            (seller_id,)).fetchall()


def seller_photo_path(photo_id: int) -> str | None:
    with _conn() as conn:
        r = conn.execute("SELECT image_path FROM seller_photos WHERE id = ?",
                         (photo_id,)).fetchone()
    return r[0] if r else None


def seller_live_products(seller_id: int, dept: str | None = None,
                         cat: str | None = None,
                         sub: str | None = None) -> list[sqlite3.Row]:
    """A seller's live products (with dept/category/subcategory names + attributes)."""
    sql = (
        "SELECT p.id, p.title, p.description, p.image_path, p.attributes, "
        "       p.price, p.category_id, d.name AS department, c.name AS category, "
        "       sc.name AS subcategory "
        "FROM products p JOIN departments d ON d.id = p.department_id "
        "LEFT JOIN categories c ON c.id = p.category_id "
        "LEFT JOIN subcategories sc ON sc.id = p.subcategory_id "
        "WHERE p.seller_id = ? AND p.status = 'live'")
    params: list = [seller_id]
    if dept:
        sql += " AND d.name = ?"; params.append(dept)
    if cat:
        sql += " AND c.name = ?"; params.append(cat)
    if sub:
        sql += " AND sc.name = ?"; params.append(sub)
    sql += " ORDER BY d.name, c.name, sc.name, p.title"
    with _conn() as conn:
        return conn.execute(sql, params).fetchall()


PROFILE_CSS = """
  .shop { background:#fff; border-bottom:1px solid #eee; }
  .shop .photos { display:flex; gap:8px; overflow-x:auto; padding:16px 24px 8px; }
  .shop .photos img { height:170px; border-radius:12px; object-fit:cover;
                      background:#f0ebe8; flex:0 0 auto; }
  .shop .noshop { padding:20px 24px 8px; color:#9a908a; font-size:13px; }
  .shop .info { padding:6px 24px 16px; }
  .shop h1 { margin:0; font-size:22px; }
  .shop .owner { font-size:13px; color:#5a504a; margin-top:2px; }
  .shop .tags { display:flex; gap:6px; flex-wrap:wrap; margin:8px 0 6px; }
  .shop .tag { font-size:12px; padding:3px 10px; border-radius:999px;
               background:#efe7e2; color:#5a504a; text-transform:capitalize; }
  .shop .contact { font-size:13px; color:#8a807a; }
  .shop .contact a { color:#a3573b; text-decoration:none; }
  .filters { padding:8px 24px; display:flex; gap:6px; flex-wrap:wrap;
             align-items:center; border-bottom:1px solid #f0eae6; }
  .filters .label { font-size:12px; color:#9a908a; margin:0 2px 0 8px; }
  .filters select { padding:5px 8px; border:1px solid #e5ddd8; border-radius:8px;
                    font-size:13px; background:#fff; }
  .catrow { display:flex; align-items:center; gap:12px; padding:12px 24px 0;
            flex-wrap:wrap; }
  .catrow .cat { padding:0; font-size:15px; color:#241f1c; text-transform:capitalize; }
  .depthead { padding:14px 24px 0; font-size:16px; font-weight:600; color:#241f1c;
              text-transform:capitalize; }
  .ptags { display:flex; gap:5px; flex-wrap:wrap; margin:0 0 8px; }
  .ptag { font-size:11px; padding:2px 8px; border-radius:999px; background:#f2ece8;
          color:#6a605a; }
  .back { display:inline-block; margin:12px 24px; color:#8a807a;
          text-decoration:none; font-size:13px; }
"""


def _attrs(row) -> dict:
    try:
        return json.loads(row["attributes"]) if row["attributes"] else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _apply_attr_filters(rows, attr_filters):
    if not attr_filters:
        return rows
    out = []
    for r in rows:
        a = _attrs(r)
        if all(str(a.get(n, "")).lower() == v.lower() for n, v in attr_filters):
            out.append(r)
    return out


def _url(seller_id, dept=None, cat=None, sub=None, attr_filters=None) -> str:
    q = []
    if dept:
        q.append("dept=" + urllib.parse.quote(dept))
    if cat:
        q.append("cat=" + urllib.parse.quote(cat))
    if sub:
        q.append("sub=" + urllib.parse.quote(sub))
    for n, v in (attr_filters or []):
        q.append("af=" + urllib.parse.quote(f"{n}:{v}"))
    return f"/seller/{seller_id}" + ("?" + "&".join(q) if q else "")


def _pill(label, url, active) -> str:
    cls = "active" if active else ""
    return (f'<a href="{html.escape(url, quote=True)}" class="{cls}">'
            f'{html.escape(label).capitalize()}</a>')


def _profile_card(r) -> str:
    title = html.escape(r["title"] or "Untitled")
    desc = html.escape((r["description"] or "").strip())
    if len(desc) > 120:
        desc = desc[:117] + "…"
    tags = []
    if r["subcategory"]:
        tags.append(html.escape(r["subcategory"]))
    for k, v in _attrs(r).items():
        tags.append(html.escape(f"{k}: {v}"))
    tag_html = ('<div class="ptags">'
                + "".join(f'<span class="ptag">{t}</span>' for t in tags)
                + '</div>') if tags else ""
    return (
        f'<div class="card"><a href="/product/{r["id"]}">'
        f'<img src="/image/{r["id"]}" alt="{title}" loading="lazy"></a>'
        f'<div class="body"><h3><a href="/product/{r["id"]}">{title}</a></h3>'
        + (f'<div class="price">{_inr(r["price"])}</div>' if r["price"] is not None else "")
        + f'{tag_html}<p>{desc}</p></div></div>')


def _subcat_select(seller_id, dept, cat, subs, current_sub) -> str:
    all_url = html.escape(_url(seller_id, dept=dept, cat=cat), quote=True)
    opts = [f'<option value="{all_url}"'
            f'{"" if current_sub else " selected"}>All subcategories</option>']
    for s in subs:
        u = html.escape(_url(seller_id, dept=dept, cat=cat, sub=s), quote=True)
        sel = " selected" if s == current_sub else ""
        opts.append(f'<option value="{u}"{sel}>{html.escape(s)}</option>')
    return ('<select onchange="if(this.value)location=this.value">'
            + "".join(opts) + "</select>")


def _category_bar(seller_id, all_live, dept, active_cat) -> str:
    """Category pills within the current department scope."""
    cats, cat_dept = [], {}
    for r in all_live:
        if r["category"] and (not dept or r["department"] == dept):
            if r["category"] not in cats:
                cats.append(r["category"]); cat_dept[r["category"]] = r["department"]
    if not cats:
        return ""
    pills = [_pill("All categories", _url(seller_id, dept=dept), not active_cat)]
    for c in cats:
        pills.append(_pill(c, _url(seller_id, dept=cat_dept[c], cat=c), c == active_cat))
    return f'<nav>{"".join(pills)}</nav>'


def render_profile(seller_id, dept=None, cat=None, sub=None, attr_filters=None):
    prof = seller_profile(seller_id)
    if prof is None:
        return None
    attr_filters = attr_filters or []
    name = html.escape(prof["name"] or "Shop")
    photos = seller_photo_rows(seller_id)
    tagged = seller_tagged_departments(seller_id)
    all_live = seller_live_products(seller_id)

    depts_present, cat_dept, cat_cid = [], {}, {}
    for r in all_live:
        if r["department"] not in depts_present:
            depts_present.append(r["department"])
        if r["category"]:
            cat_dept.setdefault(r["category"], r["department"])
            cat_cid.setdefault(r["category"], r["category_id"])

    # ── header ──
    if photos:
        photos_block = ('<div class="photos">'
                        + "".join(f'<img src="/seller-photo/{ph["id"]}" alt="shop photo">'
                                  for ph in photos) + '</div>')
    else:
        photos_block = '<div class="noshop">No shop photos yet.</div>'
    tags_html = "".join(f'<span class="tag">{html.escape(d)}</span>' for d in tagged)
    meesho = ""
    if prof["meesho_store_url"]:
        u = html.escape(prof["meesho_store_url"], quote=True)
        meesho = (f' · <a href="{u}" target="_blank" rel="noopener nofollow">'
                  f'Meesho ↗</a>')
    owner = html.escape(prof["owner_name"]) if prof["owner_name"] else ""
    owner_line = f'<div class="owner">Run by {owner}</div>' if owner else ""
    header = (f'<div class="shop">{photos_block}<div class="info"><h1>{name}</h1>'
              f'{owner_line}'
              f'<div class="tags">{tags_html}</div>'
              f'<div class="contact">Contact: {html.escape(prof["contact"] or "")}{meesho}</div>'
              f'</div></div>')

    # ── department filter bar ──
    dpills = [_pill("All", _url(seller_id), not dept)]
    for d in depts_present:
        dpills.append(_pill(d, _url(seller_id, dept=d), d == dept and not cat))
    dept_bar = f'<nav>{"".join(dpills)}</nav>' if depts_present else ""

    # ── content ──
    if not all_live:
        content = '<div class="empty">No products listed yet.</div>'
    elif cat:
        # Drill-down: one category, with subcategory dropdown + attribute pills.
        d = dept or cat_dept.get(cat)
        cat_rows = [r for r in all_live if r["category"] == cat
                    and (not dept or r["department"] == dept)]
        subs = list(dict.fromkeys(r["subcategory"] for r in cat_rows if r["subcategory"]))

        controls = [f'<div class="filters"><span class="label">'
                    f'{html.escape(cat).capitalize()}</span>']
        if subs:
            controls.append('<span class="label">subcategory</span>')
            controls.append(_subcat_select(seller_id, d, cat, subs, sub))
        current = dict(attr_filters)
        cid = cat_cid.get(cat)
        attr_names = [a["name"] for a in attributes.get_category_attributes(cid)] if cid else []
        for an in attr_names:
            vals = list(dict.fromkeys(_attrs(r).get(an) for r in cat_rows if _attrs(r).get(an)))
            if not vals:
                continue
            other = [(n, v) for (n, v) in attr_filters if n != an]
            pills = [_pill("Any", _url(seller_id, dept=d, cat=cat, sub=sub, attr_filters=other),
                           an not in current)]
            for val in vals:
                pills.append(_pill(val,
                                   _url(seller_id, dept=d, cat=cat, sub=sub,
                                        attr_filters=other + [(an, val)]),
                                   current.get(an) == val))
            controls.append(f'<span class="label">{html.escape(an)}</span>' + "".join(pills))
        controls.append('</div>')

        display = _apply_attr_filters(seller_live_products(seller_id, d, cat, sub), attr_filters)
        grid = (f'<div class="grid">{"".join(_profile_card(r) for r in display)}</div>'
                if display else '<div class="empty">No matching products.</div>')
        content = _category_bar(seller_id, all_live, d, cat) + "".join(controls) + grid
    else:
        # Overview: grouped by department → category, dropdown per category.
        rows = [r for r in all_live if (not dept or r["department"] == dept)]
        groups, order = {}, []
        for r in rows:
            key = (r["department"], r["category"] or "uncategorized")
            if key not in groups:
                groups[key] = []; order.append(key)
            groups[key].append(r)
        parts = [_category_bar(seller_id, all_live, dept, None)]
        current_dept = None
        for (d, c) in order:
            if d != current_dept:
                parts.append(f'<div class="depthead">{html.escape(d)}</div>')
                current_dept = d
            subs = list(dict.fromkeys(x["subcategory"] for x in groups[(d, c)] if x["subcategory"]))
            head = f'<div class="catrow"><div class="cat">{html.escape(c)}</div>'
            if subs and c != "uncategorized":
                head += _subcat_select(seller_id, d, c, subs, None)
            head += '</div>'
            parts.append(head)
            parts.append(f'<div class="grid">'
                         f'{"".join(_profile_card(r) for r in groups[(d, c)])}</div>')
        content = "".join(parts)

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{name} — PeerShop</title>"
        f"<style>{PAGE_CSS}{PROFILE_CSS}</style></head><body>"
        '<a class="back" href="/">← All products</a>'
        f"{header}{dept_bar}{content}</body></html>")


# ── Seller auth + dashboard (Phase H) ────────────────────────────────────────
SESSION_COOKIE = "peershop_seller"

AUTH_CSS = """
  .authwrap { max-width:440px; margin:32px auto; background:#fff; border:1px solid #eee;
              border-radius:14px; padding:24px 26px; }
  .authwrap h2 { margin:0 0 4px; font-size:20px; }
  .authwrap p.sub { margin:0 0 16px; color:#8a807a; font-size:13px; }
  .authwrap label { display:block; font-size:13px; color:#5a504a; margin:12px 0 4px; }
  .authwrap input[type=text], .authwrap input[type=password] {
      width:100%; padding:9px 11px; border:1px solid #e5ddd8; border-radius:9px; font-size:14px; }
  .depts { display:flex; flex-wrap:wrap; gap:8px; margin-top:6px; }
  .depts label { display:flex; align-items:center; gap:6px; margin:0; font-size:13px;
                 background:#f5efec; padding:6px 10px; border-radius:999px; text-transform:capitalize; }
  .btn { margin-top:18px; width:100%; padding:11px; border:0; border-radius:10px;
         background:#241f1c; color:#fff; font-size:14px; cursor:pointer; }
  .err { background:#fbece8; color:#a3341b; border:1px solid #f3cfc6; padding:9px 12px;
         border-radius:9px; font-size:13px; margin-bottom:6px; }
  .switch { margin-top:14px; font-size:13px; color:#8a807a; text-align:center; }
  .switch a { color:#a3573b; text-decoration:none; }
  .dash { max-width:840px; margin:20px auto; padding:0 20px; }
  .dash .row { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .dash table { width:100%; border-collapse:collapse; background:#fff; border:1px solid #eee;
                border-radius:12px; overflow:hidden; margin-top:14px; }
  .dash th, .dash td { text-align:left; padding:10px 12px; font-size:13px; border-bottom:1px solid #f0eae6; vertical-align:middle; }
  .dash th { background:#faf7f5; color:#8a807a; font-weight:600; }
  .dash form.inline { display:inline-flex; gap:6px; align-items:center; margin:0; }
  .dash input.price { width:84px; padding:5px 7px; border:1px solid #e5ddd8; border-radius:7px; }
  .dash button.mini { padding:5px 10px; border:0; border-radius:7px; cursor:pointer; font-size:12px; }
  .dash button.save { background:#2f6b4f; color:#fff; }
  .dash button.rm { background:#a3341b; color:#fff; }
  .dash button.plain { background:#efe7e2; color:#5a504a; }
  .pill { font-size:11px; padding:2px 8px; border-radius:999px; text-transform:capitalize; }
  .pill.live { background:#e2f0e8; color:#2f6b4f; } .pill.pending { background:#f6efe0; color:#8a6d1b; }
  .pill.rejected { background:#fbece8; color:#a3341b; } .pill.removed { background:#eee; color:#888; }
  .notice { background:#eef6f0; color:#2f6b4f; border:1px solid #cfe6d8; padding:9px 12px;
            border-radius:9px; font-size:13px; margin-top:12px; }
  .dash .thumb { width:44px; height:44px; object-fit:cover; border-radius:8px; background:#f0ebe8; }
  .dash input[type=file] { font-size:11px; max-width:150px; }
  .addform { background:#fff; border:1px solid #eee; border-radius:12px; padding:16px 18px; margin-top:14px; }
  .addform h3 { margin:0 0 10px; font-size:15px; }
  .addform input[type=text], .addform select, .addform textarea {
      padding:8px 10px; border:1px solid #e5ddd8; border-radius:8px; font-size:13px; }
  .addform .addgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }
  .addform textarea { width:100%; margin-top:8px; min-height:52px; box-sizing:border-box; }
  .addform .addrow { display:flex; gap:10px; align-items:center; margin-top:8px; flex-wrap:wrap; }
  .addform .addphotos { margin-top:10px; border-top:1px solid #f0ebe8; padding-top:8px; }
  .addform .photolabel { display:block; font-size:12px; color:#6a605a; margin-bottom:2px; }
  .hint { font-size:12px; color:#9a908a; margin:8px 0 0; }
  .uploader { display:inline-flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .uploader .u-name { font-size:11px; color:#6a605a; max-width:130px; overflow:hidden;
                      text-overflow:ellipsis; white-space:nowrap; }
"""


def _auth_page(title: str, body_html: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title><style>{PAGE_CSS}{AUTH_CSS}</style></head><body>"
        "<header><h1><a href='/'>PeerShop</a></h1><p>Seller area</p></header>"
        f"{body_html}</body></html>")


def _dept_checkboxes(selected=()) -> str:
    out = []
    for nm in onboarding.launch_departments():   # the 5 launch depts, not buckets
        chk = " checked" if nm in selected else ""
        out.append(f'<label><input type="checkbox" name="department" '
                   f'value="{html.escape(nm, quote=True)}"{chk}>{html.escape(nm)}</label>')
    return '<div class="depts">' + "".join(out) + "</div>"


def render_signup(error: str = "", prev: dict | None = None) -> str:
    prev = prev or {}
    e = f'<div class="err">{html.escape(error)}</div>' if error else ""
    def v(k):
        return html.escape(prev.get(k, ""), quote=True)
    body = (
        '<div class="authwrap"><h2>Create your seller account</h2>'
        '<p class="sub">List your shop on PeerShop.</p>'
        f'{e}<form method="post" action="/seller/signup">'
        f'<label>Shop name</label><input type="text" name="name" value="{v("name")}" required>'
        "<label>Owner's name (buyers may know your shop by this)</label>"
        f'<input type="text" name="owner_name" value="{v("owner_name")}">'
        '<label>Contact number (how buyers reach you)</label>'
        f'<input type="text" name="contact" value="{v("contact")}" required>'
        '<label>Shop pincode (6 digits — helps nearby buyers find you; optional)</label>'
        f'<input type="text" name="pincode" value="{v("pincode")}" inputmode="numeric" '
        'pattern="[1-9][0-9]{5}" placeholder="e.g. 700091">'
        f'<label>Login username</label><input type="text" name="login" value="{v("login")}" required>'
        '<label>Password (min 8 characters)</label><input type="password" name="password" required>'
        f'<label>Departments (pick one or more)</label>{_dept_checkboxes(prev.get("departments", []))}'
        '<label class="others-toggle" style="margin-top:10px;display:flex;'
        'align-items:center;gap:6px;font-size:13px">'
        f'<input type="checkbox" id="othersChk" name="others_on" value="1"'
        f'{" checked" if prev.get("others_line") else ""}> '
        "Others — my line isn't listed above</label>"
        f'<div id="othersBox" style="display:{"block" if prev.get("others_line") else "none"}">'
        '<input type="text" name="others_line" placeholder="e.g. jeans, hardware, '
        f'pet supplies" value="{v("others_line")}"></div>'
        '<p class="sub" style="margin-top:10px">Not sure? Leave these blank — we\'ll '
        'list you as <b>Uncategorized</b> until you decide. Type a line under '
        '<b>Others</b> and we\'ll place you automatically (or send it for approval).</p>'
        '<button class="btn" type="submit">Create account</button></form>'
        '<div class="switch">Already have an account? '
        '<a href="/seller/login">Log in</a></div></div>'
        '<script>var _c=document.getElementById("othersChk"),'
        '_b=document.getElementById("othersBox");'
        'if(_c)_c.addEventListener("change",function(){'
        '_b.style.display=_c.checked?"block":"none";});</script>')
    return _auth_page("Sign up — PeerShop", body)


def render_login(error: str = "") -> str:
    e = f'<div class="err">{html.escape(error)}</div>' if error else ""
    body = (
        '<div class="authwrap"><h2>Seller login</h2>'
        '<p class="sub">Manage your shop.</p>'
        f'{e}<form method="post" action="/seller/login">'
        '<label>Login username</label><input type="text" name="login" required>'
        '<label>Password</label><input type="password" name="password" required>'
        '<button class="btn" type="submit">Log in</button></form>'
        '<div class="switch">New here? '
        '<a href="/seller/signup">Create an account</a></div></div>')
    return _auth_page("Login — PeerShop", body)


def _photo_input() -> str:
    """
    A photo picker offering BOTH sources: 'From device' (storage) and 'Camera'
    (mobile capture via the HTML `capture` attribute). Two hidden file inputs
    named `image` (so the backend is unchanged); _UPLOADER_JS wires the buttons
    and keeps only one selection active. Presence is validated server-side.
    """
    return (
        '<span class="uploader">'
        '<input type="file" name="image" accept="image/*" class="u-file" style="display:none">'
        '<input type="file" name="image" accept="image/*" capture="environment" '
        'class="u-cam" style="display:none">'
        '<button type="button" class="mini plain u-pick">From device</button>'
        '<button type="button" class="mini plain u-camera">📷 Camera</button>'
        '<span class="u-name"></span></span>')


# One delegated handler wires every .uploader on the page (add form + each row):
# a button triggers its file input; picking from one source clears the other so
# only a single `image` part is submitted; the chosen filename is shown.
_UPLOADER_JS = (
    "<script>"
    "document.addEventListener('click',function(e){"
    "var b=e.target.closest&&e.target.closest('.u-pick,.u-camera');if(!b)return;"
    "var box=b.closest('.uploader');"
    "box.querySelector(b.classList.contains('u-camera')?'.u-cam':'.u-file').click();});"
    "document.addEventListener('change',function(e){"
    "var t=e.target;if(!t.closest)return;var box=t.closest('.uploader');if(!box)return;"
    "if(t.classList.contains('u-file')&&t.files.length)box.querySelector('.u-cam').value='';"
    "if(t.classList.contains('u-cam')&&t.files.length)box.querySelector('.u-file').value='';"
    "if(t.files&&t.files.length)box.querySelector('.u-name').textContent=t.files[0].name;});"
    "</script>")


def render_dashboard(seller_id: int, csrf: str, notice: str = "",
                     tg_code: str = "") -> str:
    with _conn() as conn:
        srow = conn.execute("SELECT name, pincode FROM sellers WHERE id = ?",
                           (seller_id,)).fetchone()
        depts = [r[0] for r in conn.execute(
            "SELECT d.name FROM seller_departments sd JOIN departments d "
            "ON d.id = sd.department_id WHERE sd.seller_id = ? ORDER BY d.id",
            (seller_id,))]
    name = html.escape(srow["name"] if srow else "Seller")
    csrf_h = html.escape(csrf, quote=True)
    prods = seller_actions.get_my_products(seller_id, include_removed=True)

    rows = []
    for p in prods:
        st = p["status"]
        price = "" if p["price"] is None else f'{p["price"]:.0f}'
        cat = f'/{html.escape(p["category"])}' if p["category"] else ""
        thumb = f'<img class="thumb" src="/seller/product/{p["id"]}/image" alt="">'
        actions = "—"
        if st != "removed":
            actions = (
                '<form class="inline" method="post" action="/seller/product/price">'
                f'<input type="hidden" name="csrf" value="{csrf_h}">'
                f'<input type="hidden" name="product_id" value="{p["id"]}">'
                f'<input class="price" type="text" name="price" value="{price}" placeholder="price">'
                '<button class="mini save" type="submit">Save price</button></form> '
                '<form class="inline" method="post" action="/seller/product/image" '
                'enctype="multipart/form-data">'
                f'<input type="hidden" name="csrf" value="{csrf_h}">'
                f'<input type="hidden" name="product_id" value="{p["id"]}">'
                f'{_photo_input()}'
                '<button class="mini plain" type="submit">Replace photo</button></form> '
                '<form class="inline" method="post" action="/seller/product/remove" '
                'onsubmit="return confirm(\'Remove this listing?\')">'
                f'<input type="hidden" name="csrf" value="{csrf_h}">'
                f'<input type="hidden" name="product_id" value="{p["id"]}">'
                '<button class="mini rm" type="submit">Remove</button></form>')
        rows.append(
            f'<tr><td>{thumb}</td><td>#{p["id"]}</td><td>{html.escape(p["title"] or "")}</td>'
            f'<td>{html.escape(p["department"] or "")}{cat}</td>'
            f'<td><span class="pill {st}">{st}</span></td>'
            f'<td>{actions}</td></tr>')
    table = (
        '<table><tr><th></th><th>ID</th><th>Title</th><th>Dept/Category</th>'
        '<th>Status</th><th>Actions</th></tr>'
        + ("".join(rows) if rows else '<tr><td colspan="6">No listings yet.</td></tr>')
        + "</table>")

    dept_opts = "".join(f'<option value="{html.escape(d, quote=True)}">{html.escape(d)}'
                        f'</option>' for d in depts)
    add_form = (
        '<form class="addform" method="post" action="/seller/product/add" '
        'enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf" value="{csrf_h}">'
        '<h3>Add a product</h3>'
        '<div class="addgrid">'
        '<input type="text" name="title" placeholder="Title" required>'
        '<input type="text" name="price" placeholder="Price (optional)">'
        f'<select name="department">{dept_opts}</select>'
        '<input type="text" name="category" placeholder="Category (optional)">'
        '</div>'
        '<textarea name="description" placeholder="Description (optional)"></textarea>'
        '<div class="addphotos">'
        '<label class="photolabel">Photos — up to 4 (the first is the main image; '
        'at least one is required)</label>'
        + "".join(f'<div class="addrow">{_photo_input()}</div>'
                  for _ in range(MAX_PHOTOS)) +
        '</div>'
        '<div class="addrow"><button class="mini save" type="submit">Post product'
        '</button></div>'
        '<p class="hint">New posts start as <b>pending</b> admin review; photos '
        'are shown to buyers as uploaded. An unknown category is queued for approval.'
        '</p></form>')

    n = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    logout = ('<form method="post" action="/seller/logout" style="margin:0">'
              f'<input type="hidden" name="csrf" value="{csrf_h}">'
              '<button class="mini plain" type="submit">Log out</button></form>')
    tg = _telegram_section(seller_id, csrf_h, tg_code)
    cur_pin = html.escape((srow["pincode"] if srow else "") or "", quote=True)
    pin_block = (
        '<div class="dash-block"><h3>Shop location</h3>'
        '<p class="hint" style="margin:0 0 8px">Set your 6-digit pincode so nearby '
        'buyers see your shop first when they search by location.</p>'
        '<form class="inline" method="post" action="/seller/pincode">'
        f'<input type="hidden" name="csrf" value="{csrf_h}">'
        f'<input class="price" type="text" name="pincode" value="{cur_pin}" '
        'inputmode="numeric" pattern="[1-9][0-9]{5}" placeholder="e.g. 700091">'
        '<button class="mini save" type="submit">Save pincode</button></form></div>')
    body = (
        f'<div class="dash"><div class="row"><h2>{name} — your store</h2>{logout}</div>'
        '<p style="color:#8a807a;font-size:13px;line-height:1.5">Add a product, edit '
        'a price (a price change keeps a live listing live), replace a photo, or '
        'remove a listing. '
        f'<a href="/seller/{seller_id}" style="color:#a3573b;text-decoration:none">'
        'View your public profile →</a></p>'
        f'{n}{pin_block}{tg}{add_form}{table}</div>{_UPLOADER_JS}')
    return _auth_page(f"{name} — dashboard", body)


def _telegram_section(seller_id: int, csrf_h: str, tg_code: str) -> str:
    """Dashboard block to link/unlink a Telegram account for the seller-control bot."""
    linked = telegram_link.get_telegram_for_seller(seller_id)
    if linked:
        return (
            '<div class="addform"><h3>Telegram control bot</h3>'
            '<p class="hint" style="margin:0 0 8px">Linked ✓ — you can manage this '
            'store by chatting the PeerShop bot (e.g. “change the price of X to 250”).</p>'
            '<form class="inline" method="post" action="/seller/telegram/unlink">'
            f'<input type="hidden" name="csrf" value="{csrf_h}">'
            '<button class="mini rm" type="submit">Unlink Telegram</button></form></div>')
    if tg_code:
        deep = (f'https://t.me/{html.escape(TG_BOT_USERNAME, quote=True)}'
                f'?start={html.escape(tg_code, quote=True)}') if TG_BOT_USERNAME else ""
        btn = (f'<a class="wa-btn" style="display:inline-block;text-decoration:none;'
               f'background:#229ED9;width:auto" href="{deep}" target="_blank" '
               f'rel="noopener">Open the bot to link →</a> ' if deep else "")
        return (
            '<div class="addform"><h3>Telegram control bot</h3>'
            f'{btn}'
            '<p class="hint" style="margin:8px 0 0">In the PeerShop bot on Telegram, '
            f'send this within 10 minutes:<br><code>/start {html.escape(tg_code)}</code>'
            '</p></div>')
    return (
        '<div class="addform"><h3>Telegram control bot</h3>'
        '<p class="hint" style="margin:0 0 8px">Link your Telegram to manage this '
        'store by chatting a bot in plain language.</p>'
        '<form class="inline" method="post" action="/seller/telegram/link">'
        f'<input type="hidden" name="csrf" value="{csrf_h}">'
        '<button class="mini save" type="submit">Link Telegram</button></form></div>')


# 1×1 transparent PNG, used when an image_path is missing on disk.
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c62000100000500010d0a2db40000000049454e44ae426082"
)


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # keep the console quiet
        pass

    def _client_ip(self) -> str:
        """Real client IP for rate limiting. Behind the Cloudflare tunnel the
        socket peer is localhost, so prefer CF-Connecting-IP (set by Cloudflare,
        not client-spoofable when traffic arrives via the tunnel), then the first
        X-Forwarded-For hop, else the raw peer address."""
        cf = self.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _security_headers(self):
        # Applied to every response. These don't rely on inline-script nonces, so
        # they don't break the site's existing inline <script>/<style>.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; "
                         "form-action 'self'")

    def _send(self, code, body, content_type, set_cookie=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ── seller-session helpers (Phase H) ────────────────────────────────────
    def _current_session(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            ck = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        morsel = ck.get(SESSION_COOKIE)
        if not morsel:
            return None
        res = auth.validate_session(morsel.value)
        if not res:
            return None
        return {"token": morsel.value, "seller_id": res[0], "csrf": res[1]}

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        return urllib.parse.parse_qs(body.decode("utf-8", "replace"))

    def _parse_multipart(self):
        """Minimal stdlib multipart/form-data parser (cgi is gone in 3.13).
        Returns (fields: {name: [str]}, files: {name: {filename, content_type, data}})."""
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return {}, {}
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_UPLOAD:
            return {}, {}
        body = self.rfile.read(length)
        fields, files = {}, {}
        for part in body.split(b"--" + boundary.encode()):
            if part.startswith(b"\r\n"):
                part = part[2:]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            if not part or part == b"--" or b"\r\n\r\n" not in part:
                continue
            raw_headers, _, content = part.partition(b"\r\n\r\n")
            disp, file_ctype = {}, ""
            for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
                low = line.lower()
                if low.startswith("content-disposition:"):
                    for tok in line.split(";")[1:]:
                        if "=" in tok:
                            k, v = tok.split("=", 1)
                            disp[k.strip().lower()] = v.strip().strip('"')
                elif low.startswith("content-type:"):
                    file_ctype = line.split(":", 1)[1].strip()
            name = disp.get("name")
            if not name:
                continue
            if "filename" in disp:
                if disp["filename"]:
                    # Same field name may repeat (e.g. up to 4 `image` parts) → list.
                    files.setdefault(name, []).append(
                        {"filename": disp["filename"],
                         "content_type": file_ctype, "data": content})
            else:
                fields.setdefault(name, []).append(content.decode("utf-8", "replace"))
        return fields, files

    def _save_upload(self, seller_id, fileobj):
        """Validate + persist an uploaded image to media/products. Returns the path."""
        ext = os.path.splitext(fileobj["filename"])[1].lower()
        if ext not in _IMG_EXT:
            ext = _CTYPE_EXT.get((fileobj.get("content_type") or "").lower(), "")
        if ext not in _IMG_EXT:
            raise ValueError("please upload a JPG, PNG, GIF, or WEBP image")
        if not fileobj["data"]:
            raise ValueError("the uploaded image was empty")
        os.makedirs(PRODUCT_MEDIA, exist_ok=True)
        path = os.path.join(PRODUCT_MEDIA, f"seller{seller_id}_{os.urandom(6).hex()}{ext}")
        with open(path, "wb") as f:
            f.write(fileobj["data"])
        return path

    def _redirect(self, location, set_cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self._security_headers()
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session_cookie(self, token):
        # Secure: the site is served over HTTPS via the Cloudflare tunnel, so the
        # browser must never send this cookie over a plaintext connection.
        return (f"{SESSION_COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/; "
                f"Max-Age={auth.SESSION_TTL_DAYS * 86400}")

    def _clear_cookie(self):
        return f"{SESSION_COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"

    # Public entry points wrap the routers so a bug in any one handler returns a
    # clean 500 and is logged — it can never take the server thread/process down.
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass          # client hung up mid-response — not a server error
        except Exception:
            traceback.print_exc()
            self._try_500()

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self._try_500()

    def _try_500(self):
        try:
            self._send(500, "Sorry — something went wrong. Please try again.",
                       "text/plain")
        except Exception:
            pass

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/health":
            return self._send(200, "ok", "text/plain")

        if path == "/":
            qs = urllib.parse.parse_qs(parsed.query)
            category = qs.get("category", [None])[0]
            query = qs.get("q", [None])[0]
            loc = qs.get("loc", [None])[0]
            return self._send(200, render_listing(category, query, loc),
                              "text/html; charset=utf-8")

        # Seller auth pages (must precede the public /seller/<id> profile route).
        if path == "/seller/signup":
            return self._send(200, render_signup(), "text/html; charset=utf-8")
        if path == "/seller/login":
            return self._send(200, render_login(), "text/html; charset=utf-8")
        if path == "/seller/dashboard":
            sess = self._current_session()
            if not sess:
                return self._redirect("/seller/login")
            return self._send(200, render_dashboard(sess["seller_id"], sess["csrf"]),
                              "text/html; charset=utf-8")

        # A logged-in seller's own product image (any status) — for the dashboard.
        if path.startswith("/seller/product/") and path.endswith("/image"):
            sess = self._current_session()
            if not sess:
                return self._send(403, "forbidden", "text/plain")
            try:
                pid = int(path[len("/seller/product/"):-len("/image")])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            prod = seller_actions.get_product(pid)
            if not prod or prod["seller_id"] != sess["seller_id"]:
                return self._send(404, "not found", "text/plain")
            img = prod["image_path"]
            if img and os.path.exists(img):
                ctype = mimetypes.guess_type(img)[0] or "application/octet-stream"
                with open(img, "rb") as f:
                    return self._send(200, f.read(), ctype)
            return self._send(200, _PLACEHOLDER_PNG, "image/png")

        if path.startswith("/seller-photo/"):
            try:
                phid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            img = seller_photo_path(phid)
            if img and os.path.exists(img):
                ctype = mimetypes.guess_type(img)[0] or "application/octet-stream"
                with open(img, "rb") as f:
                    return self._send(200, f.read(), ctype)
            return self._send(200, _PLACEHOLDER_PNG, "image/png")

        if path.startswith("/seller/"):
            try:
                sid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            qs = urllib.parse.parse_qs(parsed.query)
            attr_filters = []
            for raw in qs.get("af", []):
                if ":" in raw:
                    n, v = raw.split(":", 1)
                    attr_filters.append((n, v))
            page = render_profile(
                sid, qs.get("dept", [None])[0], qs.get("cat", [None])[0],
                qs.get("sub", [None])[0], attr_filters)
            if page is None:
                return self._send(404, "Seller not found", "text/plain")
            return self._send(200, page, "text/html; charset=utf-8")

        if path.startswith("/product/") and not path.endswith("/chat"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            p = live_product(pid)
            if p is None:
                return self._send(404, "Product not available", "text/plain")
            base = f"http://{self.headers.get('Host') or f'localhost:{PORT}'}"
            return self._send(200, render_detail(p, base), "text/html; charset=utf-8")

        if path.startswith("/image/"):
            # /image/<id>  → primary photo; /image/<id>/<n> → the nth gallery photo.
            parts = path[len("/image/"):].split("/")
            try:
                pid = int(parts[0])
                idx = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
            except (ValueError, IndexError):
                return self._send(404, "not found", "text/plain")
            paths = gallery_paths_for(pid)
            img = paths[idx] if 0 <= idx < len(paths) else None
            if img and os.path.exists(img):
                ctype = mimetypes.guess_type(img)[0] or "application/octet-stream"
                with open(img, "rb") as f:
                    return self._send(200, f.read(), ctype)
            # Live product with a missing file, or unknown id/index → placeholder.
            return self._send(200, _PLACEHOLDER_PNG, "image/png")

        return self._send(404, "not found", "text/plain")

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # DoS guard: reject oversized non-upload bodies before we read them.
        # Multipart uploads have their own MAX_UPLOAD cap in _parse_multipart.
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            try:
                clen = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                return self._send(400, "bad request", "text/plain")
            if clen > MAX_FORM_BODY:
                return self._send(413, "request body too large", "text/plain")

        # ── Seller auth + dashboard actions (Phase H) ───────────────────────
        if path == "/seller/signup":
            form = self._read_form()
            name = form.get("name", [""])[0].strip()
            owner_name = form.get("owner_name", [""])[0].strip()
            contact = form.get("contact", [""])[0].strip()
            login = form.get("login", [""])[0].strip()
            password = form.get("password", [""])[0]
            pincode = form.get("pincode", [""])[0].strip()
            selected = [d.strip() for d in form.get("department", []) if d.strip()]
            others_line = form.get("others_line", [""])[0].strip()
            prev = {"name": name, "owner_name": owner_name, "contact": contact,
                    "login": login, "pincode": pincode, "departments": selected,
                    "others_line": others_line}
            try:
                if not name or not contact:
                    raise auth.AuthError("shop name and contact are required")
                try:
                    pincode = onboarding.normalize_pincode(pincode)
                except ValueError as e:
                    raise auth.AuthError(str(e))
                auth.validate_credentials(login, password)
                if not auth.is_login_available(login):
                    raise auth.AuthError(f"login '{login.lower()}' is already taken")

                # Resolve departments: picks + a semantic match on the 'Others' line,
                # falling back to the 'others' bucket (pending) or 'uncategorized'.
                confirmed = list(selected)
                notices, pending_name = [], None
                if others_line:
                    m = onboarding.match_department(others_line)
                    if m["department"]:
                        if m["department"] not in confirmed:
                            confirmed.append(m["department"])
                        notices.append(
                            f"We matched “{others_line}” to "
                            f"{m['department'].capitalize()}.")
                    else:
                        if "others" not in confirmed:
                            confirmed.append("others")
                        pending_name = others_line
                if not confirmed:
                    confirmed = ["uncategorized"]
                    notices.append("We listed you under “Uncategorized” for now — "
                                   "you can set your department anytime.")

                seller_id = onboarding.onboard(name, contact, owner_name=owner_name,
                                               pincode=pincode,
                                               departments=confirmed)["seller_id"]
                auth.set_credentials(seller_id, login, password)
                if pending_name:
                    onboarding.request_department(seller_id, pending_name, context=name)
                    notices.append(
                        f"“{pending_name}” is pending admin approval — buyers see you "
                        "under “Others” until it's approved.")
                token, csrf = auth.create_session(seller_id)
            except (auth.AuthError, ValueError, seller_actions.SellerActionError) as e:
                return self._send(200, render_signup(str(e), prev),
                                  "text/html; charset=utf-8")
            # Show the seller what happened (matches / Others / Uncategorized).
            return self._send(
                200, render_dashboard(seller_id, csrf, " ".join(notices)),
                "text/html; charset=utf-8", set_cookie=self._session_cookie(token))

        if path == "/seller/login":
            form = self._read_form()
            seller_id = auth.verify_login(form.get("login", [""])[0],
                                          form.get("password", [""])[0])
            if not seller_id:
                return self._send(200, render_login("Invalid login or password."),
                                  "text/html; charset=utf-8")
            token, _csrf = auth.create_session(seller_id)
            return self._redirect("/seller/dashboard", self._session_cookie(token))

        if path == "/seller/logout":
            sess = self._current_session()
            form = self._read_form()
            if sess and hmac.compare_digest(form.get("csrf", [""])[0], sess["csrf"]):
                auth.revoke_session(sess["token"])
                return self._redirect("/seller/login", self._clear_cookie())
            return self._redirect("/seller/login")

        # Telegram control-bot linking (auth = logged-in session + CSRF).
        if path in ("/seller/telegram/link", "/seller/telegram/unlink"):
            sess = self._current_session()
            if not sess:
                return self._redirect("/seller/login")
            form = self._read_form()
            if not hmac.compare_digest(form.get("csrf", [""])[0], sess["csrf"]):
                return self._send(400, "bad request", "text/plain")
            code, notice = "", ""
            if path.endswith("/link"):
                code = telegram_link.create_link_code(sess["seller_id"])
                notice = "Telegram link code created — open the bot to finish."
            else:
                telegram_link.unlink(sess["seller_id"])
                notice = "Telegram unlinked."
            return self._send(
                200, render_dashboard(sess["seller_id"], sess["csrf"], notice, code),
                "text/html; charset=utf-8")

        # Set/clear the seller's shop pincode (auth = session + CSRF).
        if path == "/seller/pincode":
            sess = self._current_session()
            if not sess:
                return self._redirect("/seller/login")
            form = self._read_form()
            if not hmac.compare_digest(form.get("csrf", [""])[0], sess["csrf"]):
                return self._send(400, "bad request", "text/plain")
            try:
                pin = onboarding.set_pincode(sess["seller_id"],
                                             form.get("pincode", [""])[0])
                notice = f"Shop pincode set to {pin}." if pin else "Shop pincode cleared."
            except ValueError as e:
                notice = str(e)
            return self._send(
                200, render_dashboard(sess["seller_id"], sess["csrf"], notice),
                "text/html; charset=utf-8")

        # Multipart uploads: add a product (with photo) / replace a photo.
        if path in ("/seller/product/add", "/seller/product/image"):
            sess = self._current_session()
            if not sess:
                return self._redirect("/seller/login")
            fields, files = self._parse_multipart()
            if not hmac.compare_digest(fields.get("csrf", [""])[0], sess["csrf"]):
                return self._send(400, "bad request", "text/plain")
            notice = ""
            try:
                imgs = files.get("image") or []
                if path.endswith("/add"):
                    saved = [self._save_upload(sess["seller_id"], f)
                             for f in imgs[:MAX_PHOTOS]]
                    if not saved:
                        raise seller_actions.SellerActionError(
                            "please add at least one photo (JPG/PNG/GIF/WEBP)")
                    r = seller_actions.post_product(
                        sess["seller_id"],
                        fields.get("title", [""])[0].strip(),
                        fields.get("description", [""])[0].strip() or None,
                        saved[0],
                        department=fields.get("department", [""])[0].strip() or None,
                        category=fields.get("category", [""])[0].strip() or None,
                        price=fields.get("price", [""])[0].strip() or None,
                        photos=saved)
                    notice = (f"Posted “{r['title']}” (#{r['id']}) with {len(saved)} "
                              "photo(s) — pending admin review before it goes live.")
                else:
                    if not imgs:
                        raise seller_actions.SellerActionError(
                            "please choose an image (JPG/PNG/GIF/WEBP)")
                    pid = int(fields.get("product_id", ["0"])[0])
                    img_path = self._save_upload(sess["seller_id"], imgs[0])
                    r = seller_actions.update_image(sess["seller_id"], pid, img_path)
                    notice = (f"Updated the photo for “{r['title']}” — back to "
                              "pending review.")
            except (seller_actions.SellerActionError, ValueError) as e:
                notice = str(e)
            return self._send(
                200, render_dashboard(sess["seller_id"], sess["csrf"], notice),
                "text/html; charset=utf-8")

        if path in ("/seller/product/remove", "/seller/product/price"):
            sess = self._current_session()
            if not sess:
                return self._redirect("/seller/login")
            form = self._read_form()
            if not hmac.compare_digest(form.get("csrf", [""])[0], sess["csrf"]):
                return self._send(400, "bad request", "text/plain")
            try:
                pid = int(form.get("product_id", [""])[0])
            except ValueError:
                return self._redirect("/seller/dashboard")
            notice = ""
            try:
                if path.endswith("/remove"):
                    r = seller_actions.remove_product(sess["seller_id"], pid)
                    notice = f"Removed “{r['title']}”."
                else:
                    price = form.get("price", [""])[0].strip()
                    if not price:
                        raise seller_actions.SellerActionError("enter a price")
                    r = seller_actions.update_price(sess["seller_id"], pid, price)
                    notice = f"Updated “{r['title']}” to ₹{r['price']:.0f}."
            except seller_actions.SellerActionError as e:
                notice = str(e)
            return self._send(200,
                              render_dashboard(sess["seller_id"], sess["csrf"], notice),
                              "text/html; charset=utf-8")

        # POST /product/<id>/chat  → one narrow, stateless answer
        if path.startswith("/product/") and path.endswith("/chat"):
            try:
                pid = int(path[len("/product/"):-len("/chat")])
            except ValueError:
                return self._send_json(404, {"error": "not found"})
            if not rate_limit_ok(self._client_ip()):
                return self._send_json(
                    429, {"error": "Too many requests. Please wait a minute "
                                   "before asking again."})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                question = body.get("question", "")
            except (ValueError, json.JSONDecodeError):
                return self._send_json(400, {"error": "bad request"})
            if not isinstance(question, str):
                return self._send_json(400, {"error": "bad request"})
            question = question[:MAX_QUESTION]        # cap prompt size (cost/DoS guard)
            try:
                answer = chat.answer_question(pid, question)
                return self._send_json(200, {"answer": answer})
            except chat.ProductNotAvailable:
                return self._send_json(404, {"error": "product not available"})
            except Exception:
                # Free-tier hiccups (rate limit, timeout) shouldn't 500 the page.
                return self._send_json(
                    502, {"error": "The assistant is unavailable right now. "
                                   "Please try again in a moment."})

        return self._send_json(404, {"error": "not found"})

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PeerShop listing on http://{HOST}:{PORT}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
