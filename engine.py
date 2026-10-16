"""
The screener engine.

Pipeline every cycle:
  1. SNAPSHOT  - one batch quote call for all 210 F&O stocks (cash) + their futures
                 (for OI, and to show the actually-tradeable contract price).
  2. PRE-SCORE - every one of the 210 stocks gets a cheap long-score and short-score
                 from snapshot data alone (relative strength, range position, RVOL,
                 OI build-up, PDH/PDL structure, sector strength).
  3. DEEP SCAN - the top N candidates (default 40) get 1-minute candles pulled, so we
                 can compute VWAP, opening-range breakouts, 5-min EMA slope, RSI, time-
                 of-day volume bursts and intraday structure. Depth of scan is capped
                 to respect Upstox's historical-data rate limit.
  4. HARD GATES - liquidity pace, spread, circuit proximity, price/ATR sanity, ban list.
  5. FINAL RANK - weighted composite score -> top 5 longs + top 5 shorts, max 2 names
                 per sector, with entry / stop / targets / R:R built from real levels.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from sources import market_status, session_date
from upstox_client import IST, UpstoxError, parse_ts

# ------------------------------------------------------------------ clock ----
# The engine normally uses wall-clock time. A REPLAY over a past session needs it
# to believe "now" is the minute being replayed, otherwise the RVOL pace, the
# turnover projection and the session phase would all be computed for the wrong
# moment. replay.py sets REPLAY_NOW for the duration of a run.
REPLAY_NOW = None


def now_ist() -> datetime:
    """Current IST time — or the replay clock while a replay is running."""
    return REPLAY_NOW or datetime.now(IST)


# ----------------------------------------------------------------- tunables ---
DEFAULTS = {
    "refresh_sec": 20,            # snapshot poll interval
    "deep_depth": 40,             # how many candidates get 1-min candle analysis
    "min_turnover_cr": 35.0,      # pace-adjusted full-day turnover gate (Rs crore)
    "max_spread_pct": 0.18,       # bid/ask spread gate
    "min_atr_pct": 0.9,           # needs enough daily range to be worth trading
    "min_price": 40.0,
    "sector_cap": 2,              # max names from one sector in final list
    "picks_per_side": 5,
    "score_floor": 52.0,          # minimum composite score to be published
    "ban_list": [],               # NSE F&O ban-list symbols (manual paste ok)
    "show_longs": True,
    "show_shorts": True,
}

W = {"rs": 0.22, "momentum": 0.20, "structure": 0.18, "volume": 0.13,
     "oi": 0.12, "setup": 0.10, "align": 0.05}

# intraday session phases, measured in minutes from the 09:15 open.
# Each phase tells the trader whether new entries belong in this window at all.
SESSION_PHASES = [
    (0, 15, "pre-open", "Avoid", "Opening range is still forming — the engine is watching, you shouldn't be trading yet."),
    (15, 60, "open-range", "Prime", "Best window for breakout entries: the opening range is set and volume is real."),
    (60, 135, "trend", "Good", "Trend continuation window — 'BUY NOW' / pullback entries work well here."),
    (135, 255, "midday", "Choppy", "Lunchtime chop — demand A+ setups only, false breakouts are common."),
    (255, 330, "afternoon", "Prime", "Afternoon trend often decides the day — continuation and fresh breakouts both work."),
    (330, 360, "late", "Manage", "Stop opening new trades. Trail stops and bank what you have."),
    (360, 375, "close", "Exit", "Square off intraday positions — no new entries."),
]

# how much of a normal day's volume has typically traded by minute-of-session
VOL_CURVE = [(0, 0.004), (5, 0.045), (15, 0.10), (30, 0.175), (60, 0.28),
             (90, 0.36), (120, 0.43), (150, 0.50), (180, 0.56), (210, 0.62),
             (240, 0.68), (270, 0.74), (300, 0.80), (330, 0.88), (360, 0.96), (375, 1.0)]


def sig(x: float, k: float = 1.0) -> float:
    v = max(-30.0, min(30.0, x * k))
    return 1.0 / (1.0 + math.exp(-v))


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def vol_pace_fraction(minutes_elapsed: float) -> float:
    m = max(0.0, min(375.0, minutes_elapsed))
    for i in range(1, len(VOL_CURVE)):
        if m <= VOL_CURVE[i][0]:
            (m0, f0), (m1, f1) = VOL_CURVE[i - 1], VOL_CURVE[i]
            if m1 == m0:
                return f1
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


def ema(vals, period):
    if not vals:
        return None
    k = 2.0 / (period + 1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals, period=14):
    if len(vals) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al <= 1e-12:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def resample_5m(candles):
    """1-min candles -> 5-min OHLCV buckets (aligned to 9:15)."""
    out = []
    cur = None
    for c in candles:
        dt = parse_ts(c[0])
        if dt is None:
            continue
        bucket = (dt.minute // 5) * 5
        key = dt.replace(minute=bucket, second=0, microsecond=0)
        if cur is None or cur[0] != key:
            if cur:
                out.append(cur)
            cur = [key, c[1], c[2], c[3], c[4], c[5]]
        else:
            cur[2] = max(cur[2], c[2])
            cur[3] = min(cur[3], c[3])
            cur[4] = c[4]
            cur[5] += c[5]
    if cur:
        out.append(cur)
    return out


class Engine:
    def __init__(self, source, universe: dict, settings: dict | None = None,
                 cache_dir: str = "cache", log_dir: str = "logs", demo: bool = False):
        self.src = source
        self.uni = universe
        self.demo = demo
        self.settings = dict(DEFAULTS)
        if settings:
            self.settings.update(settings)
        self.cache_dir = cache_dir
        self.log_dir = log_dir
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)

        self.stocks: dict[str, dict] = {}
        for s in universe["stocks"]:
            self.stocks[s["symbol"]] = {**s, "warmed": False, "deep": False,
                                        "pre_long": 0.0, "pre_short": 0.0,
                                        "sc_long": 0.0, "sc_short": 0.0,
                                        "gate": "pending", "why_long": [], "why_short": [],
                                        "risk_long": [], "risk_short": [],
                                        "trade_long": None, "trade_short": None,
                                        "spark": [], "first_seen_long": None,
                                        "first_seen_short": None, "entry_seen_long": None,
                                        "entry_seen_short": None}
        self.index_keys = universe.get("index_keys", {})
        self.ctx = {"nifty": {}, "banknifty": {}, "vix": {}, "sectors": [], "breadth_up_pct": None,
                    "nifty_above_vwap": None, "adv": 0, "dec": 0, "regime": "Mixed", "bias": "neutral",
                    "regime_note": ""}
        self.funnel = {}
        self.state_meta = {
            "cycle": 0, "last_poll": None, "last_deep": None, "last_update_iso": None,
            "warm_done": False, "warm_total": len(self.stocks), "warm_done_count": 0,
            "deep_scanned": 0, "errors": [], "started": now_ist().isoformat(timespec="seconds"),
            "poll_seconds": None,
        }
        self.picks = {"long": [], "short": []}
        self.pick_log: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._day = session_date()
        self._warm_cache_file = os.path.join(cache_dir, f"warm_{self._day}.json")
        self._oi_file = os.path.join(cache_dir, f"oi_open_{self._day}.json")
        self.oi_open: dict[str, float] = {}
        self._load_oi_baseline()

    # ------------------------------------------------------------- persistence
    def _load_oi_baseline(self):
        try:
            if os.path.exists(self._oi_file):
                with open(self._oi_file) as f:
                    self.oi_open = json.load(f)
        except Exception:
            self.oi_open = {}

    def _save_oi_baseline(self):
        try:
            with open(self._oi_file, "w") as f:
                json.dump(self.oi_open, f)
        except Exception:
            pass

    def save_warm_cache(self):
        try:
            rows = {sym: {k: st.get(k) for k in ("prev_close", "pdh", "pdl", "atr20", "atr_pct",
                                                 "avg_vol20", "dma20", "hi20", "lo20", "trend20")}
                    for sym, st in self.stocks.items() if st.get("warmed")}
            with open(self._warm_cache_file, "w") as f:
                json.dump({"date": self._day, "rows": rows}, f)
        except Exception:
            pass

    def load_warm_cache(self) -> bool:
        try:
            if not os.path.exists(self._warm_cache_file):
                return False
            with open(self._warm_cache_file) as f:
                blob = json.load(f)
            rows = blob.get("rows") or {}
            n = 0
            for sym, row in rows.items():
                st = self.stocks.get(sym)
                if st:
                    st.update({k: v for k, v in row.items() if v is not None})
                    st["warmed"] = True
                    n += 1
            self.state_meta["warm_done_count"] = n
            self.state_meta["warm_done"] = n >= len(self.stocks) * 0.9
            return n > 0
        except Exception:
            return False

    # ---------------------------------------------------------------- warm-up
    def warmup(self, source=None, progress=None):
        """Daily candles for every F&O stock -> prev close, PDH/PDL, ATR, avg volume,
        20-day trend. Runs in the background and adapts its order: while quotes are
        already flowing (market open) it warms the most actively traded names first,
        so tradeable picks form within seconds instead of minutes."""
        src = source or self.src
        total = len(self.stocks)
        done = 0
        while True:
            if self._day != session_date():
                self._day = session_date()
                self._warm_cache_file = os.path.join(self.cache_dir, f"warm_{self._day}.json")
                self._oi_file = os.path.join(self.cache_dir, f"oi_open_{self._day}.json")
            pending = [s for s in self.stocks.values() if not s.get("warmed")]
            if not pending:
                break
            # most active first (live turnover), then the rest by contract size
            pending.sort(key=lambda s: (-(s.get("turnover_cr") or 0.0), -(s.get("lot_size") or 0)))
            st = pending[0]
            try:
                self._apply_daily(st, src.daily(st["equity_key"], 60))
            except UpstoxError as e:
                self._note_error(f"warmup {st['symbol']}: {e.friendly()}")
                if e.status in (401, 403):
                    return                      # bad token: stop burning requests
            except Exception as e:              # noqa: BLE001
                self._note_error(f"warmup {st['symbol']}: {e}")
            if not st.get("warmed"):
                st["warm_failed"] = True
            done += 1
            self.state_meta["warm_done_count"] = sum(1 for x in self.stocks.values() if x.get("warmed"))
            if progress and (done % 10 == 0 or done >= total):
                progress(done, total)
            # everything retried or genuinely unavailable? stop the loop
            if done > total * 2:
                break
        self.state_meta["warm_done"] = True
        self.save_warm_cache()

    def _apply_daily(self, st: dict, candles: list):
        if not candles:
            return
        rows = [c for c in candles if c[4] > 0]
        if len(rows) < 5:
            return
        last_dt = parse_ts(rows[-1][0])
        now = now_ist()
        today = now.date()
        in_session = (now.hour * 60 + now.minute) < (15 * 60 + 40)
        hist = rows
        # while the session is live, the last daily candle IS today -> previous session is rows[-2].
        # after the close it is a finished session, so it becomes the reference for the next day.
        if last_dt and last_dt.date() == today and in_session and len(rows) >= 2:
            hist = rows[:-1]
        prev = hist[-1]
        prev2 = hist[-2] if len(hist) >= 2 else prev
        st["prev_close"] = float(prev[4])
        st["pdh"] = float(prev[2])
        st["pdl"] = float(prev[3])
        st["prev_high2"] = float(prev2[2])
        st["prev_low2"] = float(prev2[3])
        win = hist[-20:]
        trs = []
        for i in range(1, len(win)):
            h, l, pc = win[i][2], win[i][3], win[i - 1][4]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / len(trs) if trs else (win[-1][2] - win[-1][3])
        st["atr20"] = atr
        st["atr_pct"] = atr / max(1e-9, st["prev_close"]) * 100.0
        vols = [c[5] for c in hist[-20:] if c[5]]
        st["avg_vol20"] = (sum(vols) / len(vols)) if vols else 0.0
        st["dma20"] = sum(c[4] for c in win) / len(win)
        st["hi20"] = max(c[2] for c in win)
        st["lo20"] = min(c[3] for c in win)
        st["trend20"] = (st["prev_close"] / st["dma20"] - 1.0) * 100.0 if st["dma20"] else 0.0
        st["warmed"] = True

    # ------------------------------------------------------------------ polls
    def poll(self, do_deep: bool = False, source=None):
        src = source or self.src
        cash_keys = [s["equity_key"] for s in self.stocks.values()]
        fut_keys = [s["fut_key"] for s in self.stocks.values()]
        t0 = time.time()
        cash_q = src.quotes(cash_keys)
        fut_q = src.quotes(fut_keys)
        self.state_meta["poll_seconds"] = round(time.time() - t0, 2)
        for st in self.stocks.values():
            q = cash_q.get(st["equity_key"])
            if q:
                self._apply_quote(st, q)
            fq = fut_q.get(st["fut_key"])
            if fq:
                st["fut_ltp"] = _f(fq.get("last_price"))
                st["fut_volume"] = _f(fq.get("volume"))
                st["fut_oi"] = _f(fq.get("oi"))
                st["fut_prev_oi"] = _f(fq.get("previous_oi")) or st.get("fut_prev_oi")
                st["fut_oi_day_high"] = _f(fq.get("oi_day_high"))
                st["fut_basis"] = (st.get("fut_ltp") / st["ltp"] - 1.0) * 100.0 if st.get("ltp") else None
                if st["fut_oi"] and not st.get("fut_prev_oi"):
                    self.oi_open.setdefault(st["symbol"], st["fut_oi"])
                    st["oi_open"] = self.oi_open[st["symbol"]]
        self._save_oi_baseline()
        self._index_context(src)
        self._compute_context()
        self._pre_score_all()
        if do_deep:
            self.deep_scan(src)
        self._final_score_all()
        self._select_picks()
        self.state_meta["cycle"] += 1
        self.state_meta["last_poll"] = now_ist().isoformat(timespec="seconds")
        self.state_meta["last_update_iso"] = self.state_meta["last_poll"]
        if do_deep:
            self.state_meta["last_deep"] = self.state_meta["last_poll"]

    def _apply_quote(self, st: dict, q: dict):
        ltp = _f(q.get("last_price"))
        ohlc = q.get("ohlc") or {}
        if not ltp or ltp <= 0:
            return
        st["ltp"] = ltp
        st["day_open"] = _f(ohlc.get("open")) or st.get("day_open")
        st["day_high"] = _f(ohlc.get("high")) or st.get("day_high")
        st["day_low"] = _f(ohlc.get("low")) or st.get("day_low")
        st["volume"] = _f(q.get("volume")) if q.get("volume") is not None else st.get("volume")
        # v3: ohlc.close is THIS session's close; prev_close_price is the previous session
        st["q_prev_close"] = _f(q.get("prev_close_price")) or _f(ohlc.get("close"))
        st["net_change"] = _f(q.get("net_change"))
        st["upper_c"] = _f(q.get("upper_circuit_limit"))
        st["lower_c"] = _f(q.get("lower_circuit_limit"))
        st["reference_price"] = _f(q.get("reference_price"))
        st["avg_price"] = _f(q.get("average_price"))          # exchange session average price
        st["tbq"] = _f(q.get("total_buy_quantity"))
        st["tsq"] = _f(q.get("total_sell_quantity"))
        st["year_high"] = _f(q.get("year_high")) or st.get("year_high")
        st["year_low"] = _f(q.get("year_low")) or st.get("year_low")
        if not st.get("vwap") and st["avg_price"]:
            st["vwap"] = st["avg_price"]                       # provisional until candles arrive
        depth = q.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        st["bid"] = _f(bids[0].get("price")) if bids else None
        st["ask"] = _f(asks[0].get("price")) if asks else None
        st["bid_qty"] = _f(bids[0].get("quantity")) if bids else None
        st["ask_qty"] = _f(asks[0].get("quantity")) if asks else None
        st["ts"] = _ts(q.get("timestamp"))
        st["last_trade_time"] = _ts(q.get("last_trade_time"))
        prev = st.get("q_prev_close") or st.get("prev_close")
        if prev:
            st["pct_chg"] = (ltp / prev - 1.0) * 100.0
            st["day_prev_close"] = prev
        rng = (st.get("day_high") or ltp) - (st.get("day_low") or ltp)
        st["range_pos"] = ((ltp - (st.get("day_low") or ltp)) / rng * 100.0) if rng > 0 else 50.0
        st["day_range_pct"] = (rng / prev * 100.0) if prev else 0.0
        if st.get("bid") and st.get("ask") and st["ask"] >= st["bid"] > 0:
            mid = (st["bid"] + st["ask"]) / 2.0
            st["spread_pct"] = (st["ask"] - st["bid"]) / mid * 100.0
        else:
            st["spread_pct"] = None
        st["live"] = True
        st["turnover_cr"] = (ltp * (st.get("volume") or 0.0)) / 1e7

    def _index_context(self, src):
        keys = list(self.index_keys.values())
        try:
            q = src.quotes(keys) or src.ltp(keys)
        except Exception as e:  # noqa: BLE001
            self._note_error(f"index quotes: {e}")
            q = {}
        by_name = {v: k for k, v in self.index_keys.items()}
        idx = {}
        for key, name in by_name.items():
            item = q.get(key)
            if not item:
                continue
            ltp = _f(item.get("last_price"))
            ohlc = item.get("ohlc") or {}
            pc = _f(ohlc.get("close")) or _f(item.get("prev_close"))
            chg = _f(item.get("net_change"))
            if chg is None and pc and ltp:
                chg = ltp - pc
            idx[name] = {
                "ltp": ltp,
                "prev_close": pc,
                "pct": (chg / pc * 100.0) if (chg is not None and pc) else None,
                "day_high": _f(ohlc.get("high")),
                "day_low": _f(ohlc.get("low")),
            }
        self._idx = idx
        # NIFTY intraday candles for a proper VWAP read on the index
        try:
            nk = self.index_keys.get("NIFTY")
            cans = src.intraday(nk, "minutes", 1)
            self._nifty_candles = resample_5m(cans)
            if cans:
                tv = sum(((c[2] + c[3] + c[4]) / 3.0) * (c[5] or 1.0) for c in cans)
                vv = sum((c[5] or 1.0) for c in cans)
                self._nifty_vwap = tv / vv if vv else None
            else:
                self._nifty_vwap = None
        except Exception:
            self._nifty_candles = []
            self._nifty_vwap = None

    def _compute_context(self):
        n = self._idx.get("NIFTY", {})
        b = self._idx.get("BANKNIFTY", {})
        vix = self._idx.get("INDIAVIX", {})
        up = sum(1 for s in self.stocks.values() if (s.get("pct_chg") or 0) > 0)
        dec = sum(1 for s in self.stocks.values() if (s.get("pct_chg") or 0) < 0)
        tot = up + dec
        breadth = (up / tot * 100.0) if tot else None
        # sector strength straight from our own 210 names (no extra API calls)
        agg: dict[str, list] = {}
        for s in self.stocks.values():
            if s.get("pct_chg") is None:
                continue
            agg.setdefault(s["sector"], []).append(s["pct_chg"])
        sectors = []
        for name, vals in agg.items():
            if not vals:
                continue
            vals = sorted(vals)
            med = vals[len(vals) // 2]
            rep = next((s["sector_index"] for s in self.stocks.values() if s["sector"] == name), None)
            idx_pct = (self._idx.get(rep, {}) or {}).get("pct") if rep else None
            sectors.append({"sector": name, "pct": round(med, 2), "index": rep,
                            "index_pct": round(idx_pct, 2) if idx_pct is not None else None,
                            "n": len(vals)})
        sectors.sort(key=lambda x: -x["pct"])
        for s in sectors:
            s["rank"] = sectors.index(s) + 1
        nifty_pct = n.get("pct")
        above_vwap = None
        if n.get("ltp") and self._nifty_vwap:
            above_vwap = n["ltp"] >= self._nifty_vwap
        regime, bias, note = "Mixed", "neutral", "No clear edge — trade only A+ setups, tighter stops."
        vix_pct = vix.get("pct") or 0.0
        if nifty_pct is not None:
            # above VWAP confirms strength, below VWAP confirms weakness
            up_confirms = above_vwap is not False
            down_confirms = above_vwap is not True
            br_v = breadth if breadth is not None else 50.0
            if nifty_pct >= 0.30 and br_v >= 55 and up_confirms:
                regime, bias = "Risk-On", "long"
                note = "Index trending up with broad participation — momentum longs favoured."
            elif nifty_pct <= -0.30 and br_v <= 45 and down_confirms:
                regime, bias = "Risk-Off", "short"
                note = "Index under pressure, breadth weak — short setups favoured."
            elif abs(nifty_pct) < 0.30 and 45 < br_v < 55:
                regime, bias = "Chop / Range", "neutral"
                note = "Index stuck in a range — expect false breakouts; demand volume + level confluence."
            elif abs(nifty_pct) >= 0.30:
                regime = "Trending"
                bias = "long" if nifty_pct > 0 else "short"
                note = ("Index up but breadth is thin — stick to the strongest relative-strength names."
                        if nifty_pct > 0 else
                        "Index selling off without washed-out breadth — be picky on shorts, expect bounces.")
        if vix_pct >= 8:
            note += f"  Volatility spiking (India VIX {vix_pct:+.0f}%) — widen stops, cut size."
        elif vix_pct <= -8:
            note += f"  Volatility cooling (India VIX {vix_pct:+.0f}%) — trends travel further."
        self.ctx = {
            "nifty": n, "banknifty": b, "vix": vix, "sectors": sectors,
            "breadth_up_pct": round(breadth, 1) if breadth is not None else None,
            "nifty_above_vwap": above_vwap, "adv": up, "dec": dec,
            "regime": regime, "bias": bias, "regime_note": note,
        }

    def _note_error(self, msg: str):
        with self._lock:
            self.state_meta["errors"] = (self.state_meta["errors"] + [msg])[-6:]

    # ------------------------------------------------------- 1-minute deep scan
    def _deep_deadline_sec(self) -> int:
        depth = max(1, int(self.settings["deep_depth"]))
        # keep candle traffic under ~85 calls/min
        return max(int(self.settings["refresh_sec"] * 2), int(depth * 60 / 85) + 1)

    def deep_scan(self, src=None):
        src = src or self.src
        cand = sorted(self.stocks.values(), key=lambda s: -max(s["pre_long"], s["pre_short"]))
        depth = max(1, int(self.settings["deep_depth"]))
        targets = [s for s in cand[:depth] if s.get("ltp")]
        done = 0
        for st in targets:
            try:
                cans = src.intraday(st["equity_key"], "minutes", 1)
                self._apply_intraday(st, cans)
                done += 1
            except UpstoxError as e:
                self._note_error(f"deep {st['symbol']}: {e.friendly()}")
            except Exception as e:  # noqa: BLE001
                self._note_error(f"deep {st['symbol']}: {e}")
        self.state_meta["deep_scanned"] = done

    def _apply_intraday(self, st: dict, candles: list):
        rows = [c for c in candles if c[4] > 0]
        if len(rows) < 3:
            return
        bars5 = resample_5m(rows)
        closes5 = [b[4] for b in bars5]
        st["spark"] = [round(x, 2) for x in closes5[-34:]]
        # VWAP from 1-min bars
        tv = vv = 0.0
        for c in rows:
            tp = (c[2] + c[3] + c[4]) / 3.0
            v = c[5] or 1.0
            tv += tp * v
            vv += v
        st["vwap"] = (tv / vv) if vv else None
        # opening range (first 15 minutes)
        orh = orl = None
        for c in rows:
            dt = parse_ts(c[0])
            if dt is None:
                continue
            mins = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)
            if 0 <= mins < 15:
                orh = c[2] if orh is None else max(orh, c[2])
                orl = c[3] if orl is None else min(orl, c[3])
        st["or_high"], st["or_low"] = orh, orl
        st["ema20_5m"] = ema(closes5[-40:], 20)
        if len(closes5) >= 6 and st.get("ema20_5m"):
            prev_e = ema(closes5[-40:-3], 20)
            st["ema_slope"] = ((st["ema20_5m"] / prev_e - 1.0) * 100.0) if prev_e else 0.0
        else:
            st["ema_slope"] = 0.0
        st["rsi14"] = rsi(closes5, 14)
        ltp = st.get("ltp") or (closes5[-1] if closes5 else None)
        if ltp and len(closes5) >= 4:
            st["ret15"] = (ltp / closes5[-4] - 1.0) * 100.0
            st["ret5"] = (ltp / closes5[-2] - 1.0) * 100.0
        # volume burst: last 10 min vs average 5-min bar
        if len(bars5) >= 8:
            recent = sum(b[5] for b in bars5[-2:])
            base = sum(b[5] for b in bars5[-14:-2]) / max(1, len(bars5[-14:-2]))
            st["vol_burst"] = (recent / 2.0) / base if base > 0 else 1.0
        else:
            st["vol_burst"] = 1.0
        # intraday structure: higher lows over last 6 5-min bars
        lows = [b[3] for b in bars5[-6:]]
        st["higher_lows"] = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
        st["lower_highs"] = sum(1 for i in range(1, len(lows)) if bars5[-6:][i][2] <= bars5[-6:][i - 1][2])
        # how long ago was the day's extreme printed
        hi = max(rows, key=lambda c: c[2])
        lo = min(rows, key=lambda c: c[3])
        last_dt = parse_ts(rows[-1][0])
        hi_dt, lo_dt = parse_ts(hi[0]), parse_ts(lo[0])
        if last_dt and hi_dt:
            st["hi_age_min"] = (last_dt - hi_dt).total_seconds() / 60.0
        if last_dt and lo_dt:
            st["lo_age_min"] = (last_dt - lo_dt).total_seconds() / 60.0
        st["deep"] = True

    # ------------------------------------------------------------ stage 1 score
    def _pre_score_all(self):
        nifty_pct = (self.ctx.get("nifty") or {}).get("pct") or 0.0
        sec = {s["sector"]: s["pct"] for s in self.ctx.get("sectors") or []}
        pace = self._pace_fraction()
        for st in self.stocks.values():
            if not st.get("ltp"):
                st["pre_long"] = st["pre_short"] = 0.0
                continue
            rs = (st.get("pct_chg") or 0.0) - nifty_pct
            rpos = (st.get("range_pos", 50.0) - 50.0) / 50.0      # -1..1
            rvol = self._rvol(st, pace)
            oi_sc = self._oi_signal(st)
            sec_pct = sec.get(st["sector"], 0.0)
            struct_l = self._struct_score(st, "long")
            struct_s = self._struct_score(st, "short")
            # ---- long
            s_rs = sig(rs, 0.45)
            s_mom = sig(rpos * 1.1 + (st.get("ret15") or 0.0) * 0.25, 1.0)
            s_vol = sig((rvol - 1.0) * 1.05)
            s_oi = clamp(0.5 + oi_sc * 0.5)
            s_align = sig((sec_pct * 0.6 + nifty_pct * 0.4) * 1.2)
            st["pre_long"] = 100.0 * (0.24 * s_rs + 0.22 * s_mom + 0.22 * struct_l +
                                      0.14 * s_vol + 0.10 * s_oi + 0.08 * s_align)
            # ---- short (mirror)
            s_rs_s = sig(-rs, 0.45)
            s_mom_s = sig(-rpos * 1.1 - (st.get("ret15") or 0.0) * 0.25, 1.0)
            s_oi_s = clamp(0.5 - oi_sc * 0.5)
            s_align_s = sig(-(sec_pct * 0.6 + nifty_pct * 0.4) * 1.2)
            st["pre_short"] = 100.0 * (0.24 * s_rs_s + 0.22 * s_mom_s + 0.22 * struct_s +
                                       0.14 * s_vol + 0.10 * s_oi_s + 0.08 * s_align_s)

    def _rvol(self, st: dict, pace: float) -> float:
        vol = st.get("volume") or 0.0
        avg = st.get("avg_vol20") or 0.0
        if not avg:
            return 1.0
        expected = avg * max(0.02, pace)
        return vol / expected if expected > 0 else 1.0

    def _oi_signal(self, st: dict):
        """+1 = fresh long build-up, -1 = fresh short build-up, 0 = neutral/unwinding."""
        oi = st.get("fut_oi")
        pct = st.get("pct_chg") or 0.0
        if not oi:
            return 0.0
        base = st.get("fut_prev_oi") or st.get("oi_open")
        if not base:
            return 0.0
        oi_chg = (oi / base - 1.0) * 100.0
        st["oi_chg_pct"] = oi_chg
        if oi_chg > 0.4:
            return 0.85 if pct > 0 else (-0.85 if pct < 0 else 0.1)
        if oi_chg < -0.4:
            return -0.35 if pct > 0 else 0.35      # short covering / long unwinding
        return 0.0

    def _struct_score(self, st: dict, side: str) -> float:
        s = 0.5
        ltp = st.get("ltp") or 0.0
        pdh, pdl = st.get("pdh"), st.get("pdl")
        dma = st.get("dma20")
        dh, dl = st.get("day_high"), st.get("day_low")
        orh, orl = st.get("or_high"), st.get("or_low")
        if side == "long":
            if pdh and ltp > pdh:
                s += 0.17
            if orh and ltp > orh:
                s += 0.10
            if dma and ltp > dma:
                s += 0.08
            if st.get("hi20") and ltp >= st["hi20"] * 0.995:
                s += 0.08
            if dh and dl and dh > dl and (dh - ltp) / max(1e-9, dh - dl) < 0.25:
                s += 0.10
            if (st.get("hi_age_min") or 999) <= 20:
                s += 0.06
            if (st.get("higher_lows") or 0) >= 4:
                s += 0.06
            if pdl and ltp < pdl:
                s -= 0.25
            if st.get("vwap") and ltp < st["vwap"]:
                s -= 0.14
        else:
            if pdl and ltp < pdl:
                s += 0.17
            if orl and ltp < orl:
                s += 0.10
            if dma and ltp < dma:
                s += 0.08
            if st.get("lo20") and ltp <= st["lo20"] * 1.005:
                s += 0.08
            if dh and dl and dh > dl and (ltp - dl) / max(1e-9, dh - dl) < 0.25:
                s += 0.10
            if (st.get("lo_age_min") or 999) <= 20:
                s += 0.06
            if (st.get("lower_highs") or 0) >= 4:
                s += 0.06
            if pdh and ltp > pdh:
                s -= 0.25
            if st.get("vwap") and ltp > st["vwap"]:
                s -= 0.14
        return clamp(s)

    # -------------------------------------------------------- hard gates & score
    def session_phase(self, now=None) -> dict:
        """Which part of the trading day are we in, and can you open a trade?"""
        now = now or now_ist()
        mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
        for i, (a, b, key, verdict, note) in enumerate(SESSION_PHASES):
            if a <= mins < b:
                nxt = SESSION_PHASES[i + 1] if i + 1 < len(SESSION_PHASES) else None
                return {"key": key, "label": verdict, "note": note, "minutes_in": mins,
                        "minutes_to_next": (b - mins) if nxt else 0,
                        "next_key": nxt[2] if nxt else None,
                        "tradeable": key in ("open-range", "trend", "midday", "afternoon") and mins >= 15}
        return {"key": "closed", "label": "Closed", "minutes_in": mins, "minutes_to_next": 0,
                "next_key": "pre-open", "tradeable": False,
                "note": "Market is closed — these are the freshest levels for the next session."}

    def _readiness(self, st: dict, side: str, score: float, trade: dict, rec: dict,
                   phase: dict) -> dict:
        """Turn 'here is a pick' into 'here is whether you should act, and why'.

        This is the anti-chase, anti-noise layer: a high score alone is never enough.
        """
        checks = []
        ltp = st.get("ltp") or 0.0
        vwap, atr = st.get("vwap"), st.get("atr20") or ltp * 0.02
        bias = self.ctx.get("bias", "neutral")
        rvol = self._rvol(st, self._pace_fraction())
        ext = ((ltp - vwap) / atr) if (vwap and atr) else 0.0
        if side == "short":
            ext = -ext
        risk = abs(trade["entry"] - trade["sl"]) or 1e-9
        past_r = ((ltp - trade["entry"]) if side == "long" else (trade["entry"] - ltp)) / risk

        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add("Score", score >= max(float(self.settings["score_floor"]), 62),
            f"{score:.0f} (needs {max(int(self.settings['score_floor']), 62)}+)")
        add("Volume pace", rvol >= 1.3, f"RVOL {rvol:.1f}x its normal pace")
        add("Right side of VWAP", (ltp > vwap if side == "long" else ltp < vwap) if vwap else True,
            (f"price {'above' if side == 'long' else 'below'} VWAP") if vwap else "no VWAP yet")
        add("Not extended", ext <= 2.0, f"{ext:.1f}x ATR from VWAP (max 2.0)")
        add("Regime", (bias == "neutral") or (bias == side),
            f"market bias is {bias}" + ("" if bias in ("neutral", side) else " — you'd be fighting the tape"))
        add("Held on the list", (rec.get("cycles") or 0) >= 2,
            f"{rec.get('cycles', 1)} refresh cycles" +
            ("" if (rec.get("cycles") or 0) >= 2 else " — just appeared, let it prove itself"))
        add("Entry zone", past_r <= 0.6,
            "price is at/before the entry" if past_r <= 0.6 else f"price already {past_r:.1f}R past entry — don't chase")
        add("Session window", phase.get("tradeable", False), f"{phase.get('label', '')} window")

        hard_names = ("Not extended", "Entry zone", "Session window", "Right side of VWAP")
        hard_blocks = [c for c in checks if not c["ok"] and c["name"] in hard_names]
        soft_missing = [c for c in checks if not c["ok"] and c["name"] not in hard_names]

        if hard_blocks:
            verdict, reason = "SKIP", hard_blocks[0]["detail"]
            if hard_blocks[0]["name"] == "Entry zone":
                verdict, reason = "LATE", hard_blocks[0]["detail"] + " — wait for a pullback to the entry zone."
        elif not phase.get("tradeable", False) and phase.get("key") != "closed":
            verdict, reason = "WAIT", phase.get("note", "Not a tradeable window right now.")
        elif len(soft_missing) >= 3:
            verdict, reason = "WAIT", "Needs confirmation: " + "; ".join(c["detail"] for c in soft_missing[:2])
        elif soft_missing:
            verdict, reason = "WAIT", soft_missing[0]["detail"]
        else:
            verdict, reason = "READY", "All checks pass — take it at the entry zone with the stop as given."
        if phase.get("key") == "closed":
            verdict, reason = "NEXT SESSION", ("Levels are from the last completed session. Re-check after 09:30 IST "
                                               "once the new opening range is set.")
        return {"verdict": verdict, "reason": reason, "checks": checks}

    def _pace_fraction(self) -> float:
        now = now_ist()
        mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
        if mins < 0:
            mins = 375          # market not open yet -> use full-day lens
        return vol_pace_fraction(mins)

    def _gates(self, st: dict) -> tuple[bool, str]:
        if not st.get("ltp"):
            return False, "no-data"
        if st["symbol"] in set(self.settings.get("ban_list") or []):
            return False, "ban-list"
        if st["ltp"] < float(self.settings["min_price"]):
            return False, "price-floor"
        if not st.get("warmed"):
            return False, "warming"
        atrp = st.get("atr_pct")
        if atrp is not None and atrp < float(self.settings["min_atr_pct"]):
            return False, "low-volatility"
        sp = st.get("spread_pct")
        if sp is not None and sp > float(self.settings["max_spread_pct"]):
            return False, "wide-spread"
        pace = self._pace_fraction()
        turnover_cr = (st["ltp"] * (st.get("volume") or 0.0)) / 1e7
        projected = turnover_cr / max(0.05, pace)
        st["turnover_cr"] = turnover_cr
        st["turnover_proj_cr"] = projected
        if projected < float(self.settings["min_turnover_cr"]):
            return False, "illiquid"
        # circuit proximity: within 0.6% of a circuit means you may not be able to trade
        uc, lc = st.get("upper_c"), st.get("lower_c")
        if uc and st["ltp"] > uc * 0.994:
            st["near_circuit"] = "upper"
        elif lc and st["ltp"] < lc * 1.006:
            st["near_circuit"] = "lower"
        else:
            st["near_circuit"] = None
        return True, "pass"

    def _final_score_all(self):
        pace = self._pace_fraction()
        nifty_pct = (self.ctx.get("nifty") or {}).get("pct") or 0.0
        sec = {s["sector"]: s["pct"] for s in self.ctx.get("sectors") or []}
        for st in self.stocks.values():
            ok, why = self._gates(st)
            st["gate"] = why
            if not ok:
                st["sc_long"] = st["sc_short"] = 0.0
                st["why_long"], st["why_short"] = [], []
                st["trade_long"] = st["trade_short"] = None
                continue
            rs = (st.get("pct_chg") or 0.0) - nifty_pct
            rvol = self._rvol(st, pace)
            oi_sc = self._oi_signal(st)
            sec_pct = sec.get(st["sector"], 0.0)
            rpos = (st.get("range_pos", 50.0) - 50.0) / 50.0
            deep = st.get("deep")
            # ---- trade construction (levels) ------------------------------------
            tl = self._build_trade(st, "long")
            ts = self._build_trade(st, "short")
            st["trade_long"], st["trade_short"] = tl, ts
            for side, trade in (("long", tl), ("short", ts)):
                if trade is None:
                    continue
                comp, why, risks = self._components(st, side, trade, rs, rvol, oi_sc, sec_pct,
                                                    nifty_pct, rpos, deep)
                score = 100.0 * sum(W[k] * comp[k] for k in W)
                # conviction modifiers
                if side == "long" and st.get("vwap") and st["ltp"] < st["vwap"]:
                    score = min(score, 68.0)
                    risks.append("Trading below day VWAP")
                if side == "short" and st.get("vwap") and st["ltp"] > st["vwap"]:
                    score = min(score, 68.0)
                    risks.append("Trading above day VWAP")
                if st.get("near_circuit"):
                    score *= 0.88
                    risks.append(f"Near {st['near_circuit']} circuit — limited room")
                if trade["risk_pct"] > 1.8:
                    score *= 0.95
                    risks.append(f"Wide stop ({trade['risk_pct']:.1f}%) — size down")
                if (st.get("spread_pct") or 0) > 0.12:
                    score *= 0.96
                    risks.append(f"Spread {st['spread_pct']:.2f}%")
                if not deep:
                    score *= 0.97   # prefer names we have 1-min conviction on
                if side == "long":
                    st["sc_long"], st["why_long"], st["risk_long"] = round(score, 1), why, risks
                else:
                    st["sc_short"], st["why_short"], st["risk_short"] = round(score, 1), why, risks

    def _components(self, st, side, trade, rs, rvol, oi_sc, sec_pct, nifty_pct, rpos, deep):
        ltp = st["ltp"]
        vwap = st.get("vwap")
        atr = st.get("atr20") or ltp * 0.02
        ext = ((ltp - vwap) / atr) if vwap else 0.0
        mom15 = st.get("ret15") or 0.0
        slope = st.get("ema_slope") or 0.0
        burst = st.get("vol_burst") or 1.0
        rsi_v = st.get("rsi14")
        why, risks = [], []
        if side == "long":
            c_rs = sig(rs, 0.45)
            if rs > 0.4:
                why.append((0.24 * c_rs, f"Outperforming NIFTY by {rs:+.2f}% today"))
            elif rs < -0.4:
                risks.append(f"Laggard vs NIFTY ({rs:+.2f}%)")
            c_mom = sig(mom15 * 0.75 + ext * 0.55 - 0.15, 1.0)
            if mom15 > 0.2:
                why.append((0.20 * c_mom, f"15-min momentum {mom15:+.2f}%"))
            if vwap:
                if ltp > vwap:
                    why.append((0.18, f"Holding above day VWAP (₹{vwap:,.2f})"))
                if ext > 2.2:
                    risks.append(f"Extended {ext:.1f}x ATR above VWAP — wait for a pullback")
            if slope > 0.05:
                why.append((0.16, f"5-min EMA20 rising (+{slope:.2f}%/bar)"))
            if rsi_v:
                if 55 <= rsi_v <= 72:
                    why.append((0.10, f"RSI(5m) {rsi_v:.0f} — strong but not blown out"))
                elif rsi_v > 80:
                    risks.append(f"RSI(5m) {rsi_v:.0f} overbought")
            c_struct = self._struct_score(st, "long")
            if st.get("pdh") and ltp > st["pdh"]:
                why.append((0.22, f"Trading above yesterday's high (₹{st['pdh']:,.2f})"))
            if st.get("or_high") and ltp > st["or_high"]:
                why.append((0.20, f"Cleared opening-range high (₹{st['or_high']:,.2f})"))
            if st.get("hi20") and ltp >= st["hi20"] * 0.995:
                why.append((0.15, "At a 20-day breakout level"))
            if (st.get("hi_age_min") or 999) <= 20:
                why.append((0.12, "Fresh day high in the last 20 min"))
            if (st.get("higher_lows") or 0) >= 4:
                why.append((0.10, "Higher-lows structure intact on 5-min chart"))
            c_vol = sig((rvol - 1.0) * 1.05)
            if rvol >= 1.3:
                why.append((0.13 * c_vol, f"Volume {rvol:.1f}x its usual pace"))
            if burst >= 1.6:
                why.append((0.10, f"Volume burst {burst:.1f}x in last 10 min"))
            c_oi = clamp(0.5 + oi_sc * 0.5)
            if oi_sc > 0.5:
                why.append((0.12, f"Fresh long build-up (OI {st.get('oi_chg_pct', 0):+.1f}% with price up)"))
            elif oi_sc < -0.3:
                why.append((0.08, f"Short covering (OI {st.get('oi_chg_pct', 0):+.1f}%) — move may fade"))
                risks.append("Rally driven by short covering, not new longs")
            c_align = sig((sec_pct * 0.6 + nifty_pct * 0.4) * 1.2)
            if sec_pct > 0.2:
                why.append((0.08, f"Sector {st['sector']} +{sec_pct:.2f}% (tailwind)"))
            elif sec_pct < -0.2:
                risks.append(f"Sector {st['sector']} {sec_pct:.2f}% (headwind)")
        else:
            c_rs = sig(-rs, 0.45)
            if rs < -0.4:
                why.append((0.24 * c_rs, f"Underperforming NIFTY by {rs:+.2f}% today"))
            elif rs > 0.4:
                risks.append(f"Outperformer vs NIFTY ({rs:+.2f}%) — risky short")
            c_mom = sig(-mom15 * 0.75 - ext * 0.55 - 0.15, 1.0)
            if mom15 < -0.2:
                why.append((0.20 * c_mom, f"15-min momentum {mom15:+.2f}%"))
            if vwap:
                if ltp < vwap:
                    why.append((0.18, f"Below day VWAP (₹{vwap:,.2f}) — sellers in control"))
                if ext < -2.2:
                    risks.append(f"Stretched {ext:.1f}x ATR below VWAP — bounce risk")
            if slope < -0.05:
                why.append((0.16, f"5-min EMA20 falling ({slope:.2f}%/bar)"))
            if rsi_v:
                if 28 <= rsi_v <= 45:
                    why.append((0.10, f"RSI(5m) {rsi_v:.0f} — weak without being washed out"))
                elif rsi_v < 20:
                    risks.append(f"RSI(5m) {rsi_v:.0f} oversold — squeeze risk")
            c_struct = self._struct_score(st, "short")
            if st.get("pdl") and ltp < st["pdl"]:
                why.append((0.22, f"Below yesterday's low (₹{st['pdl']:,.2f})"))
            if st.get("or_low") and ltp < st["or_low"]:
                why.append((0.20, f"Broke opening-range low (₹{st['or_low']:,.2f})"))
            if st.get("lo20") and ltp <= st["lo20"] * 1.005:
                why.append((0.15, "At a 20-day breakdown level"))
            if (st.get("lo_age_min") or 999) <= 20:
                why.append((0.12, "Fresh day low in the last 20 min"))
            if (st.get("lower_highs") or 0) >= 4:
                why.append((0.10, "Lower-highs structure on 5-min chart"))
            c_vol = sig((rvol - 1.0) * 1.05)
            if rvol >= 1.3:
                why.append((0.13 * c_vol, f"Volume {rvol:.1f}x its usual pace"))
            if burst >= 1.6:
                why.append((0.10, f"Selling burst {burst:.1f}x in last 10 min"))
            c_oi = clamp(0.5 - oi_sc * 0.5)
            if oi_sc < -0.5:
                why.append((0.12, f"Fresh short build-up (OI {st.get('oi_chg_pct', 0):+.1f}% with price down)"))
            elif oi_sc > 0.3:
                why.append((0.08, f"Long unwinding (OI {st.get('oi_chg_pct', 0):+.1f}%)"))
                risks.append("Fall driven by long unwinding — may stabilise")
            c_align = sig(-(sec_pct * 0.6 + nifty_pct * 0.4) * 1.2)
            if sec_pct < -0.2:
                why.append((0.08, f"Sector {st['sector']} {sec_pct:.2f}% (tailwind for shorts)"))
            elif sec_pct > 0.2:
                risks.append(f"Sector {st['sector']} +{sec_pct:.2f}% (headwind)")
        # order-book pressure (ignored when the book is empty, e.g. after hours)
        tbq, tsq = st.get("tbq") or 0.0, st.get("tsq") or 0.0
        if tbq > 0 and tsq > 0 and market_status()["state"] == "open":
            ratio = tbq / tsq
            st["book_ratio"] = ratio
            if side == "long" and ratio >= 1.3:
                why.append((0.06, f"Bid side heavier ({ratio:.1f}x) — buyers stacked on the book"))
            elif side == "short" and ratio <= 0.75:
                why.append((0.06, f"Offer side heavier ({1/ratio:.1f}x) — sellers stacked on the book"))
        # setup quality from the actual trade plan
        rr = trade["rr"]
        c_setup = sig((rr - 1.5) * 0.85) * 0.75 + sig((1.6 - trade["risk_pct"]) * 1.2) * 0.25
        if rr >= 1.8:
            why.append((0.10, f"Risk-reward 1:{rr:.1f} to target 2"))
        why.sort(key=lambda x: -x[0])
        return ({"rs": c_rs, "momentum": c_mom, "structure": c_struct, "volume": c_vol,
                 "oi": c_oi, "setup": c_setup, "align": c_align},
                [w for _, w in why[:6]], risks[:4])

    # -------------------------------------------------------- trade plan levels
    def _build_trade(self, st: dict, side: str):
        ltp = st.get("ltp")
        if not ltp:
            return None
        atr = st.get("atr20") or ltp * 0.02
        vwap = st.get("vwap")
        dh, dl = st.get("day_high"), st.get("day_low")
        orh, orl = st.get("or_high"), st.get("or_low")
        pdh, pdl = st.get("pdh"), st.get("pdl")
        hi20, lo20 = st.get("hi20"), st.get("lo20")
        if side == "long":
            if dh and dh > 0 and ltp >= dh * 0.9985 and pdh and dh > pdh * 0.999:
                entry, mode = ltp, "BREAKOUT — buy strength above the day high"
            elif vwap and ltp > vwap and (ltp - vwap) <= 0.8 * atr:
                entry, mode = ltp, "BUY NOW — just above VWAP support"
            elif vwap and ltp > vwap:
                entry, mode = vwap + 0.05 * atr, "BUY DIP — wait for a pullback to VWAP"
            elif vwap:
                entry, mode = vwap + 0.05 * atr, "RECLAIM — needs to get back above VWAP"
            else:
                entry, mode = ltp, "BUY NOW"
            cands = [entry - 0.45 * atr]
            if vwap and vwap < entry:
                cands.append(vwap - 0.12 * atr)
            if orl and orl < entry:
                cands.append(orl - 0.10 * atr)
            if dl and dl < entry and dl > entry - 0.025 * entry:
                cands.append(dl - 0.05 * atr)
            valid = [c for c in cands if entry - c >= 0.003 * entry]
            sl = max(valid) if valid else entry - 0.45 * atr
            # never place a stop inside the noise: floor it at 0.30x ATR (and 0.35% of price)
            min_risk = max(0.0035 * entry, 0.30 * atr)
            if entry - sl < min_risk:
                sl = entry - min_risk
            risk = max(entry - sl, 0.0025 * entry)
            t1 = entry + max(1.0 * risk, 0.5 * atr)
            t2 = entry + max(2.0 * risk, 1.0 * atr)
            if hi20 and entry + 1.3 * risk < hi20 < t2:
                t2 = hi20                                  # 20-day high is a real magnet
            if pdh and entry + 0.7 * risk < pdh <= t1 * 1.001:
                t1 = pdh                                   # only if yesterday's high is actually ahead
        else:
            if dl and dl > 0 and ltp <= dl * 1.0015 and pdl and dl < pdl * 1.001:
                entry, mode = ltp, "BREAKDOWN — sell weakness below the day low"
            elif vwap and ltp < vwap and (vwap - ltp) <= 0.8 * atr:
                entry, mode = ltp, "SELL NOW — just below VWAP resistance"
            elif vwap and ltp < vwap:
                entry, mode = vwap - 0.05 * atr, "SELL RALLY — wait for a bounce into VWAP"
            elif vwap:
                entry, mode = vwap - 0.05 * atr, "LOSS OF VWAP — needs to lose VWAP support"
            else:
                entry, mode = ltp, "SELL NOW"
            cands = [entry + 0.45 * atr]
            if vwap and vwap > entry:
                cands.append(vwap + 0.12 * atr)
            if orh and orh > entry:
                cands.append(orh + 0.10 * atr)
            if dh and dh > entry and dh < entry + 0.025 * entry:
                cands.append(dh + 0.05 * atr)
            valid = [c for c in cands if c - entry >= 0.003 * entry]
            sl = min(valid) if valid else entry + 0.45 * atr
            min_risk = max(0.0035 * entry, 0.30 * atr)
            if sl - entry < min_risk:
                sl = entry + min_risk
            risk = max(sl - entry, 0.0025 * entry)
            t1 = entry - max(1.0 * risk, 0.5 * atr)
            t2 = entry - max(2.0 * risk, 1.0 * atr)
            if lo20 and t2 < lo20 < entry - 1.3 * risk:
                t2 = lo20
            if pdl and t1 * 0.999 <= pdl < entry - 0.7 * risk:
                t1 = pdl
        risk_pct = risk / entry * 100.0
        rr = (abs(t2 - entry) / risk) if risk else 0.0
        lot = st.get("lot_size") or 0
        return {
            "direction": side, "mode": mode,
            "entry": _r(entry), "sl": _r(sl), "t1": _r(t1), "t2": _r(t2),
            "rr": round(rr, 2), "risk_pct": round(risk_pct, 2),
            "risk_per_lot": round(risk * lot, 0), "notional_per_lot": round(entry * lot, 0),
            "lot_size": lot, "basis_note": None,
        }

    # ------------------------------------------------------------- final picks
    def _select_picks(self):
        self._sel_cycle = int(getattr(self, "_sel_cycle", 0)) + 1
        longs, shorts = [], []
        for st in self.stocks.values():
            if st["gate"] != "pass":
                continue
            if st["sc_long"] > 0 and st.get("trade_long"):
                longs.append(st)
            if st["sc_short"] > 0 and st.get("trade_short"):
                shorts.append(st)
        longs.sort(key=lambda s: -s["sc_long"])
        shorts.sort(key=lambda s: -s["sc_short"])
        bias = self.ctx.get("bias", "neutral")
        bias_bonus = 2.0 if bias == "long" else (-2.0 if bias == "short" else 0.0)
        floor = float(self.settings["score_floor"])
        # regime shifts the bar: the favoured side needs less, the other side more
        f_long = floor + (0 if bias == "long" else (6 if bias == "short" else 3))
        f_short = floor + (0 if bias == "short" else (6 if bias == "long" else 3))

        def build(lst, side, thresh, bonus):
            out, seen_sec, seen_sym = [], {}, set()
            for st in lst:
                sc = (st["sc_long"] if side == "long" else st["sc_short"]) + (bonus if (side == "long") == (bias == "long") else 0)
                if sc < thresh:
                    continue
                sec = st["sector"]
                if seen_sec.get(sec, 0) >= int(self.settings["sector_cap"]):
                    continue
                if st["symbol"] in seen_sym:
                    continue
                trade = st["trade_long"] if side == "long" else st["trade_short"]
                out.append(self._make_pick(st, side, round(sc, 1), trade, self.session_phase()))
                seen_sec[sec] = seen_sec.get(sec, 0) + 1
                seen_sym.add(st["symbol"])
                if len(out) >= int(self.settings["picks_per_side"]):
                    break
            return out

        if self.settings.get("show_longs", True):
            longs_picks = build(longs, "long", f_long, bias_bonus)
        else:
            longs_picks = []
        if self.settings.get("show_shorts", True):
            shorts_picks = build(shorts, "short", f_short, bias_bonus)
        else:
            shorts_picks = []
        for i, p in enumerate(longs_picks, 1):
            p["rank"] = i
        for i, p in enumerate(shorts_picks, 1):
            p["rank"] = i
        # remember the best rank each pick ever reached (shows how solid it is)
        for p in longs_picks + shorts_picks:
            rec = self.pick_log.get(f"{p['symbol']}-{p['direction']}")
            if rec is not None:
                rec["best_rank"] = min(rec.get("best_rank", 99), p["rank"])
        # every tracked symbol that was NOT refreshed in this selection cycle has missed one
        for key, rec in self.pick_log.items():
            if rec.get("cycle_seen") != getattr(self, "_sel_cycle", 0):
                rec["misses"] = int(rec.get("misses", 0)) + 1
        self.picks = {"long": longs_picks, "short": shorts_picks}
        self._track_and_log(longs_picks + shorts_picks)
        self._build_funnel(longs_picks, shorts_picks)

    def _make_pick(self, st, side, score, trade, phase):
        ltp = st["ltp"]
        key = f"{st['symbol']}-{side}"
        rec = self.pick_log.get(key)
        now = now_ist()
        if rec is None:
            rec = {"symbol": st["symbol"], "side": side, "first_seen": now.isoformat(timespec="seconds"),
                   "first_price": ltp, "entry": trade["entry"], "sl": trade["sl"],
                   "t2": trade["t2"], "max_score": score, "logged": False,
                   "cycles": 0, "streak_start": now.isoformat(timespec="seconds"),
                   "misses": 0, "best_rank": 99, "score_first": score, "score_history": []}
            self.pick_log[key] = rec
        if int(rec.get("misses", 0)) >= 2:
            # it was off the list for real (not just one flaky refresh) -> streak restarts
            rec["streak_start"] = now.isoformat(timespec="seconds")
        rec["misses"] = 0
        rec["cycle_seen"] = int(getattr(self, "_sel_cycle", 0))
        rec["last_seen"] = now.isoformat(timespec="seconds")
        rec["cycles"] = int(rec.get("cycles", 0)) + 1
        rec["max_score"] = max(rec.get("max_score", 0), score)
        rec["score_history"] = (rec.get("score_history", []) + [round(score, 1)])[-20:]
        since = (ltp / rec["first_price"] - 1.0) * 100.0 if rec["first_price"] else 0.0
        if side == "short":
            since = -since
        risk = abs(trade["entry"] - trade["sl"]) or 1e-9
        if side == "long":
            r_now = (ltp - trade["entry"]) / risk
            stopped = ltp <= trade["sl"]
        else:
            r_now = (trade["entry"] - ltp) / risk
            stopped = ltp >= trade["sl"]
        status = "WATCH" if abs(r_now) < 0.15 else ("WORKING" if r_now > 0.15 else "FADING")
        if r_now >= 0.8:
            status = "RUNNING"
        if stopped:
            status = "STOPPED"
        readiness = self._readiness(st, side, score, trade, rec, phase)
        hist = rec.get("score_history") or [score]
        trend = "rising" if len(hist) >= 3 and hist[-1] > hist[-3] + 1 else \
                ("cooling" if len(hist) >= 3 and hist[-1] < hist[-3] - 1 else "steady")
        try:
            on_list_min = round((now - datetime.fromisoformat(rec["streak_start"])).total_seconds() / 60.0, 1)
            since_min = round((now - datetime.fromisoformat(rec["first_seen"])).total_seconds() / 60.0, 1)
        except Exception:
            on_list_min = since_min = 0.0
        why = st["why_long"] if side == "long" else st["why_short"]
        risks = st["risk_long"] if side == "long" else st["risk_short"]
        nifty_pct = (self.ctx.get("nifty") or {}).get("pct")
        vec = [s for s in self.ctx["sectors"] if s["sector"] == st["sector"]]
        return {
            "symbol": st["symbol"], "name": st["name"], "sector": st["sector"],
            "direction": side, "score": score, "status": status,
            "ltp": _r(ltp), "pct_chg": round(st.get("pct_chg") or 0, 2),
            "rel_strength": round((st.get("pct_chg") or 0) - (nifty_pct or 0), 2),
            "fut_symbol": st.get("fut_symbol"), "fut_ltp": _r(st.get("fut_ltp")),
            "fut_expiry": st.get("fut_expiry"), "lot_size": st.get("lot_size"),
            "levels": trade, "why": why, "risks": risks,
            "spark": st.get("spark") or [], "rvol": round(self._rvol(st, self._pace_fraction()), 2),
            "oi_chg_pct": round(st.get("oi_chg_pct") or 0, 2),
            "turnover_cr": round(st.get("turnover_cr") or 0, 1),
            "vwap": _r(st.get("vwap")), "pdh": _r(st.get("pdh")), "pdl": _r(st.get("pdl")),
            "day_high": _r(st.get("day_high")), "day_low": _r(st.get("day_low")),
            "or_high": _r(st.get("or_high")), "or_low": _r(st.get("or_low")),
            "atr_pct": round(st.get("atr_pct") or 0, 2),
            "spread_pct": round(st.get("spread_pct") or 0, 3),
            "sector_pct": (vec[0]["pct"] if vec else None),
            "since_flagged_pct": round(since, 2),
            "flagged_at": rec["first_seen"], "r_now": round(r_now, 2),
            "readiness": readiness,
            "stability": {"on_list_min": on_list_min, "since_flagged_min": since_min,
                          "cycles": rec.get("cycles", 1), "best_rank": min(rec.get("best_rank", 99), 99),
                          "score_peak": round(rec.get("max_score", score), 1),
                          "score_trend": trend, "fresh": (rec.get("cycles", 1) < 2)},
        }

    def _track_and_log(self, picks):
        day = now_ist().date().isoformat()
        path = os.path.join(self.log_dir, f"picks_{day}.csv")
        new = not os.path.exists(path)
        try:
            with open(path, "a") as f:
                if new:
                    f.write("logged_at,symbol,side,score,ltp,entry,sl,t1,t2,rr,status,mode,fut_symbol\n")
                for p in picks:
                    key = f"{p['symbol']}-{p['direction']}"
                    rec = self.pick_log.get(key)
                    if rec and not rec["logged"]:
                        rec["logged"] = True
                        lv = p["levels"]
                        f.write(f"{now_ist().isoformat(timespec='seconds')},{p['symbol']},"
                                f"{p['direction']},{p['score']},{p['ltp']},{lv['entry']},{lv['sl']},"
                                f"{lv['t1']},{lv['t2']},{lv['rr']},{p['status']},"
                                f"\"{lv['mode']}\",{p.get('fut_symbol')}\n")
        except Exception:
            pass

    def _build_funnel(self, longs, shorts):
        n = len(self.stocks)
        gate_counts: dict[str, int] = {}
        for st in self.stocks.values():
            g = "pass" if st["gate"] == "pass" else st["gate"]
            gate_counts[g] = gate_counts.get(g, 0) + 1
        liquid = gate_counts.get("pass", 0)
        momentum = sum(1 for st in self.stocks.values()
                       if st["gate"] == "pass" and max(st["pre_long"], st["pre_short"]) >= 45)
        self.funnel = {
            "all": n,
            "scanned": sum(1 for st in self.stocks.values() if st.get("ltp")),
            "liquid": liquid,
            "momentum": momentum,
            "deep": self.state_meta.get("deep_scanned", 0),
            "final": len(longs) + len(shorts),
            "rejects": sorted(gate_counts.items(), key=lambda x: -x[1]),
        }

    # ---------------------------------------------------------------- payload
    def snapshot(self) -> dict:
        with self._lock:
            stocks = []
            for st in self.stocks.values():
                if not st.get("ltp"):
                    continue
                stocks.append({
                    "symbol": st["symbol"], "sector": st["sector"], "ltp": _r(st.get("ltp")),
                    "pct_chg": round(st.get("pct_chg") or 0, 2),
                    "range_pos": round(st.get("range_pos") or 50, 1),
                    "rvol": round(self._rvol(st, self._pace_fraction()), 2),
                    "oi_chg": round(st.get("oi_chg_pct") or 0, 2),
                    "turnover_cr": round(st.get("turnover_cr") or 0, 1),
                    "atr_pct": round(st.get("atr_pct") or 0, 2),
                    "spread_pct": round(st.get("spread_pct") or 0, 3),
                    "score_long": st["sc_long"], "score_short": st["sc_short"],
                    "pre": round(max(st["pre_long"], st["pre_short"]), 1),
                    "gate": st["gate"], "deep": bool(st.get("deep")), "warmed": bool(st.get("warmed")),
                    "fut_symbol": st.get("fut_symbol"), "lot": st.get("lot_size"),
                    "vwap": _r(st.get("vwap")), "pdh": _r(st.get("pdh")), "pdl": _r(st.get("pdl")),
                    "day_high": _r(st.get("day_high")), "day_low": _r(st.get("day_low")),
                    "near_circuit": st.get("near_circuit"),
                })
            stocks.sort(key=lambda s: -max(s["score_long"] or 0, s["score_short"] or 0))
            total = len(self.stocks)
            warmed = sum(1 for s in self.stocks.values() if s.get("warmed"))
            covered = round((warmed + 0.5 * sum(1 for s in self.stocks.values() if s.get("deep"))) / total * 100) if total else 0
            return {
                "ok": True,
                "mode": "demo" if self.demo else "live",
                "server_time_ist": now_ist().isoformat(timespec="seconds"),
                "context": self.ctx,
                "funnel": self.funnel,
                "phase": self.session_phase(),
                "picks": self.picks,
                "universe": stocks,
                "stats": {**self.state_meta, "coverage_pct": min(covered, 100),
                          "refresh_sec": self.settings["refresh_sec"],
                          "deep_every_sec": self._deep_deadline_sec(),
                          "total": total, "warmed": warmed,
                          "api": getattr(self.src, "stats", {})},
            }


def _ts(v):
    """Timestamps arrive as epoch-ms (v2) or ISO strings (v3) — normalise to epoch ms."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        dt = parse_ts(str(v))
        return int(dt.timestamp() * 1000) if dt else None
    except Exception:
        return None


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _r(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None
