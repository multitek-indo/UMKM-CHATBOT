import os
import hashlib
import secrets
from contextlib import asynccontextmanager
import asyncpg
from app.permissions import has_permission, require_permission

DATABASE_URL = os.environ["DATABASE_URL"]
MASTER_TELEGRAM_ID = int(os.environ["MASTER_TELEGRAM_ID"])

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=15,
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


async def _set_user_context(conn: asyncpg.Connection, telegram_user_id: int):
    await conn.execute(
        "SELECT set_config('app.telegram_user_id', $1, true)",
        str(telegram_user_id),
    )


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


@asynccontextmanager
async def user_tx(telegram_user_id: int):
    async with pool().acquire() as conn:
        async with conn.transaction():
            await _set_user_context(conn, telegram_user_id)
            yield conn


@asynccontextmanager
async def tenant_tx(telegram_user_id: int):
    """Resolve tenant safely, including MASTER troubleshooting for WA-only users."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            await _set_user_context(conn, telegram_user_id)

            actor = await conn.fetchrow(
                """
                SELECT id, is_master
                FROM users
                WHERE telegram_user_id = $1
                  AND access_status = 'active'
                  AND deleted_at IS NULL
                """,
                telegram_user_id,
            )
            if not actor:
                raise LookupError("NO_ACTIVE_ACTOR")

            target_user_id = actor["id"]
            support_mode = False

            if telegram_user_id == MASTER_TELEGRAM_ID and actor["is_master"]:
                support = await conn.fetchrow("SELECT * FROM admin_get_support()")
                if support:
                    target_user_id = support["target_user_id"]
                    support_mode = True

            row = await conn.fetchrow(
                """
                SELECT
                    u.id AS user_id,
                    u.telegram_user_id,
                    u.active_tenant_id AS tenant_id,
                    u.access_status,
                    u.is_master,
                    u.full_name,
                    t.business_name,
                    tm.role
                FROM users u
                JOIN tenant_members tm
                  ON tm.user_id = u.id
                 AND tm.tenant_id = u.active_tenant_id
                 AND tm.is_active = TRUE
                JOIN tenants t
                  ON t.id = u.active_tenant_id
                 AND t.is_active = TRUE
                WHERE u.id = $1
                  AND u.access_status = 'active'
                  AND u.deleted_at IS NULL
                  AND u.is_master = FALSE
                """,
                target_user_id,
            )

            if not row:
                raise LookupError("NO_ACTIVE_TENANT")

            await _set_tenant_context(conn, row["tenant_id"])
            ctx = dict(row)
            ctx["actor_user_id"] = actor["id"]
            ctx["actor_telegram_user_id"] = telegram_user_id
            ctx["support_mode"] = support_mode
            await _set_actor_context(conn, ctx)
            yield conn, ctx

async def ensure_user(telegram_user_id: int, full_name: str | None):
    is_master = telegram_user_id == MASTER_TELEGRAM_ID
    async with user_tx(telegram_user_id) as conn:
        return await conn.fetchrow(
            """
            INSERT INTO users (
                telegram_user_id,
                full_name,
                access_status,
                is_master,
                approved_at
            )
            VALUES (
                $1,
                $2,
                CASE WHEN $3 THEN 'active' ELSE 'pending' END,
                $3,
                CASE WHEN $3 THEN NOW() ELSE NULL END
            )
            ON CONFLICT (telegram_user_id)
            DO UPDATE SET
                full_name = COALESCE(EXCLUDED.full_name, users.full_name),
                is_master = CASE WHEN $3 THEN TRUE ELSE users.is_master END,
                access_status = CASE
                    WHEN $3 THEN 'active'
                    ELSE users.access_status
                END,
                deleted_at = CASE
                    WHEN $3 THEN NULL
                    ELSE users.deleted_at
                END,
                updated_at = NOW()
            RETURNING
                id,
                telegram_user_id,
                full_name,
                active_tenant_id,
                access_status,
                is_master
            """,
            telegram_user_id,
            full_name,
            is_master,
        )


async def get_access(telegram_user_id: int):
    async with user_tx(telegram_user_id) as conn:
        if telegram_user_id == MASTER_TELEGRAM_ID:
            support = await conn.fetchrow("SELECT * FROM admin_get_support()")
            if support:
                return await conn.fetchrow(
                    """
                    SELECT
                        u.id,
                        u.telegram_user_id,
                        u.full_name,
                        u.active_tenant_id,
                        u.access_status,
                        FALSE AS is_master,
                        t.business_name,
                        tm.role,
                        TRUE AS support_mode
                    FROM users u
                    LEFT JOIN tenants t ON t.id = u.active_tenant_id
                    LEFT JOIN tenant_members tm
                      ON tm.user_id=u.id AND tm.tenant_id=u.active_tenant_id AND tm.is_active=TRUE
                    WHERE u.id = $1
                      AND u.access_status = 'active'
                      AND u.deleted_at IS NULL
                      AND u.is_master = FALSE
                    """,
                    support["target_user_id"],
                )

        return await conn.fetchrow(
            """
            SELECT
                u.id,
                u.telegram_user_id,
                u.full_name,
                u.active_tenant_id,
                u.access_status,
                u.is_master,
                t.business_name,
                tm.role,
                FALSE AS support_mode
            FROM users u
            LEFT JOIN tenants t ON t.id = u.active_tenant_id
            LEFT JOIN tenant_members tm
              ON tm.user_id=u.id AND tm.tenant_id=u.active_tenant_id AND tm.is_active=TRUE
            WHERE u.telegram_user_id = $1
            """,
            telegram_user_id,
        )


async def master_list_users(telegram_user_id: int):
    async with user_tx(telegram_user_id) as conn:
        return await conn.fetch("SELECT * FROM admin_list_users()")


async def master_start_support(telegram_user_id: int, target_user_id):
    async with user_tx(telegram_user_id) as conn:
        return await conn.fetchrow(
            "SELECT * FROM admin_start_support($1)",
            target_user_id,
        )


async def master_stop_support(telegram_user_id: int):
    async with user_tx(telegram_user_id) as conn:
        await conn.execute("SELECT admin_stop_support()")


async def master_get_support(telegram_user_id: int):
    if telegram_user_id != MASTER_TELEGRAM_ID:
        return None
    async with user_tx(telegram_user_id) as conn:
        return await conn.fetchrow("SELECT * FROM admin_get_support()")


async def master_approve_user(
    telegram_user_id: int,
    user_id,
    business_name: str,
):
    async with user_tx(telegram_user_id) as conn:
        return await conn.fetchval(
            "SELECT admin_approve_user($1, $2)",
            user_id,
            business_name,
        )


async def master_precreate_user(
    telegram_user_id: int,
    target_telegram_id: int,
    full_name: str,
    business_name: str,
):
    async with user_tx(telegram_user_id) as conn:
        return await conn.fetchval(
            "SELECT admin_precreate_user($1, $2, $3)",
            target_telegram_id,
            full_name,
            business_name,
        )


async def master_set_user_status(
    telegram_user_id: int,
    user_id,
    status: str,
):
    async with user_tx(telegram_user_id) as conn:
        await conn.execute(
            "SELECT admin_set_user_status($1, $2)",
            user_id,
            status,
        )


async def master_rename_user_business(
    telegram_user_id: int,
    user_id,
    business_name: str,
):
    async with user_tx(telegram_user_id) as conn:
        await conn.execute(
            "SELECT admin_rename_user_business($1, $2)",
            user_id,
            business_name,
        )


async def create_whatsapp_link_code(telegram_user_id: int):
    """Create a one-time 6-digit code valid for 10 minutes."""
    if telegram_user_id == MASTER_TELEGRAM_ID:
        raise ValueError("MASTER_CANNOT_LINK")

    async with user_tx(telegram_user_id) as conn:
        user = await conn.fetchrow(
            """
            SELECT id, full_name, active_tenant_id
            FROM users
            WHERE telegram_user_id=$1
              AND access_status='active'
              AND deleted_at IS NULL
              AND is_master=FALSE
            """,
            telegram_user_id,
        )
        if not user or not user["active_tenant_id"]:
            raise LookupError("NO_ACTIVE_USER")

        # Bersihkan kode lama/expired dan pastikan kode aktif tidak bentrok.
        await conn.execute(
            "DELETE FROM channel_link_codes WHERE used_at IS NOT NULL OR expires_at <= NOW() OR user_id=$1",
            user["id"],
        )

        for _ in range(20):
            code = f"{secrets.randbelow(1_000_000):06d}"
            code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO channel_link_codes(user_id, code_hash, expires_at)
                    VALUES ($1,$2,NOW()+INTERVAL '10 minutes')
                    RETURNING expires_at
                    """,
                    user["id"], code_hash,
                )
                return code, row["expires_at"]
            except asyncpg.UniqueViolationError:
                continue

        raise RuntimeError("LINK_CODE_GENERATION_FAILED")

