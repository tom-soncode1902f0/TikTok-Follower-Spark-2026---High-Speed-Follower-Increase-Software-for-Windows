"""One-time авторизация юзер-аккаунта для проверки на тень.

Запусти ОДИН раз в консоли:

    python setup_shadow_session.py

Скрипт спросит код из Telegram (и пароль 2FA, если включён) и создаст
файл сессии `<TG_SESSION_PATH>.session` рядом с проектом. После этого бот
будет использовать сессию сам, без ввода кода."""
from __future__ import annotations
import asyncio

import config


async def main() -> None:
    if not config.telethon_configured():
        raise SystemExit(
            "TG_API_ID / TG_API_HASH не заданы в .env. "
            "Получи их на https://my.telegram.org/apps"
        )
    if not config.TG_PHONE:
        raise SystemExit("TG_PHONE не задан в .env")

    try:
        from telethon import TelegramClient
    except ImportError:
        raise SystemExit("Установи telethon: pip install telethon")

    client = TelegramClient(
        config.TG_SESSION_PATH,
        config.TG_API_ID,
        config.TG_API_HASH,
    )
    await client.start(phone=config.TG_PHONE)
    me = await client.get_me()
    print(f"OK: session сохранена. Юзер: @{me.username or me.id} ({me.first_name})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
