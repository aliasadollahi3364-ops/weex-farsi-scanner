import os
import time
import asyncio
import logging
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# =========================================================
# WEEX 4H ORDER BLOCK + 15M CHoCH TELEGRAM SCANNER
# Signal-only bot. No real orders are submitted.
# Duplicate/old signal protection added.
# =========================================================

app = FastAPI(title="WEEX Farsi Scanner")

BASE_URL = "https://api-contract.weex.com/capi/v3/market"
TELEGRAM_URL = "https://api.telegram.org"

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"
]

# Strategy settings - unchanged
HTF_LIMIT = 300
ENTRY_LIMIT = 600
PIVOT4 = 3
PIVOT15 = 3
BODY_LEN = 10
OB_SEARCH = 12
MAX_WAIT = 17
ATR_LEN = 7
MAX_BARS_SINCE_TOUCH = 14
MIN_CHOCH_BODY_ATR = 0.85
RETEST_ATR = 0.15
MIN_CONFIRM_BODY_ATR = 0.20
SL_BUFFER_ATR = 0.15
RISK_REWARD = 2.0
BREAK_EVEN_TRIGGER_R = 1.0
POLL_SECONDS = 60

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("weex-scanner")

selected_symbols = DEFAULT_SYMBOLS.copy()
latest_signals = {}
last_prices = {}
scanner_errors = {}
scanner_started = False
telegram_last_test = None

# ---------------------------------------------------------
# Duplicate / old signal protection
# ---------------------------------------------------------

# May be overridden in Render with SIGNAL_STATE_FILE.
STATE_FILE = Path(
    os.getenv(
        "SIGNAL_STATE_FILE",
        str(Path(__file__).parent / "signal_state.json")
    )
)

# Last processed signal key per symbol, persisted to disk.
# Format: {"BTCUSDT": "BUY:1792732500000"}
last_processed = {}

# Latest closed 15m candle time observed for each symbol.
last_closed_candle_times = {}

# Baseline is established on the first successful scan per symbol.
# Signals at or before this candle are historical and will not be sent.
startup_baselines = {}


def load_state():
    global last_processed

    try:
        if not STATE_FILE.exists():
            log.info("No saved signal state found; starting fresh.")
            return

        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        saved = data.get("last_processed", {})
        if isinstance(saved, dict):
            last_processed = {
                str(symbol): str(key)
                for symbol, key in saved.items()
                if isinstance(symbol, str) and isinstance(key, str)
            }

        log.info(
            "Loaded saved signal state for %d symbols.",
            len(last_processed)
        )

    except Exception:
        log.exception("Could not load signal state; starting with empty state.")


def save_state():
    """Atomically save processed signal IDs."""
    temp_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")

    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "last_processed": last_processed,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_file, STATE_FILE)

    except Exception:
        log.exception("Could not save signal state.")

        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError:
            pass


def signal_key(signal):
    return f"{signal['side']}:{int(signal['candle_time'])}"


load_state()


# ---------------------------------------------------------
# WEEX market data
# ---------------------------------------------------------