async def get_user_channels(telegram_user_id: int, target_user_id=None):
    async with user_tx(telegram_user_id) as conn:
        if target_user_id is None:
            row = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_user_id=$1",
                telegram_user_id,
            )
            if not row:
                return None
            target_user_id = row["id"]
        return await conn.fetchrow(
            """
            SELECT
                u.telegram_user_id,
                wi.phone_number AS whatsapp_phone,
                wi.whatsapp_jid
            FROM users u
            LEFT JOIN whatsapp_identities wi
              ON wi.user_id=u.id AND wi.is_active=TRUE
            WHERE u.id=$1
            """,
            target_user_id,
        )


async def rename_own_business(telegram_user_id: int, business_name: str):
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        require_permission(ctx, "business.write")
        name = business_name.strip()
        if len(name) < 2 or len(name) > 100:
            raise ValueError("INVALID_BUSINESS_NAME")

        old_name = ctx["business_name"]

        await conn.execute(
            """
            UPDATE tenants
            SET business_name = $1
            WHERE id = $2
            """,
            name,
            ctx["tenant_id"],
        )

        await conn.execute(
            """
            INSERT INTO audit_logs
                (tenant_id, user_id, action, object_type, object_id, old_value, new_value)
            VALUES
                ($1, $2, 'RENAME_BUSINESS', 'tenant', $3,
                 jsonb_build_object('business_name', $4::text),
                 jsonb_build_object(
                    'business_name', $5::text,
                    'support_mode', $6::boolean
                 ))
            """,
            ctx["tenant_id"],
            ctx["actor_user_id"],
            ctx["tenant_id"],
            old_name,
            name,
            ctx["support_mode"],
        )


