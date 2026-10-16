"""
F&O Live Screener — API + UI server (standard library only, no pip installs).

Run:   python3 server.py            (then open http://localhost:8000)
Env:   PORT=8000  UPSTOX_ACCESS_TOKEN=<token>

Endpoints
  GET  /                the UI
  GET  /api/snapshot    market context, funnel, top picks, all 210 stocks
  POST /api/token       {"token": "..."}  switch to live Upstox data
  POST /api/settings    tweak refresh / scan depth / gates on the fly
  POST /api/refresh     force an immediate refresh
  POST /api/demo        back to synthetic data
  POST /api/universe    rebuild the F&O list from Upstox's instrument master
  GET  /replay          the newest session-replay report (static HTML)
  POST /api/replay/build  fetch a finished session's candles and build that report
"""
from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
from engine import DEFAULTS, Engine
from sources import DemoSource, LiveSource, last_session, market_status, session_date
from universe import build_universe
from upstox_client import IST, UpstoxError

BASE = os.path.dirname(os.path.abspath(__file__))
# Normally empty: the app talks to the real api.upstox.com. If someone sets
# UPSTOX_API_ROOT (e.g. to a local stub) we surface it so the UI can warn that
# the feed is not Upstox.
_custom_root = (os.environ.get("UPSTOX_API_ROOT") or "").rstrip("/")
CUSTOM_API_ROOT = "" if (not _custom_root or _custom_root == "https://api.upstox.com") else _custom_root
# Every writable location can be redirected (e.g. to a mounted disk on Render) so the
# warm-up cache, your token and your credentials survive restarts and redeploys.
CACHE = os.environ.get("CACHE_DIR") or os.path.join(BASE, "cache")
LOGS = os.environ.get("LOGS_DIR") or os.path.join(BASE, "logs")
RUNTIME = os.environ.get("RUNTIME_DIR") or os.path.join(BASE, "runtime")
def _ensure_dir(preferred: str, fallback: str) -> str:
    """Use the configured directory if we can create it, else fall back so the app
    still boots (e.g. CACHE_DIR pointed at a Render disk that wasn't mounted)."""
    for d in (preferred, fallback):
        try:
            os.makedirs(d, exist_ok=True)
            if d == fallback and preferred != fallback:
                print(f"[warn] could not use {preferred!r} - falling back to {fallback!r}")
            return d
        except Exception as e:                      # noqa: BLE001
            print(f"[warn] {d!r} unusable ({e})")
    return fallback


CACHE = _ensure_dir(CACHE, os.path.join(BASE, "cache"))
LOGS = _ensure_dir(LOGS, os.path.join(BASE, "logs"))
RUNTIME = _ensure_dir(RUNTIME, os.path.join(BASE, "runtime"))
# Reports and candle caches written by the replay job. LOGS can be redirected
# (e.g. to a mounted disk), so everything replay-related is derived from it
# rather than from the app directory.
REPLAY_DIR = os.path.join(LOGS, "replay")
TOKEN_FILE = os.path.join(RUNTIME, "token.txt")
# The build runs while the live screener keeps polling, and they hold separate
# rate-limit buckets, so the build's history calls are deliberately slower than
# the screener's own (95/min) to stay well inside Upstox's per-minute limit.
REPLAY_HISTORY_PER_MIN = float(os.environ.get("REPLAY_HISTORY_PER_MIN") or 60)
CREDS_FILE = os.path.join(RUNTIME, "credentials.json")
STATIC = os.path.join(BASE, "static")

# --------------------------------------------------------------- access gate
# Set APP_PASSWORD to require a login. Strongly recommended when hosting publicly
# (e.g. Render) — otherwise anyone with the URL sees your screener and settings.
def auth_enabled() -> bool:
    return bool(os.environ.get("APP_PASSWORD", "").strip())


def session_token() -> str:
    """Stable session value derived from the password, so it survives restarts."""
    pw = os.environ.get("APP_PASSWORD", "").strip()
    return hmac.new(pw.encode(), b"fno-screener-v1", hashlib.sha256).hexdigest()


PUBLIC_PATHS = {"/login", "/callback", "/api/health", "/manifest.json",
                "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png",
                "/favicon.ico", "/robots.txt"}