def get_klines(symbol, interval, limit):
    response = requests.get(
        f"{BASE_URL}/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        rows = payload.get("data", payload.get("result", []))
    else:
        rows = payload

    if not isinstance(rows, list):
        raise RuntimeError(
            f"Unexpected WEEX kline response: {str(payload)[:200]}"
        )

    candles = []
    now_ms = int(time.time() * 1000)

    for row in rows:
        try:
            if isinstance(row, dict):
                t = int(
                    row.get("time", row.get("openTime", row.get("open_time")))
                )
                o = float(row.get("open"))
                h = float(row.get("high"))
                low = float(row.get("low"))
                c = float(row.get("close"))
                v = float(row.get("volume", 0))
                ct = int(row.get("closeTime", row.get("close_time", t)))
            else:
                t = int(row[0])
                o = float(row[1])
                h = float(row[2])
                low = float(row[3])
                c = float(row[4])
                v = float(row[5]) if len(row) > 5 else 0.0
                ct = int(row[6]) if len(row) > 6 else t

            # Ignore candles that have not closed yet.
            if ct >= now_ms:
                continue

            candles.append({
                "time": t,
                "close_time": ct,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
            })

        except (TypeError, ValueError, IndexError):
            continue

    candles.sort(key=lambda x: x["time"])

    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle

    return list(unique.values())


def telegram_send(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in Render."
        )

    response = requests.post(
        f"{TELEGRAM_URL}/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {data}")


# ---------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------

def candle_body(c):
    return abs(c["close"] - c["open"])


def is_pivot_high(candles, index, left, right):
    if index - left < 0 or index + right >= len(candles):
        return False

    value = candles[index]["high"]

    return (
        all(value > candles[j]["high"] for j in range(index - left, index))
        and all(
            value >= candles[j]["high"]
            for j in range(index + 1, index + right + 1)
        )
    )


def is_pivot_low(candles, index, left, right):
    if index - left < 0 or index + right >= len(candles):
        return False

    value = candles[index]["low"]

    return (
        all(value < candles[j]["low"] for j in range(index - left, index))
        and all(
            value <= candles[j]["low"]
            for j in range(index + 1, index + right + 1)
        )
    )


def calculate_atr(candles, period):
    """Wilder ATR, similar to ta.atr() in Pine."""
    result = [None] * len(candles)

    if len(candles) < period + 1:
        return result

    true_ranges = [None] * len(candles)

    for i, c in enumerate(candles):
        if i == 0:
            true_ranges[i] = c["high"] - c["low"]
        else:
            prev_close = candles[i - 1]["close"]
            true_ranges[i] = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )

    seed = sum(true_ranges[1:period + 1]) / period
    result[period] = seed
    previous = seed

    for i in range(period + 1, len(candles)):
        previous = (
            previous * (period - 1) + true_ranges[i]
        ) / period
        result[i] = previous

    return result


# ---------------------------------------------------------
# Build confirmed 4H order-block events
# ---------------------------------------------------------

def build_htf_events(candles):
    events = []
    last_ph = None
    last_pl = None
    trend = 0

    for i, c in enumerate(candles):
        candidate = i - PIVOT4

        if candidate >= PIVOT4:
            if is_pivot_high(candles, candidate, PIVOT4, PIVOT4):
                last_ph = candles[candidate]["high"]

            if is_pivot_low(candles, candidate, PIVOT4, PIVOT4):
                last_pl = candles[candidate]["low"]

        bull_top = bull_bottom = bull_left = None
        bear_top = bear_bottom = bear_left = None

        body = candle_body(c)
        prior_bodies = [
            candle_body(candles[j])
            for j in range(max(0, i - BODY_LEN), i)
        ]

        avg_prior_body = (
            sum(prior_bodies) / BODY_LEN
            if len(prior_bodies) == BODY_LEN
            else None
        )
        strong = (
            avg_prior_body is not None
            and body > avg_prior_body
        )

        bull_bos = (
            last_ph is not None
            and c["close"] > last_ph
            and i > 0
            and candles[i - 1]["close"] <= last_ph
        )

        bear_bos = (
            last_pl is not None
            and c["close"] < last_pl
            and i > 0
            and candles[i - 1]["close"] >= last_pl
        )

        if bull_bos:
            trend = 1

            if strong:
                for j in range(1, OB_SEARCH + 1):
                    k = i - j
                    if k >= 0 and candles[k]["close"] < candles[k]["open"]:
                        bull_top = candles[k]["high"]
                        bull_bottom = candles[k]["low"]
                        bull_left = candles[k]["time"]
                        break

        if bear_bos:
            trend = -1

            if strong:
                for j in range(1, OB_SEARCH + 1):
                    k = i - j
                    if k >= 0 and candles[k]["close"] > candles[k]["open"]:
                        bear_top = candles[k]["high"]
                        bear_bottom = candles[k]["low"]
                        bear_left = candles[k]["time"]
                        break

        events.append({
            "time": c["close_time"],
            "close": c["close"],
            "trend": trend,
            "bull_top": bull_top,
            "bull_bottom": bull_bottom,
            "bull_left": bull_left,
            "bear_top": bear_top,
            "bear_bottom": bear_bottom,
            "bear_left": bear_left,
        })

    return events


# ---------------------------------------------------------
# Strategy replay: 4H OB + 15M touch/CHoCH/retest/confirm
# Strategy logic unchanged
# ---------------------------------------------------------

