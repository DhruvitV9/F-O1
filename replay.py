"""
Replay a past session through the real scoring engine.

WHY
  The UI records the first moment a stock appears in the Long/Short table for a
  given filter. This module answers the obvious follow-up: for a filter you care
  about, when did each stock first qualify during the last session — and how long
  did it stay? That is the backtest behind the "First seen" column.

HOW IT STAYS FAITHFUL
  It does not re-implement the screener. It reuses the real Engine and feeds it a
  ReplaySource that serves candles and quotes exactly as they would have looked at
  a chosen minute of the session, while engine.REPLAY_NOW pins the engine's clock
  to that same minute. So RVOL pace, the turnover projection and the session phase
  are all computed for the replayed moment, not for wall-clock time — and the
  membership rule is literally the one the UI uses.

RUN
  python3 replay.py                          # last session, default filters
  python3 replay.py --rvol 1.8 --cap 35      # your filter
  python3 replay.py --step 5 --side short    # coarser steps, short table only
  python3 replay.py --collect-only           # just fetch + cache the candles

  Needs a working Upstox token (or the demo/stub source) to collect candles.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from datetime import datetime, timedelta

import engine as E
from engine import DEFAULTS, Engine
from sources import DemoSource, LiveSource, session_date
from upstox_client import IST, UpstoxError, parse_ts
from universe import build_universe

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("CACHE_DIR") or os.path.join(BASE, "cache")
LOGS = os.environ.get("LOGS_DIR") or os.path.join(BASE, "logs")
REPLAY_CACHE = os.path.join(CACHE, "replay")

# same rule the UI applies to the two tables
SIDE_FLOOR = 50


def side_of(rec: dict, want: str) -> bool:
    a = rec.get("score_long" if want == "long" else "score_short") or 0
    b = rec.get("score_short" if want == "long" else "score_long") or 0
    return a >= SIDE_FLOOR and a > b


def passes(rec: dict, want: str, rvol_min, cap) -> bool:
    """The exact predicate the tables use, plus the user's filter."""
    if not side_of(rec, want):
        return False
    if rvol_min is not None and (rec.get("rvol") or 0) < rvol_min:
        return False
    if cap is not None:
        other = rec.get("score_short" if want == "long" else "score_long") or 0
        if other > cap:
            return False
    return True


# ------------------------------------------------------------------ collect ---
def minutes_for(src, key: str, session: str) -> list:
    """1-minute candles for ONE finished session.

    The intraday endpoint only ever serves the session in progress, so a session
    that is not today has to come from the dated historical route. We try that
    first for a past date and fall back to intraday, which also covers sources
    (and test fixtures) that only implement the intraday call.
    """
    hist = getattr(src, "historical", None)
    if session != datetime.now(IST).date().isoformat() and callable(hist):
        try:
            rows = hist(key, "minutes", 1, session, session) or []
            rows = [c for c in rows if str(c[0])[:10] == session]
            if len(rows) >= 3:
                return rows
        except UpstoxError:
            raise
        except Exception:
            pass
    rows = src.intraday(key, "minutes", 1) or []
    return [c for c in rows if str(c[0])[:10] == session]


def build_session(session: str, src, universe: dict, step: int = 1,
                  filters: dict | None = None, progress=None, force: bool = False,
                  cache_dir: str | None = None, log_dir: str | None = None) -> dict:
    """Everything a replay needs, in one call: collect -> replay -> dump -> report.

    This is what the server's build job runs, using the same functions the CLI
    uses (collect -> run_replay -> export_frames), so a report built from the
    Replay button is the same page a hand-run build produces.

    `progress(phase, done, total, note)` is called with phase='fetching' while the
    candles come in and phase='replaying' while the session runs.
    """
    def emit(phase):
        def cb(*a):
            if not progress or not a:
                return
            if len(a) == 2:
                done, total, note = a[0], a[1], ""
            else:
                done, total, note = a[0], a[1], (a[2].strftime("%H:%M") if a[2] else "")
            progress(phase, done, total, note)
        return cb

    log_dir = log_dir or os.path.join(LOGS, "replay")
    candle_cache = os.path.join(cache_dir, "replay") if cache_dir else None
    data = collect(src, universe, session, force=force, progress=emit("fetching"),
                   cache_dir=candle_cache)
    minutes = session_minutes(data)
    if not minutes:
        raise RuntimeError(
            f"Upstox returned no 1-minute candles for {session}. A replay needs the "
            f"dated historical route to have that session's minutes (minutes/1 reaches "
            f"back about a month).")
    res = run_replay(data, universe, filters or {}, step=step, cache_dir=cache_dir,
                     log_dir=log_dir, progress=emit("replaying"))
    frames_path = os.path.join(log_dir, f"frames_{session}.json")
    export_frames(res, frames_path, source=source_label(data),
                  recorded=recorded_picks(session))
    return res


