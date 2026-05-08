from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from smart_badge_api.core.config import get_settings

_settings = get_settings()
_engine_options = {"echo": _settings.database_echo}
if not _settings.database_url.startswith("sqlite"):
    _engine_options.update(
        {
            "pool_size": _settings.database_pool_size,
            "max_overflow": _settings.database_max_overflow,
            "pool_timeout": _settings.database_pool_timeout_seconds,
            "pool_recycle": _settings.database_pool_recycle_seconds,
            "pool_pre_ping": _settings.database_pool_pre_ping,
        }
    )

_engine = create_async_engine(_settings.database_url, **_engine_options)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory() as session:
        yield session
