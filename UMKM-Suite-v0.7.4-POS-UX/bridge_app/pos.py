"""Tenant-safe POS, inventory, opname, reporting, and staff services."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
import re
import secrets
import uuid

from bridge_app import db
from bridge_app.permissions import can_manage_role, require_permission
from bridge_app.pos_parser import normalize_sku
from bridge_app.product_match import choose_product_match, normalize_product_query


PAYMENT_METHODS = {"CASH", "TRANSFER", "QRIS", "CARD", "OTHER"}
STAFF_ROLES = {"ADMIN", "FINANCE", "CASHIER", "INVENTORY", "VIEWER"}


def _actor(ctx: dict):
    return ctx.get("actor_user_id") or ctx["user_id"]


async def _audit(conn, ctx: dict, action: str, object_type: str, object_id, value: dict):
    await conn.execute(
        """
        INSERT INTO audit_logs (tenant_id,user_id,action,object_type,object_id,new_value)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        """,
        ctx["tenant_id"], _actor(ctx), action[:80], object_type[:80], object_id,
        json.dumps(value, default=str, ensure_ascii=False),
    )


async def list_products(identity, *, low_stock_only: bool = False, limit: int = 50):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.read")
        rows = await conn.fetch(
            """
            SELECT id,sku,barcode,name,unit,sell_price,cost_price,
                   stock_on_hand,min_stock,is_active,updated_at
            FROM products
            WHERE tenant_id=$1 AND is_active=TRUE
              AND (NOT $2::boolean OR stock_on_hand <= min_stock)
            ORDER BY name,sku LIMIT $3
            """, ctx["tenant_id"], low_stock_only, max(1, min(int(limit), 100)),
        )
        return ctx, rows


async def get_product(identity, sku: str):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.read")
        row = await conn.fetchrow(
            """SELECT id,sku,barcode,name,unit,sell_price,cost_price,
                      stock_on_hand,min_stock,is_active
               FROM products WHERE tenant_id=$1 AND sku=$2""",
            ctx["tenant_id"], normalize_sku(sku),
        )
        return ctx, row


def _product_public(row) -> dict:
    return {
        "sku": str(row["sku"]),
        "name": str(row["name"]),
        "unit": str(row["unit"]),
        "sell_price": int(row["sell_price"]),
        "stock_on_hand": str(row["stock_on_hand"]),
        "min_stock": str(row["min_stock"]),
    }


async def resolve_product(identity, query: str):
    """Resolve a product within the caller's tenant, returning explicit choices when ambiguous."""
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.read")
        rows = await conn.fetch(
            """SELECT id,sku,barcode,name,unit,sell_price,cost_price,
                      stock_on_hand,min_stock,is_active
               FROM products
               WHERE tenant_id=$1 AND is_active=TRUE
               ORDER BY name,sku LIMIT 250""",
            ctx["tenant_id"],
        )
        selected, candidates = choose_product_match(query, rows)
        return (
            ctx,
            _product_public(selected) if selected else None,
            [_product_public(row) for row in candidates],
        )


async def suggest_product_sku(identity, name: str) -> str:
    """Create a readable tenant-unique SKU from a product name."""
    base = re.sub(r"[^A-Z0-9]+", "-", normalize_product_query(name).upper()).strip("-")
    base = (base or "PRODUK")[:32].rstrip("-")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "product.write")
        for suffix in range(1, 1000):
            candidate = base if suffix == 1 else f"{base[:35-len(str(suffix))]}-{suffix}"
            exists = await conn.fetchval(
                "SELECT 1 FROM products WHERE tenant_id=$1 AND sku=$2",
                ctx["tenant_id"], candidate,
            )
            if not exists:
                return candidate
    raise RuntimeError("Tidak dapat membuat SKU otomatis.")


