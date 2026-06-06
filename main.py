import asyncio
import logging
from pybit.unified_trading import HTTP
from telegram import Bot
from datetime import datetime

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('signals.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
API_KEY = 'YOUR_BYBIT_API_KEY'
API_SECRET = 'YOUR_BYBIT_API_SECRET'
TELEGRAM_TOKEN = '8740259151:AAFOdw_WX8eW-DE1Hoqav056Kt98BYUJ7XU'
CHAT_ID = '1148922890'

SYMBOLS = ["BTCUSDT", "SOLUSDT", "ARBUSDT", "AVAXUSDT"]
HTF = "60"   # Старший ТФ (1 час)
MTF = "15"   # Средний ТФ (15 минут)
LTF = "5"    # Младший ТФ (5 минут - вход)

# Хранилище для предотвращения дублей: { "BTCUSDT": "2023-10-27 14:00" }
sent_signals = {}

# Кэш свечей: {(symbol, interval): (timestamp, data)}
ohlcv_cache = {}
CACHE_TTL_SECONDS = 60  # Данные валидны 60 секунд

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)
tg_bot = Bot(token=TELEGRAM_TOKEN)

def get_ohlcv(symbol, interval, limit=10):
    """Получает свечи с кэшированием"""
    cache_key = (symbol, interval)
    now = datetime.now().timestamp()
    
    # Проверка кэша
    if cache_key in ohlcv_cache:
        cached_time, cached_data = ohlcv_cache[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            return cached_data
    
    # Запрос к API
    try:
        res = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        if res['retCode'] == 0:
            data = res['result']['list']
            ohlcv_cache[cache_key] = (now, data)
            return data
        return []
    except Exception as e:
        print(f"Ошибка API ByBit ({symbol}): {e}")
        # Вернуть закэшированные данные даже если они просрочены
        if cache_key in ohlcv_cache:
            return ohlcv_cache[cache_key][1]
        return []

def check_sweep(candles):
    """
    Логика Sweep:
    candles[0] - текущая (формирующаяся)
    candles[1] - предыдущая (закрытая)
    candles[2] - пред-предыдущая
    Мы ищем снятие экстремума candles[2] свечой candles[1].
    """
    if len(candles) < 3:
        return None

    # Данные ПРЕДЫДУЩЕЙ закрытой свечи (которая должна была снять ликвидность)
    prev = {
        'high': float(candles[1][2]),
        'low': float(candles[1][3]),
        'close': float(candles[1][4])
    }
    # Данные свечи ДО НЕЕ (чей уровень снимали)
    target = {
        'high': float(candles[2][2]),
        'low': float(candles[2][3])
    }

    # SHORT: Сняли хай и закрылись ниже
    if prev['high'] > target['high'] and prev['close'] < target['high']:
        return "SHORT"
    
    # LONG: Сняли лой и закрылись выше
    if prev['low'] < target['low'] and prev['close'] > target['low']:
        return "LONG"

    return None

def is_ice_orderblock(candles, side):
    """
    Упрощенная проверка Order Block на 5м.
    Для LONG: последняя падающая свеча перед резким ростом.
    """
    if len(candles) < 3: return False
    
    c1_close = float(candles[1][4])
    c1_open = float(candles[1][1])
    c2_close = float(candles[2][4])
    c2_open = float(candles[2][1])

    if side == "LONG":
        # Бычье поглощение или просто сильная зеленая после красной
        return c1_close > c1_open and c2_close < c2_open
    else:
        # Медвежье поглощение
        return c1_close < c1_open and c2_close > c2_open

async def run_bot():
    print("🚀 Бот-сигнализатор запущен и мониторит топ-4 пар...")
    
    while True:
        # Шаг 1: Собираем все HTF свечи (1 запрос на символ)
        htf_signals = {}
        for symbol in SYMBOLS:
            await asyncio.sleep(0.5)  # Пауза между запросами
            htf_candles = get_ohlcv(symbol, HTF)
            setup_side = check_sweep(htf_candles)
            if setup_side:
                htf_signals[symbol] = (setup_side, htf_candles)
        
        # Шаг 2: Проверяем только символы с сигналом на HTF
        for symbol, (setup_side, htf_candles) in htf_signals.items():
            signal_id = f"{symbol}_{setup_side}_{htf_candles[1][0]}"
            
            if signal_id not in sent_signals:
                # 2. Подтверждение на Среднем ТФ (15M)
                await asyncio.sleep(0.5)
                mtf_candles = get_ohlcv(symbol, MTF)
                conf_side = check_sweep(mtf_candles)
                
                if conf_side == setup_side:
                    # 3. Поиск точки входа на Младшем ТФ (5M)
                    await asyncio.sleep(0.5)
                    ltf_candles = get_ohlcv(symbol, LTF)
                    if is_ice_orderblock(ltf_candles, setup_side):
                            
                            # Точка входа - текущая цена закрытия на 5M
                            entry_price = float(ltf_candles[1][4])
                            
                            # Цель (TP) - уровень ликвидности, который сняли на 1H
                            # Для LONG: TP выше входа - берем high (candles[2][2])
                            # Для SHORT: TP ниже входа - берем low (candles[2][3])
                            target_price = float(htf_candles[2][2] if setup_side == "LONG" else htf_candles[2][3])
                            
                            # Стоп-лосс (SL) - за Order Block (минимум/максимум последней свечи 5M)
                            sl_price = float(ltf_candles[1][3] if setup_side == "LONG" else ltf_candles[1][2])

                            # Логирование сигнала
                            logger.info(f"SIGNAL | {symbol} | {setup_side} | Entry: {entry_price} | TP: {target_price} | SL: {sl_price}")
                            
                            # Расчет риска и награды
                            risk = abs(entry_price - sl_price)
                            reward = abs(target_price - entry_price)
                            rr_ratio = round(reward / risk, 2) if risk > 0 else 0
                            
                            msg = (f"🔥 **{setup_side} SETUP: {symbol}**\n\n"
                                   f"✅ Контекст (1H): Ликвидность снята\n"
                                   f"✅ Подтверждение (15M): Есть\n"
                                   f"🎯 Вход (5M): Order Block найден\n\n"
                                   f"📍 **Вход:** {entry_price}\n"
                                   f"🎯 **TP:** {target_price}\n"
                                   f"🛡️ **SL:** {sl_price}\n"
                                   f"📊 **R:R:** 1:{rr_ratio}")
                            
                            try:
                                await tg_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
                                sent_signals[signal_id] = True
                                print(f"[{symbol}] Сигнал отправлен!")
                            except Exception as e:
                                print(f"Ошибка отправки в TG: {e}")

        # Пауза между циклами сканирования (120 секунд для избежания rate limit)
        await asyncio.sleep(120)

if __name__ == "__main__":
    asyncio.run(run_bot())