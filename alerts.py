"""Mega Alerts MOEX ALGOPACK: присылает в Telegram новые торговые аномалии по тикерам бота
с текущей «Общей оценкой» TradingView (1 день, в скобках — 1, 2 и 4 часа) последней строкой сообщения,
и по фьючерсам — макро (доллар, юань, нефть, индексы, золото) и ближайшим на те же акции — без кнопок,
с дневной оценкой TradingView, если она там есть.

Запуск:
    python alerts.py    # нужен MOEX_ALGOPACK_TOKEN; без TELEGRAM_* локально сообщения печатаются в консоль

Источник — https://apim.moex.com/iss/datashop/algopack/{eq,fo}/alerts.json (тариф ALGOPACK Promo).
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot import COLUMN, TICKERS, fetch_values, rating, send, ticker_link

APIM_URL = "https://apim.moex.com/iss/datashop/algopack/{market}/alerts.json"  # eq — акции, fo — фьючерсы
# Сертификат apim выпущен НУЦ Минцифры; корня нет в стандартных хранилищах, поэтому он лежит в репозитории.
CERT_FILE = Path(__file__).with_name("certs") / "russian_trusted_root_ca.pem"
# SHA-256 DER корня «Russian Trusted Root CA», действует до 2032-02-27.
CERT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"
# ALERTS_STATE_FILE — для локальных запусков, чтобы не трогать файл, который коммитит workflow.
STATE_FILE = Path(os.environ.get("ALERTS_STATE_FILE") or Path(__file__).with_name("alerts_state.json"))

MSK = timezone(timedelta(hours=3))
SECIDS = {t.split(":")[1] for t in TICKERS}
# Фьючерсы: базовый актив (asset_code) → название в сообщении. Фильтр по активу, а не по контракту:
# MXZ6 при экспирации сменится на MXH7. Вечные фьючерсы — свой актив (USDRUBTOM, CNYRUBTOM, GLDRUBTOM).
# Срочные доллар (Si), юань (CNY) и золото (GOLD) убраны 02.11.2026 по решению владельца — остались вечные.
FUTURES = {
    "USDRUBTOM": "доллар",
    "CNYRUBTOM": "юань",
    "BR": "нефть Brent",
    "MIX": "индекс Мосбиржи",
    "RTS": "индекс РТС",
    "GLDRUBTOM": "золото",
}
# Фьючерсы на акции из TICKERS (с 03.11.2026) — тоже по активу, но только ближайший контракт (nearest_contract).
# Обычно актив = тикер акции (VKCO: VKZ6, VKH7), здесь — где MOEX называет его иначе. TATN, SNGR — обычки, не наши.
SHARE_ASSETS = {
    "SBRF": "SBER", "SBPR": "SBERP", "NOTKM": "NVTK", "PLZLM": "PLZL", "SNGP": "SNGSP",
    "TATP": "TATNP", "MTSI": "MTSS", "CHMFM": "CHMF", "TRNF": "TRNFP",
}
QUARTER_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}  # месяц экспирации → буква в коде контракта (SRZ6 — декабрь 2026)
# Группы фьючерсов в state["markets"]: новая группа в работающем боте начинает с молчаливой базовой точки.
FO_SCOPES = ["fo", "fo-shares"]
REQUIRED_COLUMNS = {"tradedate", "tradetime", "secid", "alert_type"}

REQUEST_TIMEOUT = 20      # секунд на запрос
FETCH_DEADLINE = 60       # секунд на все запросы запуска (шаг workflow ограничен 2 минутами)
MAX_PAGES = 20            # по 1000 строк; за день по всему рынку — сотни строк
LATE_AFTER = 15           # минут: пачка старше — помечается «с опозданием»
REPEAT_MINUTES = 30       # тот же тип по той же бумаге — не чаще раза в 30 минут
ERRORS_BEFORE_NOTICE = 3  # запусков подряд с ошибкой до сообщения «недоступны» (~15 минут)
RATING_HOURS = (1, 2, 4)  # таймфреймы оценки в скобках после дневной, часов

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
    # Только у фьючерсов: открытый интерес — сколько контрактов открыто, его скачок — крупный вход или выход.
    "oi_close_change_99_9_pctl+": "Резкий рост открытого интереса",
    "oi_close_change_99_9_pctl-": "Резкое падение открытого интереса",
}
# Не присылаются по решению владельца (22.10.2026): общий объём за минуту без направления сделок.
SKIPPED_TYPES = {"vol_99_9_pctl", "vol_max"}


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


def get_page(opener, token, day, start, market="eq"):
    """Одна страница ответа как dict; None, если за день данных нет (404)."""
    query = urllib.parse.urlencode({"date": day.isoformat(), "start": start})
    req = urllib.request.Request(
        f"{APIM_URL.format(market=market)}?{query}",
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


def fetch_day(opener, token, day, deadline, market="eq"):
    rows, start = [], 0
    for _ in range(MAX_PAGES):
        if time.monotonic() > deadline:
            raise ApimError("timeout")
        page = get_page(opener, token, day, start, market)
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


def future_of(asset):
    """Фьючерс по активу: (подпись в сообщении, группа FO_SCOPES); None — актив не наш.
    Подпись — название макро-актива («доллар») или тикер акции («SBER»)."""
    if not isinstance(asset, str):
        return None
    if asset in FUTURES:
        return FUTURES[asset], "fo"
    share = SHARE_ASSETS.get(asset, asset)
    return (share, "fo-shares") if share in SECIDS else None


def nearest_contract(day):
    """Окончание кода ближайшего фьючерса на акцию на этот день: «Z6» — декабрь 2026.

    Фьючерсы на акции — квартальные, экспирация в третью пятницу марта, июня, сентября и декабря.
    В день экспирации это ещё истекающий контракт, со следующего дня — следующий. Перенос экспирации
    из-за праздника не учитывается. Вечные (SBERF) сюда не попадают: в конце кода нет цифры года.
    """
    year, month = day.year, day.month
    while True:
        if month in QUARTER_CODES:
            first = date(year, month, 1)
            if day <= first + timedelta(days=(4 - first.weekday()) % 7 + 14):
                return f"{QUARTER_CODES[month]}{year % 10}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def tv_future(secid, day):
    """Символ фьючерса в TradingView — с полным годом: SRZ6 → RUS:SRZ2026. Вечных фьючерсов MOEX
    (USDRUBF, SBERF) в TradingView нет — None. Есть не все контракты: из наших акций на 03.11.2026 — 13 из 25."""
    if not secid[-1:].isdigit():
        return None
    year = day.year + (int(secid[-1]) - day.year) % 10
    return f"RUS:{secid[:-1]}{year}"


def select(rows, day, market="eq"):
    """Алерты по нашим тикерам (или фьючерсам: макро и ближайшим на наши акции) за этот день, кроме SKIPPED_TYPES;
    битые строки пропускаются."""
    alerts, bad = [], 0
    nearest = nearest_contract(day)
    for row in rows:
        secid = row.get("secid")
        if not isinstance(secid, str) or str(row.get("tradedate")) != day.isoformat():
            continue
        if market == "eq":
            if secid not in SECIDS:
                continue
            underlying, scope, tv = None, "eq", "RUS:" + secid
        else:
            future = future_of(row.get("asset_code"))
            if future is None or (future[1] == "fo-shares" and not secid.endswith(nearest)):
                continue
            (underlying, scope), tv = future, tv_future(secid, day)
        if str(row.get("alert_type")) in SKIPPED_TYPES:
            continue
        try:
            moment = datetime.strptime(str(row["tradetime"])[:8], "%H:%M:%S")
            alerts.append({
                "time": moment.strftime("%H:%M:%S"),
                "minute": moment.hour * 60 + moment.minute,
                "secid": secid,
                "type": str(row["alert_type"]),
                "underlying": underlying,
                "scope": scope,
                "tv": tv,
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


def split_repeats(new, day_alerts):
    """Делит новые алерты на отправляемые и повторы: тип по бумаге — не чаще раза в REPEAT_MINUTES.

    «Максимум цены за 90 дней», например, срабатывает каждую минуту, пока цена обновляет максимум, —
    X5 15.10 дал 13 сообщений за два часа. По алертам дня для каждой пары (бумага, тип) отправляемыми
    считаются срабатывания, отстоящие от предыдущего отправляемого не меньше чем на REPEAT_MINUTES
    (скользящее окно, не календарный час). Считается из данных дня, состояние не нужно.
    """
    minutes = {}
    for a in day_alerts:
        minutes.setdefault((a["secid"], a["type"]), set()).add(a["minute"])
    send = {}
    for pair, ms in minutes.items():
        last, chosen = None, set()
        for m in sorted(ms):
            if last is None or m - last >= REPEAT_MINUTES:
                chosen.add(m)
                last = m
        send[pair] = chosen
    firsts, repeats = [], []
    for key, alert in new:
        is_first = alert["minute"] in send[(alert["secid"], alert["type"])]
        (firsts if is_first else repeats).append((key, alert))
    return firsts, repeats


def future_link(secid):
    """Фьючерс в Т-Инвестициях: страница контракта, как ticker_link у акций."""
    return f'<a href="https://www.tbank.ru/invest/futures/{secid}/">{secid}</a>'


def build_lines(firsts, day):
    """Строки сообщения, одна на бумагу: (порядок, текст, ключи, символ TradingView). «27/10 SBER: Резкий рост,
    Крупные покупки», у фьючерса — с активом: «27/10 USDRUBF (доллар): Перевес продаж», «27/10 SRZ6 (SBER): …»."""
    by_secid, underlyings, symbols = {}, {}, {}
    for key, alert in sorted(firsts, key=lambda item: item[1]["time"]):
        types = by_secid.setdefault(alert["secid"], {})
        types.setdefault(alert["type"], {"time": alert["time"], "keys": []})["keys"].append(key)
        underlyings[alert["secid"]] = alert.get("underlying")
        symbols[alert["secid"]] = alert.get("tv")

    lines = []
    for secid, types in by_secid.items():
        underlying = underlyings[secid]
        name = f"{future_link(secid)} ({underlying})" if underlying else ticker_link("RUS:" + secid)
        text = f"{day:%d/%m} {name}: " + ", ".join(label(t) for t in types)
        first_time = min(e["time"] for e in types.values())
        lines.append(((first_time, secid), text, [k for e in types.values() for k in e["keys"]], symbols[secid]))

    lines.sort(key=lambda line: line[0])
    return lines


def rating_line(values):
    """(1 день, 1 ч, 2 ч, 4 ч) → «покупать (1 покупать, 2 нейтрально, 4 продавать)»; нет значения — «—».
    Нет ни одной часовой (у фьючерсов TradingView отдаёт только дневную) — без скобок: «покупать»."""
    day, *hours = (rating(v).lower() if v is not None else "—" for v in values)
    if all(v is None for v in values[1:]):
        return day
    return f"{day} ({', '.join(f'{h} {label}' for h, label in zip(RATING_HOURS, hours))})"


class Ratings:
    """Текущая «Общая оценка» TradingView по бумагам и фьючерсам — для строки в сообщении алерта: на 1 день
    и в скобках на RATING_HOURS часов.

    Запрашивается только когда есть что отправить, все таймфреймы — одним запросом: акции — все TICKERS
    один раз за запуск, фьючерсы — те, что в сообщениях (их символы известны только из алертов).
    Оценка вторична: если TradingView недоступен, алерты уходят без неё.
    """

    TIMEOUT = 15  # секунд: шаг workflow ограничен 2 минутами, из них до минуты уходит на apim
    # Дневная колонка — без суффикса, часовые — суффиксом в минутах: "Recommend.All|60".
    COLUMNS = [COLUMN] + [f"{COLUMN}|{h * 60}" for h in RATING_HOURS]

    def __init__(self):
        self._labels, self._asked = {}, set()

    def get(self, symbols):
        """{символ: строка оценки} для символов TradingView ("RUS:SBER", "RUS:SRZ2026"); без оценки — нет в ответе."""
        missing = set(symbols) - self._asked
        shares = set(TICKERS)
        if missing & shares:
            self._fetch(TICKERS, "russia")
        if missing - shares:
            self._fetch(sorted(missing - shares), "futures")
        return {s: self._labels[s] for s in symbols if s in self._labels}

    def _fetch(self, tickers, market):
        self._asked.update(tickers)
        try:
            values = fetch_values(timeout=self.TIMEOUT, columns=self.COLUMNS, tickers=tickers, market=market)
        except Exception as e:  # noqa: BLE001 — сеть, формат ответа: алерт важнее оценки
            print(f"Mega Alerts: оценка TradingView недоступна ({type(e).__name__}), шлю без неё", file=sys.stderr)
            values = {}
        self._labels.update((t, rating_line(v)) for t, v in values.items())


def split_messages(lines, header, ratings=None):
    """Одно сообщение на бумагу: (текст, ключи, тикер). Заголовок («За 13.10:», «С опозданием:») — в каждом,
    оценка TradingView — последней строкой, если она есть."""
    head = f"{header}\n" if header else ""
    labels = ratings.get([tv for *_, tv in lines if tv]) if ratings else {}
    messages = []
    for (_, secid), text, keys, tv in lines:
        label = labels.get(tv)
        tail = f"\nОценка: {label}" if label else ""
        messages.append((head + text + tail, keys, secid))
    return messages


def deliver(text, secid):
    # Кнопки «Купить» / «Продать» — только у акций: заявки tv-orders умеют только их.
    trade = "RUS:" + secid if secid in SECIDS else None
    try:
        send(text, trade=trade)
    except RuntimeError as e:
        # 429 Too Many Requests: Telegram говорит, сколько подождать. Ждём один раз.
        match = re.search(r'"retry_after":\s*(\d+)', str(e))
        if not match:
            raise
        time.sleep(min(int(match.group(1)), 30) + 1)
        send(text, trade=trade)


def load_state(kid):
    fresh = {"kid": kid, "date": None, "seen": None, "error": None, "error_count": 0, "error_notified": False,
             "markets": []}
    if not STATE_FILE.exists():
        return fresh
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    # Состояние без markets — до фьючерсов (02.11.2026): в seen только акции.
    state.setdefault("markets", ["eq"])
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


def process(state, keys, day, alerts, now, header=None, ratings=None):
    """Отправить новые алерты дня: (новых, повторов, сообщений).

    Повторы сразу помечаются увиденными без отправки; ключи каждого доставленного сообщения тоже сразу
    пишутся в состояние, чтобы при сбое на середине уже доставленное не ушло второй раз.
    """
    by_key = {keys.of(day, a): a for a in alerts}
    seen = set(state["seen"])
    new = [(k, a) for k, a in by_key.items() if k not in seen]
    firsts, repeats = split_repeats(new, list(by_key.values()))
    if repeats:
        seen.update(k for k, _ in repeats)
        state["seen"] = sorted(seen)
        save_state(state)
    if not firsts:
        return 0, len(repeats), 0

    oldest = min(datetime.combine(day, datetime.strptime(a["time"], "%H:%M:%S").time(), MSK) for _, a in firsts)
    if header is None and now - oldest > timedelta(minutes=LATE_AFTER):
        header = "С опозданием:"

    messages = split_messages(build_lines(firsts, day), header, ratings)
    for i, (text, message_keys, secid) in enumerate(messages):
        if i:
            time.sleep(1)
        deliver(text, secid)
        seen.update(message_keys)
        state["seen"] = sorted(seen)
        save_state(state)
    return len(firsts), len(repeats), len(messages)


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

    # Фьючерсы вторичны: сбой срочного рынка не задерживает акции и не шлёт «недоступны» —
    # их алерты придут следующим запуском. Хвост за вчера — только по группам, которые вчера уже присылались.
    known = [s for s in FO_SCOPES if s in state["markets"]]
    try:
        fo_tail = fetch_day(opener, token, yesterday, deadline, "fo") if catch_up and known else []
        fo_rows = fetch_day(opener, token, today, deadline, "fo")
    except ApimError as e:
        print(f"Mega Alerts: фьючерсы недоступны: {e.code}", file=sys.stderr)
        fo_tail, fo_rows = [], None
    fo_alerts = select(fo_rows, today, "fo") if fo_rows is not None else []

    if not baselined or state["date"] not in (today.isoformat(), yesterday.isoformat()):
        # Первый успешный запрос (или разрыв больше суток): запоминаем день молча, без потока с утра.
        state.update(date=today.isoformat(), seen=sorted({keys.of(today, a) for a in alerts + fo_alerts}),
                     markets=["eq"] + (FO_SCOPES if fo_rows is not None else []))
        save_state(state)
        print(f"Mega Alerts: базовая точка, строк за день {len(rows)}, по нашим тикерам {len(alerts)}, "
              f"по фьючерсам {len(fo_alerts)}")
        return

    ratings = Ratings()
    if catch_up:
        tail = select(tail_rows, yesterday) + [a for a in select(fo_tail, yesterday, "fo") if a["scope"] in known]
        new, repeats, parts = process(state, keys, yesterday, tail, now, header=f"За {yesterday:%d.%m}:", ratings=ratings)
        print(f"Mega Alerts: хвост за {yesterday:%d.%m}: строк {len(tail_rows) + len(fo_tail)}, "
              f"по нашим тикерам и фьючерсам {len(tail)}, новых {new}, повторов {repeats}, отправлено сообщений {parts}")
        state.update(date=today.isoformat(), seen=[])

    # После хвоста: он обнуляет seen и стёр бы эту базовую точку.
    new_scopes = [s for s in FO_SCOPES if s not in known] if fo_rows is not None else []
    if new_scopes:
        # Группа фьючерсов добавлена в работающего бота: уже вышедшие за день — молча, как базовая точка.
        quiet = [a for a in fo_alerts if a["scope"] in new_scopes]
        state.update(seen=sorted(set(state["seen"]) | {keys.of(today, a) for a in quiet}),
                     markets=state["markets"] + new_scopes)
        save_state(state)
        print(f"Mega Alerts: базовая точка для {', '.join(new_scopes)}, алертов {len(quiet)}")

    new, repeats, parts = process(state, keys, today, alerts + fo_alerts, now, ratings=ratings)
    save_state(state)
    print(f"Mega Alerts: строк за день {len(rows) + len(fo_rows or [])}, по нашим тикерам {len(alerts)}, "
          f"по фьючерсам {len(fo_alerts)}, новых {new}, повторов {repeats}, отправлено сообщений {parts}")


if __name__ == "__main__":
    main()