STATE = {
    "engine": None, "source": None, "mode": "starting", "settings": {},
    "public_base": "",          # detected public URL of this app (for the redirect URL)
    "callback_base": "",        # the base the last /callback request arrived on
    "warmup_thread": None, "poller": None,
    "stop": threading.Event(), "wake": threading.Event(), "lock": threading.RLock(),
    "auth_error": None, "last_error": None, "universe": None,
}


# --------------------------------------------------------------------- helpers
def read_saved_token() -> str:
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def save_token(tok: str):
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(tok.strip())
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def load_universe(force=False):
    if STATE["universe"] is None or force:
        STATE["universe"] = build_universe(CACHE, force=force)
    return STATE["universe"]


def build_engine(mode: str, token: str = "") -> Engine:
    uni = load_universe()
    live = mode == "live"
    src = LiveSource(token) if live else DemoSource(uni)
    # caches and logs are namespaced per mode so synthetic demo data can never
    # leak into a live session (and vice versa)
    eng = Engine(src, uni, settings=STATE.get("settings") or {},
                 cache_dir=os.path.join(CACHE, "live" if live else "demo"),
                 log_dir=os.path.join(LOGS, "live" if live else "demo"),
                 demo=not live)
    eng.load_warm_cache()
    STATE["settings"] = dict(eng.settings)
    return eng


def start_engine(mode: str, token: str = ""):
    with STATE["lock"]:
        STATE["stop"].set()
        time.sleep(0.25)                      # let the old threads notice
        STATE["stop"] = threading.Event()
        STATE["wake"] = threading.Event()
        eng = build_engine(mode, token)
        STATE["engine"] = eng
        STATE["mode"] = "demo" if mode != "live" else "live"
        STATE["auth_error"] = None
        STATE["last_error"] = None

        def warm():
            try:
                eng.warmup()
            except Exception as e:            # noqa: BLE001
                STATE["last_error"] = str(e)

        threading.Thread(target=warm, name="warmup", daemon=True).start()

        def loop():
            last_deep = 0.0
            while not STATE["stop"].is_set():
                cur = STATE["engine"]
                if cur is None:
                    break
                try:
                    due = (time.time() - last_deep) >= cur._deep_deadline_sec()
                    cur.poll(do_deep=due)
                    if due:
                        last_deep = time.time()
                    STATE["auth_error"] = None
                except UpstoxError as e:
                    msg = e.friendly()
                    STATE["last_error"] = msg
                    # A network failure is transient — keep retrying. Only a real
                    # rejection (401/403) or an explicit token complaint stops the loop.
                    if not e.network and (e.status in (401, 403) or "token" in msg.lower()):
                        STATE["auth_error"] = msg
                        STATE["stop"].set()   # stop hammering an invalid token
                except Exception as e:        # noqa: BLE001
                    STATE["last_error"] = f"{type(e).__name__}: {e}"
                STATE["wake"].wait(timeout=float(cur.settings["refresh_sec"]))
                STATE["wake"].clear()

        threading.Thread(target=loop, name="poller", daemon=True).start()


def boot():
    tok = read_saved_token()
    start_engine("live", tok) if tok else start_engine("demo")


# ------------------------------------------------------------------- upstox auth
def credentials() -> dict:
    return auth.load_credentials(RUNTIME)


def api_credentials(body: dict) -> dict:
    key = str((body or {}).get("api_key") or "").strip()
    secret = str((body or {}).get("api_secret") or "").strip()
    if not key or not secret:
        return {"ok": False, "error": "Both the API key and API secret are required."}, 400
    creds = credentials()
    creds.update({"api_key": key, "api_secret": secret})
    if body.get("redirect_uri"):
        creds["redirect_uri"] = str(body["redirect_uri"]).strip()
    auth.save_credentials(RUNTIME, creds)
    return {"ok": True, "message": "Credentials saved."}, 200