async def prepare_sale_preview(identity, items: list[dict], payment_method: str | None = None):
    """Resolve human product names and calculate a non-writing sale preview."""
    method = (payment_method or "").strip().upper() or None
    if method is not None and method not in PAYMENT_METHODS:
        raise ValueError("Metode pembayaran belum dikenali.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.sell")
        rows = await conn.fetch(
            """SELECT id,sku,barcode,name,unit,sell_price,cost_price,
                      stock_on_hand,min_stock,is_active
               FROM products
               WHERE tenant_id=$1 AND is_active=TRUE
               ORDER BY name,sku LIMIT 250""",
            ctx["tenant_id"],
        )
        if not rows:
            raise LookupError("Belum ada produk aktif pada usaha ini.")

        products_by_sku = {str(row["sku"]): row for row in rows}
        normalized: dict[str, Decimal] = {}
        unresolved = []
        for item_index, item in enumerate((items or [])[:50]):
            query = str(
                item.get("product_query") or item.get("query") or
                item.get("name") or item.get("sku") or ""
            ).strip()
            try:
                qty = Decimal(str(item.get("qty"))).quantize(Decimal("0.001"))
            except Exception as exc:
                raise ValueError("Jumlah produk belum valid.") from exc
            if not query or qty <= 0:
                raise ValueError("Nama produk dan jumlah wajib diisi.")
            selected, candidates = choose_product_match(query, rows)
            if selected is None:
                unresolved.append({
                    "item_index": item_index,
                    "query": query,
                    "qty": str(qty),
                    "candidates": [_product_public(row) for row in candidates],
                })
                continue
            sku = str(selected["sku"])
            normalized[sku] = normalized.get(sku, Decimal("0")) + qty

        if not normalized and not unresolved:
            raise ValueError("Minimal satu produk diperlukan.")

        preview_items = []
        total = Decimal("0")
        for sku, qty in normalized.items():
            product = products_by_sku[sku]
            if Decimal(product["stock_on_hand"]) < qty:
                raise ValueError(
                    f"Stok {product['name']} tidak cukup. Tersedia {product['stock_on_hand']} {product['unit']}."
                )
            line_total = Decimal(product["sell_price"]) * qty
            total += line_total
            preview_items.append({
                "sku": sku,
                "name": str(product["name"]),
                "unit": str(product["unit"]),
                "qty": str(qty),
                "unit_price": int(product["sell_price"]),
                "line_total": int(line_total),
                "stock_before": str(product["stock_on_hand"]),
                "stock_after": str(Decimal(product["stock_on_hand"]) - qty),
            })
        return ctx, {
            "items": preview_items,
            "unresolved": unresolved,
            "payment_method": method,
            "total": int(total),
        }


