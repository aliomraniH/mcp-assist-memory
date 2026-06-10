"""Password-protected admin dashboard for managing the MCP auth token.

The dashboard is the single source of truth for the live token: rotating it
here immediately changes the token the MCP server accepts (the auth middleware
reads from the same AdminStore cache). It is reachable at /admin and is locked
behind ADMIN_PASSWORD via a signed, HttpOnly session cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .admin_store import AdminStore

SESSION_COOKIE = "admin_session"
SESSION_TTL = 12 * 60 * 60  # 12 hours


def _admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "").strip()


def _sign(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, ttl: int = SESSION_TTL) -> str:
    exp = str(int(time.time()) + ttl)
    return f"{exp}.{_sign(secret, exp)}"


def valid_session(secret: str, cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, exp)):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


def csrf_token(secret: str, session_cookie: str) -> str:
    return _sign(secret, "csrf:" + session_cookie)


def _base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  margin: 0; background: #0b1020; color: #e7ecf5; }
.wrap { max-width: 760px; margin: 0 auto; padding: 48px 24px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: #93a2c0; font-size: 14px; margin: 0 0 28px; }
.card { background: #131a2e; border: 1px solid #243049; border-radius: 14px;
  padding: 22px; margin-bottom: 18px; }
.card h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .06em;
  color: #93a2c0; margin: 0 0 14px; }
label { display:block; font-size: 13px; color:#93a2c0; margin-bottom:6px; }
input[type=password], input[type=text] { width:100%; padding:11px 12px; border-radius:10px;
  border:1px solid #2c3a59; background:#0d1426; color:#e7ecf5; font-size:14px; }
.btn { display:inline-block; border:0; border-radius:10px; padding:10px 16px; font-size:14px;
  font-weight:600; cursor:pointer; background:#3b6cf6; color:#fff; }
.btn.secondary { background:#243049; color:#e7ecf5; }
.btn.danger { background:#7a2230; color:#ffd9df; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.token { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px;
  background:#0d1426; border:1px solid #2c3a59; border-radius:10px; padding:12px 14px;
  word-break:break-all; flex:1 1 320px; }
.meta { color:#93a2c0; font-size:12px; margin-top:10px; }
pre { background:#0d1426; border:1px solid #2c3a59; border-radius:10px; padding:14px;
  overflow:auto; font-size:12.5px; position:relative; }
.copy { position:absolute; top:8px; right:8px; }
.muted { color:#6f7f9f; font-size:12px; }
.topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
a { color:#7aa2ff; }
.err { background:#3a1622; border:1px solid #7a2230; color:#ffd9df; padding:10px 12px;
  border-radius:10px; font-size:13px; margin-bottom:16px; }
"""

_COPY_JS = """
function copyText(el){
  const t = el.getAttribute('data-copy');
  navigator.clipboard.writeText(t).then(()=>{
    const old = el.textContent; el.textContent = 'Copied'; setTimeout(()=>el.textContent=old, 1200);
  });
}
"""


