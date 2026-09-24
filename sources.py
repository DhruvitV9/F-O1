"""
Data sources for the engine.

LiveSource  -> your Upstox account (real-time market data plan)
DemoSource  -> synthetic but API-shaped data, so the whole pipeline can be
               exercised (and the UI previewed) without a token or when the
               market is shut. Generated deterministically per day.

Both expose the same three methods the engine needs:
    quotes(keys) -> {instrument_key: quote_dict}
    intraday(key, unit, interval) -> [[ts, o, h, l, c, v, oi], ...] oldest-first
    daily(key, days) -> same shape
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from upstox_client import IST, UpstoxClient

SESSION_OPEN = (9, 15)
SESSION_CLOSE = (15, 30)
TOTAL_MINUTES = 375


# ------------------------------------------------------------ market status ---
def market_status(now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    mins = now.hour * 60 + now.minute
    open_m, close_m = SESSION_OPEN[0] * 60 + SESSION_OPEN[1], SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]
    if now.weekday() >= 5:
        state, live = "weekend", False
    elif mins < open_m - 15:
        state, live = "closed", False
    elif mins < open_m:
        state, live = "pre-open", False
    elif mins <= close_m:
        state, live = "open", True
    elif mins <= close_m + 40:
        state, live = "post-market", False
    else:
        state, live = "closed", False
    return {"state": state, "is_live": live, "ist": now.isoformat(timespec="seconds"),
            "ist_hm": now.strftime("%H:%M:%S"), "weekday": now.strftime("%a")}


def session_date(now: datetime | None = None) -> str:
    """The trading session these levels apply to: today while the market is
    trading/complete, otherwise the upcoming session."""
    now = (now or datetime.now(IST)).astimezone(IST)
    d = now.date()
    after_close = (now.hour * 60 + now.minute) > (15 * 60 + 40)
    if after_close:
        d += timedelta(days=1)
    if now.weekday() >= 5 and not after_close:
        pass
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def minutes_elapsed(now: datetime | None = None) -> int:
    now = (now or datetime.now(IST)).astimezone(IST)
    m = (now.hour * 60 + now.minute) - (SESSION_OPEN[0] * 60 + SESSION_OPEN[1])
    return max(1, min(TOTAL_MINUTES, m))


# ------------------------------------------------------------------- live -----
class LiveSource:
    def __init__(self, token: str, quote_per_min: float = 240, history_per_min: float = 95):
        self.client = UpstoxClient(token, quote_per_min=quote_per_min,
                                   history_per_min=history_per_min)

    @property
    def stats(self):
        return self.client.stats

    def quotes(self, keys):
        return self.client.full_quotes(keys)

    def ltp(self, keys):
        return self.client.ltp(keys)

    def intraday(self, key, unit="minutes", interval=1):
        return self.client.intraday_candles(key, unit, interval)

    def daily(self, key, days=60):
        return self.client.daily_candles(key, days)



# ------------------------------------------------------------------- demo -----
# A handful of well-known names get plausible prices so the demo looks real.
_DEMO_PRICES = {
    "RELIANCE": 1485, "HDFCBANK": 985, "ICICIBANK": 1382, "INFY": 1548, "TCS": 3175,
    "SBIN": 862, "AXISBANK": 1186, "KOTAKBANK": 1912, "ITC": 421, "LT": 3695,
    "BHARTIARTL": 1925, "MARUTI": 12980, "TMPV": 690, "M&M": 3268, "SUNPHARMA": 1785,
    "TATASTEEL": 168, "JSWSTEEL": 1052, "HINDALCO": 745, "ADANIENT": 2480, "ADANIPORTS": 1452,
    "BAJFINANCE": 985, "TITAN": 3560, "ASIANPAINT": 2540, "HINDUNILVR": 2385, "DMART": 4120,
    "HAL": 4680, "BEL": 398, "BDL": 1725, "MAZDOCK": 2410, "DIXON": 16240,
    "PERSISTENT": 5780, "COFORGE": 8320, "POLYCAB": 6580, "KEI": 4980, "SUZLON": 68,
    "IDEA": 8.6, "NYKAA": 205, "PAYTM": 1145, "IRFC": 128, "RVNL": 385,
    "TATAPOWER": 412, "NTPC": 336, "POWERGRID": 268, "COALINDIA": 392, "VEDL": 512,
    "DLF": 742, "GODREJPROP": 2180, "OBEROIRLTY": 1712, "LODHA": 1045, "TRENT": 5180,
    "ZYDUSLIFE": 1042, "LUPIN": 2085, "AUROPHARMA": 1385, "GLENMARK": 1820,
}
# designed movers so the funnel + ranking logic really gets exercised
_DEMO_STRONG = ["PERSISTENT", "POLYCAB", "HAL", "KEI", "MAZDOCK", "BDL", "DIXON", "RVNL", "BEL", "TATAPOWER"]
_DEMO_WEAK = ["IDEA", "NYKAA", "PAYTM", "SUZLON", "WIPRO", "TECHM", "COFORGE", "BIOCON", "UPL", "BANDHANBNK"]

_INDEX_BASE = {"NIFTY": 25420, "BANKNIFTY": 56830, "FINNIFTY": 27410, "MIDCPNIFTY": 13820,
               "INDIAVIX": 12.6, "NIFTY100": 61240, "SMALLCAP": 18960}


class DemoSource:
    """Synthetic data with internally consistent daily + minute + quote series."""

    def __init__(self, universe: dict, seed: str | None = None):
        self.uni = universe
        self.seed = seed or datetime.now(IST).date().isoformat()
        self._cache: dict[str, tuple[list, list]] = {}
        self._meta: dict[str, dict] = {}
        self._prep()

    @property
    def stats(self):
        return {"calls": 0, "errors": 0, "last_error": "", "last_error_at": None}

    # ---------------------------------------------------------------- setup
    def _rng(self, key: str):
        return random.Random(f"{self.seed}:{key}")

    def _params(self, sym: str) -> dict:
        r = self._rng(sym)
        price = _DEMO_PRICES.get(sym) or round(math.exp(r.uniform(4.6, 8.2)) / 5) * 5
        daily_vol = r.uniform(0.012, 0.030)
        tier = r.random()
        turnover_cr = (r.uniform(1200, 3600) if tier > 0.75 else
                       r.uniform(280, 1100) if tier > 0.35 else r.uniform(55, 300))
        if sym in _DEMO_STRONG:
            gap, drift, volmult, oi_chg = r.uniform(0.35, 1.5), r.uniform(1.6, 3.6), r.uniform(2.0, 4.4), r.uniform(4.0, 9.5)
        elif sym in _DEMO_WEAK:
            gap, drift, volmult, oi_chg = -r.uniform(0.35, 1.5), -r.uniform(1.5, 3.4), r.uniform(1.8, 4.0), r.uniform(3.5, 9.0)
        else:
            gap, drift, volmult, oi_chg = r.uniform(-0.8, 0.8), r.uniform(-1.3, 1.3), r.uniform(0.7, 1.7), r.uniform(-4.0, 4.0)
        return {"price": float(price), "daily_vol": daily_vol, "turnover_cr": turnover_cr,
                "gap": gap, "drift": drift, "volmult": volmult, "oi_chg": oi_chg,
                "oi": r.uniform(4e6, 3.5e7)}

    def _prep(self):
        for st in self.uni["stocks"]:
            sym = st["symbol"]
            p = self._params(sym)
            meta = {"sym": sym, "equity_key": st["equity_key"], "fut_key": st["fut_key"],
                    "fut_sym": st["fut_symbol"], "lot": st["lot_size"], **p}
            self._meta[st["equity_key"]] = meta
            self._meta[st["fut_key"]] = {**meta, "is_fut": True}
        for name, key in (self.uni.get("index_keys") or {}).items():
            self._meta[key] = {"sym": name, "index": True,
                               "price": _INDEX_BASE.get(name, 1200.0),
                               "daily_vol": 0.055 if name == "INDIAVIX" else 0.0075,
                               "drift": 0.35 if name != "INDIAVIX" else -2.0,
                               "gap": 0.0, "volmult": 1.0, "oi_chg": 0.0, "turnover_cr": 0.0, "oi": 0.0}

    # ----------------------------------------------------------- generators
    def _walk_daily(self, key: str, meta: dict, n: int = 61) -> list:
        """Daily candles ending at meta['price'] (the previous session's close),
        then the current session appended as the last row."""
        r = self._rng(key + ":daily")
        today = datetime.now(IST).date()
        days = []
        d = today - timedelta(days=int(n * 1.5) + 4)
        while d <= today:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        days = days[-(n + 1):]
        prev_days = days[:-1]
        # build closes backwards so the series lands exactly on the baseline
        closes = [meta["price"] * (1 + r.gauss(0, meta["daily_vol"]) * 0) for _ in range(len(prev_days))]
        px = meta["price"]
        for i in range(len(prev_days) - 1, -1, -1):
            closes[i] = px
            px = px / (1 + r.gauss(0, meta["daily_vol"]))
        rows = []
        for i, d in enumerate(prev_days):
            c = closes[i]
            o = c * (1 + r.gauss(0, meta["daily_vol"] * 0.3))
            hi = max(o, c) * (1 + abs(r.gauss(0, meta["daily_vol"] * 0.45)))
            lo = min(o, c) * (1 - abs(r.gauss(0, meta["daily_vol"] * 0.45)))
            vol = (meta["turnover_cr"] * 1e7 / max(c, 1)) * r.uniform(0.65, 1.5)
            rows.append([d.isoformat() + "T15:30:00+05:30", round(o, 2), round(hi, 2),
                         round(lo, 2), round(c, 2), round(vol), None])
        # current session, aggregated from the minute series
        minutes = self._walk_minutes(key, meta)
        if minutes:
            o = minutes[0][1]
            hi = max(m[2] for m in minutes)
            lo = min(m[3] for m in minutes)
            c = minutes[-1][4]
            vol = sum(m[5] for m in minutes)
            rows.append([days[-1].isoformat() + "T15:30:00+05:30", round(o, 2), round(hi, 2),
                         round(lo, 2), round(c, 2), round(vol), None])
        return rows

    def _session_end_minute(self) -> int:
        ms = market_status()
        if ms["is_live"]:
            return minutes_elapsed()
        return TOTAL_MINUTES

    def _walk_minutes(self, key: str, meta: dict) -> list:
        """Intraday path pinned so the session closes exactly at the intended move."""
        r = self._rng(key + ":min")
        prev_close = meta["price"]
        open_px = prev_close * (1 + meta["gap"] / 100.0)
        end_min = self._session_end_minute()
        target = open_px * (1 + meta["drift"] / 100.0)
        vol_sd = meta["daily_vol"] * (0.17 if meta.get("index") else 0.30)
        incs = [meta["drift"] / 100.0 / TOTAL_MINUTES + r.gauss(0, vol_sd / math.sqrt(TOTAL_MINUTES))
                for _ in range(end_min)]
        px = open_px
        for g in incs:
            px *= (1 + g)
        if end_min:
            k = (target - px) / end_min / max(open_px, 1e-9)
            incs = [g + k for g in incs]
        base_vol_min = (meta["turnover_cr"] * 1e7 / max(open_px, 1)) / TOTAL_MINUTES * meta["volmult"]
        today = datetime.now(IST).date()
        # noise oscillates around the pinned path (mean-reverting), so the session
        # closes at the intended move instead of drifting away from it
        rho = 0.94
        wiggle_sd = meta["daily_vol"] * (0.22 if meta.get("index") else 0.34) * math.sqrt(1 - rho * rho)
        wiggle = 0.0
        path, out = open_px, []
        for i in range(end_min):
            t = datetime(today.year, today.month, today.day, 9, 15, tzinfo=IST) + timedelta(minutes=i)
            path = path * (1 + incs[i])
            wiggle = wiggle * rho + r.gauss(0, wiggle_sd)
            px = path * (1 + wiggle)
            u_shape = 2.0 if (i < 20 or i > TOTAL_MINUTES - 40) else (0.75 if 120 < i < 260 else 1.0)
            o = px * (1 + r.gauss(0, 0.0003))
            c = px * (1 + r.gauss(0, 0.0003))
            hi = max(o, c, px) * (1 + abs(r.gauss(0, 0.00045)))
            lo = min(o, c, px) * (1 - abs(r.gauss(0, 0.00045)))
            v = max(1.0, base_vol_min * u_shape * r.uniform(0.55, 1.6)) if meta["turnover_cr"] else 0.0
            out.append([t.isoformat(), round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), round(v), None])
        if out:
            out[-1][4] = round(path, 2)
        return out

    def _series(self, key: str) -> tuple[list, list]:
        if key not in self._cache:
            meta = self._meta.get(key)
            if not meta:
                self._cache[key] = ([], [])
            else:
                daily = self._walk_daily(key, meta)
                minute = self._walk_minutes(key, meta)
                self._cache[key] = (daily, minute)
        return self._cache[key]

    # ------------------------------------------------------ engine interface
    def daily(self, key, days=60):
        return self._series(key)[0]

    def intraday(self, key, unit="minutes", interval=1):
        return self._series(key)[1]

    def _find_eq_key(self, eq_key):
        return eq_key

    def quotes(self, keys):
        out = {}
        ts = int(datetime.now(IST).timestamp() * 1000)
        for key in keys:
            meta = self._meta.get(key)
            if not meta:
                continue
            daily, mins = self._series(key)
            if not mins:
                continue
            pc = daily[-2][4] if len(daily) > 1 else mins[0][1]
            ltp = mins[-1][4]
            if meta.get("is_fut"):
                basis = 1.0042
                spot_daily, _ = self._series(meta["equity_key"])
                oi = meta["oi"] * (1 + meta["oi_chg"] / 100.0)
                out[key] = {"instrument_token": key, "last_price": round(ltp * basis, 2),
                            "ohlc": {"open": round(mins[0][1] * basis, 2),
                                     "high": round(max(m[2] for m in mins) * basis, 2),
                                     "low": round(min(m[3] for m in mins) * basis, 2),
                                     "close": round(pc * basis, 2)},
                            "volume": int(sum(m[5] for m in mins)),
                            "oi": round(oi), "oi_day_high": round(oi * 1.0012),
                            "oi_day_low": round(oi * 0.9994),
                            "net_change": round((ltp - pc) * basis, 2), "timestamp": ts, "depth": {}}
                continue
            spread = max(0.01, round(ltp * 0.0002, 2))
            out[key] = {
                "instrument_token": key, "last_price": ltp,
                "ohlc": {"open": mins[0][1], "high": max(m[2] for m in mins),
                         "low": min(m[3] for m in mins), "close": pc},
                "volume": int(sum(m[5] for m in mins)),
                "average_price": round(sum(m[4] for m in mins) / len(mins), 2),
                "net_change": round(ltp - pc, 2),
                "oi": None, "total_buy_quantity": 1200, "total_sell_quantity": 1300,
                "lower_circuit_limit": round(pc * 0.8, 2), "upper_circuit_limit": round(pc * 1.2, 2),
                "last_trade_time": ts, "timestamp": ts,
                "depth": {"buy": [{"price": round(ltp - spread, 2), "quantity": 1500, "orders": 6}],
                          "sell": [{"price": round(ltp + spread, 2), "quantity": 1600, "orders": 7}]},
            }
        return out

    def ltp(self, keys):
        return {k: {"instrument_token": k, "last_price": v.get("last_price")}
                for k, v in self.quotes(keys).items()}
