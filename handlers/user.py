import contextlib
import re
import secrets
from datetime import datetime, timedelta
from aiogram import Router, F, Bot, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
import database
import keyboards
import schedule
from schedule import ParseError, CODES, en_options, norm, plan
from utils import valid_time, send_text, split, format_schedule, cached_plan
from config import MORNING, TZ
from logger import logger

user_router = Router()


class Setup(StatesGroup):
    pick = State()
    number = State()
    english = State()


async def choose(message, state, prompt, options, step):
    # Номер меню защищает от кнопок старого направления и от двойного нажатия.
    nonce = secrets.token_hex(4)
    await state.update_data(options=options, step=step, nonce=nonce)
    await state.set_state(Setup.pick)
    await message.answer(prompt, reply_markup=keyboards.groups([v["label"] for v in options], nonce))


@user_router.message(F.chat.type != "private")
async def private_only(message: types.Message):
    await message.answer(
        "Откройте личный чат с ботом и отправьте /start. Подписка на учебную группу настраивается там."
    )


@user_router.message(CommandStart())
@user_router.message(Command("settings"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    if not schedule.refs:
        try:
            async with schedule.lock:
                await schedule.get_index()
        except Exception:
            logger.exception("Не удалось получить указатель")
            await message.answer("Сайт расписания сейчас недоступен. Попробуйте /start позже.")
            return
    values = sorted({r["level"] for r in schedule.refs if not r["english"]})
    await choose(
        message,
        state,
        "Выберите уровень обучения. Настройку можно отменить: /cancel.",
        [dict(label=v.title(), value=v) for v in values],
        "level",
    )


@user_router.message(Command("today", "tomorrow", "week"))
async def show(message: types.Message, cmd=None):
    user = database.get_user(message.chat.id)
    if not user:
        await message.answer("Сначала выберите группу: /start.")
        return
    if user["snapshot"] is None:
        await message.answer("Расписание ещё не проверено. Попробуйте позже.")
        return
    today = datetime.now(TZ).date()
    cmd = cmd or (message.text or "/today").split()[0].split("@")[0]
    offset, count = (1, 1) if cmd == "/tomorrow" else (0, 7) if cmd == "/week" else (0, 1)
    items, name = cached_plan(user)
    for i in range(offset, offset + count):
        day = today + timedelta(days=i)
        await send_text(
            message.bot,
            message.chat.id,
            split(format_schedule(items, day, name, user["checked"], user["error"])),
        )


@user_router.message(Command("english"))
async def edit_en(message: types.Message, state: FSMContext):
    user = database.get_user(message.chat.id)
    if not user:
        await message.answer("Сначала выберите учебную группу: /start.")
        return
    ref = schedule.get_program(user["program"])
    if not ref:
        await message.answer("Направление пропало из источника. Откройте /settings.")
        return
    book = await schedule.get_book(ref["book"])
    group = book["groups"].get(user["group_id"])
    if not group:
        await message.answer("Учебная группа пропала из источника. Откройте /settings.")
        return
    await state.clear()
    await state.update_data(program=user["program"], group_id=user["group_id"], course=group["course"], en=[])
    await ask_en(message, state)


@user_router.message(Command("time"))
async def set_time(message: types.Message):
    user = database.get_user(message.chat.id)
    if not user:
        await message.answer("Сначала выберите группу: /start.")
        return
    args = (message.text or "").split()
    if len(args) != 2 or not valid_time(args[1]):
        await message.answer("Напишите /time 07:00. Время московское, формат ЧЧ:ММ.")
        return
    database.set_time(message.chat.id, args[1])
    await message.answer(f"Ежедневное расписание будет приходить в {args[1]} МСК.")


@user_router.message(Command("stop"))
async def stop(message: types.Message, state: FSMContext):
    database.stop_user(message.chat.id)
    await state.clear()
    await message.answer("Рассылка отключена. Вернуть её: /start.")


@user_router.message(Command("cancel"))
async def cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Настройка отменена. Существующая подписка сохранена.")


@user_router.message(Command("status"))
async def status(message: types.Message):
    user = database.get_user(message.chat.id)
    if not user:
        await message.answer("Подписки нет. Откройте /start.")
        return
    text = f"Рассылка: {'включена' if user['active'] else 'выключена'}.\nВремя: {user['morning']} МСК."
    if user["checked"]:
        checked = datetime.fromisoformat(user["checked"]).astimezone(TZ)
        text += f"\nПоследняя успешная проверка: {checked:%d.%m.%Y %H:%M} МСК."
    if user["error"]:
        text += "\n⚠️ " + user["error"]
    await message.answer(text)


@user_router.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer(
        "/menu — главное меню\n/start, /settings — выбрать направление и группу\n/today — сегодня\n/tomorrow — завтра\n/week — ближайшие 7 дней\n"
        "/english — выбрать английский\n/time 07:00 — время ежедневной рассылки (МСК)\n/status — состояние проверки\n"
        "/stop — отключить рассылку\n/cancel — отменить настройку\n"
        "Учебная группа и группа английского могут иметь разные номера. Уведомления получают подписавшиеся студенты этой группы в личных чатах."
    )


@user_router.callback_query()
async def callback_query(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    if callback.data == "cancel":
        await callback.answer()
        await state.clear()
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Настройка отменена. Существующая подписка сохранена.")
        return
    if callback.data in ["today", "tomorrow", "week", "settings", "english", "time", "status", "stop"]:
        await callback.answer()
        if callback.data in ["today", "tomorrow", "week"]:
            await show(callback.message, "/" + callback.data)
        elif callback.data == "settings":
            await start(callback.message, state)
        elif callback.data == "english":
            await edit_en(callback.message, state)
        elif callback.data == "time":
            await callback.message.answer("Для смены времени напишите /time 08:00. Время московское.")
        elif callback.data == "status":
            await status(callback.message)
        elif callback.data == "stop":
            await stop(callback.message, state)
        return
    if not callback.data or not callback.data.startswith("pick:") or len(callback.data.split(":")) != 3:
        await callback.answer("Это меню устарело. Откройте /settings.")
        return
    data = await state.get_data()
    _, nonce, index = callback.data.split(":")
    if nonce != data.get("nonce") or not index.isdigit() or int(index) >= len(data.get("options", [])):
        await callback.answer("Это меню устарело. Откройте /settings.", show_alert=True)
        return
    selected = data["options"][int(index)]
    await state.update_data(nonce="")
    await callback.answer()
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)
    message, step = callback.message, data["step"]
    try:
        if step == "level":
            level = selected["value"]
            await state.update_data(level=level)
            faculties = sorted({r["faculty"] for r in schedule.refs if r["level"] == level and not r["english"]})
            await choose(message, state, "Выберите факультет.", [dict(label=v, value=v) for v in faculties], "faculty")
        elif step == "faculty":
            refs = [
                r
                for r in schedule.refs
                if r["level"] == data["level"] and r["faculty"] == selected["value"] and not r["english"]
            ]
            await choose(
                message, state, "Выберите направление.", [dict(label=r["name"], value=r["id"]) for r in refs], "program"
            )
        elif step == "program":
            ref = schedule.get_program(selected["value"])
            async with schedule.lock:
                book = await schedule.get_book(ref["book"], fresh=True)
            codes = CODES.get(ref["name"])
            groups = [g for g in book["groups"].values() if not codes or g["code"] in codes]
            if not groups:
                raise ParseError("В таблице нет групп выбранного направления")
            await state.update_data(program=ref["id"], groups=groups)
            await choose(
                message,
                state,
                "Выберите курс.",
                [dict(label=f"{v} курс", value=v) for v in sorted({g["course"] for g in groups})],
                "course",
            )
        elif step == "course":
            groups = [g for g in data["groups"] if g["course"] == selected["value"]]
            await state.update_data(groups=groups)
            await state.set_state(Setup.number)
            await message.answer(
                "Напишите полное название учебной группы или её номер.\nНапример: 26КНТ-1 или 1.\n\n"
                + "\n".join(g["name"] for g in groups)
            )
        elif step == "group":
            await set_group(message, state, selected["value"])
        elif step == "en":
            ids = data.get("en", []) + [selected["value"]]
            await state.update_data(en=ids)
            await next_en(message, state)
    except Exception as e:
        logger.exception("Ошибка настройки")
        await state.clear()
        await message.answer(
            "Не удалось проверить расписание. "
            + (str(e) if isinstance(e, ParseError) else "Источник временно недоступен.")
            + "\nТекущая подписка сохранена. Попробуйте /settings позже."
        )


@user_router.message(Setup.number, F.text)
async def number(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = norm(message.text)
    groups = [g for g in data["groups"] if norm(g["name"]) == text]
    if not groups and text.isdigit():
        groups = [
            g
            for g in data["groups"]
            if re.search(r"(\d+)$", norm(g["name"])) and int(re.search(r"(\d+)$", norm(g["name"]))[1]) == int(text)
        ]
    if not groups:
        if text == "1" and len(data["groups"]) == 1:
            await set_group(message, state, data["groups"][0]["id"])
            return
        await message.answer("Такой группы нет в выбранном расписании. Скопируйте полное название из списка выше.")
    elif len(groups) > 1:
        await choose(
            message, state, "Уточните учебную группу.", [dict(label=g["name"], value=g["id"]) for g in groups], "group"
        )
    else:
        await set_group(message, state, groups[0]["id"])


async def set_group(message, state, gid):
    data = await state.get_data()
    group = next(g for g in data["groups"] if g["id"] == gid)
    await state.update_data(group_id=gid, course=group["course"], en=[], modes=None)
    await ask_en(message, state)


async def ask_en(message, state):
    data = await state.get_data()
    ref = schedule.get_english(data["course"])
    if not ref:
        await save_user(message, state)
        return
    try:
        async with schedule.lock:
            book = await schedule.get_book(ref["book"], True, fresh=True)
        program = schedule.get_program(data["program"])
        own = await schedule.get_book(program["book"])
        group = own["groups"][data["group_id"]]
        options = en_options(book, program, group)
        modes = [v for v in ["all", "upper", "lower"] if any(e["mode"] == v for e in options)]
        await state.update_data(en_options=options, modes=modes)
        await next_en(message, state)
    except Exception:
        logger.exception("Ошибка английского")
        await state.clear()
        await message.answer("Не удалось проверить английский. Текущая подписка сохранена. Попробуйте /settings позже.")


async def next_en(message, state):
    data = await state.get_data()
    modes = data.get("modes", [])
    if not modes:
        await save_user(message, state)
        return
    mode = modes[0]
    options = [e for e in data["en_options"] if e["mode"] == mode]
    await state.update_data(modes=modes[1:], choices=options, mode=mode)
    await state.set_state(Setup.english)
    title = {
        "all": "английского",
        "upper": "английского на верхней неделе",
        "lower": "английского для международных экзаменов на нижней неделе",
    }[mode]
    nums = ", ".join(str(v) for v in sorted({e["num"] for e in options}))
    await message.answer(
        f"Напишите номер вашей группы {title}. Он может отличаться от учебной группы.\n"
        f"Номера в таблице: {nums}.\n"
        "Если вы не знаете номер, уточните его у преподавателя. Можно написать «пропустить»; "
        "тогда эта часть английского не попадёт в расписание. 0 означает занятие без номера в источнике."
    )


@user_router.message(Setup.english, F.text)
async def en_number(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = (message.text or "").strip().lower()
    if text == "пропустить":
        await next_en(message, state)
        return
    if not text.isdigit():
        await message.answer("Введите номер группы английского, например 8, или «пропустить».")
        return
    options = [e for e in data["choices"] if e["num"] == int(text)]
    if not options:
        await message.answer("Такого номера нет для выбранного направления. Проверьте номер или напишите «пропустить».")
        return
    labels = [
        dict(label=f"{e['level'].title()}, {e['code'] or 'общая группа'}, " + ", ".join(e["teachers"]), value=e["id"])
        for e in options
    ]
    await choose(message, state, "Выберите свою группу по уровню и преподавателю.", labels, "en")


async def save_user(message, state):
    data = await state.get_data()
    uid = message.chat.id
    old = database.get_user(uid)
    user = dict(program=data["program"], group_id=data["group_id"], en=data.get("en", []))
    try:
        async with schedule.lock:
            book, group, en = await schedule.get_data(user, fresh=True)
        items = plan(book, group, en, user["en"])
    except Exception:
        logger.exception("Не удалось сохранить группу")
        await state.clear()
        await message.answer("Не удалось проверить расписание. Настройки не изменены. Попробуйте /settings позже.")
        return
    at = old["morning"] if old else MORNING
    database.save_user(uid, user["program"], user["group_id"], user["en"], at, items)
    await state.clear()
    await message.answer(
        f"Группа сохранена. Расписание — в {at} МСК, проверка изменений — каждый час.\n"
        "/today — сегодня, /tomorrow — завтра, /week — 7 дней.\n"
        "/time 08:00 — время рассылки, /english — английский, /stop — отключить."
    )
    if schedule.get_english(group["course"]) and not user["en"]:
        await message.answer("Группа английского не выбрана. Когда узнаете номер, откройте /english.")
    if en and en["year"] != book["year"]:
        await message.answer(
            "В таблице английского старый календарь. Даты и недели взяты из основного расписания вашей группы."
        )
    await show(message)


@user_router.message(Command("menu"))
async def menu(message: types.Message):
    await message.answer("📅 Расписание ВШЭ НН\nВыберите действие.", reply_markup=keyboards.main_menu)


@user_router.message()
async def fallback(message: types.Message):
    await message.answer("Используйте кнопки текущего меню или /help. Для новой настройки: /settings.")
