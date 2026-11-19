from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database import Monitor

STATUS = {"up": "🟢", "down": "🔴", "unknown": "⚪"}
PAGE_SIZE = 5


class MonitorAction(CallbackData, prefix="site"):
    action: str
    monitor_id: int


class MonitorPage(CallbackData, prefix="page"):
    number: int


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Добавить сайт", "add"), button("Мои сайты", MonitorPage(number=0).pack())],
        [button("Проверить все сейчас", "check_all")],
        [button("Справка", "help")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("В главное меню", "menu")]])


def monitor_list(monitors: list[Monitor], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [
        [button(f"{STATUS[monitor.last_status]} {monitor.name}", MonitorAction(action="open", monitor_id=monitor.id).pack())]
        for monitor in monitors
    ]
    navigation = []
    if page > 0:
        navigation.append(button("← Назад", MonitorPage(number=page - 1).pack()))
    if page + 1 < pages:
        navigation.append(button("Далее →", MonitorPage(number=page + 1).pack()))
    if navigation:
        rows.append(navigation)
    rows.extend(back_menu().inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def monitor_card(monitor: Monitor) -> InlineKeyboardMarkup:
    def action(title: str, name: str) -> InlineKeyboardButton:
        return button(title, MonitorAction(action=name, monitor_id=monitor.id).pack())

    return InlineKeyboardMarkup(inline_keyboard=[
        [action("Проверить сейчас", "check"), action("Пауза" if monitor.is_active else "Включить", "toggle")],
        [action("Интервал проверки", "interval"), action("Удалить", "delete")],
        [action("История сбоев", "history")],
        [button("К списку сайтов", MonitorPage(number=0).pack())],
    ])
