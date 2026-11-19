import os
from dataclasses import dataclass

from dotenv import load_dotenv


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value > 0:
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} должен быть целым положительным числом.")


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    check_interval: int
    request_timeout: int
    max_sites_per_user: int
    db_path: str
    fail_threshold: int = 2

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Укажите BOT_TOKEN в .env.")
        try:
            raw_ids = os.getenv("ADMIN_IDS", "").strip()
            admin_ids = frozenset(int(part.strip()) for part in raw_ids.split(",")) if raw_ids else frozenset()
            if any(user_id <= 0 for user_id in admin_ids):
                raise ValueError
        except ValueError:
            raise ValueError("ADMIN_IDS должен быть списком положительных Telegram ID через запятую.") from None
        db_path = os.getenv("DB_PATH", "sitewatch.db").strip()
        if not db_path:
            raise ValueError("DB_PATH не должен быть пустым.")
        return cls(
            token, admin_ids, positive_int("CHECK_INTERVAL", 60),
            positive_int("REQUEST_TIMEOUT", 10), positive_int("MAX_SITES_PER_USER", 20),
            db_path, positive_int("FAIL_THRESHOLD", 2),
        )

    def allows(self, user_id: int) -> bool:
        return not self.admin_ids or user_id in self.admin_ids