def collect(src, universe: dict, session: str, force: bool = False, progress=None,
            cache_dir: str | None = None) -> dict:
    """Fetch daily + 1-minute candles for every key and cache them for one session.

    Daily candles are stored WITHOUT the replayed session itself, so the engine's
    'previous session' reference (PDH/PDL/ATR) is the session before the one being
    replayed, whatever wall-clock time the replay runs at.

    `cache_dir` overrides where the candle cache lives (tests use a temp dir so a
    fixture can never be mistaken for a real session's candles later on).
    """
    cache_dir = cache_dir or REPLAY_CACHE
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"candles_{session}.json.gz")
    if os.path.exists(path) and not force:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("daily") and data.get("minutes"):
                return data
        except Exception:
            pass

    daily: dict[str, list] = {}
    minutes: dict[str, list] = {}
    stocks = universe["stocks"]
    eq_keys = [s["equity_key"] for s in stocks]
    # Minute candles are needed for the cash quote, the futures quote (OI) and the
    # index breadth strip, so every key gets them. Daily candles are only ever
    # read for the cash instrument (engine.py: `src.daily(st["equity_key"], 60)`),
    # so asking for a futures or index daily series would be ~200 wasted calls on
    # a 210-stock universe — a quarter of the build's API budget.
    keys = list(dict.fromkeys(eq_keys
                              + [s["fut_key"] for s in stocks if s.get("fut_key")]
                              + list((universe.get("index_keys") or {}).values())))
    want_daily = set(eq_keys)
    total = len(keys)
    errs = 0
    for i, key in enumerate(keys):
        if key in want_daily:
            try:
                rows = src.daily(key, 60) or []
                # drop the replayed session's own daily row (it is a partial candle)
                rows = [r for r in rows if str(r[0])[:10] < session]
                if len(rows) >= 5:
                    daily[key] = rows
            except UpstoxError:
                errs += 1
            except Exception:
                errs += 1
        try:
            mins = minutes_for(src, key, session)
            if len(mins) >= 3:
                minutes[key] = mins
        except UpstoxError:
            errs += 1
        except Exception:
            errs += 1
        if progress and (i % 10 == 0 or i == total - 1):
            progress(i + 1, total)

    data = {"session": session, "daily": daily, "minutes": minutes,
            "keys": total, "errors": errs,
            # where these candles actually came from, so the report can say so
            "api_root": _provenance(src),
            "collected_at": datetime.now(IST).isoformat(timespec="seconds")}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    return data


