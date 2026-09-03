import os
import json
import hashlib
from contextlib import asynccontextmanager
from datetime import date

import asyncpg
from bridge_app.permissions import has_permission, require_permission

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "umkm-bot-db-1"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_APP_USER"],
        password=os.environ["POSTGRES_APP_PASSWORD"],
        min_size=1,
        max_size=8,
        command_timeout=20,
    )


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool belum diinisialisasi")
    return _pool


async def _set_tenant_context(conn: asyncpg.Connection, tenant_id):
    await conn.execute(
        "SELECT set_config('app.tenant_id', $1, true)",
        str(tenant_id),
    )


async def _set_actor_context(conn: asyncpg.Connection, ctx: dict):
    await conn.execute(
        "SELECT set_config('app.user_id',$1,true), set_config('app.role',$2,true)",
        str(ctx["user_id"]), str(ctx.get("role") or ""),
    )


def _phone_from_jid(jid: str) -> str:
    return (jid or "").split("@", 1)[0]


async def ensure_whatsapp_user(
    whatsapp_jid: str,
    whatsapp_lid: str | None,
    display_name: str | None,
):
    phone = _phone_from_jid(whatsapp_jid)
    label = (display_name or phone or "WhatsApp User").strip()[:120]
    pending_name = f"WA: {label} ({phone})"[:240]

    async with pool().acquire() as conn:
        async with conn.transaction():
            # Hindari dua webhook pertama membuat dua user pada saat bersamaan.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                whatsapp_jid,
            )

            row = await conn.fetchrow(
                """
                SELECT
                    wi.id AS identity_id,
                    wi.user_id,
                    wi.master_notified_at,
                    u.access_status,
                    u.active_tenant_id,
                    u.deleted_at,
                    u.full_name,
                    t.business_name
                FROM whatsapp_identities wi
                JOIN users u ON u.id = wi.user_id
                LEFT JOIN tenants t ON t.id = u.active_tenant_id
                WHERE wi.whatsapp_jid = $1
                  AND wi.is_active = TRUE
                """,
                whatsapp_jid,
            )

            if not row:
                user = await conn.fetchrow(
                    """
                    INSERT INTO users (
                        telegram_user_id,
                        full_name,
                        access_status,
                        is_master,
                        approved_at,
                        deleted_at
                    )
                    VALUES (NULL, $1, 'pending', FALSE, NULL, NULL)
                    RETURNING id
                    """,
                    pending_name,
                )

                await conn.execute(
                    """
                    INSERT INTO whatsapp_identities (
                        user_id,
                        whatsapp_jid,
                        whatsapp_lid,
                        phone_number,
                        display_name,
                        is_active,
                        first_seen_at,
                        last_seen_at
                    )
                    VALUES ($1,$2,$3,$4,$5,TRUE,NOW(),NOW())
                    """,
                    user["id"],
                    whatsapp_jid,
                    whatsapp_lid,
                    phone,
                    label,
                )
            else:
                await conn.execute(
                    """
                    UPDATE whatsapp_identities
                    SET
                        whatsapp_lid = COALESCE($2, whatsapp_lid),
                        display_name = COALESCE(NULLIF($3, ''), display_name),
                        last_seen_at = NOW()
                    WHERE whatsapp_jid = $1
                    """,
                    whatsapp_jid,
                    whatsapp_lid,
                    label,
                )

                # Saat masih pending, update nama agar MASTER mudah mengenali nomor WA.
                if row["access_status"] == "pending":
                    await conn.execute(
                        """
                        UPDATE users
                        SET full_name = $2, updated_at = NOW()
                        WHERE id = $1
                        """,
                        row["user_id"],
                        pending_name,
                    )

            return await conn.fetchrow(
                """
                SELECT
                    wi.id AS identity_id,
                    wi.user_id,
                    wi.whatsapp_jid,
                    wi.phone_number,
                    wi.display_name,
                    wi.master_notified_at,
                    u.access_status,
                    u.active_tenant_id,
                    u.deleted_at,
                    u.full_name,
                    t.business_name
                FROM whatsapp_identities wi
                JOIN users u ON u.id = wi.user_id
                LEFT JOIN tenants t ON t.id = u.active_tenant_id
                WHERE wi.whatsapp_jid = $1
                  AND wi.is_active = TRUE
                """,
                whatsapp_jid,
            )


