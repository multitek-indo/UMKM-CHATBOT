import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot

TOKEN = os.environ["BOT_TOKEN"]
WORKER_DATABASE_URL = os.environ["WORKER_DATABASE_URL"]
JAKARTA = ZoneInfo("Asia/Jakarta")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reminder-worker")


async def run():
    bot = Bot(TOKEN)
    pool = await asyncpg.create_pool(
        WORKER_DATABASE_URL,
        min_size=1,
        max_size=2,
        command_timeout=15,
    )

    try:
        while True:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM worker_claim_due_reminders($1)",
                        50,
                    )

                for row in rows:
                    try:
                        local_due = row["due_at"].astimezone(JAKARTA)
                        await bot.send_message(
                            row["telegram_user_id"],
                            "🔔 <b>PENGINGAT</b>\n\n"
                            f"{row['message']}\n\n"
                            f"🗓 {local_due.strftime('%d-%m-%Y %H:%M')} WIB",
                            parse_mode="HTML",
                        )
                        async with pool.acquire() as conn:
                            await conn.execute(
                                "SELECT worker_mark_reminder_sent($1)",
                                row["reminder_id"],
                            )
                    except Exception:
                        log.exception(
                            "Gagal mengirim reminder %s",
                            row["reminder_id"],
                        )
            except Exception:
                log.exception("Worker iteration gagal")

            await asyncio.sleep(30)
    finally:
        await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
