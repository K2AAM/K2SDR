"""HTTP/WebSocket front end: A10 first run, A11 authz on HTTP and WebSocket,
A12 trusted proxies, plus /healthz, Host/Origin guards and escaping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from k2websdr.auth.net import ProxyPolicy
from k2websdr.config import Config, ConfigError, ReceiverEntry, validate, with_defaults
from k2websdr.web.app import COOKIE, create_app

from core_helpers import CENTER, cmd
from fake_worker import fake_factory

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8075"
ORIGIN = {"Origin": BASE}
ADMIN_PW = "correct horse battery staple"
RX = "replay-0"


def make_cfg(tmp_path: Path, **over) -> Config:
    cfg = Config(state_dir=tmp_path / "state", runtime_dir=tmp_path / "run",
                 worker_bin=ROOT / "build/worker/ksdr-worker", ledger=ROOT / "ledger/modes.v1.json",
                 receivers=(ReceiverEntry(RX, "Replay <b>&</b>", ROOT / "fixtures/iq/two_tone_ci16.sigmf-meta"),))
    return validate(with_defaults(replace(cfg, **over)))


def client(cfg: Config, **kw) -> TestClient:
    return TestClient(create_app(cfg, link_factory=fake_factory()), base_url=BASE, follow_redirects=False, **kw)


def token(cfg: Config) -> str:
    return cfg.setup_token_path.read_text().strip()


def do_setup(c: TestClient, cfg: Config, username="adam", password=ADMIN_PW):
    return c.post("/setup", data={"token": token(cfg), "username": username, "password": password}, headers=ORIGIN)


def login(c: TestClient, username="adam", password=ADMIN_PW):
    return c.post("/login", data={"username": username, "password": password}, headers=ORIGIN)


def csrf(c: TestClient) -> str:
    return c.get("/api/session").json()["csrf"]


def add_user(c: TestClient, username: str, role: str, password: str = "another long password"):
    return c.post("/api/admin/users", json={"username": username, "password": password, "role": role},
                  headers={**ORIGIN, "X-CSRF-Token": csrf(c)})


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_cfg(tmp_path)


@pytest.fixture
def admin(cfg):
    with client(cfg) as c:
        assert do_setup(c, cfg).status_code == 303
        assert login(c).status_code == 303
        yield c


# ===================================================================== A10

def test_fresh_install_has_no_accounts_and_a_private_setup_token(cfg, caplog):
    caplog.set_level(logging.WARNING, logger="k2websdr")
    with client(cfg) as c:
        users = sqlite3.connect(cfg.db_path).execute("SELECT count(*) FROM users").fetchone()[0]
        assert users == 0
        assert cfg.setup_token_path.stat().st_mode & 0o777 == 0o600
        tok = token(cfg)
        assert len(tok) >= 24
        assert str(cfg.setup_token_path) in caplog.text
        assert tok not in caplog.text, "the token value must never be logged"
        r = c.get("/")
        assert (r.status_code, r.headers["location"]) == (303, "/setup")


@pytest.mark.parametrize("u,p", [("admin", "admin"), ("admin", "password"), ("root", "root"), ("", ""),
                                 ("admin", "")])
def test_no_default_credentials(cfg, u, p):
    with client(cfg) as c:
        # 401 for wrong credentials; 422 when a required field is empty. Either way: no session.
        assert login(c, u, p).status_code in (401, 422)
        assert COOKIE not in c.cookies


def test_setup_requires_the_token(cfg):
    with client(cfg) as c:
        r = c.post("/setup", data={"token": "nope", "username": "adam", "password": ADMIN_PW}, headers=ORIGIN)
        assert r.status_code == 403
        assert cfg.setup_token_path.exists()
        assert sqlite3.connect(cfg.db_path).execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_setup_is_single_use(cfg):
    with client(cfg) as c:
        old = token(cfg)
        assert do_setup(c, cfg).status_code == 303
        assert not cfg.setup_token_path.exists()
        assert c.get("/setup").status_code == 404
        r = c.post("/setup", data={"token": old, "username": "eve", "password": ADMIN_PW}, headers=ORIGIN)
        assert r.status_code == 404
    with client(cfg) as c:                        # restart: no new token once an admin exists
        assert not cfg.setup_token_path.exists()
        assert c.get("/").headers["location"] == "/login"


def test_setup_enforces_password_policy(cfg):
    with client(cfg) as c:
        r = c.post("/setup", data={"token": token(cfg), "username": "adam", "password": "short"}, headers=ORIGIN)
        assert r.status_code == 400 and cfg.setup_token_path.exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.17", "100.79.117.125", "::", "optiplexsdr", "example.com"])
def test_non_loopback_bind_is_refused(tmp_path, host):
    with pytest.raises(ConfigError, match="loopback"):
        make_cfg(tmp_path, host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_bind_is_accepted(tmp_path, host):
    assert make_cfg(tmp_path, host=host).host == host


def test_config_file_loading(tmp_path):
    from k2websdr.config import load

    p = tmp_path / "k.toml"
    p.write_text('[server]\nport = 8075\nstate_dir = "s"\n[[receivers]]\nid = "r"\nrecording = "x.sigmf-meta"\n')
    c = load(p)
    assert c.state_dir == (tmp_path / "s").resolve() and c.receivers[0].recording == (tmp_path / "x.sigmf-meta").resolve()
    p.write_text('[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(ConfigError):
        load(p)
    p.write_text('[server]\nbogus = 1\n')
    with pytest.raises(ConfigError):
        load(p)
    assert (ROOT / "dev/k2websdr.dev.toml").exists() and load(ROOT / "dev/k2websdr.dev.toml").port == 8075


# ===================================================================== A11 (HTTP)

def test_login_and_setup_need_same_origin(cfg):
    with client(cfg) as c:
        assert c.post("/setup", data={"token": token(cfg), "username": "a", "password": ADMIN_PW}).status_code == 403
        assert c.post("/setup", data={"token": token(cfg), "username": "a", "password": ADMIN_PW},
                      headers={"Origin": "http://evil.example"}).status_code == 403
        assert do_setup(c, cfg).status_code == 303
        assert c.post("/login", data={"username": "adam", "password": ADMIN_PW}).status_code == 403
        assert c.post("/login", data={"username": "adam", "password": ADMIN_PW},
                      headers={"Referer": BASE + "/login"}).status_code == 303


def test_session_cookie_attributes(cfg):
    with client(cfg) as c:
        do_setup(c, cfg)
        set_cookie = login(c).headers["set-cookie"]
        for attr in ("HttpOnly", "SameSite=strict", "Path=/"):
            assert attr.lower() in set_cookie.lower()


def test_logout_needs_csrf_and_revokes(admin):
    assert admin.post("/logout", data={"csrf": "wrong"}, headers=ORIGIN).status_code == 403
    assert admin.get("/api/session").status_code == 200
    stolen = admin.cookies.get(COOKIE)
    assert admin.post("/logout", data={"csrf": csrf(admin)}, headers=ORIGIN).status_code == 303
    admin.cookies.set(COOKIE, stolen)
    assert admin.get("/api/session").status_code == 401, "a logged-out session token is dead"


def test_admin_api_requires_admin_and_csrf(admin, cfg):
    r = admin.post("/api/admin/users", json={"username": "op", "password": "operator password", "role": "operator"},
                   headers=ORIGIN)
    assert r.status_code == 403                                   # no CSRF header
    assert add_user(admin, "op", "operator").status_code == 201
    assert add_user(admin, "guest", "listener").status_code == 201
    with client(cfg) as anon:
        assert anon.get("/api/admin/audit").status_code == 401
        assert login(anon, "guest", "another long password").status_code == 303
        assert anon.get("/api/admin/audit").status_code == 403
        assert anon.post("/api/admin/users/op/disable", headers={**ORIGIN, "X-CSRF-Token": csrf(anon)}).status_code == 403


def test_admin_api_rejects_unknown_fields(admin):
    r = admin.post("/api/admin/users", json={"username": "x", "password": "long enough pw", "role": "admin", "x": 1},
                   headers={**ORIGIN, "X-CSRF-Token": csrf(admin)})
    assert r.status_code == 422


def test_disabled_user_cannot_log_in(admin, cfg):
    add_user(admin, "op", "operator")
    assert admin.post("/api/admin/users/op/disable", headers={**ORIGIN, "X-CSRF-Token": csrf(admin)}).status_code == 200
    with client(cfg) as c:
        assert login(c, "op", "another long password").status_code == 401


def test_login_rate_limit(cfg):
    with client(cfg) as c:
        do_setup(c, cfg)
        codes = [login(c, "adam", "wrong password!").status_code for _ in range(7)]
        assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
        assert login(c).status_code == 429, "correct password is also refused while limited"


def test_host_header_guard(cfg):
    with client(cfg) as c:
        assert c.get("/healthz", headers={"host": "attacker.example:8075"}).status_code == 400
        assert c.get("/healthz", headers={"host": "localhost:8075"}).status_code == 200


def test_security_headers(cfg):
    with client(cfg) as c:
        r = c.get("/login")
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_untrusted_text_is_escaped_in_pages(admin):
    html = admin.get("/").text
    assert "Replay &lt;b&gt;&amp;&lt;/b&gt;" in html
    assert "<b>&</b>" not in html


def test_status_and_modes_pages(admin):
    assert "0 of 74 baseline modes available" in admin.get("/").text
    modes = admin.get("/modes").text
    assert "required dependency &#x27;ale2g&#x27; not detected" in modes
    api = admin.get("/api/modes").json()
    assert api["available"] == [] and len(api["unavailable"]) == 74


def test_api_requires_session(cfg):
    with client(cfg) as c:
        for path in ("/api/state", "/api/session", "/api/modes"):
            assert c.get(path).status_code == 401
        assert c.get("/modes").status_code == 303


def test_state_api_returns_snapshot(admin):
    snap = admin.get("/api/state").json()
    assert snap["type"] == "snapshot" and RX in snap["state"]["receivers"]
    assert snap["state"]["receivers"][RX]["capture"]["center_hz"]["applied"] == CENTER


# ================================================================ healthz

def test_healthz(cfg):
    with client(cfg) as c:
        assert c.get("/healthz").json() == {"status": "ok", "db": "ok"}
        c.app.state.k2.conn.close()
        r = c.get("/healthz")
        assert r.status_code == 503 and r.json()["db"] == "unavailable"
        c.app.state.k2.conn = sqlite3.connect(":memory:", check_same_thread=False)   # let shutdown proceed


# ===================================================================== A11 (WebSocket)

def ws_headers(origin=BASE):
    # Starlette's TestClient sends "Host: testserver" on WebSocket upgrades; send the real one.
    return {"origin": origin, "host": "127.0.0.1:8075"}


def ws_connect(c: TestClient, origin: str = BASE, token: str | None = None):
    """Open /ws as this client. The TestClient scopes cookies to the base URL's host and does not
    send them on WebSocket upgrades, so the session cookie is passed explicitly, as a browser would."""
    headers = ws_headers(origin)
    tok = token or c.cookies.get(COOKIE)
    if tok:
        headers["cookie"] = f"{COOKIE}={tok}"
    return c.websocket_connect("/ws", headers=headers)


def ws_cmd(ws, op, **args):
    cid, raw = cmd(op, **args)
    ws.send_text(raw)
    return cid


def ws_until(ws, pred, limit=50):
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if pred(msg):
            return msg
    raise AssertionError("expected message not received")


def test_ws_requires_session(cfg):
    with client(cfg) as c:
        with pytest.raises(WebSocketDisconnect) as e:
            with ws_connect(c):
                pass
        assert e.value.code == 4401


def test_ws_rejects_foreign_origin_and_host(admin):
    for headers in (ws_headers("http://evil.example"), {"host": "127.0.0.1:8075"},
                    {"origin": BASE, "host": "evil.example"}, {"origin": BASE}):
        with pytest.raises(WebSocketDisconnect) as e:
            with admin.websocket_connect("/ws", headers=headers):
                pass
        assert e.value.code == 4403


def test_ws_listener_is_forbidden_hardware_ops_and_operator_is_not(admin, cfg):
    add_user(admin, "guest", "listener")
    add_user(admin, "op", "operator")
    with client(cfg) as g:
        login(g, "guest", "another long password")
        with ws_connect(g) as ws:
            cid = ws_cmd(ws, "receiver.claim", receiver=RX)
            ack = ws_until(ws, lambda m: m.get("id") == cid)
            assert (ack["status"], ack["code"]) == ("rejected", "E_FORBIDDEN")
    with client(cfg) as o:
        login(o, "op", "another long password")
        with ws_connect(o) as ws:
            cid = ws_cmd(ws, "receiver.claim", receiver=RX)
            assert ws_until(ws, lambda m: m.get("id") == cid and m["type"] == "result")["status"] == "applied"
            cid = ws_cmd(ws, "receiver.set_capture", receiver=RX, center_hz=CENTER)
            res = ws_until(ws, lambda m: m.get("id") == cid and m["type"] == "result")
            assert res["status"] == "applied" and isinstance(res["capture_gen"], int)


def test_ws_subscribe_gets_snapshot_then_patches(admin):
    with ws_connect(admin) as ws:
        cid = ws_cmd(ws, "state.subscribe")
        snap = ws_until(ws, lambda m: m["type"] == "snapshot")
        assert RX in snap["state"]["receivers"]
        ws_until(ws, lambda m: m.get("id") == cid and m["type"] == "result")
        ws_cmd(ws, "receiver.claim", receiver=RX)
        patch = ws_until(ws, lambda m: m["type"] == "patch")
        assert patch["base"] == snap["rev"] and patch["rev"] == snap["rev"] + 1


def second_login(c: TestClient, username: str, password: str) -> str:
    """Log a second user into the SAME app instance; returns their session token and
    restores the client's own cookie. (Each TestClient builds its own app instance.)"""
    mine = c.cookies.get(COOKIE)
    c.cookies.clear()
    assert login(c, username, password).status_code == 303
    theirs = c.cookies.get(COOKIE)
    c.cookies.clear()
    c.cookies.set(COOKIE, mine)
    return theirs


