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


def cached_plan(user):
    ref = schedule.get_program(user["program"])
    book = database.get_cache("book:" + ref["book"]) if ref else None
    group = book["groups"].get(user["group_id"]) if book else None
    name = group["name"] if group else "Ваша группа"
    saved = schedule.expand_plan_buildings([clean_item(v) for v in user["snapshot"] or []])
    if not group:
        return saved, name
    english = None
    if user["en"]:
        ref = schedule.get_english(group["course"])
        english = database.get_cache("en:" + ref["book"]) if ref else None
        if not english:
            return saved, name
    try:
        # Пересчитываем сохранённые таблицы, чтобы старые подписки получили новый вид сразу.
        return plan(book, group, english, user["en"]), name
    except ParseError:
        return saved, name


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


