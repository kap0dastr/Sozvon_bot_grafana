# Grafana Dashboard — база знаний

## Версия Grafana
**12.4.0** — важно для `pluginVersion` в JSON панелей.

## Datasource
- uid: `influxdb-discord`
- type: `influxdb`, Flux
- url: `http://influxdb:8086`, org: `discord`, bucket: `voice_logs`
- токен задаётся через `secureJsonData.token: $INFLUX_TOKEN` в `grafana/provisioning/datasources/influxdb.yml`

## Файлы дашборда
- `grafana/provisioning/dashboards/discord.json` — основной дашборд
- `grafana/provisioning/dashboards/provider.yml` — провайдер (path: `/etc/grafana/provisioning/dashboards`, updateIntervalSeconds: 30)
- Изменения подхватываются через 30 сек или `docker compose restart grafana`

## InfluxDB measurements (актуальная схема)

### `voice_presence`
Пишется каждые 5 мин. Для переменных Канал/Участник в Grafana.
- tags: `guild_id`, `user_id`, `username`, `channel_name`
- fields: `minutes=5`

### `voice_event`
Сырые события join/leave/switch.
- tags: `guild_id`, `user_id`, `username`
- fields: `event_type`, `guild_name`, `channel_from_id`, `channel_from_name`, `channel_to_id`, `channel_to_name`

### `call_session`
Пишется при завершении созвона (и при финальном снапшоте).
- tags: `channel_name`, `guild_id`, `guild_name`, `call_label`, `participants_csv`
- fields: `duration_minutes` (float), `participant_count` (int), `participants_str` (str), `end_time_str` (str)
- `_time` = start of call
- `participants_csv` = "user1,user2,user3" — используется для regex-фильтра по участнику

### `call_wide`
Широкий формат для таблицы созвонов в Grafana. Пишется каждую минуту пока созвон активен + при закрытии.
- tags: `channel_name`, `guild_id`, `call_label`, `end_time` (строка "ДД.ММ ЧЧ:ММ"), `participants_csv`
- fields: `_duration` (float), `{username}` (float, минуты) — динамически
- `_time` = end (обновляется при каждом снапшоте)

### `bot_heartbeat`
- fields: `online=1`

## Переменные дашборда
| Переменная | Запрос | allValue | multi |
|---|---|---|---|
| `channel_var` | `schema.tagValues(bucket: "voice_logs", tag: "channel_name", ...)` из `voice_presence` | `.*` | true |
| `participant_var` | `schema.tagValues(bucket: "voice_logs", tag: "username", ...)` из `voice_presence` | `.*` | true |

Фильтрация: `r.channel_name =~ /${channel_var}/` и `r.participants_csv =~ /${participant_var}/`

## Панели

### Panel 5 — Таблица "Созвоны" (table)
**Ключевые решения:**
- Данные из `call_wide` через `pivot()` в Flux → динамические колонки по именам участников
- Фильтр по участнику: сначала получаем matching `call_label` из `call_session` через `findColumn()`, потом `contains()` по `call_wide`
- Перед `pivot` фильтруем `types.isType(v: r._value, type: "float")` — в `call_wide` бывают mixed types из старых данных
- `strings.trimPrefix` убирает название канала из `call_label` (формат "channel DD.MM HH:MM" → "DD.MM HH:MM")
- `pivot(rowKey: ["_time", "end_time"], ...)` — `end_time` в rowKey чтобы не дропался при pivot
- `organize` трансформация для порядка колонок: channel_name(0), call_label(1), end_time(2), _duration(3)
- `byType: number` override → unit "m" для всех колонок участников

**Flux запрос:**
```flux
import "types"
import "strings"

labels = from(bucket: "voice_logs")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "call_session" and r._field == "duration_minutes")
  |> filter(fn: (r) => r.channel_name =~ /${channel_var}/)
  |> filter(fn: (r) => r.participants_csv =~ /${participant_var}/)
  |> group()
  |> distinct(column: "call_label")
  |> findColumn(fn: (key) => true, column: "_value")

from(bucket: "voice_logs")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "call_wide")
  |> filter(fn: (r) => r.channel_name =~ /${channel_var}/)
  |> filter(fn: (r) => contains(value: r.call_label, set: labels))
  |> filter(fn: (r) => types.isType(v: r._value, type: "float"))
  |> map(fn: (r) => ({r with call_label: strings.trimPrefix(v: r.call_label, prefix: r.channel_name + " ")}))
  |> group(columns: ["call_label", "channel_name", "guild_id", "_field"])
  |> last()
  |> group(columns: ["call_label", "channel_name", "guild_id"])
  |> pivot(rowKey: ["_time", "end_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
```

### Panel 21 — "Созвоны по дням" (timeseries)
- `pluginVersion: "12.4.0"` ОБЯЗАТЕЛЕН — без него рендер не работает в Grafana 12
- `drawStyle: "bars"` в custom fieldConfig
- Запрос: `aggregateWindow(every: 1d, fn: count)` по `call_session`
- Группировать `group(columns: ["_field", "_measurement"])` ДО `aggregateWindow`
- Override переименовывает `duration_minutes` → "Созвонов"

**Flux запрос:**
```flux
from(bucket: "voice_logs")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "call_session" and r._field == "duration_minutes")
  |> filter(fn: (r) => r.channel_name =~ /${channel_var}/)
  |> filter(fn: (r) => r.participants_csv =~ /${participant_var}/)
  |> group(columns: ["_field", "_measurement"])
  |> aggregateWindow(every: 1d, fn: count, createEmpty: false)
```

### Panel 22 — "Длительность созвонов" (bargauge)
- Горизонтальный, каждый бар = один созвон
- `map(fn: (r) => ({_time: r._time, _field: r.call_label, _value: r._value}))` — чистая строка
- `group(columns: ["_field"])` — каждый созвон отдельная серия
- Цвета: зелёный <30м, жёлтый 30-60м, красный >60м

## Известные проблемы и решения

| Проблема | Причина | Решение |
|---|---|---|
| `pivot: schema collision string/float` | В `call_wide` старые данные с mixed types | `filter(fn: (r) => types.isType(v: r._value, type: "float"))` |
| Timeseries panel пустой при верном запросе | Grafana 12 требует `pluginVersion` | Добавить `"pluginVersion": "12.4.0"` |
| `barchart` тип не рендерит time series | Тип `barchart` плохо работает с aggregateWindow | Использовать `timeseries` с `drawStyle: "bars"` |
| `end_time` пропадает после pivot | pivot дропает non-rowKey колонки при несоответствии | Добавить `end_time` в `rowKey` |
| Фильтр участника не работает на старых данных | Старый `call_wide` без тега `participants_csv` | `findColumn` + `contains` по `call_session` |
| Название канала дублируется в "Начало" | `call_label` = "channel DD.MM HH:MM" | `strings.trimPrefix(v: r.call_label, prefix: r.channel_name + " ")` |
| aggregateWindow работает некорректно | Нужна группа с `_field` до вызова | `group(columns: ["_field", "_measurement"])` перед `aggregateWindow` |

## Запуск
```bash
docker compose up -d          # запустить всё
docker compose restart grafana # подхватить изменения дашборда
docker compose up -d --build bot # пересобрать бота после изменений influx.py
```
Grafana: http://localhost:3000 (admin/admin или GRAFANA_PASSWORD из .env)