def test_ws_closed_when_account_disabled(admin):
    add_user(admin, "op", "operator")
    op_token = second_login(admin, "op", "another long password")
    with ws_connect(admin, token=op_token) as ws:
        cid = ws_cmd(ws, "session.hello", client="test")
        ws_until(ws, lambda m: m.get("id") == cid and m["type"] == "result")
        r = admin.post("/api/admin/users/op/disable", headers={**ORIGIN, "X-CSRF-Token": csrf(admin)})
        assert r.json() == {"disabled": True, "sessions_revoked": 1}
        with pytest.raises(WebSocketDisconnect) as e:     # closed immediately, without a further message
            for _ in range(10):
                ws.receive_text()
        assert e.value.code == 4401
    admin.cookies.clear()
    admin.cookies.set(COOKIE, op_token)
    assert admin.get("/api/session").status_code == 401


def test_ws_revoked_session_cannot_send_more_commands(admin, cfg):
    add_user(admin, "op", "operator")
    with client(cfg) as o:
        login(o, "op", "another long password")
        with ws_connect(o) as ws:
            # Revoke directly in the database (as another admin process would) without the socket registry.
            conn = sqlite3.connect(cfg.db_path)
            conn.execute("UPDATE sessions SET revoked=1 WHERE user_id=(SELECT id FROM users WHERE username='op')")
            conn.commit()
            ws_cmd(ws, "receiver.claim", receiver=RX)
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
            assert e.value.code == 4401


