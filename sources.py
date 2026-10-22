"""Discover full-time module PDFs on the university's schedule page."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class ParseError(ValueError):
    pass


class SourceUnavailable(ParseError):
    """The requested date has no published, verified teaching schedule."""


def module_number(text):
    match = re.search(r"\b(IV|III|II|I|[1-4])\s*модул", text, re.I)
    if match:
        value = match[1].upper()
        return int(value) if value.isdigit() else {"I": 1, "II": 2, "III": 3, "IV": 4}[value]
    return None


class SchedulePage(HTMLParser):
    def __init__(self, url):
        super().__init__()
        self.url = url
        self.modules = {}
        self.module = None
        self.teaching = False
        self.capture = None
        self.text = []
        self.href = ""

    def handle_starttag(self, tag, attrs):
        if tag in {"h2", "h3", "h4", "a"}:
            self.capture, self.text = tag, []
            self.href = dict(attrs).get("href", "")

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag != self.capture:
            return
        text = re.sub(r"\s+", " ", "".join(self.text)).strip().casefold()
        if tag in {"h2", "h3"} and module_number(text):
            self.module = module_number(text)
            self.modules.setdefault(str(self.module), {"url": None})
            self.teaching = False
        elif tag == "h2":
            self.module = None
        elif tag == "h4":
            self.teaching = "учебных занятий" in text
        elif tag == "a" and self.module and self.teaching:
            full_time = re.search(r"\bочный\b", text)
            part_time = re.search(r"очно\s*[-–—]?\s*заоч", text)
            url = urljoin(self.url, self.href)
            parts = urlsplit(url)
            if full_time and not part_time and parts.scheme in {"http", "https"} and parts.path.lower().endswith(".pdf"):
                item = self.modules[str(self.module)]
                if item["url"] and item["url"] != url:
                    raise ParseError(f"Для модуля {self.module} найдено несколько очных PDF")
                item["url"] = url
        self.capture = None


def discover_modules(html, url):
    parser = SchedulePage(url)
    parser.feed(html)
    if not parser.modules or not any(item["url"] for item in parser.modules.values()):
        raise ParseError("На странице расписания не найдены опубликованные PDF очного обучения")
    return parser.modules
