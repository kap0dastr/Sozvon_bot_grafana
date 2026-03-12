"""
InfluxDB client — хранение голосовых событий и presence-снапшотов.
"""
import logging
import os
from datetime import datetime, timezone

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

log = logging.getLogger(__name__)

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "discord")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "voice_logs")


def _client() -> InfluxDBClientAsync:
    return InfluxDBClientAsync(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


# ── Запись событий ────────────────────────────────────────────────────────────

async def log_event(
    user_id: str,
    username: str,
    guild_id: str,
    guild_name: str,
    event_type: str,
    channel_from_id: str | None,
    channel_from_name: str | None,
    channel_to_id: str | None,
    channel_to_name: str | None,
    timestamp: datetime,
):
    point = (
        Point("voice_event")
        .tag("guild_id",  guild_id)
        .tag("user_id",   user_id)
        .tag("username",  username)
        .field("event_type",        event_type)
        .field("guild_name",        guild_name)
        .field("channel_from_id",   channel_from_id   or "")
        .field("channel_from_name", channel_from_name or "")
        .field("channel_to_id",     channel_to_id     or "")
        .field("channel_to_name",   channel_to_name   or "")
        .time(timestamp)
    )
    async with _client() as c:
        await c.write_api().write(bucket=INFLUX_BUCKET, record=point)


async def write_presence_snapshot(
    guild_id: str,
    presence: list[tuple[str, str, str]],   # (user_id, username, channel_name)
    timestamp: datetime,
):
    """
    Пишет снапшот присутствия (каждые 5 мин) для Grafana.
    Поле minutes=5 — можно суммировать для получения общего времени.
    """
    if not presence:
        return
    points = [
        Point("voice_presence")
        .tag("guild_id",    guild_id)
        .tag("user_id",     uid)
        .tag("username",    uname)
        .tag("channel_name", ch)
        .field("minutes",  5)
        .time(timestamp)
        for uid, uname, ch in presence
    ]
    async with _client() as c:
        await c.write_api().write(bucket=INFLUX_BUCKET, record=points)


async def write_bot_heartbeat():
    """Раз в минуту пишем пульс бота. Отсутствие = офлайн."""
    point = Point("bot_heartbeat").field("online", 1).time(datetime.now(timezone.utc))
    async with _client() as c:
        await c.write_api().write(bucket=INFLUX_BUCKET, record=point)


async def get_open_sessions(guild_id: str) -> dict[str, dict]:
    """
    Пользователи с незакрытой сессией (последнее событие — join или switch).
    Возвращает {user_id: {"username": str, "channel_id": str, "channel_name": str}}
    """
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "voice_event" and r.guild_id == "{guild_id}")
  |> pivot(rowKey: ["_time", "user_id"], columnKey: ["_field"], valueColumn: "_value")
  |> group(columns: ["user_id"])
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 1)
  |> filter(fn: (r) => r.event_type == "join" or r.event_type == "switch")
"""
    result: dict[str, dict] = {}
    async with _client() as c:
        try:
            tables = await c.query_api().query(flux)
            for table in tables:
                for rec in table.records:
                    v = rec.values
                    uid = v.get("user_id", "")
                    if uid:
                        result[uid] = {
                            "username":     v.get("username", uid),
                            "channel_id":   v.get("channel_to_id",   ""),
                            "channel_name": v.get("channel_to_name", ""),
                        }
        except Exception as exc:
            log.warning(f"get_open_sessions: {exc}")
    return result


async def write_call_session(
    channel: str,
    guild_id: str,
    guild_name: str,
    start: datetime,
    end: datetime,
    participants: dict[str, float],  # {username: duration_minutes}
    call_label: str,                  # "general 04.03 20:15"
) -> None:
    """Пишет запись созвона (и снапшоты во время звонка, и финальную запись)."""
    duration = (end - start).total_seconds() / 60
    parts_str = ", ".join(
        f"{u} {int(d)}м" for u, d in sorted(participants.items(), key=lambda x: -x[1])
    )
    participants_csv = ",".join(sorted(participants.keys()))
    end_local = end.strftime("%d.%m %H:%M")

    session_point = (
        Point("call_session")
        .tag("channel_name",     channel)
        .tag("guild_id",         guild_id)
        .tag("guild_name",       guild_name)
        .tag("call_label",       call_label)
        .tag("participants_csv", participants_csv)
        .field("duration_minutes",  round(duration, 1))
        .field("participant_count", len(participants))
        .field("participants_str",  parts_str)
        .field("end_time_str",      end_local)
        .time(start)
    )

    # Широкий формат: имена участников = field-ключи, плюс метаданные созвона.
    # Grafana автоматически создаёт колонку на каждый _field — трансформы не нужны.
    wide_point = (
        Point("call_wide")
        .tag("channel_name", channel)
        .tag("guild_id",     guild_id)
        .tag("call_label",       call_label)          # стабильный идентификатор созвона
        .tag("end_time",         end_local)          # Конец (тег, обновляется каждый снапшот)
        .tag("participants_csv", participants_csv)   # для фильтра по участнику в Grafana
        .field("_duration",  round(duration, 1))    # Длительность (float)
        .time(end)                                  # _time = NOW() → last() работает корректно
    )
    for uname, dur in participants.items():
        wide_point = wide_point.field(uname, round(dur, 1))

    async with _client() as c:
        await c.write_api().write(bucket=INFLUX_BUCKET, record=[session_point, wide_point])
