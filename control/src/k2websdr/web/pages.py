"""Minimal server-rendered pages (M0). No JavaScript; every value is escaped.

The real UI (Svelte) starts in M1. These pages exist so a private
deployment can be set up, logged into and inspected safely.
"""

from __future__ import annotations

from html import escape

CSS = """
body{font:15px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f8;color:#1b1d1f}
main{max-width:46rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.3rem;margin:0 0 1rem}h2{font-size:1.05rem;margin:1.6rem 0 .5rem}
form.card,section.card{background:#fff;border:1px solid #dde1e5;border-radius:8px;padding:1rem 1.2rem}
label{display:block;margin:.6rem 0 .2rem;font-weight:600}
input{width:100%;box-sizing:border-box;padding:.45rem;border:1px solid #c4cad0;border-radius:6px;font:inherit}
button{margin-top:1rem;padding:.5rem 1rem;border:0;border-radius:6px;background:#1f5fae;color:#fff;font:inherit}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #dde1e5;padding:.35rem .5rem;text-align:left}
.err{color:#a4161a;font-weight:600}.muted{color:#5b6570}code{font-size:.92em}
header{display:flex;justify-content:space-between;align-items:center}
@media (prefers-color-scheme: dark){body{background:#121416;color:#e6e8ea}form.card,section.card,table{background:#1b1e21;border-color:#33383d}
td,th{border-color:#33383d}input{background:#121416;color:#e6e8ea;border-color:#454b51}.muted{color:#9aa4ad}.err{color:#ff8a8a}}
"""


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)} · K2AAM WebSDR</title><link rel="stylesheet" href="/static/app.css"></head>'
            f'<body><main>{body}</main></body></html>')


def _err(msg: str | None) -> str:
    return f'<p class="err" role="alert">{escape(msg)}</p>' if msg else ""


def setup_page(error: str | None = None) -> str:
    return page("First-run setup", f"""
<h1>First-run setup</h1>
<p>Create the administrator account. The one-time setup token is in the file named in the service log
(<code>setup-token</code> in the state directory), readable only by the service account.</p>
{_err(error)}
<form class="card" method="post" action="/setup">
<label for="token">Setup token</label><input id="token" name="token" autocomplete="off" required>
<label for="username">Admin username</label><input id="username" name="username" autocomplete="username" required>
<label for="password">Password (12+ characters)</label>
<input id="password" name="password" type="password" autocomplete="new-password" minlength="12" required>
<button type="submit">Create admin</button></form>""")


def login_page(error: str | None = None) -> str:
    return page("Sign in", f"""
<h1>Sign in</h1>{_err(error)}
<form class="card" method="post" action="/login">
<label for="username">Username</label><input id="username" name="username" autocomplete="username" required>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>""")


def home_page(username: str, role: str, csrf: str, state: dict, ledger_total: int) -> str:
    rows = []
    for rid, rx in state["receivers"].items():
        cap = rx["capture"]
        center = cap["center_hz"]["applied"]
        rate = cap["sample_rate_hz"]["applied"]
        holder = rx["lease"]["holder"]
        rows.append(
            f"<tr><td><code>{escape(rid)}</code><br><span class=muted>{escape(rx['label'])}</span></td>"
            f"<td>{escape(rx['acquisition']['state'])}"
            f"{'<br><span class=muted>' + escape(rx['acquisition']['detail']) + '</span>' if rx['acquisition']['detail'] else ''}</td>"
            f"<td>{escape(str(center)) + ' Hz' if center is not None else '—'}</td>"
            f"<td>{escape(str(rate)) + ' Hz' if rate is not None else '—'}</td>"
            f"<td>{rx['capture_gen']}</td>"
            f"<td>{escape(holder['principal']) if holder else 'free'}</td></tr>")
    table = ("<table><tr><th>Receiver</th><th>Acquisition</th><th>Center</th><th>Rate</th>"
             "<th>Gen</th><th>Lease</th></tr>" + "".join(rows) + "</table>") if rows else "<p>No receivers configured.</p>"
    avail = len(state["ledger"]["available"])
    return page("Status", f"""
<header><h1>K2AAM WebSDR</h1>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{escape(csrf)}">
<button type="submit">Sign out</button></form></header>
<p>Signed in as <b>{escape(username)}</b> ({escape(role)}). Exposure: <b>{escape(state['server']['exposure'])}</b>.
Build {escape(state['server']['build'])}.</p>
<h2>Receivers</h2>{table}
<h2>Modes</h2><p>{avail} of {ledger_total} baseline modes available. <a href="/modes">Why?</a></p>""")


def modes_page(rows: list[tuple[str, str, str, list[str]]]) -> str:
    body = "".join(
        f"<tr><td><code>{escape(i)}</code></td><td>{escape(n)}</td><td>{escape(k)}</td>"
        f"<td>{'available' if not r else '<br>'.join(escape(x) for x in r)}</td></tr>" for i, n, k, r in rows)
    return page("Modes", f"""<p><a href="/status">← Status</a></p><h1>Mode availability</h1>
<p class=muted>A mode is offered only when its adapter is integrated and tested and its dependency is present
on this host. Each row says what is missing.</p>
<table><tr><th>ID</th><th>Name</th><th>Kind</th><th>Status</th></tr>{body}</table>""")
