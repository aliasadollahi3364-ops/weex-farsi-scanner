import os, json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests

BASE = Path(__file__).parent
CONFIG = BASE / 'selected_symbols.json'
DEFAULT_SYMBOLS = ['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT','LINKUSDT','LTCUSDT']
app = FastAPI(title='اسکنر فارسی WEEX')
app.mount('/static', StaticFiles(directory=str(BASE / 'static')), name='static')

class Selection(BaseModel):
    symbols: list[str]

def load_symbols():
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
        return [s for s in data.get('symbols', []) if isinstance(s, str)]
    except Exception:
        return DEFAULT_SYMBOLS.copy()

def save_symbols(symbols):
    cleaned = []
    for s in symbols:
        s = s.strip().upper().replace('/', '').replace('-', '')
        if s and s.endswith('USDT') and s not in cleaned:
            cleaned.append(s)
    CONFIG.write_text(json.dumps({'symbols': cleaned}, ensure_ascii=False, indent=2), encoding='utf-8')
    return cleaned

@app.get('/', response_class=HTMLResponse)
def home():
    return (BASE / 'static' / 'index.html').read_text(encoding='utf-8')

@app.get('/api/symbols')
def get_symbols():
    return {'symbols': load_symbols(), 'defaults': DEFAULT_SYMBOLS}

@app.post('/api/symbols')
def set_symbols(selection: Selection):
    if len(selection.symbols) > 50:
        raise HTTPException(status_code=400, detail='حداکثر ۵۰ ارز مجاز است.')
    return {'ok': True, 'symbols': save_symbols(selection.symbols)}

@app.get('/api/status')
def status():
    return {'status': 'ok', 'mode': 'signal-selection-only', 'live_trading_enabled': False, 'selected_symbols': load_symbols()}

@app.post('/api/telegram-test')
def telegram_test():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail='متغیرهای TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID در Render تنظیم نشده‌اند.')
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id': chat_id, 'text': '✅ پیام آزمایشی از اسکنر فارسی WEEX'}, timeout=15)
        r.raise_for_status()
        return {'ok': True, 'message': 'پیام آزمایشی ارسال شد.'}
    except requests.RequestException:
        raise HTTPException(status_code=502, detail='ارسال پیام ناموفق بود؛ توکن و شناسه چت را بررسی کنید.')
