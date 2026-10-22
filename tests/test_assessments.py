import copy
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import schedule
import utils
from config import TZ
from messages import format_schedule

FIX = Path(__file__).parent / "fixtures"
# Public source downloaded on 22.10.2026; the reported annotation is in Q42:
# https://docs.google.com/spreadsheets/d/1Gz4VhoAo3Tpejbxac-0SHnCcXPSxCwcuJFuCWnfjw2w/edit


class AssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = schedule.parse_book((FIX / "economics.xlsx").read_bytes())
        cls.group = next(g for g in cls.book["groups"].values() if g["name"] == "26МПБЭ-4")

    def row(self, **values):
        row = dict(
            day=3, time="11:10-12:30",
            text="17.09-15.10 Математический анализ-семинар-Оносова И.В.\n"
                 "22.10 Контрольная работа ауд.323",
            room="225", place="Л", start="2026-09-01", end="2026-10-25", mode="all",
        )
        row.update(values)
        return row

    def test_reported_economics_control_work_reaches_telegram_table(self):
        items = schedule.plan(self.book, self.group)
        controls = [v for v in items if v["date"] == "2026-10-22" and "Контрольная работа" in v["text"]]
        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertEqual(control["time"], "11:10-12:30")
        self.assertIn("Математический анализ", control["text"])
        self.assertIn("Оносова И.В.", control["text"])
        self.assertIn("Ауд.: 323", control["text"])
        self.assertEqual(control["building"], "Львовская, 1В")
        self.assertNotIn("Экономико-управленческое", control["text"])
        self.assertNotIn("225", control["text"])
        html = format_schedule(items, date(2026, 10, 22), self.group["name"])["html"]
        self.assertIn("Контрольная работа", html)
        self.assertIn("<b>Ауд. 323</b>", html)
        self.assertIn("11:10–12:30", html)

    def test_previous_week_keeps_regular_subject_and_room(self):
        items = [v for v in schedule.plan(self.book, self.group)
                 if v["date"] == "2026-10-15" and v["time"] == "11:10-12:30"]
        self.assertEqual(len(items), 1)
        self.assertIn("Математический анализ", items[0]["text"])
        self.assertIn("Ауд.: 225", items[0]["text"])
        self.assertNotIn("Контрольная работа", items[0]["text"])

    def test_unrestricted_subject_only_shows_control_on_its_date(self):
        items = schedule.plan(self.book, self.group)
        for stamp in ("2026-10-16", "2026-10-23"):
            lessons = [v for v in items if v["date"] == stamp and v["time"] == "14:40-16:00"]
            self.assertEqual(len(lessons), 1)
            self.assertIn("Линейная алгебра", lessons[0]["text"])
            self.assertEqual("Контрольная работа" in lessons[0]["text"], stamp == "2026-10-23")

    def test_abbreviations_and_inline_room_notation(self):
        for label in ("КР", "К.Р.", "Контрольная работа"):
            for room in ("ауд.323", "ауд. 323", "аудитория: 323", "в ауд. 323"):
                with self.subTest(label=label, room=room):
                    row = self.row(text=f"17.09-15.10 Математический анализ\n22.10 {label} {room}")
                    items = schedule.on_day(row, date(2026, 10, 22), 2026, {})
                    self.assertEqual(len(items), 1)
                    self.assertIn("Математический анализ", items[0]["text"])
                    self.assertIn("Контрольная работа", items[0]["text"])
                    self.assertIn("Ауд.: 323", items[0]["text"])

    def test_date_list_applies_control_work_to_each_listed_day(self):
        row = self.row(text="Математический анализ\n08.10, 22.10 Контрольная работа ауд.323")
        for day in (8, 15, 22):
            items = schedule.on_day(row, date(2026, 10, day), 2026, {})
            self.assertEqual(len(items), 1)
            self.assertEqual("Контрольная работа" in items[0]["text"], day in (8, 22))
            self.assertIn("Ауд.: " + ("323" if day in (8, 22) else "225"), items[0]["text"])

    def test_multiple_controls_keep_their_own_dates_and_rooms(self):
        row = self.row(text="Математический анализ\n08.10 КР ауд.301\n22.10 КР ауд.323")
        for day, room in ((8, "301"), (15, "225"), (22, "323")):
            items = schedule.on_day(row, date(2026, 10, day), 2026, {})
            self.assertEqual(len(items), 1)
            self.assertIn("Ауд.: " + room, items[0]["text"])
            self.assertEqual(items[0]["text"].count("Контрольная работа"), int(day != 15))

    def test_standalone_control_does_not_repeat_on_other_weeks(self):
        row = self.row(text="22.10 Контрольная работа ауд.323")
        self.assertEqual(schedule.on_day(row, date(2026, 10, 15), 2026, {}), [])
        self.assertEqual(len(schedule.on_day(row, date(2026, 10, 22), 2026, {})), 1)

    def test_explicit_cancellation_still_wins(self):
        row = self.row(text=self.row()["text"] + "\n22.10 отмена занятия")
        self.assertEqual(schedule.on_day(row, date(2026, 10, 22), 2026, {}), [])

    def test_dated_control_without_room_keeps_the_subject_location(self):
        row = self.row(text="17.09-15.10 Математический анализ\n22.10 Контрольная работа")
        items = schedule.on_day(row, date(2026, 10, 22), 2026, {})
        self.assertEqual(len(items), 1)
        self.assertIn("Ауд.: 225", items[0]["text"])

    def test_control_after_a_blank_line_keeps_subject_and_paired_location(self):
        row = self.row(
            text="03.09 Экономика\n\n17.09-15.10 Математический анализ\n\n22.10 КР ауд.323",
            room="403\n225", place="БП\nЛ",
        )
        items = schedule.on_day(row, date(2026, 10, 22), 2026, {})
        self.assertEqual(len(items), 1)
        self.assertIn("Математический анализ", items[0]["text"])
        self.assertIn("Корпус: Л", items[0]["text"])
        self.assertNotIn("Экономика", items[0]["text"])

    def test_control_time_and_room_override_are_applied_together(self):
        row = self.row(text="17.09-15.10 Математический анализ\n22.10 КР в 13:00 ауд.323")
        items = schedule.on_day(row, date(2026, 10, 22), 2026, {})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["time"], "13:00-14:20")
        self.assertIn("Ауд.: 323", items[0]["text"])

    def test_planning_does_not_modify_the_source_book(self):
        before = copy.deepcopy(self.book)
        schedule.plan(self.book, self.group)
        self.assertEqual(self.book, before)


class AssessmentDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_subscription_recalculates_and_delivers_the_control(self):
        book = schedule.parse_book((FIX / "economics.xlsx").read_bytes())
        group = next(g for g in book["groups"].values() if g["name"] == "26МПБЭ-4")
        ref = dict(id="economics", book="economics-source")
        now = datetime(2026, 10, 22, 7, 1, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(database, "DB_PATH", Path(temp) / "bot.db"), \
                patch.object(schedule, "refs", [ref]), \
                patch.object(schedule, "download", new=AsyncMock()) as download:
            database.create_database()
            database.save_cache("book:" + ref["book"], book)
            database.save_user(1, ref["id"], group["id"], [], "07:00", [])
            user = database.get_user(1)
            items, name = utils.cached_plan(user)
            self.assertEqual(name, "26МПБЭ-4")
            self.assertTrue(any(v["date"] == "2026-10-22" and "Контрольная работа" in v["text"] for v in items))
            notice = "\n".join(utils.changes([], items, now.date()))
            self.assertIn("Контрольная работа", notice)
            self.assertIn("Ауд.: 323", notice)
            utils.morning_mail(now)
            mail = database.get_mail()
            self.assertEqual(len(mail), 1)
            self.assertIn("Контрольная работа", mail[0]["text"]["html"])
            bot = SimpleNamespace(send_message=AsyncMock(), send_rich_message=AsyncMock())
            with patch.object(utils, "datetime") as clock, patch.object(utils, "send_after", 0):
                clock.now.return_value = now
                await utils.deliver(bot)
            bot.send_rich_message.assert_awaited_once()
            html = bot.send_rich_message.await_args.kwargs["rich_message"].html
            self.assertIn("Контрольная работа", html)
            self.assertIn("<b>Ауд. 323</b>", html)
            self.assertEqual(database.get_mail(), [])
            self.assertEqual(database.get_user(1)["group_id"], group["id"])
            download.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
