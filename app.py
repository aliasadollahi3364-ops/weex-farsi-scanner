import os, time, requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

BASE = "https://api-contract.weex.com"
app = FastAPI(title="اسکنر فارسی WEEX")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()

@app.get("/api/klines")
def klines(symbol: str = Query("BTCUSDT"), interval: str = Query("15m"), limit: int = Query(200, ge=50, le=1000)):
    allowed = {"1m","5m","15m","30m","1h","4h","12h","1d","1w"}
    if interval not in allowed:
        return {"error": "تایم‌فریم نامعتبر است"}
    try:
        r = requests.get(BASE + "/capi/v3/market/klines",
                         params={"symbol": symbol.upper(), "interval": interval, "limit": limit}, timeout=12)
        r.raise_for_status()
        raw = r.json()
        candles = [{"time": int(x[0]) // 1000, "open": float(x[1]), "high": float(x[2]),
                    "low": float(x[3]), "close": float(x[4]), "volume": float(x[5])} for x in raw]
        candles.sort(key=lambda x: x["time"])
        return {"symbol": symbol.upper(), "interval": interval, "candles": candles}
    except Exception as e:
        return {"error": "دریافت داده از WEEX ناموفق بود. اتصال و نماد را بررسی کنید.", "detail": str(e)}

@app.get("/api/ticker")
def ticker(symbol: str = Query("BTCUSDT")):
    try:
        r = requests.get(BASE + "/capi/v3/market/ticker/24hr", params={"symbol": symbol.upper()}, timeout=12)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list): data = data[0] if data else {}
        return data
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/telegram-test")
def telegram_test(payload: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"ok": False, "message": "ابتدا TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID را در متغیرهای محیطی تنظیم کنید."}
    message = payload.get("message", "آزمایش اتصال اسکنر فارسی WEEX")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": message}, timeout=12)
        return {"ok": r.ok, "message": "درخواست ارسال شد" if r.ok else "تلگرام خطا برگرداند", "detail": r.text[:300]}
    except Exception as e:
        return {"ok": False, "message": str(e)}
