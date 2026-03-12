"""
Discord Voice Logger Bot
Логирует события голосовых каналов → InfluxDB.
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from influx import (
    get_open_sessions,
    log_event,
    write_bot_heartbeat,
    write_call_session,
    write_presence_snapshot,
)

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не найден — заполните .env файл")

_raw_tz = os.getenv("TIMEZONE_OFFSET", "0")
TIMEZONE_OFFSET: int = int(_raw_tz) if _raw_tz.lstrip("-").isdigit() else 0

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True

bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ── Отслеживание созвонов ─────────────────────────────────────────────────────
# {channel_id: {"channel_name": str, "guild_id": str, "guild_name": str,
#               "start": datetime,
#               "participants": {user_id: {"username": str, "join": datetime|None, "accumulated": float}}}}
active_calls: dict[int, dict] = {}


def _humans(channel: discord.VoiceChannel) -> list[discord.Member]:
    return [m for m in channel.members if not m.bot]


def _call_label(channel_name: str, start: datetime) -> str:
    local = start + timedelta(hours=TIMEZONE_OFFSET)
    return f"{channel_name} {local.strftime('%d.%m %H:%M')}"


async def _write_call_snapshot(channel_id: int) -> None:
    """Записывает текущее состояние активного созвона в InfluxDB без его закрытия."""
    call = active_calls.get(channel_id)
    if not call:
        return
    now = datetime.now(timezone.utc)
    result: dict[str, float] = {}
    for info in call["participants"].values():
        total = info["accumulated"]
        if info["join"] is not None:
            total += (now - info["join"]).total_seconds() / 60
        result[info["username"]] = round(max(total, 0.0), 1)
    if result:
        try:
            await write_call_session(
                channel=call["channel_name"],
                guild_id=call["guild_id"],
                guild_name=call["guild_name"],
                start=call["start"],
                end=now,
                participants=result,
                call_label=call["call_label"],
            )
        except Exception as exc:
            log.error(f"[CALL_SNAPSHOT] {exc}")


async def _start_call(channel: discord.VoiceChannel) -> None:
    """Начать отслеживание созвона если 2+ человек в канале."""
    if channel.id in active_calls:
        return
    members = _humans(channel)
    if len(members) >= 2:
        now = datetime.now(timezone.utc)
        label = _call_label(channel.name, now)
        active_calls[channel.id] = {
            "channel_name": channel.name,
            "guild_id":     str(channel.guild.id),
            "guild_name":   channel.guild.name,
            "start":        now,
            "call_label":   label,
            "participants": {
                m.id: {"username": m.display_name, "join": now, "accumulated": 0.0}
                for m in members
            },
        }
        log.info(f"[CALL] Начался созвон #{channel.name}: {[m.display_name for m in members]}")
        await _write_call_snapshot(channel.id)


async def _end_call(channel: discord.VoiceChannel) -> None:
    """Завершить созвон если осталось < 2 человек."""
    if channel.id not in active_calls:
        return
    if len(_humans(channel)) >= 2:
        return
    await _close_call(channel.id)


async def _close_call(channel_id: int) -> None:
    """Принудительно закрыть созвон и записать в InfluxDB."""
    call = active_calls.pop(channel_id, None)
    if not call:
        return
    now = datetime.now(timezone.utc)
    result: dict[str, float] = {}
    for info in call["participants"].values():
        total = info["accumulated"]
        if info["join"] is not None:
            total += (now - info["join"]).total_seconds() / 60
        if total >= 0.5:
            result[info["username"]] = round(total, 1)
    if result:
        try:
            await write_call_session(
                channel=call["channel_name"],
                guild_id=call["guild_id"],
                guild_name=call["guild_name"],
                start=call["start"],
                end=now,
                participants=result,
                call_label=_call_label(call["channel_name"], call["start"]),
            )
            mins = (now - call["start"]).total_seconds() / 60
            log.info(f"[CALL] Завершён #{call['channel_name']}, {mins:.0f}м, {list(result.keys())}")
        except Exception as exc:
            log.error(f"[CALL] write_call_session: {exc}")


def _member_join_call(channel_id: int, member: discord.Member) -> None:
    """Участник вошёл в канал с активным созвоном."""
    call = active_calls.get(channel_id)
    if not call:
        return
    now = datetime.now(timezone.utc)
    if member.id not in call["participants"]:
        call["participants"][member.id] = {"username": member.display_name, "join": now, "accumulated": 0.0}
    elif call["participants"][member.id]["join"] is None:
        call["participants"][member.id]["join"] = now


def _member_leave_call(channel_id: int, member: discord.Member) -> None:
    """Участник покинул канал (созвон может продолжиться)."""
    call = active_calls.get(channel_id)
    if not call:
        return
    info = call["participants"].get(member.id)
    if info and info["join"] is not None:
        info["accumulated"] += (datetime.now(timezone.utc) - info["join"]).total_seconds() / 60
        info["join"] = None


# ── Presence-снапшоты для Grafana (каждые 5 мин) ─────────────────────────────

@tasks.loop(minutes=5)
async def presence_snapshot_task():
    now = discord.utils.utcnow()
    for guild in bot.guilds:
        presence = [
            (str(m.id), m.display_name, vc.name)
            for vc in guild.voice_channels
            for m in vc.members
            if not m.bot
        ]
        if presence:
            try:
                await write_presence_snapshot(str(guild.id), presence, now)
            except Exception as exc:
                log.error(f"[PRESENCE] {guild.name}: {exc}")


@presence_snapshot_task.before_loop
async def before_presence():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def active_calls_snapshot_task():
    """Каждую минуту пишет снапшот всех активных созвонов — чтобы они сразу были видны в Grafana."""
    for channel_id in list(active_calls.keys()):
        await _write_call_snapshot(channel_id)


@active_calls_snapshot_task.before_loop
async def before_active_calls_snapshot():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def bot_heartbeat_task():
    try:
        await write_bot_heartbeat()
    except Exception as exc:
        log.error(f"[HEARTBEAT] {exc}")


@bot_heartbeat_task.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        # Кто сейчас реально в войсах
        current_voice: dict[str, tuple[str, str]] = {
            str(m.id): (str(vc.id), vc.name)
            for vc in guild.voice_channels
            for m in vc.members
            if not m.bot
        }

        # Логируем текущих как join (бот перезапустился)
        for member_id, (vc_id, vc_name) in current_voice.items():
            member = guild.get_member(int(member_id))
            if not member:
                continue
            try:
                await log_event(
                    user_id=member_id,
                    username=member.display_name,
                    guild_id=str(guild.id),
                    guild_name=guild.name,
                    event_type="join",
                    channel_from_id=None,
                    channel_from_name=None,
                    channel_to_id=vc_id,
                    channel_to_name=vc_name,
                    timestamp=now,
                )
            except Exception as exc:
                log.error(f"[READY] log_event join: {exc}")

        # Синтетический LEAVE для тех кто ушёл пока бот был офлайн
        try:
            open_sessions = await get_open_sessions(str(guild.id))
            for uid, info in open_sessions.items():
                if uid not in current_voice:
                    await log_event(
                        user_id=uid,
                        username=info["username"],
                        guild_id=str(guild.id),
                        guild_name=guild.name,
                        event_type="leave",
                        channel_from_id=info["channel_id"],
                        channel_from_name=info["channel_name"],
                        channel_to_id=None,
                        channel_to_name=None,
                        timestamp=now,
                    )
                    log.info(
                        f"[READY] LEAVE (офлайн) "
                        f"{info['username']:>20}  <-  #{info['channel_name']}"
                    )
        except Exception as exc:
            log.error(f"[READY] open_sessions: {exc}")

    # Восстанавливаем активные созвоны (если бот перезапустился во время созвона)
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            members = _humans(channel)
            if len(members) >= 2 and channel.id not in active_calls:
                now_dt = datetime.now(timezone.utc)
                label = _call_label(channel.name, now_dt)
                active_calls[channel.id] = {
                    "channel_name": channel.name,
                    "guild_id":     str(guild.id),
                    "guild_name":   guild.name,
                    "start":        now_dt,
                    "call_label":   label,
                    "participants": {
                        m.id: {"username": m.display_name, "join": now_dt, "accumulated": 0.0}
                        for m in members
                    },
                }
                log.info(f"[READY] Восстановлен созвон #{channel.name}: {[m.display_name for m in members]}")
                await _write_call_snapshot(channel.id)

    try:
        synced = await tree.sync()
        log.info(f"Синхронизировано {len(synced)} slash-команд")
    except Exception as exc:
        log.error(f"Ошибка синхронизации команд: {exc}")

    if not presence_snapshot_task.is_running():
        presence_snapshot_task.start()
    if not active_calls_snapshot_task.is_running():
        active_calls_snapshot_task.start()
    if not bot_heartbeat_task.is_running():
        bot_heartbeat_task.start()

    log.info(f"Бот запущен: {bot.user}  |  Серверов: {len(bot.guilds)}")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot or before.channel == after.channel:
        return

    now   = discord.utils.utcnow()
    guild = member.guild

    if before.channel is None and after.channel is not None:
        event_type   = "join"
        ch_from_id   = ch_from_name = None
        ch_to_id     = str(after.channel.id)
        ch_to_name   = after.channel.name
        log.info(f"[JOIN  ] {member.display_name:>24}  ->  #{after.channel.name}  [{guild.name}]")

    elif before.channel is not None and after.channel is None:
        event_type   = "leave"
        ch_from_id   = str(before.channel.id)
        ch_from_name = before.channel.name
        ch_to_id     = ch_to_name = None
        log.info(f"[LEAVE ] {member.display_name:>24}  <-  #{before.channel.name}  [{guild.name}]")

    else:
        event_type   = "switch"
        ch_from_id   = str(before.channel.id)
        ch_from_name = before.channel.name
        ch_to_id     = str(after.channel.id)
        ch_to_name   = after.channel.name
        log.info(
            f"[SWITCH] {member.display_name:>24}  "
            f"#{before.channel.name} -> #{after.channel.name}  [{guild.name}]"
        )

    try:
        await log_event(
            user_id=str(member.id),
            username=member.display_name,
            guild_id=str(guild.id),
            guild_name=guild.name,
            event_type=event_type,
            channel_from_id=ch_from_id,
            channel_from_name=ch_from_name,
            channel_to_id=ch_to_id,
            channel_to_name=ch_to_name,
            timestamp=now,
        )
    except Exception as exc:
        log.error(f"[EVENT] log_event: {exc}")

    # Обновляем отслеживание созвонов
    if before.channel and before.channel != after.channel:
        _member_leave_call(before.channel.id, member)
        await _end_call(before.channel)
    if after.channel and after.channel != before.channel:
        _member_join_call(after.channel.id, member)
        await _start_call(after.channel)


# ── Slash commands ─────────────────────────────────────────────────────────────

@tree.command(
    name="status",
    description="Показать, кто сейчас в голосовых каналах",
)
async def cmd_status(interaction: discord.Interaction):
    guild = interaction.guild
    lines: list[str] = []
    for vc in guild.voice_channels:
        humans = [m for m in vc.members if not m.bot]
        if humans:
            members_str = ", ".join(m.display_name for m in humans)
            lines.append(f"**#{vc.name}** — {members_str}")

    if not lines:
        await interaction.response.send_message("Голосовые каналы пусты.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Голосовые каналы — {guild.name}",
        description="\n".join(lines),
        color=0x57F287,
    )
    embed.set_footer(text=f"Время: {discord.utils.utcnow().strftime('%d.%m.%Y %H:%M')} UTC")
    await interaction.response.send_message(embed=embed)


# ── Terminal CLI (только если запущен с TTY, не в Docker) ─────────────────────

HELP_TEXT = """
Команды терминала:
  status  — кто сейчас в войсах
  help    — показать это сообщение
  quit    — остановить бота