async def mark_master_notified(whatsapp_jid: str):
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE whatsapp_identities
            SET master_notified_at = COALESCE(master_notified_at, NOW())
            WHERE whatsapp_jid = $1
            """,
            whatsapp_jid,
        )


async def get_whatsapp_access(whatsapp_jid: str):
    async with pool().acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                wi.user_id,
                wi.whatsapp_jid,
                wi.phone_number,
                wi.display_name,
                u.access_status,
                u.active_tenant_id AS tenant_id,
                u.deleted_at,
                u.full_name,
                t.business_name,
                tm.role
            FROM whatsapp_identities wi
            JOIN users u ON u.id = wi.user_id
            LEFT JOIN tenants t
              ON t.id = u.active_tenant_id
            LEFT JOIN tenant_members tm
              ON tm.user_id = u.id
             AND tm.tenant_id = u.active_tenant_id
             AND tm.is_active = TRUE
            WHERE wi.whatsapp_jid = $1
              AND wi.is_active = TRUE
            """,
            whatsapp_jid,
        )


@asynccontextmanager
async def tenant_tx(whatsapp_jid: str):
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT
                    wi.user_id,
                    wi.whatsapp_jid,
                    wi.phone_number,
                    u.active_tenant_id AS tenant_id,
                    u.access_status,
                    u.full_name,
                    t.business_name,
                    tm.role
                FROM whatsapp_identities wi
                JOIN users u
                  ON u.id = wi.user_id
                 AND u.access_status = 'active'
                 AND u.deleted_at IS NULL
                 AND u.is_master = FALSE
                JOIN tenants t
                  ON t.id = u.active_tenant_id
                 AND t.is_active = TRUE
                JOIN tenant_members tm
                  ON tm.user_id = u.id
                 AND tm.tenant_id = u.active_tenant_id
                 AND tm.is_active = TRUE
                WHERE wi.whatsapp_jid = $1
                  AND wi.is_active = TRUE
                """,
                whatsapp_jid,
            )
            if not row:
                raise LookupError("NO_ACTIVE_TENANT")
            await _set_tenant_context(conn, row["tenant_id"])
            ctx = dict(row)
            await _set_actor_context(conn, ctx)
            yield conn, ctx


async def create_transaction(
    whatsapp_jid: str,
    tx_type: str,
    amount: int,
    description: str,
    category: str = "Umum",
):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        require_permission(ctx, "finance.write")
        row = await conn.fetchrow(
            """
            INSERT INTO transactions
                (tenant_id, created_by, type, amount, category, description)
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING id, type, amount, category, description, transaction_date
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            tx_type,
            amount,
            category,
            description,
        )
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id, user_id, action, object_type, object_id, new_value
            )
            VALUES (
                $1,$2,'CREATE','transaction',$3,
                jsonb_build_object(
                    'channel','whatsapp',
                    'type',$4::text,
                    'amount',$5::numeric,
                    'category',$6::text,
                    'description',$7::text
                )
            )
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            row["id"],
            tx_type,
            amount,
            category,
            description,
        )
        return row


async def monthly_summary(whatsapp_jid: str):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        require_permission(ctx, "finance.read")
        rows = await conn.fetch(
            """
            SELECT type, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE tenant_id = $1
              AND status = 'posted'
              AND transaction_date >= date_trunc('month', CURRENT_DATE)::date
              AND transaction_date < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date
            GROUP BY type
            """,
            ctx["tenant_id"],
        )
        totals = {"income": 0, "expense": 0}
        for row in rows:
            totals[row["type"]] = int(row["total"])
        return ctx, totals