def analyze_symbol(symbol):
    htf = get_klines(symbol, "4h", HTF_LIMIT)
    entry = get_klines(symbol, "15m", ENTRY_LIMIT)

    if len(htf) < 30 or len(entry) < 60:
        raise RuntimeError("Not enough closed candles returned by WEEX.")

    # Save the latest closed 15m candle timestamp for startup baseline.
    last_closed_candle_times[symbol] = entry[-1]["close_time"]

    atrs = calculate_atr(entry, ATR_LEN)
    htf_events = build_htf_events(htf)

    order_blocks = []
    event_index = 0
    trend4h = 0
    last_ph15 = None
    last_pl15 = None

    position = 0
    current_sl = None
    current_tp = None
    entry_price = None
    initial_risk = None

    latest_signal = None

    for i, c in enumerate(entry):
        while (
            event_index < len(htf_events)
            and htf_events[event_index]["time"] <= c["close_time"]
        ):
            event = htf_events[event_index]
            trend4h = event["trend"]

            for ob in order_blocks:
                if not ob["active"]:
                    continue

                if (
                    ob["direction"] == 1
                    and event["close"] < ob["bottom"]
                ):
                    ob["active"] = False
                elif (
                    ob["direction"] == -1
                    and event["close"] > ob["top"]
                ):
                    ob["active"] = False

            if event["bull_left"] is not None:
                order_blocks.append({
                    "top": event["bull_top"],
                    "bottom": event["bull_bottom"],
                    "direction": 1,
                    "left": event["bull_left"],
                    "active": True,
                    "used": False,
                    "touched": False,
                    "touch_bar": None,
                    "has_choch": False,
                    "choch_level": None,
                    "choch_bar": None,
                })

            if event["bear_left"] is not None:
                order_blocks.append({
                    "top": event["bear_top"],
                    "bottom": event["bear_bottom"],
                    "direction": -1,
                    "left": event["bear_left"],
                    "active": True,
                    "used": False,
                    "touched": False,
                    "touch_bar": None,
                    "has_choch": False,
                    "choch_level": None,
                    "choch_bar": None,
                })

            event_index += 1

        if len(order_blocks) > 200:
            order_blocks = order_blocks[-200:]

        candidate = i - PIVOT15

        if candidate >= PIVOT15:
            if is_pivot_high(entry, candidate, PIVOT15, PIVOT15):
                last_ph15 = entry[candidate]["high"]

            if is_pivot_low(entry, candidate, PIVOT15, PIVOT15):
                last_pl15 = entry[candidate]["low"]

        atr = atrs[i]

        if atr is None or i == 0:
            continue

        # Simulated position management; no exchange orders are placed.
        if (
            position == 1
            and current_sl is not None
            and current_tp is not None
        ):
            if (
                c["high"]
                >= entry_price + initial_risk * BREAK_EVEN_TRIGGER_R
            ):
                current_sl = max(current_sl, entry_price)

            if c["low"] <= current_sl or c["high"] >= current_tp:
                position = 0

        elif (
            position == -1
            and current_sl is not None
            and current_tp is not None
        ):
            if (
                c["low"]
                <= entry_price - initial_risk * BREAK_EVEN_TRIGGER_R
            ):
                current_sl = min(current_sl, entry_price)

            if c["high"] >= current_sl or c["low"] <= current_tp:
                position = 0

        if position != 0:
            continue

        tolerance = atr * RETEST_ATR
        chosen = None

        for ob in order_blocks:
            if not ob["active"] or ob["used"]:
                continue

            direction = ob["direction"]

            if direction == 1 and trend4h != 1:
                continue

            if direction == -1 and trend4h != -1:
                continue

            touched_now = (
                c["low"] <= ob["top"]
                and c["high"] >= ob["bottom"]
            )

            if touched_now:
                ob["touched"] = True
                ob["touch_bar"] = i

            touch_bar = ob["touch_bar"]

            fresh = (
                ob["touched"]
                and touch_bar is not None
                and i - touch_bar <= MAX_BARS_SINCE_TOUCH
            )

            if not fresh:
                continue

            bull_cross = (
                direction == 1
                and last_ph15 is not None
                and c["close"] > last_ph15
                and entry[i - 1]["close"] <= last_ph15
                and c["close"] > c["open"]
                and c["close"] - c["open"] >= atr * MIN_CHOCH_BODY_ATR
            )

            bear_cross = (
                direction == -1
                and last_pl15 is not None
                and c["close"] < last_pl15
                and entry[i - 1]["close"] >= last_pl15
                and c["close"] < c["open"]
                and c["open"] - c["close"] >= atr * MIN_CHOCH_BODY_ATR
            )

            if not ob["has_choch"] and bull_cross:
                ob["has_choch"] = True
                ob["choch_level"] = last_ph15
                ob["choch_bar"] = i

            if not ob["has_choch"] and bear_cross:
                ob["has_choch"] = True
                ob["choch_level"] = last_pl15
                ob["choch_bar"] = i

            choch_bar = ob["choch_bar"]

            if (
                ob["has_choch"]
                and choch_bar is not None
                and i - choch_bar > MAX_WAIT
            ):
                ob["has_choch"] = False
                ob["choch_level"] = None
                ob["choch_bar"] = None

            if (
                not ob["has_choch"]
                or choch_bar is None
                or i <= choch_bar
            ):
                continue

            level = ob["choch_level"]

            if level is None:
                continue

            retest_detected = (
                c["low"] <= level + tolerance
                and c["high"] >= level - tolerance
            )

            if not retest_detected:
                continue

            if direction == 1:
                confirm = (
                    c["close"] > c["open"]
                    and c["close"] > level
                    and c["close"] - c["open"]
                    >= atr * MIN_CONFIRM_BODY_ATR
                )

                if not confirm or last_pl15 is None:
                    continue

                sl_base = min(last_pl15, ob["bottom"])
                sl = sl_base - atr * SL_BUFFER_ATR

                if sl >= c["close"] or c["close"] - sl <= 0:
                    continue

                chosen = (ob, "BUY", sl, c["close"])
                break

            else:
                confirm = (
                    c["close"] < c["open"]
                    and c["close"] < level
                    and c["open"] - c["close"]
                    >= atr * MIN_CONFIRM_BODY_ATR
                )

                if not confirm or last_ph15 is None:
                    continue

                sl_base = max(last_ph15, ob["top"])
                sl = sl_base + atr * SL_BUFFER_ATR

                if sl <= c["close"] or sl - c["close"] <= 0:
                    continue

                chosen = (ob, "SELL", sl, c["close"])
                break

        if chosen:
            ob, side, sl, price = chosen
            risk = abs(price - sl)

            tp = (
                price + risk * RISK_REWARD
                if side == "BUY"
                else price - risk * RISK_REWARD
            )

            position = 1 if side == "BUY" else -1
            current_sl = sl
            current_tp = tp
            entry_price = price
            initial_risk = risk
            ob["used"] = True

            latest_signal = {
                "symbol": symbol,
                "side": side,
                "entry": price,
                "sl": sl,
                "tp": tp,
                "candle_time": c["close_time"],
                "trend4h": trend4h,
            }

    last_prices[symbol] = entry[-1]["close"]
    return latest_signal


