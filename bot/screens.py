from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InaccessibleMessage, InlineKeyboardMarkup, Message


async def show_screen(
    message: Message | InaccessibleMessage,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    *,
    screen_id: int | None = None,
) -> int:
    """Update a bot screen and return its ID, including after a fallback."""
    target_id = screen_id if screen_id is not None else message.message_id
    try:
        if isinstance(message, Message) and screen_id is None:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.bot.edit_message_text(
                text, chat_id=message.chat.id, message_id=target_id, reply_markup=reply_markup,
            )
        return target_id
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return target_id
    except TelegramNotFound:
        pass

    if isinstance(message, Message):
        sent = await message.answer(text, reply_markup=reply_markup)
    else:
        sent = await message.bot.send_message(message.chat.id, text, reply_markup=reply_markup)
    return sent.message_id
