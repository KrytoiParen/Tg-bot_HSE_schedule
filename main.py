import argparse
import asyncio
import contextlib
import fcntl
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from config import BOT_TOKEN, DB_PATH, MORNING
from database import create_database
from handlers.user import user_router
from utils import check_schedule, valid_time
from logger import logger
import schedule


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="проверить расписания без Telegram")
    args = parser.parse_args()
    if not valid_time(MORNING):
        raise SystemExit("MORNING должен быть в формате ЧЧ:ММ, например 07:00")
    if not args.check and not BOT_TOKEN:
        raise SystemExit("Добавьте BOT_TOKEN в .env. Образец: .env.example")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Один процесс на базу, чтобы не отправлять одну рассылку дважды.
    with open(str(DB_PATH) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Бот с этой базой уже запущен. Остановите второй экземпляр.")

        create_database()
        bot = None
        task = None
        try:
            await schedule.open_sources()
            if args.check:
                await schedule.check_sources()
                return
            bot = Bot(token=BOT_TOKEN)
            dp = Dispatcher()
            dp.include_router(user_router)
            await bot.set_my_commands(
                [
                    BotCommand(command="menu", description="Главное меню"),
                    BotCommand(command="today", description="Расписание на сегодня"),
                    BotCommand(command="tomorrow", description="Расписание на завтра"),
                    BotCommand(command="week", description="Ближайшие 7 дней"),
                    BotCommand(command="settings", description="Настроить группу"),
                    BotCommand(command="english", description="Настроить английский"),
                    BotCommand(command="time", description="Время рассылки"),
                    BotCommand(command="status", description="Последняя проверка"),
                    BotCommand(command="stop", description="Отключить рассылку"),
                    BotCommand(command="help", description="Все команды"),
                ]
            )
            task = asyncio.create_task(check_schedule(bot))
            logger.info("Бот начал свою работу")
            await dp.start_polling(bot, close_bot_session=False)
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if bot:
                await bot.session.close()
            await schedule.close_sources()


if __name__ == "__main__":
    asyncio.run(main())