# ---------------------------------------------------------
# Telegram signal formatting
# ---------------------------------------------------------

def format_price(value):
    if value >= 1000:
        return f"{value:.2f}"

    if value >= 1:
        return f"{value:.5f}".rstrip("0").rstrip(".")

    return f"{value:.8f}".rstrip("0").rstrip(".")


def signal_message(signal):
    side_fa = "خرید 🟢" if signal["side"] == "BUY" else "فروش 🔴"

    candle_time = datetime.fromtimestamp(
        signal["candle_time"] / 1000,
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"📊 سیگنال استراتژی WEEX\n\n"
        f"ارز: {signal['symbol']}\n"
        f"جهت: {side_fa}\n"
        f"تایم‌فریم اردربلاک: 4H\n"
        f"تایم‌فریم ورود: 15M\n\n"
        f"ورود تقریبی: {format_price(signal['entry'])}\n"
        f"حد ضرر: {format_price(signal['sl'])}\n"
        f"حد سود (RR 1:2): {format_price(signal['tp'])}\n\n"
        f"کندل سیگنال: {candle_time}\n"
        f"⚠️ فقط سیگنال؛ معامله واقعی باز نشده است."
    )


# ---------------------------------------------------------
# Scanner loop - duplicate/old signal handling
# ---------------------------------------------------------