def test_ws_rate_limit(tmp_path):
    cfg = make_cfg(tmp_path, ws_messages_per_second=1.0, ws_burst=3)
    with client(cfg) as c:
        do_setup(c, cfg)
        login(c)
        with ws_connect(c) as ws:
            for _ in range(6):
                ws_cmd(ws, "session.hello", client="x")
            err = ws_until(ws, lambda m: m["type"] == "error")
            assert err["code"] == "E_RATE_LIMITED"


def test_ws_oversize_and_garbage(admin):
    with ws_connect(admin) as ws:
        ws.send_text("x" * (64 * 1024 + 1))
        assert ws_until(ws, lambda m: m["type"] == "error")["code"] == "E_PAYLOAD_TOO_LARGE"
        ws.send_text('{"v":1,"type":"cmd"')
        assert ws_until(ws, lambda m: m["type"] == "error")["code"] == "E_SCHEMA"
        _, raw = cmd("receiver.set_capture", receiver=RX, center_hz=7074000.0)
        ws.send_text(raw)
        ack = ws_until(ws, lambda m: m["type"] == "ack")
        assert (ack["status"], ack["code"]) == ("rejected", "E_SCHEMA")


# ===================================================================== A12

@pytest.mark.parametrize("peer,xff,trusted,expect", [
    ("203.0.113.9", "198.51.100.1", (), "203.0.113.9"),                       # untrusted peer: header ignored
    ("127.0.0.1", "198.51.100.1", ("127.0.0.1/32",), "198.51.100.1"),
    ("127.0.0.1", "198.51.100.1, 10.0.0.5", ("127.0.0.1/32", "10.0.0.0/8"), "198.51.100.1"),
    ("127.0.0.1", "6.6.6.6, 198.51.100.1, 10.0.0.5", ("127.0.0.1/32", "10.0.0.0/8"), "198.51.100.1"),  # spoofed left hop
    ("127.0.0.1", "not-an-ip", ("127.0.0.1/32",), "127.0.0.1"),
    ("127.0.0.1", None, ("127.0.0.1/32",), "127.0.0.1"),
    ("::1", "2001:db8::7", ("::1/128",), "2001:db8::7"),
    ("testclient", "198.51.100.1", ("127.0.0.1/32",), "testclient"),
])
def test_client_ip_derivation(peer, xff, trusted, expect):
    assert ProxyPolicy(trusted).client_ip(peer, xff) == expect


