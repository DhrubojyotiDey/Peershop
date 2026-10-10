#!/usr/bin/env python3
"""
PeerShop — manual review / promotion queue (admin-facing).

The gate between 'pending' and 'live'. Nothing auto-promotes; a human admin looks
at each pending listing and explicitly makes it live (or rejects it). All decisions
route through approvals.py — the SAME path Hermes uses from the CLI when no human
admin is around — so the guards/governance/seller-notification are identical.

Kept SEPARATE from serve.py (the public server) and on its own port so
promote/reject is never reachable from the public listing.

Access: gated by a single shared admin password (PEERSHOP_ADMIN_PASSWORD, env or
/root/.hermes/.env). A correct password sets a signed HttpOnly cookie; every other
route requires it. Plain HTTP on the LAN (a dev posture, like the seller site).

Each pending card shows everything needed to judge a listing:
    photo gallery · title · description · department · price · category · seller

Actions (explicit, one product at a time — no bulk approve-all):
    POST /review/<id>/promote   pending → live   (disabled while uncategorized)
    POST /review/<id>/reject    pending → rejected
    POST /review/<id>/category  set/queue a category (governed)

Usage:
    PEERSHOP_ADMIN_PASSWORD=... python3 review.py         # http://localhost:8001
    (or put PEERSHOP_ADMIN_PASSWORD in /root/.hermes/.env; supervise via run_review.sh)
"""

import hashlib
import hmac
import html
import http.cookies
import json
import mimetypes
import os
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import admin_actions
import approvals
import auth
import categorize
import telegram_api
import telegram_link

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")
HERMES_ENV = "/root/.hermes/.env"
PORT = int(os.environ.get("PEERSHOP_REVIEW_PORT", "8001"))
HOST = os.environ.get("PEERSHOP_REVIEW_HOST", "0.0.0.0")
ADMIN_COOKIE = "padmin"