async def create_transaction(
    telegram_user_id: int,
    tx_type: str,
    amount: int,
    description: str,
    category: str = "Umum",
):
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        require_permission(ctx, "finance.write")
        row = await conn.fetchrow(
            """
            INSERT INTO transactions
                (tenant_id, created_by, type, amount, category, description)
            VALUES
                ($1, $2, $3, $4, $5, $6)
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
            INSERT INTO audit_logs
                (tenant_id, user_id, action, object_type, object_id, new_value)
            VALUES
                ($1, $2, 'CREATE', 'transaction', $3,
                 jsonb_build_object(
                    'type', $4::text,
                    'amount', $5::numeric,
                    'description', $6::text,
                    'support_mode', $7::boolean
                 ))
            """,
            ctx["tenant_id"],
            ctx["actor_user_id"],
            row["id"],
            tx_type,
            amount,
            description,
            ctx["support_mode"],
        )
        return row


async def monthly_summary(telegram_user_id: int):
    async with tenant_tx(telegram_user_id) as (conn, ctx):
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


async def create_reminder(
    telegram_user_id: int,
    message: str,
    due_at_utc,
):
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        delivery_channel = "telegram"
        delivery_target = None

        # Saat MASTER troubleshooting user WA-only, reminder harus tetap sampai
        # ke channel user target, bukan hilang karena target tidak punya Telegram ID.
        if ctx.get("telegram_user_id") is None:
            wa_jid = await conn.fetchval(
                """
                SELECT whatsapp_jid
                FROM whatsapp_identities
                WHERE user_id=$1 AND is_active=TRUE
                LIMIT 1
                """,
                ctx["user_id"],
            )
            if wa_jid:
                delivery_channel = "whatsapp"
                delivery_target = wa_jid

        row = await conn.fetchrow(
            """
            INSERT INTO reminders (
                tenant_id, created_by, message, due_at,
                delivery_channel, delivery_target
            )
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING id, message, due_at, delivery_channel
            """,
            ctx["tenant_id"],
            ctx["user_id"],
            message,
            due_at_utc,
            delivery_channel,
            delivery_target,
        )

        await conn.execute(
            """
            INSERT INTO audit_logs
                (tenant_id, user_id, action, object_type, object_id, new_value)
            VALUES
                ($1, $2, 'CREATE', 'reminder', $3,
                 jsonb_build_object(
                    'message', $4::text,
                    'due_at', $5::text,
                    'delivery_channel', $6::text,
                    'support_mode', $7::boolean
                 ))
            """,
            ctx["tenant_id"],
            ctx["actor_user_id"],
            row["id"],
            message,
            str(due_at_utc),
            delivery_channel,
            ctx["support_mode"],
        )
        return row