# ------------------------------------------------------------- replay source --
class ReplaySource:
    """Serves quotes/candles as they would have looked at one moment in a session."""

    def __init__(self, data: dict, clock: datetime, spread_bps: float = 2.0):
        self.daily_map = data.get("daily") or {}
        self.min_map = data.get("minutes") or {}
        self.clock = clock
        self.spread_bps = spread_bps
        self.stats = {"calls": 0, "errors": 0, "last_error": "", "last_error_at": None}
        # Requests for keys this dump does not hold. A build reports them so it can
        # prove it fetched everything the engine reads (and nothing else).
        self.misses = {"daily": 0, "minutes": 0}

    # ---- candle views ----
    def _mins(self, key: str) -> list:
        if key not in self.min_map:
            self.misses["minutes"] += 1
        t = self.clock.isoformat()
        return [r for r in self.min_map.get(key, []) if str(r[0]) <= t]

    def daily(self, key, days=60):
        if key not in self.daily_map:
            self.misses["daily"] += 1
        return self.daily_map.get(key, [])

    def intraday(self, key, unit="minutes", interval=1):
        rows = self._mins(key)
        return rows if len(rows) >= 3 else []

    # ---- quote views, shaped exactly like the real API ----
    def _prev_close(self, key) -> float:
        rows = self.daily_map.get(key) or []
        return float(rows[-1][4]) if rows else 0.0

    def _quote(self, key: str) -> dict | None:
        mins = self._mins(key)
        if not mins:
            return None
        pc = self._prev_close(key) or float(mins[0][1])
        ltp = float(mins[-1][4])
        o = float(mins[0][1])
        hi = max(float(m[2]) for m in mins)
        lo = min(float(m[3]) for m in mins)
        vol = sum(float(m[5] or 0) for m in mins)
        gross = sum(((float(m[2]) + float(m[3]) + float(m[4])) / 3.0) * float(m[5] or 0) for m in mins)
        vwap = (gross / vol) if vol else ltp
        spread = max(0.01, ltp * self.spread_bps / 10000.0)
        q = {
            "instrument_token": key, "last_price": round(ltp, 2),
            "ohlc": {"open": round(o, 2), "high": round(hi, 2), "low": round(lo, 2), "close": round(pc, 2)},
            "volume": int(vol), "average_price": round(vwap, 2),
            "prev_close_price": round(pc, 2), "net_change": round(ltp - pc, 2),
            "lower_circuit_limit": round(pc * 0.8, 2), "upper_circuit_limit": round(pc * 1.2, 2),
            "timestamp": int(self.clock.timestamp() * 1000),
            "depth": {"buy": [{"price": round(ltp - spread, 2), "quantity": 1500, "orders": 6}],
                      "sell": [{"price": round(ltp + spread, 2), "quantity": 1600, "orders": 7}]},
        }
        # futures carry open interest in the candle payload when the feed has it
        if "|" not in key.split("FUT")[0] and key.count("|") == 1 and key.split("|")[1].endswith(("FUT",)):
            ois = [float(m[6]) for m in mins if len(m) > 6 and m[6] is not None]
            if ois:
                q["oi"] = round(ois[-1])
                q["oi_day_high"] = round(max(ois))
                q["oi_day_low"] = round(min(ois))
                q["previous_oi"] = round(ois[0])
        return q

    def quotes(self, keys):
        out = {}
        for k in keys:
            q = self._quote(k)
            if q:
                out[k] = q
        self.stats["calls"] += 1
        return out

    def ltp(self, keys):
        return {k: {"instrument_token": k, "last_price": v.get("last_price")}
                for k, v in self.quotes(keys).items()}


# --------------------------------------------------------------------- replay --
def session_minutes(data: dict) -> list[datetime]:
    """Every minute of the replayed session, from the first candle seen."""
    firsts, lasts = [], []
    for rows in (data.get("minutes") or {}).values():
        if not rows:
            continue
        d0 = parse_ts(rows[0][0])
        d1 = parse_ts(rows[-1][0])
        if d0:
            firsts.append(d0)
        if d1:
            lasts.append(d1)
    if not firsts:
        return []
    start = min(firsts).replace(second=0, microsecond=0)
    end = max(lasts).replace(second=0, microsecond=0)
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=1)
    return out


def build_engine(data: dict, universe: dict, cache_dir: str, log_dir: str,
                 settings: dict | None = None) -> Engine:
    """An engine warmed from the replay data, with no live threads."""
    clock = session_minutes(data)[0]
    src = ReplaySource(data, clock)
    eng = Engine(src, universe, settings=settings or {}, cache_dir=cache_dir, log_dir=log_dir,
                 demo=False)
    E.REPLAY_NOW = clock
    try:
        eng.warmup(source=src)
    finally:
        E.REPLAY_NOW = None
    return eng