async def add_product(
    identity, sku: str, name: str, sell_price: int, cost_price: int = 0,
    opening_stock: Decimal = Decimal("0"), unit: str = "pcs", min_stock: Decimal = Decimal("0"),
):
    sku = normalize_sku(sku)
    name = (name or "").strip()[:160]
    unit = (unit or "pcs").strip()[:30]
    opening_stock = Decimal(opening_stock).quantize(Decimal("0.001"))
    min_stock = Decimal(min_stock).quantize(Decimal("0.001"))
    if len(name) < 2 or sell_price < 0 or cost_price < 0 or opening_stock < 0 or min_stock < 0:
        raise ValueError("Data produk tidak valid.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "product.write")
        exists = await conn.fetchval(
            "SELECT 1 FROM products WHERE tenant_id=$1 AND sku=$2",
            ctx["tenant_id"], sku,
        )
        if exists:
            raise ValueError(f"SKU {sku} sudah digunakan. Gunakan kode lain atau edit produk.")
        row = await conn.fetchrow(
            """
            INSERT INTO products
                (tenant_id,sku,name,unit,sell_price,cost_price,stock_on_hand,min_stock,created_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING *
            """,
            ctx["tenant_id"], sku, name, unit, sell_price, cost_price,
            opening_stock, min_stock, ctx["user_id"],
        )
        if opening_stock:
            await conn.execute(
                """
                INSERT INTO stock_movements
                    (tenant_id,product_id,created_by,movement_type,qty_delta,stock_after,note)
                VALUES ($1,$2,$3,'opening',$4,$4,'Stok awal produk')
                """, ctx["tenant_id"], row["id"], ctx["user_id"], opening_stock,
            )
        await _audit(conn, ctx, "CREATE_PRODUCT", "product", row["id"], {"sku": sku, "name": name})
        return ctx, row


async def update_product(identity, sku: str, *, name: str, sell_price: int, cost_price: int, unit: str, min_stock: Decimal):
    sku = normalize_sku(sku)
    name, unit = (name or "").strip()[:160], (unit or "pcs").strip()[:30]
    min_stock = Decimal(min_stock).quantize(Decimal("0.001"))
    if len(name) < 2 or sell_price < 0 or cost_price < 0 or min_stock < 0:
        raise ValueError("Data produk tidak valid.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "product.write")
        row = await conn.fetchrow(
            """
            UPDATE products SET name=$3,unit=$4,sell_price=$5,cost_price=$6,
                                min_stock=$7,updated_at=NOW()
            WHERE tenant_id=$1 AND sku=$2 AND is_active=TRUE RETURNING *
            """, ctx["tenant_id"], sku, name, unit, sell_price, cost_price, min_stock,
        )
        if not row:
            raise LookupError("Produk tidak ditemukan.")
        await _audit(conn, ctx, "UPDATE_PRODUCT", "product", row["id"], {"sku": sku})
        return ctx, row


async def deactivate_product(identity, sku: str):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "product.write")
        row = await conn.fetchrow(
            """UPDATE products SET is_active=FALSE,updated_at=NOW()
               WHERE tenant_id=$1 AND sku=$2 AND is_active=TRUE RETURNING id,sku,name""",
            ctx["tenant_id"], normalize_sku(sku),
        )
        if not row:
            raise LookupError("Produk tidak ditemukan.")
        await _audit(conn, ctx, "DEACTIVATE_PRODUCT", "product", row["id"], {"sku": row["sku"]})
        return ctx, row


async def set_stock(identity, sku: str, new_stock: Decimal, note: str):
    new_stock = Decimal(new_stock).quantize(Decimal("0.001"))
    if new_stock < 0:
        raise ValueError("Stok tidak boleh negatif.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "stock.write")
        row = await conn.fetchrow(
            """SELECT * FROM products WHERE tenant_id=$1 AND sku=$2 AND is_active=TRUE FOR UPDATE""",
            ctx["tenant_id"], normalize_sku(sku),
        )
        if not row:
            raise LookupError("Produk tidak ditemukan.")
        delta = new_stock - row["stock_on_hand"]
        if not delta:
            return ctx, row, delta
        updated = await conn.fetchrow(
            """UPDATE products SET stock_on_hand=$3,updated_at=NOW()
               WHERE tenant_id=$1 AND id=$2 RETURNING *""",
            ctx["tenant_id"], row["id"], new_stock,
        )
        await conn.execute(
            """
            INSERT INTO stock_movements
                (tenant_id,product_id,created_by,movement_type,qty_delta,stock_after,note)
            VALUES ($1,$2,$3,'adjustment',$4,$5,$6)
            """, ctx["tenant_id"], row["id"], ctx["user_id"], delta, new_stock,
            (note or "Penyesuaian manual")[:300],
        )
        await _audit(conn, ctx, "ADJUST_STOCK", "product", row["id"], {"sku": row["sku"], "before": row["stock_on_hand"], "after": new_stock})
        return ctx, updated, delta


async def create_sale(identity, items: list[dict], payment_method: str, channel: str, idempotency_key: str):
    payment_method = (payment_method or "CASH").strip().upper()
    if payment_method not in PAYMENT_METHODS:
        raise ValueError("Metode bayar: CASH, TRANSFER, QRIS, CARD, atau OTHER.")
    if channel not in {"telegram", "whatsapp"}:
        raise ValueError("Channel penjualan tidak valid.")
    try:
        uuid.UUID(str(idempotency_key))
    except ValueError as exc:
        raise ValueError("Idempotency key tidak valid.") from exc

    normalized: dict[str, Decimal] = {}
    for item in items:
        try:
            sku = normalize_sku(str(item["sku"]))
            qty = Decimal(str(item["qty"])).quantize(Decimal("0.001"))
        except (KeyError, TypeError, ArithmeticError) as exc:
            raise ValueError("Item penjualan AI/command tidak valid.") from exc
        if qty <= 0:
            raise ValueError("Jumlah penjualan harus lebih dari 0.")
        normalized[sku] = normalized.get(sku, Decimal("0")) + qty
    if not normalized or len(normalized) > 50:
        raise ValueError("Penjualan harus berisi 1-50 SKU.")

    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.sell")
        existing = await conn.fetchrow(
            """SELECT id,invoice_no,total,payment_method,status
               FROM sales WHERE tenant_id=$1 AND idempotency_key=$2::uuid""",
            ctx["tenant_id"], idempotency_key,
        )
        if existing:
            return ctx, existing, True

        rows = await conn.fetch(
            """SELECT * FROM products
               WHERE tenant_id=$1 AND sku=ANY($2::text[]) AND is_active=TRUE FOR UPDATE""",
            ctx["tenant_id"], list(normalized),
        )
        products = {row["sku"]: row for row in rows}
        missing = [sku for sku in normalized if sku not in products]
        if missing:
            raise LookupError("SKU tidak ditemukan: " + ", ".join(missing))
        short = [f"{sku} (stok {products[sku]['stock_on_hand']})" for sku, qty in normalized.items() if products[sku]["stock_on_hand"] < qty]
        if short:
            raise ValueError("Stok tidak cukup: " + ", ".join(short))

        counter_key = f"SALE-{date.today():%Y%m%d}"
        seq = await conn.fetchval(
            """
            INSERT INTO tenant_counters (tenant_id,counter_key,last_value)
            VALUES ($1,$2,1)
            ON CONFLICT (tenant_id,counter_key)
            DO UPDATE SET last_value=tenant_counters.last_value+1,updated_at=NOW()
            RETURNING last_value
            """, ctx["tenant_id"], counter_key,
        )
        invoice_no = f"POS-{date.today():%Y%m%d}-{seq:05d}"
        total = sum((products[sku]["sell_price"] * qty for sku, qty in normalized.items()), Decimal("0"))
        if total <= 0:
            raise ValueError("Total penjualan harus lebih dari 0.")
        sale = await conn.fetchrow(
            """
            INSERT INTO sales
                (tenant_id,invoice_no,cashier_id,channel,status,payment_method,subtotal,discount,total,idempotency_key)
            VALUES ($1,$2,$3,$4,'posted',$5,$6,0,$6,$7::uuid)
            RETURNING *
            """, ctx["tenant_id"], invoice_no, ctx["user_id"], channel, payment_method, total, idempotency_key,
        )
        for sku, qty in normalized.items():
            product = products[sku]
            line_total = product["sell_price"] * qty
            stock_after = product["stock_on_hand"] - qty
            item = await conn.fetchrow(
                """
                INSERT INTO sale_items
                    (tenant_id,sale_id,product_id,sku_snapshot,product_name_snapshot,qty,unit_price,unit_cost,discount,line_total)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,0,$9) RETURNING id
                """, ctx["tenant_id"], sale["id"], product["id"], sku, product["name"], qty,
                product["sell_price"], product["cost_price"], line_total,
            )
            await conn.execute(
                """UPDATE products SET stock_on_hand=$3,updated_at=NOW()
                   WHERE tenant_id=$1 AND id=$2""", ctx["tenant_id"], product["id"], stock_after,
            )
            await conn.execute(
                """
                INSERT INTO stock_movements
                    (tenant_id,product_id,created_by,movement_type,qty_delta,stock_after,reference_type,reference_id,note)
                VALUES ($1,$2,$3,'sale',$4,$5,'sale',$6,$7)
                """, ctx["tenant_id"], product["id"], ctx["user_id"], -qty, stock_after,
                sale["id"], f"{invoice_no} / item {item['id']}",
            )
        await conn.execute(
            """
            INSERT INTO sale_payments (tenant_id,sale_id,method,amount,reference)
            VALUES ($1,$2,$3,$4,$5)
            """, ctx["tenant_id"], sale["id"], payment_method, total, invoice_no,
        )
        await conn.execute(
            """
            INSERT INTO transactions
                (tenant_id,created_by,type,amount,category,description,reference_type,reference_id,source_channel)
            VALUES ($1,$2,'income',$3,'Penjualan POS',$4,'sale',$5,$6)
            """, ctx["tenant_id"], ctx["user_id"], total, f"Penjualan {invoice_no}", sale["id"], channel,
        )
        await _audit(conn, ctx, "POST_SALE", "sale", sale["id"], {"invoice_no": invoice_no, "total": total, "channel": channel})
        return ctx, sale, False


async def void_sale(identity, invoice_no: str, reason: str = "Dibatalkan oleh operator"):
    invoice_no = (invoice_no or "").strip().upper()[:40]
    if not invoice_no:
        raise ValueError("Nomor invoice wajib diisi.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.void")
        sale = await conn.fetchrow(
            """SELECT * FROM sales WHERE tenant_id=$1 AND invoice_no=$2 FOR UPDATE""",
            ctx["tenant_id"], invoice_no,
        )
        if not sale:
            raise LookupError("Invoice tidak ditemukan.")
        if sale["status"] == "void":
            return ctx, sale, True
        items = await conn.fetch(
            """SELECT si.product_id,si.qty,p.sku,p.stock_on_hand
               FROM sale_items si JOIN products p ON p.id=si.product_id
               WHERE si.tenant_id=$1 AND si.sale_id=$2 FOR UPDATE OF p""",
            ctx["tenant_id"], sale["id"],
        )
        for item in items:
            stock_after = item["stock_on_hand"] + item["qty"]
            await conn.execute(
                "UPDATE products SET stock_on_hand=$3,updated_at=NOW() WHERE tenant_id=$1 AND id=$2",
                ctx["tenant_id"], item["product_id"], stock_after,
            )
            await conn.execute(
                """INSERT INTO stock_movements
                   (tenant_id,product_id,created_by,movement_type,qty_delta,stock_after,reference_type,reference_id,note)
                   VALUES ($1,$2,$3,'void',$4,$5,'sale',$6,$7)""",
                ctx["tenant_id"], item["product_id"], ctx["user_id"], item["qty"],
                stock_after, sale["id"], (reason or "Pembatalan penjualan")[:300],
            )
        sale = await conn.fetchrow(
            "UPDATE sales SET status='void',voided_at=NOW() WHERE tenant_id=$1 AND id=$2 RETURNING *",
            ctx["tenant_id"], sale["id"],
        )
        await conn.execute("UPDATE sale_payments SET status='void' WHERE tenant_id=$1 AND sale_id=$2", ctx["tenant_id"], sale["id"])
        await conn.execute("UPDATE transactions SET status='void' WHERE tenant_id=$1 AND reference_type='sale' AND reference_id=$2", ctx["tenant_id"], sale["id"])
        await _audit(conn, ctx, "VOID_SALE", "sale", sale["id"], {"invoice_no": invoice_no, "reason": reason})
        return ctx, sale, False


async def pos_summary(identity, start_date=None, end_date=None):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "pos.read")
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE s.status='posted') AS sales_count,
                   COALESCE(SUM(s.total) FILTER (WHERE s.status='posted'),0) AS revenue,
                   COALESCE(SUM(COALESCE(item_totals.gross_profit,0))
                            FILTER (WHERE s.status='posted'),0) AS gross_profit
            FROM sales s
            LEFT JOIN LATERAL (
                SELECT SUM((si.unit_price-si.unit_cost)*si.qty-si.discount) AS gross_profit
                FROM sale_items si WHERE si.tenant_id=s.tenant_id AND si.sale_id=s.id
            ) item_totals ON TRUE
            WHERE s.tenant_id=$1
              AND ($2::date IS NULL OR sold_at::date >= $2::date)
              AND ($3::date IS NULL OR sold_at::date <= $3::date)
            """, ctx["tenant_id"], start_date, end_date,
        )
        low_stock = await conn.fetchval(
            """SELECT COUNT(*) FROM products WHERE tenant_id=$1 AND is_active AND stock_on_hand<=min_stock""",
            ctx["tenant_id"],
        )
        return ctx, row, int(low_stock or 0)


async def start_opname(identity, note: str = ""):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "opname.write")
        current = await conn.fetchrow(
            """SELECT id FROM stock_opname_sessions WHERE tenant_id=$1 AND status='draft' ORDER BY started_at DESC LIMIT 1""",
            ctx["tenant_id"],
        )
        if current:
            return ctx, current, True
        row = await conn.fetchrow(
            """INSERT INTO stock_opname_sessions (tenant_id,created_by,note)
               VALUES ($1,$2,$3) RETURNING *""", ctx["tenant_id"], ctx["user_id"], (note or "")[:300],
        )
        await _audit(conn, ctx, "START_OPNAME", "stock_opname", row["id"], {"note": note})
        return ctx, row, False


async def count_opname(identity, sku: str, counted_qty: Decimal):
    counted_qty = Decimal(counted_qty).quantize(Decimal("0.001"))
    if counted_qty < 0:
        raise ValueError("Jumlah fisik tidak boleh negatif.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "opname.write")
        session = await conn.fetchrow(
            """SELECT * FROM stock_opname_sessions WHERE tenant_id=$1 AND status='draft' ORDER BY started_at DESC LIMIT 1 FOR UPDATE""",
            ctx["tenant_id"],
        )
        if not session:
            raise LookupError("Belum ada sesi opname draft. Jalankan opname mulai.")
        product = await conn.fetchrow(
            """SELECT * FROM products WHERE tenant_id=$1 AND sku=$2 AND is_active=TRUE""",
            ctx["tenant_id"], normalize_sku(sku),
        )
        if not product:
            raise LookupError("Produk tidak ditemukan.")
        item = await conn.fetchrow(
            """
            INSERT INTO stock_opname_items (tenant_id,session_id,product_id,system_qty,counted_qty)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (session_id,product_id)
            DO UPDATE SET counted_qty=EXCLUDED.counted_qty,updated_at=NOW()
            RETURNING *, counted_qty-system_qty AS variance
            """, ctx["tenant_id"], session["id"], product["id"], product["stock_on_hand"], counted_qty,
        )
        return ctx, session, product, item


async def post_opname(identity):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "opname.write")
        session = await conn.fetchrow(
            """SELECT * FROM stock_opname_sessions WHERE tenant_id=$1 AND status='draft' ORDER BY started_at DESC LIMIT 1 FOR UPDATE""",
            ctx["tenant_id"],
        )
        if not session:
            raise LookupError("Tidak ada sesi opname draft.")
        items = await conn.fetch(
            """SELECT soi.*,p.sku,p.name FROM stock_opname_items soi
               JOIN products p ON p.id=soi.product_id
               WHERE soi.tenant_id=$1 AND soi.session_id=$2 ORDER BY p.sku FOR UPDATE OF p""",
            ctx["tenant_id"], session["id"],
        )
        if not items:
            raise ValueError("Sesi opname belum memiliki hitungan produk.")
        for item in items:
            delta = item["counted_qty"] - item["system_qty"]
            await conn.execute(
                """UPDATE products SET stock_on_hand=$3,updated_at=NOW()
                   WHERE tenant_id=$1 AND id=$2""", ctx["tenant_id"], item["product_id"], item["counted_qty"],
            )
            if delta:
                await conn.execute(
                    """
                    INSERT INTO stock_movements
                        (tenant_id,product_id,created_by,movement_type,qty_delta,stock_after,reference_type,reference_id,note)
                    VALUES ($1,$2,$3,'opname',$4,$5,'stock_opname',$6,'Posting stock opname')
                    """, ctx["tenant_id"], item["product_id"], ctx["user_id"], delta,
                    item["counted_qty"], session["id"],
                )
        posted = await conn.fetchrow(
            """UPDATE stock_opname_sessions SET status='posted',posted_at=NOW()
               WHERE tenant_id=$1 AND id=$2 RETURNING *""", ctx["tenant_id"], session["id"],
        )
        await _audit(conn, ctx, "POST_OPNAME", "stock_opname", session["id"], {"items": len(items)})
        return ctx, posted, items


async def list_staff(identity):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.read")
        rows = await conn.fetch(
            """
            SELECT u.id,u.telegram_user_id,u.full_name,u.access_status,
                   tm.role,tm.is_active,wi.phone_number AS whatsapp_phone
            FROM tenant_members tm
            JOIN users u ON u.id=tm.user_id
            LEFT JOIN LATERAL (
                SELECT phone_number
                FROM whatsapp_identities
                WHERE user_id=u.id AND is_active=TRUE
                ORDER BY last_seen_at DESC
                LIMIT 1
            ) wi ON TRUE
            WHERE tm.tenant_id=$1 ORDER BY tm.role,u.full_name
            """, ctx["tenant_id"],
        )
        return ctx, rows


async def create_staff_invite(identity, full_name: str, role: str):
    """Create a short-lived, one-use WhatsApp employee invitation."""
    role = (role or "").upper()
    full_name = (full_name or "").strip()[:160]
    if role not in STAFF_ROLES or len(full_name) < 2:
        raise ValueError("Nama atau role karyawan tidak valid.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.write")
        if not can_manage_role(ctx["role"], role):
            raise PermissionError("Anda tidak boleh membuat role tersebut.")
        for _ in range(5):
            code = f"{secrets.randbelow(100_000_000):08d}"
            code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT * FROM app_create_staff_invite($1,$2,$3)",
                        code_hash, full_name, role,
                    )
                return ctx, row, code
            except Exception as exc:
                if "uq_staff_invites_unused_code" not in str(exc):
                    raise
        raise RuntimeError("Tidak dapat membuat kode undangan unik. Coba lagi.")


async def claim_staff_invite(whatsapp_jid: str, code: str):
    """Join the pending WhatsApp identity to the invite's tenant."""
    normalized = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(normalized) != 8:
        raise ValueError("Kode undangan harus 8 digit.")
    code_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    try:
        async with db.pool().acquire() as conn:
            async with conn.transaction():
                return await conn.fetchrow(
                    "SELECT * FROM app_claim_staff_invite($1,$2)",
                    whatsapp_jid, code_hash,
                )
    except Exception as exc:
        message = str(exc)
        if "INVITE_NOT_FOUND_OR_EXPIRED" in message:
            raise LookupError("Kode undangan salah, sudah dipakai, atau kedaluwarsa.") from exc
        if "ACCOUNT_ALREADY_ASSIGNED" in message:
            raise ValueError("Nomor WhatsApp ini sudah terikat pada akun/usaha lain.") from exc
        if "WA_USER_NOT_FOUND" in message:
            raise LookupError("Identitas WhatsApp belum terdaftar.") from exc
        raise


