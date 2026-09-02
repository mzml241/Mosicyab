#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""اجرای بات برای یک جلسه‌ی زمانی محدود — مناسب GitHub Actions.

هر اجرا SESSION_SECONDS ثانیه (پیش‌فرض ۲۷۰) پیام‌ها را پردازش می‌کند و سپس
تمیز خاموش می‌شود؛ cron ورک‌فلو هر ۵ دقیقه جلسه‌ی جدید را شروع می‌کند.
"""
from __future__ import annotations

import asyncio
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import bot as botmod


async def main() -> None:
    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("❌ BOT_TOKEN تنظیم نشده! (در Secrets ریپو اضافه‌اش کن)")
    seconds = float(os.environ.get("SESSION_SECONDS", "270"))

    b = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await b.get_me()
    print(f"🎵 {me.username} بالا آمد — جلسه‌ی {seconds:.0f} ثانیه‌ای شروع شد", flush=True)
    try:
        await asyncio.wait_for(
            botmod.dp.start_polling(b, polling_timeout=15),
            timeout=seconds,
        )
    except asyncio.TimeoutError:
        print("⏱ زمان جلسه تمام شد — خاموش شدن تمیز", flush=True)
    finally:
        botmod._save_store()  # دکمه‌ها برای جلسه‌ی بعد ذخیره شوند
        await b.session.close()


if __name__ == "__main__":
    asyncio.run(main())
