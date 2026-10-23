import os
import requests
from pathlib import Path

from fastapi import FastAPI, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
BASE = "https://api-contract.weex.com"

TIMEFRAMES = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "12h", "1d", "1w",
}

app = FastAPI(title="اسکنر فارسی WEEX | Order Block")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    if not INDEX_FILE.is_file():
        return HTMLResponse(
            "<h2>فایل static/index.html پیدا نشد.</h2>",
            status_code=500,
        )
    try:
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("خطا در خواندن فایل صفحه اصلی.", status_code=500)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "index_file_exists": INDEX_FILE.is_file(),
        "static_directory_exists": STATIC_DIR.is_dir(),
    }


def parse_candles(raw):
    """تبدیل قالب‌های متداول پاسخ WEEX به OHLCV استاندارد."""
    if isinstance(raw, dict):
        if raw.get("code") not in (None, 0, "0", "00000"):
            raise ValueError(f"WEEX error: {str(raw)[:250]}")
        raw = (
            raw.get("data")
            or raw.get("result")
            or raw.get("rows")
            or raw.get("list")
            or []
        )

    if isinstance(raw, dict):
        raw = raw.get("list") or raw.get("rows") or []

    if not isinstance(raw, list):
        raise ValueError("ساختار پاسخ کندل‌ها قابل شناسایی نیست.")

    candles = []
    for item in raw:
        try:
            if isinstance(item, dict):
                timestamp = (
                    item.get("time")
                    or item.get("timestamp")
                    or item.get("ts")
                )
                op = item.get("open")
                hi = item.get("high")
                lo = item.get("low")
                cl = item.get("close")
                vol = item.get("volume", item.get("vol", 0))
            elif isinstance(item, (list, tuple)) and len(item) >= 6:
                # قالب رایج: timestamp, open, high, low, close, volume
                timestamp, op, hi, lo, cl, vol = item[:6]
            else:
                continue

            timestamp = int(float(timestamp))
            if timestamp > 10_000_000_000:
                timestamp //= 1000

            candle = {
                "time": timestamp,
                "open": float(op),
                "high": float(hi),
                "low": float(lo),
                "close": float(cl),
                "volume": float(vol or 0),
            }

            prices = [
                candle["open"], candle["high"],
                candle["low"], candle["close"],
            ]
            if (
                candle["time"] > 0
                and all(p > 0 for p in prices)
                and candle["high"] >= max(prices)
                and candle["low"] <= min(prices)
            ):
                candles.append(candle)

        except (TypeError, ValueError, OverflowError):
            continue

    # حذف زمان‌های تکراری
    unique = {c["time"]: c for c in candles}
    return [unique[t] for t in sorted(unique)]


@app.get("/api/klines")
def klines(
    symbol: str = Query("BTCUSDT", min_length=3, max_length=30),
    interval: str = Query("15m"),
    limit: int = Query(500, ge=50, le=1000),
):
    symbol = symbol.strip().upper()

    if interval not in TIMEFRAMES:
        return JSONResponse(
            status_code=400,
            content={"error": "تایم‌فریم انتخاب‌شده معتبر نیست."},
        )
    if not symbol.isalnum():
        return JSONResponse(
            status_code=400,
            content={"error": "نماد معاملاتی نامعتبر است."},
        )

    try:
        response = requests.get(
            f"{BASE}/capi/v3/market/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
            timeout=15,
        )
        response.raise_for_status()
        candles = parse_candles(response.json())

        if not candles:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "از WEEX کندل معتبری دریافت نشد.",
                    "detail": "نماد، تایم‌فریم و قالب پاسخ API را بررسی کنید.",
                },
            )

        return {
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
        }

    except requests.RequestException as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "ارتباط با API فیوچرز WEEX ناموفق بود.",
                "detail": str(exc)[:300],
            },
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "پاسخ API قابل پردازش نیست.",
                "detail": str(exc)[:300],
            },
        )


@app.get("/api/ticker")
def ticker(
    symbol: str = Query("BTCUSDT", min_length=3, max_length=30)
):
    symbol = symbol.strip().upper()

    if not symbol.isalnum():
        return JSONResponse(
            status_code=400,
            content={"error": "نماد معاملاتی نامعتبر است."},
        )

    try:
        response = requests.get(
            f"{BASE}/capi/v3/market/ticker/24hr",
            params={"symbol": symbol},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]

        if not isinstance(data, dict):
            return JSONResponse(
                status_code=502,
                content={"error": "پاسخ قیمت WEEX قابل شناسایی نیست."},
            )

        return data

    except (requests.RequestException, ValueError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "دریافت قیمت از WEEX ناموفق بود.",
                "detail": str(exc)[:300],
            },
        )


@app.post("/api/telegram-test")
def telegram_test(payload: dict = Body(default={})):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        return {
            "ok": False,
            "message": (
                "متغیرهای TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID "
                "را در Environment بخش Render تنظیم کنید."
            ),
        }

    message = str(
        payload.get("message", "آزمایش اتصال اسکنر فارسی WEEX")
    )[:4000]

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        try:
            result = response.json()
        except ValueError:
            result = {}

        if response.ok and result.get("ok") is True:
            return {
                "ok": True,
                "message": "پیام آزمایشی با موفقیت ارسال شد.",
            }

        return {
            "ok": False,
            "message": "تلگرام پیام را نپذیرفت.",
            "detail": str(
                result.get("description", response.text)
            )[:300],
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "message": "ارتباط با تلگرام برقرار نشد.",
            "detail": str(exc)[:300],
        }
