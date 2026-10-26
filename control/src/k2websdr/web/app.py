"""FastAPI application: pages, JSON API and the control WebSocket.

Security posture (outline §9; acceptance A10–A12):
- Host header allow-list on every request (DNS-rebinding guard), including
  WebSocket upgrades.
- Every POST must come from an allowed Origin (or same-origin Referer).
  Authenticated mutations also need the session's CSRF token.
- WebSocket upgrades need an allowed Origin and a valid session cookie.
  The session is re-checked on every message, and revoking or disabling an
  account closes its sockets immediately.
- Cookies: HttpOnly, SameSite=Strict, Secure when configured.
- Client IPs come from the TCP peer unless it is a configured trusted proxy.
- No default credentials: first run requires the single-use setup token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import accounts as acc
from ..auth.net import ProxyPolicy, SlidingWindow, TokenBucket
from ..config import Config
from ..core.receiver import ReceiverConfig, replay_link_factory
from ..core.service import ControlCore, Principal, Session
from ..db.migrate import connect, migrate
from ..ledger.load import availability, load_ledger, summary
from ..protocol import models as m
from ..protocol.errors import ErrorCode
from ..replay import sigmf
from . import pages

log = logging.getLogger("k2websdr")
COOKIE = "k2_session"
CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


@dataclass
class AppState:
    cfg: Config
    conn: sqlite3.Connection | None = None
    core: ControlCore | None = None
    ledger: object | None = None
    proxies: ProxyPolicy | None = None
    login_limiter: SlidingWindow | None = None
    sockets: dict[str, set[WebSocket]] = field(default_factory=dict)   # session token hash -> sockets


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewUser(_Strict):
    username: str = Field(max_length=64)
    password: str = Field(max_length=acc.MAX_PASSWORD)
    role: Literal["listener", "operator", "admin"]


def create_app(cfg: Config, *, link_factory=None) -> FastAPI:
    st = AppState(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st.conn = connect(cfg.db_path)
        migrate(st.conn)
        if acc.ensure_setup_token(st.conn, cfg.setup_token_path):
            log.warning("first-run setup pending: open /setup and use the token in %s", cfg.setup_token_path)
        st.proxies = ProxyPolicy(cfg.trusted_proxies)
        st.login_limiter = SlidingWindow(cfg.login_attempts_per_minute, 60.0)
        st.ledger = load_ledger(cfg.ledger)
        st.core = ControlCore(build=cfg.build, exposure=cfg.exposure, ledger=summary(st.ledger))
        cfg.lock_dir.mkdir(parents=True, exist_ok=True)
        factory = link_factory or replay_link_factory(cfg.worker_bin, cfg.lock_dir)
        for i, r in enumerate(cfg.receivers):
            rec = sigmf.load(r.recording)
            st.core.add_receiver(ReceiverConfig(receiver_id=r.id, label=r.label, receiver_idx=i, recording=rec,
                                                takeover_policy=r.takeover_policy), factory)
        for rt in st.core.receivers.values():
            try:
                await rt.start()
            except Exception as e:  # noqa: BLE001 - state shows the failure; the service stays up
                log.error("receiver %s failed to start: %s", rt.id, e)
        try:
            yield
        finally:
            await st.core.shutdown()
            st.conn.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.k2 = st

    # ------------------------------------------------------------ middleware
    allowed_hosts = set(cfg.allowed_hosts)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("bad host", status_code=400)
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # --------------------------------------------------------------- helpers
    def client_ip(request: Request) -> str | None:
        peer = request.client.host if request.client else None
        return st.proxies.client_ip(peer, request.headers.get("x-forwarded-for"))

    def origin_ok(headers) -> bool:
        origin = headers.get("origin")
        if origin is not None:
            return origin in cfg.allowed_origins
        ref = headers.get("referer") or ""
        return any(ref.startswith(o + "/") for o in cfg.allowed_origins)

    def require_origin(request: Request) -> None:
        if not origin_ok(request.headers):
            raise HTTPException(403, "cross-origin request refused")

    def session_of(request: Request) -> acc.SessionInfo | None:
        return acc.lookup_session(st.conn, request.cookies.get(COOKIE))

    def require_api_session(request: Request, *, permission: str | None = None) -> acc.SessionInfo:
        info = session_of(request)
        if info is None:
            raise HTTPException(401, "not signed in")
        if permission and permission not in info.permissions:
            raise HTTPException(403, f"requires {permission}")
        return info

    def require_csrf(request: Request, info: acc.SessionInfo, presented: str | None) -> None:
        require_origin(request)
        if not acc.csrf_ok(info, presented):
            raise HTTPException(403, "missing or bad CSRF token")

    async def close_sockets(token_hashes: list[str]) -> None:
        for th in token_hashes:
            for ws in list(st.sockets.get(th, ())):
                try:
                    await ws.close(code=4401, reason="session revoked")
                except RuntimeError:
                    pass

    # ----------------------------------------------------------------- health
    @app.get("/healthz")
    async def healthz():
        try:
            st.conn.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "error", "db": "unavailable"}, status_code=503)
        return {"status": "ok", "db": "ok"}

    @app.get("/static/app.css")
    async def css():
        return Response(pages.CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        if not acc.admin_exists(st.conn):
            return RedirectResponse("/setup", status_code=303)
        info = session_of(request)
        if info is None:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(pages.home_page(info.username, info.role, info.csrf_token, st.core.store.view(),
                                           len(st.ledger.entries)))

    @app.get("/modes", response_class=HTMLResponse)
    async def modes(request: Request):
        if session_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        av = availability(st.ledger)
        rows = [(e.id, e.name, e.kind, av[e.id]) for e in st.ledger.entries]
        return HTMLResponse(pages.modes_page(rows))

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_form():
        if acc.admin_exists(st.conn):
            raise HTTPException(404)
        return HTMLResponse(pages.setup_page())

    @app.post("/setup", response_class=HTMLResponse)
    async def setup_submit(request: Request, token: str = Form(max_length=128), username: str = Form(max_length=64),
                           password: str = Form(max_length=acc.MAX_PASSWORD)):
        require_origin(request)
        if acc.admin_exists(st.conn):
            raise HTTPException(404)
        if not st.login_limiter.allow(f"setup:{client_ip(request)}"):
            raise HTTPException(429, "too many attempts")
        try:
            acc.complete_setup(st.conn, cfg.setup_token_path, token, username, password)
        except PermissionError:
            return HTMLResponse(pages.setup_page("That setup token is not valid."), status_code=403)
        except acc.AccountError as e:
            return HTMLResponse(pages.setup_page(str(e)), status_code=400)
        log.warning("first-run setup completed; admin %r created", acc.check_username(username))
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form():
        return HTMLResponse(pages.login_page())

    @app.post("/login")
    async def login_submit(request: Request, username: str = Form(max_length=64),
                           password: str = Form(max_length=acc.MAX_PASSWORD)):
        require_origin(request)
        ip = client_ip(request)
        if not st.login_limiter.allow(f"login:{ip}"):
            acc.audit(st.conn, f"ip:{ip}", "login.rate_limited")
            return HTMLResponse(pages.login_page("Too many attempts. Wait a minute."), status_code=429)
        row = acc.verify_login(st.conn, username, password)
        if row is None:
            acc.audit(st.conn, f"ip:{ip}", "login.failed", username[:64])
            return HTMLResponse(pages.login_page("Wrong username or password."), status_code=401)
        token, _csrf = acc.create_session(st.conn, row["id"], cfg.session_hours, ip)
        acc.audit(st.conn, f"user:{row['username']}", "login.ok", f"ip={ip}")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=cfg.session_hours * 3600, httponly=True, samesite="strict",
                        secure=cfg.cookie_secure, path="/")
        return resp

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form(max_length=128)):
        info = session_of(request)
        if info is not None:
            require_csrf(request, info, csrf)
            acc.revoke_session(st.conn, info.token_hash)
            await close_sockets([info.token_hash])
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # -------------------------------------------------------------------- API
    @app.get("/api/session")
    async def api_session(request: Request):
        info = require_api_session(request)
        return {"username": info.username, "role": info.role, "permissions": sorted(info.permissions),
                "csrf": info.csrf_token}

    @app.get("/api/state")
    async def api_state(request: Request):
        require_api_session(request, permission="listen")
        return JSONResponse(content=json.loads(st.core.store.snapshot().model_dump_json()))

    @app.get("/api/modes")
    async def api_modes(request: Request):
        require_api_session(request)
        av = availability(st.ledger)
        return {"available": [i for i, r in av.items() if not r],
                "unavailable": {i: r for i, r in av.items() if r}}

    @app.post("/api/admin/users", status_code=201)
    async def admin_create_user(request: Request, body: NewUser):
        info = require_api_session(request, permission="admin")
        require_csrf(request, info, request.headers.get("x-csrf-token"))
        try:
            acc.create_user(st.conn, body.username, body.password, body.role, actor=f"user:{info.username}")
        except acc.AccountError as e:
            raise HTTPException(400, str(e)) from None
        return {"username": acc.check_username(body.username), "role": body.role}

    @app.post("/api/admin/users/{username}/disable")
    async def admin_disable(request: Request, username: str):
        info = require_api_session(request, permission="admin")
        require_csrf(request, info, request.headers.get("x-csrf-token"))
        try:
            acc.set_disabled(st.conn, username, True, actor=f"user:{info.username}")
            revoked = acc.revoke_user_sessions(st.conn, username, actor=f"user:{info.username}")
        except acc.AccountError as e:
            raise HTTPException(404, str(e)) from None
        await close_sockets(revoked)
        return {"disabled": True, "sessions_revoked": len(revoked)}

    @app.post("/api/admin/users/{username}/revoke-sessions")
    async def admin_revoke(request: Request, username: str):
        info = require_api_session(request, permission="admin")
        require_csrf(request, info, request.headers.get("x-csrf-token"))
        try:
            revoked = acc.revoke_user_sessions(st.conn, username, actor=f"user:{info.username}")
        except acc.AccountError as e:
            raise HTTPException(404, str(e)) from None
        await close_sockets(revoked)
        return {"sessions_revoked": len(revoked)}

    @app.get("/api/admin/audit")
    async def admin_audit(request: Request):
        require_api_session(request, permission="admin")
        rows = st.conn.execute("SELECT ts_utc, actor, action, detail FROM audit ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- WebSocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if ws.headers.get("host") not in allowed_hosts or ws.headers.get("origin") not in cfg.allowed_origins:
            await ws.close(code=4403)
            return
        token = ws.cookies.get(COOKIE)
        info = acc.lookup_session(st.conn, token)
        if info is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        sess: Session = st.core.open_session(Principal(f"user:{info.username}", info.permissions))
        st.sockets.setdefault(info.token_hash, set()).add(ws)
        bucket = TokenBucket(cfg.ws_messages_per_second, cfg.ws_burst)

        async def pump():
            while True:
                msg = await sess.outbox.get()
                await ws.send_text(msg.model_dump_json())

        async def receive_loop():
            while True:
                raw = await ws.receive_text()
                if acc.lookup_session(st.conn, token) is None:
                    return "session ended"
                if not bucket.take():
                    sess.send(m.ErrorMsg(v=1, type="error", code=ErrorCode.E_RATE_LIMITED,
                                         detail="too many messages"))
                    continue
                await st.core.handle(sess, raw)

        async def session_watchdog():
            # Enforces expiry and revocation on idle sockets, independently of client messages:
            # wake at the session's expiry, and at least every ws_session_recheck_s for revocations
            # made elsewhere (another process, or the database directly).
            while True:
                remaining = (info.expires - datetime.now(timezone.utc)).total_seconds()
                await asyncio.sleep(max(0.0, min(remaining, cfg.ws_session_recheck_s)))
                if acc.lookup_session(st.conn, token) is None:
                    return "session expired or revoked"

        tasks = [asyncio.create_task(pump(), name="ws-send"),
                 asyncio.create_task(receive_loop(), name="ws-recv"),
                 asyncio.create_task(session_watchdog(), name="ws-session")]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            ended = next(iter(done))
            if ended.get_name() in ("ws-recv", "ws-session") and not ended.cancelled() \
                    and ended.exception() is None and ended.result():
                try:
                    await ws.close(code=4401, reason=ended.result())
                except RuntimeError:
                    pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Ending the core session releases this session's operator leases; job leases are untouched.
            st.core.close_session(sess)
            st.sockets.get(info.token_hash, set()).discard(ws)

    return app

