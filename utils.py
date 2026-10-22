import asyncio
import re
import time
from datetime import date, datetime, timedelta
from aiogram import Bot
from aiogram.types import InputRichMessage
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError
import database
import schedule
from schedule import ParseError, plan
from config import TZ
from logger import logger
from messages import format_schedule, split_rich, clean_title

send_after = 0


def split(text, limit=3500):
    if isinstance(text, dict):
        return split_rich(text)
    chunks = []
    while len(text) > limit:
        pos = text.rfind("\n", 0, limit)
        if pos < limit // 2:
            pos = limit
        chunks.append(text[:pos])
        text = text[pos:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def lesson(item):
    return item["time"] + "\n" + item["text"]


def cached_plan(user, day=None):
    day = day or datetime.now(TZ).date()
    ref = schedule.get_program(user["program"], day)
    book = database.get_cache("book:" + ref["book"]) if ref else None
    group = book["groups"].get(user["group_id"]) if book else None
    name = group["name"] if group else "Ваша группа"
    saved = schedule.expand_plan_buildings([clean_item(v) for v in user["snapshot"] or []])
    if not group:
        if schedule.source_index:
            raise schedule.SourceUnavailable("Расписание выбранного модуля ещё не загружено. Повторите запрос позже.")
        return saved, name
    schedule.validate_schedule(book, group, day)
    english = None
    if user["en"]:
        ref = schedule.get_english(group["course"], day)
        english = database.get_cache("en:" + ref["book"]) if ref else None
        if not english:
            if schedule.source_index:
                raise schedule.SourceUnavailable("Расписание английского для выбранного модуля ещё не загружено. Повторите запрос позже.")
            return saved, name
        schedule.validate_english(english, book, user["en"], day)
    try:
        # Пересчитываем сохранённые таблицы, чтобы старые подписки получили новый вид сразу.
        return plan(book, group, english, user["en"]), name
    except ParseError:
        if schedule.source_index:
            raise
        return saved, name


async def daily_schedule(user, day, seen=None):
    notice = schedule.source_notice(day)
    if notice:
        return [], "Ваша группа", notice
    try:
        if schedule.source_index:
            async with schedule.lock:
                book, group, english = await schedule.get_data(user, day=day, seen=seen)
            return plan(book, group, english, user["en"]), group["name"], None
        items, name = cached_plan(user, day)
        return items, name, None
    except schedule.SourceUnavailable as error:
        return [], "Ваша группа", str(error)
    except Exception:
        logger.exception("Не удалось получить расписание на %s", day)
        try:
            items, name = cached_plan(user, day)
            return items, name, "Источник временно недоступен. Показана сохранённая версия расписания."
        except ParseError as error:
            return [], "Ваша группа", str(error)


def clean_item(item):
    day = date.fromisoformat(item["date"])
    year = day.year if day.month >= 9 else day.year - 1
    lines = []
    for line in item["text"].splitlines():
        if re.match(r"^(?:Ауд\.:|Корпус:)", line):
            lines.append(schedule.base_location(line))
        else:
            lines.append(schedule.clean_dates(line, day, year))
    text = "\n".join(line for line in lines if line)
    text = "\n".join(clean_title(line) for line in text.splitlines())
    return dict(item, text=text)


def changes(old, new, today):
    def index(items):
        result = {}
        for item in schedule.expand_plan_buildings([clean_item(v) for v in items]):
            if item["date"] >= today.isoformat():
                result[item["date"], item["time"], item["text"]] = item
        return result

    a, b = index(old), index(new)
    removed = [a[k] for k in a.keys() - b.keys()]
    added = [b[k] for k in b.keys() - a.keys()]
    if not removed and not added:
        return []
    result = ["Изменилось ваше расписание."]
    for title, items in [("❌ Удалено:", removed), ("✅ Добавлено:", added)]:
        if items:
            result.append(title)
            for item in sorted(items, key=lambda v: (v["date"], v["time"], v["text"])):
                day = date.fromisoformat(item["date"])
                result.append(f"{day:%d.%m.%Y}\n{lesson(item)}")
    return split("\n\n".join(result))


def valid_time(text):
    return bool(re.fullmatch(r"(?:0\d|1\d|2[0-3]):[0-5]\d", text))


async def send_text(bot, uid, texts):
    for text in texts:
        if isinstance(text, dict):
            await bot.send_rich_message(uid, rich_message=InputRichMessage(**text))
        else:
            await bot.send_message(uid, text)


async def refresh():
    async with schedule.lock:
        try:
            await schedule.get_index()
        except Exception:
            logger.exception("Не удалось загрузить указатель")
            for user in database.get_users():
                database.save_error(
                    user["id"],
                    "Указатель расписания недоступен.",
                    ["Не удалось проверить расписание. Сохранённая версия: /today. Повторим через час."],
                )
            return
        seen = {}
        for user in database.get_users():
            try:
                book, group, en = await schedule.get_data(user, fresh=True, seen=seen)
                items = plan(book, group, en, user["en"])
                notes = (
                    changes(user["snapshot"], items, datetime.now(TZ).date()) if user["snapshot"] is not None else []
                )
                current = database.get_user(user["id"])
                # Пока загружались таблицы, студент мог сменить группу.
                if current and current["active"] and all(current[k] == user[k] for k in ("program", "group_id", "en")):
                    database.save_plan(user["id"], items, notes)
            except Exception as e:
                logger.exception("Не удалось проверить группу %s", user["id"])
                text = str(e) if isinstance(e, ParseError) else "Не удалось загрузить расписание."
                database.save_error(user["id"], text, [text + " Сохранённая версия: /today. Повторим через час."])


def morning_mail(now):
    day = now.date()
    for u in database.get_users():
        hour, minute = map(int, u["morning"].split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not due <= now <= due + timedelta(hours=2) or u["snapshot"] is None:
            continue
        notice = schedule.source_notice(day)
        items, name = [], "Ваша группа"
        if not notice:
            try:
                items, name = cached_plan(u, day)
            except ParseError as error:
                notice = str(error)
        text = format_schedule(items, day, name, u["checked"], u["error"], notice=notice)
        database.add_morning(u["id"], day.isoformat(), split(text))


async def deliver(bot):
    global send_after
    if time.monotonic() < send_after:
        return min(60, send_after - time.monotonic())
    failed = set()
    for mail in database.get_mail():
        uid = mail["user_id"]
        if uid in failed:
            continue
        if mail["tag"].startswith("morning:") and mail["tag"][8:] != datetime.now(TZ).date().isoformat():
            database.mark_sent(mail["id"])
            continue
        try:
            await send_text(bot, uid, [mail["text"]])
            database.mark_sent(mail["id"])
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            logger.warning("Telegram ограничил отправку на %s секунд", e.retry_after)
            send_after = time.monotonic() + e.retry_after + 1
            return min(e.retry_after + 1, 60)
        except TelegramForbiddenError:
            database.stop_user(uid)
        except TelegramBadRequest as e:
            logger.error("Не удалось отправить сообщение %s: %s", mail["id"], e)
            if "chat not found" in str(e).lower():
                database.stop_user(uid)
            else:
                failed.add(uid)
        except TelegramAPIError:
            failed.add(uid)
            logger.exception("Ошибка Telegram; сообщение осталось в очереди")
    return 0


async def check_schedule(bot: Bot):
    next_check = 0
    while True:
        try:
            if time.monotonic() >= next_check:
                # Проверка раз в час; длительная загрузка не создаёт вторую копию задания.
                next_check = time.monotonic() + 3600
                await refresh()
            morning_mail(datetime.now(TZ))
            delay = await deliver(bot)
            await asyncio.sleep(max(15, delay))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой задачи; повтор через минуту")
            await asyncio.sleep(60)
