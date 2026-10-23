
import os
import requests
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# مسیرها بر اساس محل app.py، نه پوشه‌ای که سرور از آن اجرا شده
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# API فیوچرز WEEX
BASE = "https://api-contract.weex.com"

app = FastAPI(title="اسکنر فارسی WEEX")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# اگر پوشه وجود داشته باشد، فایل‌های استاتیک منتشر می‌شوند
if STATIC_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )


@app.get("/", response_class=HTMLResponse)
def home():
    if not INDEX_FILE.is_file():
        return HTMLResponse(
            content="""
            <!doctype html>
            <html lang="fa" dir="rtl">
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>خطای اسکنر</title>
            <body style="font-family:Tahoma,Arial;padding:25px;background:#0b1220;color:white">
                <h2>فایل صفحه اصلی پیدا نشد</h2>
                <p>باید فایل index.html داخل پوشه static مخزن GitHub باشد.</p>
            </body>
            </html>
            """,
            status_code=500,
        )

    try:
        html = INDEX_FILE.read_text(encoding="utf-8")
        return HTMLResponse(content=html)
    except OSError:
        return HTMLResponse(
            content="خطا در خواندن فایل صفحه اصلی.",
            status_code=500,
        )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "index_file_exists": INDEX_FILE.is_file(),
        "static_directory_exists": STATIC_DIR.is_dir(),
    }


@app.get("/api/klines")
def klines(
    symbol: str = Query("BTCUSDT", min_length=3, max_length=30),
    interval: str = Query("15m"),
    limit: int = Query(200, ge=50, le=1000),
):
    allowed = {
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "12h",
        "1d", "1w",
    }

    symbol = symbol.strip().upper()

    if interval not in allowed:
        return {"error": "تایم‌فریم انتخاب‌شده معتبر نیست."}

    if not symbol.isalnum():
        return {"error": "نماد معاملاتی نامعتبر است."}

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
        raw = response.json()

        # پاسخ API ممکن است لیست کندل‌ها یا یک شیء شامل داده باشد
        if isinstance(raw, dict):
            if raw.get("code") not in (None, 0, "0", "00000"):
                return {
                    "error": "WEEX داده کندل را نپذیرفت.",
                    "detail": str(raw)[:300],
                }

            raw = (
                raw.get("data")
                or raw.get("result")
                or raw.get("rows")
                or []
            )

        if not isinstance(raw, list):
            return {
                "error": "ساختار پاسخ کندل‌های WEEX قابل شناسایی نیست."
            }

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
                    vol = item.get("volume", 0)
                elif isinstance(item, (list, tuple)) and len(item) >= 6:
                    timestamp, op, hi, lo, cl, vol = item[:6]
                else:
                    continue

                timestamp = int(float(timestamp))

                # تبدیل میلی‌ثانیه به ثانیه برای نمودار
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

                if (
                    candle["time"] > 0
                    and candle["high"] >= candle["low"]
                    and min(
                        candle["open"],
                        candle["high"],
                        candle["low"],
                        candle["close"],
                    ) > 0
                ):
                    candles.append(candle)

            except (TypeError, ValueError, OverflowError):
                continue

        candles.sort(key=lambda candle: candle["time"])

        if not candles:
            return {
                "error": "از WEEX کندل معتبری دریافت نشد.",
                "detail": "نماد، تایم‌فریم و پاسخ API باید بررسی شوند.",
            }

        # حذف زمان‌های تکراری احتمالی
        unique = {candle["time"]: candle for candle in candles}
        candles = [unique[t] for t in sorted(unique)]

        return {
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
        }

    except requests.RequestException as exc:
        return {
            "error": "ارتباط با API فیوچرز WEEX ناموفق بود.",
            "detail": str(exc)[:300],
        }
    except (ValueError, TypeError) as exc:
        return {
            "error": "پاسخ API قابل پردازش نیست.",
            "detail": str(exc)[:300],
        }


@app.get("/api/ticker")
def ticker(
    symbol: str = Query("BTCUSDT", min_length=3, max_length=30)
):
    symbol = symbol.strip().upper()

    if not symbol.isalnum():
        return {"error": "نماد معاملاتی نامعتبر است."}

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

        if not isinstance(data, dict):
            return {"error": "پاسخ قیمت WEEX قابل شناسایی نیست."}

        return data

    except (requests.RequestException, ValueError) as exc:
        return {
            "error": "دریافت قیمت از WEEX ناموفق بود.",
            "detail": str(exc)[:300],
        }


@app.post("/api/telegram-test")
def telegram_test(payload: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "message": (
                    "متغیرهای TELEGRAM_BOT_TOKEN و "
                    "TELEGRAM_CHAT_ID را در Environment بخش Render تنظیم کنید."
                ),
            },
        )

    message = str(
        payload.get(
            "message",
            "آزمایش اتصال اسکنر فارسی WEEX",
        )
    )[:4000]

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
            },
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
            "detail": str(result.get("description", response.text))[:300],
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "message": "ارتباط با تلگرام برقرار نشد.",
            "detail": str(exc)[:300],
        }