def _login_page(error: str = "") -> HTMLResponse:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin · assist-memory</title><style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>assist-memory admin</h1>
<p class="sub">Sign in to manage the MCP auth token.</p>
{err}
<div class="card"><h2>Sign in</h2>
<form method="post" action="/admin/login">
<label for="password">Admin password</label>
<input id="password" name="password" type="password" autofocus autocomplete="current-password">
<div style="height:14px"></div>
<button class="btn" type="submit">Sign in</button>
</form></div>
<p class="muted">Set the password via the ADMIN_PASSWORD secret.</p>
</div></body></html>"""
    return HTMLResponse(body)


def _dashboard_page(request: Request, admin: AdminStore, session_secret: str,
                    session_cookie: str) -> HTMLResponse:
    info = admin.info()
    token = info.token if info else ""
    created = info.created_at.strftime("%Y-%m-%d %H:%M UTC") if info else "—"
    base = _base_url(request)
    mcp_url = f"{base}/mcp"
    csrf = csrf_token(session_secret, session_cookie)
    esc_token = html.escape(token)
    esc_mcp = html.escape(mcp_url)

    cli = f'claude mcp add -s user --transport http assist-memory {mcp_url} -H "Authorization: Bearer {token}"'
    web = f"{mcp_url}?token={token}"
    cursor = (
        '{"mcpServers": {"assist-memory": {"url": "%s", '
        '"headers": {"Authorization": "Bearer %s"}}}}' % (mcp_url, token)
    )

    def block(title: str, content: str) -> str:
        esc = html.escape(content)
        return (
            f'<div class="card"><div class="topbar"><h2>{title}</h2>'
            f'<button class="btn secondary copy" type="button" data-copy="{esc}" '
            f'onclick="copyText(this)">Copy</button></div>'
            f'<pre>{esc}</pre></div>'
        )

    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin · assist-memory</title><style>{_PAGE_CSS}</style>
<script>{_COPY_JS}</script></head>
<body><div class="wrap">
<div class="topbar"><h1>assist-memory admin</h1>
<form method="post" action="/admin/logout" style="margin:0">
<input type="hidden" name="csrf" value="{csrf}">
<button class="btn secondary" type="submit">Sign out</button></form></div>
<p class="sub">This is the live token. Rotating it immediately invalidates the old one.</p>

<div class="card"><h2>Live auth token</h2>
<div class="row">
<div class="token" id="tok">{esc_token}</div>
<button class="btn" type="button" data-copy="{esc_token}" onclick="copyText(this)">Copy token</button>
</div>
<div class="meta">Created {created}</div>
<form method="post" action="/admin/rotate" style="margin-top:16px"
 onsubmit="return confirm('Rotate the token? Every client using the current token will stop working until updated.');">
<input type="hidden" name="csrf" value="{csrf}">
<button class="btn danger" type="submit">Rotate token</button>
</form>
</div>

<div class="card"><h2>Endpoint</h2>
<div class="row"><div class="token">{esc_mcp}</div>
<button class="btn secondary" type="button" data-copy="{esc_mcp}" onclick="copyText(this)">Copy</button></div>
</div>

{block("Claude Code CLI / Desktop", cli)}
{block("claude.ai web connector URL", web)}
{block("Cursor (~/.cursor/mcp.json)", cursor)}

<p class="muted">Other clients: streamable-http transport to {esc_mcp} with the
bearer header, or <code>?token=&lt;token&gt;</code> if headers aren't supported.</p>
</div></body></html>"""
    return HTMLResponse(body)


def build_routes(admin: AdminStore, session_secret: str) -> list[Route]:
    async def login_get(request: Request) -> Response:
        if valid_session(session_secret, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/admin", status_code=303)
        return _login_page()

    async def login_post(request: Request) -> Response:
        form = await request.form()
        supplied = str(form.get("password", ""))
        expected = _admin_password()
        if not expected:
            return _login_page("ADMIN_PASSWORD is not configured on the server.")
        if not secrets.compare_digest(supplied, expected):
            return _login_page("Incorrect password.")
        cookie = make_session(session_secret)
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE, cookie, max_age=SESSION_TTL, httponly=True,
            samesite="lax", secure=True, path="/admin",
        )
        return resp

    async def dashboard_get(request: Request) -> Response:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not valid_session(session_secret, cookie):
            return RedirectResponse("/admin/login", status_code=303)
        return _dashboard_page(request, admin, session_secret, cookie)

    def _check(request: Request, form) -> bool:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not valid_session(session_secret, cookie):
            return False
        return hmac.compare_digest(
            str(form.get("csrf", "")), csrf_token(session_secret, cookie)
        )

    async def rotate_post(request: Request) -> Response:
        form = await request.form()
        if not _check(request, form):
            return RedirectResponse("/admin/login", status_code=303)
        admin.rotate()
        return RedirectResponse("/admin", status_code=303)

    async def logout_post(request: Request) -> Response:
        form = await request.form()
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and hmac.compare_digest(
            str(form.get("csrf", "")), csrf_token(session_secret, cookie)
        ):
            pass
        resp = RedirectResponse("/admin/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/admin")
        return resp

    return [
        Route("/admin", dashboard_get, methods=["GET"]),
        Route("/admin/login", login_get, methods=["GET"]),
        Route("/admin/login", login_post, methods=["POST"]),
        Route("/admin/rotate", rotate_post, methods=["POST"]),
        Route("/admin/logout", logout_post, methods=["POST"]),
    ]
