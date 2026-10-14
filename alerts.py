"""Mega Alerts MOEX ALGOPACK: присылает в Telegram новые торговые аномалии по тикерам бота.

Запуск:
    python alerts.py    # нужен MOEX_ALGOPACK_TOKEN; без TELEGRAM_* локально сообщения печатаются в консоль

Источник — https://apim.moex.com/iss/datashop/algopack/eq/alerts.json (тариф ALGOPACK Promo).
Состояние — alerts_state.json. Репозиторий публичный, поэтому там только HMAC-хэши ключей алертов,
а в лог пишутся только счётчики: содержимое алертов (данные MOEX) уходит только в Telegram.
"""
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import TICKERS, send, ticker_link

APIM_URL = "https://apim.moex.com/iss/datashop/algopack/eq/alerts.json"
# Сертификат apim выпущен НУЦ Минцифры; корня нет в стандартных хранилищах, поэтому он лежит в репозитории.
CERT_FILE = Path(__file__).with_name("certs") / "russian_trusted_root_ca.pem"
# SHA-256 DER корня «Russian Trusted Root CA», действует до 2032-02-27.
CERT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"
# ALERTS_STATE_FILE — для локальных запусков, чтобы не трогать файл, который коммитит workflow.
STATE_FILE = Path(os.environ.get("ALERTS_STATE_FILE") or Path(__file__).with_name("alerts_state.json"))

MSK = timezone(timedelta(hours=3))
SECIDS = {t.split(":")[1] for t in TICKERS}
REQUIRED_COLUMNS = {"tradedate", "tradetime", "secid", "alert_type"}

REQUEST_TIMEOUT = 20      # секунд на запрос
FETCH_DEADLINE = 60       # секунд на все запросы запуска (шаг workflow ограничен 2 минутами)
MAX_PAGES = 20            # по 1000 строк; за день по всему рынку — сотни строк
REPEAT_WINDOW = 30        # минут: тот же тип по той же бумаге в этом окне — повтор, он сворачивается
LATE_AFTER = 15           # минут: пачка старше — помечается «с опозданием»
ERRORS_BEFORE_NOTICE = 3  # запусков подряд с ошибкой до сообщения «недоступны» (~15 минут)
MAX_MESSAGE = 4000        # лимит Telegram — 4096 символов

LABELS = {
    "vol_99_9_pctl": "Аномальный объём",
    "vol_b_99_9_pctl": "Крупные покупки",
    "vol_s_99_9_pctl": "Крупные продажи",
    "vol_max": "Рекорд объёма за 90 дней",
    "vol_b_max": "Рекорд покупок за 90 дней",
    "vol_s_max": "Рекорд продаж за 90 дней",
    "net_vol_99_9_pctl+": "Перевес покупок",
    "net_vol_99_9_pctl-": "Перевес продаж",
    "net_vol_max": "Рекордный перевес покупок",
    "net_vol_min": "Рекордный перевес продаж",
    "pr_change_99_9_pctl+": "Резкий рост",
    "pr_change_99_9_pctl-": "Резкое падение",
    "pr_change_max": "Рекордный рост за минуту",
    "pr_change_min": "Рекордное падение за минуту",
    "pr_high_max": "Максимум цены за 90 дней",
    "pr_low_min": "Минимум цены за 90 дней",
}