"""


async def cli_handler():
    loop = asyncio.get_event_loop()
    print(HELP_TEXT)
    while True:
        try:
            raw = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break

        parts = raw.strip().split()
        if not parts:
            continue
        cmd = parts[0]

        if cmd in ("quit", "exit", "q"):
            print("Останавливаю бота...")
            await bot.close()
            break

        elif cmd == "status":
            if not bot.guilds:
                print("Бот ещё не подключён к серверам.")
                continue
            for guild in bot.guilds:
                found = False
                for vc in guild.voice_channels:
                    humans = [m.display_name for m in vc.members if not m.bot]
                    if humans:
                        print(f"  #{vc.name}: {', '.join(humans)}")
                        found = True
                if not found:
                    print(f"[{guild.name}] Все войсы пусты.")

        elif cmd == "help":
            print(HELP_TEXT)

        else:
            print(f"Неизвестная команда: '{cmd}'. Введи 'help'.")


# ── Entry point ───────────────────────────────────────────────────────────────

async def _write_all_leaves():
    """Синтетический LEAVE для всех кто сейчас в войсах (при выключении бота)."""
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                try:
                    await log_event(
                        user_id=str(member.id),
                        username=member.display_name,
                        guild_id=str(guild.id),
                        guild_name=guild.name,
                        event_type="leave",
                        channel_from_id=str(vc.id),
                        channel_from_name=vc.name,
                        channel_to_id=None,
                        channel_to_name=None,
                        timestamp=now,
                    )
                    log.info(f"[SHUTDOWN] LEAVE  {member.display_name:>20}  <-  #{vc.name}")
                except Exception as exc:
                    log.error(f"[SHUTDOWN] leave: {exc}")

    # Закрываем все активные созвоны
    for channel_id in list(active_calls.keys()):
        await _close_call(channel_id)


async def main():
    loop = asyncio.get_event_loop()

    async def graceful_shutdown():
        log.info("[SHUTDOWN] Сигнал получен — записываю LEAVE для всех в войсах...")
        await _write_all_leaves()
        await bot.close()

    def _signal_handler():
        asyncio.create_task(graceful_shutdown())

    # SIGTERM (docker stop) и SIGINT (Ctrl+C) — только на Linux/Mac
    try:
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT,  _signal_handler)
    except NotImplementedError:
        pass  # Windows — сигналы не поддерживаются в asyncio, on_ready-рекончиляция покрывает

    async with bot:
        if sys.stdin.isatty():
            asyncio.create_task(cli_handler())
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
