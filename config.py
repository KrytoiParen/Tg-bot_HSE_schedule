import os
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

CURRENT_DIR = Path(__file__).resolve().parent
load_dotenv(CURRENT_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.db"))
if not DB_PATH.is_absolute():
    DB_PATH = CURRENT_DIR / DB_PATH

SCHEDULE_URL = os.getenv("SCHEDULE_URL", "https://nnov.hse.ru/uch/schedule")
MORNING = os.getenv("MORNING", "07:00")
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
TZ = ZoneInfo("Europe/Moscow")
