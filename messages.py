"""Telegram rich-message presentation for an already resolved daily schedule."""

import re
from datetime import datetime
from html import escape

from config import TZ

DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MAX_TABLE_ROWS = 200
TABLE_OPEN = "<table bordered striped compact>"
TABLE_HEADER = "<tr><th>Время</th><th>Занятие</th><th>Где</th></tr>"
ENGLISH_TITLE = re.compile(
    r"^(?:А?ЯДОКЦ[А-ЯA-Z]*|английский(?:\s+язык)?|english(?:\s+language)?)\b", re.I
)
LESSON_TYPE = re.compile(
    r"\s+[-–—]\s+(?=(?:лекци\w*|семинар\w*|практи\w*|лаборатор\w*|"
    r"консультаци\w*|зач[её]т\w*|экзамен\w*)\b)", re.I
)


def clean_title(text):
    """Give English a human-readable name without internal course/group labels."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not ENGLISH_TITLE.match(text):
        return text
    special = re.search(r"для\s+специальных\s+целей\b.*", text, re.I)
    if special:
        return "Английский язык " + special.group(0)
    if re.search(r"\bfor\s+specific\s+purposes\b", text, re.I):
        return "Английский язык для специальных целей"
    return "Английский язык"


def _lesson_cells(item):
    content, rooms, places = [], [], []
    target = content
    for line in str(item.get("text", "")).splitlines():
        line = line.strip()
        if not line:
            continue
        room = re.match(r"^(?:Ауд\.?|Аудитория)\s*:\s*(.*)", line, re.I)
        place = re.match(r"^Корпус\s*:\s*(.*)", line, re.I)
        if room:
            target = rooms
            rooms.append(room.group(1))
        elif place:
            target = places
            places.append(place.group(1))
        else:
            target.append(line)

    title = content.pop(0) if content else "Занятие"
    parts = LESSON_TYPE.split(title, maxsplit=1)
    title = clean_title(parts[0])
    if len(parts) > 1:
        content.insert(0, parts[1])
    subject = "<b>" + escape(title) + "</b>"
    if content:
        subject += "<br>" + "<br>".join(escape(line) for line in content)
    location = []
    if rooms:
        location.append("<b>Ауд. " + escape(", ".join(rooms)) + "</b>")
    location.extend(escape(place) for place in places)
    return subject, "<br>".join(location) or "—"


def _time_order(item):
    match = re.match(r"(\d{1,2}):(\d{2})", str(item.get("time", "")))
    return (int(match[1]) * 60 + int(match[2]) if match else 1440, str(item.get("text", "")))


def format_schedule(items, day, name, checked=None, error=None):
    """Return an InputRichMessage-compatible dictionary for one schedule day.

    Dates, cancellations and room overrides must be applied by the schedule parser
    before presentation. All values from the source are escaped as literal text.
    """
    blocks = [
        f"<h2>{escape(str(name))}</h2>",
        f"<h3>{day:%d.%m.%Y} · {DAYS[day.weekday()]}</h3>",
    ]
    today = sorted((item for item in items if item["date"] == day.isoformat()), key=_time_order)
    if today:
        blocks.extend([TABLE_OPEN, TABLE_HEADER])
        for item in today:
            subject, location = _lesson_cells(item)
            time = re.sub(r"\s*[-–—]\s*", "–", str(item.get("time", "")))
            blocks.append(
                f'<tr><td valign="top"><b>{escape(time)}</b></td>'
                f'<td valign="top">{subject}</td><td valign="top">{location}</td></tr>'
            )
        blocks.append("</table>")
    else:
        blocks.append("<p>В опубликованном расписании занятий на этот день нет.</p>")
    if error:
        blocks.append("<p>⚠️ Не удалось обновить расписание. Показана сохранённая версия; проверьте источник.</p>")
    if checked:
        stamp = checked if isinstance(checked, datetime) else datetime.fromisoformat(checked)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=TZ)
        stamp = stamp.astimezone(TZ)
        blocks.append(f"<footer>Проверено: {stamp:%d.%m.%Y %H:%M} МСК.</footer>")
    return {"html": "\n".join(blocks), "skip_entity_detection": True}


def split_rich(payload, limit=30000):
    """Split generated schedule HTML at whole rows, repeating its context.

    The character budget counts markup too, conservatively below Telegram's
    32768-character text limit. A maximum of 200 data rows leaves room below the
    500-block limit. A single row that cannot fit is rejected without data loss.
    """
    if limit <= 0:
        raise ValueError("Лимит rich-сообщения должен быть положительным.")
    html = payload["html"]
    if not html:
        return []
    table = re.fullmatch(r"(.*?)(<table\b[^>]*>)(.*?)(</table>)(.*)", html, re.S)
    if not table:
        if len(html) > limit:
            raise ValueError("Заголовок rich-сообщения превышает лимит.")
        return [dict(payload)]

    before, opening, body, closing, after = table.groups()
    rows = re.findall(r"<tr\b[^>]*>.*?</tr>", body, re.S)
    if not rows or "<th" not in rows[0]:
        raise ValueError("В rich-таблице отсутствует строка заголовков.")
    prefix = before + opening + "\n" + rows[0] + "\n"
    suffix = "\n" + closing + after
    overhead = len(prefix) + len(suffix)
    chunks, current = [], []
    length = overhead
    for row in rows[1:]:
        if overhead + len(row) > limit:
            raise ValueError("Одна строка расписания превышает лимит rich-сообщения.")
        addition = len(row) + bool(current)
        if current and (length + addition > limit or len(current) >= MAX_TABLE_ROWS):
            chunks.append(dict(payload, html=prefix + "\n".join(current) + suffix))
            current, length = [], overhead
            addition = len(row)
        current.append(row)
        length += addition
    if current:
        chunks.append(dict(payload, html=prefix + "\n".join(current) + suffix))
    elif not chunks:
        if overhead > limit:
            raise ValueError("Заголовок rich-таблицы превышает лимит.")
        chunks.append(dict(payload, html=prefix + suffix))
    return chunks
