"""Serving the browser UI (M1 step 5b): /app/ for signed-in users, hashed assets, the status page
moved to /status, and the CSP the UI needs (script-src 'self', the WebSocket named explicitly)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from test_web import ADMIN_PW, client, do_setup, login, make_cfg


@pytest.fixture
def web_cfg(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text('<!doctype html><script type="module" src="/app/assets/index-abc.js"></script>')
    (web / "assets" / "index-abc.js").write_text("console.log(1)")
    (web / "assets" / "index-abc.css").write_text("body{}")
    (web / "assets" / "notes.txt").write_text("not an asset type")
    (tmp_path / "secret.js").write_text("secret")
    return replace(make_cfg(tmp_path), web_dir=web)


def test_signed_in_home_goes_to_the_app_and_the_app_needs_a_session(web_cfg):
    with client(web_cfg) as c:
        do_setup(c, web_cfg)
        assert c.get("/app/").headers["location"] == "/login"            # not signed in
        login(c, "adam", ADMIN_PW)
        assert c.get("/").headers["location"] == "/app/"
        r = c.get("/app/")
        assert r.status_code == 200 and "/app/assets/index-abc.js" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert "6 of 74 baseline modes available" in c.get("/status").text   # the status page lives on


def test_assets_are_served_with_types_and_long_cache_and_nothing_else(web_cfg):
    with client(web_cfg) as c:
        r = c.get("/app/assets/index-abc.js")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
        assert "immutable" in r.headers["cache-control"]
        assert c.get("/app/assets/index-abc.css").headers["content-type"].startswith("text/css")
        for bad in ("/app/assets/notes.txt", "/app/assets/missing.js", "/app/assets/..%2Fsecret.js",
                    "/app/assets/%2E%2E%2F%2E%2E%2Fsecret.js"):
            assert c.get(bad).status_code == 404, bad


def test_csp_allows_own_scripts_and_names_the_websocket(web_cfg):
    with client(web_cfg) as c:
        csp = c.get("/login").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp
    assert "connect-src 'self' ws://127.0.0.1:8075 wss://127.0.0.1:8075" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_without_a_build_home_is_the_status_page(tmp_path):
    cfg = make_cfg(tmp_path)                                   # web_dir does not exist
    with client(cfg) as c:
        do_setup(c, cfg)
        login(c, "adam", ADMIN_PW)
        assert "baseline modes available" in c.get("/").text
        assert c.get("/app/").status_code == 404