async def list_pending_reminders(telegram_user_id: int, limit: int = 10):
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        return await conn.fetch(
            """
            SELECT id, message, due_at
            FROM reminders
            WHERE tenant_id = $1
              AND status = 'pending'
            ORDER BY due_at ASC
            LIMIT $2
            """,
            ctx["tenant_id"],
            limit,
        )

async def ai_finance_context(telegram_user_id: int, limit: int = 12):
    """Return only the active tenant's finance context for the AI layer."""
    limit = max(1, min(int(limit), 25))
    async with tenant_tx(telegram_user_id) as (conn, ctx):
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
        }


async def ai_business_context(telegram_user_id: int, limit: int = 12):
    """Build a role-filtered tenant context for AI routing and product resolution."""
    limit = max(1, min(int(limit), 25))
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        result = {
            "business_name": ctx["business_name"], "role": ctx["role"],
            "month_income": None, "month_expense": None, "month_balance": None,
            "recent_transactions": [], "product_catalog": [],
            "pos_today": None, "pos_month": None,
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
                   FROM transactions WHERE tenant_id=$1 AND status='posted'
                   ORDER BY transaction_date DESC,created_at DESC LIMIT $2""",
                ctx["tenant_id"], limit,
            )
            income, expense = int(totals["income"] or 0), int(totals["expense"] or 0)
            result.update({
                "month_income": income, "month_expense": expense,
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
            result["pos_today"] = {"sales_count": int(summaries["today_count"] or 0), "revenue": int(summaries["today_revenue"] or 0)}
            result["pos_month"] = {"sales_count": int(summaries["month_count"] or 0), "revenue": int(summaries["month_revenue"] or 0)}
        return result


async def log_ai_usage(
    telegram_user_id: int,
    provider: str,
    model: str,
    intent: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    success: bool = True,
    error_code: str | None = None,
):
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        await conn.execute(
            """
            INSERT INTO ai_usage_logs (
                tenant_id,
                user_id,
                provider,
                model,
                intent,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                success,
                error_code
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            ctx["tenant_id"],
            ctx["actor_user_id"],
            provider[:40],
            model[:120],
            (intent or "unknown")[:60],
            max(0, int(prompt_tokens or 0)),
            max(0, int(completion_tokens or 0)),
            max(0, int(total_tokens or 0)),
            bool(success),
            (error_code or "")[:120] or None,
        )

async def finance_export_data(
    telegram_user_id: int,
    start_date=None,
    end_date=None,
):
    """Return posted transactions for only the active tenant and requested period."""
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        require_permission(ctx, "finance.export")
        rows = await conn.fetch(
            """
            SELECT
                transaction_date,
                type,
                amount,
                category,
                description,
                created_at
            FROM transactions
            WHERE tenant_id = $1
              AND status = 'posted'
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
    telegram_user_id: int,
    export_format: str,
    period_label: str,
    row_count: int,
):
    """Audit successful finance exports without storing the generated file."""
    async with tenant_tx(telegram_user_id) as (conn, ctx):
        await conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id,
                user_id,
                action,
                object_type,
                object_id,
                new_value
            )
            VALUES (
                $1,
                $2,
                'EXPORT_FINANCE',
                'finance_report',
                NULL,
                jsonb_build_object(
                    'format', $3::text,
                    'period', $4::text,
                    'row_count', $5::integer
                )
            )
            """,
            ctx["tenant_id"],
            ctx["actor_user_id"],
            export_format[:20],
            period_label[:120],
            max(0, int(row_count)),
        )
