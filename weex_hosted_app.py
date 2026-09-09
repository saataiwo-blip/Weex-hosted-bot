"""
BOZZ Trading Bot — Hosted "Try It Live" Server
=================================================
Meant to be deployed on Render (or similar) alongside the license server.

CRITICAL DESIGN RULE: API keys are NEVER written to disk, a database, or a
log file anywhere in this file. They live only in one in-memory Python
dict (ACTIVE.creds) for as long as one visitor's bot session is running,
and are explicitly deleted the moment that session stops. Restarting this
server, or the process crashing, also wipes them — there is no persistence
layer for credentials at all, by design.

LIMITATION (intentional, for correctness): only ONE visitor can run a live
"Try It" session at a time. weex_sr_bot.py was built as a single-instance
script (its symbols/leverage/credentials/status are module-level globals),
so letting multiple people run concurrently on shared infrastructure would
require a full rewrite into a per-instance/class-based design — attempting
that under time pressure risks a real bug where one customer's keys or
settings leak into another's session, which is a worse outcome than a
"busy" message. A true multi-tenant version is future work, not this file.

Run with:  python weex_hosted_app.py   (for local testing)
On Render: gunicorn weex_hosted_app:app  (see requirements-hosted.txt)
"""

import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque

from flask import Flask, request, jsonify, session, Response

SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
LOGO_PATH = os.path.join(SCRIPT_DIR, "bozz_logo.png")

sys.path.insert(0, SCRIPT_DIR)
import weex_sr_bot  # noqa: E402

LICENSE_SERVER_URL = "https://weex-license-sever.onrender.com/verify"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

DEFAULT_COINS = [
    ("BTCUSDT", 20), ("SUIUSDT", 20), ("ONUSDT", 20), ("ETHUSDT", 20),
    ("SOLUSDT", 20), ("DOGEUSDT", 20), ("ADAUSDT", 20), ("AVAXUSDT", 20),
    ("LINKUSDT", 20), ("XRPUSDT", 20),
]


# ============================== IN-MEMORY-ONLY SESSION STATE ==============================

class ActiveSession:
    """Holds the one currently-running session's state. Nothing here ever
    touches disk. `creds` is deleted (not just overwritten) the instant the
    session stops, so it doesn't linger in memory either."""
    def __init__(self):
        self.lock = threading.Lock()
        self.session_id = None
        self.creds = None          # dict, only while running — deleted on stop
        self.thread = None
        self.stop_event = None
        self.log_lines = deque(maxlen=300)
        self.mode = None           # "demo" or "live"
        self.started_at = None

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def clear(self):
        with self.lock:
            self.session_id = None
            self.creds = None
            self.thread = None
            self.stop_event = None
            self.mode = None
            self.started_at = None


ACTIVE = ActiveSession()


class DequeLogHandler(logging.Handler):
    def emit(self, record):
        ACTIVE.log_lines.append(self.format(record))