# ── config / auth ──────────────────────────────────────────────────────────────
def _hermes_val(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    if os.path.exists(HERMES_ENV):
        for line in open(HERMES_ENV):
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    return ""


def _admin_password() -> str:
    return _hermes_val("PEERSHOP_ADMIN_PASSWORD")


def _token(pw: str) -> str:
    """Stateless cookie value: HMAC of a fixed label keyed by the admin password."""
    return hmac.new(pw.encode(), b"peershop-admin-v1", hashlib.sha256).hexdigest()


def _is_authed(handler) -> bool:
    pw = _admin_password()
    if not pw:
        return False
    raw = handler.headers.get("Cookie")
    if not raw:
        return False
    try:
        ck = http.cookies.SimpleCookie(raw)
    except http.cookies.CookieError:
        return False
    m = ck.get(ADMIN_COOKIE)
    return bool(m) and hmac.compare_digest(m.value, _token(pw))


# ── Data access ──────────────────────────────────────────────────────────────
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def all_categories_grouped() -> list[tuple]:
    """Every category across ALL departments, grouped for the admin dropdown:
    [(department_name, [(category_id, category_name), ...]), ...]. The admin may
    assign any of these to any product (picking one from another department moves
    the product into that department — see categorize.assign_category_by_id)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT d.name AS dept, c.id, c.name FROM categories c "
            "JOIN departments d ON d.id = c.department_id "
            "ORDER BY d.id, c.name").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["dept"], []).append((r["id"], r["name"]))
    return list(groups.items())


def user_rows() -> list[dict]:
    """Every seller with the data an admin wants at a glance: account, contact,
    login, departments, product counts, Telegram-link status, created, last login,
    and password-change summary (WHEN/by whom — never the password itself)."""
    with _conn() as conn:
        sellers = conn.execute(
            "SELECT id, name, owner_name, contact FROM sellers ORDER BY id").fetchall()
        out = []
        for s in sellers:
            sid = s["id"]
            cred = conn.execute(
                "SELECT login, created_at FROM seller_credentials WHERE seller_id = ?",
                (sid,)).fetchone()
            depts = [r[0] for r in conn.execute(
                "SELECT d.name FROM seller_departments sd JOIN departments d "
                "ON d.id = sd.department_id WHERE sd.seller_id = ? ORDER BY d.id", (sid,))]
            counts = {st: 0 for st in ("live", "pending", "rejected", "removed")}
            for st, ct in conn.execute(
                    "SELECT status, COUNT(*) FROM products WHERE seller_id = ? "
                    "GROUP BY status", (sid,)):
                counts[st] = ct
            tg = conn.execute("SELECT 1 FROM seller_telegram WHERE seller_id = ?",
                              (sid,)).fetchone() is not None
            last_login = conn.execute(
                "SELECT MAX(created_at) FROM seller_sessions WHERE seller_id = ?",
                (sid,)).fetchone()[0]
            changes = conn.execute(
                "SELECT COUNT(*) FROM credential_events WHERE seller_id = ? "
                "AND event = 'changed'", (sid,)).fetchone()[0]
            last_ev = conn.execute(
                "SELECT event, actor, created_at FROM credential_events "
                "WHERE seller_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (sid,)).fetchone()
            out.append({
                "id": sid, "name": s["name"], "owner_name": s["owner_name"],
                "contact": s["contact"], "login": cred["login"] if cred else None,
                "acct_created": cred["created_at"] if cred else None,
                "departments": depts, "counts": counts, "telegram": tg,
                "last_login": last_login, "pw_changes": changes,
                "pw_last": dict(zip(("event", "actor", "at"), last_ev)) if last_ev else None,
            })
        return out


def pending_photo(product_id: int, idx: int) -> str | None:
    """Path to the nth photo of a product (fallback to its image_path). Not status-
    scoped: the queue only lists pending products, but the Live-products takedown
    page needs live images too, and this route is admin-authenticated either way."""
    with _conn() as conn:
        base = conn.execute(
            "SELECT image_path FROM products WHERE id = ?",
            (product_id,)).fetchone()
        if not base:
            return None
        rows = conn.execute(
            "SELECT image_path FROM product_photos WHERE product_id = ? "
            "ORDER BY position, id", (product_id,)).fetchall()
    paths = [r[0] for r in rows] or [base[0]]
    return paths[idx] if 0 <= idx < len(paths) else None


# ── HTML ─────────────────────────────────────────────────────────────────────
CSS = """
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,sans-serif; background:#f4f2ef; color:#241f1c; }
  header { padding:20px 24px; background:#fff; border-bottom:1px solid #e7e1db;
           display:flex; justify-content:space-between; align-items:center; }
  header h1 { margin:0; font-size:19px; }
  header p { margin:4px 0 0; color:#8a807a; font-size:13px; }
  header form { margin:0; }
  .wrap { max-width:860px; margin:0 auto; padding:20px 24px 48px; }
  .row { display:grid; grid-template-columns:160px 1fr; gap:18px; background:#fff;
         border:1px solid #e7e1db; border-radius:12px; padding:14px; margin-bottom:16px; }
  .row .gal img.main { width:160px; height:160px; object-fit:cover; border-radius:8px;
             background:#efe9e4; }
  .thumbs { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
  .thumbs img { width:36px; height:36px; object-fit:cover; border-radius:6px;
                cursor:pointer; border:2px solid transparent; }
  .thumbs img.active { border-color:#2f6b45; }
  .row h3 { margin:0 0 4px; font-size:16px; }
  .row .meta { font-size:12px; color:#9a908a; margin:0 0 8px; }
  .row .meta b { color:#5a504a; font-weight:600; }
  .badge { display:inline-block; font-size:11px; text-transform:uppercase;
           letter-spacing:.04em; padding:3px 9px; border-radius:999px;
           background:#e9f2e9; color:#3f6b45; }
  .badge.none { background:#f6e9e6; color:#9a4a34; }
  .row p.desc { font-size:13px; color:#5a504a; line-height:1.45; margin:8px 0 12px; }
  .actions { display:flex; gap:10px; align-items:center; }
  .actions form { margin:0; }
  button { font-size:13px; padding:8px 16px; border-radius:9px; border:0; cursor:pointer; }
  button.promote { background:#2f6b45; color:#fff; }
  button.promote[disabled] { background:#cfd8cf; color:#8a908a; cursor:default; }
  button.reject { background:#fff; color:#9a4a34; border:1px solid #e6c6bd; }
  button.logout { background:#efe9e4; color:#5a504a; }
  button.rm { background:#fff; color:#9a4a34; border:1px solid #e6c6bd; }
  button.rm:hover { background:#9a4a34; color:#fff; }
  .rmform { display:flex; gap:6px; align-items:center; margin:0; }
  .rmform input.reason { width:220px; padding:6px 9px; border:1px solid #e0d8d2;
                         border-radius:8px; font-size:12px; }
  .livesearch { display:flex; gap:8px; align-items:center; margin:0 0 12px; max-width:560px; }
  .livesearch input { flex:1; padding:8px 11px; border:1px solid #e0d8d2;
                      border-radius:8px; font-size:13px; }
  img.lthumb { width:44px; height:44px; object-fit:cover; border-radius:6px; background:#f0ebe8; }
  .note { font-size:12px; color:#9a4a34; }
  .catrow { display:flex; gap:8px; align-items:flex-start; margin:10px 0; flex-wrap:wrap; }
  .catrow input { padding:6px 9px; border:1px solid #e0d8d2; border-radius:8px; font-size:12px; }
  .catrow input.newcat { width:170px; }
  .rowform { margin:0; }
  .combo { position:relative; display:inline-block; }
  .catsearch { width:200px; }
  .catmenu { position:absolute; z-index:30; left:0; top:calc(100% + 2px); background:#fff;
             border:1px solid #e0d8d2; border-radius:8px; width:250px; max-height:230px;
             overflow-y:auto; box-shadow:0 8px 24px rgba(0,0,0,.14); }
  .catopt { padding:6px 10px; font-size:12px; cursor:pointer; }
  .catopt:hover { background:#f2efe9; }
  .catopt .muted { float:right; }
  button.save { background:#efe9e4; color:#5a504a; }
  button.save.danger { background:#f4d4d0; color:#8a2c20; }
  button.save.danger:hover { background:#edbdb6; }
  button.save[disabled], button.addcat[disabled] { background:#eceae6; color:#bdb5ae;
        cursor:default; opacity:.65; }
  .empty { text-align:center; color:#9a908a; padding:60px 0; }
  .navbar { display:flex; gap:14px; align-items:center; }
  .navlink { color:#2f6b45; text-decoration:none; font-size:13px; font-weight:600; }
  .muted { color:#9a908a; font-size:11px; }
  h2.sub { font-size:15px; margin:26px 0 10px; }
  .scroll { overflow-x:auto; }
  .utable { width:100%; border-collapse:collapse; font-size:12px; background:#fff;
            border:1px solid #e7e1db; border-radius:10px; }
  .utable th { text-align:left; background:#f6f2ee; padding:8px 10px; font-size:11px;
               text-transform:uppercase; letter-spacing:.03em; color:#8a807a;
               border-bottom:1px solid #e7e1db; white-space:nowrap; }
  .utable td { padding:8px 10px; border-bottom:1px solid #f0ebe8; vertical-align:top; }
  .utable form { display:flex; gap:4px; margin:0; }
  .utable input { padding:5px 7px; border:1px solid #e0d8d2; border-radius:7px;
                  font-size:11px; width:120px; }
  .authwrap { max-width:340px; margin:60px auto; background:#fff; border:1px solid #e7e1db;
              border-radius:12px; padding:24px; }
  .authwrap h2 { margin:0 0 12px; font-size:18px; }
  .authwrap input { width:100%; padding:9px 12px; border:1px solid #e0d8d2;
        border-radius:8px; font-size:14px; margin-bottom:10px; }
  .authwrap button { width:100%; background:#2f6b45; color:#fff; }
  .err { color:#9a4a34; font-size:13px; margin-bottom:10px; }
"""

_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c62000100000500010d0a2db40000000049454e44ae426082"
)

_SELECT_JS = (
    "<script>"
    # gallery: swap the main image when a thumbnail is clicked
    "function selp(el){var b=el.closest('.gal');b.querySelector('.main').src=el.src;"
    "b.querySelectorAll('.thumbs img').forEach(function(t){t.classList.remove('active');});"
    "el.classList.add('active');}"
    # card controls: Promote enabled when an EXISTING category is chosen (hidden
    # .catval holds its id); 'Add category' enabled when free text is typed with no
    # existing category selected.
    # 'Add category' enables ONLY when the typed text is a non-empty name that does
    # NOT match any existing category (case-insensitive) — an existing name should
    # be picked from the list, not re-added.
    "function upd(el){var f=el.closest('.rowform');"
    "var catid=f.querySelector('.catval').value;"
    "var txt=f.querySelector('.catsearch').value.trim().toLowerCase();"
    "f.querySelector('.promote').disabled=!catid;"
    "var isNew=txt&&CATSET.indexOf(txt)<0;"
    "f.querySelector('.addcat').disabled=!isNew;}"
    # searchable combobox: filter the menu letter-by-letter as you type.
    "function filt(inp){var box=inp.closest('.combo');var menu=box.querySelector('.catmenu');"
    "var q=inp.value.trim().toLowerCase();menu.hidden=false;"
    "menu.querySelectorAll('.catopt').forEach(function(o){"
    "o.style.display=o.textContent.toLowerCase().indexOf(q)>=0?'':'none';});}"
    # typing invalidates any prior menu selection (now it's a search / new name).
    "function typed(inp){inp.closest('.rowform').querySelector('.catval').value='';"
    "filt(inp);upd(inp);}"
    # confirm before creating a new category from the typed text.
    "function addcat(btn){var name=btn.closest('.rowform').querySelector('.catsearch').value.trim();"
    "if(!name)return false;"
    "return confirm('Add \"'+name+'\" as a new category? It will be created and "
    "assigned to this product.');}"
    "document.addEventListener('click',function(e){"
    "var opt=e.target.closest?e.target.closest('.catopt'):null;"
    "if(opt){var box=opt.closest('.combo');"
    "box.querySelector('.catval').value=opt.getAttribute('data-id');"
    "box.querySelector('.catsearch').value=opt.getAttribute('data-label');"
    "box.querySelector('.catmenu').hidden=true;upd(box.querySelector('.catval'));return;}"
    "if(!(e.target.closest&&e.target.closest('.combo'))){"
    "document.querySelectorAll('.catmenu').forEach(function(m){m.hidden=true;});}});"
    "document.addEventListener('DOMContentLoaded',function(){"
    "document.querySelectorAll('.rowform .catval').forEach(function(v){upd(v);});});"
    "</script>")


def _gallery(p: dict) -> str:
    n = len(p["photos"])
    main = f'<img class="main" src="/image/{p["id"]}/0" alt="">'
    if n <= 1:
        return f'<div class="gal">{main}</div>'
    thumbs = "".join(
        f'<img src="/image/{p["id"]}/{i}"{" class=active" if i == 0 else ""} '
        'onclick="selp(this)">' for i in range(n))
    return f'<div class="gal">{main}<div class="thumbs">{thumbs}</div></div>'


def _controls(p: dict) -> str:
    """One form per card: a category dropdown + a NEW-category box + the actions.

    UX: picking any existing category (≠ uncategorized) enables 'Promote to live'
    (promote applies that category and goes live in one click). 'Save category'
    appears ONLY when a NEW category is typed (governed — unknown names are queued).
    Multiple submit buttons route via formaction to /promote, /category, /reject.
    """
    pid = p["id"]
    cur_id = p["category_id"]
    cur_label = html.escape(p["category"], quote=True) if p["category"] else ""
    # Searchable combobox: a hidden 'category' field holds the chosen category id;
    # the text input filters the menu letter-by-letter (see filt() in _SELECT_JS).
    menu = ['<div class="catopt" data-id="" data-label="">— uncategorized —</div>']
    for dept, cats in all_categories_grouped():
        for cid, cname in cats:
            menu.append(
                f'<div class="catopt" data-id="{cid}" data-label="{html.escape(cname, quote=True)}">'
                f'{html.escape(cname)} <span class="muted">{html.escape(dept.title())}</span></div>')
    dis = "" if cur_id is not None else " disabled"
    return (
        f'<form class="rowform" method="post" action="/review/{pid}/promote">'
        '<div class="catrow">'
        '<span class="combo">'
        f'<input type="hidden" name="category" class="catval" '
        f'value="{cur_id if cur_id is not None else ""}">'
        '<input type="text" name="new_category" class="catsearch" autocomplete="off" '
        f'placeholder="Search or type a category…" value="{cur_label}" '
        'oninput="typed(this)" onfocus="filt(this)" '
        'onkeydown="if(event.key===\'Enter\')event.preventDefault();">'
        '<div class="catmenu" hidden>' + "".join(menu) + '</div>'
        '</span>'
        f'<button class="save addcat" type="submit" formaction="/review/{pid}/category" '
        'onclick="return addcat(this)" disabled>Add category</button>'
        '</div>'
        '<div class="muted" style="margin:-2px 0 6px">Search to pick an existing '
        'category (choosing one from another department moves this item there), or '
        'type a new name and click <b>Add category</b>.</div>'
        '<div class="actions">'
        f'<button class="promote" type="submit"{dis}>Promote to live</button>'
        f'<button class="reject" type="submit" formaction="/review/{pid}/reject">'
        'Reject</button>'
        '</div></form>')


def _row_html(p: dict) -> str:
    title = html.escape(p["title"] or "Untitled")
    desc = html.escape((p["description"] or "").strip()) or "No description provided."
    seller = html.escape(p["seller"] or "")
    dept = html.escape((p["department"] or "").title())
    price = f'₹{p["price"]:.0f}' if p["price"] is not None else "no price"
    has_cat = p["category_id"] is not None
    badge = (f'<span class="badge">{html.escape(p["category"])}</span>' if has_cat
             else '<span class="badge none">uncategorized</span>')
    return (
        '<div class="row">'
        f'{_gallery(p)}'
        '<div>'
        f'<h3>{title}</h3>'
        f'<p class="meta"><b>{seller}</b> · {dept} · <b>{price}</b> · '
        f'{len(p["photos"])} photo(s)</p>'
        f'{badge}'
        f'<p class="desc">{desc}</p>'
        f'{_controls(p)}'
        '</div></div>')


def _catset_js() -> str:
    """Existing category names (lowercased) for the client — 'Add category' is
    disabled when the typed text matches one of these."""
    names = [cname.lower() for _dept, cats in all_categories_grouped()
             for _cid, cname in cats]
    return f"<script>var CATSET={json.dumps(names)};</script>"


def render_queue(notice: str = "") -> str:
    rows = approvals.pending_queue()
    body = "".join(_row_html(p) for p in rows) if rows else \
        '<div class="empty">Nothing pending. All caught up.</div>'
    n = f'<div class="note" style="margin-bottom:12px">{html.escape(notice)}</div>' if notice else ""
    nav = ('<div class="navbar"><a class="navlink" href="/live">Live products →</a>'
           '<a class="navlink" href="/users">Users →</a>'
           '<form method="post" action="/logout">'
           '<button class="logout" type="submit">Log out</button></form></div>')
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>PeerShop — Review</title><style>{CSS}</style></head><body>"
        "<header><div><h1>PeerShop · Review queue</h1>"
        "<p>Pending listings awaiting a human go/no-go. Promoting makes a "
        f"product visible to customers.</p></div>{nav}</header>"
        f'<div class="wrap">{n}{body}</div>{_catset_js()}{_SELECT_JS}</body></html>')


def _live_row(p: dict, q: str = "") -> str:
    price = f"₹{p['price']:.0f}" if p["price"] is not None else "—"
    cat = html.escape(p["category"] or "—")
    # Carry the current search on the action URL so the filter survives a removal.
    action = f'/remove/{p["id"]}'
    if q:
        action += "?q=" + urllib.parse.quote(q)
    return (
        f'<tr><td><img class="lthumb" src="/image/{p["id"]}" alt=""></td>'
        f'<td>#{p["id"]}</td><td><b>{html.escape(p["title"] or "")}</b></td>'
        f'<td>#{p["seller_id"]} {html.escape(p["seller"] or "")}</td>'
        f'<td>{html.escape(p["department"] or "")} / {cat}</td>'
        f'<td>{price}</td>'
        f'<td><form class="rmform" method="post" action="{action}" '
        "onsubmit=\"return confirm('Remove this live listing from the site? "
        "It will disappear from PeerShop immediately and the seller will be "
        "notified.')\">"
        '<input class="reason" type="text" name="reason" maxlength="300" '
        'placeholder="Reason (optional) — sent to the seller">'
        '<button class="rm" type="submit">Remove</button></form></td></tr>')


def render_live(notice: str = "", q: str = "") -> str:
    q = (q or "").strip()
    rows = approvals.live_listings(q or None)
    n = f'<div class="note" style="margin-bottom:12px">{html.escape(notice)}</div>' if notice else ""
    nav = ('<div class="navbar"><a class="navlink" href="/">← Queue</a>'
           '<a class="navlink" href="/users">Users →</a>'
           '<form method="post" action="/logout">'
           '<button class="logout" type="submit">Log out</button></form></div>')
    search = (
        '<form class="livesearch" method="get" action="/live">'
        f'<input type="text" name="q" value="{html.escape(q, quote=True)}" '
        'placeholder="Find a product — title, seller, or #id" '
        'aria-label="Search live products">'
        '<button class="save" type="submit">Search</button>'
        + ('<a class="navlink" href="/live">Clear</a>' if q else '')
        + '</form>')
    count = (f'<p class="muted" style="margin:0 0 10px">{len(rows)} match(es) for '
             f'“{html.escape(q)}”.</p>' if q else "")
    if rows:
        table = ('<div class="scroll"><table class="utable">'
                 '<tr><th></th><th>ID</th><th>Title</th><th>Seller</th>'
                 '<th>Dept / Category</th><th>Price</th><th>Reason (optional) · Remove</th></tr>'
                 + "".join(_live_row(p, q) for p in rows) + '</table></div>')
    elif q:
        table = f'<div class="empty">No live product matches “{html.escape(q)}”.</div>'
    else:
        table = '<div class="empty">No live products.</div>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>PeerShop — Live products</title><style>{CSS}</style></head><body>"
        "<header><div><h1>PeerShop · Live products</h1>"
        "<p>Every product currently visible to customers. Removing one takes it "
        f"off the site immediately (soft delete) and notifies the seller.</p></div>{nav}</header>"
        f'<div class="wrap">{n}{search}{count}{table}</div></body></html>')


def _pw_cell(u: dict) -> str:
    last = u["pw_last"]
    if not last:
        return '<span class="muted">— no login —</span>'
    when = html.escape((last["at"] or "")[:16])
    if u["pw_changes"]:
        return (f'<b>{u["pw_changes"]}</b> change(s)<br>'
                f'<span class="muted">last {when} by {html.escape(last["actor"] or "?")}</span>')
    return f'<span class="muted">set at signup<br>{when}</span>'


def _user_row(u: dict) -> str:
    c = u["counts"]
    prod = (f'{c["live"]} live · {c["pending"]} pending · {c["rejected"]} rej · '
            f'{c["removed"]} removed')
    owner = f'<br><span class="muted">{html.escape(u["owner_name"])}</span>' if u["owner_name"] else ""
    login = html.escape(u["login"]) if u["login"] else '<span class="muted">(none)</span>'
    depts = html.escape(", ".join(u["departments"]) or "—")
    tg = "✓ linked" if u["telegram"] else "—"
    created = html.escape((u["acct_created"] or "")[:16]) or "—"
    last_login = html.escape((u["last_login"] or "")[:16]) if u["last_login"] else "never"
    reset = ('' if not u["login"] else
             f'<form method="post" action="/users/{u["id"]}/reset" '
             'onsubmit="return confirm(\'Reset password for this seller?\')">'
             '<input type="password" name="new_password" placeholder="new pw (min 8)" required>'
             '<button class="save" type="submit">Reset</button></form>')
    delete = (f'<form method="post" action="/users/{u["id"]}/delete" '
              f'onsubmit="return confirm(\'PERMANENTLY delete {html.escape(u["name"])} (#{u["id"]}) '
              'and ALL its data \u2014 products, photos, credentials, Telegram link? '
              'This cannot be undone.\')">'
              '<button class="save danger" type="submit">Delete account</button></form>')
    return (
        f'<tr><td>#{u["id"]}</td>'
        f'<td><b>{html.escape(u["name"])}</b>{owner}</td>'
        f'<td>{login}</td><td>{html.escape(u["contact"] or "")}</td>'
        f'<td>{depts}</td><td>{prod}</td><td>{tg}</td>'
        f'<td>{created}</td><td>{last_login}</td>'
        f'<td>{_pw_cell(u)}</td><td>{reset}</td><td>{delete}</td></tr>')


def render_users(notice: str = "") -> str:
    users = user_rows()
    events = auth.all_credential_events(limit=50)
    n = f'<div class="note" style="margin-bottom:12px">{html.escape(notice)}</div>' if notice else ""
    head = ('<tr><th>ID</th><th>Shop / Owner</th><th>Login</th><th>Contact</th>'
            '<th>Departments</th><th>Products</th><th>Telegram</th><th>Created</th>'
            '<th>Last login</th><th>Password</th><th>Reset password</th><th>Danger zone</th></tr>')
    table = ('<div class="scroll"><table class="utable">' + head
             + "".join(_user_row(u) for u in users) + '</table></div>')
    if events:
        logrows = "".join(
            f'<tr><td>{html.escape((e["at"] or "")[:16])}</td>'
            f'<td>#{e["seller_id"]} {html.escape(e["seller"])}</td>'
            f'<td>{html.escape(e["event"])}</td>'
            f'<td>{html.escape(e["actor"] or "?")}</td></tr>' for e in events)
        log = ('<h2 class="sub">Password-change activity</h2>'
               '<div class="scroll"><table class="utable">'
               '<tr><th>When</th><th>Seller</th><th>Event</th><th>By</th></tr>'
               + logrows + '</table></div>')
    else:
        log = ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>PeerShop — Users</title><style>{CSS}</style></head><body>"
        "<header><div><h1>PeerShop · Users</h1>"
        "<p>Every seller account. Passwords are one-way hashed — never shown; you "
        "can see change history and reset one.</p></div>"
        '<div class="navbar"><a class="navlink" href="/">← Review queue</a>'
        '<a class="navlink" href="/live">Live products →</a></div></header>'
        f'<div class="wrap">{n}{table}{log}</div></body></html>')


def render_login(error: str = "") -> str:
    e = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>PeerShop — Review login</title><style>{CSS}</style></head><body>"
        f'<div class="authwrap"><h2>Admin review</h2>{e}'
        '<form method="post" action="/login">'
        '<input type="password" name="password" placeholder="Admin password" required autofocus>'
        '<button type="submit">Enter</button></form></div></body></html>')


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, content_type, extra_headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location, extra_headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self._send(200, render_login(), "text/html; charset=utf-8")
        if not _is_authed(self):
            return self._redirect("/login")
        if path == "/":
            return self._send(200, render_queue(), "text/html; charset=utf-8")
        if path == "/live":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
            return self._send(200, render_live(q=q), "text/html; charset=utf-8")
        if path == "/users":
            return self._send(200, render_users(), "text/html; charset=utf-8")
        if path.startswith("/image/"):
            parts = path[len("/image/"):].split("/")
            try:
                pid = int(parts[0])
                idx = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
            except (ValueError, IndexError):
                return self._send(404, "not found", "text/plain")
            img = pending_photo(pid, idx)
            if img and os.path.exists(img):
                ctype = mimetypes.guess_type(img)[0] or "application/octet-stream"
                with open(img, "rb") as f:
                    return self._send(200, f.read(), ctype)
            return self._send(200, _PLACEHOLDER_PNG, "image/png")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        if path == "/login":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            pw = _admin_password()
            given = (form.get("password", [""])[0] or "")
            if pw and hmac.compare_digest(given, pw):
                cookie = (f"{ADMIN_COOKIE}={_token(pw)}; HttpOnly; SameSite=Strict; "
                          "Path=/; Max-Age=43200")
                return self._redirect("/", [("Set-Cookie", cookie)])
            return self._send(200, render_login("Wrong password." if pw else
                              "PEERSHOP_ADMIN_PASSWORD is not set on the server."),
                              "text/html; charset=utf-8")
        if path == "/logout":
            clear = f"{ADMIN_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            return self._redirect("/login", [("Set-Cookie", clear)])

        if not _is_authed(self):
            return self._redirect("/login")

        # Admin password reset for a seller (audited as actor='admin'; seller notified).
        uparts = path.strip("/").split("/")      # ["users", "<id>", "reset"]
        if len(uparts) == 3 and uparts[0] == "users" and uparts[2] == "reset":
            try:
                sid = int(uparts[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            new_pw = form.get("new_password", [""])[0]
            login = auth.get_login(sid)
            if not login:
                return self._send(200, render_users(f"#{sid} has no login to reset."),
                                  "text/html; charset=utf-8")
            try:
                auth.set_credentials(sid, login, new_pw, actor="admin")
            except auth.AuthError as e:
                return self._send(200, render_users(f"#{sid}: {e}"),
                                  "text/html; charset=utf-8")
            try:
                tid = telegram_link.get_telegram_for_seller(sid)
                if tid:
                    telegram_api.send_message(
                        tid, "🔐 Your PeerShop password was reset by an admin. If this "
                        "wasn't expected, contact the marketplace.")
            except Exception:
                pass
            return self._send(200, render_users(f"Password reset for #{sid} “{login}”."),
                              "text/html; charset=utf-8")

        # Admin account removal: irreversible, full wipe (sellers row + every
        # FK-cascaded table + media files on disk). See admin_actions.py.
        # Built so a single Telegram account can be reused for repeat testing
        # of the self-service onboarding flow (sign up -> test -> wipe -> again).
        if len(uparts) == 3 and uparts[0] == "users" and uparts[2] == "delete":
            try:
                sid = int(uparts[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            result = admin_actions.delete_seller(sid)
            if result["ok"]:
                notice = (f"Deleted #{sid} “{result['name']}” and all its data "
                          f"({result['media_removed']} media file(s) removed).")
            else:
                notice = result.get("message", "")
            return self._send(200, render_users(notice), "text/html; charset=utf-8")

        # Admin takedown of ANY product (live/pending/rejected) → removed, soft.
        rparts = path.strip("/").split("/")       # ["remove", "<id>"]
        if len(rparts) == 2 and rparts[0] == "remove":
            try:
                pid = int(rparts[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            reason = (form.get("reason", [""])[0] or "").strip() or None
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
            r = approvals.admin_remove(pid, reason=reason, actor="review-web")
            if r["ok"]:
                notice = (f"Removed #{pid} “{r.get('title', '')}” from the site"
                          + (" — reason sent to the seller." if reason
                             else " (no reason sent)."))
            else:
                notice = r.get("message", "")
            return self._send(200, render_live(notice, q=q), "text/html; charset=utf-8")

        parts = path.strip("/").split("/")       # ["review", "<id>", "<action>"]
        if len(parts) == 3 and parts[0] == "review" and \
                parts[2] in ("promote", "reject", "category"):
            try:
                pid = int(parts[1])
            except ValueError:
                return self._send(404, "not found", "text/plain")
            notice = ""
            if parts[2] == "promote":
                # Apply the chosen category (by global id — may move the product's
                # department), then promote — one click.
                form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                chosen = (form.get("category", [""])[0] or "").strip()
                if chosen.isdigit():
                    try:
                        categorize.assign_category_by_id(pid, int(chosen))
                    except ValueError:
                        pass
                r = approvals.approve(pid, actor="review-web")
                notice = "" if r["ok"] else r.get("message", "")
            elif parts[2] == "reject":
                approvals.reject(pid, actor="review-web")
            else:  # category — admin creates a new category from the typed name
                form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                new_cat = (form.get("new_category", [""])[0] or "").strip()
                if new_cat:
                    try:
                        r = categorize.admin_add_category(pid, new_cat)
                        verb = "created" if r["status"] == "created" else "assigned"
                        notice = (f"Category “{r['name']}” {verb} and set on #{pid} — "
                                  "you can promote it now.")
                    except ValueError as e:
                        notice = str(e)
            # Re-render with any notice (e.g. "set a category before promoting").
            if notice:
                return self._send(200, render_queue(notice), "text/html; charset=utf-8")
            return self._redirect("/")
        return self._send(404, "not found", "text/plain")


def main():
    if not _admin_password():
        print("WARNING: PEERSHOP_ADMIN_PASSWORD is not set (env or /root/.hermes/.env) "
              "— nobody can log in until it is.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PeerShop review queue on http://localhost:{PORT}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