def run_replay(data: dict, universe: dict, filters: dict, step: int = 1,
               cache_dir: str | None = None, log_dir: str | None = None,
               settings: dict | None = None, progress=None) -> dict:
    """Step through the session and record when each stock first qualified."""
    minutes = session_minutes(data)
    if not minutes:
        return {"ok": False, "error": "no candles collected for this session"}
    step = max(1, int(step))
    stamps = minutes[::step]
    if stamps[-1] != minutes[-1]:
        stamps.append(minutes[-1])

    cache_dir = cache_dir or os.path.join(CACHE, "replay_engine")
    log_dir = log_dir or os.path.join(LOGS, "replay")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    src = ReplaySource(data, stamps[0])
    eng = Engine(src, universe, settings=settings or {}, cache_dir=cache_dir, log_dir=log_dir,
                 demo=False)
    E.REPLAY_NOW = stamps[0]
    try:
        eng.warmup(source=src)
    except Exception as e:                                   # noqa: BLE001
        print(f"[warn] warmup: {e}")

    sess = data.get("session") or session_date()
    # one record per (stock, table): the live screen keeps a first-seen time per
    # side, so a stock that flips tables gets a first-seen time in each of them.
    rec: dict[tuple[str, str], dict] = {}
    # Every stamp's scores, so a filter can be changed AFTER the fact without
    # replaying again. Only rows that could ever pass the shared floor are kept.
    symbols: list[str] = []
    sym_ix: dict[str, int] = {}
    frames: list[list[list[float]]] = []
    try:
        for i, t in enumerate(stamps):
            E.REPLAY_NOW = t
            src.clock = t
            eng.poll(do_deep=True, source=src)
            snap = eng.snapshot()
            iso = t.isoformat(timespec="seconds")
            # ---- snapshot of every qualifying-scoring row at this minute ----
            frame = []
            for r in snap["universe"]:
                sl = float(r.get("score_long") or 0.0)
                ss = float(r.get("score_short") or 0.0)
                if max(sl, ss) < SIDE_FLOOR:
                    continue                       # can never enter either table
                s = r["symbol"]
                i2 = sym_ix.get(s)
                if i2 is None:
                    i2 = len(symbols)
                    sym_ix[s] = i2
                    symbols.append(s)
                frame.append([i2, round(sl, 1), round(ss, 1), round(float(r.get("rvol") or 0.0), 2)])
            frames.append(frame)

            for want in ("long", "short"):
                cap = filters.get(f"cap_{want}")
                for r in snap["universe"]:
                    if not passes(r, want, filters.get("rvol"), cap):
                        continue
                    key = (r["symbol"], want)
                    e = rec.get(key)
                    if e is None:
                        rec[key] = {
                            "symbol": r["symbol"], "side": want,
                            "first_seen": iso, "last_seen": iso, "ticks": 1,
                            "score": r.get("score_long" if want == "long" else "score_short"),
                            "other_score": r.get("score_short" if want == "long" else "score_long"),
                            "other_side_seen": None,
                            "rvol": r.get("rvol"), "ltp": r.get("ltp"),
                            "pct_chg": r.get("pct_chg"), "atr_pct": r.get("atr_pct"),
                            "oi_chg": r.get("oi_chg"), "sector": r.get("sector"),
                        }
                    else:
                        e["last_seen"] = iso
                        e["ticks"] += 1
            if progress and (i % 5 == 0 or i == len(stamps) - 1):
                progress(i + 1, len(stamps), t)
    finally:
        E.REPLAY_NOW = None

    rows = sorted(rec.values(), key=lambda x: (x["side"], x["first_seen"], x["symbol"]))
    for r in rows:
        r["minutes_on"] = round(r["ticks"] * step, 1)
        other = "short" if r["side"] == "long" else "long"
        r["other_side_seen"] = other if (r["symbol"], other) in rec else None
    out = {
        "ok": True, "session": sess,
        "filters": {"rvol": filters.get("rvol"), "cap_long": filters.get("cap_long"),
                    "cap_short": filters.get("cap_short")},
        "step_min": step, "stamps": len(stamps),
        "session_start": stamps[0].isoformat(timespec="seconds"),
        "session_end": stamps[-1].isoformat(timespec="seconds"),
        "count": len(rows), "long": sum(1 for r in rows if r["side"] == "long"),
        "short": sum(1 for r in rows if r["side"] == "short"),
        "rows": rows,
        # keys the engine asked for that the dump does not hold — should be empty
        "cache_misses": dict(src.misses),
        "symbols": symbols, "frames": frames,
        "stamps_iso": [t.isoformat(timespec="seconds") for t in stamps],
    }
    write_csv(out, log_dir)
    return out