def api_auth_code(body: dict) -> dict:
    """Exchange an auth code (or finish a manual paste) and go live."""
    raw = str((body or {}).get("code") or "").strip()
    if not raw:
        return {"ok": False, "error": "Paste the code from the redirect URL."}, 400
    # accept a full redirect URL, a query string, or a bare code
    code = raw
    if "code=" in raw:
        query = raw.split("?", 1)[-1].replace("?", "&")
        code = (urllib.parse.parse_qs(query).get("code") or [raw])[0]
    code = urllib.parse.unquote(code.strip().strip('"').strip("'"))
    creds = credentials()
    if not creds["api_key"] or not creds["api_secret"]:
        return {"ok": False, "error": "Save your API key and secret first, then run the login."}, 400
    redirect_uri = creds.get("redirect_uri") or STATE.get("callback_base") or         auth.self_redirect_url("localhost:8000")
    try:
        token, meta = auth.exchange_code(code, creds["api_key"], creds["api_secret"], redirect_uri)
    except auth.AuthError as e:
        return {"ok": False, "error": str(e)}, 400
    save_token(token)
    start_engine("live", token)
    who = ""
    if isinstance(meta.get("user_id"), str):
        who = meta["user_id"]
    return {"ok": True, "mode": "live", "user": who,
            "message": "Login complete — live data is flowing."}, 200


def auth_status(host_header: str = "") -> dict:
    creds = credentials()
    auto = auth.self_redirect_url(host_header) if host_header else ""
    if auto and not auto.startswith("http://localhost"):
        STATE["public_base"] = auto.replace("/callback", "")
    # prefer the host the user is actually browsing on, then anything /callback learned,
    # then whatever was explicitly saved, then localhost
    redirect_uri = auto or ((STATE.get("callback_base") or "") + "/callback" if STATE.get("callback_base") else "") \
        or creds.get("redirect_uri") or ""
    local = f"http://localhost:{os.environ.get('PORT', '8000')}/callback"
    return {
        "api_key_set": bool(creds["api_key"]),
        "api_secret_set": bool(creds["api_secret"]),
        "api_key_hint": (creds["api_key"][:6] + "…" + creds["api_key"][-4:]) if creds["api_key"] else "",
        "redirect_uri": redirect_uri,
        "redirect_uri_local": local,
        "login_url": auth.login_url(creds["api_key"], redirect_uri or local, creds.get("state", ""))
        if creds["api_key"] else "",
        "token_present": bool(read_saved_token()),
    }