async def scan_loop():
    while True:
        for symbol in list(selected_symbols):
            try:
                signal = await asyncio.to_thread(analyze_symbol, symbol)
                scanner_errors.pop(symbol, None)

                # Establish the baseline on the first successful scan.
                # The latest historical signal must not be sent as new.
                if symbol not in startup_baselines:
                    startup_baselines[symbol] = (
                        last_closed_candle_times.get(symbol)
                    )
                    log.info(
                        "%s startup baseline: %s",
                        symbol,
                        startup_baselines[symbol],
                    )

                if not signal:
                    continue

                latest_signals[symbol] = signal
                key = signal_key(signal)

                # Already processed this exact signal.
                if last_processed.get(symbol) == key:
                    continue

                baseline = startup_baselines.get(symbol)
                signal_time = int(signal["candle_time"])

                # Historical signal at startup: mark it as processed,
                # but do not send it to Telegram.
                if baseline is not None and signal_time <= baseline:
                    last_processed[symbol] = key
                    save_state()

                    log.info(
                        "Suppressed historical signal: %s %s at %s",
                        symbol,
                        signal["side"],
                        signal_time,
                    )
                    continue

                # Send first; only mark processed after Telegram succeeds.
                await asyncio.to_thread(
                    telegram_send,
                    signal_message(signal)
                )

                last_processed[symbol] = key
                save_state()

                log.info(
                    "Telegram signal sent: %s %s at %s",
                    symbol,
                    signal["side"],
                    signal_time,
                )

            except Exception as exc:
                scanner_errors[symbol] = str(exc)
                log.exception("Scan failed for %s", symbol)

        await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def startup_event():
    global scanner_started

    if not scanner_started:
        scanner_started = True
        asyncio.create_task(scan_loop())


# ---------------------------------------------------------
# Web API
# ---------------------------------------------------------

@app.get("/health")
@app.get("/api/status")
def status():
    return {
        "ok": True,
        "scanner": "running",
        "symbols": selected_symbols,
        "last_prices": last_prices,
        "latest_signals": latest_signals,
        "errors": scanner_errors,
        "telegram_configured": bool(
            os.getenv("TELEGRAM_BOT_TOKEN")
            and os.getenv("TELEGRAM_CHAT_ID")
        ),
        "duplicate_protection": True,
        "state_file": str(STATE_FILE),
        "startup_baselines_set": list(startup_baselines.keys()),
    }


@app.get("/api/symbols")
def get_symbols():
    return {"symbols": selected_symbols}


@app.post("/api/symbols")
def set_symbols(payload: dict):
    global selected_symbols

    raw = payload.get("symbols", [])

    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail="symbols must be a list"
        )

    cleaned = []

    for item in raw:
        symbol = str(item).strip().upper()
        symbol = symbol.replace("WEEX:", "").replace("/", "")

        if symbol and symbol.isalnum() and symbol.endswith("USDT"):
            if symbol not in cleaned:
                cleaned.append(symbol)

    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail="Select at least one USDT symbol"
        )

    if len(cleaned) > 30:
        raise HTTPException(
            status_code=400,
            detail="Maximum 30 symbols"
        )

    selected_symbols = cleaned

    return {"ok": True, "symbols": selected_symbols}


@app.get("/api/klines")
def api_klines(
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    limit: int = 200
):
    symbol = symbol.upper().replace("WEEX:", "").replace("/", "")
    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}

    if interval not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Unsupported interval"
        )

    try:
        candles = get_klines(
            symbol,
            interval,
            max(1, min(limit, 1000))
        )

        return {
            "symbol": symbol,
            "interval": interval,
            "data": candles
        }

    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/ticker")
def api_ticker(symbol: str = "BTCUSDT"):
    symbol = symbol.upper().replace("WEEX:", "").replace("/", "")

    try:
        candles = get_klines(symbol, "15m", 2)

        if not candles:
            raise RuntimeError("No closed candles returned")

        return {
            "symbol": symbol,
            "price": candles[-1]["close"]
        }

    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/telegram-test")
async def telegram_test():
    global telegram_last_test

    try:
        await asyncio.to_thread(
            telegram_send,
            "✅ اتصال ربات WEEX به تلگرام برقرار است.\n"
            "این پیام آزمایشی است."
        )

        telegram_last_test = datetime.now(timezone.utc).isoformat()

        return {
            "ok": True,
            "message": "Telegram test sent"
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------
# Frontend
# ---------------------------------------------------------

static_dir = Path(__file__).parent / "static"

if static_dir.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(static_dir)),
        name="static"
    )


@app.get("/", response_class=HTMLResponse)
def home():
    index_file = static_dir / "index.html"

    if index_file.exists():
        return index_file.read_text(encoding="utf-8")

    return """
    <html>
    <head>
        <meta charset="utf-8">
        <title>WEEX Scanner</title>
    </head>
    <body style="font-family:Arial;max-width:800px;margin:40px auto">
        <h2>WEEX Telegram Scanner</h2>
        <p>ربات در حال اجرا است.</p>
        <p><a href="/api/status">وضعیت ربات</a></p>
        <p><a href="/docs">API documentation</a></p>
    </body>
    </html>
