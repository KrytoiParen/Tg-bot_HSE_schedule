import re
import unittest
from datetime import date
from html.parser import HTMLParser

from messages import clean_title, format_schedule, split_rich, MAX_TABLE_ROWS


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.rows = 0

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            return
        if tag == "tr":
            self.rows += 1
        if "td" in self.stack:
            assert tag in {"b", "i", "u", "s", "code"}, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


class RichMessageTests(unittest.TestCase):
    day = date(2026, 10, 21)

    def item(self, text="Линейная алгебра - лекция\nШапошников В.Е.\nАуд.: 323\nКорпус: Львовская 1В", **kwargs):
        return dict(date=self.day.isoformat(), time="11:10-12:30", text=text, **kwargs)

    def test_daily_table_and_location(self):
        payload = format_schedule([self.item()], self.day, "26КНТ-6")
        html = payload["html"]
        self.assertTrue(payload["skip_entity_detection"])
        self.assertIn("<h2>26КНТ-6</h2>", html)
        self.assertIn("21.10.2026 · среда", html)
        self.assertIn("<table bordered striped compact>", html)
        self.assertIn("<b>Линейная алгебра</b><br>лекция<br>Шапошников В.Е.", html)
        self.assertIn("<b>Ауд. 323</b><br>Львовская 1В", html)
        self.assertIn("11:10–12:30", html)
        parser = TableParser()
        parser.feed(html)
        self.assertEqual(parser.stack, [])
        self.assertEqual(parser.rows, 2)

    def test_every_source_field_is_escaped(self):
        item = self.item('<script>Предмет & "текст"</script>\n<a>Преподаватель</a>\nАуд.: <301>\nКорпус: А & Б')
        item["time"] = "<10:00>"
        html = format_schedule([item], self.day, "<Группа & 6>")["html"]
        for literal in ["<script>", "<a>", "<301>", "<Группа"]:
            self.assertNotIn(literal, html)
        for escaped in ["&lt;script&gt;", "&amp;", "&quot;", "&lt;301&gt;", "&lt;10:00&gt;"]:
            self.assertIn(escaped, html)

    def test_english_names_drop_internal_labels(self):
        for label in [
            "АЯДОКЦ: ОСНОВНОЙ курс", "АЯДОКЦОК: ПРОДВИНУТЫЙ курс",
            "ЯДОКЦ: БАЗОВЫЙ курс", "АЯДОКЦПК:ОСНОВНОЙ курс",
            "Английский язык: ОСНОВНОЙ гр 1 ( КНТ )", "English language: basic course",
        ]:
            with self.subTest(label=label):
                self.assertEqual(clean_title(label), "Английский язык")
                html = format_schedule([self.item(label + "\nИльичева К.Л.")], self.day, "Группа")["html"]
                self.assertIn("<b>Английский язык</b><br>Ильичева К.Л.", html)
                self.assertNotIn("курс", html)

    def test_special_purpose_english_keeps_subject(self):
        title = "Английский язык: Английский язык для специальных целей. Лингвокультурология"
        self.assertEqual(clean_title(title), "Английский язык для специальных целей. Лингвокультурология")
        self.assertEqual(clean_title("Дискретная математика"), "Дискретная математика")

    def test_other_days_are_filtered_and_lessons_sorted(self):
        late = dict(self.item("Поздняя"), time="13:00-14:20")
        early = dict(self.item("Ранняя"), time="9:30-10:50")
        other = dict(self.item("Завтрашняя"), date="2026-10-22")
        html = format_schedule([late, other, early], self.day, "Группа")["html"]
        self.assertLess(html.index("Ранняя"), html.index("Поздняя"))
        self.assertNotIn("Завтрашняя", html)

    def test_empty_day_footer_and_refresh_failure(self):
        payload = format_schedule([], self.day, "Группа", "2026-10-21T12:17:00+00:00", "<ошибка>")
        self.assertNotIn("<table", payload["html"])
        self.assertIn("занятий на этот день нет", payload["html"])
        self.assertIn("Показана сохранённая версия", payload["html"])
        self.assertNotIn("<ошибка>", payload["html"])
        self.assertIn("<footer>Проверено: 21.10.2026 15:17 МСК.</footer>", payload["html"])
        self.assertEqual(split_rich(payload), [payload])

    def test_long_table_splits_at_whole_rows_with_repeated_context(self):
        items = [self.item(f"Предмет {i:03}\nПреподаватель\nАуд.: {i}\nКорпус: Львовская 1В") for i in range(40)]
        payload = format_schedule(items, self.day, "26КНТ-6", "2026-10-21T12:17:00+00:00")
        chunks = split_rich(payload, limit=1400)
        self.assertGreater(len(chunks), 1)
        subjects = []
        for chunk in chunks:
            self.assertLessEqual(len(chunk["html"]), 1400)
            self.assertTrue(chunk["skip_entity_detection"])
            self.assertIn("<h2>26КНТ-6</h2>", chunk["html"])
            self.assertIn("<th>Время</th>", chunk["html"])
            self.assertIn("Проверено: 21.10.2026 15:17", chunk["html"])
            parser = TableParser()
            parser.feed(chunk["html"])
            self.assertEqual(parser.stack, [])
            subjects.extend(re.findall(r"Предмет (\d+)", chunk["html"]))
        self.assertEqual(subjects, [f"{i:03}" for i in range(40)])

    def test_table_block_limit_applies_even_with_large_character_budget(self):
        payload = format_schedule([self.item("Предмет") for _ in range(MAX_TABLE_ROWS + 5)], self.day, "Группа")
        chunks = split_rich(payload, limit=100000)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["html"].count("<tr>"), MAX_TABLE_ROWS + 1)
        self.assertEqual(chunks[1]["html"].count("<tr>"), 6)

    def test_single_oversized_row_is_rejected_without_breaking_markup(self):
        payload = format_schedule([self.item("Предмет " + "длинный " * 1000)], self.day, "Группа")
        with self.assertRaisesRegex(ValueError, "Одна строка"):
            split_rich(payload, limit=1500)


if __name__ == "__main__":
    unittest.main()
