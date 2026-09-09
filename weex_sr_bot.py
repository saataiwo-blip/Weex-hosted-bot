"""
WEEX Support/Resistance Breakout Bot
=====================================

Strategy:
- Looks back N candles to find the highest high (resistance) and lowest low (support)
- LONG when price closes above resistance with volume confirmation
- SHORT when price closes below support with volume confirmation
- SL is placed just beyond the broken support/resistance level (structure-based,
  not a flat %); TP is a multiple of that SL distance (REWARD_RISK_RATIO)

IMPORTANT — READ BEFORE RUNNING
--------------------------------
1. DRY_RUN defaults to True. In this mode the bot calls WEEX's official
   SIMULATED order endpoint (/capi/v3/sim/order) — real market data, fake
   money, no funds at risk. Do NOT flip DRY_RUN to False until you've
   watched it run for at least a few days and are happy with its decisions.
2. This is a starting framework, not a finished profitable strategy.
   Breakout strategies are prone to false breakouts — tune LOOKBACK,
   CONFIRMATION_CANDLES, and VOLUME_MULTIPLIER against your own backtests
   before trusting it with size.
3. WEEX's contract API (endpoints/params below) was current as of when this
   was built (Aug 2026) but exchanges change APIs without much notice.
   If you get auth or 404 errors, check https://www.weex.com/api-doc/contract/
   against the endpoints used here before assuming the strategy logic is broken.
4. Never commit your API keys. Use environment variables (see README).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import requests

# ============================== CONFIG ==============================

API_KEY = os.environ.get("WEEX_API_KEY", "")
API_SECRET = os.environ.get("WEEX_API_SECRET", "")
API_PASSPHRASE = os.environ.get("WEEX_API_PASSPHRASE", "")
BASE_URL = "https://api-contract.weex.com"

def candle_symbol(raw: str) -> str:
    """WEEX's V2 candles/market-data endpoint expects lowercase symbols with
    a 'cmt_' prefix, e.g. 'cmt_btcusdt'."""
    s = raw.strip().lower()
    if not s.startswith("cmt_"):
        s = "cmt_" + s
    return s


def order_symbol(raw: str) -> str:
    """WEEX's V3 order-placement endpoint expects plain uppercase symbols,
    e.g. 'BTCUSDT' — no 'cmt_' prefix, unlike the candles endpoint."""
    s = raw.strip().upper()
    if s.startswith("CMT_"):
        s = s[4:]
    return s


def to_sim_symbol(order_sym: str) -> str:
    """Converts a live-order symbol like 'BTCUSDT' to WEEX's demo/sim-order
    format, which inserts an 'S' before the quote currency: 'BTCSUSDT'."""
    s = order_sym.strip().upper()
    if s.endswith("USDT") and not s.endswith("SUSDT"):
        return s[:-4] + "SUSDT"
    return s


# Comma-separated list, e.g. WEEX_SYMBOLS=ONUSDT,BTCUSDT,SUIUSDT
# Each entry is stored once, then converted to whichever format a given
# endpoint needs (candles vs order placement use different conventions).
_env_symbols = [
    s.strip()
    for s in os.environ.get("WEEX_SYMBOLS", os.environ.get("WEEX_SYMBOL", "BTCUSDT")).split(",")
    if s.strip()
]

RISK_TIERS_NOTE = "Balance-tiered risk % replaced by per-coin fixed-margin sizing — see MARGIN_BY_SYMBOL / LEVERAGE_BY_SYMBOL"
STOP_LOSS_BUFFER_PCT = 0.3  # minimum buffer beyond the broken level, as a % — used for low-volatility coins
ATR_PERIOD = 14              # candles used to measure recent volatility
ATR_BUFFER_MULTIPLIER = 0.5  # SL buffer = max(STOP_LOSS_BUFFER_PCT, ATR × this) — widens the stop for volatile coins
REWARD_RISK_RATIO = 2.0     # TP distance = this × SL distance (structure-based, not flat %)

# ---- Fixed-margin position sizing (defaults — overridden by config.json if present) ----
_default_margin_by_symbol = {
    "BTCUSDT": 5.0,
    "SUIUSDT": 5.0,
    "ONUSDT": 5.0,
    "ETHUSDT": 5.0,
    "SOLUSDT": 5.0,
    "DOGEUSDT": 5.0,
    "ADAUSDT": 5.0,
    "AVAXUSDT": 5.0,
    "LINKUSDT": 5.0,
    "XRPUSDT": 5.0,
}
DEFAULT_MARGIN_USDT = 5.0  # used for any symbol not listed above
_default_leverage_by_symbol = {
    "BTCUSDT": 20,
    "SUIUSDT": 20,
    "ONUSDT": 20,
    "ETHUSDT": 20,
    "SOLUSDT": 20,
    "DOGEUSDT": 20,
    "ADAUSDT": 20,
    "AVAXUSDT": 20,
    "LINKUSDT": 20,
    "XRPUSDT": 20,
}
DEFAULT_LEVERAGE = 20  # used for any symbol not listed above

# ---- Load config.json (written by settings_app.py) if it exists — this is
# the easy way to change margin/leverage/coins without editing this file. ----
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: could not read config.json ({e}), using defaults.")
    return {}


# WEEX also lists non-crypto perpetuals (stocks, ETFs, metals, forex) using
# the same USDT-quoted format as crypto pairs — there's no reliable way to
# tell them apart from the ticker data alone, so known non-crypto base
# assets are excluded here by name. This list is best-effort and may need
# updating if WEEX adds new stock/commodity/forex-linked contracts.
NON_CRYPTO_BASE_ASSETS = {
    # precious metals / commodities
    "XAU", "XAG",
    # forex
    "CAD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD",
    # US stocks / leveraged stock ETFs seen on WEEX
    "MU", "ASML", "QQQ", "SPY", "SOXL", "SOXS", "MSFU", "MSFD", "AMZU", "AMZD",
    "TSLL", "TSLQ", "NVDL", "NVDS", "GLW", "KKR",
    # international stocks / ADRs
    "SKHYNIX", "SKHY", "SNDK", "SAMSUNG", "KIOXIA", "ANTA",
    # country/region ETFs
    "EWZ", "KORU",
    # pre-IPO / private company perpetuals
    "SPCX",
}


def is_crypto_symbol(order_sym: str) -> bool:
    """True unless the base asset (before USDT) is a known non-crypto
    instrument WEEX also lists (stocks, metals, forex, etc.)."""
    base = order_sym.upper().replace("USDT", "")
    return base not in NON_CRYPTO_BASE_ASSETS


def fetch_top_symbols_by_volume(client, top_n: int, tradable_filter: Optional[set] = None, crypto_only: bool = True):
    """Fetches 24h ticker data for all symbols, ranks by quote volume, and
    returns the top_n USDT-quoted symbols (uppercase, plain format e.g.
    'BTCUSDT'). If tradable_filter is given, only symbols in that set are
    considered — used to skip coins that chart fine but aren't actually
    orderable via the API. If crypto_only is True (default), known
    stock/commodity/forex-linked perpetuals are excluded — see
    NON_CRYPTO_BASE_ASSETS.

    Ticker symbols are normalized with order_symbol() before comparing
    against tradable_filter, since WEEX's ticker endpoint may return symbols
    in a different format (e.g. 'cmt_btcusdt') than the plain uppercase
    format the tradable-symbols list uses — without this, a format mismatch
    would silently filter out almost every coin."""
    raw = client.get_ticker_24hr()
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(data, list):
        raise ValueError(f"Unexpected ticker response shape: {type(data)}")

    log.info(f"Ticker endpoint returned {len(data)} raw entries.")

    ranked = []
    skipped_not_usdt = 0
    skipped_not_tradable = 0
    skipped_non_crypto = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_sym = str(item.get("symbol", ""))
        sym = order_symbol(raw_sym)  # normalizes away any cmt_ prefix, uppercases
        if not sym.endswith("USDT"):
            skipped_not_usdt += 1
            continue
        if crypto_only and not is_crypto_symbol(sym):
            skipped_non_crypto += 1
            continue
        if tradable_filter and sym not in tradable_filter:
            skipped_not_tradable += 1
            continue
        vol = item.get("quoteVolume", item.get("volume", 0))
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            vol = 0.0
        ranked.append((sym, vol))

    log.info(f"After filtering: {len(ranked)} candidates "
             f"(skipped {skipped_not_usdt} non-USDT, {skipped_non_crypto} non-crypto, {skipped_not_tradable} not API-tradable).")

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in ranked[:top_n]]


def reload_runtime_config():
    """Re-reads API keys (env vars) and config.json into the module's globals.
    Called at the start of run() so that when this module is imported once
    and run() is called repeatedly (e.g. from weex_bot_app.py's Start button),
    each run picks up whatever was most recently saved — not stale values
    from whenever the module first happened to be imported.

    Note: this does NOT resolve auto_top_n (top-N-by-volume) — that requires
    a live API call, so it's handled separately at the start of run(), after
    the WeexClient exists."""
    global API_KEY, API_SECRET, API_PASSPHRASE
    global RAW_SYMBOLS, MARGIN_BY_SYMBOL, LEVERAGE_BY_SYMBOL, SYMBOLS, ORDER_SYMBOL_MAP
    global AUTO_TOP_N, AUTO_TOP_SELECTED, DEFAULT_MARGIN_USDT, DEFAULT_LEVERAGE

    API_KEY = os.environ.get("WEEX_API_KEY", "")
    API_SECRET = os.environ.get("WEEX_API_SECRET", "")
    API_PASSPHRASE = os.environ.get("WEEX_API_PASSPHRASE", "")

    env_symbols = [
        s.strip()
        for s in os.environ.get("WEEX_SYMBOLS", os.environ.get("WEEX_SYMBOL", "BTCUSDT")).split(",")
        if s.strip()
    ]
    config = load_config()

    AUTO_TOP_N = config.get("auto_top_n")  # e.g. 50, or None/0 to disable
    # Optional: if set, only these coins (a subset of the fetched top-N) are
    # actually traded — lets the person pick specific coins out of the top-N
    # list instead of trading all of them automatically.
    AUTO_TOP_SELECTED = set(s.upper() for s in config.get("auto_top_selected", []))
    RAW_SYMBOLS = config.get("symbols", env_symbols)  # used when AUTO_TOP_N is not set
    MARGIN_BY_SYMBOL = {k.upper(): float(v) for k, v in config.get("margin_by_symbol", _default_margin_by_symbol).items()}
    LEVERAGE_BY_SYMBOL = {k.upper(): int(v) for k, v in config.get("leverage", _default_leverage_by_symbol).items()}
    DEFAULT_MARGIN_USDT = float(config.get("default_margin_usdt", DEFAULT_MARGIN_USDT))
    DEFAULT_LEVERAGE = int(config.get("default_leverage", DEFAULT_LEVERAGE))
    SYMBOLS = [candle_symbol(s) for s in RAW_SYMBOLS]
    ORDER_SYMBOL_MAP = {candle_symbol(s): order_symbol(s) for s in RAW_SYMBOLS}


def margin_for_symbol(order_sym: str) -> float:
    """USDT margin to use for this coin, from MARGIN_BY_SYMBOL."""
    return MARGIN_BY_SYMBOL.get(order_sym.upper(), DEFAULT_MARGIN_USDT)


# Initial load, so the module still works if run() is called directly or
# the script is executed standalone without going through the GUI app.
reload_runtime_config()

TIMEFRAME = "15m"          # candle interval for analysis
LOOKBACK = 20               # candles used to find support/resistance
CONFIRMATION_CANDLES = 1    # candles closing beyond the level before entry
VOLUME_MULTIPLIER = 1.3     # breakout candle volume vs recent average

POLL_SECONDS = 60           # how often to check for a new candle close
MAX_FETCH_WORKERS = 10       # how many coins' price data to fetch simultaneously (speeds up large coin lists)
# Max concurrent positions is now tiered by balance — see max_positions_for_balance()

DRY_RUN = True              # <-- flip to False only after paper testing

LOG_FILE = "weex_bot.log"

# Windows terminals often default to a legacy codepage (cp1252) that can't
# print every character an exchange API might return in an error message.
# Force UTF-8 with safe fallback so a stray character never crashes logging.
try:
    sys_stdout = open(1, "w", encoding="utf-8", errors="replace", closefd=False, buffering=1)
except Exception:
    sys_stdout = None

_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
_handlers.append(logging.StreamHandler(sys_stdout) if sys_stdout else logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("weex_bot")

# Structured status the GUI polls to show a plain-language view (open
# positions, balance, last check time) instead of the raw technical log.
BOT_STATUS = {
    "running": False,
    "balance": None,
    "watching": [],
    "open_positions": {},   # symbol -> {"side", "entry", "sl", "tp"}
    "last_update": None,
}


# ============================== API CLIENT ==============================

class WeexClient:
    def __init__(self, api_key: str, api_secret: str, passphrase: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url
        self.session = requests.Session()

    def _sign(self, timestamp: str, method: str, path: str, query: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{path}{query}{body}"
        digest = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, method: str, path: str, query: str = "", body: str = "") -> dict:
        timestamp = str(int(time.time() * 1000))
        sig = self._sign(timestamp, method, path, query, body)
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sig,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    def _get(self, path: str, params: Optional[dict] = None, private: bool = False):
        params = params or {}
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._headers("GET", path, query) if private else {}
        resp = self.session.get(self.base_url + path, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict):
        body_str = json.dumps(body)
        headers = self._headers("POST", path, "", body_str)
        resp = self.session.post(self.base_url + path, data=body_str, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ---- Market data ----
    def get_klines(self, symbol: str, interval: str, limit: int = 100):
        # NOTE: verify path/param names against current WEEX contract market-data docs
        return self._get("/capi/v2/market/candles", {
            "symbol": symbol, "granularity": interval, "limit": limit,
        })

    def get_api_trading_symbols(self):
        """List of symbols actually enabled for order placement via the API —
        this can be a stricter/different list than what candles/market-data
        accepts, which is why a symbol can chart fine but still get rejected
        when you try to place an order on it."""
        return self._get("/capi/v3/market/apiTradingSymbols")

    def get_exchange_info(self, symbol: Optional[str] = None):
        """Per-symbol contract config: price precision, quantity precision,
        min/max leverage, min/max order size, etc. Confirmed against WEEX's
        own API docs (GET /capi/v3/market/exchangeInfo)."""
        params = {"symbol": symbol} if symbol else {}
        return self._get("/capi/v3/market/exchangeInfo", params)

    def get_ticker_24hr(self):
        """24h price/volume stats for all symbols — used to rank coins by
        trading volume when auto-selecting the top N most active coins.
        Confirmed endpoint (GET /capi/v3/market/ticker/24hr)."""
        return self._get("/capi/v3/market/ticker/24hr")

    def set_leverage(self, symbol: str, leverage: int, margin_type: str = "CROSSED"):
        """Sets leverage for a symbol before opening a position.
        Confirmed against WEEX's own API docs (POST /capi/v3/account/leverage).
        Since we trade with cross margin, this sets crossLeverage; for
        isolated margin you'd send isolatedLongLeverage/isolatedShortLeverage
        instead."""
        body = {"symbol": symbol, "marginType": margin_type}
        if margin_type.upper() == "CROSSED":
            body["crossLeverage"] = str(leverage)
        else:
            body["isolatedLongLeverage"] = str(leverage)
            body["isolatedShortLeverage"] = str(leverage)
        return self._post("/capi/v3/account/leverage", body)

    # ---- Account ----
    def get_balance(self):
        return self._get("/capi/v2/account/accounts", private=True)

    # ---- Orders ----
    def place_order(self, symbol: str, side: str, position_side: str, order_type: str,
                     quantity: str, price: Optional[str] = None,
                     tp_trigger: Optional[str] = None, sl_trigger: Optional[str] = None,
                     dry_run: bool = True):
        path = "/capi/v3/sim/order" if dry_run else "/capi/v3/order"
        # WEEX's demo/sim endpoint uses a different symbol convention than
        # live orders — it inserts an 'S' before the quote currency, e.g.
        # 'BTCUSDT' (live) becomes 'BTCSUSDT' (sim). Confirmed from WEEX's
        # own API docs example for the sim/order endpoint.
        order_symbol_final = to_sim_symbol(symbol) if dry_run else symbol
        body = {
            "symbol": order_symbol_final,
            "side": side,                 # BUY or SELL
            "positionSide": position_side,  # LONG or SHORT
            "type": order_type,           # LIMIT or MARKET
            "timeInForce": "GTC",
            "quantity": quantity,
            "newClientOrderId": f"srbot-{int(time.time())}",
        }
        if price:
            body["price"] = price
        if tp_trigger:
            body["tpTriggerPrice"] = tp_trigger
            body["TpWorkingType"] = "CONTRACT_PRICE"
        if sl_trigger:
            body["slTriggerPrice"] = sl_trigger
            body["SlWorkingType"] = "MARK_PRICE"
        return self._post(path, body)


# ============================== STRATEGY ==============================

@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float


def parse_candles(raw) -> list:
    """Adjust field order/parsing to match actual kline response shape."""
    candles = []
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    for row in data:
        # typical order: [timestamp, open, high, low, close, volume]
        candles.append(Candle(
            open=float(row[1]), high=float(row[2]), low=float(row[3]),
            close=float(row[4]), volume=float(row[5]),
        ))
    return candles


def find_support_resistance(candles: list, lookback: int):
    window = candles[-lookback:]
    resistance = max(c.high for c in window)
    support = min(c.low for c in window)
    return support, resistance


def average_true_range(candles: list, period: int = 14) -> float:
    """Average True Range — measures how much a coin actually moves candle
    to candle, in price terms. Used to size the SL buffer so it scales with
    each coin's own volatility instead of one flat % for everything."""
    window = candles[-(period + 1):]
    if len(window) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(window)):
        high, low, prev_close = window[i].high, window[i].low, window[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def avg_volume(candles: list, lookback: int) -> float:
    window = candles[-lookback:]
    return sum(c.volume for c in window) / len(window)


def check_breakout_signal(candles: list) -> Optional[str]:
    """Returns 'LONG', 'SHORT', or None based on the most recent closed candle."""
    if len(candles) < LOOKBACK + CONFIRMATION_CANDLES + 1:
        return None

    reference = candles[:-1]  # exclude current forming candle from S/R calc
    support, resistance = find_support_resistance(reference, LOOKBACK)
    avg_vol = avg_volume(reference, LOOKBACK)

    last = candles[-1]
    volume_confirmed = last.volume >= avg_vol * VOLUME_MULTIPLIER

    if last.close > resistance and volume_confirmed:
        return "LONG"
    if last.close < support and volume_confirmed:
        return "SHORT"
    return None


# ============================== RISK / SIZING ==============================

def position_size(margin_usdt: float, entry_price: float, leverage: int) -> float:
    """Fixed-margin sizing: uses exactly margin_usdt of margin at the given
    leverage. Position value = margin × leverage; quantity = that ÷ price."""
    if entry_price == 0:
        return 0
    position_value = margin_usdt * leverage
    return round(position_value / entry_price, 4)


def leverage_for_symbol(order_sym: str) -> int:
    """Leverage to use for this coin, from LEVERAGE_BY_SYMBOL."""
    return LEVERAGE_BY_SYMBOL.get(order_sym.upper(), DEFAULT_LEVERAGE)


def max_positions_for_balance(equity: float) -> int:
    """Balance-tiered cap on simultaneously open trades.
    - Below 50 USDT: max 2 concurrent trades
    - 50 to 100 USDT: max 4 concurrent trades
    - Above 100 USDT: max 6 concurrent trades
    Adjust thresholds/counts here if the rules change."""
    if equity < 50:
        return 2
    elif equity < 100:
        return 4
    else:
        return 6


def required_margin(qty: float, entry_price: float, leverage: int) -> float:
    """How much margin this position ties up, given leverage."""
    if leverage <= 0:
        return qty * entry_price
    return round((qty * entry_price) / leverage, 4)


def risk_pct_for_balance(equity: float) -> Optional[float]:
    """Tiered risk sizing based on account balance.
    - Below 11 USDT: no trading (returns None)
    - 11 to 50 USDT: 7% risk per trade
    - Above 50 USDT: 5% risk per trade
    Adjust the thresholds/percentages here if your rules change."""
    if equity < 11:
        return None
    elif equity <= 50:
        return 7.0
    else:
        return 5.0


def get_equity(client: "WeexClient") -> Optional[float]:
    """Fetch and parse available USDT balance from WEEX's account endpoint.

    WEEX returns one entry per sub-account/coin_id. There's no plain
    'currency' field to match on, so if more than one entry is present we
    can't safely guess which is your real trading balance vs a demo/sim
    account — we log all of them and let WEEX_COIN_ID (env var) pick the
    right one once you've identified it from the log."""
    try:
        raw = client.get_balance()
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        accounts = data.get("collateral", data) if isinstance(data, dict) else data
        if not isinstance(accounts, list):
            accounts = [accounts]

        entries = []
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            amount = acct.get("amount")
            coin_id = acct.get("coin_id")
            if amount is not None:
                try:
                    amt_val = float(amount)
                except (TypeError, ValueError):
                    continue
                entries.append((coin_id, amt_val))

        if not entries:
            log.error(f"Could not find any 'amount' fields in balance response. Raw: {raw}")
            return None

        target_coin_id = os.environ.get("WEEX_COIN_ID")
        if target_coin_id:
            for coin_id, amount in entries:
                if str(coin_id) == str(target_coin_id):
                    return amount
            log.error(f"WEEX_COIN_ID={target_coin_id} not found among accounts: {entries}")
            return None

        if len(entries) == 1:
            return entries[0][1]

        log.error(
            f"Multiple accounts found, can't tell which is your real balance: {entries} "
            f"— set WEEX_COIN_ID to the correct coin_id in run_bot.bat once you identify it."
        )
        return None
    except Exception as e:
        log.exception(f"Failed to fetch/parse balance: {e}")
        return None


# ============================== MAIN LOOP ==============================

def run(stop_event=None, dry_run_override=None):
    """Main bot loop. Pass a threading.Event as stop_event to allow it to be
    stopped cleanly from another thread (used by weex_bot_app.py) — if not
    given, it only stops on Ctrl+C or process kill, as before.

    dry_run_override: if True or False, forces that mode for this run
    regardless of the DRY_RUN constant below — used by weex_bot_app.py to
    lock Free/Demo mode to simulated trading, and to only allow real trading
    once a license is verified."""
    reload_runtime_config()
    effective_dry_run = DRY_RUN if dry_run_override is None else dry_run_override

    if not (API_KEY and API_SECRET and API_PASSPHRASE):
        log.error("Missing WEEX_API_KEY / WEEX_API_SECRET / WEEX_API_PASSPHRASE env vars. Exiting.")
        return

    client = WeexClient(API_KEY, API_SECRET, API_PASSPHRASE, BASE_URL)

    # Check which symbols are actually enabled for order placement —
    # candles can load fine for a symbol that still gets rejected on order entry.
    tradable_set = set()
    try:
        tradable_raw = client.get_api_trading_symbols()
        log.info(f"Raw apiTradingSymbols response (for reference): {tradable_raw}")
        data = tradable_raw.get("data", tradable_raw) if isinstance(tradable_raw, dict) else tradable_raw
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    tradable_set.add(item.upper())
                elif isinstance(item, dict) and "symbol" in item:
                    tradable_set.add(str(item["symbol"]).upper())
    except Exception as e:
        log.warning(f"Could not fetch apiTradingSymbols (non-fatal, continuing): {e}")

    # If auto_top_n is configured, override RAW_SYMBOLS/SYMBOLS with the top-N
    # coins by 24h volume instead of the manually-typed list. Any coin not
    # explicitly listed in MARGIN_BY_SYMBOL / LEVERAGE_BY_SYMBOL falls back
    # to the defaults (DEFAULT_MARGIN_USDT / DEFAULT_LEVERAGE).
    global RAW_SYMBOLS, SYMBOLS, ORDER_SYMBOL_MAP
    if AUTO_TOP_N:
        try:
            top_symbols = fetch_top_symbols_by_volume(client, AUTO_TOP_N, tradable_filter=tradable_set or None)
            if AUTO_TOP_SELECTED:
                before = len(top_symbols)
                top_symbols = [s for s in top_symbols if s in AUTO_TOP_SELECTED]
                log.info(f"Narrowed top-{AUTO_TOP_N} list from {before} to {len(top_symbols)} coins based on your selection.")
            if top_symbols:
                RAW_SYMBOLS = top_symbols
                SYMBOLS = [candle_symbol(s) for s in RAW_SYMBOLS]
                ORDER_SYMBOL_MAP = {candle_symbol(s): order_symbol(s) for s in RAW_SYMBOLS}
                log.info(f"Trading {len(top_symbols)} coins (from top {AUTO_TOP_N} by 24h volume): {top_symbols}")
            else:
                log.warning("No symbols left after filtering — falling back to configured symbol list.")
        except Exception as e:
            log.warning(f"Could not fetch top-volume symbols (falling back to configured list): {e}")

    log.info(f"Starting bot | symbols={SYMBOLS} timeframe={TIMEFRAME} dry_run={effective_dry_run}")
    if not effective_dry_run:
        log.warning("LIVE TRADING IS ENABLED. Real orders will be placed.")

    if tradable_set:
        unsupported = [s for s in SYMBOLS if ORDER_SYMBOL_MAP.get(s, "").upper() not in tradable_set]
        if unsupported:
            log.warning(f"These symbols may NOT be enabled for API order placement: {unsupported} "
                        f"(order format: {[ORDER_SYMBOL_MAP.get(s) for s in unsupported]}) "
                        f"— signals on them will likely fail at order time.")
    else:
        log.warning("Could not parse the tradable-symbols list — unknown response shape, see raw log above.")

    # Fetch price/quantity precision per symbol — needed so SL/TP/quantity
    # sent with each order match WEEX's required step size (varies per coin;
    # e.g. BTC needs 1 price decimal, other coins need more or fewer).
    price_precision = {}
    qty_precision = {}
    try:
        exch_info = client.get_exchange_info()
        info_data = exch_info.get("data", exch_info) if isinstance(exch_info, dict) else exch_info
        symbols_info = info_data.get("symbols", []) if isinstance(info_data, dict) else []
        for s in symbols_info:
            if isinstance(s, dict) and "symbol" in s:
                sym = str(s["symbol"]).upper()
                if "pricePrecision" in s:
                    price_precision[sym] = int(s["pricePrecision"])
                if "quantityPrecision" in s:
                    qty_precision[sym] = int(s["quantityPrecision"])
        log.info(f"Loaded price/quantity precision for {len(price_precision)} symbols.")
    except Exception as e:
        log.warning(f"Could not fetch exchangeInfo precision data (will default to 2 decimals): {e}")

    def round_price(order_sym: str, price: float) -> float:
        decimals = price_precision.get(order_sym.upper(), 2)
        return round(price, decimals)

    def round_qty(order_sym: str, qty: float) -> float:
        decimals = qty_precision.get(order_sym.upper(), 4)
        return round(qty, decimals)

    # Track an open-position flag per symbol independently
    in_position = {symbol: False for symbol in SYMBOLS}
    open_position_details = {}  # symbol -> {"side", "entry", "sl", "tp"}

    BOT_STATUS.update({"running": True, "watching": list(SYMBOLS), "open_positions": {}})

    while True:
        if stop_event is not None and stop_event.is_set():
            log.info("Stop requested — shutting down cleanly.")
            BOT_STATUS.update({"running": False})
            return

        equity = get_equity(client)
        BOT_STATUS.update({"balance": equity, "last_update": time.time()})
        if equity is None:
            log.error("Skipping this cycle — could not determine account balance.")
            time.sleep(POLL_SECONDS)
            continue

        min_margin_needed = min(MARGIN_BY_SYMBOL.values(), default=DEFAULT_MARGIN_USDT)
        if equity < min_margin_needed:
            log.info(f"Balance {equity:.2f} USDT is below the smallest configured margin ({min_margin_needed} USDT) — no trading this cycle.")
            time.sleep(POLL_SECONDS)
            continue

        max_positions = max_positions_for_balance(equity)

        # Fetch all symbols' candle data at the same time instead of one by
        # one — this is what actually speeds things up with a large coin
        # list, since most of the old delay was just waiting on network
        # responses, not real work.
        candle_data = {}
        with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
            future_to_symbol = {
                executor.submit(client.get_klines, symbol, TIMEFRAME, LOOKBACK + 10): symbol
                for symbol in SYMBOLS
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    candle_data[symbol] = ("ok", future.result())
                except requests.HTTPError as e:
                    candle_data[symbol] = ("error", f"HTTP error: {e} | body={getattr(e.response, 'text', '')[:300]}")
                except Exception as e:
                    candle_data[symbol] = ("error", str(e))

        # Now process each symbol's result sequentially — this part is fast
        # (no network calls except for the rare case a real signal fires),
        # and keeping it sequential avoids race conditions in in_position.
        for symbol in SYMBOLS:
            try:
                open_count = sum(1 for v in in_position.values() if v)
                if open_count >= max_positions:
                    log.info(f"[{symbol}] Skipped — max concurrent positions ({max_positions} at balance {equity:.2f}) already open.")
                    continue

                status, payload = candle_data.get(symbol, ("error", "No data fetched"))
                if status == "error":
                    log.error(f"[{symbol}] {payload}")
                    continue

                candles = parse_candles(payload)
                signal = check_breakout_signal(candles)

                if signal and not in_position[symbol]:
                    entry_price = candles[-1].close
                    support, resistance = find_support_resistance(candles[:-1], LOOKBACK)
                    atr = average_true_range(candles[:-1], ATR_PERIOD)

                    # Buffer beyond the level: whichever is bigger — the flat % floor,
                    # or a multiple of this coin's actual recent volatility (ATR).
                    # This is what stops a volatile coin's normal noise from tripping the SL.
                    buffer_from_pct = entry_price * (STOP_LOSS_BUFFER_PCT / 100)
                    buffer_from_atr = atr * ATR_BUFFER_MULTIPLIER
                    buffer_amount = max(buffer_from_pct, buffer_from_atr)

                    if signal == "LONG":
                        sl_price = resistance - buffer_amount
                        sl_distance = entry_price - sl_price
                        tp_price = entry_price + (sl_distance * REWARD_RISK_RATIO)
                        side, pos_side = "BUY", "LONG"
                    else:
                        sl_price = support + buffer_amount
                        sl_distance = sl_price - entry_price
                        tp_price = entry_price - (sl_distance * REWARD_RISK_RATIO)
                        side, pos_side = "SELL", "SHORT"

                    order_sym = ORDER_SYMBOL_MAP.get(symbol, order_symbol(symbol))
                    leverage = leverage_for_symbol(order_sym)
                    margin = margin_for_symbol(order_sym)
                    if equity < margin:
                        log.info(f"[{symbol}] Skipped — balance {equity:.2f} is below this coin's {margin} USDT margin.")
                        continue
                    qty = round_qty(order_sym, position_size(margin, entry_price, leverage))
                    margin_needed = required_margin(qty, entry_price, leverage)
                    tp_price_rounded = round_price(order_sym, tp_price)
                    sl_price_rounded = round_price(order_sym, sl_price)

                    log.info(f"[{symbol}] Signal={signal} entry={entry_price} SL={sl_price_rounded} TP={tp_price_rounded} "
                             f"atr={atr:.4f} buffer={buffer_amount:.4f} "
                             f"qty={qty} leverage={leverage}x margin_needed={margin_needed} balance={equity:.2f} order_symbol={order_sym}")

                    try:
                        lev_result = client.set_leverage(order_sym, leverage)
                        log.info(f"[{symbol}] Set leverage result: {lev_result}")
                    except Exception as e:
                        log.warning(f"[{symbol}] Could not set leverage (continuing with order anyway, "
                                    f"WEEX may use account-default leverage instead): {e}")

                    result = client.place_order(
                        symbol=order_sym, side=side, position_side=pos_side,
                        order_type="MARKET", quantity=str(qty),
                        tp_trigger=str(tp_price_rounded), sl_trigger=str(sl_price_rounded),
                        dry_run=effective_dry_run,
                    )
                    log.info(f"[{symbol}] Order result: {result}")
                    in_position[symbol] = True
                    open_position_details[symbol] = {
                        "side": signal, "entry": entry_price,
                        "sl": sl_price_rounded, "tp": tp_price_rounded,
                    }
                    BOT_STATUS["open_positions"] = dict(open_position_details)

                elif not signal:
                    log.info(f"[{symbol}] No signal this candle.")

            except requests.HTTPError as e:
                body = getattr(e.response, "text", "")[:300]
                log.error(f"[{symbol}] HTTP error: {e} | body={body}")
            except Exception as e:
                log.exception(f"[{symbol}] Unexpected error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