# ------------------------------------------------------------------------ API
REPLAY_MISSING_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Session replay \u00b7 F&amp;O screener</title>
<style>body{margin:0;background:#070a10;color:#e9eefb;
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
 .w{max-width:720px;margin:0 auto;padding:52px 20px}a{color:#4c8dff;text-decoration:none}
 code{background:#131b28;border:1px solid rgba(148,170,210,.18);border-radius:6px;padding:2px 7px;
 font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
 h1{font-size:19px;margin:0 0 6px}p{color:#8a97b4}
 pre{background:#0e141f;border:1px solid rgba(148,170,210,.10);border-radius:12px;padding:14px 16px;
 overflow:auto;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;color:#c3cede}
 button{font:inherit;font-weight:600;cursor:pointer;border:none;border-radius:10px;padding:11px 18px;
 background:linear-gradient(135deg,#3b7cf6,#6d8fff);color:#fff;box-shadow:0 3px 12px rgba(76,141,255,.3)}
 button:disabled{opacity:.55;cursor:progress}
 #st{margin-top:12px;font-size:13.5px;color:#8a97b4;min-height:20px}
 .bar{height:6px;border-radius:4px;background:#131b28;overflow:hidden;margin:10px 0 0;display:none}
 .bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#3b7cf6,#2ee6a8)}
 .err{color:#ff8b9c}</style>
</head><body><div class="w">
<h1>No session replay built yet</h1>
<p>The replay walks a finished session through the real scoring engine, one minute at a
time, and writes a filterable page per session. Build one now — it fetches that
session's 1-minute candles from Upstox, so it takes a few minutes the first time and
seconds after that.</p>
<p><button id="go">Build the replay</button></p>
<div class="bar" id="bar"><i id="barfill"></i></div>
<div id="st"></div>
<p style="margin-top:22px" class="dim">Keep this tab (or the live screen) open while it builds.
You can also run it by hand with a token:</p>
<pre>python3 replay.py --session YYYY-MM-DD
python3 replay_report.py</pre>
<p><a href="/">&larr; back to the live screen</a></p>
</div>
<script>
const $ = id => document.getElementById(id);
const T = {starting: [0, 0], fetching: [15, 85], replaying: [88, 11], ready: [100, 0], error: [0, 0]};
let timer = null;
function show(job){
  if(!job || !job.state || job.state === 'idle'){
    $('go').disabled = false;
    $('bar').style.display = 'none';
    $('st').className = '';
    $('st').textContent = '';
    if(timer){ clearInterval(timer); timer = null; }
    return;
  }
  const [base, span] = T[job.state] || [0, 0];
  const frac = job.total ? Math.min(1, (job.done || 0) / job.total) : 0;
  if(job.state === 'error'){
    $('st').className = 'err';
    $('st').textContent = job.error || 'The build failed.';
    $('go').disabled = false;
    $('bar').style.display = 'none';
    if(timer){ clearInterval(timer); timer = null; }
    return;
  }
  if(job.state === 'ready'){
    $('st').textContent = 'Done — opening ' + (job.report || 'the report') + '…';
    location.reload();
    return;
  }
  $('go').disabled = true;
  $('bar').style.display = 'block';
  $('barfill').style.width = Math.round(base + span * frac) + '%';
  const mine = job.total ? ' (' + job.done + '/' + job.total + ')' : '';
  $('st').textContent = (job.state === 'replaying'
    ? 'Replaying the session minute by minute' + (job.note ? ' — at ' + job.note : '')
    : 'Fetching historical candles' + mine) + '…';
}
async function poll(){
  try{
    const r = await fetch('/api/snapshot', {cache: 'no-store'});
    const j = await r.json();
    show((j.app && j.app.replay && j.app.replay.job) || null);
  }catch(e){ /* keep the last message */ }
}
$('go').onclick = async () => {
  $('go').disabled = true;
  $('st').className = '';
  $('st').textContent = 'Starting…';
  try{
    const r = await fetch('/api/replay/build', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}'});
    const j = await r.json();
    if(!j.ok){
      $('st').className = 'err';
      $('st').textContent = j.error || 'Could not start the build.';
      $('go').disabled = false;
      return;
    }
    if(!timer) timer = setInterval(poll, 4000);
    poll();
  }catch(e){
    $('st').className = 'err';
    $('st').textContent = 'Could not reach the server.';
    $('go').disabled = false;
  }
};
poll();
</script>
</body></html>"""


# ------------------------------------------------------------ replay build job
# A build fetches a whole session's candles (~2 calls per stock, rate limited to
# well under a hundred a minute), so it runs on its own thread and reports
# progress through /api/snapshot. The UI and the "no report yet" page both read
# it from there.
REPLAY_JOB = {
    "state": "idle",        # idle | starting | fetching | replaying | ready | error
    "phase": "", "done": 0, "total": 0, "note": "",
    "session": "", "started": "", "ended": "", "error": "", "report": "",
    "source": "",
}
JOB_LOCK = threading.Lock()


def replay_job_info() -> dict:
    with JOB_LOCK:
        return dict(REPLAY_JOB)


def _job_set(**kw):
    with JOB_LOCK:
        REPLAY_JOB.update(kw)


def _now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _replay_worker(session: str, mode: str, token: str, step: int):
    """Fetch that session's candles, replay them, write the report. Never raises."""
    try:
        import replay as R
        import replay_report as RR

        uni = load_universe()
        if mode == "live":
            src = LiveSource(token, history_per_min=REPLAY_HISTORY_PER_MIN)
            src_label = "upstox"
        else:
            src = DemoSource(uni)
            src_label = "demo"
        _job_set(source=src_label)

        def progress(phase, done, total, note):
            _job_set(state=phase, phase=phase, done=int(done), total=int(total), note=note)

        res = R.build_session(session, src, uni, step=step, progress=progress)
        out, frames = RR.build(res["frames_json"])
        _job_set(state="ready", phase="ready", report=os.path.basename(out),
                 session=res["session"], done=res["stamps"], total=res["stamps"],
                 note=f"{res['count']} stock-sides qualified", ended=_now_ist())
        print(f"[replay] {res['session']} ready: {res['count']} stock-sides, "
              f"{res['stamps']} minutes -> {out}")
    except Exception as e:                                   # noqa: BLE001
        msg = e.friendly() if isinstance(e, UpstoxError) else f"{type(e).__name__}: {e}"
        _job_set(state="error", phase="error", error=msg, ended=_now_ist())
        print(f"[replay] build failed: {msg}")


def start_replay_build(body: dict) -> tuple[dict, int]:
    """POST /api/replay/build — kick the job off and return immediately."""
    session = str(body.get("session") or "").strip()
    if session and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", session):
        return {"ok": False, "error": "session must look like 2026-10-16"}, 400
    mode = STATE.get("mode") or "demo"
    token = str(body.get("token") or "").strip() or read_saved_token()
    if mode == "live" and not token:
        return {"ok": False, "error": "No Upstox token yet — paste one in Settings, "
                                      "then build the replay."}, 400
    with JOB_LOCK:
        if REPLAY_JOB["state"] in ("starting", "fetching", "replaying"):
            return {"ok": False, "error": "A replay build is already running.",
                    "job": dict(REPLAY_JOB)}, 409
    sess = session or last_session()
    try:
        step = max(1, min(15, int(body.get("step") or 1)))
    except (TypeError, ValueError):
        step = 1
    _job_set(state="starting", phase="starting", done=0, total=0, session=sess,
             started=_now_ist(), ended="", error="", report="", note="")
    threading.Thread(target=_replay_worker, args=(sess, mode, token, step),
                     name="replay-build", daemon=True).start()
    return {"ok": True, "session": sess, "job": replay_job_info()}, 200


def replay_info() -> dict:
    """Newest session-replay report on disk, so the UI can label the Replay button.

    Reports are static HTML written by replay_report.py; this only reports what
    exists and never builds anything.
    """
    try:
        reports = glob.glob(os.path.join(REPLAY_DIR, "report_*.html"))
    except OSError:
        return {}
    if not reports:
        return {}
    newest = max(reports, key=os.path.getmtime)
    name = os.path.basename(newest)
    sess = name[len("report_"):-len(".html")] if name.startswith("report_") else ""
    try:
        built = datetime.fromtimestamp(os.path.getmtime(newest), IST).isoformat(timespec="seconds")
    except OSError:
        built = ""
    return {"session": sess, "built": built, "file": name}


def ui_version() -> str:
    """Short hash of the current index.html — changes whenever the UI changes."""
    try:
        with open(os.path.join(STATIC, "index.html"), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return ""


def app_info() -> dict:
    """Everything the UI needs to know about the server itself (no engine needed)."""
    return {
        "password_gate": auth_enabled(),
        "persistent_dirs": {"cache": CACHE, "logs": LOGS, "runtime": RUNTIME},
        # Lets an open tab notice that the UI on disk has changed.
        "ui_version": ui_version(),
        # Set only when UPSTOX_API_ROOT points away from Upstox itself. The UI
        # warns loudly in that case, so a stand-in feed can never be mistaken
        # for real market data.
        "custom_api_root": CUSTOM_API_ROOT,
        # Session the /replay report covers, any build in flight, and the session
        # the Replay button would build next.
        "replay": {**replay_info(), "job": replay_job_info(),
                   "last_session": last_session()},
    }


def api_snapshot(host_header: str = "") -> dict:
    with STATE["lock"]:
        eng, mode = STATE["engine"], STATE["mode"]
        auth_error, last_error = STATE["auth_error"], STATE["last_error"]
    if eng is None:
        return {"ok": False, "error": "engine not started"}
    data = eng.snapshot()
    data["market"] = market_status()
    data["session_date"] = session_date()
    data["token_present"] = bool(read_saved_token())
    data["auth_error"] = auth_error
    data["last_error"] = last_error
    data["settings"] = eng.settings
    data["mode"] = mode
    data["auth"] = auth_status(host_header)
    data["app"] = app_info()
    return data


def api_token(tok: str) -> dict:
    tok = (tok or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if len(tok) < 20:
        return {"ok": False, "error": "That doesn't look like an access token."}, 400
    try:
        probe = LiveSource(tok)
        q = probe.quotes(["NSE_INDEX|Nifty 50"]) or probe.ltp(["NSE_INDEX|Nifty 50"])
        if not q:
            raise UpstoxError("Token accepted but no market data came back.")
    except UpstoxError as e:
        return {"ok": False, "error": e.friendly()}, 400
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "error": f"Could not reach Upstox: {e}"}, 400
    save_token(tok)
    start_engine("live", tok)
    if CUSTOM_API_ROOT:
        return {"ok": True, "mode": "live",
                "message": f"Connected — but this server is pointed at {CUSTOM_API_ROOT}, "
                           f"not Upstox. The feed is simulated."}, 200
    return {"ok": True, "mode": "live", "message": "Live mode on — pulling real quotes now."}, 200


INT_RANGES = {"refresh_sec": (5, 120), "deep_depth": (5, 120),
              "picks_per_side": (1, 10), "sector_cap": (1, 5)}


def api_settings(body: dict) -> dict:
    eng = STATE["engine"]
    if eng is None:
        return {"ok": False, "error": "engine not started"}, 503
    for k, v in (body or {}).items():
        if k not in DEFAULTS and k not in eng.settings:
            continue
        try:
            if k == "ban_list":
                v = [str(x).strip().upper() for x in (v or []) if str(x).strip()]
            elif k == "tv_url":
                v = str(v or "").strip()[:300]
                if v and not re.match(r"^https?://", v):
                    continue          # a chart link, or nothing at all
            elif k in ("show_longs", "show_shorts"):
                v = bool(v)
            elif k in INT_RANGES:
                lo, hi = INT_RANGES[k]
                v = max(lo, min(hi, int(v)))
            else:
                v = float(v)
        except (TypeError, ValueError):
            continue
        eng.settings[k] = v
    STATE["settings"] = dict(eng.settings)
    STATE["wake"].set()
    return {"ok": True, "settings": eng.settings}, 200


def api_demo(body) -> dict:
    start_engine("demo")
    return {"ok": True, "mode": "demo"}, 200


def api_refresh(body) -> dict:
    STATE["wake"].set()
    return {"ok": True, "message": "refresh queued"}, 200


def api_rebuild(body) -> dict:
    return _rebuild()


ROUTES_POST = {
    "/api/token": lambda b: api_token(b.get("token", "")),
    "/api/credentials": lambda b: api_credentials(b),
    "/api/auth-code": lambda b: api_auth_code(b),
    "/api/settings": lambda b: api_settings(b),
    "/api/demo": api_demo,
    "/api/refresh": api_refresh,
    "/api/universe": api_rebuild,
    "/api/replay/build": start_replay_build,
}


def _rebuild():
    try:
        u = load_universe(force=True)
        start_engine(STATE["mode"], read_saved_token())
        return {"ok": True, "count": u["count"], "built_at": u["built_at"]}, 200
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "error": str(e)}, 500


ROUTES_GET = {
    "/api/snapshot": lambda: (api_snapshot(), 200),
    "/api/health": lambda: ({"ok": True, "mode": STATE["mode"],
                             "ist": datetime.now(IST).isoformat(timespec="seconds"),
                             "cycle": (getattr(STATE["engine"], "state_meta", {}) or {}).get("cycle")}, 200),
}


def callback_page(ok: bool, title: str, detail: str, code: str = "") -> bytes:
    color = "#22c55e" if ok else "#ef4444"
    icon = "✅" if ok else "⚠️"
    if code:
        codeblock = ('<div class="code"><div class="lbl">authorization code</div>'
                     '<code id="c">' + code + '</code>'
                     '<button onclick="copyCode()">copy</button></div>'
                     '<script>function copyCode(){navigator.clipboard.writeText('
                     'document.getElementById("c").textContent);}</script>')
    else:
        codeblock = ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Upstox login</title></head>
<body style="margin:0;background:#0b0e14;color:#e8edf7;font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<div style="max-width:560px;margin:8vh auto;padding:26px;background:#141b2b;border:1px solid #232d45;border-radius:16px">
  <div style="font-size:34px">{icon}</div>
  <h1 style="font-size:19px;margin:10px 0 6px;color:{color}">{title}</h1>
  <p style="color:#8b98b4;margin:0 0 18px">{detail}</p>
  {codeblock}
  <a href="/" style="display:inline-block;margin-top:8px;background:linear-gradient(135deg,#3b7cf6,#6d8fff);
     color:#fff;text-decoration:none;padding:10px 18px;border-radius:10px;font-weight:600">Open the screener →</a>
</div>
<style>
.code{{background:#0b0e14;border:1px solid #232d45;border-radius:10px;padding:12px;margin-bottom:14px}}
.code .lbl{{font-size:11px;color:#5f6d8c;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}}
.code code{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;word-break:break-all}}
.code button{{margin-left:10px;background:#1a2337;border:1px solid #232d45;color:#e8edf7;
  border-radius:8px;padding:4px 10px;cursor:pointer;font-size:12px}}
</style></body></html>""".encode()


def login_page(bad: bool = False) -> bytes:
    border = "#7f2226" if bad else "#232d45"
    ph = "wrong password - try again" if bad else "password"
    return ("""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0e14"><title>F&O Screener - sign in</title></head>
<body style="margin:0;background:#0b0e14;color:#e8edf7;font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<form method="POST" action="/login" style="max-width:340px;margin:16vh auto;padding:24px;background:#141b2b;
      border:1px solid #232d45;border-radius:16px">
  <div style="font-size:30px">&#9889;</div>
  <h1 style="font-size:18px;margin:8px 0 4px">F&amp;O Live Screener</h1>
  <p style="color:#8b98b4;margin:0 0 16px;font-size:13px">Enter the app password to continue.</p>
  <input type="password" name="password" autofocus autocomplete="current-password" placeholder="__PH__"
    style="width:100%;box-sizing:border-box;background:#0b0e14;border:1px solid __BORDER__;color:#e8edf7;
    border-radius:10px;padding:12px;font-size:16px">
  <button type="submit" style="width:100%;margin-top:10px;padding:12px;border:none;border-radius:10px;
    background:linear-gradient(135deg,#3b7cf6,#6d8fff);color:#fff;font-weight:700;font-size:15px">Sign in</button>
  <p style="color:#5f6d8c;font-size:12px;margin:14px 0 0">Set APP_PASSWORD in your hosting environment.</p>
</form></body></html>""").replace("__PH__", ph).replace("__BORDER__", border).encode()


def manifest_json() -> bytes:
    return json.dumps({
        "name": "F&O Live Screener", "short_name": "F&O Screener",
        "description": "Live NSE F&O screener: filters all 210 F&O stocks down to the best few.",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#0b0e14", "theme_color": "#0b0e14", "orientation": "portrait-primary",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, indent=1).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "FnoScreener/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # keep the console readable
        if "error" in str(fmt).lower() or "500" in str(args):
            print(f"[http] {fmt % args}")

    def _send(self, obj, status=200, ctype="application/json"):
        if isinstance(obj, bytes):
            body = obj
        elif isinstance(obj, str):          # e.g. the HTML pages above
            body = obj.encode("utf-8")
        else:
            body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_replay(self):
        """Serve the newest session-replay report built by replay_report.py.

        It is a single self-contained HTML file (no assets, no API calls), so it
        is served straight off disk and can never interfere with the live screen.
        """
        reports = glob.glob(os.path.join(REPLAY_DIR, "report_*.html"))
        if not reports:
            return self._send(REPLAY_MISSING_PAGE, 200, "text/html")
        newest = max(reports, key=os.path.getmtime)
        try:
            with open(newest, "rb") as f:
                return self._send(f.read(), 200, "text/html")
        except OSError:
            return self._send(REPLAY_MISSING_PAGE, 200, "text/html")

    def _host(self) -> str:
        return (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()

    def do_GET(self):                            # noqa: N802
        path = self.path.split("?")[0]
        if path == "/login":
            return self._send(login_page(), 200, "text/html")
        if path == "/manifest.json":
            return self._send(manifest_json(), 200, "application/manifest+json")
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/favicon.ico"):
            fp = os.path.join(STATIC, path.lstrip("/"))
            if not os.path.exists(fp) and path == "/favicon.ico":
                fp = os.path.join(STATIC, "favicon.png")
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(f.read(), 200, "image/png")
            return self._send(b"", 404, "image/png")
        if path == "/robots.txt":
            return self._send(b"User-agent: *\nDisallow: /\n", 200, "text/plain")
        if self._gate():
            return
        if path == "/callback":
            return self._handle_callback()
        if path in ("/", "/index.html", "/static/index.html"):
            fp = os.path.join(STATIC, "index.html")
            try:
                with open(fp, "rb") as f:
                    body = f.read()
                # Stamp the page with a hash of its own content. An open tab keeps
                # rebuilding its table from /api/snapshot using the template it was
                # loaded with, so without this a stale tab never notices new code.
                ver = ui_version().encode()
                body = body.replace(b"__UI_VERSION__", ver)
                return self._send(body, 200, "text/html")
            except OSError:
                return self._send({"ok": False, "error": "index.html missing"}, 500)
        if path == "/replay":
            return self._serve_replay()
        if path == "/api/snapshot":
            obj, status = api_snapshot(self._host()), 200
            return self._send(obj, status)
        fn = ROUTES_GET.get(path)
        if fn:
            obj, status = fn()
            return self._send(obj, status)
        if path.startswith("/static/"):
            fp = os.path.join(BASE, path.lstrip("/"))
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self._send(f.read(), 200, "text/plain")
        self._send({"ok": False, "error": "not found"}, 404)

    def _authed(self) -> bool:
        if not auth_enabled():
            return True
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"fnoscreen=([^;\s]+)", cookie)
        return bool(m) and hmac.compare_digest(m.group(1), session_token())

    def _gate(self) -> bool:
        """Return True if the request was already handled (blocked/redirected)."""
        if self._authed() or self.path.split("?")[0] in PUBLIC_PATHS:
            return False
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            self._send({"ok": False, "error": "not signed in", "login": "/login"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return True

    def _handle_login(self, raw_body: bytes = b""):
        if not auth_enabled():
            return self._redirect("/")
        data = {}
        try:
            data = {k: v[0] for k, v in urllib.parse.parse_qs(raw_body.decode()).items()}
        except Exception:
            pass
        if hmac.compare_digest((data.get("password") or ""), os.environ.get("APP_PASSWORD", "").strip()):
            self.send_response(302)
            self.send_header("Location", "/")
            # 30 days; HttpOnly + SameSite so it works from a home-screen app too
            self.send_header("Set-Cookie",
                             f"fnoscreen={session_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(login_page(bad=True), 401, "text/html")

    def _redirect(self, to: str):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_callback(self):
        """Upstox redirects the browser here with ?code=... after login.

        Tolerant about how the query string arrives: some hosts prepend their own
        query params (?e2b-traffic-access-token=...?code=XYZ), so we normalise
        '?' to '&' inside the query and look for `code` anywhere.
        """
        from urllib.parse import parse_qs
        raw = self.path.split("?", 1)[1] if "?" in self.path else ""
        normalised = raw.replace("?", "&")
        q = parse_qs(normalised, keep_blank_values=True)
        err = (q.get("error") or q.get("error_description") or [""])[0]
        code = (q.get("code") or [""])[0].strip()
        host = self._host()
        if host:
            scheme = "http" if host.startswith(("localhost", "127.0.0.1", "0.0.0.0")) else "https"
            STATE["callback_base"] = f"{scheme}://{host}"
        if err:
            return self._send(callback_page(False, "Login was not completed",
                                            f"Upstox returned: <b>{err}</b>. "
                                            "Close this tab and start the login again."),
                              200, "text/html")
        if not code:
            return self._send(callback_page(False, "No authorization code in this link",
                                            "This page is the OAuth callback for the screener. If you "
                                            "expected a login to complete, start it again from "
                                            "Settings &rarr; Connect Upstox."), 200, "text/html")
        creds = credentials()
        if not creds["api_key"] or not creds["api_secret"]:
            return self._send(callback_page(
                False, "Code received — now paste it into the screener",
                "The code is below (it's also in this page's address bar). Open the screener &rarr; "
                "Settings &rarr; Connect Upstox &rarr; paste it in the &lsquo;code&rsquo; box. "
                "(Save your API key + secret first and this step happens automatically next time.)",
                code=code), 200, "text/html")
        try:
            token, meta = auth.exchange_code(code, creds["api_key"], creds["api_secret"],
                                             (STATE.get("callback_base") or "") + "/callback")
        except auth.AuthError as e:
            return self._send(callback_page(
                False, "Upstox rejected the code exchange", str(e), code=code), 200, "text/html")
        save_token(token)
        start_engine("live", token)
        who = meta.get("user_name") or meta.get("user_id") or ""
        return self._send(callback_page(
            True, "Connected to Upstox — live data is on",
            f"Access token received{' for ' + str(who) if who else ''} and saved on this machine. "
            "The screener is now pulling real quotes."), 200, "text/html")

    def do_POST(self):                           # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/login":
            return self._handle_login(raw)
        if self._gate():
            return
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            body = {}
        fn = ROUTES_POST.get(path)
        if not fn:
            return self._send({"ok": False, "error": "not found"}, 404)
        try:
            obj, status = fn(body)
        except Exception as e:                   # noqa: BLE001
            obj, status = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500
        self._send(obj, status)


def main():
    port = int(os.environ.get("PORT", "8000"))
    boot()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"\n  F&O Live Screener  ->  http://localhost:{port}")
    print(f"  mode: {STATE['mode']}   (paste your Upstox token in Settings to go live)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        STATE["stop"].set()


if __name__ == "__main__":
    main()
