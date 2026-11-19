from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from bot.config import Config
from bot.database import Database, Monitor
from bot.keyboards import (
    PAGE_SIZE, STATUS, MonitorAction, MonitorPage, back_menu, monitor_card, monitor_list,
)
from bot.screens import show_screen
from bot.services.checker import BLOCKED_ADDRESS, parse_target
from bot.services.scheduler import Scheduler, metrics
from bot.states import AddMonitor, SetInterval

router = Router()
NAME_STEP = "Шаг 1/2. Введите короткое название проекта (например: Мой магазин)"
URL_STEP = "Шаг 2/2. Введите полный URL (например: https://example.com)"


def summary(monitor: Monitor) -> str:
    paused = " · на паузе" if not monitor.is_active else ""
    pending = ""
    if monitor.fail_count and monitor.last_status != "down":
        pending = f" · ошибок подряд: {monitor.fail_count}"
    return (
        f"{STATUS[monitor.last_status]} {monitor.name}{paused}{pending}\n"
        f"{metrics(monitor.last_code, monitor.last_latency_ms, monitor.ssl_days_left)}"
    )


async def show_card(
    message: Message | InaccessibleMessage, monitor: Monitor, config: Config,
    extra: str = "", *, screen_id: int | None = None,
) -> None:
    interval = monitor.check_interval or config.check_interval
    await show_screen(
        message, f"{summary(monitor)}\n{monitor.url}\nИнтервал проверки: {interval} с{extra}",
        monitor_card(monitor), screen_id=screen_id,
    )


async def show_form(message: Message, state: FSMContext, text: str) -> int:
    screen_id = (await state.get_data())["screen_id"]
    updated_id = await show_screen(message, text, back_menu(), screen_id=screen_id)
    if updated_id != screen_id:
        await state.update_data(screen_id=updated_id)
    return updated_id


async def show_history(message: Message | InaccessibleMessage, monitor: Monitor, db: Database) -> None:
    incidents = await db.recent_incidents(monitor.id, monitor.user_id)
    entries = []
    for incident in incidents:
        timestamp = datetime.fromisoformat(incident.created_at).strftime("%Y-%m-%d %H:%M:%S UTC")
        label = "🔴 Сбой" if incident.event_type == "down" else "🟢 Восстановление"
        entries.append(f"{label} · {timestamp}\n{incident.details}")
    history = "\n\n".join(entries) or "Сбоев и восстановлений пока нет."
    await show_screen(message, f"История сбоев · {monitor.name}\n\n{history}", monitor_card(monitor))


@router.callback_query(F.data == "add")
async def begin_add(query: CallbackQuery, state: FSMContext, db: Database, config: Config) -> None:
    await query.answer()
    await state.clear()
    if query.message is None:
        return
    if len(await db.list_for_user(query.from_user.id)) >= config.max_sites_per_user:
        await show_screen(query.message, f"Достигнут лимит сайтов: {config.max_sites_per_user}.", back_menu())
        return
    screen_id = await show_screen(query.message, NAME_STEP, back_menu())
    await state.set_data({"screen_id": screen_id})
    await state.set_state(AddMonitor.name)


@router.message(AddMonitor.name)
async def add_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if name.lower().startswith(("http://", "https://")):
        await show_form(message, state, f"{NAME_STEP}\n\nВведите текстовое название проекта, а не ссылку.")
        return
    if not 1 <= len(name) <= 60 or not name.isprintable():
        await show_form(message, state, f"{NAME_STEP}\n\nНазвание должно быть одной строкой, от 1 до 60 символов.")
        return
    await show_form(message, state, URL_STEP)
    await state.update_data(name=name)
    await state.set_state(AddMonitor.url)


@router.message(AddMonitor.url)
async def add_url(message: Message, state: FSMContext, config: Config, scheduler: Scheduler) -> None:
    try:
        raw_url = (message.text or "").strip()
        if len(raw_url) > 1024:
            raise ValueError("URL должен содержать не более 1024 символов.")
        url = str(parse_target(raw_url))
        if len(url) > 1024:
            raise ValueError("URL слишком длинный после кодирования.")
    except ValueError as exc:
        await show_form(message, state, f"{URL_STEP}\n\n{exc}")
        return
    if message.from_user is None:
        return
    user_id = message.from_user.id
    name = (await state.get_data())["name"]
    screen_id = await show_form(message, state, "Выполняю первую проверку…")
    result = await scheduler.probe(url)
    if result.error == BLOCKED_ADDRESS:
        await show_form(message, state, f"{URL_STEP}\n\n{BLOCKED_ADDRESS}")
        return
    monitor = await scheduler.add_monitor(user_id, name, url, result)
    await state.clear()
    if monitor is None:
        await show_screen(
            message, f"Достигнут лимит сайтов: {config.max_sites_per_user}.", back_menu(), screen_id=screen_id,
        )
        return
    extra = f"\n{result.error}" if result.error else ""
    await show_card(message, monitor, config, extra, screen_id=screen_id)


