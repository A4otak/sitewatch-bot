import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.config import Config
from bot.database import Database, Monitor
from bot.services.checker import CheckResult, check_target

logger = logging.getLogger(__name__)


def metrics(code: int | None, latency: int | None, ssl_days: int | None) -> str:
    http = code if code is not None else "—"
    ping = f"{latency} мс" if latency is not None else "—"
    ssl = f"{ssl_days} дн." if ssl_days is not None else "—"
    return f"HTTP {http} · отклик {ping} · SSL {ssl}"


def result_text(result: CheckResult) -> str:
    text = metrics(result.code, result.latency_ms, result.ssl_days_left)
    return f"{text}\n{result.error}" if result.error else text


class Scheduler:
    def __init__(self, bot: Bot, db: Database, config: Config):
        self.bot = bot
        self.db = db
        self.config = config
        self._slots = asyncio.Semaphore(12)
        self._locks = [asyncio.Lock() for _ in range(64)]
        self.wake = asyncio.Event()

    def lock(self, monitor_id: int) -> asyncio.Lock:
        return self._locks[monitor_id % len(self._locks)]

    async def probe(self, url: str) -> CheckResult:
        async with self._slots:
            return await check_target(url, self.config.request_timeout)

    async def _notify(self, monitor: Monitor, message: str) -> bool:
        try:
            await self.bot.send_message(monitor.user_id, message)
        except TelegramAPIError as exc:
            logger.warning("Уведомление для монитора %d не доставлено (%s)", monitor.id, type(exc).__name__)
            return False
        return True

    def _seconds_until_due(self, monitor: Monitor) -> float:
        if not monitor.last_checked_at:
            return 0
        timestamp = datetime.fromisoformat(monitor.last_checked_at)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        interval = monitor.check_interval or self.config.check_interval
        return min(interval, interval - age)

    async def check(self, monitor_id: int, user_id: int, *, background: bool = False) -> CheckResult | None:
        async with self.lock(monitor_id):
            monitor = await self.db.get(monitor_id, user_id)
            if monitor is None or not self.config.allows(user_id):
                return None
            if background and (not monitor.is_active or self._seconds_until_due(monitor) > 0):
                return None
            result = await self.probe(monitor.url)
            event_type = await self.db.save_result(
                monitor, result, self.config.fail_threshold, result_text(result),
            )
            if monitor.is_active:
                await self._send_alerts(monitor, result, event_type)
            return result

    async def add_monitor(self, user_id: int, name: str, url: str, result: CheckResult) -> Monitor | None:
        monitor_id = await self.db.add(
            user_id, name, url, result, self.config.max_sites_per_user,
            self.config.fail_threshold, result_text(result),
        )
        if monitor_id is None:
            return None
        async with self.lock(monitor_id):
            monitor = await self.db.get(monitor_id, user_id)
            if monitor:
                event_type = "down" if result.status == "down" and self.config.fail_threshold == 1 else None
                await self._send_alerts(monitor, result, event_type)
        self.wake.set()
        return monitor

    async def _send_alerts(self, monitor: Monitor, result: CheckResult, event_type: str | None) -> None:
        target = f"{monitor.name}\n{monitor.url}"
        if event_type == "down":
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            await self._notify(monitor, f"🔴 Сайт недоступен\n{target}\n{result_text(result)}\n{now}")
        elif event_type == "up":
            await self._notify(monitor, f"🟢 Сайт снова доступен\n{target}\n{result_text(result)}")
        if result.ssl_days_left is not None and result.ssl_days_left <= 7 and not monitor.ssl_warned:
            sent = await self._notify(monitor, f"⚠️ Истекает SSL-сертификат\n{target}\nОсталось дней: {result.ssl_days_left}")
            if sent:
                await self.db.mark_ssl_warned(monitor)

    async def check_many(self, monitors: list[Monitor], *, background: bool = False) -> list[CheckResult]:
        results = []
        for offset in range(0, len(monitors), 24):
            batch = monitors[offset:offset + 24]
            outcomes = await asyncio.gather(
                *(self.check(monitor.id, monitor.user_id, background=background) for monitor in batch),
                return_exceptions=True,
            )
            for monitor, outcome in zip(batch, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    logger.error("Ошибка проверки монитора %d", monitor.id, exc_info=outcome)
                elif outcome is not None:
                    results.append(outcome)
        return results

    async def run(self) -> None:
        while True:
            self.wake.clear()
            delay = self.config.check_interval
            try:
                active = [monitor for monitor in await self.db.list_active() if self.config.allows(monitor.user_id)]
                due = []
                for monitor in active:
                    remaining = self._seconds_until_due(monitor)
                    if remaining <= 0:
                        due.append(monitor)
                    else:
                        delay = min(delay, remaining)
                if due:
                    started = asyncio.get_running_loop().time()
                    await self.check_many(due, background=True)
                    delay -= asyncio.get_running_loop().time() - started
                    delay = min(delay, *(monitor.check_interval or self.config.check_interval for monitor in due))
            except Exception:
                logger.exception("Ошибка фонового планировщика")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(0.1, delay))
            except asyncio.TimeoutError:
                pass
