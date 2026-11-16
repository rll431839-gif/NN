import sys, os, time, json, math, hmac, hashlib, base64, logging, argparse, warnings
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HALVINGS = [pd.Timestamp(d) for d in ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20", "2028-04-15"]]
PRIOR_W = np.array([0.28, 0.22, 0.32, 0.18], dtype=float)
UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "ADA-USD", "LINK-USD", "AVAX-USD", "DOGE-USD", "DOT-USD",
    "LTC-USD", "BCH-USD", "TRX-USD", "ATOM-USD", "ETC-USD",
]
STATE_FILE = Path("apex_portfolio_state.json")
CONFIG_FILE = Path("apex_config.json")
FEED_FILE = Path("apex_chat_feed.jsonl")

DEFAULT_CFG = {
    "active_universe": UNIVERSE.copy(),
    "vol_target": 0.60,
    "cvar_limit": 0.055,
    "hysteresis": 0.05,
    "order_usd": 5000.0,
    "fee_rate": 0.0010,
    "slippage": 0.0005,
    "tax_rate": 0.42,
    "tax_aware": True,
    "llm": {"provider": "", "model": "", "api_key": "", "base_url": "", "verified": False},
    "exchanges": {"binance_key": "", "binance_secret": "", "binance_verified": False,
                  "kraken_key": "", "kraken_secret": "", "kraken_verified": False},
}

def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if CONFIG_FILE.exists():
        try:
            u = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in u.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    if not cfg.get("active_universe"):
        cfg["active_universe"] = UNIVERSE.copy()
    return cfg