async def ai_finance_context(whatsapp_jid: str, limit: int = 12):
    limit = max(1, min(int(limit), 25))
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        require_permission(ctx, "finance.read")
        totals = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(amount) FILTER (
                    WHERE type = 'income' AND status = 'posted'
                ), 0) AS income,
                COALESCE(SUM(amount) FILTER (
                    WHERE type = 'expense' AND status = 'posted'
                ), 0) AS expense
            FROM transactions
            WHERE tenant_id = $1
              AND transaction_date >= date_trunc('month', CURRENT_DATE)::date
              AND transaction_date < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date
            """,
            ctx["tenant_id"],
        )
        recent = await conn.fetch(
            """
            SELECT type, amount, category, description, transaction_date
            FROM transactions
            WHERE tenant_id = $1
              AND status = 'posted'
            ORDER BY transaction_date DESC, created_at DESC
            LIMIT $2
            """,
            ctx["tenant_id"],
            limit,
        )

        reminders = await conn.fetch(
            """
            SELECT
                id, message, due_at, recurrence_type, recurrence_interval,
                recurrence_day, recurrence_weekday, recurrence_time
            FROM reminders
            WHERE tenant_id = $1
              AND status = 'pending'
            ORDER BY due_at ASC
            LIMIT 10
            """,
            ctx["tenant_id"],
        )

        income = int(totals["income"] or 0)
        expense = int(totals["expense"] or 0)

        return {
            "business_name": ctx["business_name"],
            "role": ctx["role"],
            "month_income": income,
            "month_expense": expense,
            "month_balance": income - expense,
            "recent_transactions": [
                {
                    "type": row["type"],
                    "amount": int(row["amount"]),
                    "category": row["category"],
                    "description": row["description"],
                    "date": row["transaction_date"].isoformat(),
                }
                for row in recent
            ],
            "active_reminders": [
                {
                    "id": str(row["id"]),
                    "message": row["message"],
                    "due_at": row["due_at"].isoformat(),
                    "recurrence_type": row["recurrence_type"],
                    "recurrence_interval": row["recurrence_interval"],
                    "recurrence_day": row["recurrence_day"],
                    "recurrence_weekday": row["recurrence_weekday"],
                    "recurrence_time": (
                        row["recurrence_time"].isoformat()
                        if row["recurrence_time"] else None
                    ),
                }
                for row in reminders
            ],
        }


async def ai_business_context(whatsapp_jid: str, limit: int = 12):
    """Build a role-filtered tenant context for AI routing and product resolution."""
    limit = max(1, min(int(limit), 25))
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        result = {
            "business_name": ctx["business_name"],
            "role": ctx["role"],
            "month_income": None,
            "month_expense": None,
            "month_balance": None,
            "recent_transactions": [],
            "active_reminders": [],
            "product_catalog": [],
            "pos_today": None,
            "pos_month": None,
        }
        if has_permission(ctx["role"], "finance.read"):
            totals = await conn.fetchrow(
                """SELECT
                       COALESCE(SUM(amount) FILTER (WHERE type='income' AND status='posted'),0) AS income,
                       COALESCE(SUM(amount) FILTER (WHERE type='expense' AND status='posted'),0) AS expense
                   FROM transactions
                   WHERE tenant_id=$1
                     AND transaction_date >= date_trunc('month',CURRENT_DATE)::date
                     AND transaction_date < (date_trunc('month',CURRENT_DATE)+interval '1 month')::date""",
                ctx["tenant_id"],
            )
            recent = await conn.fetch(
                """SELECT type,amount,category,description,transaction_date
                   FROM transactions
                   WHERE tenant_id=$1 AND status='posted'
                   ORDER BY transaction_date DESC,created_at DESC LIMIT $2""",
                ctx["tenant_id"], limit,
            )
            income, expense = int(totals["income"] or 0), int(totals["expense"] or 0)
            result.update({
                "month_income": income,
                "month_expense": expense,
                "month_balance": income - expense,
                "recent_transactions": [
                    {"type": row["type"], "amount": int(row["amount"]),
                     "category": row["category"], "description": row["description"],
                     "date": row["transaction_date"].isoformat()}
                    for row in recent
                ],
            })
        if has_permission(ctx["role"], "pos.read"):
            products = await conn.fetch(
                """SELECT sku,name,unit,sell_price,stock_on_hand,min_stock
                   FROM products WHERE tenant_id=$1 AND is_active=TRUE
                   ORDER BY name,sku LIMIT 150""",
                ctx["tenant_id"],
            )
            summaries = await conn.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE status='posted' AND sold_at::date=CURRENT_DATE) AS today_count,
                       COALESCE(SUM(total) FILTER (WHERE status='posted' AND sold_at::date=CURRENT_DATE),0) AS today_revenue,
                       COUNT(*) FILTER (WHERE status='posted' AND sold_at >= date_trunc('month',CURRENT_DATE)) AS month_count,
                       COALESCE(SUM(total) FILTER (WHERE status='posted' AND sold_at >= date_trunc('month',CURRENT_DATE)),0) AS month_revenue
                   FROM sales WHERE tenant_id=$1""",
                ctx["tenant_id"],
            )
            result["product_catalog"] = [
                {"sku": row["sku"], "name": row["name"], "unit": row["unit"],
                 "sell_price": int(row["sell_price"]),
                 "stock_on_hand": str(row["stock_on_hand"]),
                 "min_stock": str(row["min_stock"])}
                for row in products
            ]
            result["pos_today"] = {
                "sales_count": int(summaries["today_count"] or 0),
                "revenue": int(summaries["today_revenue"] or 0),
            }
            result["pos_month"] = {
                "sales_count": int(summaries["month_count"] or 0),
                "revenue": int(summaries["month_revenue"] or 0),
            }
        return result


