import asyncio
import copy
import io
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import load_workbook

import database
import schedule as source
import utils as bot_code
from handlers import user as user_code
from config import TZ
from utils import valid_time, changes, split
from schedule import catalog, parse_book, en_options, plan, on_day, date_rules, ParseError, current_english

FIX = Path(__file__).parent / "fixtures"


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.refs = catalog((FIX / "index.pdf").read_bytes())
        cls.ref = next(r for r in cls.refs if r["name"] == "Компьютерные науки и технологии")
        cls.book = parse_book((FIX / "main.xlsx").read_bytes())
        cls.en1 = parse_book((FIX / "en1.xlsx").read_bytes(), True)
        cls.en2 = parse_book((FIX / "en2.xlsx").read_bytes(), True)
        cls.group = next(g for g in cls.book["groups"].values() if g["name"] == "26КНТ-1")

    def option(self, number, level, book=None, course=1, mode=None):
        group = dict(self.group, course=course)
        choices = en_options(book or self.en1, self.ref, group, mode)
        return next(e for e in choices if e["num"] == number and e["level"] == level and e["code"] == "КНТ")

    def test_catalog_is_links_with_faculties(self):
        self.assertEqual(len(self.refs), 27)
        self.assertEqual(len({r["book"] for r in self.refs}), 23)
        self.assertEqual(self.ref["faculty"], "Факультет информатики, математики и компьютерных наук")
        self.assertEqual(len([r for r in self.refs if r["english"]]), 2)

    def test_courses_and_groups(self):
        self.assertEqual(len(self.book["groups"]), 30)
        self.assertEqual(self.group["course"], 1)
        self.assertTrue(self.group["refs"])

    def test_english_merged_cell_uses_exact_line_and_level(self):
        a = self.option(8, "БАЗОВЫЙ")
        b = self.option(8, "ОСНОВНОЙ")
        d = date(2026, 10, 1)
        first = plan(self.book, {"rows": []}, self.en1, [a["id"]])
        second = plan(self.book, {"rows": []}, self.en1, [b["id"]])
        first = [v for v in first if v["date"] == d.isoformat()]
        second = [v for v in second if v["date"] == d.isoformat()]
        self.assertEqual(len(first), 2)
        self.assertTrue(all("Султанова А.В." in v["text"] and "Ауд.: 324" in v["text"] for v in first))
        self.assertTrue(all("Милащенко Н.Г." in v["text"] and "Ауд.: 309" in v["text"] for v in second))
        self.assertTrue(all("Галочкин" not in v["text"] and "Давыдова" not in v["text"] for v in first + second))

    def test_number_1_does_not_pick_11(self):
        e = self.option(1, "БАЗОВЫЙ")
        for row in e["rows"]:
            self.assertEqual(row["group"].split("(")[0].strip(), "гр 1")
        items = plan(self.book, {"rows": []}, self.en1, [e["id"]])
        self.assertTrue(any("Ватлецов" in v["text"] for v in items))
        self.assertFalse(any("Давыдова" in v["text"] for v in items))

    def test_faculty_and_program_filter(self):
        choices = en_options(self.en1, self.ref, self.group)
        self.assertFalse(any(e["code"] == "ТИДИ" for e in choices))
        self.assertFalse(any(e["faculty"] == "Факультет права" for e in choices))

    def test_shared_business_program_uses_shared_english_scope(self):
        ref = next(
            r
            for r in self.refs
            if r["name"] == "Международный бакалавриат по бизнесу и экономике" and r["faculty"] == "Факультет экономики"
        )
        choices = en_options(self.en1, ref, dict(self.group, code="МПБЭ"))
        self.assertTrue(any(e["code"] == "МПБЭ" for e in choices))
        self.assertTrue(any(not e["code"] and e["faculty"] == "Факультет менеджмента" for e in choices))

    def test_second_course_week_is_from_official_calendar(self):
        self.assertEqual(self.en2["year"], 2025)
        fixed = current_english(self.en2, self.book)
        self.assertEqual(fixed["year"], 2026)
        self.assertEqual(fixed["weeks"]["2026-09-14"], "upper")
        self.assertEqual(fixed["weeks"]["2026-09-21"], "lower")
        choices = en_options(self.en2, self.ref, dict(self.group, course=2))
        upper = next(e for e in choices if e["mode"] == "upper" and e["rows"])
        lower = next(e for e in choices if e["mode"] == "lower" and e["rows"])
        for e in [upper, lower]:
            items = plan(self.book, {"rows": []}, self.en2, [e["id"]])
            self.assertTrue(items)
            self.assertTrue(all(fixed["weeks"][v["date"]] == e["mode"] for v in items))

    def test_english_start_and_cancellation(self):
        e = self.option(6, "ОСНОВНОЙ")
        row = next(r for r in e["rows"] if r["day"] == 3 and r["time"] == "14:40-16:00")
        self.assertFalse(on_day(row, date(2026, 9, 17), 2026, self.en1["weeks"]))
        self.assertTrue(on_day(row, date(2026, 9, 24), 2026, self.en1["weeks"]))
        row = dict(
            day=0,
            time="09:30-10:50",
            text="с 14.09. Теория государства и права\nсеминар\n05.10. - отмена занятия",
            room="107",
            place="С",
            start="2026-09-01",
            end="2026-10-25",
            mode="all",
        )
        self.assertFalse(on_day(row, date(2026, 10, 5), 2026, {}))
        self.assertTrue(on_day(row, date(2026, 10, 12), 2026, {}))

    def test_date_ranges_and_lists(self):
        allowed, denied = date_rules("07.09.-19.10. Основы государственности\n12.10. отмена занятия", 2026)
        self.assertIn(date(2026, 10, 5), allowed)
        self.assertIn(date(2026, 10, 12), denied)
        allowed, _ = date_rules("07.09-19.10 Предмет", 2026)
        self.assertIn(date(2026, 10, 5), allowed)
        allowed, _ = date_rules("07.09., 21.09., 05.10., 19.10. Предмет", 2026)
        self.assertNotIn(date(2026, 10, 12), allowed)
        allowed, _ = date_rules("07.09, 14.09, с 05.10 по 19.10\nПредмет", 2026)
        self.assertNotIn(date(2026, 9, 28), allowed)
        self.assertIn(date(2026, 10, 12), allowed)

    def test_alternative_subjects_have_correct_rooms(self):
        row = dict(
            day=4,
            time="08:00-09:20",
            text="02.10., 16.10. Программирование\nЛупанова\n\n09.10., 23.10. Технологии\nУлитин",
            room="102\n\n\n203",
            place="Р",
            start="2026-09-01",
            end="2026-10-25",
            mode="all",
        )
        result = on_day(row, date(2026, 10, 9), 2026, {})
        self.assertEqual(len(result), 1)
        self.assertIn("Ауд.: 203", result[0]["text"])
        self.assertNotIn("Лупанова", result[0]["text"])

    def test_time_override_does_not_make_every_week(self):
        row = dict(
            day=1,
            time="11:10-12:30",
            text="20.10. в 11:10-11:50 Линейная алгебра - лекция - Хомицкий",
            room="302",
            place="БП",
            start="2026-09-01",
            end="2026-10-25",
            mode="all",
        )
        self.assertFalse(on_day(row, date(2026, 10, 13), 2026, {}))
        self.assertEqual(on_day(row, date(2026, 10, 20), 2026, {})[0]["time"], "11:10-11:50")

    def test_room_override(self):
        row = dict(
            day=3,
            time="08:00-09:20",
            text="Английский",
            teacher="Ватлецов С.Г. 24.09. в 324 (Львовская)",
            room="301",
            place="БП",
            start="2026-09-14",
            end="2026-10-25",
            mode="all",
        )
        result = on_day(row, date(2026, 9, 24), 2026, {})[0]["text"]
        self.assertIn("Ауд.: 324", result)
        self.assertIn("Корпус: Львовская", result)

    def test_horizontal_merge_copies_lecture(self):
        w = load_workbook(FIX / "main.xlsx", data_only=True)
        s = w["1 курс"]
        s.merge_cells("M13:S13")
        s["M13"] = "05.10. Общая лекция"
        s["T13"] = "999"
        s["U13"] = "БП"
        out = io.BytesIO()
        w.save(out)
        w.close()
        b = parse_book(out.getvalue())
        for name in ["26КНТ-3", "26КНТ-4"]:
            g = next(g for g in b["groups"].values() if g["name"] == name)
            items = [v for v in plan(b, g) if v["date"] == "2026-10-05" and "Общая лекция" in v["text"]]
            self.assertEqual(len(items), 1)
            self.assertIn("Ауд.: 999", items[0]["text"])

    def test_bad_source_fails(self):
        with self.assertRaises(Exception):
            parse_book(b"<html>sign in</html>")
        with self.assertRaises(ParseError):
            plan(self.book, self.group, self.en1, ["missing"])

    def test_diff_is_semantic_and_has_all_changes(self):
        old = [dict(date="2026-10-05", time="08:00-09:20", text="Математика\nАуд.: 301")]
        new = [dict(old[0], text="Математика\nАуд.: 302")]
        text = "\n".join(changes(old, new, date(2026, 10, 3)))
        self.assertIn("Удалено", text)
        self.assertIn("Добавлено", text)
        self.assertIn("301", text)
        self.assertIn("302", text)
        self.assertEqual(changes(old, old, date(2026, 10, 3)), [])
        self.assertEqual(changes(old, [], date(2026, 10, 6)), [])

    def test_telegram_message_limit(self):
        text = "Предмет\n" * 1800
        values = split(text)
        self.assertGreater(len(values), 1)
        self.assertTrue(all(len(v) <= 3500 for v in values))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"
        self.source = source
        self.refs = catalog((FIX / "index.pdf").read_bytes())
        self.patches = [
            patch.object(database, "DB_PATH", self.path),
            patch.multiple(
                source,
                refs=self.refs,
                lock=asyncio.Lock(),
                get_book=source.get_book,
                get_index=source.get_index,
                http=None,
            ),
            patch.object(bot_code, "send_after", 0),
            patch.object(user_code, "MORNING", "07:00"),
        ]
        for item in self.patches:
            item.start()
        database.create_database()
        self.ref = next(r for r in source.refs if r["name"] == "Компьютерные науки и технологии")
        self.book = parse_book((FIX / "main.xlsx").read_bytes())
        database.save_cache("book:" + self.ref["book"], self.book)
        database.save_cache("catalog", source.refs)
        self.groups = [g for g in self.book["groups"].values() if g["name"] in ["26КНТ-1", "26КНТ-2"]]
        for uid, g in enumerate(self.groups, 1):
            database.save_user(uid, self.ref["id"], g["id"], [], "07:00", plan(self.book, g))

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_cache_survives_restart(self):
        # Каждое чтение открывает базу заново.
        self.assertEqual(database.get_user(1)["group_id"], self.groups[0]["id"])
        self.assertEqual(database.get_cache("catalog"), self.source.refs)

    async def test_morning_is_once_per_day_after_restart(self):
        now = datetime(2026, 10, 3, 7, 1, tzinfo=TZ)
        bot_code.morning_mail(now)
        bot_code.morning_mail(now)
        self.assertEqual(len(database.get_mail()), 2)
        for item in database.get_mail():
            database.mark_sent(item["id"])
        bot_code.morning_mail(now)
        self.assertEqual(len(database.get_mail()), 0)
        bot_code.morning_mail(now + timedelta(days=1))
        self.assertEqual(len(database.get_mail()), 2)

    async def test_missed_morning_is_not_sent_at_night(self):
        bot_code.morning_mail(datetime(2026, 10, 3, 23, 0, tzinfo=TZ))
        self.assertEqual(database.get_mail(), [])

    async def test_stop_clears_pending(self):
        bot_code.morning_mail(datetime(2026, 10, 3, 7, 1, tzinfo=TZ))
        database.stop_user(1)
        self.assertEqual([v["user_id"] for v in database.get_mail()], [2])

    async def test_refresh_notifies_only_affected_group(self):
        changed = copy.deepcopy(self.book)
        changed["groups"][self.groups[0]["id"]]["rows"][0]["room"] = "999"

        async def book(bid, en=False, fresh=False):
            if fresh:
                database.save_cache("book:" + bid, changed)
            return database.get_cache("book:" + bid)

        self.source.get_book = book
        self.source.get_index = AsyncMock()
        with patch("utils.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 3, tzinfo=TZ)
            await bot_code.refresh()
        self.assertTrue(database.get_mail())
        self.assertEqual({v["user_id"] for v in database.get_mail()}, {1})
        for item in database.get_mail():
            database.mark_sent(item["id"])
        with patch("utils.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 3, tzinfo=TZ)
            await bot_code.refresh()
        self.assertEqual(database.get_mail(), [])

    async def test_fetch_error_does_not_delete_lessons(self):
        old = database.get_user(1)["snapshot"]
        self.source.get_index = AsyncMock()

        async def book(bid, en=False, fresh=False):
            if fresh:
                raise ParseError("download failed")
            return self.book

        self.source.get_book = book
        await bot_code.refresh()
        self.assertEqual(database.get_user(1)["snapshot"], old)
        self.assertTrue(database.get_user(1)["error"])
        self.assertFalse(any("Удалено" in v["text"] for v in database.get_mail()))

    async def test_failed_telegram_send_stays_in_queue(self):
        bot_code.morning_mail(datetime.now(TZ).replace(hour=7, minute=1))
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import SendMessage

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=TelegramNetworkError(SendMessage(chat_id=1, text="x"), "network")),
            send_rich_message=AsyncMock(side_effect=TelegramNetworkError(SendMessage(chat_id=1, text="x"), "network")),
        )
        before = database.get_mail()
        with patch("utils.datetime") as clock:
            clock.now.return_value = datetime.now(TZ).replace(hour=7, minute=1)
            await bot_code.deliver(bot)
        self.assertEqual(database.get_mail(), before)

    async def test_daily_queue_and_snapshot_are_atomic(self):
        old = database.get_user(1)["snapshot"]
        database.save_plan(1, old, ["Изменение"])
        self.assertEqual(len(database.get_mail()), 1)
        self.assertEqual(database.get_user(1)["snapshot"], old)

    async def test_time_validation(self):
        for v in ["00:00", "07:00", "23:59"]:
            self.assertTrue(valid_time(v))
        for v in ["24:00", "7:00", "12:60", "no"]:
            self.assertFalse(valid_time(v))


class DialogTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ServiceTests.asyncSetUp
    asyncTearDown = ServiceTests.asyncTearDown

    async def test_full_setup_through_aiogram(self):
        from aiogram import Bot, Dispatcher
        from aiogram.client.session.base import BaseSession
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.context import FSMContext
        from aiogram.types import Message, Chat, User, Update, CallbackQuery
        from aiogram.methods import SendMessage, SendRichMessage, AnswerCallbackQuery

        sent = []
        requests = []
        answers = []

        class Session(BaseSession):
            async def make_request(self, bot, method, timeout=None):
                if isinstance(method, (SendMessage, SendRichMessage)):
                    requests.append(method)
                    msg = Message(
                        message_id=len(sent) + 1,
                        date=datetime.now(TZ),
                        chat=Chat(id=9, type="private"),
                        text=method.rich_message.html if isinstance(method, SendRichMessage) else method.text,
                    )
                    sent.append(msg)
                    return msg
                if isinstance(method, AnswerCallbackQuery):
                    answers.append(method.text)
                return True

            async def close(self):
                pass

            async def stream_content(self, *args, **kwargs):
                yield b""

        bot = Bot("123456789:" + "A" * 35, session=Session())
        dp = Dispatcher()
        dp.include_router(user_code.user_router)
        state = FSMContext(storage=dp.storage, key=StorageKey(bot_id=bot.id, chat_id=9, user_id=9))
        en = parse_book((FIX / "en1.xlsx").read_bytes(), True)
        er = self.source.get_english(1)
        database.save_cache("en:" + er["book"], en)

        async def book(bid, english=False, fresh=False):
            return en if english else self.book

        self.source.get_book = book
        user = User(id=9, is_bot=False, first_name="Студент")
        uid = 0

        async def message(text):
            nonlocal uid
            uid += 1
            msg = Message(
                message_id=uid, date=datetime.now(TZ), chat=Chat(id=9, type="private"), from_user=user, text=text
            )
            await dp.feed_update(bot, Update(update_id=uid, message=msg))

        async def pick(value):
            data = await state.get_data()
            index = next(i for i, o in enumerate(data["options"]) if o["value"] == value)
            await click(f"pick:{data['nonce']}:{index}")

        async def click(data):
            nonlocal uid
            uid += 1
            query = CallbackQuery(id=str(uid), from_user=user, chat_instance="9", message=sent[-1], data=data)
            await dp.feed_update(bot, Update(update_id=uid, callback_query=query))

        await message("/start")
        await message("/cancel")
        self.assertIsNone(await state.get_state())
        self.assertIsNone(database.get_user(9))
        await message("/start")
        await click("cancel")
        self.assertIsNone(await state.get_state())
        await click("pick:bad")
        self.assertIn("устарело", answers[-1])

        await message("/start")
        await pick(self.ref["level"])
        await pick(self.ref["faculty"])
        await pick(self.ref["id"])
        await pick(1)
        await message("26КНТ-1")
        await message("8")
        data = await state.get_data()
        self.assertEqual(len(data["options"]), 2)
        eid = next(
            e["id"] for e in en_options(en, self.ref, self.groups[0]) if e["num"] == 8 and e["level"] == "БАЗОВЫЙ"
        )
        await pick(eid)
        saved = database.get_user(9)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["en"], [eid])
        self.assertEqual(saved["group_id"], self.groups[0]["id"])
        self.assertIsNone(await state.get_state())
        await message("/menu")
        self.assertIn("Выберите действие", sent[-1].text)
        with patch("handlers.user.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 3, tzinfo=TZ)
            await click("tomorrow")
        self.assertIn("04.10.2026", sent[-1].text)
        self.assertIsInstance(requests[-1], SendRichMessage)
        await click("settings")
        self.assertIsNotNone(await state.get_state())
        await click("cancel")
        self.assertIsNone(await state.get_state())
        self.assertEqual(database.get_user(9)["en"], saved["en"])
        await click("english")
        self.assertEqual(await state.get_state(), user_code.Setup.english.state)
        await message("/cancel")
        self.assertIsNone(await state.get_state())
        self.assertEqual(database.get_user(9)["en"], saved["en"])
        await click("time")
        self.assertIn("/time 08:00", sent[-1].text)
        await message("/time 08:00")
        self.assertEqual(database.get_user(9)["morning"], "08:00")
        await click("status")
        self.assertIn("08:00", sent[-1].text)
        await click("stop")
        self.assertFalse(database.get_user(9)["active"])
        await bot.session.close()

    async def test_english_change_is_only_for_selected_english_group(self):
        en = parse_book((FIX / "en1.xlsx").read_bytes(), True)
        choices = en_options(en, self.ref, self.groups[0])
        first = next(e for e in choices if e["num"] == 8 and e["level"] == "БАЗОВЫЙ")
        second = next(e for e in choices if e["num"] == 8 and e["level"] == "ОСНОВНОЙ")
        for uid, e in [(1, first), (2, second)]:
            database.save_user(
                uid,
                self.ref["id"],
                self.groups[0]["id"],
                [e["id"]],
                "07:00",
                plan(self.book, self.groups[0], en, [e["id"]]),
            )
        changed = copy.deepcopy(en)
        for row in changed["en"][first["id"]]["rows"]:
            row["room"] = "777"

        async def book(bid, english=False, fresh=False):
            value = changed if english else self.book
            database.save_cache(("en:" if english else "book:") + bid, value)
            return value

        self.source.get_book = book
        self.source.get_index = AsyncMock()
        with patch("utils.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 3, tzinfo=TZ)
            await bot_code.refresh()
        self.assertTrue(database.get_mail())
        self.assertEqual({v["user_id"] for v in database.get_mail()}, {1})


if __name__ == "__main__":
    unittest.main()
