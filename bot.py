"""Следит за общей оценкой теханализа TradingView (таймфрейм 1 день) и шлёт в Telegram смены оценки.

Запуск:
    python bot.py              # одна проверка: сравнить с state.json, прислать смены
    python bot.py --summary    # то же + сводка по всем бумагам
    python bot.py --loop 60    # проверять каждые 60 секунд (для своей машины/VPS)

Без TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID сообщения печатаются в консоль, а не отправляются.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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
STATE_FILE = Path(__file__).with_name("state.json")


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


def side(label):
    """Сторона оценки: +1 — покупать, -1 — продавать, 0 — нейтрально."""
    if label in ("Покупать", "Активно покупать"):
        return 1
    if label in ("Продавать", "Активно продавать"):
        return -1
    return 0


def should_notify(notified, new):
    """Сообщаем только о смене стороны относительно последней присланной оценки.

    Уход в «Нейтрально» и смена силы внутри стороны — молча: за 12–14.10 из 100 смен 78 были
    качелями «туда-обратно» (Покупать ↔ Нейтрально, Покупать ↔ Активно покупать), смен стороны — около 10.
    """
    return side(new) != 0 and side(new) != side(notified)


def fetch_values():
    body = json.dumps({"symbols": {"tickers": TICKERS}, "columns": [COLUMN]}).encode()
    req = urllib.request.Request(SCANNER_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
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


def buy_button(ticker):
    """Кнопка «Купить 1 лот». Нажатие обрабатывает tv-bot-serverless, заявку выставляет приватный репозиторий."""
    return {"inline_keyboard": [[{"text": "Купить 1 лот", "callback_data": f"buy:{ticker.split(':')[1]}"}]]}


def send(text, silent=False, buy=None):
    """buy — тикер («RUS:SBER»): под сообщением будет кнопка «Купить 1 лот»."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[dry-run{', silent' if silent else ''}{', buy' if buy else ''}] {text}")
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "disable_notification": silent,
    }
    if buy:
        payload["reply_markup"] = buy_button(buy)
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
        if buy and e.code == 400:
            # Кнопка не должна стоить алерта: если Telegram её отверг, шлём то же сообщение без неё.
            print(f"Telegram отверг сообщение с кнопкой, шлю без неё: {detail}", file=sys.stderr)
            send(text, silent)
            return
        raise RuntimeError(f"Telegram ответил {e.code}: {detail}") from None


def load_state():
    """{тикер: {"rating": текущая оценка, "notified": последняя присланная}}."""
    if not STATE_FILE.exists():
        return {}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    # Старый формат — строка с оценкой: она же и последняя присланная.
    return {t: v if isinstance(v, dict) else {"rating": v, "notified": v} for t, v in state.items()}


def check(summary=False):
    state = load_state()
    values = fetch_values()

    new_state = {}
    changes = []
    for ticker in TICKERS:
        old = state.get(ticker)
        if ticker not in values:
            # Нет данных в этот раз — оставляем прошлую оценку.
            if old:
                new_state[ticker] = old
            continue
        label = rating(values[ticker])
        if old is None:
            # Тикер без прошлой оценки (первый запуск, новый тикер) запоминаем молча.
            new_state[ticker] = {"rating": label, "notified": label}
            continue
        notified = old["notified"]
        if should_notify(notified, label):
            changes.append((ticker, f"{ticker_link(ticker)}: {notified} → <b>{label}</b>"))
            notified = label
        new_state[ticker] = {"rating": label, "notified": notified}

    # Одно сообщение на бумагу. Состояние пишем после отправки: если Telegram упал на середине,
    # у неотправленных бумаг «последняя присланная» откатывается, и их смена уйдёт при следующей проверке.
    unsent = [t for t, _ in changes]
    try:
        for i, (ticker, text) in enumerate(changes):
            if i:
                time.sleep(1)
            send(text, buy=ticker)
            unsent.remove(ticker)
        if summary:
            lines = [f"{ticker_link(t)}: {rating(values[t])}" for t in TICKERS if t in values]
            send("Оценка TradingView (1 день):\n" + "\n".join(lines))
    finally:
        for ticker in unsent:
            new_state[ticker]["notified"] = state[ticker]["notified"]
        # Сравниваем текст, а не dict: файл в старом формате перепишется в новый.
        text = json.dumps(new_state, ensure_ascii=False, indent=2) + "\n"
        if not STATE_FILE.exists() or STATE_FILE.read_text(encoding="utf-8") != text:
            STATE_FILE.write_text(text, encoding="utf-8")
    print(f"Проверено {len(values)}/{len(TICKERS)}, смен: {len(changes)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true", help="прислать сводку по всем бумагам")
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="проверять в цикле с этим интервалом")
    args = parser.parse_args()

    if not args.loop:
        check(summary=args.summary)
        return

    summary = args.summary
    while True:
        try:
            check(summary=summary)
            summary = False
        except Exception as e:  # в цикле не падаем из-за разового сбоя сети
            print(f"Ошибка: {e}", file=sys.stderr)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