async def log_ai_usage(
    whatsapp_jid: str,
    provider: str,
    model: str,
    intent: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    success: bool = True,
    error_code: str | None = None,
):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        await conn.execute(
            """
            INSERT INTO ai_usage_logs (
                tenant_id,user_id,provider,model,intent,
                prompt_tokens,completion_tokens,total_tokens,success,error_code
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            provider[:40],
            model[:120],
            (intent or "unknown")[:60],
            max(0, int(prompt_tokens or 0)),
            max(0, int(completion_tokens or 0)),
            max(0, int(total_tokens or 0)),
            bool(success),
            (error_code or "")[:120] or None,
        )


async def create_reminder(
    whatsapp_jid: str,
    message: str,
    due_at_utc,
    recurrence_type: str = "once",
    recurrence_interval: int = 1,
    recurrence_day: int | None = None,
    recurrence_weekday: int | None = None,
    recurrence_time=None,
    schedule_timezone: str = "Asia/Jakarta",
):
    recurrence_type = (recurrence_type or "once").lower()
    if recurrence_type not in {"once", "daily", "weekly", "monthly"}:
        raise ValueError("INVALID_RECURRENCE")

    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        row = await conn.fetchrow(
            """
            INSERT INTO reminders (
                tenant_id, created_by, message, due_at,
                delivery_channel, delivery_target,
                recurrence_type, recurrence_interval,
                recurrence_day, recurrence_weekday, recurrence_time,
                schedule_timezone, updated_at
            )
            VALUES (
                $1,$2,$3,$4,'whatsapp',$5,
                $6,$7,$8,$9,$10,$11,NOW()
            )
            RETURNING
                id, message, due_at, status,
                recurrence_type, recurrence_interval,
                recurrence_day, recurrence_weekday, recurrence_time
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            message,
            due_at_utc,
            whatsapp_jid,
            recurrence_type,
            max(1, int(recurrence_interval or 1)),
            recurrence_day,
            recurrence_weekday,
            recurrence_time,
            schedule_timezone,
        )
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,user_id,action,object_type,object_id,new_value
            )
            VALUES (
                $1,$2,'CREATE','reminder',$3,
                jsonb_build_object(
                    'channel','whatsapp',
                    'message',$4::text,
                    'due_at',$5::timestamptz,
                    'recurrence_type',$6::text
                )
            )
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            row["id"],
            message,
            due_at_utc,
            recurrence_type,
        )
        return row


