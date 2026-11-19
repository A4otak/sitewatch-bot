from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import back_menu, main_menu
from bot.screens import show_screen

router = Router()
WELCOME = "SiteWatch · мониторинг сайтов и API"
HELP = (
    "Добавьте публичный URL сайта или API. SiteWatch проверит его сразу и продолжит "
    "проверять активные мониторы в фоне.\n\n"
    "Откройте «Мои сайты», чтобы проверить адрес вручную, поставить монитор на паузу, "
    "изменить интервал, посмотреть историю сбоев или удалить его. «Проверить все сейчас» проверяет также мониторы на паузе.\n\n"
    "Уведомления приходят при сбое, восстановлении и приближении срока истечения TLS-сертификата.\n"
    "/cancel — отменить ввод и вернуться в меню."
)


@router.message(CommandStart())
@router.message(Command("cancel"))
async def start(message: Message, state: FSMContext) -> None:
    screen_id = (await state.get_data()).get("screen_id")
    await state.clear()
    if screen_id is None:
        await message.answer(WELCOME, reply_markup=main_menu())
    else:
        await show_screen(message, WELCOME, main_menu(), screen_id=screen_id)


@router.message(Command("help"))
async def help_command(message: Message, state: FSMContext) -> None:
    screen_id = (await state.get_data()).get("screen_id")
    await state.clear()
    if screen_id is None:
        await message.answer(HELP, reply_markup=back_menu())
    else:
        await show_screen(message, HELP, back_menu(), screen_id=screen_id)


@router.callback_query(F.data.in_({"menu", "help"}))
async def navigate(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    if query.message is None:
        return
    if query.data == "help":
        await show_screen(query.message, HELP, back_menu())
    else:
        await show_screen(query.message, WELCOME, main_menu())