class ApimError(Exception):
    """Сбой apim (сеть, TLS, HTTP-статус, формат ответа). code — короткий стабильный код для состояния."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Редиректы не выполняем: urllib унёс бы заголовок Authorization на другой хост. 3xx станет HTTPError.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def apim_opener():
    pem = CERT_FILE.read_text(encoding="ascii")
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if digest != CERT_SHA256:
        raise RuntimeError(f"{CERT_FILE.name}: отпечаток {digest} не совпадает с ожидаемым")
    # Системные корни + корень Минцифры, только для apim. create_default_context(cafile=...) заменил бы системные.
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=pem)
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect)


def get_page(opener, token, day, start):
    """Одна страница ответа как dict; None, если за день данных нет (404)."""
    query = urllib.parse.urlencode({"date": day.isoformat(), "start": start})
    req = urllib.request.Request(
        f"{APIM_URL}?{query}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise ApimError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise ApimError("TLS") from None
        if isinstance(e.reason, TimeoutError):
            raise ApimError("timeout") from None
        raise ApimError("сеть") from None
    except TimeoutError:
        raise ApimError("timeout") from None
    except ssl.SSLError:
        raise ApimError("TLS") from None
    except (OSError, http.client.HTTPException):
        raise ApimError("сеть") from None
    except ValueError:
        # http.client не пропускает ключ с переводом строки или не-латиницей. Текст исключения
        # с заголовком Authorization в публичный лог не печатаем.
        raise ApimError("ключ") from None

    # Без подписки ISS отдаёт 200 с HTML-страницей — статусу 200 не верим, смотрим тип.
    if "json" not in content_type.lower():
        raise ApimError("не JSON")
    try:
        return json.loads(body)
    except ValueError:
        raise ApimError("не JSON") from None


def parse_page(page):
    """Строки страницы (dict с колонками в нижнем регистре) и TOTAL из курсора, если есть."""
    block = page.get("data") if isinstance(page, dict) else None
    if not isinstance(block, dict) or not isinstance(block.get("columns"), list) or not isinstance(block.get("data"), list):
        raise ApimError("формат")
    columns = [str(c).lower() for c in block["columns"]]
    if not REQUIRED_COLUMNS <= set(columns):
        raise ApimError("формат")
    rows = [dict(zip(columns, row)) for row in block["data"] if isinstance(row, list)]
    if len(rows) != len(block["data"]):
        print(f"Mega Alerts: пропущено битых строк: {len(block['data']) - len(rows)}", file=sys.stderr)

    # Курсор нужен только чтобы не делать лишний запрос: если он странный, листаем до пустой страницы.
    total = None
    try:
        cursor = page["data.cursor"]
        total = cursor["data"][0][[str(c).upper() for c in cursor["columns"]].index("TOTAL")]
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return rows, total


def fetch_day(opener, token, day, deadline):
    rows, start = [], 0
    for _ in range(MAX_PAGES):
        if time.monotonic() > deadline:
            raise ApimError("timeout")
        page = get_page(opener, token, day, start)
        if page is None:
            break
        page_rows, total = parse_page(page)
        if not page_rows:
            break
        rows.extend(page_rows)
        start += len(page_rows)
        if isinstance(total, int) and start >= total:
            break
    else:
        print(f"Mega Alerts: за {day} больше {MAX_PAGES} страниц, остальное не прочитано", file=sys.stderr)
    return rows


def select(rows, day):
    """Алерты по нашим тикерам за этот день; битые строки пропускаются."""
    alerts, bad = [], 0
    for row in rows:
        secid = row.get("secid")
        if not isinstance(secid, str) or secid not in SECIDS or str(row.get("tradedate")) != day.isoformat():
            continue
        try:
            moment = datetime.strptime(str(row["tradetime"])[:8], "%H:%M:%S")
            alerts.append({
                "time": moment.strftime("%H:%M:%S"),
                "minute": moment.hour * 60 + moment.minute,
                "secid": secid,
                "type": str(row["alert_type"]),
            })
        except (KeyError, ValueError):
            bad += 1
    if bad:
        print(f"Mega Alerts: пропущено битых строк: {bad}", file=sys.stderr)
    return alerts


class Keys:
    """Ключи дедупа как HMAC от API-ключа: в публичном state.json не видно, какие алерты были."""

    def __init__(self, secret):
        self.secret = secret.encode()
        self.kid = self._digest("kid")[:8]

    def _digest(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def of(self, day, alert):
        return self._digest(f"{day.isoformat()}|{alert['time']}|{alert['secid']}|{alert['type']}")[:16]


def label(alert_type):
    return LABELS.get(alert_type, html.escape(alert_type))


def episode_starts(day_alerts):
    """Минута начала эпизода для каждого (бумага, тип, минута).

    Эпизод — срабатывания одного типа по одной бумаге с разрывами не больше REPEAT_WINDOW минут.
    """
    chains = {}
    for a in sorted(day_alerts, key=lambda a: a["minute"]):
        chains.setdefault((a["secid"], a["type"]), []).append(a["minute"])
    starts = {}
    for (secid, alert_type), minutes in chains.items():
        start = previous = None
        for minute in minutes:
            if previous is None or minute - previous > REPEAT_WINDOW:
                start = minute
            starts[(secid, alert_type, minute)] = start
            previous = minute
    return starts


def build_lines(new, day_alerts):
    """Строки сообщения, одна на бумагу: (порядок, текст, ключи, только_повторы).

    «SBER: Резкий рост, Крупные покупки ×3»: причины в порядке первого срабатывания, ×N — тип сработал
    в пачке N раз. Строка из одних повторов (срабатываний внутри уже начатого эпизода) — беззвучная.
    """
    starts = episode_starts(day_alerts)
    by_secid = {}
    for key, alert in sorted(new, key=lambda item: item[1]["time"]):
        entry = by_secid.setdefault(alert["secid"], {}).setdefault(
            alert["type"], {"time": alert["time"], "keys": [], "first": False})
        entry["keys"].append(key)
        if starts[(alert["secid"], alert["type"], alert["minute"])] == alert["minute"]:
            entry["first"] = True

    lines = []
    for secid, types in by_secid.items():
        reasons = [label(t) + (f" ×{len(e['keys'])}" if len(e["keys"]) > 1 else "") for t, e in types.items()]
        text = f"{ticker_link('RUS:' + secid)}: " + ", ".join(reasons)
        first_time = min(e["time"] for e in types.values())
        keys = [k for e in types.values() for k in e["keys"]]
        repeats_only = not any(e["first"] for e in types.values())
        lines.append(((first_time, secid), text, keys, repeats_only))

    lines.sort(key=lambda line: line[0])
    return lines


def split_messages(lines, header):
    """Части не длиннее MAX_MESSAGE, режем только между строками: (текст, ключи, беззвучно).

    Заголовок («За 13.10:», «С опозданием:») повторяется в каждой части.
    """
    head = [header] if header else []
    messages, texts, keys, repeats_only = [], list(head), [], True
    for _, text, line_keys, repeat in lines:
        if keys and len("\n".join(texts + [text])) > MAX_MESSAGE:
            messages.append(("\n".join(texts), keys, repeats_only))
            texts, keys, repeats_only = list(head), [], True
        texts.append(text)
        keys = keys + line_keys
        repeats_only = repeats_only and repeat
    if keys:
        messages.append(("\n".join(texts), keys, repeats_only))
    return messages


def deliver(text, silent):
    try:
        send(text, silent=silent)
    except RuntimeError as e:
        # 429 Too Many Requests: Telegram говорит, сколько подождать. Ждём один раз.
        match = re.search(r'"retry_after":\s*(\d+)', str(e))
        if not match:
            raise
        time.sleep(min(int(match.group(1)), 30) + 1)
        send(text, silent=silent)


def load_state(kid):
    fresh = {"kid": kid, "date": None, "seen": None, "error": None, "error_count": 0, "error_notified": False}
    if not STATE_FILE.exists():
        return fresh
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if state.get("kid") != kid:
        # Сменился API-ключ — старые хэши несравнимы: начинаем заново молча, статус ошибок сохраняем.
        for field in ("error", "error_count", "error_notified"):
            if field in state:
                fresh[field] = state[field]
        return fresh
    return {**fresh, **state}


def save_state(state):
    text = json.dumps(state, ensure_ascii=False, indent=1) + "\n"
    if not STATE_FILE.exists() or STATE_FILE.read_text(encoding="utf-8") != text:
        STATE_FILE.write_text(text, encoding="utf-8")


def register_error(state, code):
    print(f"Mega Alerts: ошибка apim: {code}", file=sys.stderr)
    if state["error_notified"]:
        return  # уже сообщили; не трогаем состояние, чтобы не коммитить каждые 5 минут
    state["error"] = code
    state["error_count"] += 1
    if state["error_count"] >= ERRORS_BEFORE_NOTICE:
        send(f"Mega Alerts недоступны: {html.escape(code)}")
        state["error_notified"] = True
    save_state(state)


def register_ok(state):
    if state["error_notified"]:
        send("Mega Alerts снова работают")
    state.update(error=None, error_count=0, error_notified=False)
    # Сразу на диск: если дальше упадёт отправка алертов, «снова работают» не придёт второй раз.
    save_state(state)


def process(state, keys, day, alerts, now, header=None):
    """Отправить новые алерты дня; ключи каждой доставленной части сразу пишутся в состояние."""
    by_key = {keys.of(day, a): a for a in alerts}
    seen = set(state["seen"])
    new = [(k, a) for k, a in by_key.items() if k not in seen]
    if not new:
        return 0, 0

    oldest = min(datetime.combine(day, datetime.strptime(a["time"], "%H:%M:%S").time(), MSK) for _, a in new)
    if header is None and now - oldest > timedelta(minutes=LATE_AFTER):
        header = "С опозданием:"

    messages = split_messages(build_lines(new, list(by_key.values())), header)
    for i, (text, message_keys, silent) in enumerate(messages):
        if i:
            time.sleep(1)
        deliver(text, silent)
        seen.update(message_keys)
        state["seen"] = sorted(seen)
        save_state(state)
    return len(new), len(messages)


def main():
    # strip: при вставке в форму секрета GitHub легко прихватить перевод строки.
    token = os.environ.get("MOEX_ALGOPACK_TOKEN", "").strip()
    if not token:
        print("Mega Alerts: MOEX_ALGOPACK_TOKEN не задан, пропускаю")
        return
    if os.environ.get("GITHUB_ACTIONS") and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        # Без Telegram send() напечатал бы алерты в публичный лог Actions.
        sys.exit("Mega Alerts: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")

    keys = Keys(token)
    state = load_state(keys.kid)
    now = datetime.now(MSK)
    today = now.date()
    yesterday = today - timedelta(days=1)
    baselined = state["seen"] is not None and state["date"] is not None
    catch_up = baselined and state["date"] == yesterday.isoformat()

    opener = apim_opener()
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        tail_rows = fetch_day(opener, token, yesterday, deadline) if catch_up else []
        rows = fetch_day(opener, token, today, deadline)
    except ApimError as e:
        register_error(state, e.code)
        return

    register_ok(state)
    alerts = select(rows, today)

    if not baselined or state["date"] not in (today.isoformat(), yesterday.isoformat()):
        # Первый успешный запрос (или разрыв больше суток): запоминаем день молча, без потока с утра.
        state.update(date=today.isoformat(), seen=sorted({keys.of(today, a) for a in alerts}))
        save_state(state)
        print(f"Mega Alerts: базовая точка, строк за день {len(rows)}, по нашим тикерам {len(alerts)}")
        return

    if catch_up:
        tail = select(tail_rows, yesterday)
        new, parts = process(state, keys, yesterday, tail, now, header=f"За {yesterday:%d.%m}:")
        print(f"Mega Alerts: хвост за {yesterday:%d.%m}: строк {len(tail_rows)}, по нашим тикерам {len(tail)}, "
              f"новых {new}, отправлено частей {parts}")
        state.update(date=today.isoformat(), seen=[])

    new, parts = process(state, keys, today, alerts, now)
    save_state(state)
    print(f"Mega Alerts: строк за день {len(rows)}, по нашим тикерам {len(alerts)}, новых {new}, "
          f"отправлено частей {parts}")


if __name__ == "__main__":
    main()
