import asyncio
import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import database
import schedule
import utils
from config import TZ
from handlers import user as dialogs

FIX = Path(__file__).parent / "fixtures"
LAW_NAMES = ("Правовое обеспечение и защита бизнеса", "Управление правовыми рисками в бизнесе")


class ProgramGroupTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.refs = schedule.catalog((FIX / "index.pdf").read_bytes())
        cls.law_refs = [ref for ref in cls.refs if ref["name"] in LAW_NAMES]
        cls.law = schedule.parse_book((FIX / "law.xlsx").read_bytes())
        cls.english = {
            ref["book"]: schedule.parse_book((FIX / ("en1.xlsx" if ref["name"] == "1 курс" else "en2.xlsx")).read_bytes(), True)
            for ref in cls.refs if ref["english"]
        }

    def test_both_law_program_names_include_current_and_legacy_codes(self):
        extra = {
            "old": dict(id="old", name="27 ПОЗБ-1", course=1, code="ПОЗБ"),
            "bachelor": dict(id="bachelor", name="26-Ю1", course=1, code="Ю"),
            "other": dict(id="other", name="26КНТ-1", course=1, code="КНТ"),
        }
        book = dict(groups={**self.law["groups"], **extra})
        self.assertEqual(len(self.law_refs), 2)
        self.assertEqual(len({ref["book"] for ref in self.law_refs}), 1)
        for ref in self.law_refs:
            with self.subTest(program=ref["name"]):
                groups = schedule.program_groups(book, ref)
                self.assertEqual({g["name"] for g in groups}, {
                    "26 УПРБ-1", "26 УПРБ-2", "25-ЮМ1", "27 ПОЗБ-1",
                })

    async def test_law_setup_from_program_selection_to_saved_subscription(self):
        async def get_book(bid, english=False, fresh=False):
            book = self.english[bid] if english else self.law
            database.save_cache(("en:" if english else "book:") + bid, book)
            return book

        storage = MemoryStorage()
        self.addAsyncCleanup(storage.close)
        index = dict(year=self.law["year"], days=self.law["calendar_days"],
                     modules={"1": dict(url="fixture:index.pdf", refs=self.refs)})
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(database, "DB_PATH", Path(temp) / "bot.db"), \
                patch.multiple(schedule, refs=self.refs, source_index=index, lock=asyncio.Lock()), \
                patch.object(schedule, "get_book", new=AsyncMock(side_effect=get_book)), \
                patch.object(dialogs, "datetime") as clock, \
                patch.object(schedule, "datetime", clock), \
                patch.object(utils, "send_after", 0):
            database.create_database()
            clock.now.return_value = datetime(2026, 10, 21, 12, tzinfo=TZ)
            cases = [(ref, group) for ref in self.law_refs for group in self.law["groups"].values()]
            for uid, (ref, group) in enumerate(cases, 10):
                with self.subTest(program=ref["name"], group=group["name"]):
                    bot = SimpleNamespace(send_message=AsyncMock(), send_rich_message=AsyncMock())
                    message = SimpleNamespace(chat=SimpleNamespace(id=uid, type="private"), text="/start", bot=bot,
                                              answer=AsyncMock(), edit_reply_markup=AsyncMock())
                    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))

                    async def pick(value):
                        data = await state.get_data()
                        index = next(i for i, option in enumerate(data["options"]) if option["value"] == value)
                        callback = SimpleNamespace(data=f"pick:{data['nonce']}:{index}", message=message, answer=AsyncMock())
                        await dialogs.callback_query(callback, state, bot)

                    await dialogs.start(message, state)
                    await pick("МАГИСТРАТУРА")
                    await pick("Факультет права")
                    await pick(ref["id"])
                    data = await state.get_data()
                    self.assertEqual(data["step"], "course")
                    self.assertEqual({o["value"] for o in data["options"]}, {1, 2})
                    await pick(group["course"])
                    self.assertEqual(await state.get_state(), dialogs.Setup.number.state)
                    message.text = group["name"]
                    await dialogs.number(message, state)
                    for _ in range(3):
                        if await state.get_state() != dialogs.Setup.english.state:
                            break
                        message.text = "пропустить"
                        await dialogs.en_number(message, state)
                    self.assertIsNone(await state.get_state())
                    saved = database.get_user(uid)
                    self.assertIsNotNone(saved)
                    self.assertEqual(saved["program"], ref["id"])
                    self.assertEqual(saved["group_id"], group["id"])
                    self.assertTrue(saved["snapshot"])
                    bot.send_rich_message.assert_awaited_once()
                    html = bot.send_rich_message.await_args.kwargs["rich_message"].html
                    self.assertIn(group["name"], html)
                    if group["course"] == 1:
                        self.assertIn("Аудитория не указана", html)
                        self.assertIn("Корпус не указан", html)

    async def test_source_check_validates_all_programs_sharing_one_workbook(self):
        with patch.multiple(schedule, refs=self.law_refs, source_index={}), \
                patch.object(schedule, "get_index", new=AsyncMock()), \
                patch.object(schedule, "get_book", new=AsyncMock(return_value=self.law)) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            await schedule.check_sources()
            fetch.assert_awaited_once()

    async def test_source_check_rejects_a_program_whose_groups_are_filtered_out(self):
        unrelated = dict(self.law_refs[0], name="Компьютерные науки и технологии")
        with patch.multiple(schedule, refs=[self.law_refs[0], unrelated], source_index={}), \
                patch.object(schedule, "get_index", new=AsyncMock()), \
                patch.object(schedule, "get_book", new=AsyncMock(return_value=self.law)), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(schedule.ParseError, "Компьютерные науки и технологии.*нет групп"):
            await schedule.check_sources()


if __name__ == "__main__":
    unittest.main()
