"""Проверка «тени» через юзер-аккаунт (Telethon).

Идея: Bot API не отдаёт статус видимости в поиске. А юзер-клиент через
`contacts.Search` получает те же результаты, что показывает поисковая строка
Telegram. Если бот не появляется в выдаче по своей «тушке» (или даже по
полному юзу) — он в тени.

Дополнительно резолвим юз через `ResolveUsername`, чтобы отличить «не
существует» от «существует, но невидим в поиске».
"""
from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

import config

log = logging.getLogger(__name__)

_clients: dict[int, Any] = {}
_lock = asyncio.Lock()

SESSIONS_DIR = Path("sessions")


def _tushka(username: str) -> str:
    low = username.lower()
    if low.endswith("_bot"):
        return username[:-4]
    if low.endswith("bot"):
        return username[:-3]
    return username


def _fix_double_ext(path: Path) -> Path:
    """Если файл называется <name>.session.session — переименовать в <name>.session.
    Возвращает финальный путь."""
    if path.name.endswith(".session.session"):
        fixed = path.with_name(path.name[: -len(".session")])
        if fixed.exists():
            log.warning("нашёл и %s и %s — использую %s, лишний игнорирую", path, fixed, fixed)
            return fixed
        path.rename(fixed)
        log.info("переименовал %s → %s", path.name, fixed.name)
        return fixed
    return path


def _discover_session_file() -> Optional[Path]:
    """Ищет любой *.session файл, который можно использовать.

    Приоритет:
    1. Явный TG_SESSION_PATH (если файл существует)
    2. Любой *.session файл в папке sessions/
    3. Любой *.session файл в корне проекта

    Автоматически исправляет двойное расширение .session.session.
    """
    explicit = Path(f"{config.TG_SESSION_PATH}.session")
    if explicit.exists():
        return explicit
    explicit_dbl = Path(f"{config.TG_SESSION_PATH}.session.session")
    if explicit_dbl.exists():
        return _fix_double_ext(explicit_dbl)

    for folder in (SESSIONS_DIR, Path(".")):
        if not folder.exists():
            continue
        candidates = sorted(folder.glob("*.session*"))
        for c in candidates:
            if c.name.endswith(".session-journal"):
                continue
            if c.name.endswith(".session.session"):
                c = _fix_double_ext(c)
            if c.name.endswith(".session"):
                return c
    return None


def _session_arg_from_file(path: Path) -> str:
    """Telethon ждёт путь БЕЗ расширения .session — оно добавляется автоматом."""
    return str(path.with_suffix(""))


async def session_ready(admin_id: int | None = None) -> bool:
    """Есть ли готовая сессия (файл или StringSession) + креды."""
    if not config.telethon_configured():
        return False
    if admin_id is not None:
        import database
        profile = await database.get_admin_profile(admin_id)
        path = profile.get("shadow_session_path")
        return bool(path and Path(path).exists())
    if config.TG_STRING_SESSION:
        return True
    return _discover_session_file() is not None


async def _get_client(admin_id: int | None = None):
    """Ленивая инициализация клиента. Использует уже сохранённую сессию —
    интерактивного логина при работе бота не будет."""
    key = admin_id or 0
    if key in _clients:
        return _clients[key]
    async with _lock:
        if key in _clients:
            return _clients[key]
        if not await session_ready(admin_id):
            raise RuntimeError(
                "Telethon-сессия не готова. Положи любой .session файл в папку "
                "`sessions/` (имя не важно), или задай TG_STRING_SESSION в .env, "
                "или запусти `python setup_shadow_session.py`."
            )
        from telethon import TelegramClient
        if admin_id is not None:
            import database
            profile = await database.get_admin_profile(admin_id)
            found = Path(profile["shadow_session_path"])
            session = _session_arg_from_file(found)
            source = f"admin:{admin_id}:{found}"
        elif config.TG_STRING_SESSION:
            from telethon.sessions import StringSession
            session = StringSession(config.TG_STRING_SESSION)
            source = "StringSession"
        else:
            found = _discover_session_file()
            session = _session_arg_from_file(found)
            source = f"file:{found}"
        client = TelegramClient(
            session,
            config.TG_API_ID,
            config.TG_API_HASH,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                "Telethon-сессия не авторизована. Файл сессии повреждён, "
                "устарел или был разлогинен в Telegram."
            )
        _clients[key] = client
        log.info("Telethon client connected (source=%s)", source)
        return client


async def shutdown() -> None:
    for client in list(_clients.values()):
        try:
            await client.disconnect()
        except Exception:
            pass
    _clients.clear()


async def _resolve_username(client, handle: str) -> bool:
    """True если юз кем-то занят и резолвится."""
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError
    try:
        await client(ResolveUsernameRequest(username=handle))
        return True
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return False
    except Exception as e:
        log.info("resolve %s: %s", handle, e)
        return False


async def _search_hits(client, query: str) -> tuple[list[str], list[str]]:
    """Возвращает (usernames_из_моих_контактов_и_чатов, usernames_из_global).
    Юзы приведены к lowercase без @."""
    from telethon.tl.functions.contacts import SearchRequest
    try:
        res = await client(SearchRequest(q=query, limit=50))
    except Exception as e:
        log.info("search %s: %s", query, e)
        return [], []

    my: list[str] = []
    globs: list[str] = []
    for u in getattr(res, "users", []) or []:
        uname = (getattr(u, "username", "") or "").lower()
        if uname:
            my.append(uname)
    for u in getattr(res, "results", []) or []:
        # peers, not full users — try username via chats
        pass
    # global users list в SearchRequest — res.users содержит все
    # (мои + глобальные), отдельного разделения нет по умолчанию —
    # используем один общий список.
    return my, globs


async def _check_one(client, username: str) -> tuple[str, bool, str]:
    """Возвращает (username, has_shadow, note)."""
    handle = username.lstrip("@")
    handle_low = handle.lower()
    tush = _tushka(handle)
    tush_low = tush.lower()

    resolved = await _resolve_username(client, handle)
    if not resolved:
        return (handle, True, "юз не занят / не резолвится")

    # Ищем по полному юзу — если даже полный юз не в поиске, это тень.
    full_hits, _ = await _search_hits(client, handle)
    seen_full = handle_low in full_hits

    # Ищем по «тушке» — по требованию юзера, бот должен быть виден по тушке.
    tush_hits, _ = await _search_hits(client, tush) if tush_low != handle_low else (full_hits, [])
    seen_tush = handle_low in tush_hits

    if not seen_full and not seen_tush:
        return (handle, True, "не в поиске (тень)")
    if seen_full and not seen_tush:
        return (handle, True, f"найден по полному юзу, по тушке «{tush}» — нет")
    if not seen_full and seen_tush:
        return (handle, False, f"найден по тушке «{tush}»")
    return (handle, False, f"найден по тушке «{tush}» и по полному юзу")


async def check_usernames(usernames: list[str], admin_id: int | None = None) -> list[tuple[str, bool, str]]:
    if not usernames:
        return []
    client = await _get_client(admin_id)
    results: list[tuple[str, bool, str]] = []
    for u in usernames:
        try:
            res = await _check_one(client, u)
        except Exception as e:
            res = (u.lstrip("@"), True, f"ошибка: {e}")
        results.append(res)
        await asyncio.sleep(0.4)  # мягкий rate-limit
    return results
