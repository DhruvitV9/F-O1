"""
F&O Universe builder.

Pulls ALL NSE stock-F&O underlyings straight from the Upstox instrument master,
so the list never goes stale (new F&O entrants appear automatically).

For every underlying we resolve:
  - the NSE cash (spot) instrument key  -> used for live quotes / VWAP / candles
  - the nearest tradeable stock-futures contract -> key, symbol, expiry, lot size
  - a sector tag -> used for sector-strength context and sector-capped picks
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"

SYM_RE = re.compile(r"^(?P<sym>.+?)\d{2}[A-Z]{3}FUT$")

# ---------------------------------------------------------------- indices ----
# Keys verified against the live instrument master.
INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "INDIAVIX": "NSE_INDEX|India VIX",
    "NIFTY100": "NSE_INDEX|NIFTY MIDCAP 100",
    "SMALLCAP": "NSE_INDEX|NIFTY SMLCAP 100",
    # sector indices (context only)
    "NIFTY IT": "NSE_INDEX|Nifty IT",
    "NIFTY AUTO": "NSE_INDEX|Nifty Auto",
    "NIFTY PHARMA": "NSE_INDEX|Nifty Pharma",
    "NIFTY METAL": "NSE_INDEX|Nifty Metal",
    "NIFTY ENERGY": "NSE_INDEX|Nifty Energy",
    "NIFTY OIL AND GAS": "NSE_INDEX|NIFTY OIL AND GAS",
    "NIFTY FMCG": "NSE_INDEX|Nifty FMCG",
    "NIFTY REALTY": "NSE_INDEX|Nifty Realty",
    "NIFTY PSU BANK": "NSE_INDEX|Nifty PSU Bank",
    "NIFTY PVT BANK": "NSE_INDEX|Nifty Pvt Bank",
    "NIFTY INFRA": "NSE_INDEX|Nifty Infra",
    "NIFTY CEMENT": "NSE_INDEX|Nifty Cement",
    "NIFTY CHEMICALS": "NSE_INDEX|Nifty Chemicals",
    "NIFTY HEALTHCARE": "NSE_INDEX|NIFTY HEALTHCARE",
    "NIFTY CONSR DURBL": "NSE_INDEX|NIFTY CONSR DURBL",
    "NIFTY IND DEFENCE": "NSE_INDEX|Nifty Ind Defence",
    "NIFTY CAPITAL MKT": "NSE_INDEX|Nifty Capital Mkt",
    "NIFTY IND TOURISM": "NSE_INDEX|Nifty Ind Tourism",
    "NIFTY 500": "NSE_INDEX|Nifty 500",
}

# --------------------------------------------------------------- sectors ----
# symbol -> (sector label, representative index used for sector strength)
SECTOR_OF = {
    # Banks
    "HDFCBANK": ("Banks - Private", "NIFTY PVT BANK"), "ICICIBANK": ("Banks - Private", "NIFTY PVT BANK"),
    "KOTAKBANK": ("Banks - Private", "NIFTY PVT BANK"), "AXISBANK": ("Banks - Private", "NIFTY PVT BANK"),
    "INDUSINDBK": ("Banks - Private", "NIFTY PVT BANK"), "FEDERALBNK": ("Banks - Private", "NIFTY PVT BANK"),
    "RBLBANK": ("Banks - Private", "NIFTY PVT BANK"), "AUBANK": ("Banks - Private", "NIFTY PVT BANK"),
    "BANDHANBNK": ("Banks - Private", "NIFTY PVT BANK"), "IDFCFIRSTB": ("Banks - Private", "NIFTY PVT BANK"),
    "YESBANK": ("Banks - Private", "NIFTY PVT BANK"),
    "SBIN": ("Banks - PSU", "NIFTY PSU BANK"), "BANKBARODA": ("Banks - PSU", "NIFTY PSU BANK"),
    "PNB": ("Banks - PSU", "NIFTY PSU BANK"), "CANBK": ("Banks - PSU", "NIFTY PSU BANK"),
    "UNIONBANK": ("Banks - PSU", "NIFTY PSU BANK"), "INDIANB": ("Banks - PSU", "NIFTY PSU BANK"),
    "BANKINDIA": ("Banks - PSU", "NIFTY PSU BANK"), "MAHABANK": ("Banks - PSU", "NIFTY PSU BANK"),
    # NBFC / lending / insurance / capital markets / fintech
    "BAJFINANCE": ("NBFC & Lending", "FINNIFTY"), "BAJAJFINSV": ("NBFC & Lending", "FINNIFTY"),
    "BAJAJHLDNG": ("NBFC & Lending", "FINNIFTY"), "CHOLAFIN": ("NBFC & Lending", "FINNIFTY"),
    "SHRIRAMFIN": ("NBFC & Lending", "FINNIFTY"), "MUTHOOTFIN": ("NBFC & Lending", "FINNIFTY"),
    "MANAPPURAM": ("NBFC & Lending", "FINNIFTY"), "LTF": ("NBFC & Lending", "FINNIFTY"),
    "MFSL": ("NBFC & Lending", "FINNIFTY"), "LICHSGFIN": ("NBFC & Lending", "FINNIFTY"),
    "PNBHOUSING": ("NBFC & Lending", "FINNIFTY"), "SBICARD": ("NBFC & Lending", "FINNIFTY"),
    "JIOFIN": ("NBFC & Lending", "FINNIFTY"), "ABCAPITAL": ("NBFC & Lending", "FINNIFTY"),
    "IRFC": ("NBFC & Lending", "FINNIFTY"), "PFC": ("NBFC & Lending", "FINNIFTY"),
    "RECLTD": ("NBFC & Lending", "FINNIFTY"), "IREDA": ("NBFC & Lending", "FINNIFTY"),
    "ICICIGI": ("Insurance", "FINNIFTY"), "ICICIPRULI": ("Insurance", "FINNIFTY"),
    "HDFCLIFE": ("Insurance", "FINNIFTY"), "SBILIFE": ("Insurance", "FINNIFTY"),
    "LICI": ("Insurance", "FINNIFTY"),
    "BSE": ("Capital Markets", "NIFTY CAPITAL MKT"), "MCX": ("Capital Markets", "NIFTY CAPITAL MKT"),
    "CDSL": ("Capital Markets", "NIFTY CAPITAL MKT"), "CAMS": ("Capital Markets", "NIFTY CAPITAL MKT"),
    "KFINTECH": ("Capital Markets", "NIFTY CAPITAL MKT"), "ANGELONE": ("Capital Markets", "NIFTY CAPITAL MKT"),
    "360ONE": ("Capital Markets", "NIFTY CAPITAL MKT"), "NAM-INDIA": ("Capital Markets", "NIFTY CAPITAL MKT"),
    "HDFCAMC": ("Capital Markets", "NIFTY CAPITAL MKT"), "IEX": ("Capital Markets", "NIFTY CAPITAL MKT"),
    "MOTILALOFS": ("Capital Markets", "NIFTY CAPITAL MKT"),
    "POLICYBZR": ("Fintech", "NIFTY 500"), "PAYTM": ("Fintech", "NIFTY 500"),
    "SAGILITY": ("Healthcare Services", "NIFTY HEALTHCARE"),
    # IT
    "TCS": ("IT", "NIFTY IT"), "INFY": ("IT", "NIFTY IT"), "WIPRO": ("IT", "NIFTY IT"),
    "HCLTECH": ("IT", "NIFTY IT"), "TECHM": ("IT", "NIFTY IT"), "LTM": ("IT", "NIFTY IT"),
    "MPHASIS": ("IT", "NIFTY IT"), "COFORGE": ("IT", "NIFTY IT"), "PERSISTENT": ("IT", "NIFTY IT"),
    "OFSS": ("IT", "NIFTY IT"), "TATAELXSI": ("IT", "NIFTY IT"), "KPITTECH": ("IT", "NIFTY IT"),
    # Auto & ancillaries
    "MARUTI": ("Auto", "NIFTY AUTO"), "M&M": ("Auto", "NIFTY AUTO"), "TMPV": ("Auto", "NIFTY AUTO"),
    "TVSMOTOR": ("Auto", "NIFTY AUTO"), "BAJAJ-AUTO": ("Auto", "NIFTY AUTO"),
    "HEROMOTOCO": ("Auto", "NIFTY AUTO"), "EICHERMOT": ("Auto", "NIFTY AUTO"),
    "HYUNDAI": ("Auto", "NIFTY AUTO"), "ATHERENERG": ("Auto", "NIFTY AUTO"),
    "FORCEMOT": ("Auto", "NIFTY AUTO"), "MOTHERSON": ("Auto Ancillary", "NIFTY AUTO"),
    "SONACOMS": ("Auto Ancillary", "NIFTY AUTO"), "UNOMINDA": ("Auto Ancillary", "NIFTY AUTO"),
    "BHARATFORG": ("Auto Ancillary", "NIFTY AUTO"), "BOSCHLTD": ("Auto Ancillary", "NIFTY AUTO"),
    "TIINDIA": ("Auto Ancillary", "NIFTY AUTO"),
    # Pharma / healthcare
    "SUNPHARMA": ("Pharma", "NIFTY PHARMA"), "CIPLA": ("Pharma", "NIFTY PHARMA"),
    "DRREDDY": ("Pharma", "NIFTY PHARMA"), "DIVISLAB": ("Pharma", "NIFTY PHARMA"),
    "LUPIN": ("Pharma", "NIFTY PHARMA"), "AUROPHARMA": ("Pharma", "NIFTY PHARMA"),
    "ZYDUSLIFE": ("Pharma", "NIFTY PHARMA"), "TORNTPHARM": ("Pharma", "NIFTY PHARMA"),
    "ALKEM": ("Pharma", "NIFTY PHARMA"), "GLENMARK": ("Pharma", "NIFTY PHARMA"),
    "LAURUSLABS": ("Pharma", "NIFTY PHARMA"), "MANKIND": ("Pharma", "NIFTY PHARMA"),
    "BIOCON": ("Pharma", "NIFTY PHARMA"),
    "APOLLOHOSP": ("Hospitals", "NIFTY HEALTHCARE"), "MAXHEALTH": ("Hospitals", "NIFTY HEALTHCARE"),
    "FORTIS": ("Hospitals", "NIFTY HEALTHCARE"),
    # Metals
    "TATASTEEL": ("Metals", "NIFTY METAL"), "JSWSTEEL": ("Metals", "NIFTY METAL"),
    "HINDALCO": ("Metals", "NIFTY METAL"), "VEDL": ("Metals", "NIFTY METAL"),
    "SAIL": ("Metals", "NIFTY METAL"), "JINDALSTEL": ("Metals", "NIFTY METAL"),
    "NATIONALUM": ("Metals", "NIFTY METAL"), "HINDZINC": ("Metals", "NIFTY METAL"),
    "NMDC": ("Metals", "NIFTY METAL"),
    # Oil, gas & coal
    "RELIANCE": ("Oil & Gas", "NIFTY OIL AND GAS"), "ONGC": ("Oil & Gas", "NIFTY OIL AND GAS"),
    "IOC": ("Oil & Gas", "NIFTY OIL AND GAS"), "BPCL": ("Oil & Gas", "NIFTY OIL AND GAS"),
    "HINDPETRO": ("Oil & Gas", "NIFTY OIL AND GAS"), "GAIL": ("Oil & Gas", "NIFTY OIL AND GAS"),
    "OIL": ("Oil & Gas", "NIFTY OIL AND GAS"), "PETRONET": ("Oil & Gas", "NIFTY OIL AND GAS"),
    "COALINDIA": ("Oil & Gas", "NIFTY ENERGY"),
    # Power & renewables
    "NTPC": ("Power", "NIFTY ENERGY"), "POWERGRID": ("Power", "NIFTY ENERGY"),
    "TATAPOWER": ("Power", "NIFTY ENERGY"), "ADANIPOWER": ("Power", "NIFTY ENERGY"),
    "JSWENERGY": ("Power", "NIFTY ENERGY"), "NHPC": ("Power", "NIFTY ENERGY"),
    "ADANIGREEN": ("Power", "NIFTY ENERGY"), "ADANIENSOL": ("Power", "NIFTY ENERGY"),
    "SUZLON": ("Power", "NIFTY ENERGY"), "INOXWIND": ("Power", "NIFTY ENERGY"),
    "PREMIERENE": ("Power", "NIFTY ENERGY"), "WAAREEENER": ("Power", "NIFTY ENERGY"),
    # Capital goods / defence / electricals
    "ABB": ("Capital Goods", "NIFTY INFRA"), "SIEMENS": ("Capital Goods", "NIFTY INFRA"),
    "CGPOWER": ("Capital Goods", "NIFTY INFRA"), "CUMMINSIND": ("Capital Goods", "NIFTY INFRA"),
    "POWERINDIA": ("Capital Goods", "NIFTY INFRA"), "GVT&D": ("Capital Goods", "NIFTY INFRA"),
    "BHEL": ("Capital Goods", "NIFTY INFRA"), "APLAPOLLO": ("Capital Goods", "NIFTY INFRA"),
    "PGEL": ("Capital Goods", "NIFTY INFRA"), "SOLARINDS": ("Capital Goods", "NIFTY INFRA"),
    "LT": ("Infrastructure", "NIFTY INFRA"), "NBCC": ("Infrastructure", "NIFTY INFRA"),
    "RVNL": ("Infrastructure", "NIFTY INFRA"), "ADANIPORTS": ("Infrastructure", "NIFTY INFRA"),
    "GMRAIRPORT": ("Infrastructure", "NIFTY INFRA"), "CONCOR": ("Logistics", "NIFTY INFRA"),
    "DELHIVERY": ("Logistics", "NIFTY INFRA"), "INDIGO": ("Aviation", "NIFTY IND TOURISM"),
    "HAL": ("Defence", "NIFTY IND DEFENCE"), "BEL": ("Defence", "NIFTY IND DEFENCE"),
    "BDL": ("Defence", "NIFTY IND DEFENCE"), "MAZDOCK": ("Defence", "NIFTY IND DEFENCE"),
    "COCHINSHIP": ("Defence", "NIFTY IND DEFENCE"),
    "POLYCAB": ("Electricals", "NIFTY CONSR DURBL"), "KEI": ("Electricals", "NIFTY CONSR DURBL"),
    "DIXON": ("Electricals", "NIFTY CONSR DURBL"), "AMBER": ("Electricals", "NIFTY CONSR DURBL"),
    "KAYNES": ("Electricals", "NIFTY CONSR DURBL"), "HAVELLS": ("Electricals", "NIFTY CONSR DURBL"),
    "VOLTAS": ("Electricals", "NIFTY CONSR DURBL"), "BLUESTARCO": ("Electricals", "NIFTY CONSR DURBL"),
    "CROMPTON": ("Electricals", "NIFTY CONSR DURBL"),
    # Cement
    "ULTRACEMCO": ("Cement", "NIFTY CEMENT"), "AMBUJACEM": ("Cement", "NIFTY CEMENT"),
    "SHREECEM": ("Cement", "NIFTY CEMENT"), "GRASIM": ("Cement", "NIFTY CEMENT"),
    # FMCG / consumer
    "HINDUNILVR": ("FMCG", "NIFTY FMCG"), "ITC": ("FMCG", "NIFTY FMCG"),
    "NESTLEIND": ("FMCG", "NIFTY FMCG"), "BRITANNIA": ("FMCG", "NIFTY FMCG"),
    "DABUR": ("FMCG", "NIFTY FMCG"), "MARICO": ("FMCG", "NIFTY FMCG"),
    "GODREJCP": ("FMCG", "NIFTY FMCG"), "COLPAL": ("FMCG", "NIFTY FMCG"),
    "TATACONSUM": ("FMCG", "NIFTY FMCG"), "PATANJALI": ("FMCG", "NIFTY FMCG"),
    "RADICO": ("FMCG", "NIFTY FMCG"), "UNITDSPR": ("FMCG", "NIFTY FMCG"),
    "GODFRYPHLP": ("FMCG", "NIFTY FMCG"), "VBL": ("FMCG", "NIFTY FMCG"),
    "JUBLFOOD": ("Consumer Services", "NIFTY 500"), "INDHOTEL": ("Consumer Services", "NIFTY IND TOURISM"),
    "DMART": ("Retail", "NIFTY 500"), "TRENT": ("Retail", "NIFTY 500"),
    "NYKAA": ("Retail", "NIFTY 500"), "VMM": ("Retail", "NIFTY 500"),
    "KALYANKJIL": ("Retail", "NIFTY 500"), "TITAN": ("Retail", "NIFTY 500"),
    "ETERNAL": ("Internet & Digital", "NIFTY 500"), "SWIGGY": ("Internet & Digital", "NIFTY 500"),
    "PAGEIND": ("Retail", "NIFTY 500"),
    # Realty / chemicals / telecom
    "DLF": ("Realty", "NIFTY REALTY"), "GODREJPROP": ("Realty", "NIFTY REALTY"),
    "OBEROIRLTY": ("Realty", "NIFTY REALTY"), "LODHA": ("Realty", "NIFTY REALTY"),
    "PRESTIGE": ("Realty", "NIFTY REALTY"), "PHOENIXLTD": ("Realty", "NIFTY REALTY"),
    "PIIND": ("Chemicals", "NIFTY CHEMICALS"), "SRF": ("Chemicals", "NIFTY CHEMICALS"),
    "UPL": ("Chemicals", "NIFTY CHEMICALS"),
    "BHARTIARTL": ("Telecom", "NIFTY 500"), "IDEA": ("Telecom", "NIFTY 500"),
    "INDUSTOWER": ("Telecom", "NIFTY 500"),
    "PIDILITIND": ("Chemicals", "NIFTY CHEMICALS"), "SUPREMEIND": ("Chemicals", "NIFTY 500"),
    "ASTRAL": ("Chemicals", "NIFTY 500"),
    "ASHOKLEY": ("Auto", "NIFTY AUTO"),
    "ASIANPAINT": ("Paints", "NIFTY 500"),
    "ADANIENT": ("Conglomerate", "NIFTY 500"),
    "NAUKRI": ("Internet & Digital", "NIFTY 500"),
}


def fetch_instrument_master(cache_dir: str, max_age_days: float = 3.0) -> str:
    """Download (and cache) the Upstox instrument master CSV.gz. Returns local path."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "instruments.csv.gz")
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age_days * 86400
    if not fresh:
        req = urllib.request.Request(INSTRUMENTS_URL, headers={"User-Agent": "fno-screener/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    return path


def build_universe(cache_dir: str, force: bool = False) -> dict:
    """Return {'stocks': [...], 'built_at': iso, 'count': n}."""
    cache_file = os.path.join(cache_dir, "universe.json")
    if os.path.exists(cache_file) and not force:
        age_h = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_h < 12:
            try:
                with open(cache_file) as f:
                    return json.load(f)
            except Exception:
                pass

    path = fetch_instrument_master(cache_dir)
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(io.StringIO(f.read())))

    cash, futs = {}, []
    for r in rows:
        ex = (r.get("exchange") or "").strip()
        if ex == "NSE_EQ" and (r.get("instrument_type") or "").strip() == "EQUITY":
            sym = (r.get("tradingsymbol") or "").strip()
            if sym:
                cash[sym] = r
        elif ex == "NSE_FO" and (r.get("instrument_type") or "").strip() == "FUTSTK":
            futs.append(r)

    today = datetime.now(IST).date()
    by_symbol: dict[str, list] = {}
    for r in futs:
        ts = (r.get("tradingsymbol") or "").strip()
        m = SYM_RE.match(ts)
        if not m:
            continue
        by_symbol.setdefault(m.group("sym"), []).append(r)

    stocks, skipped = [], []
    for sym, contracts in by_symbol.items():
        eq = cash.get(sym)
        if not eq:
            skipped.append(sym)
            continue
        # nearest contract that has not expired yet (rolls automatically on expiry day)
        live = []
        for c in contracts:
            try:
                exp = datetime.strptime((c.get("expiry") or "").strip(), "%Y-%m-%d").date()
            except Exception:
                continue
            live.append((exp, c))
        live.sort(key=lambda x: x[0])
        nxt = [x for x in live if x[0] > today] or live[-1:]
        cur = nxt[0]
        sector, sector_idx = SECTOR_OF.get(sym, ("Others", "NIFTY 500"))
        stocks.append({
            "symbol": sym,
            "name": (eq.get("name") or sym).strip(),
            "sector": sector,
            "sector_index": sector_idx,
            "equity_key": (eq.get("instrument_key") or "").strip(),
            "fut_key": (cur[1].get("instrument_key") or "").strip(),
            "fut_symbol": (cur[1].get("tradingsymbol") or "").strip(),
            "fut_expiry": cur[0].isoformat(),
            "fut_next_symbol": (nxt[1][1].get("tradingsymbol") or "").strip() if len(nxt) > 1 else "",
            "fut_next_expiry": nxt[1][0].isoformat() if len(nxt) > 1 else "",
            "lot_size": int(float(cur[1].get("lot_size") or 0)),
        })

    stocks.sort(key=lambda s: s["symbol"])
    out = {
        "stocks": stocks,
        "count": len(stocks),
        "index_keys": INDEX_KEYS,
        "built_at": datetime.now(IST).isoformat(timespec="seconds"),
        "unmatched": skipped,
    }
    with open(cache_file, "w") as f:
        json.dump(out, f)
    return out


if __name__ == "__main__":
    import sys
    cd = sys.argv[1] if len(sys.argv) > 1 else "cache"
    u = build_universe(cd, force=True)
    print(f"F&O underlyings: {u['count']}   unmatched: {u['unmatched']}")
    others = [s["symbol"] for s in u["stocks"] if s["sector"] == "Others"]
    print("sector 'Others' (needs mapping):", others)
    from collections import Counter
    print(Counter(s["sector"] for s in u["stocks"]).most_common())
    print("sample:", u["stocks"][0], "\n", u["stocks"][-1])
