"""Проверка «тени» ботов.

Приоритет: юзер-аккаунт через Telethon (`shadow_user`), т.к. он видит ту же
поисковую выдачу, что и клиент. Если Telethon-сессия не настроена — падаем
обратно на эвристику по HTML-странице t.me/<username>.
"""
from __future__ import annotations
import asyncio
import logging
import re

import aiohttp

import shadow_user

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Маркеры «живой» превью-страницы t.me
_MARK_TITLE = "tgme_page_title"
_MARK_PHOTO = "tgme_page_photo_image"
_MARK_EXTRA = "tgme_page_extra"
_MARK_ACTION = "tgme_action_button_new"

_TITLE_RE = re.compile(
    r'<div class="tgme_page_title"[^>]*>\s*<span[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)


def parse_usernames(raw: str) -> list[str]:
    """Разбирает произвольный ввод: пробелы, запятые, переносы строк, с @ или без."""
    if not raw:
        return []
    cleaned = raw.replace(",", " ").replace(";", " ").replace("\n", " ")
    parts = [p.strip().lstrip("@") for p in cleaned.split() if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        low = p.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(p)
    return out


def _tushka(username: str) -> str:
    """Отрезаем bot/_bot суффикс — «тушка» юза."""
    low = username.lower()
    if low.endswith("_bot"):
        return username[:-4]
    if low.endswith("bot"):
        return username[:-3]
    return username


async def _fetch(session: aiohttp.ClientSession, url: str) -> tuple[int, str]:
    async with session.get(url, allow_redirects=True) as resp:
        return resp.status, await resp.text(errors="ignore")


async def _check_one(session: aiohttp.ClientSession, username: str) -> tuple[str, bool, str]:
    """Возвращает (username, has_shadow, note)."""
    handle = username.lstrip("@")
    url = f"https://t.me/{handle}"
    try:
        status, html = await _fetch(session, url)
    except Exception as e:
        return (handle, True, f"ошибка сети: {e}")

    if status != 200:
        return (handle, True, f"http {status}")

    has_title = _MARK_TITLE in html
    has_photo = _MARK_PHOTO in html
    has_extra = _MARK_EXTRA in html
    has_action = _MARK_ACTION in html

    if not has_title:
        # generic «If you have Telegram…» — юз не найден или в глубокой тени
        return (handle, True, "нет превью (тень / не существует)")

    # Название есть — но нет аватара и extra: сильный сигнал ограничения
    if not has_photo and not has_extra:
        return (handle, True, "нет аватара и статистики (тень)")

    # Аватара нет, но extra есть — подозрительно
    if not has_photo:
        return (handle, True, "нет аватара (тень)")

    # Иначе — видимо, всё ок
    m = _TITLE_RE.search(html)
    title = m.group(1).strip() if m else handle
    tail = "" if has_action else ", нет кнопки"
    return (handle, False, f"виден в превью → {title}{tail}")


async def check_usernames(usernames: list[str], admin_id: int | None = None) -> list[tuple[str, bool, str]]:
    if not usernames:
        return []
    # Приоритет: юзер-аккаунт (реальный поиск).
    if await shadow_user.session_ready(admin_id):
        try:
            return await shadow_user.check_usernames(usernames, admin_id=admin_id)
        except Exception as e:
            log.warning("shadow_user failed, fallback to t.me heuristic: %s", e)
    # Fallback — HTML-эвристика по t.me.
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    async with aiohttp.ClientSession(headers=headers, timeout=_TIMEOUT) as session:
        results: list[tuple[str, bool, str]] = []
        for u in usernames:
            res = await _check_one(session, u)
            results.append(res)
            await asyncio.sleep(0.15)
    return results


async def check_bot_tushkas(usernames: list[str]) -> list[tuple[str, bool, str]]:
    """Для каждого юза бота проверяет его «тушку» (без bot/_bot суффикса)."""
    tushkas: list[str] = []
    seen: set[str] = set()
    for u in usernames:
        t = _tushka(u.lstrip("@"))
        if not t:
            continue
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        tushkas.append(t)
    return await check_usernames(tushkas)


def format_report(results: list[tuple[str, bool, str]]) -> str:
    if not results:
        return "Нечего проверять."
    lines = []
    shadow = 0
    for u, has_shadow, note in results:
        icon = "🌑" if has_shadow else "✅"
        if has_shadow:
            shadow += 1
        lines.append(f"{icon} <code>{u}</code> — {note}")
    header = f"<b>Проверка на тень</b>: {len(results)} юзов, в тени: {shadow}\n"
    return header + "\n".join(lines)