async def list_active_reminders(whatsapp_jid: str, limit: int = 20):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        return await conn.fetch(
            """
            SELECT
                id, message, due_at, status,
                recurrence_type, recurrence_interval,
                recurrence_day, recurrence_weekday, recurrence_time,
                schedule_timezone, last_sent_at, created_at
            FROM reminders
            WHERE tenant_id = $1
              AND status = 'pending'
            ORDER BY due_at ASC
            LIMIT $2
            """,
            ctx["tenant_id"],
            max(1, min(int(limit), 50)),
        )


async def get_reminder(whatsapp_jid: str, reminder_id):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        return await conn.fetchrow(
            """
            SELECT
                id, message, due_at, status,
                recurrence_type, recurrence_interval,
                recurrence_day, recurrence_weekday, recurrence_time,
                schedule_timezone
            FROM reminders
            WHERE tenant_id = $1
              AND id = $2
            """,
            ctx["tenant_id"],
            reminder_id,
        )


async def update_reminder_message(
    whatsapp_jid: str,
    reminder_id,
    message: str,
):
    message = (message or "").strip()
    if len(message) < 2 or len(message) > 500:
        raise ValueError("INVALID_REMINDER_MESSAGE")

    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        old = await conn.fetchrow(
            """
            SELECT message
            FROM reminders
            WHERE tenant_id=$1 AND id=$2 AND status='pending'
            FOR UPDATE
            """,
            ctx["tenant_id"],
            reminder_id,
        )
        if not old:
            raise LookupError("REMINDER_NOT_FOUND")

        row = await conn.fetchrow(
            """
            UPDATE reminders
            SET message=$3, updated_at=NOW()
            WHERE tenant_id=$1 AND id=$2 AND status='pending'
            RETURNING id, message, due_at, recurrence_type,
                      recurrence_day, recurrence_weekday, recurrence_time
            """,
            ctx["tenant_id"],
            reminder_id,
            message,
        )
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,user_id,action,object_type,object_id,old_value,new_value
            )
            VALUES (
                $1,$2,'UPDATE','reminder',$3,
                jsonb_build_object('message',$4::text),
                jsonb_build_object('message',$5::text,'channel','whatsapp')
            )
            """,
            ctx["tenant_id"], ctx["user_id"], reminder_id,
            old["message"], message,
        )
        return row


async def update_reminder_schedule(
    whatsapp_jid: str,
    reminder_id,
    due_at_utc,
    recurrence_type: str = "once",
    recurrence_interval: int = 1,
    recurrence_day: int | None = None,
    recurrence_weekday: int | None = None,
    recurrence_time=None,
    schedule_timezone: str = "Asia/Jakarta",
):
    recurrence_type = (recurrence_type or "once").lower()
    if recurrence_type not in {"once", "daily", "weekly", "monthly"}:
        raise ValueError("INVALID_RECURRENCE")

    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        old = await conn.fetchrow(
            """
            SELECT due_at, recurrence_type
            FROM reminders
            WHERE tenant_id=$1 AND id=$2 AND status='pending'
            FOR UPDATE
            """,
            ctx["tenant_id"], reminder_id,
        )
        if not old:
            raise LookupError("REMINDER_NOT_FOUND")

        row = await conn.fetchrow(
            """
            UPDATE reminders
            SET
                due_at=$3,
                recurrence_type=$4,
                recurrence_interval=$5,
                recurrence_day=$6,
                recurrence_weekday=$7,
                recurrence_time=$8,
                schedule_timezone=$9,
                sent_at=NULL,
                updated_at=NOW()
            WHERE tenant_id=$1 AND id=$2 AND status='pending'
            RETURNING id, message, due_at, recurrence_type,
                      recurrence_day, recurrence_weekday, recurrence_time
            """,
            ctx["tenant_id"], reminder_id, due_at_utc,
            recurrence_type, max(1, int(recurrence_interval or 1)),
            recurrence_day, recurrence_weekday, recurrence_time,
            schedule_timezone,
        )
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,user_id,action,object_type,object_id,old_value,new_value
            )
            VALUES (
                $1,$2,'UPDATE','reminder',$3,
                jsonb_build_object(
                    'due_at',$4::timestamptz,
                    'recurrence_type',$5::text
                ),
                jsonb_build_object(
                    'due_at',$6::timestamptz,
                    'recurrence_type',$7::text,
                    'channel','whatsapp'
                )
            )
            """,
            ctx["tenant_id"], ctx["user_id"], reminder_id,
            old["due_at"], old["recurrence_type"],
            due_at_utc, recurrence_type,
        )
        return row


