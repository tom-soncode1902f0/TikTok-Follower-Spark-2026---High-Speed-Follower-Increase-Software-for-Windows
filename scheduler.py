"""Обёртка над APScheduler: авторассылка + health-check + one-shot broadcast."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramAPIError,
)

import config
import database
import worker_manager

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def start() -> None:
    s = get_scheduler()
    if not s.running:
        s.start()


def shutdown() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


# ---------- health check ----------

def register_healthcheck() -> None:
    get_scheduler().add_job(
        worker_manager.health_check_all,
        trigger=IntervalTrigger(minutes=config.HEALTHCHECK_INTERVAL_MIN),
        id="healthcheck",
        replace_existing=True,
        max_instances=1,
    )
    import recovery
    get_scheduler().add_job(recovery.check_pending, trigger=IntervalTrigger(minutes=1), id="pending-shadow-check", replace_existing=True, max_instances=1)
    get_scheduler().add_job(recovery.check_active_shadows, trigger=IntervalTrigger(minutes=max(5, config.HEALTHCHECK_INTERVAL_MIN)), id="active-shadow-check", replace_existing=True, max_instances=1)


# ---------- helpers ----------

def _build_kb(button_text: str | None, button_url: str | None) -> InlineKeyboardMarkup | None:
    if button_text and button_url:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
        )
    return None


# ---------- mailing ----------

def _mailing_job_id(bot_id: int) -> str:
    return f"mailing-{bot_id}"


async def _do_mailing(bot_id: int) -> None:
    bot = worker_manager.get_bot_instance(bot_id)
    if not bot:
        log.info("mailing skip: bot_id=%s not running", bot_id)
        return
    bot_row = await database.get_bot(bot_id)
    if not bot_row or not bot_row["is_alive"]:
        log.info("mailing skip: bot_id=%s dead/missing", bot_id)
        return
    template = await database.get_template(bot_row["template_id"])
    if not template or not template["mailing_enabled"]:
        log.info("mailing skip: bot_id=%s no template/disabled", bot_id)
        return
    text = template["mailing_text"]
    photo = template["mailing_photo_path"]
    users = await database.get_bot_users(bot_id)
    log.info("mailing fire: bot_id=%s users=%s", bot_id, len(users))
    sent = failed = removed = 0
    for u in users:
        try:
            if photo and Path(photo).exists():
                await bot.send_photo(u["tg_user_id"], FSInputFile(photo), caption=text or None)
            else:
                await bot.send_message(u["tg_user_id"], text or "🔔")
            sent += 1
        except TelegramForbiddenError:
            # заблокировали / удалили — вычищаем из базы
            await database.delete_bot_user(bot_id, u["tg_user_id"])
            removed += 1
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "chat not found" in msg or "user is deactivated" in msg:
                await database.delete_bot_user(bot_id, u["tg_user_id"])
                removed += 1
            else:
                failed += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            failed += 1
        except TelegramAPIError as e:
            log.info("mailing send failed for %s: %s", u["tg_user_id"], e)
            failed += 1
        await asyncio.sleep(0.05)
    log.info("mailing done: bot_id=%s sent=%s failed=%s removed=%s", bot_id, sent, failed, removed)


def add_mailing_job(bot_id: int, interval_minutes: int) -> None:
    # первый прогон — через interval, не сразу (даём боту разогреться)
    next_run = datetime.now() + timedelta(minutes=interval_minutes)
    get_scheduler().add_job(
        _do_mailing,
        trigger=IntervalTrigger(minutes=interval_minutes),
        args=[bot_id],
        id=_mailing_job_id(bot_id),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=next_run,
    )
    log.info("mailing scheduled: bot_id=%s interval=%s min, next=%s",
             bot_id, interval_minutes, next_run)


def remove_mailing_job(bot_id: int) -> None:
    s = get_scheduler()
    job_id = _mailing_job_id(bot_id)
    if s.get_job(job_id):
        s.remove_job(job_id)


async def register_all_mailings() -> None:
    bots = await database.list_bots(alive_only=True)
    for b in bots:
        t = await database.get_template(b["template_id"])
        if t and t["mailing_enabled"] and t["mailing_interval_minutes"]:
            add_mailing_job(b["id"], int(t["mailing_interval_minutes"]))


# ---------- one-shot broadcast (per-admin) ----------

async def broadcast_to_admin(
    admin_id: int,
    text: str | None,
    photo_path: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
) -> tuple[int, int, int]:
    """Разослать сообщение всем юзерам во всех живых воркерах, принадлежащих admin_id.
    Поддерживает фото и inline-кнопку. Пользователей, которые нас заблокировали
    или удалили, вычищаем из базы. Возвращает (sent, failed, removed)."""
    sent = failed = removed = 0
    kb = _build_kb(button_text, button_url)
    bots = await database.list_bots(owner_admin_id=admin_id, alive_only=True)
    for b in bots:
        bot = worker_manager.get_bot_instance(b["id"])
        if not bot:
            continue
        users = await database.get_bot_users(b["id"])
        for u in users:
            try:
                if photo_path and Path(photo_path).exists():
                    await bot.send_photo(
                        u["tg_user_id"],
                        FSInputFile(photo_path),
                        caption=text or None,
                        reply_markup=kb,
                    )
                else:
                    await bot.send_message(
                        u["tg_user_id"],
                        text or "🔔",
                        reply_markup=kb,
                    )
                sent += 1
            except TelegramForbiddenError:
                await database.delete_bot_user(b["id"], u["tg_user_id"])
                removed += 1
            except TelegramBadRequest as e:
                msg = str(e).lower()
                if "chat not found" in msg or "user is deactivated" in msg:
                    await database.delete_bot_user(b["id"], u["tg_user_id"])
                    removed += 1
                else:
                    failed += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                failed += 1
            except TelegramAPIError:
                failed += 1
            await asyncio.sleep(0.05)
    return sent, failed, removed
