"""
Offline proof that a replay can be built from a *finished* session's candles.

This runs with no network and no token. It pins the three things the Replay
button depends on:

  1. `UpstoxClient.historical_candles()` asks for the dated route with the right
     path — the intraday route it replaced can only ever serve today.
  2. `replay.minutes_for()` uses that dated route for a past session and falls
     back to intraday for the current one.
  3. a full pipeline run (collect -> replay -> frames -> report) produces the
     artefacts the server serves, from a source that only knows how to answer
     dated requests.

The candles come from the app's own demo generator, re-stamped onto the session
being replayed, so timestamps and prices are synthetic. What is being tested is
the plumbing, not market data.

Run:  python3 tests/offline_replay.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import replay                                     # noqa: E402
import replay_report                              # noqa: E402
from sources import IST, DemoSource, last_session  # noqa: E402
from upstox_client import UpstoxClient             # noqa: E402

CHECKS = []


def check(name: str, ok: bool, extra: str = "") -> None:
    CHECKS.append((name, bool(ok), extra))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))


def make_universe(n: int = 14) -> dict:
    """A universe shaped exactly like build_universe() produces, without network."""
    syms = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "SBIN", "ITC", "LT", "AXISBANK",
            "MARUTI", "SUNPHARMA", "TATASTEEL", "HINDALCO", "DLF", "TRENT",
            "BEL", "HAL"][:n]
    sectors = ["Energy", "Financials", "IT", "IT", "Financials", "FMCG", "Infra",
               "Financials", "Auto", "Pharma", "Metals", "Metals", "Realty", "Retail",
               "Defence", "Defence"]
    stocks = []
    for i, sym in enumerate(syms):
        stocks.append({
            "symbol": sym, "name": sym, "sector": sectors[i], "sector_index": "NIFTY 500",
            "equity_key": f"NSE_EQ|DEMO{i:03d}", "fut_key": f"NSE_FO|{9000 + i}",
            "fut_symbol": f"{sym}26OCTFUT", "fut_expiry": "2026-10-29",
            "fut_next_symbol": f"{sym}26NOVFUT", "fut_next_expiry": "2026-11-26",
            "lot_size": (250, 500, 1000)[i % 3],
        })
    return {"stocks": stocks, "count": len(stocks),
            "index_keys": {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"},
            "built_at": datetime.now(IST).isoformat(timespec="seconds"), "unmatched": []}


class DatedDemo(DemoSource):
    """Demo candles re-stamped onto a chosen session.

    It stands in for Upstox's dated historical route: `historical()` answers with
    candles dated to the session asked for, and `intraday()` keeps serving the
    'current' session only, exactly like the real API.
    """

    def __init__(self, universe: dict, session: str):
        super().__init__(universe)
        self.session = session
        self.historical_calls = 0
        self.daily_calls = 0
        self.intraday_calls = 0
        sess = datetime.fromisoformat(session).date()
        self.shift = (sess - datetime.now(IST).date()).days

    def _redate(self, rows, days: int) -> list:
        out = []
        for r in rows:
            ts = datetime.fromisoformat(str(r[0]).replace("Z", "+00:00"))
            out.append([(ts + timedelta(days=days)).isoformat()] + list(r[1:]))
        return out

    def daily(self, key, days=60):
        self.daily_calls += 1
        return super().daily(key, days)                     # today-anchored, like the API

    def intraday(self, key, unit="minutes", interval=1):
        self.intraday_calls += 1
        return super().intraday(key, unit, interval)        # today only, like Upstox

    def historical(self, key, unit="minutes", interval=1, to_date="", from_date=""):
        self.historical_calls += 1
        if (to_date or "") [:10] != self.session:
            return []                                       # only this session exists here
        daily, mins = self._series(key)
        rows = self._redate(daily if unit == "days" else mins, self.shift)
        return [r for r in rows if str(r[0])[:10] == self.session]


def test_client_url() -> None:
    seen = []

    def fake_get(path, params=None, kind="quote", retries=3):
        seen.append((path, kind))
        return {"data": {"candles": []}}

    c = UpstoxClient("x" * 25)
    c._get = fake_get                                   # no network
    c.historical_candles("NSE_EQ|INE002A01018", "minutes", 1, "2026-10-16", "2026-10-16")
    path, kind = seen[-1]
    check("dated minute request uses /historical-candle/{key}/minutes/1/{to}/{from}",
          path == "/v3/historical-candle/NSE_EQ|INE002A01018/minutes/1/2026-10-16/2026-10-16"
          and kind == "history", path)
    c.historical_candles("NSE_EQ|X", "days", 1, "2026-10-16")
    check("a single date is reused for both ends of the range",
          seen[-1][0].endswith("/days/1/2026-10-16/2026-10-16"), seen[-1][0])
    c.daily_candles("NSE_EQ|X", 60)
    check("daily candles still go through the same dated route",
          "/days/1/" in seen[-1][0] and seen[-1][1] == "history", seen[-1][0])


def test_last_session() -> None:
    def ls(s): return last_session(datetime.fromisoformat(s).replace(tzinfo=IST))
    cases = [("2026-10-16T09:05", "2026-10-15", "before the close -> previous session"),
             ("2026-10-16T15:29", "2026-10-15", "still trading -> previous session"),
             ("2026-10-16T15:30", "2026-10-16", "closing bell -> today"),
             ("2026-10-16T22:10", "2026-10-16", "after the close -> today"),
             ("2026-10-17T11:00", "2026-10-16", "saturday -> friday"),
             ("2026-10-18T11:00", "2026-10-16", "sunday -> friday"),
             ("2026-10-19T09:20", "2026-10-16", "monday pre-open -> friday")]
    bad = [c for c in cases if ls(c[0]) != c[1]]
    check("last_session() picks the newest finished session", not bad,
          " ".join(f"{c[0]}->{ls(c[0])} want {c[1]}" for c in bad))


def previous_session(session_iso: str) -> str:
    """The trading day before a session — a date both routes cannot confuse."""
    d = datetime.fromisoformat(session_iso).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def test_session_choice(tmp: str, universe: dict) -> str:
    today = datetime.now(IST).date().isoformat()
    # `last_session()` is what the button builds by default, but right after a
    # close that IS today — so the dated route is exercised with a day before it.
    past = previous_session(today)
    src = DatedDemo(universe, past)
    key = universe["stocks"][0]["equity_key"]

    before = src.historical_calls
    rows_today = replay.minutes_for(src, key, today)
    check("current session is fetched with the intraday route",
          src.historical_calls == before and src.intraday_calls > 0,
          f"historical={src.historical_calls - before} intraday={src.intraday_calls}")

    before = src.historical_calls
    rows_past = replay.minutes_for(src, key, past)
    check("a finished session is fetched with the dated historical route",
          src.historical_calls > before and src.intraday_calls == 1,
          f"historical calls={src.historical_calls - before}, intraday calls={src.intraday_calls}")
    check("the button's default target is the newest finished session",
          last_session() in (today, past), last_session())
    check("the dated fetch returns that session's minutes",
          len(rows_past) > 300 and all(str(r[0])[:10] == past for r in rows_past),
          f"{len(rows_past)} rows" if rows_past else "no rows")
    check("today's minutes are the intraday session too",
          len(rows_today) > 0 and all(str(r[0])[:10] == today for r in rows_today),
          f"{len(rows_today)} rows")
    return past


def test_pipeline(tmp: str, universe: dict, session: str) -> None:
    src = DatedDemo(universe, session)
    cache_dir = os.path.join(tmp, "cache")
    log_dir = os.path.join(tmp, "logs")

    phases = []

    def progress(phase, done, total, note):
        phases.append((phase, done, total))

    res = replay.build_session(session, src, universe, step=5, progress=progress,
                               cache_dir=cache_dir, log_dir=log_dir)
    check("the build reports progress while it runs",
          any(p[0] == "fetching" for p in phases) and any(p[0] == "replaying" for p in phases),
          f"{len(phases)} ticks")
    check("the build used the dated route, not intraday",
          src.historical_calls >= len(universe["stocks"]), f"{src.historical_calls} calls")
    check("the replay found stocks that qualified",
          res["count"] > 0, f"{res['count']} stock-sides")
    # The fetch budget is what makes a build take minutes rather than an hour, so
    # pin it: one daily series per cash instrument (that is the only daily series
    # the engine ever reads — engine.py warmup), and one minute series per key.
    n = len(universe["stocks"])
    check("daily candles are fetched once per stock, never for futures or indices",
          src.daily_calls == n, f"{src.daily_calls} daily calls for {n} stocks")
    check("minute candles are fetched for cash, futures and index keys",
          src.historical_calls >= 2 * n, f"{src.historical_calls} minute calls")
    check("nothing the engine read was missing from the dump",
          res.get("cache_misses") == {"daily": 0, "minutes": 0},
          json.dumps(res.get("cache_misses")))

    frames = res["frames_json"]
    check("frames dump written", os.path.exists(frames) and os.path.getsize(frames) > 1000,
          f"{os.path.getsize(frames)} bytes" if os.path.exists(frames) else "missing")
    csvs = [f for f in os.listdir(log_dir) if f.startswith("replay_") and f.endswith(".csv")]
    check("per-filter CSV written", bool(csvs), ", ".join(csvs))

    out, d = replay_report.build(frames, os.path.join(log_dir, f"report_{session}.html"))
    html = open(out, encoding="utf-8").read()
    check("report written next to the frames",
          os.path.exists(out) and out.endswith(f"report_{session}.html")
          and os.path.dirname(out) == log_dir, out)
    check("report embeds the session and the timeline", session in html and "Session timeline" in html)
    check("report carries every stock's minutes",
          f'"stamps":{d["stamps"]}' in html.replace(" ", ""), f'{d["stamps"]} stamps')
    check("no half-written report left behind", not os.path.exists(out + ".part"))
    sym = sorted(d["symbols"])[0]
    check("report lists at least one symbol", f'"{sym}"' in html, sym)

    # The frames dump is what the report recomputes filters from, so it has to
    # answer exactly what the replay answered. Re-derive the first-seen times
    # straight from the dump and compare them with the CSV rows.
    d = json.load(open(frames, encoding="utf-8"))
    check("frames carry one snapshot per step",
          len(d["frames"]) == d["stamps"] == len(d["stamps_iso"]),
          f'{len(d["frames"])} frames / {d["stamps"]} stamps')
    check("frames name their source and session",
          d.get("source") and d.get("session") == session, f'{d.get("source")} / {d.get("session")}')
    every = [ix for fr in d["frames"] for ix, *_ in fr]
    check("every frame entry points at a listed symbol",
          bool(every) and max(every) < len(d["symbols"]), f'{len(d["symbols"])} symbols')

    def first_from_frames(want, rvol=None, cap=None):
        found = {}
        for i, fr in enumerate(d["frames"]):
            iso = d["stamps_iso"][i]
            for ix, sl, ss, rv in fr:
                rec = {"score_long": sl, "score_short": ss, "rvol": rv}
                if not replay.passes(rec, want, rvol, cap):
                    continue
                found.setdefault((d["symbols"][ix], want), iso)
        return found

    from_csv = {(r["symbol"], r["side"]): r["first_seen"] for r in res["rows"]}
    for want in ("long", "short"):
        got = first_from_frames(want)
        mine = {k: v for k, v in from_csv.items() if k[1] == want}
        check(f"recomputing the {want} table from the frames reproduces the CSV",
              got == mine, f"{len(got)} from frames vs {len(mine)} from the CSV")
    loose = first_from_frames("long")
    max_rv = max((rv for fr in d["frames"] for *_, rv in fr), default=0.0)
    check("a non-binding RVOL box recomputes exactly the same table",
          bool(loose) and first_from_frames("long", rvol=0.0) == loose, f"{len(loose)} rows")
    check("the recompute can also exclude — nothing clears an impossible box",
          first_from_frames("long", rvol=round(max_rv + 1, 2)) == {}
          and first_from_frames("long", cap=-1.0) == {}, f"highest RVOL seen {max_rv}")

    # a cached session must not hit the API again
    src2 = DatedDemo(universe, session)
    replay.build_session(session, src2, universe, step=5, cache_dir=cache_dir, log_dir=log_dir)
    check("a second build reuses the cached candles (no API calls)", src2.historical_calls == 0,
          f"{src2.historical_calls} calls")


def test_build_endpoint(tmp: str) -> None:
    """The POST route the button calls: refusals, the in-flight guard, no threads."""
    os.environ["CACHE_DIR"] = os.path.join(tmp, "srv-cache")
    os.environ["LOGS_DIR"] = os.path.join(tmp, "srv-logs")
    os.environ["RUNTIME_DIR"] = os.path.join(tmp, "srv-runtime")
    os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
    import server as S

    S.STATE["mode"] = "live"
    obj, status = S.start_replay_build({})
    check("live build without a token is refused with a readable reason",
          status == 400 and "token" in (obj.get("error") or "").lower(), obj.get("error"))

    obj, status = S.start_replay_build({"session": "16-10-2026"})
    check("a malformed session date is rejected", status == 400, obj.get("error"))

    # from here on the endpoint must not touch the network
    S.STATE["mode"] = "demo"
    started = []
    S._replay_worker = lambda *a: started.append(a)          # no thread, no API calls

    obj, status = S.start_replay_build({"session": "2026-10-15"})
    check("a valid build starts and returns immediately",
          status == 200 and obj.get("ok") and obj.get("session") == "2026-10-15", obj)

    obj2, status2 = S.start_replay_build({})
    check("a second build while one is running is refused",
          status2 == 409 and "already" in (obj2.get("error") or "").lower(), obj2.get("error"))

    info = S.replay_job_info()
    check("progress state is exposed for the UI",
          all(k in info for k in ("state", "done", "total", "note", "session", "error")), list(info))
    app = S.app_info()                                        # what /api/snapshot publishes
    check("app.replay carries the report, the job and the next session",
          all(k in app.get("replay", {}) for k in ("job", "last_session"))
          and "state" in app["replay"]["job"]
          and len(app["replay"]["last_session"]) == 10,
          json.dumps(app["replay"]))
    check("app.replay names the source while a job runs", app["replay"]["job"].get("source") in ("", "demo", "upstox"),
          app["replay"]["job"].get("source"))
    S._job_set(state="idle", session="", done=0, total=0, note="", error="")
    check("the guard clears once the job is idle", S.start_replay_build({})[1] == 200)
    S._job_set(state="idle")
    del started


def test_pick_log(tmp: str) -> None:
    """The report's cross-check reads the app's own pick log; prove it parses."""
    logs = os.path.join(tmp, "picks-logs")
    os.makedirs(os.path.join(logs, "demo"), exist_ok=True)
    session = "2026-10-09"
    with open(os.path.join(logs, "demo", f"picks_{session}.csv"), "w", newline="",
              encoding="utf-8") as f:
        f.write("logged_at,symbol,side,score,ltp,entry,sl,t1,t2,rr,status,mode,fut_symbol\n")
        f.write(f"{session}T10:12:00+05:30,BEL,long,70.4,412.5,410,400,430,2.1,WATCH,demo,BEL26OCTFUT\n")
        f.write(f"{session}T11:30:00+05:30,BEL,long,68.0,415.0,410,400,430,2.1,WATCH,demo,BEL26OCTFUT\n")

    real_logs = replay.LOGS
    replay.LOGS = logs
    try:
        picks = replay.recorded_picks(session)
    finally:
        replay.LOGS = real_logs
    check("the pick log is read once per symbol, first sighting wins",
          list(picks) == ["BEL"] and picks["BEL"]["logged_at"].startswith(f"{session}T10:12"),
          json.dumps(picks)[:90])
    check("a session with no pick log cross-checks as empty",
          replay.recorded_picks("2026-10-08") == {})

    check("cached candles from the demo generator are labelled demo",
          replay.source_label({"api_root": "demo"}) == "demo")
    check("cached candles from api.upstox.com are labelled upstox",
          replay.source_label({"api_root": "https://api.upstox.com/v2"}) == "upstox")
    check("cached candles from anywhere else are labelled stand-in",
          replay.source_label({"api_root": "http://127.0.0.1:8099"}) == "stand-in")
    check("candles with no recorded origin are labelled unknown",
          replay.source_label({}) == "unknown")


def main() -> int:
    print("offline replay pipeline")
    tmp = tempfile.mkdtemp(prefix="replay-test-")
    try:
        universe = make_universe()
        print(f"\nclient route")
        test_client_url()
        print(f"\nsession picking")
        test_last_session()
        print(f"\nminute source")
        session = test_session_choice(tmp, universe)
        print(f"\nfull build for {session}")
        test_pipeline(tmp, universe, session)
        print(f"\npick log + provenance")
        test_pick_log(tmp)
        print(f"\nbuild endpoint")
        test_build_endpoint(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