async def cancel_reminder(whatsapp_jid: str, reminder_id):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        row = await conn.fetchrow(
            """
            UPDATE reminders
            SET status='cancelled', updated_at=NOW()
            WHERE tenant_id=$1 AND id=$2 AND status='pending'
            RETURNING id, message, due_at, recurrence_type
            """,
            ctx["tenant_id"], reminder_id,
        )
        if not row:
            raise LookupError("REMINDER_NOT_FOUND")

        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,user_id,action,object_type,object_id,new_value
            )
            VALUES (
                $1,$2,'CANCEL','reminder',$3,
                jsonb_build_object('channel','whatsapp','cancelled_at',NOW())
            )
            """,
            ctx["tenant_id"], ctx["user_id"], reminder_id,
        )
        return row


async def link_whatsapp_to_telegram(whatsapp_jid: str, code: str):
    """Attach this WhatsApp identity to an existing Telegram user safely.

    Linking is automatic only while the current WA placeholder has no financial
    or reminder records. This prevents accidental cross-business data merges.
    """
    normalized = ''.join(ch for ch in (code or '') if ch.isdigit())
    if len(normalized) != 6:
        raise ValueError("INVALID_LINK_CODE")
    code_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async with pool().acquire() as conn:
        async with conn.transaction():
            identity = await conn.fetchrow(
                """
                SELECT wi.id, wi.user_id, u.active_tenant_id, u.access_status
                FROM whatsapp_identities wi
                JOIN users u ON u.id=wi.user_id
                WHERE wi.whatsapp_jid=$1 AND wi.is_active=TRUE
                FOR UPDATE OF wi, u
                """,
                whatsapp_jid,
            )
            if not identity:
                raise LookupError("WA_IDENTITY_NOT_FOUND")

            link = await conn.fetchrow(
                """
                SELECT c.id, c.user_id, u.active_tenant_id, u.full_name,
                       t.business_name
                FROM channel_link_codes c
                JOIN users u ON u.id=c.user_id
                LEFT JOIN tenants t ON t.id=u.active_tenant_id
                WHERE c.code_hash=$1
                  AND c.used_at IS NULL
                  AND c.expires_at > NOW()
                  AND u.telegram_user_id IS NOT NULL
                  AND u.access_status='active'
                  AND u.deleted_at IS NULL
                  AND u.is_master=FALSE
                ORDER BY c.created_at DESC
                LIMIT 1
                FOR UPDATE OF c
                """,
                code_hash,
            )
            if not link:
                raise LookupError("LINK_CODE_NOT_FOUND")

            source_user_id = identity["user_id"]
            target_user_id = link["user_id"]
            if source_user_id == target_user_id:
                await conn.execute(
                    "UPDATE channel_link_codes SET used_at=NOW() WHERE id=$1",
                    link["id"],
                )
                return {
                    "status": "already_linked",
                    "business_name": link["business_name"],
                    "full_name": link["full_name"],
                }

            existing_target = await conn.fetchval(
                """
                SELECT whatsapp_jid
                FROM whatsapp_identities
                WHERE user_id=$1 AND is_active=TRUE
                LIMIT 1
                """,
                target_user_id,
            )
            if existing_target:
                raise ValueError("TARGET_ALREADY_HAS_WHATSAPP")

            source_tenant = identity["active_tenant_id"]
            if source_tenant:
                # transactions/reminders memakai FORCE RLS; set context source
                # agar pemeriksaan data tidak salah membaca nol.
                await _set_tenant_context(conn, source_tenant)
                data_count = await conn.fetchval(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM transactions WHERE tenant_id=$1)
                        + (SELECT COUNT(*) FROM reminders WHERE tenant_id=$1)
                    """,
                    source_tenant,
                )
                if int(data_count or 0) > 0:
                    raise ValueError("WA_ACCOUNT_HAS_DATA")

            await conn.execute(
                "UPDATE whatsapp_identities SET user_id=$2 WHERE id=$1",
                identity["id"], target_user_id,
            )

            if source_tenant and source_tenant != link["active_tenant_id"]:
                await conn.execute(
                    "UPDATE tenant_members SET is_active=FALSE WHERE user_id=$1",
                    source_user_id,
                )
                await conn.execute(
                    "UPDATE tenants SET is_active=FALSE WHERE id=$1",
                    source_tenant,
                )

            await conn.execute(
                """
                UPDATE users
                SET access_status='deleted', deleted_at=NOW(), updated_at=NOW()
                WHERE id=$1 AND is_master=FALSE
                """,
                source_user_id,
            )
            await conn.execute(
                "UPDATE channel_link_codes SET used_at=NOW() WHERE id=$1",
                link["id"],
            )

            return {
                "status": "linked",
                "business_name": link["business_name"],
                "full_name": link["full_name"],
            }


