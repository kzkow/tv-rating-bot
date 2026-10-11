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
# Топ-30 по весу в индексе Мосбиржи IMOEX на 09.10.2026.
TICKERS = [
    "RUS:LKOH", "RUS:SBER", "RUS:GAZP", "RUS:TATN", "RUS:YDEX",
    "RUS:T", "RUS:NVTK", "RUS:GMKN", "RUS:OZON", "RUS:ROSN",
    "RUS:X5", "RUS:VTBR", "RUS:SBERP", "RUS:PLZL", "RUS:SNGSP",
    "RUS:SNGS", "RUS:TATNP", "RUS:HEAD", "RUS:MOEX", "RUS:IRAO",
    "RUS:MTSS", "RUS:CHMF", "RUS:CBOM", "RUS:NLMK", "RUS:RUAL",
    "RUS:PHOR", "RUS:SVCB", "RUS:DOMRF", "RUS:MAGN", "RUS:TRNFP",
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
    exchange, symbol = ticker.split(":")
    return f'<a href="https://ru.tradingview.com/symbols/{exchange}-{symbol}/technicals/">{symbol}</a>'


def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[dry-run] {text}")
        return

    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram ответил {e.code}: {e.read().decode(errors='replace')}") from None


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def check(summary=False):
    state = load_state()
    values = fetch_values()

    new_state = {}
    changes = []
    for ticker in TICKERS:
        if ticker not in values:
            # Нет данных в этот раз — оставляем прошлую оценку.
            if ticker in state:
                new_state[ticker] = state[ticker]
            continue
        label = rating(values[ticker])
        new_state[ticker] = label
        # Тикер без прошлой оценки (первый запуск, новый тикер) запоминаем молча.
        if ticker in state and state[ticker] != label:
            changes.append(f"{ticker_link(ticker)}: {state[ticker]} → <b>{label}</b> ({values[ticker]:+.2f})")

    if changes:
        send("Смена оценки TradingView (1 день):\n" + "\n".join(changes))
    if summary:
        lines = [f"{ticker_link(t)}: {rating(values[t])} ({values[t]:+.2f})" for t in TICKERS if t in values]
        send("Оценка TradingView (1 день):\n" + "\n".join(lines))

    # Состояние пишем после отправки: если Telegram упал, смена уйдёт при следующей проверке.
    if new_state != state:
        STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
