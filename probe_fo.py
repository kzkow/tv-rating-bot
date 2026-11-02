"""Разовая проверка: отдаёт ли ключ ALGOPACK Mega Alerts срочного (fo) и валютного (fx) рынков; eq — для сравнения.

Результат — JSON в каталог из аргумента (приватный tv-history), в лог — только статусы и счётчики:
данные MOEX в публичный лог не пишем. Удалить после проверки.
"""
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import alerts

SAMPLE = 300  # строк каждого рынка в файл, кроме счётчиков


def probe(opener, token, market, day):
    alerts.APIM_URL = f"https://apim.moex.com/iss/datashop/algopack/{market}/alerts.json"
    result = {"market": market, "date": day.isoformat()}
    try:
        page = alerts.get_page(opener, token, day, 0)
    except alerts.ApimError as e:
        return {**result, "status": e.code}
    if page is None:
        return {**result, "status": "404"}
    block = page.get("data") if isinstance(page, dict) else None
    result["blocks"] = list(page) if isinstance(page, dict) else None
    result["columns"] = block.get("columns") if isinstance(block, dict) else None
    try:
        rows = alerts.fetch_day(opener, token, day, time.monotonic() + 60)
    except alerts.ApimError as e:
        return {**result, "status": e.code}
    return {
        **result,
        "status": "ok",
        "rows": len(rows),
        "by_secid": Counter(r["secid"] for r in rows).most_common(),
        "by_type": Counter(r["alert_type"] for r in rows).most_common(),
        "sample": rows[:SAMPLE],
    }


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("MOEX_ALGOPACK_TOKEN", "").strip()
    if not token:
        sys.exit("нет MOEX_ALGOPACK_TOKEN")
    opener = alerts.apim_opener()
    day = datetime.now(alerts.MSK).date()
    for market in ("eq", "fo", "fx"):
        result = probe(opener, token, market, day)
        print(f"{market}: {result['status']}, строк {result.get('rows', '-')}, "
              f"бумаг {len(result.get('by_secid', []))}, типов {len(result.get('by_type', []))}")
        (out / f"{market}-alerts-{day}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