@router.callback_query(MonitorPage.filter())
async def show_list(query: CallbackQuery, callback_data: MonitorPage, state: FSMContext, db: Database) -> None:
    await query.answer()
    await state.clear()
    if query.message is None:
        return
    monitors = await db.list_for_user(query.from_user.id)
    if not monitors:
        await show_screen(query.message, "Список сайтов пуст.", back_menu())
        return
    pages = (len(monitors) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(max(callback_data.number, 0), pages - 1)
    visible = monitors[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = "\n\n".join(map(summary, visible))
    await show_screen(query.message, text, monitor_list(visible, page, pages))


@router.callback_query(F.data == "check_all")
async def check_all(query: CallbackQuery, state: FSMContext, db: Database, scheduler: Scheduler) -> None:
    await query.answer("Проверяю...")
    await state.clear()
    if query.message is None:
        return
    monitors = await db.list_for_user(query.from_user.id)
    if not monitors:
        await show_screen(query.message, "Список сайтов пуст.", back_menu())
        return
    results = await scheduler.check_many(monitors)
    up = sum(result.status == "up" for result in results)
    await show_screen(
        query.message,
        f"Проверено: {len(results)} из {len(monitors)}\n"
        f"🟢 Доступны: {up}\n🔴 Недоступны: {len(results) - up}",
        back_menu(),
    )


@router.callback_query(MonitorAction.filter())
async def manage(
    query: CallbackQuery, callback_data: MonitorAction, state: FSMContext,
    db: Database, config: Config, scheduler: Scheduler,
) -> None:
    action = callback_data.action
    await query.answer("Проверяю..." if action == "check" else None)
    await state.clear()
    message = query.message
    if message is None:
        return
    monitor_id, user_id = callback_data.monitor_id, query.from_user.id
    monitor = await db.get(monitor_id, user_id)
    if monitor is None:
        await show_screen(message, "Сайт не найден.", back_menu())
        return

    if action == "history":
        await show_history(message, monitor, db)
        return
    if action == "interval":
        screen_id = await show_screen(
            message,
            f"Введите интервал в секундах (1–86400) или 0 для общего интервала ({config.check_interval} с).",
            back_menu(),
        )
        await state.set_data({"monitor_id": monitor_id, "screen_id": screen_id})
        await state.set_state(SetInterval.seconds)
        return

    extra = ""
    if action == "check":
        result = await scheduler.check(monitor_id, user_id)
        if result and result.error:
            extra = f"\n{result.error}"
    elif action in {"toggle", "delete"}:
        async with scheduler.lock(monitor_id):
            if action == "toggle":
                await db.toggle(monitor_id, user_id)
            else:
                await db.delete(monitor_id, user_id)
        scheduler.wake.set()
        if action == "delete":
            await show_screen(message, "Сайт удалён.", back_menu())
            return
    elif action != "open":
        return

    monitor = await db.get(monitor_id, user_id)
    if monitor:
        await show_card(message, monitor, config, extra)
    else:
        await show_screen(message, "Сайт не найден.", back_menu())


@router.message(SetInterval.seconds)
async def set_interval(message: Message, state: FSMContext, db: Database, config: Config, scheduler: Scheduler) -> None:
    value = (message.text or "").strip()
    if not (value.isascii() and value.isdecimal() and len(value) <= 5 and int(value) <= 86400):
        await show_form(message, state, "Введите целое число от 0 до 86400 секунд. 0 возвращает общий интервал.")
        return
    if message.from_user is None:
        return
    data = await state.get_data()
    monitor_id = data["monitor_id"]
    screen_id = data["screen_id"]
    user_id = message.from_user.id
    async with scheduler.lock(monitor_id):
        await db.set_interval(monitor_id, user_id, int(value) or None)
        monitor = await db.get(monitor_id, user_id)
    await state.clear()
    scheduler.wake.set()
    if monitor:
        await show_card(message, monitor, config, screen_id=screen_id)
    else:
        await show_screen(message, "Сайт не найден.", back_menu(), screen_id=screen_id)