async def rename_business(whatsapp_jid: str, business_name: str):
    name = (business_name or "").strip()
    if len(name) < 2 or len(name) > 100:
        raise ValueError("INVALID_BUSINESS_NAME")
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        require_permission(ctx, "business.write")
        old = ctx["business_name"]
        await conn.execute(
            "UPDATE tenants SET business_name=$1 WHERE id=$2",
            name,
            ctx["tenant_id"],
        )
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,user_id,action,object_type,object_id,old_value,new_value
            )
            VALUES (
                $1,$2,'UPDATE','tenant',$1,
                jsonb_build_object('business_name',$3::text),
                jsonb_build_object('business_name',$4::text,'channel','whatsapp')
            )
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            old,
            name,
        )
        return name


async def finance_export_data(
    whatsapp_jid: str,
    start_date: date | None = None,
    end_date: date | None = None,
):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        require_permission(ctx, "finance.export")
        rows = await conn.fetch(
            """
            SELECT transaction_date,type,amount,category,description,created_at
            FROM transactions
            WHERE tenant_id=$1
              AND status='posted'
              AND ($2::date IS NULL OR transaction_date >= $2::date)
              AND ($3::date IS NULL OR transaction_date <= $3::date)
            ORDER BY transaction_date ASC, created_at ASC
            """,
            ctx["tenant_id"],
            start_date,
            end_date,
        )
        return ctx, rows


async def log_finance_export(
    whatsapp_jid: str,
    export_format: str,
    period_label: str,
    row_count: int,
):
    async with tenant_tx(whatsapp_jid) as (conn, ctx):
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,user_id,action,object_type,new_value
            )
            VALUES (
                $1,$2,'EXPORT_FINANCE','report',
                jsonb_build_object(
                    'channel','whatsapp',
                    'format',$3::text,
                    'period',$4::text,
                    'row_count',$5::integer
                )
            )
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            export_format[:20],
            period_label[:120],
            int(row_count),
        )


