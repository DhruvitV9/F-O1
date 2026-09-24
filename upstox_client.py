"""
Thin, resilient Upstox API v3 client (quotes + candles), built for polling.

Handles:  token injection, URL-encoding of instrument keys, rate-limit buckets
(history has a much lower limit than quotes), retry with backoff, and clear
error messages the UI can show to the user.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
API_ROOT = "https://api.upstox.com"


class UpstoxError(Exception):
    def __init__(self, msg, status=None, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code

    def friendly(self) -> str:
        if self.status == 401 or self.status == 403:
            return "Access token invalid or expired — generate a fresh token for today."
        if "UDAPI100043" in str(self.code or "") or "500" in str(self.code or ""):
            return "Too many instrument keys in one request (Upstox limit is 500)."
        if self.status == 429:
            return "Rate limit hit — Upstox is throttling. The app will slow down automatically."
        return str(self)


class _Bucket:
    """Simple sliding-window rate limiter shared by all threads."""

    def __init__(self, per_minute: float):
        self.limit = per_minute
        self.times: deque[float] = deque()
        self.lock = threading.Lock()

    def set_limit(self, per_minute: float):
        with self.lock:
            self.limit = max(5.0, per_minute)

    def acquire(self, timeout: float = 30.0):
        deadline = time.time() + timeout
        while True:
            with self.lock:
                now = time.time()
                while self.times and now - self.times[0] > 60.0:
                    self.times.popleft()
                if len(self.times) < self.limit:
                    self.times.append(now)
                    return True
                wait = 60.0 - (now - self.times[0]) + 0.02
            if time.time() > deadline:
                return False
            time.sleep(min(max(wait, 0.02), 1.0))


class UpstoxClient:
    def __init__(self, token: str, quote_per_min: float = 240, history_per_min: float = 80,
                 timeout: float = 20.0):
        self.token = (token or "").strip()
        self.timeout = timeout
        self.quote_bucket = _Bucket(quote_per_min)
        self.hist_bucket = _Bucket(history_per_min)
        self.stats = {"calls": 0, "errors": 0, "last_error": "", "last_error_at": None}
        self._s = threading.Lock()

    # ------------------------------------------------------------------ core
    def _get(self, path: str, params: dict | None = None, kind: str = "quote",
             retries: int = 3) -> dict:
        bucket = self.quote_bucket if kind == "quote" else self.hist_bucket
        url = f"{API_ROOT}{path}"
        if params:
            # values may already be percent-encoded (instrument keys), so keep % , | : intact
            url += "?" + urllib.parse.urlencode(params, safe="%,|:")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "fno-screener/1.0",
        }
        last_exc = None
        for attempt in range(retries):
            if not bucket.acquire():
                raise UpstoxError("Local rate limiter timed out waiting for a slot", 429)
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                with self._s:
                    self.stats["calls"] += 1
                return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                try:
                    text = e.read().decode("utf-8", "replace")
                except Exception:
                    text = str(e)
                code = None
                try:
                    errs = (json.loads(text) or {}).get("errors") or []
                    if errs and isinstance(errs, list):
                        code = errs[0].get("errorCode")
                except Exception:
                    pass
                with self._s:
                    self.stats["calls"] += 1
                last_exc = UpstoxError(text[:300], e.code, code)
                if e.code in (401, 403) or (e.code == 400 and attempt == retries - 1):
                    break
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(min((2.0 ** attempt) * (3.0 if e.code == 429 else 1.2), 20.0))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = UpstoxError(f"network: {e}")
                time.sleep(min(2.0 ** attempt, 8.0))
        with self._s:
            self.stats["errors"] += 1
            self.stats["last_error"] = last_exc.friendly() if last_exc else "unknown error"
            self.stats["last_error_at"] = datetime.now(IST).isoformat(timespec="seconds")
        raise last_exc or UpstoxError("request failed")

    @staticmethod
    def _enc_keys(keys) -> str:
        """Percent-encode each instrument key; commas stay literal as separators."""
        if isinstance(keys, str):
            keys = [keys]
        return ",".join(urllib.parse.quote(str(k).strip(), safe="|") for k in keys)

    # ---------------------------------------------------------------- quotes
    def full_quotes(self, keys, chunk: int = 200) -> dict:
        """Full market quotes (OHLC + volume + OI + depth + circuits). Keyed by instrument_key."""
        keys = list(dict.fromkeys([k for k in keys if k]))
        out: dict[str, dict] = {}
        for i in range(0, len(keys), chunk):
            part = keys[i:i + chunk]
            data = self._get("/v3/market-quote/quotes",
                             {"instrument_key": self._enc_keys(part)}, kind="quote")
            payload = (data or {}).get("data") or {}
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(v, dict):
                        ik = (v.get("instrument_token") or "").strip() or self._key_from_payload_key(k)
                        out[ik] = v
        return out

    @staticmethod
    def _key_from_payload_key(k: str) -> str:
        # v2/v3 return dict keys like "NSE_EQ:RELIANCE" or "NSE_EQ|INE002A01018"
        if ":" in k:
            ex, sym = k.split(":", 1)
            return f"{ex}|{sym}"
        return k

    def ltp(self, keys, chunk: int = 200) -> dict:
        """LTP quotes — cheapest call, used for indices."""
        keys = list(dict.fromkeys([k for k in keys if k]))
        out: dict[str, dict] = {}
        for i in range(0, len(keys), chunk):
            part = keys[i:i + chunk]
            data = self._get("/v3/market-quote/ltp",
                             {"instrument_key": self._enc_keys(part)}, kind="quote")
            payload = (data or {}).get("data") or {}
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(v, dict):
                        ik = (v.get("instrument_token") or "").strip() or self._key_from_payload_key(k)
                        out[ik] = v
        return out

    # --------------------------------------------------------------- candles
    def intraday_candles(self, key: str, unit: str = "minutes", interval: int = 1) -> list:
        """Today's candles. Returns list of [ts, o, h, l, c, v, oi] ascending by time."""
        data = self._get(f"/v3/historical-candle/intraday/{urllib.parse.quote(key, safe='|')}"
                         f"/{unit}/{interval}", kind="history")
        return _candles(data)

    def daily_candles(self, key: str, days: int = 90) -> list:
        """Daily candles for the recent past (ascending)."""
        to_d = datetime.now(IST).date()
        from_d = to_d - timedelta(days=int(days * 1.6) + 5)
        data = self._get(f"/v3/historical-candle/{urllib.parse.quote(key, safe='|')}"
                         f"/days/1/{to_d.isoformat()}/{from_d.isoformat()}", kind="history")
        return _candles(data)


def _candles(data: dict) -> list:
    cds = ((data or {}).get("data") or {}).get("candles") or []
    out = []
    for c in cds:
        try:
            ts, o, h, l, cl = c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4])
            v = float(c[5]) if len(c) > 5 and c[5] is not None else 0.0
            oi = float(c[6]) if len(c) > 6 and c[6] is not None else None
        except (TypeError, ValueError, IndexError):
            continue
        out.append([ts, o, h, l, cl, v, oi])
    # Upstox returns newest-first for intraday; normalise to oldest-first
    try:
        out.sort(key=lambda x: str(x[0]))
    except Exception:
        pass
    return out


def parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(IST)
    except Exception:
        return None
