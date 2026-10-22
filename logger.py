import logging
from config import DATE_FORMAT

logger = logging.getLogger("hse")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt=DATE_FORMAT))
logger.addHandler(console_handler)