async def set_pending_action(
    whatsapp_jid: str,
    action_type: str,
    payload: dict,
    ttl_minutes: int = 15,
):
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO whatsapp_pending_actions (
                whatsapp_jid, action_type, payload, expires_at, updated_at
            )
            VALUES ($1,$2,$3::jsonb,NOW()+($4::integer * INTERVAL '1 minute'),NOW())
            ON CONFLICT (whatsapp_jid)
            DO UPDATE SET
                action_type=EXCLUDED.action_type,
                payload=EXCLUDED.payload,
                expires_at=EXCLUDED.expires_at,
                updated_at=NOW()
            """,
            whatsapp_jid,
            action_type,
            json.dumps(payload, ensure_ascii=False),
            max(1, min(int(ttl_minutes), 60)),
        )


async def get_pending_action(whatsapp_jid: str):
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT action_type,payload,expires_at
            FROM whatsapp_pending_actions
            WHERE whatsapp_jid=$1
              AND expires_at > NOW()
            """,
            whatsapp_jid,
        )
        if row:
            return row
        await conn.execute(
            "DELETE FROM whatsapp_pending_actions WHERE whatsapp_jid=$1",
            whatsapp_jid,
        )
        return None


async def clear_pending_action(whatsapp_jid: str):
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM whatsapp_pending_actions WHERE whatsapp_jid=$1",
            whatsapp_jid,
        )


async def enqueue_event(
    message_id: str,
    whatsapp_jid: str,
    raw_payload: dict,
):
    async with pool().acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO whatsapp_inbox_events (
                message_id, whatsapp_jid, payload, status,
                attempts, next_attempt_at, received_at
            )
            VALUES ($1,$2,$3::jsonb,'queued',0,NOW(),NOW())
            ON CONFLICT (message_id) DO NOTHING
            """,
            message_id,
            whatsapp_jid,
            json.dumps(raw_payload, ensure_ascii=False),
        )
        return result.endswith("1")


async def claim_next_event():
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT message_id, whatsapp_jid, payload, attempts
                FROM whatsapp_inbox_events
                WHERE status='queued'
                  AND next_attempt_at <= NOW()
                ORDER BY received_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            if not row:
                return None
            updated = await conn.fetchrow(
                """
                UPDATE whatsapp_inbox_events
                SET status='processing',
                    attempts=attempts+1,
                    last_error=NULL
                WHERE message_id=$1
                RETURNING message_id, whatsapp_jid, payload, attempts
                """,
                row["message_id"],
            )
            return updated


async def mark_event_done(message_id: str):
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE whatsapp_inbox_events
            SET status='done', processed_at=NOW(), last_error=NULL
            WHERE message_id=$1
            """,
            message_id,
        )


async def mark_event_failed(message_id: str, attempts: int, error: str):
    attempts = int(attempts or 1)
    async with pool().acquire() as conn:
        if attempts >= 5:
            await conn.execute(
                """
                UPDATE whatsapp_inbox_events
                SET status='failed', last_error=$2, processed_at=NOW()
                WHERE message_id=$1
                """,
                message_id,
                error[:1000],
            )
        else:
            await conn.execute(
                """
                UPDATE whatsapp_inbox_events
                SET status='queued',
                    next_attempt_at=NOW()+INTERVAL '30 seconds',
                    last_error=$2
                WHERE message_id=$1
                """,
                message_id,
                error[:1000],
            )


async def claim_due_reminders(limit: int = 20):
    async with pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM wa_claim_due_reminders($1)",
            max(1, min(int(limit), 100)),
        )


async def mark_reminder_sent(reminder_id):
    async with pool().acquire() as conn:
        await conn.execute(
            "SELECT wa_mark_reminder_sent($1)",
            reminder_id,
        )