def save_cfg(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def log_feed(kind: str, msg: str, meta: dict | None = None):
    entry = {"ts": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "msg": msg, "meta": meta or {}}
    with open(FEED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def read_feed(limit: int = 15) -> list[dict]:
    if not FEED_FILE.exists():
        return []
    lines = [ln.strip() for ln in FEED_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out = []
    for ln in lines[-limit:]:
        try: out.append(json.loads(ln))
        except Exception: pass
    return out

def smart_sleep_q(seconds: int) -> bool:
    try:
        import msvcrt
        end_t = time.time() + max(1, seconds)
        while time.time() < end_t:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"q", b"Q", b"\x1b"):
                    return True
            time.sleep(0.15)
        return False
    except Exception:
        time.sleep(max(1, seconds))
        return False

FEAT_W = {
    "trend_z200": 1.2, "sma_ratio": 1.0, "roc_s": 0.5, "roc_m": 0.6, "roc_l": 0.5,
    "vol_w": 1.1, "atr_pct": 0.8, "vol_ratio": 0.6, "drawdown_ath": 1.1,
    "taker_imb": 0.7, "halving_sin": 0.6, "halving_cos": 0.6, "sp500_roc": 0.8,
    "corr_sp500": 0.5, "dxy_roc": 0.9, "tnx_chg": 0.7, "btc_roc": 0.8, "rel_btc": 0.7,
}

TF_CFG = {
    "1s":  {"sec": 1,       "fast": 50, "slow": 200, "rs": 20, "rm": 60, "rl": 120, "vw": 60, "h": 30, "reb": 15, "gap": 30, "min_h": 350, "notaus_b": 60, "bars": 4000, "src": "binance"},
    "1m":  {"sec": 60,      "fast": 50, "slow": 200, "rs": 20, "rm": 60, "rl": 120, "vw": 60, "h": 30, "reb": 15, "gap": 30, "min_h": 350, "notaus_b": 60, "bars": 4000, "src": "binance"},
    "5m":  {"sec": 300,     "fast": 50, "slow": 200, "rs": 20, "rm": 60, "rl": 120, "vw": 60, "h": 30, "reb": 15, "gap": 30, "min_h": 350, "notaus_b": 60, "bars": 4000, "src": "binance"},
    "15m": {"sec": 900,     "fast": 50, "slow": 200, "rs": 20, "rm": 60, "rl": 120, "vw": 60, "h": 30, "reb": 15, "gap": 30, "min_h": 350, "notaus_b": 60, "bars": 4000, "src": "binance"},
    "1h":  {"sec": 3600,    "fast": 50, "slow": 200, "rs": 24, "rm": 72, "rl": 168, "vw": 48, "h": 24, "reb": 12, "gap": 24, "min_h": 350, "notaus_b": 48, "bars": 4000, "src": "binance"},
    "1d":  {"sec": 86400,   "fast": 50, "slow": 200, "rs": 30, "rm": 90, "rl": 180, "vw": 60, "h": 90, "reb": 30, "gap": 60, "min_h": 350, "notaus_b": 60, "bars": 0,    "src": "yf"},
    "1wk": {"sec": 604800,  "fast": 10, "slow": 40,  "rs": 4,  "rm": 12, "rl": 26,  "vw": 12, "h": 12, "reb": 4,  "gap": 8,  "min_h": 80,  "notaus_b": 8,  "bars": 0,    "src": "yf"},
    "1mo": {"sec": 2592000, "fast": 3,  "slow": 10,  "rs": 1,  "rm": 3,  "rl": 6,   "vw": 12, "h": 3,  "reb": 1,  "gap": 3,  "min_h": 36,  "notaus_b": 3,  "bars": 0,    "src": "yf"},
}

def normalize_ticker(ticker: str) -> str:
    t = ticker.strip().upper()
    if any(ch in t for ch in ["-", "^", "=", "."]):
        return t
    return f"{t}-USD"

def is_crypto_ticker(ticker: str) -> bool:
    t = ticker.upper()
    return t.endswith(("-USD", "USDT", "-EUR")) or t.startswith(("BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "LINK", "AVAX", "DOGE", "TRX"))

def to_binance_symbol(ticker: str) -> str:
    t = ticker.upper().replace("/", "")
    if t.endswith("-USD"): return t[:-4] + "USDT"
    if t.endswith("-EUR"): return t[:-4] + "EUR"
    return t

def to_okx_swap(ticker: str) -> str:
    return f"{ticker.upper().split('-')[0].replace('USDT', '')}-USDT-SWAP"

def to_kraken_pair(ticker: str) -> str:
    base = ticker.upper().split("-")[0]
    return ("XBT" if base == "BTC" else base) + "USD"

def days_since_halving(idx: pd.DatetimeIndex) -> pd.Series:
    return pd.Series([(d - max([h for h in HALVINGS if h <= d], default=d)).days for d in idx], index=idx, dtype=float)

def estimate_bars_per_year(idx: pd.DatetimeIndex, bar_sec: int) -> float:
    if len(idx) < 20:
        return (365.25 * 86400.0) / max(bar_sec, 1)
    span_yr = (idx[-1] - idx[0]).total_seconds() / (365.25 * 86400.0)
    if span_yr <= 1e-6:
        return (365.25 * 86400.0) / max(bar_sec, 1)
    return float(max(1.0, (len(idx) - 1) / span_yr))

def http_json(url: str, timeout: float = 5.0) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def http_post_json(url: str, body: dict, headers: dict, timeout: float = 15.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": "APEX-QUANT/9.0", **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"error": raw[:300]}
    except Exception as e:
        return 0, {"error": str(e)}

# ======================================================================================
# ECHTE KI/LLM-API & BOERSEN-API VALIDIERUNG + CLIENT
# ======================================================================================
def call_llm(provider: str, model: str, api_key: str, messages: list[dict], base_url: str = "") -> tuple[bool, str, float]:
    t0 = time.time()
    prov = (provider or "").strip().lower()
    if not prov:
        return False, "Kein KI-Provider konfiguriert.", 0.0
    if prov in ("openai", "openrouter"):
        url = base_url.strip() or ("https://api.openai.com/v1/chat/completions" if prov == "openai" else "https://openrouter.ai/api/v1/chat/completions")
        if not api_key: return False, f"{prov}: API-Key fehlt.", 0.0
        st, res = http_post_json(url, {"model": model or ("gpt-4o-mini" if prov == "openai" else "openai/gpt-4o-mini"),
                                       "messages": messages, "temperature": 0.2, "max_tokens": 600},
                                 {"Authorization": f"Bearer {api_key}"})
        ms = (time.time() - t0) * 1000.0
        if st == 200 and res.get("choices"):
            return True, res["choices"][0]["message"]["content"].strip(), ms
        return False, f"HTTP {st}: {res.get('error', res)}", ms
    if prov == "gemini":
        if not api_key: return False, "Gemini: API-Key fehlt.", 0.0
        m = model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={urllib.parse.quote(api_key)}"
        sys_txt = "\n".join(x["content"] for x in messages if x["role"] == "system")
        usr_txt = "\n".join(f"{x['role'].upper()}: {x['content']}" for x in messages if x["role"] != "system")
        payload = {"contents": [{"parts": [{"text": (sys_txt + "\n\n" + usr_txt).strip()}]}]}
        st, res = http_post_json(url, payload, {})
        ms = (time.time() - t0) * 1000.0
        if st == 200 and res.get("candidates"):
            parts = res["candidates"][0].get("content", {}).get("parts", [])
            txt = "".join(p.get("text", "") for p in parts).strip()
            return True, txt, ms
        return False, f"HTTP {st}: {res.get('error', res)}", ms
    if prov == "anthropic":
        if not api_key: return False, "Anthropic: API-Key fehlt.", 0.0
        sys_txt = "\n".join(x["content"] for x in messages if x["role"] == "system")
        msgs = [x for x in messages if x["role"] != "system"]
        st, res = http_post_json("https://api.anthropic.com/v1/messages",
                                 {"model": model or "claude-3-5-sonnet-latest", "max_tokens": 600, "system": sys_txt, "messages": msgs},
                                 {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        ms = (time.time() - t0) * 1000.0
        if st == 200 and res.get("content"):
            return True, "".join(c.get("text", "") for c in res["content"]).strip(), ms
        return False, f"HTTP {st}: {res.get('error', res)}", ms
    if prov == "ollama":
        url = (base_url.strip().rstrip("/") or "http://localhost:11434") + "/api/chat"
        st, res = http_post_json(url, {"model": model or "llama3.1", "messages": messages, "stream": False}, {})
        ms = (time.time() - t0) * 1000.0
        if st == 200 and isinstance(res.get("message"), dict):
            return True, res["message"].get("content", "").strip(), ms
        return False, f"HTTP {st}: {res.get('error', res)}", ms
    return False, f"Unbekannter Provider: {provider}", 0.0

def verify_llm_connection(cfg: dict) -> tuple[bool, str]:
    lm = cfg.get("llm", {})
    ok, txt, ms = call_llm(lm.get("provider", ""), lm.get("model", ""), lm.get("api_key", ""),
                           [{"role": "user", "content": "Antworte exakt mit: OK"}], lm.get("base_url", ""))
    lm["verified"] = bool(ok)
    save_cfg(cfg)
    if ok:
        return True, f"KI-API gueltig! Provider={lm['provider']} | Modell={lm['model']} | Latenz={ms:.0f}ms | Antwort='{txt[:30]}'"
    return False, f"KI-API ungueltig: {txt}"

def get_exchange_creds(ex_name: str, cfg: dict) -> tuple[str, str]:
    ex = cfg.get("exchanges", {})
    if ex_name == "binance":
        return ex.get("binance_key") or os.environ.get("BINANCE_API_KEY", ""), ex.get("binance_secret") or os.environ.get("BINANCE_API_SECRET", "")
    return ex.get("kraken_key") or os.environ.get("KRAKEN_API_KEY", ""), ex.get("kraken_secret") or os.environ.get("KRAKEN_API_SECRET", "")

def verify_exchange_connection(ex_name: str, cfg: dict) -> tuple[bool, str]:
    key, sec = get_exchange_creds(ex_name, cfg)
    if not key or not sec:
        cfg["exchanges"][f"{ex_name}_verified"] = False
        save_cfg(cfg)
        return False, f"{ex_name.upper()}: Kein API-Key/Secret hinterlegt."
    try:
        if ex_name == "binance":
            qs = urllib.parse.urlencode({"timestamp": int(time.time() * 1000)})
            sig = hmac.new(sec.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
            req = urllib.request.Request(f"https://api.binance.com/api/v3/account?{qs}&signature={sig}", headers={"X-MBX-APIKEY": key})
            with urllib.request.urlopen(req, timeout=8.0) as r:
                res = json.loads(r.read().decode("utf-8"))
            bals = [f"{b['asset']}={float(b['free']):.4f}" for b in res.get("balances", []) if float(b.get("free", 0)) > 0.0001][:5]
            cfg["exchanges"]["binance_verified"] = bool(res.get("canTrade", True))
            save_cfg(cfg)
            return True, f"BINANCE gueltig! canTrade={res.get('canTrade')} | Bestaende: {', '.join(bals) if bals else '0.00'}"
        if ex_name == "kraken":
            path = "/0/private/Balance"
            nonce = str(int(time.time() * 1000))
            pd_s = urllib.parse.urlencode({"nonce": nonce})
            msg = path.encode("utf-8") + hashlib.sha256((nonce + pd_s).encode("utf-8")).digest()
            sig = base64.b64encode(hmac.new(base64.b64decode(sec), msg, hashlib.sha512).digest()).decode("utf-8")
            req = urllib.request.Request("https://api.kraken.com" + path, data=pd_s.encode("utf-8"),
                                         headers={"API-Key": key, "API-Sign": sig})
            with urllib.request.urlopen(req, timeout=8.0) as r:
                res = json.loads(r.read().decode("utf-8"))
            if res.get("error"):
                cfg["exchanges"]["kraken_verified"] = False; save_cfg(cfg)
                return False, f"KRAKEN Fehler: {res['error']}"
            bals = [f"{k}={float(v):.4f}" for k, v in res.get("result", {}).items() if float(v) > 0.0001][:5]
            cfg["exchanges"]["kraken_verified"] = True; save_cfg(cfg)
            return True, f"KRAKEN gueltig! Bestaende: {', '.join(bals) if bals else '0.00'}"
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        cfg["exchanges"][f"{ex_name}_verified"] = False; save_cfg(cfg)
        return False, f"{ex_name.upper()} HTTP {e.code}: {raw[:180]}"
    except Exception as e:
        cfg["exchanges"][f"{ex_name}_verified"] = False; save_cfg(cfg)
        return False, f"{ex_name.upper()} Fehler: {e}"
    return False, "Unbekannte Boerse."

def simulate_l2_fill(levels: list, notional_usd: float, mid: float) -> tuple[float, float, float]:
    if not levels or mid <= 0:
        return mid, 999.0, 0.0
    tot_usd = sum(float(px) * float(q) for px, q in levels)
    if notional_usd <= 0:
        return float(levels[0][0]), abs(float(levels[0][0]) / mid - 1.0) * 10000.0, tot_usd
    rem, filled_qty, cost_usd = notional_usd, 0.0, 0.0
    for px_s, q_s in levels:
        px, q = float(px_s), float(q_s)
        take_usd = min(rem, px * q)
        filled_qty += take_usd / px
        cost_usd += take_usd
        rem -= take_usd
        if rem <= 1e-6:
            break
    if filled_qty <= 0 or rem > notional_usd * 0.1:
        return float(levels[-1][0]), 500.0, tot_usd
    vwap = cost_usd / filled_qty
    return vwap, abs(vwap / mid - 1.0) * 10000.0, tot_usd

def fetch_live_intel(ticker: str, order_usd: float = 5000.0) -> dict:
    sym = to_binance_symbol(ticker)
    out = {"ticker": ticker, "mid": np.nan, "spread_bps": np.nan, "obi": 0.0,
           "buy_vwap": np.nan, "sell_vwap": np.nan, "buy_slip_bps": 5.0, "sell_slip_bps": 5.0,
           "bid_depth_usd": 0.0, "ask_depth_usd": 0.0, "vol_24h_m": np.nan,
           "funding_8h": np.nan, "dvol": np.nan, "l2_ok": False}
    depth = http_json(f"https://data-api.binance.vision/api/v3/depth?symbol={sym}&limit=50", timeout=4.0)
    if isinstance(depth, dict) and depth.get("bids") and depth.get("asks"):
        bids, asks = depth["bids"], depth["asks"]
        bb, ba = float(bids[0][0]), float(asks[0][0])
        mid = 0.5 * (bb + ba)
        spread_bps = ((ba - bb) / mid) * 10000.0 if mid > 0 else 999.0
        bv20 = sum(float(p) * float(q) for p, q in bids[:20])
        av20 = sum(float(p) * float(q) for p, q in asks[:20])
        obi = (bv20 - av20) / max(bv20 + av20, 1e-9)
        b_vwap, b_slip, a_dep = simulate_l2_fill(asks, order_usd, mid)
        s_vwap, s_slip, b_dep = simulate_l2_fill(bids, order_usd, mid)
        out.update({"mid": mid, "spread_bps": spread_bps, "obi": obi,
                    "buy_vwap": b_vwap, "sell_vwap": s_vwap,
                    "buy_slip_bps": b_slip, "sell_slip_bps": s_slip,
                    "bid_depth_usd": b_dep, "ask_depth_usd": a_dep, "l2_ok": True})
    t24 = http_json(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}", timeout=3.0)
    if isinstance(t24, dict) and "quoteVolume" in t24:
        out["vol_24h_m"] = float(t24["quoteVolume"]) / 1e6
    fr = http_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={to_okx_swap(ticker)}", timeout=3.0)
    if isinstance(fr, dict) and fr.get("code") == "0" and fr.get("data"):
        out["funding_8h"] = float(fr["data"][0]["fundingRate"])
    base = ticker.upper().split("-")[0]
    if base in ("BTC", "ETH"):
        dv = http_json(f"https://www.deribit.com/api/v2/public/get_index_price?index_name={base.lower()}dvol_usdc", timeout=3.0)
        if isinstance(dv, dict) and isinstance(dv.get("result"), dict):
            out["dvol"] = float(dv["result"].get("index_price", np.nan))
    return out

def fetch_binance_klines(ticker: str, tf: str = "1m", target_bars: int = 4000) -> pd.DataFrame:
    sym = to_binance_symbol(ticker)
    interval = "1w" if tf == "1wk" else ("1M" if tf == "1mo" else tf)
    rows, end_ms = [], None
    for _ in range(int(np.ceil(target_bars / 1000.0)) + 1):
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={interval}&limit=1000"
        if end_ms is not None:
            url += f"&endTime={end_ms}"
        batch = http_json(url, timeout=8.0)
        if not batch or isinstance(batch, dict):
            break
        rows = batch + rows
        end_ms = int(batch[0][0]) - 1
        if len(rows) >= target_bars or len(batch) < 1000:
            break
    if not rows:
        raise ValueError(f"Keine Marktdaten fuer {ticker} ({sym}, {tf}) verfuegbar.")
    rows = rows[-target_bars:]
    df = pd.DataFrame({
        "Open": [float(x[1]) for x in rows], "High": [float(x[2]) for x in rows],
        "Low": [float(x[3]) for x in rows], "Close": [float(x[4]) for x in rows],
        "Volume": [float(x[5]) for x in rows], "TakerBuyVol": [float(x[9]) for x in rows],
    }, index=pd.to_datetime([int(x[0]) for x in rows], unit="ms"))
    return df[~df.index.duplicated(keep="last")].sort_index()

def fetch_ohlcv(ticker: str, tf: str = "1d", start: str = "2014-09-17", target_bars: int = 0) -> pd.DataFrame:
    cfg = TF_CFG[tf]
    if cfg["src"] == "binance" and is_crypto_ticker(ticker):
        return fetch_binance_klines(ticker, tf=tf, target_bars=target_bars or cfg["bars"])
    import yfinance as yf
    eff_start = "2014-09-17" if is_crypto_ticker(ticker) else start
    try:
        raw = yf.download(ticker, start=eff_start, interval=tf, progress=False, auto_adjust=True)
    except Exception:
        raw = None
    if (raw is None or raw.empty) and is_crypto_ticker(ticker):
        return fetch_binance_klines(ticker, tf=tf, target_bars=2500 if tf == "1d" else 600)
    if raw is None or raw.empty:
        raise ValueError(f"Keine Daten fuer {ticker} ({tf}) gefunden.")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.loc[:, ~raw.columns.duplicated()]
    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
    df = raw[cols].dropna(subset=["Close"]).copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    if tf == "1mo" and len(df) < 60 and is_crypto_ticker(ticker):
        try:
            df_b = fetch_binance_klines(ticker, tf="1mo", target_bars=200)
            if len(df_b) > len(df): return df_b
        except Exception:
            pass
    return df[~df.index.duplicated(keep="last")].sort_index()

def fetch_universe_daily(tickers: list[str], start: str = "2020-01-01") -> dict[str, pd.DataFrame]:
    import yfinance as yf
    out = {}
    try:
        raw = yf.download(tickers, start=start, progress=False, auto_adjust=True, group_by="ticker", threads=True)
        if raw is not None and not raw.empty and isinstance(raw.columns, pd.MultiIndex):
            lv0 = set(raw.columns.get_level_values(0))
            for t in tickers:
                if t in lv0:
                    sub = raw[t].dropna(subset=["Close"]).copy()
                    if len(sub) >= 260:
                        sub.index = pd.to_datetime(sub.index).tz_localize(None)
                        out[t] = sub[~sub.index.duplicated(keep="last")].sort_index()
    except Exception:
        pass
    for t in tickers:
        if t not in out:
            try: out[t] = fetch_binance_klines(t, tf="1d", target_bars=2200)
            except Exception: pass
    return out

def fetch_fng() -> pd.Series | None:
    res = http_json("https://api.alternative.me/fng/?limit=0&format=json", timeout=4.0)
    if not isinstance(res, dict) or not res.get("data"):
        return None
    rows = res["data"]
    s = pd.Series([float(x["value"]) for x in rows],
                  index=pd.to_datetime([int(x["timestamp"]) for x in rows], unit="s").normalize()).sort_index()
    return s[~s.index.duplicated(keep="last")]

def load_macro_bundle(start: str = "2014-09-17") -> tuple[dict[str, pd.Series], pd.Series | None]:
    bundle = {}
    for name, sym in {"SP500": "^GSPC", "DXY": "DX-Y.NYB", "TNX": "^TNX", "BTC": "BTC-USD"}.items():
        try: bundle[name] = fetch_ohlcv(sym, tf="1d", start=start)["Close"]
        except Exception: pass
    return bundle, fetch_fng()

def build_asset_df(ticker: str, tf: str, bundle: dict[str, pd.Series] | None = None, start: str = "2014-09-17") -> tuple[pd.DataFrame, list[str]]:
    df = fetch_ohlcv(ticker, tf=tf, start=start)
    if TF_CFG[tf]["sec"] < 86400 or not bundle:
        return df, ["Binance Public Klines" if TF_CFG[tf]["src"] == "binance" else "OHLCV"]
    is_btc, is_sp, loaded = ticker.upper().startswith("BTC"), ticker.upper() in ("^GSPC", "SPY"), []
    for name, s in bundle.items():
        if (name == "BTC" and is_btc) or (name == "SP500" and is_sp):
            continue
        aligned = s.reindex(df.index).ffill()
        if aligned.notna().mean() >= 0.85:
            df[name] = aligned; loaded.append(name)
    return df.dropna(subset=["Close"] + loaded), loaded

def validate_market_data(df: pd.DataFrame, ticker: str, tf: str = "1d", check_staleness: bool = True) -> list[str]:
    cfg = TF_CFG[tf]
    min_bars = cfg["slow"] + cfg["min_h"] + cfg["h"] + 10
    if df is None or df.empty or len(df) < min_bars:
        return [f"Zu wenig Daten fuer {ticker} ({tf}: {0 if df is None else len(df)} < {min_bars} Bars)."]
    errs, c = [], df["Close"]
    if (c <= 0).any() or c.iloc[-min(30, len(c)):].isna().any():
        errs.append("Ungueltige Preise (<= 0 oder NaN in letzten Bars).")
    tail = c.iloc[-min(len(c), max(200, int(365 * 86400 / max(cfg["sec"], 1)))):].pct_change().dropna()
    hi_lim = 2.5 if cfg["sec"] >= 604800 else (1.50 if cfg["sec"] >= 86400 else 0.50)
    lo_lim = -0.80 if cfg["sec"] >= 604800 else (-0.70 if cfg["sec"] >= 86400 else -0.45)
    bad = tail[(tail > hi_lim) | (tail < lo_lim)]
    if not bad.empty:
        errs.append(f"Extremer Bar-Glitch am {bad.index[0]} ({bad.iloc[0]:+.1%}).")
    if check_staleness:
        now = pd.Timestamp.utcnow().tz_localize(None) if cfg["src"] == "binance" else pd.Timestamp.now()
        age_sec = (now - df.index[-1]).total_seconds()
        max_age = max(3600, cfg["sec"] * 30) if cfg["src"] == "binance" else max(35 * 86400 if tf == "1mo" else 10 * 86400 if tf == "1wk" else 5 * 86400, cfg["sec"] * 3)
        if age_sec > max_age:
            errs.append(f"Daten veraltet ({tf}): Letzte Kerze {df.index[-1]} ({age_sec/3600:.1f}h alt).")
    return errs

class QuantCore:
    @staticmethod
    def yang_zhang_vol(df: pd.DataFrame, window: int, bars_per_year: float) -> pd.Series:
        c = df["Close"]
        lr = np.log(c / c.shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        cc_vol = lr.rolling(window).std() * np.sqrt(bars_per_year)
        if not all(k in df.columns for k in ("Open", "High", "Low")):
            return cc_vol.bfill().fillna(0.40)
        o, h, l = df["Open"], df["High"], df["Low"]
        valid = (o > 0) & (h > 0) & (l > 0) & (c > 0) & (o.shift(1) > 0) & (c.shift(1) > 0)
        if valid.mean() < 0.80:
            return cc_vol.bfill().fillna(0.40)
        lo = np.where(valid, np.log(np.maximum(o, 1e-12) / np.maximum(c.shift(1), 1e-12)), 0.0)
        lc = np.where(valid, np.log(np.maximum(c, 1e-12) / np.maximum(o, 1e-12)), 0.0)
        lh = np.where(valid, np.log(np.maximum(h, 1e-12) / np.maximum(o, 1e-12)), 0.0)
        ll = np.where(valid, np.log(np.maximum(l, 1e-12) / np.maximum(o, 1e-12)), 0.0)
        rs = pd.Series(lh * (lh - lc) + ll * (ll - lc), index=df.index)
        s_o = pd.Series(lo, index=df.index).rolling(window).var()
        s_c = pd.Series(lc, index=df.index).rolling(window).var()
        k = 0.34 / (1.34 + (window + 1.0) / max(window - 1.0, 1.0))
        yz = np.sqrt(np.maximum(s_o + k * s_c + (1.0 - k) * rs.rolling(window).mean(), 1e-12) * bars_per_year)
        return yz.where(yz > 1e-6, cc_vol).bfill().fillna(0.40)

    @staticmethod
    def ewma_vol(close: pd.Series, span: int, bars_per_year: float) -> pd.Series:
        lr = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return (lr.ewm(span=span, adjust=False).std() * np.sqrt(bars_per_year)).bfill().fillna(0.40)

    @staticmethod
    def bayesian_regime_filter(close: pd.Series, vol_ann: pd.Series, bars_per_year: float) -> pd.DataFrame:
        lr = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        v_bar = np.maximum(vol_ann.values / np.sqrt(bars_per_year), 1e-5)
        probs = np.zeros((len(lr), 3), dtype=float)
        p = np.array([0.40, 0.40, 0.20], dtype=float)
        A = np.array([[0.96, 0.03, 0.01], [0.02, 0.95, 0.03], [0.01, 0.04, 0.95]], dtype=float)
        mu_m, sig_m = np.array([0.08, 0.0, -0.10]), np.array([0.85, 1.05, 1.75])
        for t in range(len(lr)):
            p_pred = p @ A
            s, m = sig_m * v_bar[t], mu_m * v_bar[t]
            post = p_pred * np.maximum(np.exp(-0.5 * ((lr[t] - m) / s) ** 2) / np.maximum(s, 1e-9), 1e-18)
            p = (post / post.sum()) if post.sum() > 0 else np.array([0.34, 0.33, 0.33])
            probs[t] = p
        return pd.DataFrame(probs, index=close.index, columns=["p_bull", "p_chop", "p_bear"])

    @staticmethod
    def ledoit_wolf_cov(ret_df: pd.DataFrame) -> np.ndarray:
        R = ret_df.dropna(how="any").values
        if len(R) < 10:
            return np.eye(ret_df.shape[1]) * 0.001
        T, N = R.shape
        X = R - R.mean(axis=0, keepdims=True)
        S = (X.T @ X) / T
        if N == 1: return np.maximum(S, 1e-8)
        F = np.diag(np.diag(S))
        X2 = X ** 2
        phi_mat = (X2.T @ X2) / T - (S ** 2)
        phi_off = float(np.sum(phi_mat) - np.sum(np.diag(phi_mat)))
        delta = float(np.clip((phi_off / max(float(np.sum((S - F) ** 2)), 1e-12)) / T, 0.05, 1.0))
        sig = delta * F + (1.0 - delta) * S
        idx = np.arange(N)
        sig[idx, idx] = np.maximum(sig[idx, idx], 1e-8)
        return sig

    @staticmethod
    def optimize_portfolio(mu: np.ndarray, cov: np.ndarray, max_w: float | np.ndarray = 0.40,
                           max_gross: float = 1.0, risk_aversion: float = 2.0,
                           cvar_limit: float = 0.055) -> np.ndarray:
        N = len(mu)
        if N == 0 or max_gross <= 1e-6: return np.zeros(N)
        vols = np.sqrt(np.maximum(np.diag(cov), 1e-8))
        w = (np.maximum(mu, 0.0) + 0.15) / vols
        if w.sum() <= 1e-9: return np.zeros(N)
        w = np.minimum((w / w.sum()) * max_gross, max_w)
        mu_d = mu * 0.0025
        for _ in range(45):
            w = np.clip(w + 0.20 * (mu_d - risk_aversion * (cov @ w)), 0.0, max_w)
            if w.sum() > max_gross: w = w * (max_gross / w.sum())
        est_cvar = 2.063 * float(np.sqrt(max(w @ cov @ w, 0.0)))
        if est_cvar > cvar_limit and est_cvar > 1e-9:
            w = w * (cvar_limit / est_cvar)
        return np.round(w, 4)

def perf_metrics(eq: pd.Series, bars_per_year: float) -> tuple[float, float, float, float]:
    tr = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    dd = float(((eq / eq.cummax()) - 1.0).min())
    dr = eq.pct_change().dropna()
    sh = float((dr.mean() / dr.std()) * np.sqrt(bars_per_year)) if dr.std() > 1e-9 else 0.0
    return tr, dd, sh, (tr / abs(dd)) if dd < -1e-6 else 0.0

def spearman_ic(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if len(x) < 10: return 0.0, 1.0
    rx, ry = pd.Series(x).rank().values, pd.Series(y).rank().values
    if np.std(rx) < 1e-9 or np.std(ry) < 1e-9: return 0.0, 1.0
    ic = float(np.corrcoef(rx, ry)[0, 1])
    t = abs(ic) * np.sqrt((len(x) - 2) / max(1.0 - ic ** 2, 1e-12))
    return ic, float(math.erfc(t / np.sqrt(2.0)))

class SelfAdaptingCryptoAgent:
    def __init__(self, df: pd.DataFrame, ticker: str = "BTC-USD", tf: str = "1d", fng: pd.Series = None):
        self.df = df.sort_index().copy()
        self.ticker, self.tf, self.fng = ticker, tf, fng
        self.cfg = TF_CFG[tf]
        self.horizon, self.bar_sec = self.cfg["h"], self.cfg["sec"]
        self.bars_per_year = estimate_bars_per_year(self.df.index, self.bar_sec)
        self.is_crypto, self.is_btc = is_crypto_ticker(ticker), ticker.upper().startswith("BTC")
        c = self.df["Close"]
        self.sma_fast, self.sma_slow = c.rolling(self.cfg["fast"]).mean(), c.rolling(self.cfg["slow"]).mean()
        self.yz_vol = QuantCore.yang_zhang_vol(self.df, self.cfg["vw"], self.bars_per_year)
        self.ewma_fast = QuantCore.ewma_vol(c, max(5, self.cfg["rs"] // 2), self.bars_per_year)
        self.bayes = QuantCore.bayesian_regime_filter(c, self.yz_vol, self.bars_per_year)
        self.features = self._build_features()
        cw = self.cfg["vw"]
        self.er = ((c - c.shift(cw)).abs() / c.diff().abs().rolling(cw).sum().replace(0, np.nan)).clip(0.0, 1.0).reindex(self.features.index).fillna(0.30)
        self.sma_slow_slope = self.sma_slow.pct_change(self.cfg["rs"]).reindex(self.features.index).fillna(0.0)
        self.fwd_ret_s = c.shift(-self.cfg["rs"]) / c - 1.0
        self.fwd_ret_h = c.shift(-self.horizon) / c - 1.0
        self.fwd_ret_l = c.shift(-self.cfg["rl"]) / c - 1.0
        idxr = pd.api.indexers.FixedForwardWindowIndexer(window_size=self.horizon)
        self.fwd_max_dd = np.minimum(0.0, (c.shift(-1).rolling(window=idxr).min() / c) - 1.0)
        self.alpha_cache = self._precompute_alphas()
        self.analog_cache, self.cal_history = {}, []

    def _build_features(self) -> pd.DataFrame:
        c, cfg = self.df["Close"], self.cfg
        f = pd.DataFrame(index=self.df.index)
        f["trend_z200"] = (c - self.sma_slow) / c.rolling(cfg["slow"]).std().replace(0, np.nan)
        f["sma_ratio"] = self.sma_fast / self.sma_slow
        f["roc_s"], f["roc_m"], f["roc_l"] = c.pct_change(cfg["rs"]), c.pct_change(cfg["rm"]), c.pct_change(cfg["rl"])
        f["vol_w"] = self.yz_vol
        f["vol_shock"] = (self.ewma_fast / np.maximum(self.yz_vol, 1e-4)).clip(0.3, 3.0)
        f["p_bull"], f["p_bear"] = self.bayes["p_bull"], self.bayes["p_bear"]
        if "High" in self.df.columns and "Low" in self.df.columns:
            h, l, pc = self.df["High"], self.df["Low"], c.shift(1)
            tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
            f["atr_pct"] = (tr.rolling(cfg["rs"]).mean() / c).fillna(0.0)
        else:
            f["atr_pct"] = f["vol_w"] / np.sqrt(self.bars_per_year)
        if "Volume" in self.df.columns:
            v = self.df["Volume"].replace(0, np.nan)
            f["vol_ratio"] = (v.rolling(cfg["rs"]).mean() / v.rolling(cfg["rl"]).mean().replace(0, np.nan)).fillna(1.0)
        else:
            f["vol_ratio"] = 1.0
        if "TakerBuyVol" in self.df.columns and "Volume" in self.df.columns:
            tb, tv = self.df["TakerBuyVol"].rolling(cfg["rs"]).sum(), self.df["Volume"].rolling(cfg["rs"]).sum()
            f["taker_imb"] = pd.Series(np.where(tv > 0, (tb / tv - 0.5) * 2.0, 0.0), index=self.df.index)
        f["drawdown_ath"] = (c / c.cummax()) - 1.0
        if self.is_crypto and 86400 <= self.bar_sec <= 7 * 86400:
            cd = days_since_halving(self.df.index)
            f["halving_sin"], f["halving_cos"] = np.sin(2.0 * np.pi * cd / 1460.0), np.cos(2.0 * np.pi * cd / 1460.0)
        if self.bar_sec >= 86400:
            if "SP500" in self.df.columns:
                sp = self.df["SP500"]
                f["sp500_roc"] = sp.pct_change(cfg["rm"])
                f["corr_sp500"] = c.pct_change().rolling(cfg["rm"]).corr(sp.pct_change()).fillna(0.0)
            if "DXY" in self.df.columns: f["dxy_roc"] = self.df["DXY"].pct_change(cfg["rm"])
            if "TNX" in self.df.columns: f["tnx_chg"] = self.df["TNX"] - self.df["TNX"].shift(cfg["rm"])
            if not self.is_btc and "BTC" in self.df.columns:
                btc = self.df["BTC"]
                f["btc_roc"], f["rel_btc"] = btc.pct_change(cfg["rm"]), (c / btc).pct_change(cfg["rm"])
        return f.replace([np.inf, -np.inf], np.nan).dropna()

    def _precompute_alphas(self) -> pd.DataFrame:
        f, min_p = self.features, max(40, min(200, len(self.features) // 3))
        out = pd.DataFrame(index=f.index)
        s1 = np.where(f["sma_ratio"] > 1.0, 0.30, -0.30) + np.where(f["roc_m"] > 0, 0.28, -0.28)
        s1 += np.where(f["trend_z200"] > 0, 0.22, -0.22) + 0.20 * (f["p_bull"] - f["p_bear"])
        out["a_trend"] = np.clip(s1, -1.0, 1.0)
        z_pct = f["trend_z200"].expanding(min_periods=min_p).rank(pct=True).fillna(0.5)
        v_pct = f["vol_w"].expanding(min_periods=min_p).rank(pct=True).fillna(0.5)
        out["a_val"] = np.where(z_pct <= 0.15, np.where(f["roc_s"] <= -0.10, 0.25, 1.0),
                       np.where(z_pct <= 0.65, 0.48, np.where(z_pct <= 0.85, -0.25, -1.0)))
        out["z200_pct"], out["vol_pct"] = z_pct, v_pct
        s4 = np.zeros(len(f))
        if "sp500_roc" in f.columns: s4 += np.where(f["sp500_roc"] > 0, 0.25, -0.25)
        if "dxy_roc" in f.columns: s4 += np.where(f["dxy_roc"] > 0.02, -0.25, np.where(f["dxy_roc"] < -0.02, 0.20, 0.0))
        if "tnx_chg" in f.columns: s4 += np.where(f["tnx_chg"] > 0.35, -0.20, np.where(f["tnx_chg"] < -0.25, 0.15, 0.0))
        if "taker_imb" in f.columns: s4 += np.clip(f["taker_imb"].values * 1.5, -0.45, 0.45)
        s4 += np.where(f["vol_shock"] > 1.35, -0.25, 0.0) + np.where(v_pct > 0.85, -0.25, np.where(v_pct < 0.45, 0.20, 0.0))
        out["a_macro"] = np.clip(s4, -1.0, 1.0)
        return out

    def _analog_match(self, as_of: pd.Timestamp, top_k: int = 12, fast: bool = False) -> dict:
        if as_of in self.analog_cache and fast:
            return self.analog_cache[as_of]
        hist = self.features.loc[:as_of]
        if len(hist) <= self.horizon + 20:
            return {"a_analog": 0.0, "w_win_s": 0.5, "w_exp_s": 0.0, "w_win_h": 0.5, "w_exp_h": 0.0,
                    "w_exp_l": np.nan, "w_exp_dd": 0.0, "avg_sim": 0.0, "adf": pd.DataFrame(), "q25_h": 0.0, "worst_h": 0.0}
        pool = hist.iloc[:-self.horizon]
        med = hist.median()
        mad = (hist - med).abs().median() * 1.4826
        scale = mad.where(mad > 1e-6, hist.std()).replace(0, 1.0)
        wv = np.array([FEAT_W.get(c, 1.0) for c in hist.columns], dtype=float)
        xp = ((pool - med) / scale).clip(-3.5, 3.5).values * wv
        xn = ((hist.iloc[[-1]] - med) / scale).clip(-3.5, 3.5).values * wv
        euc = np.linalg.norm(xp - xn, axis=1)
        np_prod = np.linalg.norm(xp, axis=1) * np.linalg.norm(xn)
        cos = np.where(np_prod > 1e-9, (xp @ xn.T).ravel() / np_prod, 0.0)
        h_dist = euc * (1.0 + 0.25 * (1.0 - cos))
        vol_now, min_gap = max(float(hist.iloc[-1]["vol_w"]), 1e-6), max(2, self.cfg["gap"])
        n_sq, picked_pos, analogs = np.sqrt(hist.shape[1]), [], []
        pool_vols, fwd_h_vals = pool["vol_w"].values, self.fwd_ret_h.loc[pool.index].values
        for pos in np.argsort(h_dist):
            if all(abs(pos - p) >= min_gap for p in picked_pos):
                picked_pos.append(pos)
                cand = pool.index[pos]
                vs = float(np.clip(vol_now / max(float(pool_vols[pos]), 1e-6), 0.35, 1.50))
                sim = 1.0 / (1.0 + float(h_dist[pos]) / n_sq)
                rw = 0.5 ** ((len(hist) - 1 - pos) / max(1.0, 5.0 * self.bars_per_year))
                rh = float(fwd_h_vals[pos])
                if fast:
                    analogs.append({"weight": sim * rw, "adj_ret_h": rh * vs})
                else:
                    rl_v = self.fwd_ret_l.loc[cand] if pos <= len(hist) - 1 - self.cfg["rl"] else np.nan
                    analogs.append({"date": cand, "similarity": sim, "weight": sim * rw, "vol_scale": vs,
                                    "adj_ret_s": float(self.fwd_ret_s.loc[cand]) * vs, "fwd_ret_h": rh,
                                    "adj_ret_h": rh * vs, "adj_ret_l": float(rl_v) * vs if pd.notna(rl_v) else np.nan,
                                    "adj_max_dd": max(-0.95, float(self.fwd_max_dd.loc[cand]) * vs)})
            if len(analogs) >= top_k: break
        adf = pd.DataFrame(analogs)
        wn = adf["weight"] / adf["weight"].sum()
        w_win_h, w_exp_h = float(((adf["adj_ret_h"] > 0).astype(float) * wn).sum()), float((adf["adj_ret_h"] * wn).sum())
        a_an = float(np.clip((w_win_h - 0.5) + np.clip(w_exp_h * 2.0, -0.5, 0.5), -1.0, 1.0))
        if fast:
            res = {"a_analog": a_an, "w_win_h": w_win_h, "w_exp_h": w_exp_h}
            self.analog_cache[as_of] = res
            return res
        v_l = adf["adj_ret_l"].dropna()
        w_exp_l = float((v_l * (wn.loc[v_l.index] / wn.loc[v_l.index].sum())).sum()) if len(v_l) > 0 else np.nan
        res = {"a_analog": a_an, "w_win_s": float(((adf["adj_ret_s"] > 0).astype(float) * wn).sum()),
               "w_exp_s": float((adf["adj_ret_s"] * wn).sum()), "w_win_h": w_win_h, "w_exp_h": w_exp_h,
               "w_exp_l": w_exp_l, "w_exp_dd": float((adf["adj_max_dd"] * wn).sum()),
               "q25_h": float(adf["adj_ret_h"].quantile(0.25)), "worst_h": float(adf["adj_ret_h"].min()),
               "avg_sim": float(adf["similarity"].mean()), "adf": adf}
        self.analog_cache[as_of] = {"a_analog": a_an, "w_win_h": w_win_h, "w_exp_h": w_exp_h}
        return res

    def _meta_weights(self, as_of: pd.Timestamp) -> np.ndarray:
        pos_now, step = self.features.index.get_loc(as_of), max(1, self.cfg["reb"])
        end_pos, start_pos = pos_now - step - 1, max(self.cfg["min_h"] // 2, pos_now - min(pos_now, max(step * 12, int(2.0 * self.bars_per_year))))
        if end_pos - start_pos < step * 6: return PRIOR_W.copy()
        fwd = self.fwd_ret_s.loc[self.features.index[start_pos:end_pos:step]].dropna()
        if len(fwd) < 6: return PRIOR_W.copy()
        sub_s = self.alpha_cache.loc[fwd.index]
        a_an_hist = np.array([self._analog_match(d, fast=True)["a_analog"] for d in fwd.index], dtype=float)
        dw = 0.5 ** (np.array([(pos_now - self.features.index.get_loc(d)) / max(step, 1) for d in fwd.index]) / 9.0)
        dw /= dw.sum()
        utils = [float(np.sum(dw * (np.clip(0.5 + 0.5 * s, 0.0, 1.0) * fwd.values))) / (float(np.sqrt(np.sum(dw * (np.minimum(0.0, np.clip(0.5 + 0.5 * s, 0.0, 1.0) * fwd.values) ** 2)))) + 0.02)
                 for s in [sub_s["a_trend"].values, sub_s["a_val"].values, a_an_hist, sub_s["a_macro"].values]]
        sw = np.exp(np.clip(np.array(utils, dtype=float) * 2.0, -4.0, 4.0))
        w = np.clip(0.50 * PRIOR_W + 0.50 * (sw / sw.sum()), 0.10, 0.48)
        w /= w.sum()
        if float(self.er.loc[as_of]) < 0.15:
            m = w * np.array([0.70, 1.25, 1.15, 0.90])
            w = np.clip(m / m.sum(), 0.10, 0.50); w /= w.sum()
        return w

    def _calibrate(self, as_of: pd.Timestamp, raw_win: float, raw_exp: float) -> tuple[float, float, int]:
        pos_now, step = self.features.index.get_loc(as_of), max(self.horizon, self.cfg["reb"])
        mature = [(d, pw, pe, ar) for (d, pw, pe, ar) in self.cal_history if self.features.index.get_loc(d) <= pos_now - self.horizon]
        if len(mature) < 8:
            s_start, s_end = max(self.cfg["min_h"] // 2, pos_now - 24 * step), pos_now - self.horizon
            if s_end > s_start:
                for dt in self.features.index[s_start:s_end:max(1, step)]:
                    ar = float(self.fwd_ret_h.loc[dt]) if pd.notna(self.fwd_ret_h.loc[dt]) else np.nan
                    if pd.notna(ar):
                        an_p = self._analog_match(dt, fast=True)
                        mature.append((dt, an_p["w_win_h"], an_p["w_exp_h"], ar))
        if len(mature) < 6:
            return float(np.clip(0.35 * raw_win + 0.65 * 0.50, 0.10, 0.90)), raw_exp, len(mature)
        pws, pes, ars = np.array([x[1] for x in mature[-24:]]), np.array([x[2] for x in mature[-24:]]), np.array([x[3] for x in mature[-24:]])
        real_win = float((ars > 0).mean())
        cal_win = float(np.clip(0.20 * (raw_win - float(pws.mean() - real_win)) + 0.80 * real_win, 0.10, 0.90))
        cal_exp = float(raw_exp - 0.40 * float(np.clip(pes.mean() - ars.mean(), -0.15, 0.15)))
        return cal_win, cal_exp, len(mature)

    def analyze(self, as_of: pd.Timestamp = None, adaptive: bool = True, vol_target: float = 0.60,
                fee_rate: float = 0.0010, slippage: float = 0.0005, enforce_cost_gate: bool = True,
                live_intel: dict | None = None) -> dict:
        if as_of is None: as_of = self.features.index[-1]
        hist = self.features.loc[:as_of]
        if len(hist) < self.cfg["min_h"]:
            raise ValueError(f"Zu wenig Feature-Bars fuer {self.ticker} ({self.tf}: {len(hist)} < {self.cfg['min_h']}).")
        now, row_a = hist.iloc[-1], self.alpha_cache.loc[as_of]
        an = self._analog_match(as_of)
        w = self._meta_weights(as_of) if adaptive else PRIOR_W.copy()
        a_macro, fng_now, fng_30 = float(row_a["a_macro"]), None, None
        if self.fng is not None and self.bar_sec >= 86400:
            fh = self.fng.loc[:as_of]
            if len(fh) >= 30 and (as_of - fh.index[-1]).days <= 5:
                fng_now, fng_30 = float(fh.iloc[-1]), float(fh.iloc[-30:].mean())
                if fng_30 <= 25: a_macro = float(np.clip(a_macro + 0.20, -1.0, 1.0))
                elif fng_30 >= 78: a_macro = float(np.clip(a_macro - 0.20, -1.0, 1.0))
        if live_intel and as_of == self.features.index[-1]:
            if pd.notna(live_intel.get("funding_8h")):
                fr = live_intel["funding_8h"]
                if fr > 0.0005: a_macro = float(np.clip(a_macro - 0.20, -1.0, 1.0))
                elif fr < -0.0002: a_macro = float(np.clip(a_macro + 0.20, -1.0, 1.0))
            if live_intel.get("l2_ok"):
                a_macro = float(np.clip(a_macro + 0.15 * live_intel["obi"], -1.0, 1.0))
        alphas = np.array([float(row_a["a_trend"]), float(row_a["a_val"]), an["a_analog"], a_macro])
        score = float(np.clip(np.dot(w, alphas) * 100.0, -100.0, 100.0))
        if adaptive:
            cal_win, cal_exp, cal_n = self._calibrate(as_of, an["w_win_h"], an["w_exp_h"])
            if pd.notna(self.fwd_ret_h.loc[as_of]):
                self.cal_history.append((as_of, an["w_win_h"], an["w_exp_h"], float(self.fwd_ret_h.loc[as_of])))
        else:
            cal_win, cal_exp, cal_n = an["w_win_h"], an["w_exp_h"], 0
        std_out = float(an["adf"]["adj_ret_h"].std()) if len(an["adf"]) > 1 else 0.5
        t_stat = float(cal_exp / (max(std_out, 1e-4) / np.sqrt(max(len(an["adf"]), 1))))
        conv = float(np.clip(0.30 + 0.80 * abs(cal_win - 0.5) + 0.20 * (np.clip(abs(cal_exp) / max(std_out, 0.05), 0.0, 1.5) / 1.5) + 0.10 * an["avg_sim"], 0.35, 1.0))
        if score >= 40: verdict, alloc = "STARK BULLISH (Uebergewichten)", 0.50 + 0.50 * conv
        elif score >= 12: verdict, alloc = "MODERAT BULLISH (Halten / Aufbauen)", 0.40 + 0.40 * conv
        elif score > -12: verdict, alloc = "NEUTRAL / ABWARTEND (Teilposition)", 0.40
        elif score > -40: verdict, alloc = "VORSICHTIG BAERISCH (Risiko senken)", 0.15
        else: verdict, alloc = "STARK BAERISCH (Kapitalschutz / Cash)", 0.00
        vol_now, v_pct = float(now["vol_w"]), float(row_a["vol_pct"])
        eff_vt = vol_target if self.is_crypto else min(vol_target or 0.22, 0.22)
        if eff_vt is not None:
            alloc = min(1.0, alloc * float(np.clip(eff_vt / max(vol_now, 0.05), 0.45, 1.20)))
        rt_cost, caps = 2.0 * (fee_rate + slippage), []
        if float(now["vol_shock"]) > 1.35:
            alloc = min(alloc, 0.50); caps.append(f"EWMA-Vol-Schock ({now['vol_shock']:.2f}x > 1.35x) -> Max 50%")
        if an["avg_sim"] < 0.32:
            alloc = min(alloc, 0.40); caps.append(f"Unbekanntes Marktterrain (Aehnl. {an['avg_sim']:.1%} < 32%) -> Max 40%")
        if v_pct > 0.85:
            alloc = min(alloc, 0.65); caps.append(f"Extrem-Volatilitaet (Perzentil {v_pct:.0%}) -> Max 65%")
        sma_slow_p, price_now = float(self.sma_slow.loc[as_of]), float(self.df.loc[as_of, "Close"])
        below_inval = price_now < sma_slow_p * 0.92
        if below_inval:
            alloc = min(alloc, 0.40); caps.append(f"Unter Struktur-Notbremse (-8% SMA{self.cfg['slow']}: ${sma_slow_p * 0.92:,.2f}) -> Max 40%")
        cost_blocked = False
        if enforce_cost_gate and rt_cost > 0 and cal_exp <= rt_cost and alloc > 0:
            if self.bar_sec < 86400 or (below_inval and cal_exp <= 0.0):
                alloc = 0.0; cost_blocked = True
                caps.append(f"H0-Kosten-Gate ({self.tf}): Erw. {cal_exp:+.2%} <= Schwelle -> 0% Cash")
            elif score < 25:
                alloc = min(alloc, 0.25); cost_blocked = True
                caps.append(f"H0-Schwacher Edge: Erw. {cal_exp:+.2%} <= Kosten {rt_cost:.2%} -> Max 25%")
        z200, er_val = float(now["trend_z200"]), float(self.er.loc[as_of])
        regime = "BULLEN-REGIME" if (now["sma_ratio"] > 1.0 and z200 > 0) else ("BAEREN-REGIME" if (now["sma_ratio"] < 1.0 and z200 < 0) else "UEBERGANG / SEITWAERTS")
        names = ["Trend", "Bewertung", "Analoga", "Makro/Flow"]
        min_idx, max_idx = int(np.argmin(alphas)), int(np.argmax(alphas))
        reasons = [
            f"[PRO]  Staerkster Treiber : {names[max_idx]} ({alphas[max_idx]*100:+.1f}) | Regime: {regime} | ER={er_val:.2f}.",
            f"[H1]   Kalibrierte These  : Erw.({self.horizon}B) = {cal_exp:+.2%} (Roh {an['w_exp_h']:+.2%}) | Hit = {cal_win:.0%} (n={cal_n}) | t={t_stat:+.2f}.",
            f"[H0]   Staerkste Gegenthese: {names[min_idx]} ({alphas[min_idx]*100:+.1f}) | P25={an['q25_h']:+.2%} | Worst={an['worst_h']:+.2%} | Kosten={rt_cost:.2%}.",
            f"[RISK] Yang-Zhang & Bayes : YZ-Vol={vol_now:.1%} p.a. (Perz.{v_pct:.0%}, Schock {now['vol_shock']:.2f}x) | P(Bull)={now['p_bull']:.0%} P(Bear)={now['p_bear']:.0%}.",
        ]
        return {
            "date": as_of, "tf": self.tf, "price": price_now, "sma_slow_price": sma_slow_p,
            "invalidation_price": sma_slow_p * 0.92, "regime": regime, "er": er_val, "total_score": score,
            "conviction": conv, "verdict": verdict, "target_alloc": round(float(alloc), 2), "safety_caps": caps,
            "cost_blocked": cost_blocked, "t_stat": t_stat, "cal_win": cal_win, "cal_exp": cal_exp,
            "meta_weights": {"Trend": float(w[0]), "Bewertung": float(w[1]), "Analoga": float(w[2]), "Makro/Flow": float(w[3])},
            "alpha_scores": {"Trend": float(alphas[0]*100), "Bewertung": float(alphas[1]*100), "Analoga": float(alphas[2]*100), "Makro/Flow": float(alphas[3]*100)},
            "stats": {"w_win_s": an["w_win_s"], "w_exp_s": an["w_exp_s"], "w_win_h": an["w_win_h"], "w_exp_h": an["w_exp_h"],
                      "w_exp_l": an["w_exp_l"], "w_exp_dd": an["w_exp_dd"], "fng_now": fng_now,
                      "days_since_halving": int(days_since_halving(pd.DatetimeIndex([as_of])).iloc[0]) if (self.is_crypto and self.bar_sec >= 86400) else None},
            "reasons": reasons, "analogs": an["adf"],
        }

    def run_surgical_backtest(self, lookback_years: int | None = 5, adaptive: bool = True, fee_rate: float = 0.0010,
                              slippage: float = 0.0005, hysteresis: float = 0.15, notaus_dd: float = -0.28,
                              vol_target: float = 0.60, tax_rate: float = 0.0, tax_aware: bool = False,
                              enforce_cost_gate: bool = True) -> dict:
        self.cal_history = []
        min_idx = min(self.cfg["min_h"], len(self.features) - 10)
        start_dt = max(self.features.index[-1] - pd.Timedelta(days=365 * lookback_years), self.features.index[min_idx]) if (lookback_years and self.bar_sec >= 86400) else self.features.index[min_idx]
        sim_dates = self.df.loc[start_dt:].index
        rebal_set, eff_fee = set(sim_dates[::self.cfg["reb"]]), fee_rate + slippage
        cap, cash, lots = 10000.0, 10000.0, []
        bh_units, sma_cash, sma_units, peak_eq = cap / float(self.df.loc[sim_dates[0], "Close"]), cap, 0.0, cap
        notaus_active, notaus_bar, notaus_triggers = False, 0, 0
        trades, fees_paid, tax_paid, tf_gains, tx_gains, tax_blocked, loss_pool, cost_blocks = 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0
        daily_rows, sig_rows = [], []

        for bar_i, d in enumerate(sim_dates):
            price = float(self.df.loc[d, "Close"])
            cu = sum(l["units"] for l in lots)
            cur_eq = cash + cu * price
            peak_eq = max(peak_eq, cur_eq)
            strat_dd = (cur_eq / peak_eq) - 1.0
            if d in rebal_set:
                sma_t = 1.0 if price > float(self.sma_slow.loc[d]) else 0.0
                sma_eq = sma_cash + sma_units * price
                du = ((sma_eq * sma_t) / price) - sma_units
                if abs(du * price) > sma_eq * 0.05:
                    sma_cash -= du * price + abs(du * price) * eff_fee; sma_units += du
            rebal_now = (d in rebal_set) and (d in self.features.index)
            sma_slow_v, slope_v = float(self.sma_slow.loc[d]), float(self.sma_slow_slope.get(d, 0.0))
            bear_dec = (price < sma_slow_v * 0.92) and (slope_v < 0.0)
            cur_alloc = (cu * price) / cur_eq if cur_eq > 0 else 0.0

            if (not notaus_active) and (strat_dd <= notaus_dd) and bear_dec and (cur_alloc > 0.25):
                notaus_active, notaus_bar, notaus_triggers, rtax = True, bar_i, notaus_triggers + 1, 0.0
                for lot in lots:
                    hd = (d - lot["date"]).total_seconds() / 86400.0
                    g = (price * (1.0 - eff_fee) - lot["cost_per_unit"]) * lot["units"]
                    if hd >= 365.0:
                        if g > 0: tf_gains += g
                    else:
                        if g < 0: loss_pool += abs(g)
                        elif g > 0:
                            tx_gains += g
                            if tax_rate > 0: off = min(loss_pool, g); loss_pool -= off; rtax += (g - off) * tax_rate
                fee = (cu * price) * eff_fee
                cash += max(0.0, (cu * price) - fee - rtax)
                lots, fees_paid, tax_paid, trades = [], fees_paid + fee, tax_paid + rtax, trades + 1
            elif notaus_active and rebal_now:
                if (price > float(self.sma_fast.loc[d])) or (float(self.features.loc[d, "roc_s"]) > 0.04) or ((bar_i - notaus_bar) >= self.cfg["notaus_b"]):
                    notaus_active, peak_eq = False, cash

            if rebal_now and (not notaus_active):
                rep = self.analyze(as_of=d, adaptive=adaptive, vol_target=vol_target, fee_rate=fee_rate, slippage=slippage, enforce_cost_gate=enforce_cost_gate)
                if rep["cost_blocked"]: cost_blocks += 1
                des = 0.40 if (strat_dd <= -0.20 and bear_dec and rep["target_alloc"] > 0.40) else rep["target_alloc"]
                if pd.notna(self.fwd_ret_h.loc[d]):
                    sig_rows.append({"date": d, "score": rep["total_score"], "cal_win": rep["cal_win"], "cal_exp": rep["cal_exp"], "fwd_ret_h": float(self.fwd_ret_h.loc[d])})
                cu = sum(l["units"] for l in lots)
                cur_eq = cash + cu * price
                cur_alloc = (cu * price) / cur_eq if cur_eq > 0 else 0.0
                if abs(des - cur_alloc) >= hysteresis:
                    dv = (cur_eq * des) - (cu * price)
                    if dv > 0:
                        bg = min(dv, cash); fee = bg * eff_fee; nu = max(0.0, bg - fee) / price
                        if nu > 0:
                            cash -= bg; lots.append({"date": d, "units": nu, "cost_per_unit": price * (1.0 + eff_fee)})
                            trades += 1; fees_paid += fee
                    elif dv < 0:
                        u_sell, sold_u, rtax, nl = min(cu, abs(dv) / price), 0.0, 0.0, []
                        for lot in lots:
                            if u_sell <= 1e-12: nl.append(lot); continue
                            hd = (d - lot["date"]).total_seconds() / 86400.0
                            if tax_aware and (270.0 <= hd < 365.0) and price > lot["cost_per_unit"] and rep["total_score"] > -35:
                                nl.append(lot); tax_blocked += 1; u_sell = 0.0; continue
                            tk = min(lot["units"], u_sell); u_sell -= tk; sold_u += tk
                            g = (price * (1.0 - eff_fee) - lot["cost_per_unit"]) * tk
                            if hd >= 365.0:
                                if g > 0: tf_gains += g
                            else:
                                if g < 0: loss_pool += abs(g)
                                elif g > 0:
                                    tx_gains += g
                                    if tax_rate > 0: off = min(loss_pool, g); loss_pool -= off; rtax += (g - off) * tax_rate
                            if lot["units"] - tk > 1e-12:
                                nl.append({"date": lot["date"], "units": lot["units"] - tk, "cost_per_unit": lot["cost_per_unit"]})
                        lots = nl
                        if sold_u > 0:
                            proc = sold_u * price; fee = proc * eff_fee
                            cash += max(0.0, proc - fee - rtax)
                            trades += 1; fees_paid += fee; tax_paid += rtax

            cu = sum(l["units"] for l in lots); ag_eq = cash + cu * price
            reg_n = "BULLEN" if (price > sma_slow_v and slope_v >= 0) else ("BAEREN" if price < sma_slow_v else "UEBERGANG")
            daily_rows.append({"date": d, "agent_eq": ag_eq, "bh_eq": bh_units * price, "sma_eq": sma_cash + sma_units * price,
                               "alloc": (cu * price) / ag_eq if ag_eq > 0 else 0.0, "regime": reg_n})

        return {"equity": pd.DataFrame(daily_rows).set_index("date"), "signals": pd.DataFrame(sig_rows),
                "trades": trades, "notaus_triggers": notaus_triggers, "fees_paid": fees_paid, "tax_paid": tax_paid,
                "taxfree_gains": tf_gains, "taxable_gains": tx_gains, "tax_blocked": tax_blocked,
                "loss_pool_end": loss_pool, "cost_blocks": cost_blocks}

PANEL_FEATS = [
    "tsmom_14", "tsmom_30", "tsmom_60", "tsmom_120", "trend_z200",
    "sma_ratio", "er_60", "rel_btc_60", "rel_btc_120", "vol_shock", "drawdown_ath", "amihud_z",
]

class ApexPortfolioEngine:
    def __init__(self, data_dict: dict[str, pd.DataFrame], macro: dict[str, pd.Series] | None = None,
                 fng: pd.Series | None = None, horizon: int = 21):
        self.data = {k: v.sort_index().copy() for k, v in data_dict.items() if len(v) >= 260}
        self.macro, self.fng, self.horizon = macro or {}, fng, horizon
        self.btc = self.data.get("BTC-USD", next(iter(self.data.values())))["Close"]
        self.panel, self.closes, self.vols, self.sma50s, self.sma200s, self.fwd_rets, self.fwd_vols = self._build_panel()

    def _build_panel(self):
        panel, closes, vols, sma50s, sma200s, fwd_rets, fwd_vols = {}, {}, {}, {}, {}, {}, {}
        for sym, df in self.data.items():
            c = df["Close"]
            lr = np.log(c / c.shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            yz = QuantCore.yang_zhang_vol(df, 30, 365.0)
            ew_f, ew_s = QuantCore.ewma_vol(c, 10, 365.0), QuantCore.ewma_vol(c, 60, 365.0)
            sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
            std200 = c.rolling(200).std().replace(0, np.nan)
            er60 = ((c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)).clip(0.0, 1.0).fillna(0.30)
            rel_btc = c / self.btc.reindex(df.index).ffill().replace(0, np.nan)
            if "Volume" in df.columns:
                ami = (lr.abs() / (c * df["Volume"]).replace(0, np.nan)).rolling(30).mean()
                ami_z = ((ami - ami.rolling(180, min_periods=60).mean()) / ami.rolling(180, min_periods=60).std().replace(0, np.nan)).fillna(0.0)
            else:
                ami_z = pd.Series(0.0, index=df.index)
            f = pd.DataFrame(index=df.index)
            f["tsmom_14"] = (c.pct_change(14) / (yz * np.sqrt(14.0 / 365.0)).replace(0, np.nan)).clip(-3.5, 3.5)
            f["tsmom_30"] = (c.pct_change(30) / (yz * np.sqrt(30.0 / 365.0)).replace(0, np.nan)).clip(-3.5, 3.5)
            f["tsmom_60"] = (c.pct_change(60) / (yz * np.sqrt(60.0 / 365.0)).replace(0, np.nan)).clip(-3.5, 3.5)
            f["tsmom_120"] = (c.pct_change(120) / (yz * np.sqrt(120.0 / 365.0)).replace(0, np.nan)).clip(-3.5, 3.5)
            f["trend_z200"] = ((c - sma200) / std200).clip(-3.5, 3.5)
            f["sma_ratio"] = ((sma50 / sma200) - 1.0) * 4.0
            f["er_60"] = (er60 - 0.25) * 3.0
            f["rel_btc_60"] = (rel_btc.pct_change(60) * 3.0).clip(-3.5, 3.5)
            f["rel_btc_120"] = (rel_btc.pct_change(120) * 2.0).clip(-3.5, 3.5)
            f["vol_shock"] = ((ew_f / np.maximum(ew_s, 1e-4)) - 1.0).clip(-2.0, 2.0)
            f["drawdown_ath"] = ((c / c.cummax()) - 1.0) * 3.0
            f["amihud_z"] = ami_z.clip(-3.0, 3.0)
            panel[sym] = f.replace([np.inf, -np.inf], np.nan).dropna()
            closes[sym], vols[sym], sma50s[sym], sma200s[sym] = c, yz, sma50, sma200
            fwd_rets[sym] = c.shift(-self.horizon) / c - 1.0
            fwd_vols[sym] = lr.shift(-1).rolling(pd.api.indexers.FixedForwardWindowIndexer(window_size=self.horizon)).std() * np.sqrt(365.0)
        return panel, closes, vols, sma50s, sma200s, fwd_rets, fwd_vols

    def _fit_ridge(self, X: np.ndarray, y: np.ndarray, lam: float = 350.0) -> np.ndarray:
        if len(X) < 80:
            w = np.zeros(len(PANEL_FEATS)); w[2], w[3], w[5], w[7] = 0.25, 0.25, 0.25, 0.25; return w
        return np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ y)

    def _compute_ic_weights(self, matured_hist: list[dict]) -> tuple[np.ndarray, dict]:
        names = ["f_trend", "f_relbtc", "f_pullback", "f_ridge", "f_lowvol"]
        if len(matured_hist) < 80:
            return np.array([0.35, 0.20, 0.15, 0.10, 0.20]), {k: 0.0 for k in names}
        df = pd.DataFrame(matured_hist[-450:])
        ics, raw_w = {}, []
        for k in names:
            ic, _ = spearman_ic(df[k].values, df["fwd_ret"].values)
            ics[k] = ic; raw_w.append(max(0.0, ic + 0.02))
        arr = np.array(raw_w, dtype=float)
        if arr.sum() <= 1e-6: return np.array([0.40, 0.15, 0.15, 0.00, 0.30]), ics
        return arr / arr.sum(), ics

    def _score_cross_section(self, as_of: pd.Timestamp, beta: np.ndarray, factor_w: np.ndarray,
                             live_intel_map: dict[str, dict] | None = None) -> tuple[pd.DataFrame, float, bool]:
        rows = []
        for sym, f in self.panel.items():
            if as_of not in f.index: continue
            r = f.loc[as_of]
            px, sma5, sma2 = float(self.closes[sym].loc[as_of]), float(self.sma50s[sym].loc[as_of]), float(self.sma200s[sym].loc[as_of])
            f_trend = float(0.45 * r["tsmom_60"] + 0.35 * r["tsmom_120"] + 0.20 * r["sma_ratio"])
            f_relbtc = float(0.60 * r["rel_btc_60"] + 0.40 * r["rel_btc_120"])
            f_pullback = float(np.clip(-r["tsmom_14"], -2.0, 2.0)) if px >= sma2 * 0.92 else float(np.clip(-0.3 * r["tsmom_14"] - 0.5, -2.0, 1.0))
            f_ridge = float(np.clip(r[PANEL_FEATS].values.astype(float) @ beta, -2.5, 2.5))
            vol = float(self.vols[sym].loc[as_of])
            f_lowvol = float(np.clip((0.70 - vol) * 2.0 - 0.3 * r["vol_shock"], -2.0, 2.0))
            alpha = float(np.dot(factor_w, [f_trend, f_relbtc, f_pullback, f_ridge, f_lowvol]))
            liq_penalty, spread_bps, funding = 0.0, np.nan, np.nan
            if live_intel_map and sym in live_intel_map:
                li = live_intel_map[sym]
                spread_bps, funding = li.get("spread_bps", np.nan), li.get("funding_8h", np.nan)
                if li.get("l2_ok"):
                    alpha += 0.10 * li["obi"]
                    if spread_bps > 20.0: liq_penalty = -0.50
                if pd.notna(funding):
                    if funding > 0.0005: alpha -= 0.15
                    elif funding < -0.0002: alpha += 0.12
            rows.append({"ticker": sym, "price": px, "sma50": sma5, "sma200": sma2, "vol": vol,
                         "vol_shock": float(r["vol_shock"]), "f_trend": f_trend, "f_relbtc": f_relbtc,
                         "f_pullback": f_pullback, "f_ridge": f_ridge, "f_lowvol": f_lowvol,
                         "alpha": alpha + liq_penalty, "above_sma200": px > sma2,
                         "trend_ok": (px > sma2) and (sma5 >= sma2 * 0.96),
                         "spread_bps": spread_bps, "funding_8h": funding})
        if not rows: return pd.DataFrame(), 0.0, False
        df = pd.DataFrame(rows).set_index("ticker")
        breadth = float(df["above_sma200"].mean())
        btc_bull = bool(df.loc["BTC-USD", "above_sma200"]) if "BTC-USD" in df.index else (breadth >= 0.4)
        return df, breadth, btc_bull

    def build_target_weights(self, as_of: pd.Timestamp, beta: np.ndarray, factor_w: np.ndarray,
                             live_intel_map: dict[str, dict] | None = None,
                             cvar_limit: float = 0.055) -> tuple[dict[str, float], pd.DataFrame, float, float, bool]:
        cs, breadth, btc_bull = self._score_cross_section(as_of, beta, factor_w, live_intel_map)
        if cs.empty: return {}, cs, 0.0, 0.0, False
        macro_mult = 1.0
        if "SP500" in self.macro:
            sp = self.macro["SP500"].loc[:as_of]
            if len(sp) > 90 and (sp.iloc[-1] / sp.iloc[-90] - 1.0) < -0.05: macro_mult *= 0.80
        if self.fng is not None:
            fh = self.fng.loc[:as_of]
            if len(fh) >= 30 and (as_of - fh.index[-1]).days <= 5:
                f30 = float(fh.iloc[-30:].mean())
                if f30 <= 22: macro_mult *= 1.10
                elif f30 >= 80: macro_mult *= 0.85
        max_gross = 0.00 if ((not btc_bull) and breadth < 0.25) else (0.15 * macro_mult if not btc_bull else (0.70 * macro_mult if breadth < 0.40 else 1.00 * macro_mult))
        max_gross = float(np.clip(max_gross, 0.0, 1.0))
        elig = cs[(cs["trend_ok"]) & (cs["alpha"] > -0.10) & (cs["vol_shock"] < 0.65)].sort_values("alpha", ascending=False).head(6)
        if elig.empty or max_gross <= 0.01:
            cs["target_w"] = 0.0
            return {s: 0.0 for s in cs.index}, cs, breadth, max_gross, btc_bull
        ret_df = pd.DataFrame({s: np.log(self.closes[s].loc[:as_of] / self.closes[s].loc[:as_of].shift(1)).iloc[-90:] for s in elig.index}).dropna(how="any")
        max_ws = np.array([0.65 if s == "BTC-USD" else 0.30 for s in elig.index], dtype=float)
        if len(ret_df) < 20:
            w_arr = np.minimum(max_ws, max_gross / len(elig))
        else:
            w_arr = QuantCore.optimize_portfolio(elig["alpha"].values, QuantCore.ledoit_wolf_cov(ret_df), max_w=max_ws, max_gross=max_gross, risk_aversion=2.0, cvar_limit=cvar_limit)
        weights = {s: 0.0 for s in cs.index}
        for s, w in zip(elig.index, w_arr): weights[s] = float(w)
        cs["target_w"] = [weights[s] for s in cs.index]
        return weights, cs, breadth, max_gross, btc_bull

    def fit_and_eval_now(self, as_of: pd.Timestamp, adaptive: bool = True):
        cutoff = as_of - pd.Timedelta(days=self.horizon)
        X_tr, y_tr, mat_eval = [], [], []
        panel_dates = sorted({d for f in self.panel.values() for d in f.index[::self.horizon]})
        for s, f in self.panel.items():
            sub_dates = [td for td in panel_dates if td <= cutoff and td in f.index]
            if not sub_dates: continue
            mat_X = f.loc[sub_dates, PANEL_FEATS].values.astype(float)
            frs, vols = self.fwd_rets[s].reindex(sub_dates).values, np.maximum(self.vols[s].reindex(sub_dates).values, 0.10)
            ok = ~np.isnan(frs)
            if ok.any():
                X_tr.append(mat_X[ok]); y_tr.append(np.clip(frs[ok] / (vols[ok] * np.sqrt(self.horizon / 365.0)), -3.5, 3.5))
        beta = self._fit_ridge(np.vstack(X_tr)[-3000:] if X_tr else np.empty((0, len(PANEL_FEATS))),
                               np.concatenate(y_tr)[-3000:] if y_tr else np.empty(0), lam=350.0)
        eval_dates = [td for td in panel_dates if td <= cutoff][-25:]
        for s, f in self.panel.items():
            sub_d = [td for td in eval_dates if td in f.index]
            if not sub_d: continue
            sub_f = f.loc[sub_d]
            frs, pxs, s2s, vs = self.fwd_rets[s].reindex(sub_d).values, self.closes[s].reindex(sub_d).values, self.sma200s[s].reindex(sub_d).values, self.vols[s].reindex(sub_d).values
            preds = np.clip(sub_f[PANEL_FEATS].values.astype(float) @ beta, -2.5, 2.5)
            for idx_i in range(len(sub_d)):
                if pd.isna(frs[idx_i]): continue
                r = sub_f.iloc[idx_i]
                mat_eval.append({
                    "f_trend": float(0.45 * r["tsmom_60"] + 0.35 * r["tsmom_120"] + 0.20 * r["sma_ratio"]),
                    "f_relbtc": float(0.60 * r["rel_btc_60"] + 0.40 * r["rel_btc_120"]),
                    "f_pullback": float(np.clip(-r["tsmom_14"], -2.0, 2.0)) if pxs[idx_i] >= s2s[idx_i] * 0.92 else float(np.clip(-0.3 * r["tsmom_14"] - 0.5, -2.0, 1.0)),
                    "f_ridge": float(preds[idx_i]),
                    "f_lowvol": float(np.clip((0.70 - float(vs[idx_i])) * 2.0 - 0.3 * r["vol_shock"], -2.0, 2.0)),
                    "fwd_ret": float(frs[idx_i]),
                })
        fw, ics = self._compute_ic_weights(mat_eval) if adaptive else (np.array([0.35, 0.20, 0.15, 0.10, 0.20]), {})
        return beta, fw, ics

    def run_portfolio_backtest(self, lookback_years: int = 5, adaptive: bool = True, fee_rate: float = 0.0010,
                               slippage: float = 0.0005, hysteresis: float = 0.05, cvar_limit: float = 0.055) -> dict:
        all_dates = self.btc.index
        start_dt = max(all_dates[-1] - pd.Timedelta(days=365 * lookback_years), all_dates[min(260, len(all_dates) - 20)])
        sim_dates = all_dates[all_dates >= start_dt]
        rebal_set, eff_fee = set(sim_dates[::self.horizon]), fee_rate + slippage
        panel_dates = sorted({d for f in self.panel.values() for d in f.index[::self.horizon]})
        matured_X, matured_y, ptr, pending_eval, matured_eval = [], [], 0, [], []
        cap, cash, units, ew_units, last_px = 10000.0, 10000.0, {s: 0.0 for s in self.data}, {}, {}
        btc_units = cap / float(self.btc.loc[sim_dates[0]])
        ew_init = [s for s in self.data if sim_dates[0] in self.closes[s].index]
        for s in ew_init: ew_units[s] = (cap / len(ew_init)) / float(self.closes[s].loc[sim_dates[0]])
        eq_rows, oos_rows, trades, fees_paid, ridge_vetos = [], [], 0, 0.0, 0

        for d in sim_dates:
            px_now = {s: float(self.closes[s].loc[d]) for s in self.data if d in self.closes[s].index}
            last_px.update(px_now)
            for s in list(units.keys()):
                if units[s] > 1e-12 and s in px_now and d in self.sma200s[s].index:
                    s2 = float(self.sma200s[s].loc[d])
                    if pd.notna(s2) and px_now[s] < s2 * 0.92:
                        sell_usd = units[s] * px_now[s]; fee = sell_usd * eff_fee
                        cash += max(0.0, sell_usd - fee); units[s] = 0.0; fees_paid += fee; trades += 1
            if d in rebal_set:
                cutoff = d - pd.Timedelta(days=self.horizon)
                while ptr < len(panel_dates) and panel_dates[ptr] <= cutoff:
                    td = panel_dates[ptr]; ptr += 1
                    for s, f in self.panel.items():
                        if td in f.index and pd.notna(self.fwd_rets[s].get(td, np.nan)):
                            v = max(float(self.vols[s].loc[td]), 0.10)
                            yr = float(self.fwd_rets[s].loc[td]) / (v * np.sqrt(self.horizon / 365.0))
                            matured_X.append(f.loc[td, PANEL_FEATS].values.astype(float))
                            matured_y.append(float(np.clip(yr, -3.5, 3.5)))
                still = []
                for item in pending_eval:
                    if item["date"] <= cutoff: matured_eval.append(item)
                    else: still.append(item)
                pending_eval = still
                beta = self._fit_ridge(np.array(matured_X[-3000:], dtype=float) if matured_X else np.empty((0, len(PANEL_FEATS))),
                                       np.array(matured_y[-3000:], dtype=float) if matured_y else np.empty(0), lam=350.0)
                if adaptive:
                    fw, ics = self._compute_ic_weights(matured_eval)
                    if ics.get("f_ridge", 0.0) <= 0.0: ridge_vetos += 1
                else:
                    fw = np.array([0.35, 0.20, 0.15, 0.10, 0.20])
                tgt_w, cs, _, _, _ = self.build_target_weights(d, beta, fw, None, cvar_limit=cvar_limit)
                for sym, row in cs.iterrows():
                    fr, fv = self.fwd_rets[sym].get(d, np.nan), self.fwd_vols[sym].get(d, np.nan)
                    if pd.notna(fr) and pd.notna(fv):
                        rec = {"date": d, "ticker": sym, "alpha": float(row["alpha"]), "f_trend": float(row["f_trend"]),
                               "f_relbtc": float(row["f_relbtc"]), "f_pullback": float(row["f_pullback"]),
                               "f_ridge": float(row["f_ridge"]), "f_lowvol": float(row["f_lowvol"]),
                               "pred_vol": float(row["vol"]), "above_sma200": bool(row["above_sma200"]),
                               "fwd_ret": float(fr), "fwd_vol": float(fv)}
                        pending_eval.append(rec); oos_rows.append(rec)
                cur_val = cash + sum(units[s] * last_px.get(s, 0.0) for s in units)
                for s in list(units.keys()):
                    if s not in px_now: continue
                    tw, cw = tgt_w.get(s, 0.0), (units[s] * px_now[s]) / max(cur_val, 1e-9)
                    if tw < cw and (abs(tw - cw) >= hysteresis or tw == 0.0):
                        sell_usd = (units[s] * px_now[s]) if tw == 0.0 else min(units[s] * px_now[s], (cw - tw) * cur_val)
                        if sell_usd > 10.0:
                            fee = sell_usd * eff_fee; units[s] = max(0.0, units[s] - sell_usd / px_now[s])
                            cash += max(0.0, sell_usd - fee); fees_paid += fee; trades += 1
                for s, tw in tgt_w.items():
                    if s not in px_now or tw <= 0: continue
                    cw = (units[s] * px_now[s]) / max(cur_val, 1e-9)
                    if tw > cw and abs(tw - cw) >= hysteresis:
                        buy_usd = min(cash, (tw - cw) * cur_val)
                        if buy_usd > 10.0:
                            fee = buy_usd * eff_fee; units[s] += max(0.0, buy_usd - fee) / px_now[s]
                            cash -= buy_usd; fees_paid += fee; trades += 1
            port_val = cash + sum(units[s] * last_px.get(s, 0.0) for s in units)
            ew_val = sum(ew_units[s] * last_px.get(s, 0.0) for s in ew_units)
            eq_rows.append({"date": d, "apex_eq": port_val, "ew_eq": ew_val, "btc_eq": btc_units * last_px.get("BTC-USD", 0.0),
                            "invest": (port_val - cash) / max(port_val, 1e-9)})
        return {"equity": pd.DataFrame(eq_rows).set_index("date"), "oos": pd.DataFrame(oos_rows),
                "trades": trades, "fees_paid": fees_paid, "ridge_vetos": ridge_vetos}

class ExchangeBroker:
    @staticmethod
    def binance_lot_round(symbol: str, qty: float) -> float:
        info = http_json(f"https://data-api.binance.vision/api/v3/exchangeInfo?symbol={symbol}", timeout=4.0)
        if isinstance(info, dict) and info.get("symbols"):
            for flt in info["symbols"][0].get("filters", []):
                if flt.get("filterType") == "LOT_SIZE":
                    step = float(flt.get("stepSize", 1e-6))
                    if step > 0: return math.floor(qty / step) * step
        return round(qty, 6)

    @staticmethod
    def place_binance_order(ticker: str, side: str, usd_notional: float, qty: float, cfg: dict) -> dict:
        key, sec = get_exchange_creds("binance", cfg)
        if not key or not sec:
            raise RuntimeError("BINANCE API-Key fehlt. Bitte in Menue [6] einrichten & pruefen.")
        sym = to_binance_symbol(ticker)
        params = {"symbol": sym, "side": side.upper(), "type": "MARKET", "timestamp": int(time.time() * 1000)}
        if side.upper() == "BUY": params["quoteOrderQty"] = f"{usd_notional:.2f}"
        else:
            q = ExchangeBroker.binance_lot_round(sym, qty)
            if q <= 0: raise ValueError(f"Menge zu klein fuer LOT_SIZE ({sym}).")
            params["quantity"] = f"{q:.8f}".rstrip("0").rstrip(".")
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(sec.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"https://api.binance.com/api/v3/order?{qs}&signature={sig}", method="POST", headers={"X-MBX-APIKEY": key})
        with urllib.request.urlopen(req, timeout=8.0) as r: return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def place_kraken_order(ticker: str, side: str, qty: float, cfg: dict) -> dict:
        key, sec = get_exchange_creds("kraken", cfg)
        if not key or not sec:
            raise RuntimeError("KRAKEN API-Key fehlt. Bitte in Menue [6] einrichten & pruefen.")
        path, nonce = "/0/private/AddOrder", str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode({"nonce": nonce, "ordertype": "market", "type": side.lower(), "volume": f"{qty:.6f}", "pair": to_kraken_pair(ticker)})
        sig = base64.b64encode(hmac.new(base64.b64decode(sec), path.encode("utf-8") + hashlib.sha256((nonce + post_data).encode("utf-8")).digest(), hashlib.sha512).digest()).decode("utf-8")
        req = urllib.request.Request("https://api.kraken.com" + path, data=post_data.encode("utf-8"), headers={"API-Key": key, "API-Sign": sig})
        with urllib.request.urlopen(req, timeout=8.0) as r: return json.loads(r.read().decode("utf-8"))

class AutonomousTrader:
    def __init__(self, state_path: Path = STATE_FILE, initial_capital: float = 10000.0):
        self.state_path, self.initial_capital = state_path, initial_capital
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"cash_usd": self.initial_capital, "positions": {}, "peak_equity": self.initial_capital,
                "daily_date": "", "daily_start_equity": self.initial_capital, "halted": False,
                "halt_reason": "", "trades": 0, "fees_paid": 0.0, "tax_paid": 0.0, "loss_pool": 0.0,
                "last_run": None, "history": []}

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def mark_to_market(self, prices: dict[str, float]) -> float:
        return float(self.state["cash_usd"]) + sum(sum(float(l["units"]) for l in lots) * prices.get(sym, 0.0) for sym, lots in self.state["positions"].items())

    def run_cycle(self, mode: str = "paper", confirm_live: bool = False, tax_rate: float = 0.0,
                  tax_aware: bool = True, fee_rate: float = 0.0010, live_intel_map: dict | None = None,
                  engine: ApexPortfolioEngine | None = None, precomputed: tuple | None = None,
                  cfg: dict | None = None, log_events: bool = True) -> dict:
        cfg = cfg or load_cfg()
        if mode.startswith("live-"):
            if not confirm_live:
                raise PermissionError("SICHERHEITS-SPERRE: Live-Modus erfordert explizit --confirm-live!")
            ex_n = mode.split("-")[1]
            ok_ex, msg_ex = verify_exchange_connection(ex_n, cfg)
            if not ok_ex:
                raise PermissionError(f"LIVE-SPERRE: {msg_ex}")

        now = pd.Timestamp.utcnow().tz_localize(None)
        today_s = now.strftime("%Y-%m-%d")
        if engine is None:
            data = fetch_universe_daily(cfg.get("active_universe", UNIVERSE), start="2020-01-01")
            macro, fng = load_macro_bundle()
            engine = ApexPortfolioEngine(data, macro, fng, horizon=21)
        as_of = engine.btc.index[-1]
        beta, fw, ics = precomputed if precomputed is not None else engine.fit_and_eval_now(as_of, adaptive=True)

        if live_intel_map is None:
            with ThreadPoolExecutor(max_workers=6) as ex:
                live_intel_map = dict(ex.map(lambda s: (s, fetch_live_intel(s, float(cfg.get("order_usd", 5000.0)))), list(engine.panel.keys())))

        tgt_w, cs, breadth, max_gross, btc_bull = engine.build_target_weights(as_of, beta, fw, live_intel_map, cvar_limit=float(cfg.get("cvar_limit", 0.055)))
        prices = {s: (live_intel_map[s]["mid"] if (s in live_intel_map and pd.notna(live_intel_map[s].get("mid"))) else float(engine.closes[s].loc[:as_of].iloc[-1])) for s in engine.closes}

        eq_before = self.mark_to_market(prices)
        if self.state["daily_date"] != today_s:
            self.state["daily_date"], self.state["daily_start_equity"], self.state["halted"], self.state["halt_reason"] = today_s, eq_before, False, ""
        self.state["peak_equity"] = max(float(self.state["peak_equity"]), eq_before)
        daily_ret = (eq_before / max(self.state["daily_start_equity"], 1e-9)) - 1.0
        port_dd = (eq_before / max(self.state["peak_equity"], 1e-9)) - 1.0

        actions = []
        if daily_ret <= -0.05 or port_dd <= -0.20:
            self.state["halted"] = True
            self.state["halt_reason"] = f"Circuit-Breaker: Daily {daily_ret:+.1%} / DD {port_dd:+.1%}"
            tgt_w = {s: 0.0 for s in tgt_w}
            actions.append(f"[NOTAUS] {self.state['halt_reason']}")

        for sym, lots in list(self.state["positions"].items()):
            px = prices.get(sym, 0.0)
            if lots and px > 0:
                stop_px = max(float(l.get("stop_price", 0.0)) for l in lots)
                vol_d = float(engine.vols[sym].iloc[-1]) / np.sqrt(365.0) if sym in engine.vols else 0.04
                for l in lots: l["stop_price"] = max(float(l.get("stop_price", 0.0)), px * (1.0 - 3.0 * vol_d))
                if px < stop_px:
                    tgt_w[sym] = 0.0
                    actions.append(f"[STOP-LOSS] {sym}: ${px:,.2f} < Stop ${stop_px:,.2f}")

        hysteresis = float(cfg.get("hysteresis", 0.05))
        for sym in list(self.state["positions"].keys()):
            lots = self.state["positions"].get(sym, [])
            cur_u = sum(float(l["units"]) for l in lots)
            px = prices.get(sym, 0.0)
            if cur_u <= 1e-12 or px <= 0: continue
            cw, tw = (cur_u * px) / max(eq_before, 1e-9), tgt_w.get(sym, 0.0)
            if tw < cw and (abs(cw - tw) >= hysteresis or tw == 0.0):
                li = live_intel_map.get(sym, {})
                if li.get("l2_ok") and li.get("spread_bps", 0.0) > 35.0 and not self.state["halted"]:
                    actions.append(f"[L2-SKIP SELL] {sym}: Spread {li['spread_bps']:.1f}bps"); continue
                sell_usd = (cw - tw) * eq_before if tw > 0 else (cur_u * px)
                u_need = min(cur_u, sell_usd / px)
                fill_px = li.get("sell_vwap") if (li.get("l2_ok") and pd.notna(li.get("sell_vwap"))) else px * 0.9995
                if mode == "live-binance":
                    resp = ExchangeBroker.place_binance_order(sym, "SELL", sell_usd, u_need, cfg)
                    actions.append(f"[LIVE-BINANCE SELL] {sym} ~${sell_usd:,.2f} | ID={resp.get('orderId')}")
                elif mode == "live-kraken":
                    resp = ExchangeBroker.place_kraken_order(sym, "sell", u_need, cfg)
                    actions.append(f"[LIVE-KRAKEN SELL] {sym} ~${sell_usd:,.2f} | {resp.get('result')}")
                u_rem, sold_u, rtax, nl = u_need, 0.0, 0.0, []
                for lot in lots:
                    if u_rem <= 1e-12: nl.append(lot); continue
                    hd = (now - pd.Timestamp(lot["date"])).total_seconds() / 86400.0
                    if tax_aware and (270.0 <= hd < 365.0) and fill_px > lot["cost_per_unit"] and not self.state["halted"] and tw > 0:
                        nl.append(lot); u_rem = 0.0; actions.append(f"[DE-FIFO-SCHUTZ] {sym}: Tag {hd:.0f}/365 geschont."); continue
                    tk = min(float(lot["units"]), u_rem); u_rem -= tk; sold_u += tk
                    g = (fill_px * (1.0 - fee_rate) - float(lot["cost_per_unit"])) * tk
                    if hd < 365.0:
                        if g < 0: self.state["loss_pool"] += abs(g)
                        elif g > 0 and tax_rate > 0:
                            off = min(self.state["loss_pool"], g); self.state["loss_pool"] -= off; rtax += (g - off) * tax_rate
                    if float(lot["units"]) - tk > 1e-12: lot["units"] = float(lot["units"]) - tk; nl.append(lot)
                if nl: self.state["positions"][sym] = nl
                else: self.state["positions"].pop(sym, None)
                if sold_u > 0:
                    proc = sold_u * fill_px; fee = proc * fee_rate
                    self.state["cash_usd"] += max(0.0, proc - fee - rtax)
                    self.state["fees_paid"] += fee; self.state["tax_paid"] += rtax; self.state["trades"] += 1
                    actions.append(f"[FILL SELL {mode.upper()}] {sym}: {sold_u:.6f} @ ${fill_px:,.2f} (${proc:,.2f}, Steuer ${rtax:,.2f})")

        if not self.state["halted"]:
            for sym, tw in sorted(tgt_w.items(), key=lambda kv: kv[1], reverse=True):
                if tw <= 0: continue
                px = prices.get(sym, 0.0)
                if px <= 0: continue
                cur_u = sum(float(l["units"]) for l in self.state["positions"].get(sym, []))
                cw = (cur_u * px) / max(eq_before, 1e-9)
                if tw > cw and abs(tw - cw) >= hysteresis:
                    buy_usd = min(float(self.state["cash_usd"]), (tw - cw) * eq_before)
                    if buy_usd < 15.0: continue
                    li = live_intel_map.get(sym, {})
                    if li.get("l2_ok") and (li.get("spread_bps", 0.0) > 25.0 or li.get("buy_slip_bps", 0.0) > 30.0):
                        actions.append(f"[L2-SKIP BUY] {sym}: Spread/Slip zu hoch"); continue
                    fill_px = li.get("buy_vwap") if (li.get("l2_ok") and pd.notna(li.get("buy_vwap"))) else px * 1.0005
                    if mode == "live-binance":
                        resp = ExchangeBroker.place_binance_order(sym, "BUY", buy_usd, 0.0, cfg)
                        actions.append(f"[LIVE-BINANCE BUY] {sym} ${buy_usd:,.2f} | ID={resp.get('orderId')}")
                    elif mode == "live-kraken":
                        resp = ExchangeBroker.place_kraken_order(sym, "buy", buy_usd / fill_px, cfg)
                        actions.append(f"[LIVE-KRAKEN BUY] {sym} ${buy_usd:,.2f} | {resp.get('result')}")
                    fee = buy_usd * fee_rate; nu = max(0.0, buy_usd - fee) / fill_px
                    vol_d = float(engine.vols[sym].iloc[-1]) / np.sqrt(365.0) if sym in engine.vols else 0.04
                    stop_px = fill_px * (1.0 - 3.0 * vol_d)
                    self.state["cash_usd"] -= buy_usd
                    self.state["positions"].setdefault(sym, []).append({"date": now.strftime("%Y-%m-%d %H:%M:%S"), "units": nu, "cost_per_unit": fill_px * (1.0 + fee_rate), "stop_price": stop_px})
                    self.state["fees_paid"] += fee; self.state["trades"] += 1
                    actions.append(f"[FILL BUY  {mode.upper()}] {sym}: {nu:.6f} @ ${fill_px:,.2f} (${buy_usd:,.2f}, Stop ${stop_px:,.2f})")

        eq_after = self.mark_to_market(prices)
        self.state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        self.state["history"].append({"ts": self.state["last_run"], "equity": round(eq_after, 2), "cash": round(self.state["cash_usd"], 2)})
        self.state["history"] = self.state["history"][-500:]
        self.save_state()
        if log_events:
            if actions:
                for act in actions: log_feed("TRADE", f"[{mode.upper()}] {act}", {"equity": round(eq_after, 2)})
            else:
                log_feed("HEARTBEAT", f"[{mode.upper()}] Equity=${eq_after:,.2f} | Cash=${self.state['cash_usd']:,.2f} | BTC>SMA200={btc_bull} | Breite={breadth:.0%} | Keine Order (Kapitalschutz/Toleranz).")
        fng_v = float(engine.fng.iloc[-1]) if (engine.fng is not None and len(engine.fng) > 0) else np.nan
        return {"equity": eq_after, "cash": self.state["cash_usd"], "breadth": breadth, "max_gross": max_gross,
                "btc_bull": btc_bull, "fw": fw, "ics": ics, "tgt_w": tgt_w, "fng_v": fng_v,
                "actions": actions, "cs": cs, "intel": live_intel_map, "prices": prices, "as_of": as_of}

def print_ultra_reasoning(as_of, breadth, btc_bull, max_gross, fw, ics, cs, intel, fng_v, tgt_w):
    btc_li = intel.get("BTC-USD", {})
    btc_dvol = btc_li.get("dvol", np.nan)
    btc_yz = float(cs.loc["BTC-USD", "vol"]) * 100.0 if "BTC-USD" in cs.index else np.nan
    vrp = (btc_dvol - btc_yz) if pd.notna(btc_dvol) and pd.notna(btc_yz) else np.nan
    fr_v = btc_li.get("funding_8h", np.nan)
    fr_apr = fr_v * 3.0 * 365.0 * 100.0 if pd.notna(fr_v) else np.nan
    carry_lbl = f"Carry-Opp ({fr_apr:+.1f}% APR)" if (pd.notna(fr_apr) and fr_apr > 12.0) else (f"Short-Crowding ({fr_apr:+.1f}% APR)" if (pd.notna(fr_apr) and fr_apr < -5.0) else f"Neutral ({fr_apr:+.1f}% APR)")
    print("\n--- [ULTRA REASONING TRACE: 6-STUFEN SELBSTPRUEFUNG] ---")
    print(f"  [1] MIKROSTRUKTUR : BTC Spread={btc_li.get('spread_bps', np.nan):.2f}bps | OBI={btc_li.get('obi', 0.0):+.2f} | Slip={btc_li.get('buy_slip_bps', np.nan):.2f}bps | Fund={carry_lbl} | VRP={vrp:+.1f}%")
    print(f"  [2] REGIME & MAKRO: BTC>SMA200={'JA' if btc_bull else 'NEIN'} | Marktbreite={breadth:.0%} | Fear&Greed={fng_v:.0f} | Max-Gross-Limit={max_gross:.0%}")
    f_names = ["f_trend", "f_relbtc", "f_pullback", "f_ridge", "f_lowvol"]
    ic_str = " | ".join([f"{k.replace('f_','').upper()}: {fw[i]:.0%}(IC={ics.get(k,0.0):+.2f})" for i, k in enumerate(f_names)])
    print(f"  [3] IC-SELBSTAUDIT: {ic_str}")
    vetos = []
    if not btc_bull: vetos.append("BTC<SMA200 (Krypto-Winter-Schutz)")
    if breadth < 0.25: vetos.append(f"Marktbreite {breadth:.0%} < 25%")
    if pd.notna(fng_v) and fng_v <= 20: vetos.append(f"Extreme Fear ({fng_v:.0f})")
    if (not cs.empty) and cs["vol_shock"].mean() > 0.30: vetos.append("EWMA-Vol-Schock")
    print(f"  [4] FALSIFIKATION : {', '.join(vetos) if vetos else 'Keine aktiven Makro-Vetos'}")
    top_pos = [(s, w) for s, w in sorted(tgt_w.items(), key=lambda kv: kv[1], reverse=True) if w > 0]
    cash_w = max(0.0, 1.0 - sum(tgt_w.values()))
    pos_txt = ", ".join([f"{s} {w:.1%}" for s, w in top_pos]) if top_pos else "KEINE (100% Kapitalschutz)"
    print(f"  [5] OPTIMIERUNG   : Ledoit-Wolf + CVaR -> {pos_txt} | Cash={cash_w:.1%}")

def monte_carlo_pvalue(allocs: pd.Series, mkt_ret: pd.Series, actual_sh: float, bars_per_year: float, block_sz: int = 30, n_sim: int = 400) -> float:
    if (allocs.abs() < 1e-9).all(): return 1.0
    rng, av, mv = np.random.default_rng(42), allocs.values, mkt_ret.values
    blocks = [av[i:i + max(5, block_sz)] for i in range(0, len(av), max(5, block_sz))]
    beats = sum(1 for _ in range(n_sim) if ((lambda sr: (sr.mean() / sr.std()) * np.sqrt(bars_per_year) >= actual_sh if sr.std() > 1e-9 else False)(np.concatenate([blocks[i] for i in rng.permutation(len(blocks))])[:len(av)] * mv)))
    return beats / n_sim

def save_audit(ticker: str, rep: dict):
    ds = rep["date"].strftime("%Y-%m-%d %H:%M:%S")
    p = Path("agent_predictions_log.csv")
    row = pd.DataFrame([{"date": ds, "ticker": ticker, "tf": rep["tf"], "price_usd": round(rep["price"], 4), "regime": rep["regime"],
                         "score": round(rep["total_score"], 2), "conviction": round(rep["conviction"], 2), "verdict": rep["verdict"],
                         "target_alloc": round(rep["target_alloc"], 2), "cal_exp_h": round(rep["cal_exp"], 4), "invalidation_usd": round(rep["invalidation_price"], 4)}])
    if p.exists():
        ex = pd.read_csv(p)
        if "tf" not in ex.columns: ex["tf"] = "1d"
        pd.concat([ex[~((ex["date"] == ds) & (ex["ticker"] == ticker) & (ex["tf"] == rep["tf"]))], row], ignore_index=True).to_csv(p, index=False)
    else:
        row.to_csv(p, index=False)
    payload = {"date": ds, "ticker": ticker, "tf": rep["tf"], "price": round(rep["price"], 4), "score": round(rep["total_score"], 2), "alloc": rep["target_alloc"]}
    payload["sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    with open("audit_trail.jsonl", "a", encoding="utf-8") as f: f.write(json.dumps(payload) + "\n")

def run_self_test():
    print("\n" + "=" * 92 + "\n APEX-QUANT v9 WORKSTATION SELBSTTEST (5/5 GATES)\n" + "=" * 92)
    rng = np.random.default_rng(777)
    dates = pd.date_range("2015-01-01", pd.Timestamp.now().normalize(), freq="D")
    n = len(dates)
    c = 300 * np.exp(np.cumsum(0.0007 + 0.0028 * np.sin(2 * np.pi * np.arange(n) / 1460.0) + rng.normal(0, 0.032, n)))
    df_ok = pd.DataFrame({"Open": c * 0.998, "Close": c, "High": c * 1.02, "Low": c * 0.98, "Volume": rng.lognormal(18, 0.5, n),
                          "SP500": 2000 * np.exp(np.cumsum(0.0003 + rng.normal(0, 0.01, n))),
                          "DXY": 95 * np.exp(np.cumsum(rng.normal(0, 0.004, n))),
                          "TNX": np.clip(2.5 + np.cumsum(rng.normal(0, 0.02, n)), 0.5, 6.0)}, index=dates)
    assert validate_market_data(df_ok, "SYN-OK", "1d") == []
    b1 = df_ok.copy(); b1.iloc[-3, 1] = 0.0; assert len(validate_market_data(b1, "Z", "1d")) > 0
    b2 = df_ok.copy(); b2.iloc[-5, 1] *= 3.0; assert len(validate_market_data(b2, "S", "1d")) > 0
    assert len(validate_market_data(df_ok.iloc[:-15], "T", "1d")) > 0
    print("  [PASS] Gate 1: Datenvalidierung, Null-Preis, Glitch-Spike & Staleness-Notaus.")

    d1s = pd.date_range(pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(seconds=3999), periods=4000, freq="s")
    c1s = 65000.0 * np.exp(np.cumsum(rng.normal(0, 0.00015, 4000)))
    df_1s = pd.DataFrame({"Open": c1s, "Close": c1s, "High": c1s * 1.0001, "Low": c1s * 0.9999, "Volume": rng.lognormal(5, 0.5, 4000)}, index=d1s)
    rep_1s = SelfAdaptingCryptoAgent(df_1s, "BTC-USD", "1s").analyze(fee_rate=0.001, slippage=0.0005, enforce_cost_gate=True)
    assert rep_1s["target_alloc"] == 0.0 and rep_1s["cost_blocked"]
    print("  [PASS] Gate 2: 1s-Skala & H0-Kosten-Gate blockiert Rausch-Trading (0% Cash).")

    cov = QuantCore.ledoit_wolf_cov(pd.DataFrame(rng.normal(0, 0.03, (90, 5)), columns=list("ABCDE")))
    assert np.all(np.linalg.eigvalsh(cov) > 0)
    w_opt = QuantCore.optimize_portfolio(np.array([0.8, 0.5, 0.2, -0.3, 0.6]), cov, max_w=0.35, max_gross=1.0, cvar_limit=0.045)
    assert w_opt.sum() <= 1.0001 and (2.063 * np.sqrt(w_opt @ cov @ w_opt)) <= 0.0451
    vwap, _, dep = simulate_l2_fill([["100.0", "20"], ["100.5", "50"]], 3000.0, 99.9)
    assert abs(vwap - 100.16666) < 0.01 and dep == 7025.0
    print("  [PASS] Gate 3: Yang-Zhang, Bayes, Ledoit-Wolf, CVaR-Optimierer & L2-VWAP.")

    tmp_state = Path("_selftest_state.json")
    if tmp_state.exists(): tmp_state.unlink()
    syn_univ = {"BTC-USD": df_ok.iloc[-600:], "ETH-USD": df_ok.iloc[-600:].assign(Close=df_ok["Close"].iloc[-600:] * 0.05)}
    eng = ApexPortfolioEngine(syn_univ, horizon=21)
    tr = AutonomousTrader(state_path=tmp_state, initial_capital=10000.0)
    try:
        tr.run_cycle(mode="live-binance", confirm_live=False, engine=eng, live_intel_map={}, log_events=False)
        assert False
    except PermissionError:
        pass
    out = tr.run_cycle(mode="paper", engine=eng, log_events=False, live_intel_map={"BTC-USD": {"l2_ok": True, "mid": float(df_ok["Close"].iloc[-1]), "spread_bps": 1.0, "obi": 0.2, "buy_vwap": float(df_ok["Close"].iloc[-1])*1.0002, "sell_vwap": float(df_ok["Close"].iloc[-1])*0.9998, "buy_slip_bps": 2.0, "sell_slip_bps": 2.0}, "ETH-USD": {"l2_ok": False}})
    assert tmp_state.exists() and out["equity"] > 9900.0
    tmp_state.unlink()
    print("  [PASS] Gate 4: Autonome Paper-Execution, 5-Faktor-IC-Audit & Live-Sicherheits-Lock.")

    ag = SelfAdaptingCryptoAgent(df_ok, "BTC-USD", "1d")
    bt = ag.run_surgical_backtest(lookback_years=5, adaptive=True)
    r_a, dd_a, _, cal_a = perf_metrics(bt["equity"]["agent_eq"], ag.bars_per_year)
    r_b, dd_b, _, cal_b = perf_metrics(bt["equity"]["bh_eq"], ag.bars_per_year)
    print(f"  [PASS] Gate 5 (5J-Zyklus): Agent {r_a:+.1%} (DD {dd_a:.1%}, Calmar {cal_a:.2f}) vs B&H {r_b:+.1%} (DD {dd_b:.1%}, Calmar {cal_b:.2f})")
    print("=" * 92 + "\n")

def run_scan():
    cfg = load_cfg()
    univ = cfg.get("active_universe", UNIVERSE)
    print(f"Scanne {len(univ)} aktive Assets + Makro + L2-Orderbuecher + OKX Funding + Deribit DVOL...")
    data = fetch_universe_daily(univ, start="2020-01-01")
    macro, fng = load_macro_bundle()
    eng = ApexPortfolioEngine(data, macro, fng, horizon=21)
    as_of = eng.btc.index[-1]
    beta, fw, ics = eng.fit_and_eval_now(as_of, adaptive=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        intel = dict(ex.map(lambda s: (s, fetch_live_intel(s, float(cfg.get("order_usd", 5000.0)))), list(eng.panel.keys())))
    tgt_w, cs, breadth, max_gross, btc_bull = eng.build_target_weights(as_of, beta, fw, intel, cvar_limit=float(cfg.get("cvar_limit", 0.055)))
    fng_v = float(fng.iloc[-1]) if fng is not None and len(fng) > 0 else np.nan
    print("\n" + "=" * 112)
    print(f" APEX-QUANT v9 LIVE-MARKTSCANNER | Stand: {as_of.strftime('%Y-%m-%d')} | Assets: {len(data)}/{len(univ)}")
    print("=" * 112)
    print(f"  {'Ticker':<9} {'Preis':>10} {'Alpha':>7} {'Trend':>7} {'Pullbk':>7} {'YZ-Vol':>7} {'Spread':>8} {'OBI':>7} {'FundAPR':>9} {'Ziel-Gew'}")
    print("  " + "-" * 106)
    for sym, r in cs.sort_values("alpha", ascending=False).iterrows():
        sp_v = r["spread_bps"]
        sp_s = (f"{sp_v:>5.2f}bp" if sp_v < 1.0 else f"{sp_v:>5.1f}bp") if pd.notna(sp_v) else "    n/a"
        obi_s = f"{intel[sym]['obi']:>+6.2f}" if intel.get(sym, {}).get("l2_ok") else "   n/a"
        fr_apr = r["funding_8h"] * 3.0 * 365.0 * 100.0 if pd.notna(r["funding_8h"]) else np.nan
        fr_s = f"{fr_apr:>+7.1f}%" if pd.notna(fr_apr) else "     n/a"
        print(f"  {sym:<9} ${r['price']:>9,.2f} {r['alpha']:>+6.2f} {r['f_trend']:>+6.2f} {r['f_pullback']:>+6.2f} {r['vol']:>6.1%} {sp_s:>8} {obi_s:>7} {fr_s:>9} {r['target_w']:>8.1%}")
    cash_w = max(0.0, 1.0 - sum(tgt_w.values()))
    print(f"  {'CASH-USD':<9} {'$1.00':>10} {'--':>7} {'--':>7} {'--':>7} {'0.0%':>7} {'--':>8} {'--':>7} {'--':>9} {cash_w:>8.1%}")
    print_ultra_reasoning(as_of, breadth, btc_bull, max_gross, fw, ics, cs, intel, fng_v, tgt_w)
    print("=" * 112 + "\n")

def run_derivatives_monitor():
    cfg = load_cfg()
    univ = cfg.get("active_universe", UNIVERSE)
    print(f"Lade Live-Orderbuecher, OKX Funding & Deribit DVOL fuer {len(univ)} Coins...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        intel = dict(ex.map(lambda s: (s, fetch_live_intel(s, float(cfg.get("order_usd", 5000.0)))), univ))
    print("\n" + "=" * 106)
    print(f" DERIVATE-, ORDERBUCH- & LIQUIDITAETS-MONITOR (Ordergroesse: ${cfg.get('order_usd', 5000.0):,.0f})")
    print("=" * 106)
    print(f"  {'Ticker':<9} {'Mid-Preis':>11} {'Spread':>8} {'Slip($)':>8} {'OBI-20':>7} {'BidTiefe':>11} {'AskTiefe':>11} {'FundAPR':>9} {'DVOL/Signal'}")
    print("  " + "-" * 100)
    for s in univ:
        li = intel.get(s, {})
        mid = li.get("mid", np.nan)
        sp = li.get("spread_bps", np.nan)
        sl = li.get("buy_slip_bps", np.nan)
        fr = li.get("funding_8h", np.nan)
        apr = fr * 3.0 * 365.0 * 100.0 if pd.notna(fr) else np.nan
        dv = li.get("dvol", np.nan)
        sig = f"DVOL {dv:.1f}%" if pd.notna(dv) else ("Carry-Opp" if (pd.notna(apr) and apr > 12.0) else ("Squeeze-Fuel" if (pd.notna(apr) and apr < -10.0) else "Normal"))
        print(f"  {s:<9} ${mid:>10,.2f} {sp:>6.2f}bp {sl:>6.2f}bp {li.get('obi',0):>+6.2f} ${li.get('bid_depth_usd',0):>9,.0f} ${li.get('ask_depth_usd',0):>9,.0f} {apr:>+7.1f}%  {sig}")
    print("=" * 106 + "\n")

def run_portfolio_view():
    cfg = load_cfg()
    tr = AutonomousTrader(STATE_FILE)
    st = tr.state
    syms = list(st["positions"].keys())
    with ThreadPoolExecutor(max_workers=6) as ex:
        intel = dict(ex.map(lambda s: (s, fetch_live_intel(s, 5000.0)), syms)) if syms else {}
    prices = {s: intel[s]["mid"] for s in syms if pd.notna(intel[s].get("mid"))}
    eq = tr.mark_to_market(prices)
    print("\n" + "=" * 96)
    print(" PORTFOLIO-, POSITIONS- & DE-STEUER-STATUS")
    print("=" * 96)
    print(f"  Gesamt-Equity   : ${eq:,.2f}  | Cash: ${st['cash_usd']:,.2f} | Peak: ${st['peak_equity']:,.2f}")
    print(f"  Kumulierte Daten: Trades={st['trades']} | Fees=${st['fees_paid']:,.2f} | Steuer=${st['tax_paid']:,.2f} | Verlusttopf=${st['loss_pool']:,.2f}")
    print(f"  Steuer-Settings : DE-Satz={cfg['tax_rate']:.0%} | FIFO-Haltefrist-Schutz={'AN' if cfg['tax_aware'] else 'AUS'} | Halted={st['halted']}")
    if st["positions"]:
        print("\n  Offene Positionen:")
        for sym, lots in st["positions"].items():
            u = sum(float(l["units"]) for l in lots)
            cost_u = sum(float(l["units"]) * float(l["cost_per_unit"]) for l in lots) / max(u, 1e-12)
            px = prices.get(sym, cost_u)
            pnl = (px - cost_u) * u
            stop_px = max(float(l.get("stop_price", 0.0)) for l in lots)
            oldest_d = min((pd.Timestamp.utcnow().tz_localize(None) - pd.Timestamp(l["date"])).total_seconds() / 86400.0 for l in lots)
            print(f"    * {sym:<9}: {u:.6f} Stk | Einstand ${cost_u:,.2f} | Aktuell ${px:,.2f} | PnL {pnl:+,.2f} ({(px/cost_u-1):+.1%}) | Stop ${stop_px:,.2f} | Alter {oldest_d:.0f}T")
    else:
        print("\n  Offene Positionen: Keine (100% Cash-Schutz).")
    feed = read_feed(8)
    if feed:
        print("\n  Letzte Aktivitaeten (Live-Feed):")
        for ev in feed:
            print(f"    [{ev['ts']}] {ev['kind']}: {ev['msg']}")
    print("=" * 96 + "\n")

def run_dashboard():
    cfg = load_cfg()
    tr = AutonomousTrader(STATE_FILE)
    data = fetch_universe_daily(cfg.get("active_universe", UNIVERSE), start="2020-01-01")
    macro, fng = load_macro_bundle()
    eng = ApexPortfolioEngine(data, macro, fng, horizon=21)
    as_of = eng.btc.index[-1]
    beta, fw, ics = eng.fit_and_eval_now(as_of, adaptive=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        intel = dict(ex.map(lambda s: (s, fetch_live_intel(s, float(cfg.get("order_usd", 5000.0)))), list(eng.panel.keys())))
    tgt_w, cs, breadth, max_gross, btc_bull = eng.build_target_weights(as_of, beta, fw, intel)
    prices = {s: (intel[s]["mid"] if pd.notna(intel[s].get("mid")) else float(eng.closes[s].loc[:as_of].iloc[-1])) for s in eng.closes}
    eq = tr.mark_to_market(prices)
    fng_v = float(fng.iloc[-1]) if fng is not None and len(fng) > 0 else np.nan
    print("\n" + "=" * 102)
    print(f" APEX-QUANT v9 LIVE-DASHBOARD | {pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 102)
    print(f"  Konto   : Equity=${eq:,.2f} | Cash=${tr.state['cash_usd']:,.2f} | Pos={len(tr.state['positions'])} | Trades={tr.state['trades']} | Halted={tr.state['halted']}")
    print(f"  Markt   : BTC=${prices.get('BTC-USD',0):,.2f} | BTC>SMA200={'JA' if btc_bull else 'NEIN'} | Breite={breadth:.0%} | F&G={fng_v:.0f} | MaxGross={max_gross:.0%}")
    print(f"  APIs    : KI={cfg['llm'].get('provider') or 'Aus'} (OK={cfg['llm'].get('verified')}) | Binance={cfg['exchanges'].get('binance_verified')} | Kraken={cfg['exchanges'].get('kraken_verified')}")
    print("\n  Top-5 Assets nach aktuellem Ultra-Alpha:")
    for sym, r in cs.sort_values("alpha", ascending=False).head(5).iterrows():
        print(f"    {sym:<9} ${r['price']:>9,.2f} | Alpha={r['alpha']:>+5.2f} | YZ-Vol={r['vol']:>5.1%} | >SMA200={str(r['above_sma200']):<5} | Ziel={r['target_w']:>5.1%}")
    feed = read_feed(6)
    print("\n  Live Trade- & Event-Feed:")
    if feed:
        for ev in feed: print(f"    [{ev['ts']}] {ev['msg']}")
    else:
        print("    (Noch keine Events im Feed)")
    print("=" * 102 + "\n")

def build_live_context_snapshot(cfg: dict) -> tuple[str, ApexPortfolioEngine, dict, dict]:
    univ = cfg.get("active_universe", UNIVERSE)
    data = fetch_universe_daily(univ, start="2020-01-01")
    macro, fng = load_macro_bundle()
    eng = ApexPortfolioEngine(data, macro, fng, horizon=21)
    as_of = eng.btc.index[-1]
    beta, fw, ics = eng.fit_and_eval_now(as_of, adaptive=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        intel = dict(ex.map(lambda s: (s, fetch_live_intel(s, float(cfg.get("order_usd", 5000.0)))), list(eng.panel.keys())))
    tgt_w, cs, breadth, max_gross, btc_bull = eng.build_target_weights(as_of, beta, fw, intel)
    tr = AutonomousTrader(STATE_FILE)
    prices = {s: (intel[s]["mid"] if pd.notna(intel[s].get("mid")) else float(eng.closes[s].loc[:as_of].iloc[-1])) for s in eng.closes}
    eq = tr.mark_to_market(prices)
    fng_v = float(fng.iloc[-1]) if fng is not None and len(fng) > 0 else np.nan
    top_txt = "; ".join([f"{s}: ${r['price']:,.2f}, alpha={r['alpha']:+.2f}, vol={r['vol']:.0%}, ziel={r['target_w']:.0%}" for s, r in cs.sort_values("alpha", ascending=False).head(6).iterrows()])
    feed_txt = " | ".join([f"[{e['ts']}] {e['msg']}" for e in read_feed(6)])
    ctx = (f"Stand={as_of.strftime('%Y-%m-%d')} | Equity=${eq:,.2f} | Cash=${tr.state['cash_usd']:,.2f} | Pos={list(tr.state['positions'].keys())} | "
           f"BTC=${prices.get('BTC-USD',0):,.2f} | BTC>SMA200={btc_bull} | Breite={breadth:.0%} | F&G={fng_v:.0f} | MaxGross={max_gross:.0%} | "
           f"ICs={ {k: round(v,2) for k,v in ics.items()} } | Top6=[{top_txt}] | LetzteEvents=[{feed_txt}]")
    return ctx, eng, intel, {"beta": beta, "fw": fw, "ics": ics, "cs": cs, "breadth": breadth, "max_gross": max_gross, "btc_bull": btc_bull, "tgt_w": tgt_w, "eq": eq, "cash": tr.state["cash_usd"], "fng": fng_v}

def run_chat_mode():
    cfg = load_cfg()
    lm = cfg.get("llm", {})
    has_llm = bool(lm.get("verified") and lm.get("provider"))
    print("\n" + "=" * 98)
    print(" APEX-QUANT v9 MODE [10]: LIVE TRADING-CHAT & KI-KOMMANDOBRUECKE")
    print(f" KI-Status: {'VERBUNDEN -> ' + lm.get('provider','') + ' / ' + lm.get('model','') if has_llm else 'OFFLINE (Quant-Assistent aktiv | KI in Menue [6] anschliessbar)'}")
    print(" Befehle: /feed (Live-Trades), /scan, /trade (1x Paper), /auto [sek] (Live-Ticker), /status, /exit")
    print("=" * 98)
    feed = read_feed(10)
    if feed:
        print("\n--- LETZTE LIVE-EVENTS & TRADES DES AGENTEN ---")
        for ev in feed: print(f"  [{ev['ts']}] {ev['kind']}: {ev['msg']}")
    print("\nLade aktuellen Live-Marktkontext...")
    ctx, eng, intel, snap = build_live_context_snapshot(cfg)
    print(f"Bereit! Equity=${snap['eq']:,.2f} | Cash=${snap['cash']:,.2f} | BTC>SMA200={snap['btc_bull']} | Marktbreite={snap['breadth']:.0%}\n")
    history = []

    while True:
        try:
            inp = input("Du > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nChat beendet.\n"); break
        if not inp: continue
        low = inp.lower()
        if low in ("/exit", "/quit", "exit", "quit"):
            print("Zurueck ins Menue...\n"); break
        if low == "/feed":
            evs = read_feed(15)
            print("\n--- LIVE AGENT-FEED ---")
            for ev in evs: print(f"  [{ev['ts']}] {ev['kind']}: {ev['msg']}")
            if not evs: print("  (Keine Events vorhanden)")
            print()
            continue
        if low == "/status":
            run_portfolio_view(); continue
        if low == "/scan":
            ctx, eng, intel, snap = build_live_context_snapshot(cfg)
            print(f"\n[SCAN-UPDATE] {ctx}\n"); continue
        if low == "/trade":
            tr = AutonomousTrader(STATE_FILE, initial_capital=10000.0)
            res = tr.run_cycle(mode="paper", tax_rate=float(cfg.get("tax_rate", 0.42)), tax_aware=bool(cfg.get("tax_aware", True)), engine=eng, precomputed=(snap["beta"], snap["fw"], snap["ics"]), cfg=cfg)
            print(f"\n[AUTO-PAPER] Equity=${res['equity']:,.2f} | Cash=${res['cash']:,.2f}")
            for a_s in (res["actions"] or ["Keine Order (Kapitalschutz/Toleranz)."]): print(f"  -> {a_s}")
            print()
            ctx, eng, intel, snap = build_live_context_snapshot(cfg)
            continue
        if low.startswith("/auto"):
            parts = low.split()
            iv = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            print(f"\n[CHAT-LIVE-FEED] Autonomer Agent tickt alle {iv}s. Druecke [Q] oder [ESC] um im Chat weiterzuschreiben...\n")
            tr = AutonomousTrader(STATE_FILE, initial_capital=10000.0)
            while True:
                res = tr.run_cycle(mode="paper", tax_rate=float(cfg.get("tax_rate", 0.42)), tax_aware=bool(cfg.get("tax_aware", True)), engine=eng, precomputed=(snap["beta"], snap["fw"], snap["ics"]), cfg=cfg)
                ts_n = pd.Timestamp.utcnow().strftime("%H:%M:%S")
                acts = " | ".join(res["actions"]) if res["actions"] else f"Halten (Cash=${res['cash']:,.2f}, MaxGross={res['max_gross']:.0%})"
                print(f"  [{ts_n}] Equity=${res['equity']:,.2f} | BTC=${res['prices'].get('BTC-USD',0):,.2f} | {acts}")
                if smart_sleep_q(iv):
                    print("\n[CHAT-LIVE-FEED pausiert - du kannst jetzt wieder schreiben.]\n")
                    break
            continue

        if has_llm:
            sys_prompt = ("Du bist der integrierte KI-Co-Pilot von APEX-QUANT v9. Antworte praezise auf Deutsch, faktenbasiert, ohne Finanz-Hype. "
                          "Nutze ausschliesslich die echten Live-Marktdaten und Events aus dem Kontext:\n" + ctx)
            msgs = [{"role": "system", "content": sys_prompt}] + history[-6:] + [{"role": "user", "content": inp}]
            ok, ans, ms = call_llm(lm["provider"], lm["model"], lm["api_key"], msgs, lm.get("base_url", ""))
            if ok:
                print(f"\nKI+Agent ({ms:.0f}ms) > {ans}\n")
                history += [{"role": "user", "content": inp}, {"role": "assistant", "content": ans}]
                log_feed("CHAT-AI", f"User: {inp[:80]} -> AI: {ans[:120]}")
            else:
                print(f"\n[KI-FEHLER] {ans}\n")
        else:
            top3 = ", ".join([f"{s} (Alpha {r['alpha']:+.2f}, Vol {r['vol']:.0%})" for s, r in snap["cs"].sort_values("alpha", ascending=False).head(3).iterrows()])
            evs = read_feed(3)
            ev_s = " | ".join(e["msg"] for e in evs) if evs else "Keine"
            print(f"\nQuant-Agent > Live-Stand: Equity=${snap['eq']:,.2f}, Cash=${snap['cash']:,.2f}. "
                  f"BTC>SMA200={'JA' if snap['btc_bull'] else 'NEIN'}, Marktbreite={snap['breadth']:.0%}, Max-Gross={snap['max_gross']:.0%}, F&G={snap['fng']:.0f}. "
                  f"Top-3 Alpha: {top3}. Letzte Aktionen: {ev_s}. "
                  f"(Tipp: Fuer KI-Dialog in Menue [6] -> [1] einen LLM-Key anschliessen.)\n")

def run_prove(main_ticker: str = "BTC-USD"):
    cfg = load_cfg()
    univ = cfg.get("active_universe", UNIVERSE)
    print("\n" + "#" * 96 + "\n APEX-QUANT v9 ECHTHEITS- & BEWEIS-SUITE\n" + "#" * 96)
    bundle, fng = load_macro_bundle()
    data = fetch_universe_daily(univ, start="2020-01-01")
    print(f"\n[CHECK] Geladene Krypto-Assets: {len(data)}/{len(univ)} ({', '.join(sorted(data.keys()))})")
    eng = ApexPortfolioEngine(data, bundle, fng, horizon=21)
    print("\n[BEWEIS 1] PORTFOLIO-ABLATION (5J, 21T-REBAL, NETTO)")
    pbt_st = eng.run_portfolio_backtest(lookback_years=5, adaptive=False)
    pbt_ad = eng.run_portfolio_backtest(lookback_years=5, adaptive=True)
    eq_st, eq_ad = pbt_st["equity"], pbt_ad["equity"]
    r_bt, dd_bt, sh_bt, cal_bt = perf_metrics(eq_ad["btc_eq"], 365.0)
    r_ew, dd_ew, sh_ew, cal_ew = perf_metrics(eq_ad["ew_eq"], 365.0)
    r_st, dd_st, sh_st, cal_st = perf_metrics(eq_st["apex_eq"], 365.0)
    r_ad, dd_ad, sh_ad, cal_ad = perf_metrics(eq_ad["apex_eq"], 365.0)
    print(f"  Trades: {pbt_ad['trades']} | Fees: ${pbt_ad['fees_paid']:,.0f} | Ridge-Vetos: {pbt_ad['ridge_vetos']} | Avg Invest: {eq_ad['invest'].mean():.0%}")
    print(f"  BTC Buy & Hold         : {r_bt:>+8.1%} | Max-DD: {dd_bt:>6.1%} | Sharpe: {sh_bt:>5.2f} | Calmar: {cal_bt:>5.2f}")
    print(f"  Equal-Weight ({len(eng.data)} Coins): {r_ew:>+8.1%} | Max-DD: {dd_ew:>6.1%} | Sharpe: {sh_ew:>5.2f} | Calmar: {cal_ew:>5.2f}")
    print(f"  APEX Statisch (Netto)  : {r_st:>+8.1%} | Max-DD: {dd_st:>6.1%} | Sharpe: {sh_st:>5.2f} | Calmar: {cal_st:>5.2f}")
    print(f"  APEX Ultra Adaptiv     : {r_ad:>+8.1%} | Max-DD: {dd_ad:>6.1%} | Sharpe: {sh_ad:>5.2f} | Calmar: {cal_ad:>5.2f}")
    print("\n[BEWEIS 2] OUT-OF-SAMPLE PROGNOSEKRAFT")
    oos = pbt_ad["oos"]
    if not oos.empty:
        ic_a, p_a = spearman_ic(oos["alpha"].values, oos["fwd_ret"].values)
        ic_r, p_r = spearman_ic(oos["f_ridge"].values, oos["fwd_ret"].values)
        ic_v, p_v = spearman_ic(oos["pred_vol"].values, oos["fwd_vol"].values)
        bull_r, bear_r = oos.loc[oos["above_sma200"], "fwd_ret"], oos.loc[~oos["above_sma200"], "fwd_ret"]
        print(f"  OOS n={len(oos)} | Ultra-Alpha IC={ic_a:+.3f} (p={p_a:.4f}) | Raw-Ridge IC={ic_r:+.3f} (p={p_r:.4f}) | YZ-Vol IC={ic_v:+.3f} (p={p_v:.2e})")
        if len(bull_r) > 10 and len(bear_r) > 10:
            print(f"  SMA200-Regime: Bull-21T {bull_r.mean():+.2%} vs Bear-21T {bear_r.mean():+.2%}")
    print(f"\n[BEWEIS 3] MULTI-SKALEN ({main_ticker}: 1s -> 1mo)")
    print(f"  {'TF':<5} {'Bars':>6} {'IC(p)':>14} {'Brutto-Sh':>10} {'Netto-Sh':>10} {'Netto-Ret':>10} {'B&H-Ret':>9} {'H0-Block':>9}")
    print("  " + "-" * 86)
    for tf in ["1s", "1m", "5m", "15m", "1h", "1d", "1wk", "1mo"]:
        try:
            df, _ = build_asset_df(main_ticker, tf, bundle if TF_CFG[tf]["sec"] >= 86400 else None)
            errs = validate_market_data(df, main_ticker, tf, check_staleness=False)
            if errs: print(f"  {tf:<5} -- {errs[0]}"); continue
            ag = SelfAdaptingCryptoAgent(df, main_ticker, tf, fng if tf == "1d" else None)
            lb = None if tf in ("1wk", "1mo") or TF_CFG[tf]["sec"] < 86400 else 5
            bt_gr = ag.run_surgical_backtest(lookback_years=lb, adaptive=True, fee_rate=0.0, slippage=0.0, enforce_cost_gate=False)
            bt_nt = ag.run_surgical_backtest(lookback_years=lb, adaptive=True, fee_rate=0.0010, slippage=0.0002, enforce_cost_gate=True)
            _, _, sh_gr, _ = perf_metrics(bt_gr["equity"]["agent_eq"], ag.bars_per_year)
            rn, _, sh_nt, _ = perf_metrics(bt_nt["equity"]["agent_eq"], ag.bars_per_year)
            rb, _, _, _ = perf_metrics(bt_nt["equity"]["bh_eq"], ag.bars_per_year)
            sigs = bt_gr["signals"]
            ic_s, p_s = spearman_ic(sigs["score"].values, sigs["fwd_ret_h"].values) if len(sigs) >= 10 else (0.0, 1.0)
            print(f"  {tf:<5} {len(df):>6} {ic_s:+.2f}({p_s:.2f}) {sh_gr:>10.2f} {sh_nt:>10.2f} {rn:>+9.1%} {rb:>+8.1%} {bt_nt['cost_blocks']:>9}")
        except Exception as e:
            print(f"  {tf:<5} Fehler: {e}")
    print("#" * 96 + "\n")

def print_single_report(ticker: str, tf: str, years: int, tax_rate: float, tax_aware: bool):
    cfg = load_cfg()
    print(f"Lade Marktdaten ({tf}) & Live-Mikrostruktur fuer {ticker}...")
    try:
        bundle, fng = load_macro_bundle() if TF_CFG[tf]["sec"] >= 86400 else ({}, None)
        df, macros = build_asset_df(ticker, tf, bundle)
    except Exception as e:
        print(f"\n[FEHLER] {e}\n"); return
    errs = validate_market_data(df, ticker, tf, check_staleness=True)
    if errs:
        print("\n" + "!" * 86 + "\n FAIL-SAFE NOTAUS AKTIVIERT:")
        for e in errs: print(f"   * {e}")
        print(" Handlungsempfehlung: SAFE MODE (Kein Trade).\n" + "!" * 86 + "\n"); return
    li = fetch_live_intel(ticker, float(cfg.get("order_usd", 5000.0))) if is_crypto_ticker(ticker) else None
    agent = SelfAdaptingCryptoAgent(df, ticker=ticker, tf=tf, fng=fng if tf == "1d" else None)
    rep = agent.analyze(adaptive=True, vol_target=float(cfg.get("vol_target", 0.60)), live_intel=li)
    save_audit(ticker, rep)
    fng_t = f" | Fear&Greed: {rep['stats']['fng_now']:.0f}" if rep["stats"]["fng_now"] is not None else ""
    l2_t = f" | Spread: {li['spread_bps']:.2f}bps OBI: {li['obi']:+.2f}" if (li and li.get("l2_ok")) else ""
    fr_t = f" | OKX-Fund: {li['funding_8h']*100:+.3f}%" if (li and pd.notna(li.get("funding_8h"))) else ""
    dv_t = f" | DVOL: {li['dvol']:.1f}%" if (li and pd.notna(li.get("dvol"))) else ""
    print("\n" + "=" * 92)
    print(f" APEX-QUANT v9 DEEP-DIVE | {ticker} [{tf}] | Stand: {rep['date']}")
    print(f" Quellen: {', '.join(macros) if macros else 'OHLCV'}{fng_t}{l2_t}{fr_t}{dv_t}")
    print("=" * 92)
    print(f" Preis: ${rep['price']:,.2f} (SMA{agent.cfg['slow']}: ${rep['sma_slow_price']:,.2f}) | Notbremse (-8%): ${rep['invalidation_price']:,.2f}")
    print(f" Regime: {rep['regime']} (ER: {rep['er']:.2f}) | Urteil: {rep['verdict']}")
    print(f" Score: {rep['total_score']:+.1f} | Conviction: {rep['conviction']:.0%} | Allokation: {rep['target_alloc']:.0%} {ticker} / {1-rep['target_alloc']:.0%} Cash")
    for sc in rep["safety_caps"]: print(f" [FAIL-SAFE SCHUTZ]: {sc}")
    print("\n--- STRATEGIE-GEWICHTE ---")
    for k in rep["meta_weights"]: print(f"  {k:<15}: Gewicht {rep['meta_weights'][k]:>5.1%} | Alpha {rep['alpha_scores'][k]:>+6.1f}")
    print("\n--- ULTRA REASONING (H1 vs H0) ---")
    for r in rep["reasons"]: print(f"  * {r}")
    print(f"\n--- CHIRURGISCHER BACKTEST ({tf}) ---")
    lb = years if (tf == "1d" and years > 0) else None
    bt = agent.run_surgical_backtest(lookback_years=lb, adaptive=True, tax_rate=tax_rate, tax_aware=tax_aware)
    eq = bt["equity"]
    if not eq.empty:
        bh_r, bh_dd, bh_sh, bh_cal = perf_metrics(eq["bh_eq"], agent.bars_per_year)
        ag_r, ag_dd, ag_sh, ag_cal = perf_metrics(eq["agent_eq"], agent.bars_per_year)
        mkt_ret = agent.df["Close"].loc[eq.index].pct_change().fillna(0.0)
        alloc_held = eq["alloc"].shift(1).fillna(0.0)
        gross_sr = alloc_held * mkt_ret
        gross_sh = float((gross_sr.mean() / gross_sr.std()) * np.sqrt(agent.bars_per_year)) if gross_sr.std() > 1e-9 else 0.0
        pval = monte_carlo_pvalue(alloc_held, mkt_ret, gross_sh, agent.bars_per_year, agent.cfg["reb"], 400)
        print(f"  Trades: {bt['trades']} | Notaus: {bt['notaus_triggers']} | Fees: ${bt['fees_paid']:,.0f} | Steuer: ${bt['tax_paid']:,.0f} (FIFO: {bt['tax_blocked']})")
        print(f"  Steuerfrei (>365T): ${bt['taxfree_gains']:,.0f} | Kurzfristig: ${bt['taxable_gains']:,.0f} | Verlusttopf: ${bt['loss_pool_end']:,.0f}")
        print(f"  Buy & Hold  : {bh_r:>+8.1%} | Max-DD: {bh_dd:>6.1%} | Sharpe: {bh_sh:>5.2f} | Calmar: {bh_cal:>5.2f}")
        print(f"  Agent v9    : {ag_r:>+8.1%} | Max-DD: {ag_dd:>6.1%} | Sharpe: {ag_sh:>5.2f} | Calmar: {ag_cal:>5.2f} | Invest: {eq['alloc'].mean():.0%} | p={pval:.3f}")
    print("\n" + "=" * 92 + "\n")

def run_autonomous_trade(mode: str, confirm_live: bool, capital: float, tax_rate: float, tax_aware: bool, daemon: bool, interval: int):
    cfg = load_cfg()
    if mode.startswith("live-"):
        if not confirm_live: raise PermissionError("Live-Modus erfordert --confirm-live!")
        ok_ex, msg_ex = verify_exchange_connection(mode.split("-")[1], cfg)
        if not ok_ex:
            print(f"\n[LIVE-SPERRE] {msg_ex}\nBitte zuerst in Menue [6] gueltige Boersen-API-Keys anschliessen & pruefen!\n"); return
    trader = AutonomousTrader(STATE_FILE, initial_capital=capital)
    c_eng, c_ts, c_pre = None, 0.0, None
    try:
        while True:
            now_s = time.time()
            if c_eng is None or (now_s - c_ts) >= 900.0:
                cfg = load_cfg()
                data = fetch_universe_daily(cfg.get("active_universe", UNIVERSE), start="2020-01-01")
                macro, fng = load_macro_bundle()
                c_eng = ApexPortfolioEngine(data, macro, fng, horizon=21)
                c_pre = c_eng.fit_and_eval_now(c_eng.btc.index[-1], adaptive=True)
                c_ts = now_s
            res = trader.run_cycle(mode=mode, confirm_live=confirm_live, tax_rate=tax_rate, tax_aware=tax_aware, engine=c_eng, precomputed=c_pre, cfg=cfg)
            print("\n" + "=" * 96)
            print(f" APEX-QUANT v9 AUTONOMER ZYKLUS [{mode.upper()}] | {trader.state['last_run']}")
            print(f" Equity: ${res['equity']:,.2f} | Cash: ${res['cash']:,.2f} | Trades={trader.state['trades']} | Fees=${trader.state['fees_paid']:,.2f} | Halted={trader.state['halted']}")
            print("=" * 96)
            print_ultra_reasoning(res["as_of"], res["breadth"], res["btc_bull"], res["max_gross"], res["fw"], res["ics"], res["cs"], res["intel"], res["fng_v"], res["tgt_w"])
            print("\n--- [6] EXECUTION & POSITIONSBEWEGUNG ---")
            if res["actions"]:
                for act in res["actions"]: print(f"  -> {act}")
            else:
                print("  -> Keine Order ausgefuehrt (Ziel-Allokation erreicht / Kapitalschutz aktiv).")
            if trader.state["positions"]:
                print("\n  Aktive Positionen:")
                for sym, lots in trader.state["positions"].items():
                    u = sum(float(l["units"]) for l in lots)
                    px = res["prices"].get(sym, 0.0)
                    st = max(float(l.get("stop_price", 0.0)) for l in lots)
                    print(f"    * {sym:<9}: {u:.6f} Stk (${u*px:,.2f} = {(u*px)/max(res['equity'],1e-9):.1%}) | Stop: ${st:,.2f}")
            print(f"  [State in {STATE_FILE} | Feed in {FEED_FILE}]\n")
            if not daemon: break
            print(f"  [DAEMON] Naechster Live-Tick in {interval}s | Druecke [Q] oder [ESC] fuer zurueck ins Menue...")
            if smart_sleep_q(interval):
                print("\n  [DAEMON] Sauber gestoppt -> Zurueck ins Hauptmenue.\n")
                break
    except KeyboardInterrupt:
        print("\n  [DAEMON] Sauber gestoppt.\n")

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="APEX-QUANT v9 WORKSTATION")
    p.add_argument("ticker", nargs="?", default="BTC-USD")
    p.add_argument("--tf", default="1d", choices=list(TF_CFG.keys()))
    p.add_argument("--scan", action="store_true")
    p.add_argument("--derivatives", action="store_true")
    p.add_argument("--portfolio", action="store_true")
    p.add_argument("--dashboard", action="store_true")
    p.add_argument("--chat", action="store_true")
    p.add_argument("--feed", action="store_true")
    p.add_argument("--verify-llm", action="store_true")
    p.add_argument("--verify-exchange", default="")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--prove", action="store_true")
    p.add_argument("--trade", action="store_true")
    p.add_argument("--mode", default="paper", choices=["paper", "live-binance", "live-kraken"])
    p.add_argument("--confirm-live", action="store_true")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--capital", type=float, default=10000.0)
    p.add_argument("--reset-paper", action="store_true")
    p.add_argument("--tax", type=float, default=None)
    p.add_argument("--tax-aware", action="store_true")
    p.add_argument("--years", type=int, default=5)
    a = p.parse_args()
    cfg_now = load_cfg()
    tax_r = cfg_now["tax_rate"] if a.tax is None else a.tax
    tax_aw = bool(cfg_now["tax_aware"] or a.tax_aware)
    try:
        if a.reset_paper and STATE_FILE.exists():
            STATE_FILE.unlink(); print(f"[Reset] {STATE_FILE} geloescht.")
        if a.self_test: run_self_test()
        elif a.prove: run_prove(normalize_ticker(a.ticker))
        elif a.scan: run_scan()
        elif a.derivatives: run_derivatives_monitor()
        elif a.portfolio: run_portfolio_view()
        elif a.dashboard: run_dashboard()
        elif a.chat: run_chat_mode()
        elif a.feed:
            print("\n--- LIVE AGENT-FEED ---")
            for ev in read_feed(25): print(f"  [{ev['ts']}] {ev['kind']}: {ev['msg']}")
            print()
        elif a.verify_llm:
            ok, m = verify_llm_connection(cfg_now); print(f"\n[{'PASS' if ok else 'FAIL'}] {m}\n")
        elif a.verify_exchange:
            ok, m = verify_exchange_connection(a.verify_exchange.lower(), cfg_now); print(f"\n[{'PASS' if ok else 'FAIL'}] {m}\n")
        elif a.trade:
            run_autonomous_trade(a.mode, a.confirm_live, a.capital, tax_r, tax_aw, a.daemon, a.interval)
        else:
            print_single_report(normalize_ticker(a.ticker), a.tf, a.years, tax_r, tax_aw)
    except KeyboardInterrupt:
        print("\n[Abgebrochen]\n")
    except Exception as e:
        print(f"\n[FEHLER] {e}\n")