def _staff_uuid(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("ID karyawan tidak valid.") from exc


async def set_staff_role_by_id(identity, staff_user_id, role: str):
    role = (role or "").upper()
    staff_user_id = _staff_uuid(staff_user_id)
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.write")
        target = await conn.fetchrow(
            """SELECT u.id,tm.role FROM users u JOIN tenant_members tm ON tm.user_id=u.id
               WHERE tm.tenant_id=$1 AND u.id=$2 AND tm.is_active=TRUE FOR UPDATE OF tm""",
            ctx["tenant_id"], staff_user_id,
        )
        if not target:
            raise LookupError("Karyawan tidak ditemukan pada usaha ini.")
        if target["role"] == "OWNER" or not can_manage_role(ctx["role"], role):
            raise PermissionError("Anda tidak boleh mengubah karyawan/role tersebut.")
        await conn.execute(
            "UPDATE tenant_members SET role=$3 WHERE tenant_id=$1 AND user_id=$2",
            ctx["tenant_id"], staff_user_id, role,
        )
        await _audit(conn, ctx, "CHANGE_STAFF_ROLE", "user", staff_user_id,
                     {"old": target["role"], "new": role, "channel": "whatsapp"})
        return ctx, target, role


async def remove_staff_by_id(identity, staff_user_id):
    staff_user_id = _staff_uuid(staff_user_id)
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.write")
        target = await conn.fetchrow(
            """SELECT u.id,tm.role FROM users u JOIN tenant_members tm ON tm.user_id=u.id
               WHERE tm.tenant_id=$1 AND u.id=$2 AND tm.is_active=TRUE FOR UPDATE OF tm""",
            ctx["tenant_id"], staff_user_id,
        )
        if not target:
            raise LookupError("Karyawan tidak ditemukan pada usaha ini.")
        if target["role"] == "OWNER" or (ctx["role"] == "ADMIN" and target["role"] == "ADMIN"):
            raise PermissionError("Role ini tidak dapat Anda nonaktifkan.")
        await conn.execute(
            "UPDATE tenant_members SET is_active=FALSE WHERE tenant_id=$1 AND user_id=$2",
            ctx["tenant_id"], staff_user_id,
        )
        await conn.execute(
            "UPDATE users SET access_status='suspended',updated_at=NOW() WHERE id=$1",
            staff_user_id,
        )
        await _audit(conn, ctx, "REMOVE_STAFF", "user", staff_user_id,
                     {"old_role": target["role"], "channel": "whatsapp"})
        return ctx, target


async def add_staff(identity, telegram_user_id: int, full_name: str, role: str):
    role = (role or "").upper()
    full_name = (full_name or "").strip()[:160]
    if role not in STAFF_ROLES or len(full_name) < 2:
        raise ValueError("Nama atau role staf tidak valid.")
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.write")
        if not can_manage_role(ctx["role"], role):
            raise PermissionError("Anda tidak boleh membuat role tersebut.")
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_user_id=$1 FOR UPDATE", telegram_user_id)
        if user and user["is_master"]:
            raise PermissionError("Akun MASTER tidak dapat dijadikan staf.")
        if user and user["active_tenant_id"] and user["active_tenant_id"] != ctx["tenant_id"]:
            raise ValueError("Telegram ID sudah terikat pada usaha lain.")
        if not user:
            user = await conn.fetchrow(
                """INSERT INTO users (telegram_user_id,full_name,access_status,approved_at,approved_by,active_tenant_id)
                   VALUES ($1,$2,'active',NOW(),$3,$4) RETURNING *""",
                telegram_user_id, full_name, _actor(ctx), ctx["tenant_id"],
            )
        else:
            user = await conn.fetchrow(
                """UPDATE users SET full_name=$2,access_status='active',approved_at=COALESCE(approved_at,NOW()),
                                      approved_by=COALESCE(approved_by,$3),active_tenant_id=$4,deleted_at=NULL,updated_at=NOW()
                   WHERE id=$1 RETURNING *""", user["id"], full_name, _actor(ctx), ctx["tenant_id"],
            )
        await conn.execute(
            """INSERT INTO tenant_members (tenant_id,user_id,role,is_active)
               VALUES ($1,$2,$3,TRUE)
               ON CONFLICT (tenant_id,user_id) DO UPDATE SET role=EXCLUDED.role,is_active=TRUE""",
            ctx["tenant_id"], user["id"], role,
        )
        await _audit(conn, ctx, "ADD_STAFF", "user", user["id"], {"telegram_user_id": telegram_user_id, "role": role})
        return ctx, user, role


async def set_staff_role(identity, telegram_user_id: int, role: str):
    role = (role or "").upper()
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.write")
        target = await conn.fetchrow(
            """SELECT u.id,tm.role FROM users u JOIN tenant_members tm ON tm.user_id=u.id
               WHERE tm.tenant_id=$1 AND u.telegram_user_id=$2 AND tm.is_active=TRUE FOR UPDATE OF tm""",
            ctx["tenant_id"], telegram_user_id,
        )
        if not target:
            raise LookupError("Staf tidak ditemukan pada usaha ini.")
        if target["role"] == "OWNER" or not can_manage_role(ctx["role"], role):
            raise PermissionError("Anda tidak boleh mengubah staf/role tersebut.")
        await conn.execute(
            "UPDATE tenant_members SET role=$3 WHERE tenant_id=$1 AND user_id=$2",
            ctx["tenant_id"], target["id"], role,
        )
        await _audit(conn, ctx, "CHANGE_STAFF_ROLE", "user", target["id"], {"old": target["role"], "new": role})
        return ctx, target, role


async def remove_staff(identity, telegram_user_id: int):
    async with db.tenant_tx(identity) as (conn, ctx):
        require_permission(ctx, "staff.write")
        target = await conn.fetchrow(
            """SELECT u.id,tm.role FROM users u JOIN tenant_members tm ON tm.user_id=u.id
               WHERE tm.tenant_id=$1 AND u.telegram_user_id=$2 AND tm.is_active=TRUE FOR UPDATE OF tm""",
            ctx["tenant_id"], telegram_user_id,
        )
        if not target:
            raise LookupError("Staf tidak ditemukan pada usaha ini.")
        if target["role"] == "OWNER" or (ctx["role"] == "ADMIN" and target["role"] == "ADMIN"):
            raise PermissionError("Role ini tidak dapat Anda hapus.")
        await conn.execute("UPDATE tenant_members SET is_active=FALSE WHERE tenant_id=$1 AND user_id=$2", ctx["tenant_id"], target["id"])
        await conn.execute("UPDATE users SET access_status='suspended',updated_at=NOW() WHERE id=$1", target["id"])
        await _audit(conn, ctx, "REMOVE_STAFF", "user", target["id"], {"telegram_user_id": telegram_user_id, "old_role": target["role"]})
        return ctx, target
