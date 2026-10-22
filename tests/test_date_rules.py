import unittest
from datetime import date
from pathlib import Path

from schedule import clean_dates, date_rules, on_day, parse_book, plan


class DateRulesTests(unittest.TestCase):
    def row(self, **values):
        row = dict(
            day=2, time="11:10-12:30", text="Математика", room="323", place="Л",
            start="2026-09-01", end="2026-10-25", mode="all",
        )
        row.update(values)
        return row

    def test_start_date_filters_lessons_and_is_hidden(self):
        row = self.row(text="с 09.09. Линейная алгебра и геометрия - лекция\nШапошников В.Е.")
        self.assertFalse(on_day(row, date(2026, 9, 2), 2026, {}))
        lesson = on_day(row, date(2026, 10, 21), 2026, {})[0]
        self.assertIn("Линейная алгебра и геометрия - лекция", lesson["text"])
        self.assertIn("Шапошников В.Е.", lesson["text"])
        self.assertNotIn("09.09", lesson["text"])

    def test_teacher_cancellation_only_skips_the_cancelled_day(self):
        for note in ["17.09 отм", "17.09. - отмена занятия", "отм. 17.09", "не проводится 17.09"]:
            with self.subTest(note=note):
                row = self.row(day=3, text="Английский язык", teacher="Ильичева К.Л. " + note)
                self.assertFalse(on_day(row, date(2026, 9, 17), 2026, {}))
                lesson = on_day(row, date(2026, 10, 22), 2026, {})[0]
                self.assertIn("Ильичева К.Л.", lesson["text"])
                self.assertNotIn("17.09", lesson["text"])
                self.assertNotIn("отм", lesson["text"])
                self.assertNotIn("не проводится", lesson["text"])

    def test_exceptions_do_not_turn_into_allowed_dates(self):
        allowed, denied = date_rules("с 09.09 по 21.10, кроме 23.09, 30.09\nПредмет", 2026)
        self.assertIn(date(2026, 10, 21), allowed)
        self.assertEqual(denied, {date(2026, 9, 23), date(2026, 9, 30)})
        row = self.row(text="с 09.09 по 21.10, кроме 23.09, 30.09\nПредмет")
        self.assertFalse(on_day(row, date(2026, 9, 23), 2026, {}))
        self.assertTrue(on_day(row, date(2026, 10, 7), 2026, {}))

    def test_date_list_and_range_select_actual_subject(self):
        row = self.row(text="02.09., 16.09. Алгебра\nПреподаватель А.\n\nс 23.09 по 21.10 Геометрия\nПреподаватель Б.")
        early = on_day(row, date(2026, 9, 16), 2026, {})[0]["text"]
        later = on_day(row, date(2026, 10, 21), 2026, {})[0]["text"]
        self.assertIn("Алгебра", early)
        self.assertNotIn("Геометрия", early)
        self.assertIn("Геометрия", later)
        self.assertNotIn("Алгебра", later)
        self.assertNotRegex(early + later, r"\d{2}\.\d{2}")

    def test_only_current_teacher_and_class_type_remain(self):
        row = self.row(
            text="Предмет\nс 02.09 по 23.09 - лекция\nс 30.09 по 21.10 - семинар",
            teacher="Преподаватель А. с 02.09 по 23.09\nПреподаватель Б. с 30.09 по 21.10",
        )
        lesson = on_day(row, date(2026, 10, 21), 2026, {})[0]["text"]
        self.assertIn("семинар", lesson)
        self.assertIn("Преподаватель Б.", lesson)
        self.assertNotIn("лекция", lesson)
        self.assertNotIn("Преподаватель А.", lesson)
        self.assertNotRegex(lesson, r"\d{2}\.\d{2}")

    def test_teacher_and_group_start_dates_are_applied(self):
        for field in ["teacher", "group"]:
            row = self.row(**{field: "с 16.09"})
            with self.subTest(field=field):
                self.assertFalse(on_day(row, date(2026, 9, 9), 2026, {}))
                self.assertTrue(on_day(row, date(2026, 9, 16), 2026, {}))

    def test_room_override_does_not_restrict_other_weeks_or_leak_note(self):
        row = self.row(day=3, teacher="Ватлецов С.Г. 24.09. в 324 (Львовская)", room="301")
        usual = on_day(row, date(2026, 10, 1), 2026, {})[0]["text"]
        changed = on_day(row, date(2026, 9, 24), 2026, {})[0]["text"]
        self.assertIn("Ауд.: 301", usual)
        self.assertIn("Ватлецов С.Г.", usual)
        self.assertIn("Ауд.: 324", changed)
        self.assertIn("Корпус: Львовская", changed)
        self.assertNotIn("24.09", usual + changed)

    def test_room_note_is_hidden_and_used_only_on_that_date(self):
        row = self.row(room="305\n23.09 в 103")
        usual = on_day(row, date(2026, 10, 21), 2026, {})[0]["text"]
        changed = on_day(row, date(2026, 9, 23), 2026, {})[0]["text"]
        self.assertIn("Ауд.: 305", usual)
        self.assertIn("Ауд.: 103", changed)
        self.assertNotIn("23.09", usual + changed)

    def test_time_change_keeps_the_room_and_other_weeks(self):
        row = self.row(teacher="Преподаватель А. (23.09 с 13:00)")
        usual = on_day(row, date(2026, 10, 21), 2026, {})[0]
        changed = on_day(row, date(2026, 9, 23), 2026, {})[0]
        self.assertEqual(usual["time"], "11:10-12:30")
        self.assertEqual(changed["time"], "13:00-14:20")
        self.assertIn("Ауд.: 323", changed["text"])
        self.assertNotIn("23.09", usual["text"] + changed["text"])

    def test_online_change_only_applies_on_the_given_day(self):
        row = self.row(text="Математика\n23.09 - online")
        usual = on_day(row, date(2026, 10, 21), 2026, {})[0]["text"]
        changed = on_day(row, date(2026, 9, 23), 2026, {})[0]["text"]
        self.assertIn("Ауд.: 323", usual)
        self.assertIn("Корпус: Онлайн", changed)
        self.assertNotIn("Ауд.", changed)
        self.assertNotIn("23.09", usual + changed)

    def test_latin_start_marker_and_spaced_date(self):
        row = self.row(text="c 09. 09 по 21.10 Математика")
        self.assertFalse(on_day(row, date(2026, 9, 2), 2026, {}))
        self.assertIn("Математика", on_day(row, date(2026, 10, 21), 2026, {})[0]["text"])
        self.assertEqual(clean_dates(row["text"], date(2026, 10, 21), 2026), "Математика")

    def test_do_range_is_a_real_date_restriction(self):
        allowed, denied = date_rules("с 09.09 до 07.10 Математика", 2026)
        self.assertEqual(denied, set())
        self.assertNotIn(date(2026, 9, 2), allowed)
        self.assertIn(date(2026, 10, 7), allowed)
        self.assertNotIn(date(2026, 10, 14), allowed)
        row = self.row(text="с 09.09 до 07.10 Математика")
        self.assertFalse(on_day(row, date(2026, 9, 2), 2026, {}))
        self.assertTrue(on_day(row, date(2026, 10, 7), 2026, {}))
        self.assertFalse(on_day(row, date(2026, 10, 14), 2026, {}))

    def test_fixture_matches_screenshot_without_date_annotations(self):
        fixtures = Path(__file__).parent / "fixtures"
        book = parse_book((fixtures / "main.xlsx").read_bytes())
        english = parse_book((fixtures / "en1.xlsx").read_bytes(), True)
        group = next(group for group in book["groups"].values() if group["name"] == "26КНТ-6")
        algebra = [lesson for lesson in plan(book, group) if lesson["date"] == "2026-10-21" and lesson["time"] == "11:10-12:30"]
        self.assertEqual(len(algebra), 1)
        self.assertNotIn("с 09.09", algebra[0]["text"])
        option = next(item for item in english["en"].values() if item["code"] == "КНТ" and item["num"] == 10 and item["level"] == "ОСНОВНОЙ")
        lessons = plan(book, {"rows": []}, english, [option["id"]])
        self.assertFalse(any(lesson["date"] == "2026-09-17" for lesson in lessons))
        current = [lesson for lesson in lessons if lesson["date"] == "2026-10-22"]
        self.assertEqual(len(current), 2)
        self.assertTrue(all("Ильичева К.Л." in lesson["text"] for lesson in current))
        self.assertTrue(all("17.09" not in lesson["text"] for lesson in current))


if __name__ == "__main__":
    unittest.main()
