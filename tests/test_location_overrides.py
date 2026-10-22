import unittest
from datetime import date

from messages import _lesson_cells, format_schedule
from schedule import DEFAULT_BUILDINGS, on_day, plan


class LocationOverrideTests(unittest.TestCase):
    def row(self, **values):
        # Ячейки сохранённой официальной таблицы, группа 26-Ю2.
        row = dict(
            day=2, time="08:00-09:20",
            text="Теория государства и права - лекция - \nМакарейко Н.В.",
            room="402\n30.09 в 203", place="К\nБПок",
            start="2026-09-01", end="2026-10-25", mode="all",
        )
        row.update(values)
        return row

    def test_room_and_building_override_apply_together_only_on_the_date(self):
        book = dict(year=2026, weeks={}, buildings=DEFAULT_BUILDINGS)
        items = plan(book, {"rows": [self.row()]})
        for stamp in ["2026-09-23", "2026-10-07", "2026-10-21"]:
            with self.subTest(stamp=stamp):
                item = next(v for v in items if v["date"] == stamp)
                self.assertIn("Ауд.: 402", item["text"])
                self.assertEqual(item["building"], "Костина, 2Б")
                self.assertNotIn("Покровская", item["text"])
        changed = next(v for v in items if v["date"] == "2026-09-30")
        self.assertIn("Ауд.: 203", changed["text"])
        self.assertEqual(changed["building"], "Большая Покровская, 22")
        self.assertNotIn("Костина", changed["text"])

    def test_blank_lines_do_not_separate_the_date_from_its_building(self):
        row = self.row(room="108\n\n21.10. в 012", place="С\n\nК")
        usual = on_day(row, date(2026, 10, 14), 2026, {})[0]["text"]
        changed = on_day(row, date(2026, 10, 21), 2026, {})[0]["text"]
        self.assertIn("Ауд.: 108\nКорпус: С", usual)
        self.assertNotIn("Корпус: К", usual)
        self.assertIn("Ауд.: 012\nКорпус: К", changed)
        self.assertNotIn("Корпус: С", changed)

    def test_multiple_overrides_pick_only_the_current_building(self):
        row = self.row(day=4, room="314\n16.10. в 406\n25.09. в 203", place="БП\nБП\nК")
        for stamp, room, place in [
            (date(2026, 9, 25), "203", "К"),
            (date(2026, 10, 16), "406", "БП"),
            (date(2026, 10, 23), "314", "БП"),
        ]:
            with self.subTest(stamp=stamp):
                text = on_day(row, stamp, 2026, {})[0]["text"]
                self.assertTrue(text.endswith(f"Ауд.: {room}\nКорпус: {place}"), text)

    def test_dated_online_location_does_not_leak_other_buildings(self):
        row = self.row(day=1, room="06.10. в ауд. 203\n\n13.10., 20.10.", place="БПок\n\nonline")
        first = on_day(row, date(2026, 10, 6), 2026, {})[0]["text"]
        later = on_day(row, date(2026, 10, 20), 2026, {})[0]["text"]
        self.assertTrue(first.endswith("Ауд.: 203\nКорпус: БПок"), first)
        self.assertNotIn("Ауд.", later)
        self.assertTrue(later.endswith("Корпус: online"), later)

    def test_separate_subjects_keep_their_own_base_and_override_locations(self):
        row = self.row(
            text="с 02.09 по 30.09 Первый предмет\n\nс 07.10 по 21.10 Второй предмет",
            room="402\n30.09 в 203\n301\n14.10 в 102", place="К\nБПок\nР\nЛ",
        )
        for stamp, room, place in [
            (date(2026, 9, 23), "402", "К"),
            (date(2026, 9, 30), "203", "БПок"),
            (date(2026, 10, 14), "102", "Л"),
            (date(2026, 10, 21), "301", "Р"),
        ]:
            with self.subTest(stamp=stamp):
                items = on_day(row, stamp, 2026, {})
                self.assertEqual(len(items), 1)
                self.assertTrue(items[0]["text"].endswith(f"Ауд.: {room}\nКорпус: {place}"), items)

    def test_multiline_location_stays_in_location_column(self):
        item = dict(date="2026-10-21", time="08:00-09:20",
                    text="Предмет\nПреподаватель\nАуд.: 402\n405\nКорпус: Костина, 2Б\nБольшая Покровская, 22")
        subject, location = _lesson_cells(item)
        self.assertNotIn("405", subject)
        self.assertNotIn("Покровская", subject)
        self.assertIn("405", location)
        self.assertIn("Большая Покровская, 22", location)

    def test_screenshot_group_has_only_correct_location_in_rich_table(self):
        rows = [self.row(), self.row(time="09:30-10:50", text="История государства и права России - лекция -\nЛошкарева М.Е."),
                self.row(time="11:10-12:30", text="02.09.-16.09., 30.09.-21.10. История государства и права России - лекция - Лошкарева М.Е.")]
        items = plan(dict(year=2026, weeks={}, buildings=DEFAULT_BUILDINGS), {"rows": rows})
        html = format_schedule(items, date(2026, 10, 21), "26-Ю2")["html"]
        self.assertEqual(html.count("Костина, 2Б"), 3)
        self.assertEqual(html.count("Ауд. 402"), 3)
        self.assertNotIn("Покровская", html)


if __name__ == "__main__":
    unittest.main()
