# Discord Voice Logger

Бот логирует кто, когда и сколько сидел в войс-каналах Discord.
Данные хранятся в InfluxDB, графики — в Grafana.

---

## Что нужно

- Docker + Docker Compose
- Discord-бот (токен с портала разработчиков)

---

## Быстрый старт

### 1. Склонировать / скачать проект

```bash
git clone <url>
cd discord-voice-logger
```

### 2. Заполнить `.env`

```env
DISCORD_TOKEN=твой_токен_бота
INFLUX_TOKEN=придумай_любую_длинную_строку_типа_пароля_123abc
TIMEZONE_OFFSET=3        # UTC+3 для Москвы
```

Остальное можно не трогать.

### 3. Запустить

```bash
docker compose up -d
```

Всё. Бот поднялся, InfluxDB и Grafana тоже.

---

## Настройка Discord-бота

1. Открыть https://discord.com/developers/applications
2. **New Application** → дать имя
3. Вкладка **Bot** → **Reset Token** → скопировать в `.env` как `DISCORD_TOKEN`
4. Там же включить **Privileged Gateway Intents**:
   - ✅ Server Members Intent
   - ✅ Voice States *(обычно включён по умолчанию)*
5. Вкладка **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Bot Permissions: `View Channels`, `Connect`, `Send Messages`
6. Скопировать ссылку → перейти → добавить бота на сервер

---

## Команды в Discord

| Команда | Что делает |
|---|---|
| `/status` | Кто сейчас сидит в голосовых каналах |

---

## Grafana (графики)

Открыть в браузере: **http://localhost:3000**
Логин: `admin` / пароль: `GRAFANA_PASSWORD` из `.env` (по умолчанию `admin`)

InfluxDB уже подключён автоматически. Дашборд настраивается в UI или через provisioning.

Пример Flux-запроса для графика «кто сколько сидел сегодня»:
```flux
from(bucket: "voice_logs")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "voice_presence" and r._field == "minutes")
  |> group(columns: ["username"])
  |> sum()
```

---

## InfluxDB (база данных)

Открыть в браузере: **http://localhost:8086**
Логин: `admin` / пароль: `INFLUX_ADMIN_PASSWORD` из `.env`

Данные хранятся **90 дней** (настраивается через `INFLUX_RETENTION` в `.env`).

### Measurements

| Measurement | Что хранит |
|---|---|
| `voice_event` | Сырые события: join / leave / switch |
| `voice_presence` | Снапшот каждые 5 мин — кто в каком канале |
| `call_session` | Завершённые созвоны (2+ участника) |
| `call_wide` | То же в широком формате для таблиц Grafana |
| `bot_heartbeat` | Пульс бота раз в минуту |

---

## Переменные `.env`

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DISCORD_TOKEN` | — | **Обязательно.** Токен бота |
| `INFLUX_TOKEN` | — | **Обязательно.** API-токен InfluxDB (придумай сам) |
| `TIMEZONE_OFFSET` | `0` | Смещение UTC. Москва = `3` |
| `INFLUX_URL` | `http://influxdb:8086` | Адрес InfluxDB (в Docker не менять) |
| `INFLUX_ORG` | `discord` | Организация в InfluxDB |
| `INFLUX_BUCKET` | `voice_logs` | Бакет в InfluxDB |
| `INFLUX_RETENTION` | `90d` | Срок хранения данных |
| `INFLUX_ADMIN_USER` | `admin` | Логин для InfluxDB UI |
| `INFLUX_ADMIN_PASSWORD` | `changeme123` | Пароль для InfluxDB UI |
| `GRAFANA_PASSWORD` | `admin` | Пароль для Grafana UI |

---

## Управление

```bash
docker compose stop          # остановить
docker compose start         # запустить снова
docker compose restart       # перезапустить
docker compose down          # удалить контейнеры (данные сохранятся)
docker compose down -v       # удалить всё включая данные (осторожно!)
docker compose logs -f bot   # логи бота
```

---

## Если что-то не работает

**Бот не реагирует на `/status`** — слэш-команды синхронизируются при старте, подождать 1–2 минуты. Если не помогло — перезапустить бот.

**Ошибка подключения к InfluxDB** — InfluxDB стартует дольше бота. Docker сам перезапустит бота когда база будет готова (healthcheck).

**Команды не появляются в Discord** — убедись что в OAuth2 выбраны скоупы `bot` + `applications.commands`.

> Подробная инструкция по развёртыванию InfluxDB и Grafana — в [SETUP.md](SETUP.md).