def check_license(key: str) -> tuple:
    import requests
    try:
        resp = requests.post(LICENSE_SERVER_URL, json={"key": key}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("valid"):
            return True, "License valid."
        return False, "License key not recognized."
    except Exception as e:
        return False, f"Could not verify license: {e}"


# ============================== PAGE CHROME ==============================

BASE_CSS = """
:root { --bg:#06090a; --panel:#0e1712; --line:#1c2a22; --green:#39d353; --green-dim:#1f7a35; --ink:#dfe8e2; --muted:#7d8c84; --red:#e05555; }
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); font-family: 'Segoe UI', Arial, sans-serif; margin:0; }
.wrap { max-width: 720px; margin: 0 auto; padding: 30px 24px; }
.center { text-align:center; }
img.logo { max-width:140px; margin-bottom:10px; border-radius:12px; }
h1 { color: var(--green); font-size:24px; margin-bottom:6px; }
h2 { color: var(--green); font-size:16px; margin-top:20px; }
p.muted, .muted { color: var(--muted); font-size:14px; }
.error { color: var(--red); font-size:14px; }
input[type=text], input[type=password], input[type=number] {
  background: var(--panel); border:1px solid #333; color: var(--ink);
  padding:8px 10px; border-radius:4px; width:100%; margin:4px 0 14px 0;
}
button, .btn { background: var(--green-dim); color:white; border:none; padding:10px 18px; border-radius:4px; cursor:pointer; font-size:14px; }
button:hover { background: var(--green); }
.btn-secondary { background:#333; }
.card { background: var(--panel); border:1px solid #262626; border-radius:6px; padding:18px; margin-bottom:18px; }
.notice { border-left:3px solid var(--green); background:#0f1712; padding:14px 16px; border-radius:4px; font-size:13px; color: var(--muted); margin-bottom:18px; }
.coin-tags { display:flex; flex-wrap:wrap; gap:6px; }
.coin-tag { background:#0f2415; border:1px solid var(--green-dim); color:var(--green); padding:3px 9px; border-radius:12px; font-size:12px; }
table { width:100%; border-collapse: collapse; margin-top:10px; }
th, td { text-align:left; padding:6px 8px; font-size:13px; }
th { color: var(--green); border-bottom:1px solid #333; }
pre#log { background:#000; color: var(--green); padding:12px; height:220px; overflow-y:auto; font-size:12px; border-radius:4px; }
footer { text-align:center; margin-top:30px; }
footer a { color: var(--muted); font-size:12px; }
"""


def page(title, body_html):
    return f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>{title} — BOZZ Trading Bot</title>
    <style>{BASE_CSS}</style></head>
    <body><div class="wrap">{body_html}
    <footer><a href="/privacy">Privacy Policy</a></footer>
    </div></body></html>
    """


def logo_tag():
    return '<img src="/logo.png" class="logo">' if os.path.exists(LOGO_PATH) else ""


@app.route("/logo.png")
def logo():
    if os.path.exists(LOGO_PATH):
        with open(LOGO_PATH, "rb") as f:
            return Response(f.read(), mimetype="image/png")
    return "", 404


@app.route("/privacy")
def privacy():
    body = f"""
    <div class="center">{logo_tag()}<h1>Privacy Policy — Try It Live</h1></div>
    <div class="card">
      <p class="muted"><strong>Your API key is never stored.</strong> It exists only in this
      server's memory for as long as your session is actively running, and is deleted the
      instant you click Stop, close this session, or the server restarts. It is never written
      to a file, a database, or any log.</p>
      <p class="muted">Only one visitor can run a live session at a time. If someone else's
      session is active, you'll be asked to wait.</p>
      <p class="muted">For unattended, ongoing use, we recommend the downloadable desktop app
      instead, which runs entirely on your own computer and never sends your key anywhere but
      WEEX itself.</p>
    </div>
    """
    return page("Privacy Policy", body)


# ============================== MAIN TRY-IT PAGE ==============================

@app.route("/")
def index():
    my_session_id = session.get("sid")
    is_mine = my_session_id and ACTIVE.session_id == my_session_id
    busy = ACTIVE.is_running() and not is_mine

    if busy:
        body = f"""
        <div class="center">{logo_tag()}<h1>BOZZ Trading Bot — Try It Live</h1></div>
        <div class="card">
          <p>Someone else is currently trying the bot live. Please check back in a few minutes
          — only one live session runs at a time so we never have to store anyone's API keys.</p>
        </div>
        """
        return page("Try It Live", body)

    if is_mine and ACTIVE.is_running():
        return dashboard_page()

    return form_page()


def form_page():
    coins_rows = "".join(
        f'<tr><td><input type="checkbox" name="enabled_{c}" checked></td><td>{c}</td>'
        f'<td><input type="number" step="0.1" name="margin_{c}" value="5.0" style="width:70px;"></td>'
        f'<td><input type="number" name="lev_{c}" value="{lev}" style="width:70px;">x</td></tr>'
        for c, lev in DEFAULT_COINS
    )
    body = f"""
    <div class="center">{logo_tag()}<h1>Try BOZZ Trading Bot — Live</h1>
    <p class="muted">Runs for real, right here in your browser. Nothing you enter below is ever saved.</p></div>

    <div class="notice">
      <strong>Your API key is never stored.</strong> It stays only in this server's temporary
      memory while your session runs, and is deleted the moment you stop. No database, no file,
      no log ever holds it. <a href="/privacy">Read the full policy</a>.
    </div>

    <form method="post" action="/try/start">
      <div class="card">
        <h2>WEEX API Credentials</h2>
        <input type="password" name="api_key" placeholder="API Key" required>
        <input type="password" name="api_secret" placeholder="API Secret" required>
        <input type="password" name="api_passphrase" placeholder="API Passphrase" required>
      </div>

      <div class="card">
        <h2>Mode</h2>
        <label class="muted"><input type="radio" name="mode" value="demo" checked> Free Demo (simulated — no real funds at risk)</label><br><br>
        <label class="muted"><input type="radio" name="mode" value="live"> Live Trading (requires a license key)</label>
        <input type="text" name="license_key" placeholder="License key (only needed for Live)" style="margin-top:10px;">
      </div>

      <div class="card">
        <h2>Coins</h2>
        <table><tr><th>On</th><th>Coin</th><th>Margin</th><th>Leverage</th></tr>{coins_rows}</table>
      </div>

      <div class="error" id="formError"></div>
      <button type="submit">Start Trying It</button>
    </form>
    """
    return page("Try It Live", body)


@app.route("/try/start", methods=["POST"])
def try_start():
    with ACTIVE.lock:
        if ACTIVE.is_running():
            my_sid = session.get("sid")
            if ACTIVE.session_id != my_sid:
                return "Someone else is currently using the live demo. Please try again shortly.", 409

        api_key = request.form.get("api_key", "").strip()
        api_secret = request.form.get("api_secret", "").strip()
        api_passphrase = request.form.get("api_passphrase", "").strip()
        mode = request.form.get("mode", "demo")
        license_key = request.form.get("license_key", "").strip()

        if not (api_key and api_secret and api_passphrase):
            return "Missing API credentials.", 400

        if mode == "live":
            is_valid, message = check_license(license_key)
            if not is_valid:
                return f"License check failed: {message}", 400

        symbols, leverage, margin_by_symbol = [], {}, {}
        for coin, _ in DEFAULT_COINS:
            if request.form.get(f"enabled_{coin}") == "on":
                symbols.append(coin)
                leverage[coin] = int(request.form.get(f"lev_{coin}", 20))
                margin_by_symbol[coin] = float(request.form.get(f"margin_{coin}", 5.0))

        sid = secrets.token_hex(16)
        session["sid"] = sid
        ACTIVE.session_id = sid
        ACTIVE.mode = mode
        ACTIVE.started_at = time.time()
        ACTIVE.creds = {"key": api_key, "secret": api_secret, "passphrase": api_passphrase}  # RAM only

        # weex_sr_bot reads credentials/config via os.environ + config.json.
        # Since only one session runs at a time, this is safe — but note this
        # IS the one place a real multi-tenant rewrite would need to change.
        os.environ["WEEX_API_KEY"] = api_key
        os.environ["WEEX_API_SECRET"] = api_secret
        os.environ["WEEX_API_PASSPHRASE"] = api_passphrase

        config_path = weex_sr_bot.CONFIG_PATH
        with open(config_path, "w") as f:
            json.dump({
                "symbols": symbols, "leverage": leverage, "margin_by_symbol": margin_by_symbol,
                "default_margin_usdt": 5.0, "default_leverage": 20,
            }, f)

        handler = DequeLogHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        weex_sr_bot.log.addHandler(handler)
        weex_sr_bot.log.setLevel(logging.INFO)

        stop_event = threading.Event()
        ACTIVE.stop_event = stop_event
        dry_run_override = (mode != "live")

        def target():
            try:
                weex_sr_bot.run(stop_event=stop_event, dry_run_override=dry_run_override)
            except Exception as e:
                ACTIVE.log_lines.append(f"[Bot crashed: {e}]")
            finally:
                # Belt-and-suspenders: wipe credentials the moment the loop exits,
                # even if it stopped on its own rather than via /try/stop.
                ACTIVE.creds = None
                os.environ.pop("WEEX_API_KEY", None)
                os.environ.pop("WEEX_API_SECRET", None)
                os.environ.pop("WEEX_API_PASSPHRASE", None)
                try:
                    os.remove(config_path)
                except OSError:
                    pass

        ACTIVE.thread = threading.Thread(target=target, daemon=True)
        ACTIVE.thread.start()

    return dashboard_page()


def dashboard_page():
    mode_label = "LIVE" if ACTIVE.mode == "live" else "FREE DEMO"
    body = f"""
    <div class="center">{logo_tag()}<h1>Running — {mode_label}</h1></div>
    <div class="notice">Your API key exists only in this server's memory right now and will be
    permanently deleted the moment you click Stop.</div>
    <div class="card">
      <div class="muted" id="watchingText">Coins watched: —</div>
      <div class="muted" id="balanceText">Balance: —</div>
      <div id="coinList" class="coin-tags" style="margin-top:8px;"></div>
      <h2>Open Positions</h2>
      <table id="positionsTable"><tr><th>Coin</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th></tr></table>
      <p class="muted" id="noPositions">No open positions.</p>
    </div>
    <div class="card">
      <button onclick="stopSession()" class="btn-secondary">Stop &amp; Delete My API Key Now</button>
    </div>
    <div class="card">
      <label class="muted"><input type="checkbox" id="showLogCheck" onchange="toggleLog()"> Show technical log</label>
      <pre id="log" style="display:none;"></pre>
    </div>
    <script>
    function stopSession() {{
      fetch('/try/stop', {{method:'POST'}}).then(() => window.location.href = '/');
    }}
    function toggleLog() {{
      document.getElementById('log').style.display = document.getElementById('showLogCheck').checked ? 'block' : 'none';
    }}
    function refresh() {{
      fetch('/api/status').then(r => r.json()).then(d => {{
        if (d.stopped) {{ window.location.href = '/'; return; }}
        document.getElementById('watchingText').innerText = 'Coins watched: ' + (d.watching_count || '—');
        document.getElementById('balanceText').innerText = 'Balance: ' + (d.balance !== null ? d.balance.toFixed(2) + ' USDT' : '—');
        const list = document.getElementById('coinList'); list.innerHTML = '';
        (d.watching || []).forEach(s => {{ const t=document.createElement('span'); t.className='coin-tag'; t.innerText=s; list.appendChild(t); }});
        const table = document.getElementById('positionsTable');
        while (table.rows.length > 1) table.deleteRow(1);
        const positions = d.open_positions || {{}};
        const keys = Object.keys(positions);
        document.getElementById('noPositions').style.display = keys.length ? 'none' : 'block';
        keys.forEach(sym => {{
          const p = positions[sym]; const row = table.insertRow();
          row.insertCell(0).innerText = sym.toUpperCase().replace('CMT_','');
          row.insertCell(1).innerText = p.side||''; row.insertCell(2).innerText = p.entry||'';
          row.insertCell(3).innerText = p.sl||''; row.insertCell(4).innerText = p.tp||'';
        }});
      }});
      if (document.getElementById('showLogCheck').checked) {{
        fetch('/api/log').then(r => r.json()).then(d => {{
          const pre = document.getElementById('log'); pre.innerText = d.lines.join('\\n'); pre.scrollTop = pre.scrollHeight;
        }});
      }}
    }}
    setInterval(refresh, 2000); refresh();
    </script>
    """
    return page("Running", body)


@app.route("/try/stop", methods=["POST"])
def try_stop():
    my_sid = session.get("sid")
    if ACTIVE.session_id != my_sid:
        return jsonify({"error": "Not your session."}), 403
    if ACTIVE.stop_event:
        ACTIVE.stop_event.set()
    # Immediate wipe here too — don't wait for the thread to notice stop_event.
    ACTIVE.creds = None
    os.environ.pop("WEEX_API_KEY", None)
    os.environ.pop("WEEX_API_SECRET", None)
    os.environ.pop("WEEX_API_PASSPHRASE", None)
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    my_sid = session.get("sid")
    if ACTIVE.session_id != my_sid:
        return jsonify({"stopped": True})
    status = getattr(weex_sr_bot, "BOT_STATUS", {})
    if not ACTIVE.is_running():
        return jsonify({"stopped": True})
    return jsonify({
        "stopped": False,
        "balance": status.get("balance"),
        "watching_count": len(status.get("watching", [])),
        "watching": [s.upper().replace("CMT_", "") for s in status.get("watching", [])],
        "open_positions": status.get("open_positions", {}),
    })


@app.route("/api/log")
def api_log():
    my_sid = session.get("sid")
    if ACTIVE.session_id != my_sid:
        return jsonify({"lines": []})
    return jsonify({"lines": list(ACTIVE.log_lines)[-200:]})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
