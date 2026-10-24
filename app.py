import os
import json
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
CONFIG = BASE / "selected_symbols.json"

WEEX_BASE_URL = "https://api-contract.weex.com"

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"
]

ALLOWED_INTERVALS = {
    "1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"
}

app = FastAPI(title="اسکنر فارسی WEEX")

if not STATIC_DIR.is_dir():
    raise RuntimeError("پوشه static پیدا نشد.")

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static"
)


class Selection(BaseModel):
    symbols: list[str]


class TelegramMessage(BaseModel):
    message: str = "آزمایش اتصال اسکنر فارسی WEEX"


def load_symbols():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        symbols = data.get("symbols", [])

        if isinstance(symbols, list):
            cleaned = [
                s for s in symbols
                if isinstance(s, str)
                and s.endswith("USDT")
            ]
            return cleaned

    except (OSError, ValueError, TypeError):
        pass

    return DEFAULT_SYMBOLS.copy()


def save_symbols(symbols):
    cleaned = []

    for symbol in symbols:
        if not isinstance(symbol, str):
            continue

        symbol = (
            symbol.strip()
            .upper()
            .replace("/", "")
            .replace("-", "")
        )

        if (
            symbol
            and symbol.endswith("USDT")
            and symbol not in cleaned
        ):
            cleaned.append(symbol)

    CONFIG.write_text(
        json.dumps(
            {"symbols": cleaned},
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    return cleaned


@app.get("/", response_class=HTMLResponse)
def home():
    index_file = STATIC_DIR / "index.html"

    if not index_file.is_file():
        raise HTTPException(
            status_code=500,
            detail="فایل static/index.html پیدا نشد."
        )

    return HTMLResponse(
        index_file.read_text(encoding="utf-8")
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "WEEX Persian Scanner"}


@app.get("/api/status")
def status():
    return {
        "status": "ok",
        "mode": "signal-selection-only",
        "live_trading_enabled": False,
        "selected_symbols": load_symbols()
    }


@app.get("/api/symbols")
def get_symbols():
    return {
        "symbols": load_symbols(),
        "defaults": DEFAULT_SYMBOLS
    }


@app.post("/api/symbols")
def set_symbols(selection: Selection):
    if len(selection.symbols) > 50:
        raise HTTPException(
            status_code=400,
            detail="حداکثر ۵۰ ارز مجاز است."
        )

    return {
        "ok": True,
        "symbols": save_symbols(selection.symbols)
    }


@app.get("/api/klines")
def get_klines(
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    limit: int = 200
):
    symbol = symbol.strip().upper()

    if (
        not symbol.endswith("USDT")
        or not symbol.replace("USDT", "").isalnum()
    ):
        raise HTTPException(
            status_code=400,
            detail="نماد معاملاتی نامعتبر است."
        )

    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail="تایم‌فریم نامعتبر است."
        )

    limit = max(1, min(limit, 1000))

    try:
        response = requests.get(
            f"{WEEX_BASE_URL}/capi/v3/market/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            },
            timeout=20
        )

        response.raise_for_status()
        raw = response.json()

        # در صورت دریافت پاسخ خطا از API
        if isinstance(raw, dict):
            raise HTTPException(
                status_code=502,
                detail="WEEX پاسخ خطا برگرداند: "
                       + str(raw.get("msg", raw.get("message", "خطای نامشخص")))
            )

        if not isinstance(raw, list):
            raise HTTPException(
                status_code=502,
                detail="قالب پاسخ کندل‌های WEEX ناشناخته است."
            )

        candles = []

        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue

            try:
                timestamp = int(float(item[0]))

                # تبدیل میلی‌ثانیه به ثانیه در صورت نیاز
                if timestamp > 10_000_000_000:
                    timestamp //= 1000

                candle = {
                    "time": timestamp,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5])
                }

                if (
                    candle["time"] > 0
                    and candle["open"] > 0
                    and candle["high"] >= candle["low"]
                    and candle["high"] > 0
                    and candle["low"] > 0
                    and candle["close"] > 0
                ):
                    candles.append(candle)

            except (ValueError, TypeError, OverflowError):
                continue

        candles.sort(key=lambda item: item["time"])

        # حذف زمان‌های تکراری احتمالی
        unique = {}
        for candle in candles:
            unique[candle["time"]] = candle

        candles = list(unique.values())

        if not candles:
            raise HTTPException(
                status_code=502,
                detail="هیچ کندل معتبری از WEEX دریافت نشد؛ "
                       "آدرس API، نماد و قالب پاسخ باید بررسی شود."
            )

        return {
            "symbol": symbol,
            "interval": interval,
            "candles": candles
        }

    except HTTPException:
        raise

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="ارتباط با API صرافی WEEX ناموفق بود."
        )


@app.post("/api/telegram-test")
def telegram_test(message: TelegramMessage | None = None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise HTTPException(
            status_code=400,
            detail="متغیرهای TELEGRAM_BOT_TOKEN و "
                   "TELEGRAM_CHAT_ID در Render تنظیم نشده‌اند."
        )

    text = (
        message.message
        if message and message.message
        else "آزمایش اتصال اسکنر فارسی WEEX"
    )

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise HTTPException(
                status_code=502,
                detail="تلگرام پیام را تأیید نکرد."
            )

        return {
            "ok": True,
            "message": "پیام آزمایشی تلگرام ارسال شد."
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="ارسال پیام ناموفق بود؛ تنظیمات تلگرام و "
                   "ارتباط سرور را بررسی کنید."
        )
