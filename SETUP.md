# Развёртывание Discord Voice Logger

## Быстрый старт (всё в Docker)

### 1. Заполни `.env`

```env
DISCORD_TOKEN=your_discord_bot_token

TIMEZONE_OFFSET=3          # UTC+3 для Москвы

INFLUX_URL=http://influxdb:8086
INFLUX_TOKEN=придумай_любой_длинный_токен
INFLUX_ORG=discord
INFLUX_BUCKET=voice_logs
INFLUX_RETENTION=90d

INFLUX_ADMIN_USER=admin
INFLUX_ADMIN_PASSWORD=changeme123

GRAFANA_PASSWORD=admin
```

> `INFLUX_TOKEN` — придумай сам, это просто строка-пароль для API. Используется и при инициализации БД, и ботом для записи.

### 2. Запусти

```bash
docker compose up -d
```

При первом запуске InfluxDB сам создаст организацию, бакет и токен из `.env`.

---

## InfluxDB

### Вариант А — встроенный (docker-compose)

Всё настраивается автоматически. InfluxDB доступен на `http://localhost:8086`.

Войди через браузер: логин/пароль из `INFLUX_ADMIN_USER` / `INFLUX_ADMIN_PASSWORD`.

**Важно:** `DOCKER_INFLUXDB_INIT_*` переменные работают только при первом запуске (когда volume пустой). Если нужно переинициализировать — удали volume:
```bash
docker compose down -v
docker compose up -d
```

### Вариант Б — внешняя InfluxDB (уже есть своя)

1. В своей InfluxDB создай:
   - Organization (например `discord`)
   - Bucket (например `voice_logs`) с нужным retention
   - API Token с правами на запись в этот bucket

2. В `.env` укажи адрес внешней БД и убери influxdb из docker-compose:

```env
INFLUX_URL=http://192.168.1.100:8086   # или https://...
INFLUX_TOKEN=твой_токен_из_UI
INFLUX_ORG=discord
INFLUX_BUCKET=voice_logs
```

3. В `docker-compose.yml` удали сервис `influxdb` и его `depends_on` у бота и grafana.

4. В `grafana/provisioning/datasources/influxdb.yml` поменяй `url`:
```yaml
url: http://192.168.1.100:8086
```

### Схема данных (measurements)

| Measurement | Что хранит |
|---|---|
| `voice_event` | join/leave/switch события |
| `voice_presence` | снапшоты каждые 5 мин (для Grafana) |
| `call_session` | завершённые созвоны |
| `call_wide` | созвоны в широком формате (для таблицы в Grafana) |
| `bot_heartbeat` | пульс бота раз в минуту |

---

## Grafana

### Встроенная (docker-compose)

Доступна на `http://localhost:3000`. Логин `admin`, пароль из `GRAFANA_PASSWORD`.

Datasource и дашборд подключаются автоматически через provisioning из папки `grafana/provisioning/`.

**Проблема: datasource не подхватывает токен автоматически**

Grafana при provisioning не подставляет `$INFLUX_TOKEN` из env в `secureJsonData`. Нужно добавить токен вручную:

1. Открой `http://localhost:3000/connections/datasources`
2. Нажми на **InfluxDB**
3. В поле **Token** вставь значение `INFLUX_TOKEN` из `.env`
4. Нажми **Save & Test** — должно появиться «datasource is working»

### Вариант — внешняя Grafana (уже есть своя)

1. В своей Grafana добавь datasource вручную:
   - Type: **InfluxDB**
   - Query Language: **Flux**
   - URL: адрес InfluxDB
   - Organization: значение `INFLUX_ORG`
   - Token: значение `INFLUX_TOKEN`
   - Default Bucket: значение `INFLUX_BUCKET`

2. Импортируй дашборд:
   - Открой `grafana/provisioning/dashboards/discord.json`
   - В Grafana: **Dashboards → Import → Upload JSON file**
   - После импорта в настройках дашборда укажи datasource `influxdb-discord`

3. В `docker-compose.yml` удали сервис `grafana`.

---

## Пересборка после изменений

```bash
# Пересобрать бота (после изменений bot.py / influx.py)
docker compose up -d --build bot

# Перезапустить Grafana (после изменений дашборда)
docker compose restart grafana

# Посмотреть логи бота
docker compose logs -f bot
```
