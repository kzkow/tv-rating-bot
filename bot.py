"""Общая оценка теханализа TradingView (таймфрейм 1 день) и отправка в Telegram.

Оценку по расписанию бот не опрашивает: alerts.py берёт её, только когда шлёт Mega Alerts, и пишет в сообщение.
Здесь — запрос оценки, ссылки на бумаги, кнопки «Купить» / «Продать» и ручная сводка.

Запуск:
    python bot.py    # сводка «Общей оценки» по всем бумагам

Без TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID сообщения печатаются в консоль, а не отправляются.
"""
import json
import os
import sys
import urllib.error
import urllib.request

# Тикеры в формате TradingView: <биржа>:<тикер>.
# Топ-30 по весу в индексе Мосбиржи IMOEX на 09.10.2026 без GAZP, VTBR, SNGS, TATN, IRAO, CBOM, RUAL.
TICKERS = [
    "RUS:LKOH", "RUS:SBER", "RUS:YDEX", "RUS:T", "RUS:NVTK",
    "RUS:GMKN", "RUS:OZON", "RUS:ROSN", "RUS:X5", "RUS:SBERP",
    "RUS:PLZL", "RUS:SNGSP", "RUS:TATNP", "RUS:HEAD", "RUS:MOEX",
    "RUS:MTSS", "RUS:CHMF", "RUS:NLMK", "RUS:PHOR", "RUS:SVCB",
    "RUS:DOMRF", "RUS:MAGN", "RUS:TRNFP",
]

SCANNER_URL = "https://scanner.tradingview.com/russia/scan"
# Общая оценка на дневном таймфрейме. Другие таймфреймы — суффиксом: "Recommend.All|60", "|1W" и т.д.
COLUMN = "Recommend.All"


def rating(value):
    """Число от -1 до 1 -> оценка, пороги как на сайте TradingView."""
    if value < -0.5:
        return "Активно продавать"
    if value < -0.1:
        return "Продавать"
    if value <= 0.1:
        return "Нейтрально"
    if value <= 0.5:
        return "Покупать"
    return "Активно покупать"


def fetch_values(timeout=30):
    """Значения «Общей оценки» по бумагам: {"RUS:SBER": 0.38, ...}; бумаги без оценки пропущены."""
    body = json.dumps({"symbols": {"tickers": TICKERS}, "columns": [COLUMN]}).encode()
    req = urllib.request.Request(SCANNER_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)

    values = {row["s"]: row["d"][0] for row in data["data"] if row["d"][0] is not None}
    missing = [t for t in TICKERS if t not in values]
    if missing:
        print(f"Нет оценки для: {', '.join(missing)}", file=sys.stderr)
    return values


def ticker_link(ticker):
    """Ссылка на бумагу в Т-Инвестициях.

    tbank.ru/invest/stocks/* — универсальная ссылка приложения «Т-Инвестиции» (apple-app-site-association,
    assetlinks.json на tbank.ru): на телефоне с приложением открывается в нём, без приложения — на сайте.
    """
    symbol = ticker.split(":")[1]
    return f'<a href="https://www.tbank.ru/invest/stocks/{symbol}/">{symbol}</a>'


def trade_buttons(ticker):
    """Кнопки «Купить» и «Продать» (1 лот по рынку). Нажатие обрабатывает tv-bot-serverless,
    заявку выставляет приватный репозиторий."""
    symbol = ticker.split(":")[1]
    return {"inline_keyboard": [[
        {"text": "Купить", "callback_data": f"buy:{symbol}"},
        {"text": "Продать", "callback_data": f"sell:{symbol}"},
    ]]}


def send(text, silent=False, trade=None):
    """trade — тикер («RUS:SBER»): под сообщением будут кнопки «Купить» и «Продать»."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[dry-run{', silent' if silent else ''}{', trade' if trade else ''}] {text}")
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "disable_notification": silent,
    }
    if trade:
        payload["reply_markup"] = trade_buttons(trade)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if trade and e.code == 400:
            # Кнопки не должны стоить алерта: если Telegram их отверг, шлём то же сообщение без них.
            print(f"Telegram отверг сообщение с кнопками, шлю без них: {detail}", file=sys.stderr)
            send(text, silent)
            return
        raise RuntimeError(f"Telegram ответил {e.code}: {detail}") from None


def summary():
    values = fetch_values()
    lines = [f"{ticker_link(t)}: {rating(values[t])}" for t in TICKERS if t in values]
    send("Оценка TradingView (1 день):\n" + "\n".join(lines))
    print(f"Сводка: оценка есть по {len(values)}/{len(TICKERS)}")


if __name__ == "__main__":
    summary()
