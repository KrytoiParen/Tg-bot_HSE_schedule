import copy
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendRichMessage
from aiogram.types import InputRichMessage

import database
import schedule
import utils
from config import TZ


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(database, "DB_PATH", Path(self.tmp.name) / "bot.db")
        self.db.start()
        database.create_database()
        database.save_user(1, "program", "group", [], "07:00", [])
        self.payload = {"html": "<h2>Расписание</h2><p>Занятий нет.</p>", "skip_entity_detection": True}
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_rich_message=AsyncMock())

    async def asyncTearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    async def test_queue_and_delivery_preserve_rich_content_and_old_text(self):
        database.add_morning(1, datetime.now(TZ).date().isoformat(), [self.payload, "старое сообщение"])
        self.assertEqual([v["text"] for v in database.get_mail()], [self.payload, "старое сообщение"])
        with patch("utils.send_after", 0):
            await utils.deliver(self.bot)
        args = self.bot.send_rich_message.await_args
        self.assertEqual(args.args, (1,))
        self.assertIsInstance(args.kwargs["rich_message"], InputRichMessage)
        self.assertEqual(args.kwargs["rich_message"].html, self.payload["html"])
        self.bot.send_message.assert_awaited_once_with(1, "старое сообщение")
        self.assertEqual(database.get_mail(), [])

    async def test_rich_send_failure_keeps_message_for_retry(self):
        database.add_morning(1, datetime.now(TZ).date().isoformat(), [self.payload])
        method = SendRichMessage(chat_id=1, rich_message=InputRichMessage(**self.payload))
        self.bot.send_rich_message.side_effect = TelegramNetworkError(method, "network")
        with patch("utils.send_after", 0):
            await utils.deliver(self.bot)
        self.assertEqual([v["text"] for v in database.get_mail()], [self.payload])
        self.bot.send_message.assert_not_awaited()

    async def test_rich_rate_limit_keeps_queue_and_delays_retry(self):
        database.add_morning(1, datetime.now(TZ).date().isoformat(), [self.payload])
        method = SendRichMessage(chat_id=1, rich_message=InputRichMessage(**self.payload))
        self.bot.send_rich_message.side_effect = TelegramRetryAfter(method, "retry later", retry_after=30)
        with patch("utils.send_after", 0):
            delay = await utils.deliver(self.bot)
            self.assertEqual(delay, 31)
            await utils.deliver(self.bot)
        self.assertEqual(len(database.get_mail()), 1)
        self.bot.send_rich_message.assert_awaited_once()

    async def test_old_queued_plain_json_is_not_reinterpreted(self):
        text = '{"html": "обычный текст"}'
        database.add_morning(1, "2026-10-21", [text])
        self.assertEqual(database.get_mail()[0]["text"], text)


class CacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures = Path(__file__).parent / "fixtures"
        cls.book = schedule.parse_book((fixtures / "main.xlsx").read_bytes())
        cls.group = next(g for g in cls.book["groups"].values() if g["name"] == "26КНТ-6")

    def test_existing_cache_recalculates_without_network_or_changes_to_subscription(self):
        book = copy.deepcopy(self.book)
        book.pop("buildings", None)
        user = dict(program="program", group_id=self.group["id"], en=[], snapshot=[])
        original = copy.deepcopy(user)
        with patch("schedule.get_program", return_value={"book": "source"}), \
                patch("database.get_cache", return_value=book), \
                patch("schedule.download", new=AsyncMock()) as download:
            items, name = utils.cached_plan(user)
        self.assertEqual(name, "26КНТ-6")
        today = [v for v in items if v["date"] == "2026-10-21"]
        self.assertEqual(len(today), 3)
        self.assertTrue(all("Львовская, 1В" in v["text"] for v in today))
        self.assertTrue(all("09.09" not in v["text"] for v in today))
        self.assertEqual(user, original)
        download.assert_not_awaited()

    def test_formatting_upgrade_does_not_create_change_notification(self):
        old = [dict(date="2026-10-21", time="11:10-12:30",
                    text="с 09.09. Линейная алгебра и геометрия - лекция\nШапошников В.Е.\nАуд.: 323\nКорпус: Л")]
        new = [v for v in schedule.plan(self.book, self.group)
               if v["date"] == "2026-10-21" and v["time"] == "11:10-12:30"]
        self.assertEqual(utils.changes(old, new, date(2026, 10, 21)), [])

    def test_missing_source_still_formats_legacy_snapshot(self):
        user = dict(program="program", group_id=self.group["id"], en=[], snapshot=[
            dict(date="2026-10-21", time="11:10-12:30",
                 text="с 09.09. Линейная алгебра - лекция\nШапошников В.Е.\nАуд.: 323\nКорпус: Л")
        ])
        original = copy.deepcopy(user)
        with patch("schedule.get_program", return_value=None):
            items, _ = utils.cached_plan(user)
        html = utils.format_schedule(items, date(2026, 10, 21), "26КНТ-6")["html"]
        self.assertIn("Львовская, 1В", html)
        self.assertNotIn("09.09", html)
        self.assertEqual(user, original)

    def test_missing_english_cache_keeps_english_in_fallback(self):
        user = dict(program="program", group_id=self.group["id"], en=["english"], snapshot=[
            dict(date="2026-10-22", time="09:30-10:50",
                 text="АЯДОКЦ: ОСНОВНОЙ курс\nИльичева К.Л. 17.09 отм\nАуд.: 303\nКорпус: Л")
        ])
        with patch("schedule.get_program", return_value={"book": "source"}), \
                patch("schedule.get_english", return_value={"book": "english"}), \
                patch("database.get_cache", side_effect=[self.book, None]):
            items, _ = utils.cached_plan(user)
        html = utils.format_schedule(items, date(2026, 10, 22), "26КНТ-6")["html"]
        self.assertIn("Английский язык", html)
        self.assertIn("Ильичева К.Л.", html)
        self.assertIn("Львовская, 1В", html)
        self.assertNotIn("17.09", html)
        self.assertNotIn("ОСНОВНОЙ", html)


if __name__ == "__main__":
    unittest.main()
