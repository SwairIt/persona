"""Infrastructure adapters for autowake."""

from app.adapters.autowake.sqlite_repository import SqliteAutowakeRepository
from app.adapters.autowake.telegram_gateway import TelegramOwnerGateway

__all__ = ["SqliteAutowakeRepository", "TelegramOwnerGateway"]
