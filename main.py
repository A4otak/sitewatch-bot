import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import LinkPreviewOptions
from aiogram.utils.token import TokenValidationError

from bot.config import Config
from bot.database import Database
from bot.handlers import monitors, start
from bot.services.scheduler import Scheduler

logger = logging.getLogger(__name__)


class PrivateAccess(BaseMiddleware):
    def __init__(self, config: Config):
        self.config = config

    async def __call__(self, handler, event, data):
        try:
            user, chat = data.get("event_from_user"), data.get("event_chat")
            if user and chat and chat.type == ChatType.PRIVATE and chat.id == user.id and self.config.allows(user.id):
                return await handler(event, data)
            if event.callback_query:
                await event.callback_query.answer("Бот доступен только разрешённым пользователям в личном чате.", show_alert=True)
            elif event.message and chat and chat.type == ChatType.PRIVATE:
                await event.message.answer("У вас нет доступа к этому боту.")
        except TelegramAPIError as exc:
            logger.warning("Ответ Telegram не доставлен (%s)", type(exc).__name__)
        return None


async def main() -> None:
    config = Config.from_env()
    defaults = DefaultBotProperties(link_preview=LinkPreviewOptions(is_disabled=True))
    async with Bot(config.bot_token, default=defaults) as bot:
        db = Database(config.db_path)
        await db.initialize()
        scheduler = Scheduler(bot, db, config)
        dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
        dispatcher.update.outer_middleware(PrivateAccess(config))
        dispatcher.include_routers(start.router, monitors.router)
        await bot.delete_webhook()
        worker = asyncio.create_task(scheduler.run(), name="sitewatch")
        try:
            await dispatcher.start_polling(
                bot, close_bot_session=False, db=db, config=config, scheduler=scheduler,
            )
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(main())
    except (ValueError, TokenValidationError) as exc:
        raise SystemExit(str(exc)) from None
    except KeyboardInterrupt:
        pass

