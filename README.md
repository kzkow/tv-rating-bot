# tv-rating-bot

Следит за «Общей оценкой» теханализа TradingView (таймфрейм 1 день) по списку акций и пишет в Telegram, когда оценка меняется.

Данные — из `scanner.tradingview.com` (то же, что показывает страница «Теханализ»); без подписки они с задержкой 15 минут.
Список бумаг — `TICKERS` в `bot.py`.

## Настройка

1. Создать бота в @BotFather, получить токен.
2. Написать боту `/start`, открыть `https://api.telegram.org/bot<ТОКЕН>/getUpdates` и взять `message.chat.id`.
   Для канала: добавить бота админом, chat_id — `@имя_канала`.
3. Залить репозиторий на GitHub (публичный — в приватном не хватит бесплатных минут).
4. Settings → Secrets and variables → Actions → New repository secret: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
5. Actions → TradingView rating → Run workflow — придёт сводка по всем бумагам. Дальше — только смены оценки.

## Локально

```bash
python bot.py --summary           # без переменных TELEGRAM_* — печать в консоль
python bot.py --loop 60           # проверка каждую минуту
```
