# tv-rating-bot

Следит за «Общей оценкой» теханализа TradingView (таймфрейм 1 день) по списку акций и пишет в Telegram, когда оценка
переходит на другую сторону: покупать ↔ продавать (например, «SNGSP: Покупать → Продавать»). Уход в «Нейтрально»
и смена силы («Покупать ↔ Активно покупать») не присылаются; в сообщении — последняя присланная оценка и новая.

Данные — из `scanner.tradingview.com` (то же, что показывает страница «Теханализ»); без подписки они с задержкой 15 минут.
Список бумаг — `TICKERS` в `bot.py`.

## Настройка

1. Создать бота в @BotFather, получить токен.
2. Написать боту `/start`, открыть `https://api.telegram.org/bot<ТОКЕН>/getUpdates` и взять `message.chat.id`.
   Для канала: добавить бота админом, chat_id — `@имя_канала`.
3. Залить репозиторий на GitHub (публичный — в приватном не хватит бесплатных минут).
4. Settings → Secrets and variables → Actions → New repository secret: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
5. Actions → TradingView rating → Run workflow с галочкой «Прислать сводку» — придёт сводка по всем бумагам. Дальше — только смены оценки.
6. Свой cron GitHub исполняет раз в 3–4 часа, поэтому workflow запускает внешний планировщик (cron-job.org):
   `POST https://api.github.com/repos/kzkow/tv-rating-bot/actions/workflows/rating.yml/dispatches`,
   тело `{"ref":"main"}`, заголовки `Authorization: Bearer <токен>`, `Accept: application/vnd.github+json`.
   Токен — fine-grained, только этот репозиторий, право Actions: Read and write.

## Mega Alerts (MOEX ALGOPACK)

`alerts.py` присылает в тот же чат торговые аномалии MOEX («Mega Alerts», тариф ALGOPACK Promo) по тем же бумагам из `TICKERS`.

- Секрет `MOEX_ALGOPACK_TOKEN` — API-ключ из https://data.moex.com/personal-account. Без него шаг пропускается.
- Одна строка на бумагу — тикер и причины: `SBER: Перевес покупок, Аномальный объём, Крупные покупки ×3`
  (×N — тип сработал в пачке несколько раз).
- Повтор — срабатывание того же типа с разрывом не больше 30 минут. Сообщение только из повторов приходит
  беззвучно; если в нём есть новое срабатывание — со звуком.
- Первый запуск (и первый после смены ключа или перерыва больше суток) ничего не присылает — запоминает уже вышедшие алерты.
- Если apim недоступен 3 запуска подряд — одно сообщение «недоступны», после восстановления — «снова работают».
- Сертификат apim выпущен НУЦ Минцифры, его корня нет в стандартных хранилищах: он лежит в `certs/russian_trusted_root_ca.pem`
  (https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt, отпечаток сверяется в коде) и используется только для apim.
- Данные MOEX нельзя передавать третьим лицам, а репозиторий публичный: в `alerts_state.json` лежат только HMAC-хэши
  уже отправленных алертов, в логах Actions — только счётчики.

## Локально

```bash
python bot.py --summary           # без переменных TELEGRAM_* — печать в консоль
python bot.py --loop 60           # проверка каждую минуту
# Mega Alerts; без TELEGRAM_* — печать в консоль. Состояние — во временный файл:
# alerts_state.json в репозитории ведёт workflow, локальные правки в нём спрятали бы алерты в Telegram.
MOEX_ALGOPACK_TOKEN=... ALERTS_STATE_FILE=/tmp/alerts_state.json python alerts.py
```