def write_csv(res: dict, log_dir: str) -> str:
    f = res["filters"]
    sig = f"rv{f['rvol'] if f['rvol'] is not None else '-'}_cl{f['cap_long'] if f['cap_long'] is not None else '-'}" \
          f"_cs{f['cap_short'] if f['cap_short'] is not None else '-'}"
    path = os.path.join(log_dir, f"replay_{res['session']}_{sig}.csv")
    cols = ["symbol", "side", "first_seen", "last_seen", "minutes_on", "score", "other_score",
            "rvol", "ltp", "pct_chg", "atr_pct", "oi_chg", "sector", "other_side_seen"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in res["rows"]:
            w.writerow(r)
    res["csv"] = path
    return path


def export_frames(res: dict, path: str, source: str = "", recorded: dict | None = None) -> str:
    """Write the per-minute snapshot set the HTML report recomputes filters from."""
    obj = {
        "session": res["session"], "source": source,
        "recorded": recorded or {},
        "step_min": res["step_min"], "stamps": res["stamps"],
        "stamps_iso": res.get("stamps_iso") or [],
        "symbols": res.get("symbols") or [],
        "frames": res.get("frames") or [],
        "filters": res.get("filters") or {},
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    res["frames_json"] = path
    return path


def recorded_picks(session: str) -> dict[str, dict]:
    """The app's own pick log for that day, if one was written.

    Only used as a cross-check: these are wall-clock stamps of a live poll, not
    session times, so they prove *which* names the engine picked, not when.
    """
    out: dict[str, dict] = {}
    for mode in ("live", "demo"):
        fp = os.path.join(LOGS, mode, f"picks_{session}.csv")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sym = (row.get("symbol") or "").strip()
                    if sym and sym not in out:
                        out[sym] = {"symbol": sym, "side": row.get("side"),
                                    "score": row.get("score"), "logged_at": row.get("logged_at"),
                                    "mode": mode}
        except OSError:
            continue
    return out


# ------------------------------------------------------------------------ cli --
def _provenance(src) -> str:
    """Tag stored with the candles: demo generator, or the API host they came from."""
    if isinstance(src, DemoSource):
        return "demo"
    import upstox_client as UC
    return UC.API_ROOT


def source_label(data: dict) -> str:
    """Honest label for where the candles came from — used by the report banner.

    Read from the cache, not from the current environment: a set of candles
    collected against a stand-in API stays labelled as such even if you later
    replay it with a real Upstox token configured.
    """
    root = (data.get("api_root") or "").lower()
    if root == "demo":
        return "demo"
    if "api.upstox.com" in root:
        return "upstox"
    return "stand-in" if root else "unknown"


def _token() -> str:
    tok = (os.environ.get("UPSTOX_ACCESS_TOKEN") or "").strip()
    if tok:
        return tok
    fp = os.path.join(os.environ.get("RUNTIME_DIR") or os.path.join(BASE, "runtime"), "token.txt")
    if os.path.exists(fp):
        with open(fp) as f:
            return f.read().strip()
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay a past session through the screener engine.")
    ap.add_argument("--session", default=None, help="YYYY-MM-DD (default: the session in the data)")
    ap.add_argument("--rvol", type=float, default=None, help="minimum RVOL (e.g. 1.8)")
    ap.add_argument("--cap", type=float, default=None, help="cap on the OTHER side, both tables")
    ap.add_argument("--cap-long", type=float, default=None, help="cap on Short for the Long table")
    ap.add_argument("--cap-short", type=float, default=None, help="cap on Long for the Short table")
    ap.add_argument("--side", choices=["long", "short", "both"], default="both")
    ap.add_argument("--step", type=int, default=1, help="minutes per step (coarser = faster)")
    ap.add_argument("--demo", action="store_true", help="use synthetic data instead of Upstox")
    ap.add_argument("--collect-only", action="store_true", help="only fetch + cache candles")
    ap.add_argument("--force", action="store_true", help="ignore the cached candles")
    ap.add_argument("--export", default=None,
                    help="path for the per-minute snapshot dump (default: logs/replay/frames_<session>.json)")
    ap.add_argument("--no-export", action="store_true", help="skip the per-minute snapshot dump")
    args = ap.parse_args(argv)

    universe = build_universe(CACHE)
    if args.demo:
        base = DemoSource(universe)
    else:
        tok = _token()
        if not tok:
            print("No Upstox token found. Paste one in the app, set UPSTOX_ACCESS_TOKEN, "
                  "or run with --demo.")
            return 2
        base = LiveSource(tok)

    session = args.session or session_date()

    def prog(i, n, *rest):
        extra = ""
        if rest:
            extra = "  (at " + rest[0].strftime("%H:%M") + ")"
        print(f"\r  collecting {i}/{n}{extra}   ", end="", flush=True)

    data = collect(base, universe, session, force=args.force, progress=prog)
    print(f"\r  candles: {len(data['daily'])} daily / {len(data['minutes'])} minute series"
          f" for {data['session']}   ")
    if args.collect_only:
        return 0

    minutes = session_minutes(data)
    if not minutes:
        print("No minute candles for that session — nothing to replay.")
        return 1
    print(f"  session {data['session']}: {minutes[0]:%H:%M} → {minutes[-1]:%H:%M} IST, "
          f"stepping {args.step} min")

    filters = {"rvol": args.rvol,
               "cap_long": args.cap_long if args.cap_long is not None else args.cap,
               "cap_short": args.cap_short if args.cap_short is not None else args.cap}

    def rprog(i, n, t):
        if i % 25 and i != n:
            return
        print(f"\r  replaying {i}/{n}  at {t:%H:%M}   ", end="", flush=True)

    res = run_replay(data, universe, filters, step=args.step, progress=rprog)
    print("\r" + " " * 60 + "\r", end="")
    if not res.get("ok"):
        print("replay failed:", res.get("error"))
        return 1
    missed = {k: v for k, v in (res.get("cache_misses") or {}).items() if v}
    if missed:
        print(f"  [warn] the candles fetched do not cover every key the engine read: {missed}")
    logged = recorded_picks(res["session"])
    if not args.no_export:
        fp = args.export or os.path.join(LOGS, "replay", f"frames_{res['session']}.json")
        print(f"  snapshots: {export_frames(res, fp, source=source_label(data), recorded=logged)}\n"
              f"  report:    python3 replay_report.py --frames {fp}")

    want = {"long", "short"} if args.side == "both" else {args.side}
    rows = [r for r in res["rows"] if r["side"] in want]
    f = res["filters"]
    print(f"\nFilter: RVOL ≥ {f['rvol'] if f['rvol'] is not None else 'any'}"
          f" · Short ≤ {f['cap_long'] if f['cap_long'] is not None else 'any'} (long table)"
          f" · Long ≤ {f['cap_short'] if f['cap_short'] is not None else 'any'} (short table)")
    print(f"Session {res['session']} · {res['stamps']} steps · {len(rows)} stock-sides qualified\n")
    for side in ("long", "short"):
        got = [r for r in rows if r["side"] == side]
        if not got:
            continue
        print(f"{'▲ LONG' if side == 'long' else '▼ SHORT'} table — {len(got)} stocks")
        print(f"  {'symbol':<12}{'first seen':<11}{'mins on':>8}{'score':>7}{'other':>7}{'rvol':>6}")
        for r in got:
            fs = parse_ts(r["first_seen"])
            print(f"  {r['symbol']:<12}{fs:%H:%M:%S}   {r['minutes_on']:>7}"
                  f"{(r['score'] or 0):>7.1f}{(r['other_score'] or 0):>7.1f}{(r['rvol'] or 0):>6.1f}")
        print()
    print(f"CSV written to {res.get('csv')}")

    # ---- cross-check against the app's own pick log for that session ----
    if logged:
        print(f"\nCross-check — stocks the app logged as picks that day ({len(logged)}):")
        print(f"  {'symbol':<12}{'side':<7}{'logged':<10}{'src score':>10}   replay first seen under this filter")
        miss = []
        for sym, p in sorted(logged.items(), key=lambda kv: kv[1]["logged_at"] or ""):
            same = [x for x in res["rows"] if x["symbol"] == sym]
            r = next((x for x in same if x["side"] == p.get("side")), same[0] if same else None)
            when = (parse_ts(r["first_seen"]).strftime("%H:%M:%S") + f"  ({r['side']})") if r else "— not in either table"
            if r is None:
                miss.append(sym)
            print(f"  {sym:<12}{(p['side'] or ''):<7}{(p['logged_at'] or '')[11:19]:<10}"
                  f"{(p['score'] or ''):>10}   {when}")
        res["recorded_missed"] = miss
    return 0


if __name__ == "__main__":
    sys.exit(main())