def _last_audit_ip(cfg) -> str:
    row = sqlite3.connect(cfg.db_path).execute(
        "SELECT actor FROM audit WHERE action='login.failed' ORDER BY id DESC LIMIT 1").fetchone()
    return row[0].removeprefix("ip:")


def test_forwarded_for_ignored_from_untrusted_peer(tmp_path):
    cfg = make_cfg(tmp_path)
    with client(cfg, client=("127.0.0.1", 40000)) as c:
        c.post("/login", data={"username": "x", "password": "y"}, headers={**ORIGIN, "X-Forwarded-For": "6.6.6.6"})
        assert _last_audit_ip(cfg) == "127.0.0.1"


def test_forwarded_for_used_from_trusted_proxy(tmp_path):
    cfg = make_cfg(tmp_path, trusted_proxies=("127.0.0.1/32",))
    with client(cfg, client=("127.0.0.1", 40000)) as c:
        c.post("/login", data={"username": "x", "password": "y"}, headers={**ORIGIN, "X-Forwarded-For": "198.51.100.1"})
        assert _last_audit_ip(cfg) == "198.51.100.1"


def test_spoofed_forwarded_for_does_not_evade_login_limit(tmp_path):
    cfg = make_cfg(tmp_path)
    with client(cfg, client=("127.0.0.1", 40000)) as c:
        codes = [c.post("/login", data={"username": "x", "password": "y"},
                        headers={**ORIGIN, "X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(7)]
        assert codes[-1] == 429
