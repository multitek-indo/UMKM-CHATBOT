"""Telegram command surface for UMKM Suite POS."""
from datetime import date
from decimal import Decimal
import html
import re
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import db, pos
from app.permissions import PermissionDenied, has_permission
from app.pos_parser import (
    parse_nonnegative_decimal, parse_nonnegative_money, parse_pipe_fields,
    parse_human_sale_items, parse_sale_items,
)


router = Router(name="pos-v070")


class POSWizard(StatesGroup):
    sale_items = State()
    sale_payment = State()
    sale_product = State()
    sale_confirm = State()
    product_name = State()
    product_price = State()
    product_stock = State()
    product_confirm = State()
    stock_product = State()
    stock_qty = State()
    stock_note = State()
    stock_confirm = State()


def rupiah(value) -> str:
    return "Rp{:,.0f}".format(Decimal(value or 0)).replace(",", ".")


def qty(value) -> str:
    return f"{Decimal(value):f}".rstrip("0").rstrip(".") or "0"


def payload(message: Message, command: str) -> str:
    return re.sub(rf"^/{re.escape(command)}(?:@\w+)?\s*", "", message.text or "", flags=re.I).strip()


async def active_identity(message: Message):
    tg = message.from_user
    await db.ensure_user(tg.id, tg.full_name if tg else None)
    access = await db.get_access(tg.id)
    if access["is_master"]:
        await message.answer("Masuk ke salah satu user melalui menu 🛠 Login User sebelum membuka POS.")
        return None
    if access["access_status"] != "active" or not access["active_tenant_id"]:
        await message.answer("Akun belum aktif atau belum memiliki usaha.")
        return None
    return tg.id


async def active_callback(callback: CallbackQuery):
    tg = callback.from_user
    await db.ensure_user(tg.id, tg.full_name if tg else None)
    access = await db.get_access(tg.id)
    if access["is_master"]:
        await callback.answer("Masuk sebagai user usaha terlebih dahulu.", show_alert=True)
        return None
    if access["access_status"] != "active" or not access["active_tenant_id"]:
        await callback.answer("Akun belum aktif atau belum memiliki usaha.", show_alert=True)
        return None
    return tg.id, access


async def run(message: Message, action):
    identity = await active_identity(message)
    if identity is None:
        return None
    try:
        return await action(identity)
    except PermissionDenied as exc:
        await message.answer(f"⛔ {html.escape(str(exc))}")
    except PermissionError as exc:
        await message.answer(f"⛔ {html.escape(str(exc))}")
    except (ValueError, LookupError) as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
    return None


POS_HELP = """⚙️ <b>Fitur Lanjutan POS</b>

Cara termudah: ketik dengan bahasa biasa, misalnya:
<code>jual 1 susu tunai</code>
<code>berapa stok kopi susu?</code>
<code>laporan POS bulan ini</code>

Perintah lanjutan:

Transaksi:
<code>/sale KOPI:2,ROTI:1 | QRIS</code>
<code>/sale_void POS-YYYYMMDD-00001 | Alasan</code> (OWNER/ADMIN)
<code>/products</code> · <code>/stock_low</code> · <code>/pos_report today</code>

Produk & stok (OWNER/ADMIN/INVENTORY):
<code>/product_add SKU | Nama | Harga jual | Harga modal | Stok awal | Satuan | Stok minimum</code>
<code>/product_edit SKU | Nama | Harga jual | Harga modal | Satuan | Stok minimum</code>
<code>/product_off SKU</code>
<code>/stock SKU | Stok baru | Alasan</code>

Stock opname:
<code>/opname_start Catatan</code>
<code>/opname_count SKU | Jumlah fisik</code>
<code>/opname_post</code>

Staf (OWNER/ADMIN):
<code>/staff_list</code>
<code>/staff_add TELEGRAM_ID | Nama | ROLE</code>
<code>/staff_role TELEGRAM_ID | ROLE</code>
<code>/staff_remove TELEGRAM_ID</code>
ROLE: ADMIN, FINANCE, CASHIER, INVENTORY, VIEWER."""


def pos_menu_keyboard(role: str | None) -> InlineKeyboardMarkup:
    buttons = []
    if has_permission(role, "pos.sell"):
        buttons.append(("🧾 Penjualan Baru", "posux:sale"))
    if has_permission(role, "pos.read"):
        buttons.append(("📦 Daftar Produk", "posux:products"))
    if has_permission(role, "product.write"):
        buttons.append(("➕ Tambah Produk", "posux:product_add"))
    if has_permission(role, "stock.write"):
        buttons.append(("🧮 Atur Stok", "posux:stock"))
    if has_permission(role, "pos.read"):
        buttons.extend([
            ("📊 Laporan", "posux:report"),
            ("⚠️ Stok Rendah", "posux:low"),
        ])
    if has_permission(role, "staff.read"):
        buttons.append(("👥 Karyawan", "posux:staff"))
    rows = [
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in buttons[index:index + 2]]
        for index in range(0, len(buttons), 2)
    ]
    rows.append([InlineKeyboardButton(text="⚙️ Fitur Lanjutan", callback_data="posux:advanced")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 Tunai", callback_data="posux:pay:CASH"),
            InlineKeyboardButton(text="🏦 Transfer", callback_data="posux:pay:TRANSFER"),
        ],
        [
            InlineKeyboardButton(text="📱 QRIS", callback_data="posux:pay:QRIS"),
            InlineKeyboardButton(text="💳 Kartu", callback_data="posux:pay:CARD"),
        ],
        [InlineKeyboardButton(text="Lainnya", callback_data="posux:pay:OTHER")],
        [InlineKeyboardButton(text="❌ Batal", callback_data="posux:cancel")],
    ])


def confirm_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Simpan", callback_data=f"posux:{kind}:save")],
        [
            InlineKeyboardButton(text="✏️ Ubah", callback_data=f"posux:{kind}:edit"),
            InlineKeyboardButton(text="❌ Batal", callback_data="posux:cancel"),
        ],
    ])


def serializable_items(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        query = str(item.get("product_query") or item.get("sku") or "").strip()
        if query and item.get("qty") is not None:
            result.append({"product_query": query, "qty": str(item["qty"])})
    return result


def sale_preview_html(ctx: dict, preview: dict) -> str:
    lines = [f"🧾 <b>Periksa Penjualan · {html.escape(ctx['business_name'])}</b>", ""]
    for item in preview["items"]:
        lines.append(
            f"• <b>{html.escape(item['name'])}</b> × {qty(item['qty'])} {html.escape(item['unit'])}"
            f" = {rupiah(item['line_total'])}\n  Sisa stok: {qty(item['stock_after'])}"
        )
    lines.extend([
        "", f"<b>Total: {rupiah(preview['total'])}</b>",
        f"Pembayaran: <b>{html.escape(preview['payment_method'])}</b>",
        "", "Belum ada data yang disimpan. Pilih <b>Simpan</b> untuk melanjutkan.",
    ])
    return "\n".join(lines)


async def show_pos_menu(message: Message, access: dict):
    await message.answer(
        f"🛒 <b>POS · {html.escape(access['business_name'] or '-')}</b>\n\n"
        "Pilih yang ingin dikerjakan. Anda tidak perlu menghafal command.",
        reply_markup=pos_menu_keyboard(access["role"]),
        parse_mode="HTML",
    )


async def callback_result(callback: CallbackQuery, action):
    active = await active_callback(callback)
    if not active:
        return None
    try:
        return active, await action(active[0])
    except (PermissionDenied, PermissionError) as exc:
        await callback.message.answer(f"⛔ {html.escape(str(exc))}")
    except (ValueError, LookupError) as exc:
        await callback.message.answer(f"⚠️ {html.escape(str(exc))}")
    return None


async def send_products(message: Message, identity, *, low_only: bool = False):
    ctx, rows = await pos.list_products(identity, low_stock_only=low_only)
    if not rows:
        text = "✅ Tidak ada stok rendah." if low_only else "Belum ada produk. Tekan <b>➕ Tambah Produk</b>."
        await message.answer(text, parse_mode="HTML")
        return
    title = "⚠️ Stok Rendah" if low_only else f"📦 Produk · {html.escape(ctx['business_name'])}"
    lines = [f"<b>{title}</b>"]
    for row in rows:
        marker = " ⚠️" if row["stock_on_hand"] <= row["min_stock"] else ""
        lines.append(
            f"• <b>{html.escape(row['name'])}</b> · {qty(row['stock_on_hand'])} "
            f"{html.escape(row['unit'])} · {rupiah(row['sell_price'])}{marker}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


async def prepare_telegram_sale(
    message: Message, state: FSMContext, identity, items: list[dict], payment: str | None
):
    draft = serializable_items(items)
    try:
        ctx, preview = await pos.prepare_sale_preview(identity, draft, payment)
    except (PermissionDenied, PermissionError) as exc:
        await state.clear()
        await message.answer(f"⛔ {html.escape(str(exc))}")
        return
    except (ValueError, LookupError) as exc:
        await state.set_state(POSWizard.sale_items)
        await message.answer(
            f"⚠️ {html.escape(str(exc))}\n\nKetik kembali produk dan jumlahnya.\n"
            "Contoh: <code>2 kopi dan 1 roti</code>",
            parse_mode="HTML",
        )
        return

    if preview["unresolved"]:
        unresolved = preview["unresolved"][0]
        candidates = unresolved["candidates"]
        await state.update_data(
            sale_items=draft,
            sale_payment=payment,
            sale_item_index=unresolved["item_index"],
            sale_candidates=candidates,
        )
        if not candidates:
            await state.set_state(POSWizard.sale_items)
            await message.answer(
                f"⚠️ Produk <b>{html.escape(unresolved['query'])}</b> tidak ditemukan.\n"
                "Ketik nama yang tersedia atau buka Daftar Produk.",
                parse_mode="HTML",
            )
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{item['name']} · stok {qty(item['stock_on_hand'])}",
                callback_data=f"posux:sale_product:{index}",
            )]
            for index, item in enumerate(candidates[:8])
        ] + [[InlineKeyboardButton(text="❌ Batal", callback_data="posux:cancel")]])
        await state.set_state(POSWizard.sale_product)
        await message.answer(
            f"🔎 Produk <b>{html.escape(unresolved['query'])}</b> belum spesifik. Pilih produk:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    normalized = [{"sku": item["sku"], "qty": str(item["qty"])} for item in preview["items"]]
    await state.update_data(sale_items=normalized)
    if not payment:
        await state.set_state(POSWizard.sale_payment)
        await message.answer("💳 Pilih metode pembayaran:", reply_markup=payment_keyboard())
        return

    await state.update_data(sale_payment=payment, sale_preview=preview)
    await state.set_state(POSWizard.sale_confirm)
    await message.answer(
        sale_preview_html(ctx, preview),
        reply_markup=confirm_keyboard("sale"),
        parse_mode="HTML",
    )


@router.message(Command("pos"))
@router.message(F.text == "🛒 POS & Stok")
async def pos_help(message: Message, state: FSMContext):
    await state.clear()
    identity = await active_identity(message)
    if identity is not None:
        await show_pos_menu(message, await db.get_access(identity))


@router.callback_query(F.data == "posux:menu")
async def pos_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    active = await active_callback(callback)
    if active:
        await callback.answer()
        await show_pos_menu(callback.message, active[1])


@router.callback_query(F.data == "posux:advanced")
async def pos_advanced(callback: CallbackQuery):
    if await active_callback(callback):
        await callback.answer()
        await callback.message.answer(POS_HELP, parse_mode="HTML")


@router.callback_query(F.data == "posux:cancel")
async def pos_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    active = await active_callback(callback)
    await callback.answer("Dibatalkan")
    if active:
        await callback.message.answer("❌ Proses dibatalkan.", reply_markup=pos_menu_keyboard(active[1]["role"]))


@router.callback_query(F.data == "posux:products")
async def products_callback(callback: CallbackQuery):
    result = await callback_result(callback, lambda ident: pos.list_products(ident))
    if result:
        await callback.answer()
        await send_products(callback.message, result[0][0])


@router.callback_query(F.data == "posux:low")
async def low_callback(callback: CallbackQuery):
    result = await callback_result(callback, lambda ident: pos.list_products(ident, low_stock_only=True))
    if result:
        await callback.answer()
        await send_products(callback.message, result[0][0], low_only=True)


@router.callback_query(F.data == "posux:staff")
async def staff_callback(callback: CallbackQuery):
    if await active_callback(callback):
        await callback.answer()
        await callback.message.answer(
            "👥 <b>Karyawan</b>\n\nCara termudah menambah dan mengelola karyawan adalah melalui WhatsApp:\n"
            "<b>MENU → POS & Stok → Kelola Karyawan</b>.\n\n"
            "Di sana pemilik dapat membuat kode undangan tanpa bantuan MASTER.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "posux:report")
async def report_menu_callback(callback: CallbackQuery):
    if await active_callback(callback):
        await callback.answer()
        await callback.message.answer(
            "📊 Pilih periode laporan:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Hari Ini", callback_data="posux:report:today"),
                    InlineKeyboardButton(text="Bulan Ini", callback_data="posux:report:month"),
                ],
                [InlineKeyboardButton(text="⬅️ Menu POS", callback_data="posux:menu")],
            ]),
        )


@router.callback_query(F.data.startswith("posux:report:"))
async def report_callback(callback: CallbackQuery):
    mode = callback.data.rsplit(":", 1)[-1]
    today = date.today()
    start = today if mode == "today" else today.replace(day=1)
    result = await callback_result(callback, lambda ident: pos.pos_summary(ident, start, today))
    if result:
        await callback.answer()
        _, (ctx, row, low) = result
        await callback.message.answer(
            f"📊 <b>Laporan POS · {html.escape(ctx['business_name'])}</b>\n"
            f"Periode: {start:%d-%m-%Y} s.d. {today:%d-%m-%Y}\n"
            f"Transaksi: <b>{row['sales_count']}</b>\nOmzet: <b>{rupiah(row['revenue'])}</b>\n"
            f"Laba kotor: <b>{rupiah(row['gross_profit'])}</b>\nStok rendah: <b>{low}</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "posux:sale")
async def sale_start_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    if not has_permission(active[1]["role"], "pos.sell"):
        await callback.answer("Peran Anda tidak boleh membuat penjualan.", show_alert=True)
        return
    await state.clear()
    await state.set_state(POSWizard.sale_items)
    await callback.answer()
    await callback.message.answer(
        "🧾 <b>Penjualan Baru · Langkah 1/2</b>\n\nKetik produk dan jumlahnya.\n"
        "Contoh: <code>2 kopi dan 1 roti</code>\n\nKetik <code>0</code> untuk batal.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("posux:pay:"), POSWizard.sale_payment)
async def sale_payment_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    payment = callback.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    await callback.answer()
    await prepare_telegram_sale(callback.message, state, active[0], data.get("sale_items") or [], payment)


@router.callback_query(F.data.startswith("posux:sale_product:"), POSWizard.sale_product)
async def sale_product_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    data = await state.get_data()
    try:
        candidate = data["sale_candidates"][int(callback.data.rsplit(":", 1)[-1])]
        items = list(data["sale_items"])
        item_index = int(data["sale_item_index"])
        items[item_index] = {"product_query": candidate["sku"], "qty": items[item_index]["qty"]}
    except (KeyError, ValueError, IndexError, TypeError):
        await callback.answer("Pilihan sudah kedaluwarsa.", show_alert=True)
        return
    await callback.answer()
    await prepare_telegram_sale(callback.message, state, active[0], items, data.get("sale_payment"))


@router.callback_query(F.data == "posux:product_add")
async def product_start_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    if not has_permission(active[1]["role"], "product.write"):
        await callback.answer("Peran Anda tidak boleh menambah produk.", show_alert=True)
        return
    await state.clear()
    await state.set_state(POSWizard.product_name)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Tambah Produk · Langkah 1/3</b>\n\nKetik nama produk. Satuan boleh ditambahkan setelah tanda |.\n"
        "Contoh: <code>Kopi Susu | cup</code>\n\nKetik <code>0</code> untuk batal.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "posux:stock")
async def stock_start_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    if not has_permission(active[1]["role"], "stock.write"):
        await callback.answer("Peran Anda tidak boleh mengubah stok.", show_alert=True)
        return
    await state.clear()
    await state.set_state(POSWizard.stock_product)
    await callback.answer()
    await callback.message.answer(
        "🧮 <b>Atur Stok · Langkah 1/3</b>\n\nKetik nama produk yang sudah dihitung fisik.\n"
        "Contoh: <code>Kopi Susu</code>\n\nKetik <code>0</code> untuk batal.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "posux:sale:save", POSWizard.sale_confirm)
async def sale_save_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    data = await state.get_data()
    try:
        _, row, repeated = await pos.create_sale(
            active[0], data.get("sale_items") or [], data.get("sale_payment") or "",
            "telegram", str(uuid.uuid4()),
        )
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await callback.answer("Penjualan gagal.", show_alert=True)
        await callback.message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.clear()
    await callback.answer("Penjualan tersimpan")
    suffix = " (permintaan yang sama sudah diproses)" if repeated else ""
    await callback.message.answer(
        f"✅ <b>Penjualan tersimpan</b>{suffix}\nInvoice: <code>{row['invoice_no']}</code>\n"
        f"Total: <b>{rupiah(row['total'])}</b>\nStok dan pemasukan sudah diperbarui.",
        reply_markup=pos_menu_keyboard(active[1]["role"]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "posux:sale:edit", POSWizard.sale_confirm)
async def sale_edit_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(POSWizard.sale_items)
    await callback.answer()
    await callback.message.answer(
        "✏️ Ketik ulang produk dan jumlahnya. Contoh: <code>2 kopi dan 1 roti</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "posux:product:save", POSWizard.product_confirm)
async def product_save_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    data = await state.get_data()
    try:
        _, row = await pos.add_product(
            active[0], data["product_sku"], data["product_name"],
            int(data["product_price"]), int(data.get("product_cost", 0)),
            Decimal(data["product_stock"]), data["product_unit"],
            Decimal(data.get("product_min", "0")),
        )
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await callback.answer("Produk gagal disimpan.", show_alert=True)
        await callback.message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.clear()
    await callback.answer("Produk tersimpan")
    await callback.message.answer(
        f"✅ Produk <b>{html.escape(row['name'])}</b> tersimpan.\n"
        f"SKU: <code>{row['sku']}</code> · Stok: {qty(row['stock_on_hand'])} {html.escape(row['unit'])}",
        reply_markup=pos_menu_keyboard(active[1]["role"]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "posux:product:edit", POSWizard.product_confirm)
async def product_edit_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(POSWizard.product_name)
    await callback.answer()
    await callback.message.answer("✏️ Ketik ulang nama produk. Contoh: <code>Kopi Susu | cup</code>", parse_mode="HTML")


@router.callback_query(F.data == "posux:stock:save", POSWizard.stock_confirm)
async def stock_save_callback(callback: CallbackQuery, state: FSMContext):
    active = await active_callback(callback)
    if not active:
        return
    data = await state.get_data()
    try:
        _, row, delta = await pos.set_stock(
            active[0], data["stock_sku"], Decimal(data["stock_qty"]), data["stock_note"]
        )
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await callback.answer("Stok gagal diperbarui.", show_alert=True)
        await callback.message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.clear()
    await callback.answer("Stok diperbarui")
    delta_text = qty(delta)
    if Decimal(delta) > 0:
        delta_text = "+" + delta_text
    await callback.message.answer(
        f"✅ Stok <b>{html.escape(row['name'])}</b> sekarang {qty(row['stock_on_hand'])} {html.escape(row['unit'])} "
        f"({delta_text}).",
        reply_markup=pos_menu_keyboard(active[1]["role"]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "posux:stock:edit", POSWizard.stock_confirm)
async def stock_edit_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(POSWizard.stock_product)
    await callback.answer()
    await callback.message.answer("✏️ Ketik ulang nama produk yang stoknya akan diatur.")


@router.message(Command("products"))
async def products(message: Message):
    result = await run(message, lambda ident: pos.list_products(ident))
    if not result:
        return
    ctx, rows = result
    if not rows:
        await message.answer("Belum ada produk. Gunakan /product_add.")
        return
    lines = [f"📦 <b>Produk · {html.escape(ctx['business_name'])}</b>"]
    for row in rows:
        marker = " ⚠️" if row["stock_on_hand"] <= row["min_stock"] else ""
        lines.append(f"• <code>{row['sku']}</code> — {html.escape(row['name'])} · {qty(row['stock_on_hand'])} {html.escape(row['unit'])} · {rupiah(row['sell_price'])}{marker}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("stock_low"))
async def stock_low(message: Message):
    result = await run(message, lambda ident: pos.list_products(ident, low_stock_only=True))
    if not result:
        return
    _, rows = result
    if not rows:
        await message.answer("✅ Tidak ada stok yang menyentuh batas minimum.")
        return
    await message.answer("⚠️ <b>Stok rendah</b>\n" + "\n".join(
        f"• <code>{r['sku']}</code> {html.escape(r['name'])}: {qty(r['stock_on_hand'])} / min {qty(r['min_stock'])}"
        for r in rows
    ), parse_mode="HTML")


@router.message(Command("product_add"))
async def product_add(message: Message):
    try:
        sku, name, sell, cost, opening, unit, minimum = parse_pipe_fields(payload(message, "product_add"), 7)
        values = (sku, name, parse_nonnegative_money(sell), parse_nonnegative_money(cost),
                  parse_nonnegative_decimal(opening), unit, parse_nonnegative_decimal(minimum))
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}\nContoh: <code>/product_add KOPI | Kopi Susu | 18000 | 9000 | 20 | cup | 5</code>", parse_mode="HTML")
        return
    result = await run(message, lambda ident: pos.add_product(ident, *values))
    if result:
        _, row = result
        await message.answer(f"✅ Produk <code>{row['sku']}</code> tersimpan. Stok {qty(row['stock_on_hand'])}, harga {rupiah(row['sell_price'])}.", parse_mode="HTML")


@router.message(Command("product_edit"))
async def product_edit(message: Message):
    try:
        sku, name, sell, cost, unit, minimum = parse_pipe_fields(payload(message, "product_edit"), 6)
        values = dict(name=name, sell_price=parse_nonnegative_money(sell), cost_price=parse_nonnegative_money(cost), unit=unit, min_stock=parse_nonnegative_decimal(minimum))
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    result = await run(message, lambda ident: pos.update_product(ident, sku, **values))
    if result:
        _, row = result
        await message.answer(f"✅ Produk <code>{row['sku']}</code> diperbarui.", parse_mode="HTML")


@router.message(Command("product_off"))
async def product_off(message: Message):
    result = await run(message, lambda ident: pos.deactivate_product(ident, payload(message, "product_off")))
    if result:
        _, row = result
        await message.answer(f"✅ Produk <code>{row['sku']}</code> dinonaktifkan.", parse_mode="HTML")


@router.message(Command("stock"))
async def stock_set(message: Message):
    try:
        sku, amount, note = parse_pipe_fields(payload(message, "stock"), 3)
        new_stock = parse_nonnegative_decimal(amount)
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}\nContoh: <code>/stock KOPI | 24 | Hasil hitung gudang</code>", parse_mode="HTML")
        return
    result = await run(message, lambda ident: pos.set_stock(ident, sku, new_stock, note))
    if result:
        _, row, delta = result
        delta_text = qty(delta)
        if Decimal(delta) > 0:
            delta_text = "+" + delta_text
        await message.answer(f"✅ Stok <code>{row['sku']}</code> = {qty(row['stock_on_hand'])} ({delta_text} perubahan).", parse_mode="HTML")


@router.message(Command("sale"))
async def sale(message: Message, state: FSMContext):
    try:
        raw_items, payment = parse_pipe_fields(payload(message, "sale"), 2)
        items = parse_sale_items(raw_items)
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}\nContoh: <code>/sale KOPI:2,ROTI:1 | QRIS</code>", parse_mode="HTML")
        return
    identity = await active_identity(message)
    if identity is not None:
        await state.clear()
        await prepare_telegram_sale(message, state, identity, items, payment)


@router.message(Command("pos_report"))
async def pos_report(message: Message):
    mode = payload(message, "pos_report").lower() or "today"
    if mode not in {"today", "month"}:
        await message.answer("Gunakan <code>/pos_report today</code> atau <code>/pos_report month</code>.", parse_mode="HTML")
        return
    today = date.today()
    start = today if mode == "today" else today.replace(day=1)
    result = await run(message, lambda ident: pos.pos_summary(ident, start, today))
    if result:
        ctx, row, low = result
        await message.answer(f"📊 <b>Laporan POS · {html.escape(ctx['business_name'])}</b>\nPeriode: {start:%d-%m-%Y} s.d. {today:%d-%m-%Y}\nTransaksi: <b>{row['sales_count']}</b>\nOmzet: <b>{rupiah(row['revenue'])}</b>\nLaba kotor: <b>{rupiah(row['gross_profit'])}</b>\nSKU stok rendah: <b>{low}</b>", parse_mode="HTML")


@router.message(Command("sale_void"))
async def sale_void(message: Message):
    try:
        fields = parse_pipe_fields(payload(message, "sale_void"), (1, 2))
        invoice, reason = fields[0], fields[1] if len(fields) == 2 else "Dibatalkan melalui Telegram"
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    result = await run(message, lambda ident: pos.void_sale(ident, invoice, reason))
    if result:
        _, row, repeated = result
        await message.answer(("ℹ️ Invoice sudah void: " if repeated else "✅ Penjualan dibatalkan: ") + f"<code>{row['invoice_no']}</code>. Stok dikembalikan dan pemasukan di-void.", parse_mode="HTML")


@router.message(Command("opname_start"))
async def opname_start(message: Message):
    result = await run(message, lambda ident: pos.start_opname(ident, payload(message, "opname_start")))
    if result:
        _, row, existed = result
        await message.answer(("ℹ️ Melanjutkan" if existed else "✅ Memulai") + f" sesi opname <code>{row['id']}</code>.", parse_mode="HTML")


@router.message(Command("opname_count"))
async def opname_count(message: Message):
    try:
        sku, amount = parse_pipe_fields(payload(message, "opname_count"), 2)
        counted = parse_nonnegative_decimal(amount)
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    result = await run(message, lambda ident: pos.count_opname(ident, sku, counted))
    if result:
        _, _, product, item = result
        await message.answer(f"✅ <code>{product['sku']}</code>: sistem {qty(item['system_qty'])}, fisik {qty(item['counted_qty'])}, selisih {qty(item['variance'])}.", parse_mode="HTML")


@router.message(Command("opname_post"))
async def opname_post(message: Message):
    result = await run(message, lambda ident: pos.post_opname(ident))
    if result:
        _, row, items = result
        await message.answer(f"✅ Opname <code>{row['id']}</code> diposting. {len(items)} produk diperbarui atomik.", parse_mode="HTML")


@router.message(Command("staff_list"))
async def staff_list(message: Message):
    result = await run(message, lambda ident: pos.list_staff(ident))
    if result:
        _, rows = result
        await message.answer("👥 <b>Staf usaha</b>\n" + "\n".join(
            f"• {html.escape(r['full_name'] or '-')} · <code>{r['telegram_user_id'] or '-'}</code> · {r['role']} · {'aktif' if r['is_active'] else 'nonaktif'}"
            for r in rows
        ), parse_mode="HTML")


@router.message(Command("staff_add"))
async def staff_add(message: Message):
    try:
        tg_id, name, role = parse_pipe_fields(payload(message, "staff_add"), 3)
        tg_id = int(tg_id)
    except (ValueError, TypeError) as exc:
        await message.answer(f"⚠️ Format: <code>/staff_add 123456789 | Nama | CASHIER</code>\n{html.escape(str(exc))}", parse_mode="HTML")
        return
    result = await run(message, lambda ident: pos.add_staff(ident, tg_id, name, role))
    if result:
        _, user, assigned = result
        await message.answer(f"✅ {html.escape(user['full_name'] or name)} ditambahkan sebagai <b>{assigned}</b> pada usaha ini.", parse_mode="HTML")


@router.message(Command("staff_role"))
async def staff_role(message: Message):
    try:
        tg_id, role = parse_pipe_fields(payload(message, "staff_role"), 2)
        tg_id = int(tg_id)
    except (ValueError, TypeError) as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    result = await run(message, lambda ident: pos.set_staff_role(ident, tg_id, role))
    if result:
        await message.answer(f"✅ Role staf diubah menjadi <b>{result[2]}</b>.", parse_mode="HTML")


@router.message(Command("staff_remove"))
async def staff_remove(message: Message):
    try:
        tg_id = int(payload(message, "staff_remove"))
    except ValueError:
        await message.answer("Format: <code>/staff_remove TELEGRAM_ID</code>", parse_mode="HTML")
        return
    result = await run(message, lambda ident: pos.remove_staff(ident, tg_id))
    if result:
        await message.answer("✅ Staf dinonaktifkan dari usaha ini.")


def wizard_cancelled(text: str | None) -> bool:
    return (text or "").strip().casefold() in {"0", "batal", "cancel", "/cancel"}


async def cancel_wizard_message(message: Message, state: FSMContext):
    await state.clear()
    identity = await active_identity(message)
    if identity is not None:
        access = await db.get_access(identity)
        await message.answer("❌ Proses dibatalkan.", reply_markup=pos_menu_keyboard(access["role"]))


@router.message(POSWizard.sale_items)
async def sale_items_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    try:
        items = parse_human_sale_items(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"⚠️ {html.escape(str(exc))}\nContoh: <code>2 kopi dan 1 roti</code>",
            parse_mode="HTML",
        )
        return
    identity = await active_identity(message)
    if identity is not None:
        old = await state.get_data()
        await prepare_telegram_sale(message, state, identity, items, old.get("sale_payment"))


@router.message(POSWizard.sale_payment)
async def sale_payment_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    raw = re.sub(r"[^a-z0-9]+", " ", (message.text or "").casefold()).strip()
    method = {
        "1": "CASH", "cash": "CASH", "tunai": "CASH",
        "2": "TRANSFER", "transfer": "TRANSFER", "tf": "TRANSFER",
        "3": "QRIS", "qris": "QRIS", "qr": "QRIS",
        "4": "CARD", "card": "CARD", "kartu": "CARD", "debit": "CARD",
        "5": "OTHER", "other": "OTHER", "lainnya": "OTHER",
    }.get(raw)
    if not method:
        await message.answer("Pilih tombol pembayaran atau ketik: tunai, transfer, QRIS, kartu, atau lainnya.", reply_markup=payment_keyboard())
        return
    identity = await active_identity(message)
    if identity is not None:
        data = await state.get_data()
        await prepare_telegram_sale(message, state, identity, data.get("sale_items") or [], method)


@router.message(POSWizard.product_name)
async def product_name_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    parts = [part.strip() for part in (message.text or "").split("|", 1)]
    name = parts[0]
    unit = parts[1] if len(parts) == 2 else "pcs"
    if not 2 <= len(name) <= 160 or not 1 <= len(unit) <= 30:
        await message.answer("⚠️ Ketik nama 2–160 karakter. Contoh: <code>Kopi Susu | cup</code>", parse_mode="HTML")
        return
    identity = await active_identity(message)
    if identity is None:
        return
    try:
        sku = await pos.suggest_product_sku(identity, name)
    except (PermissionDenied, PermissionError, ValueError, RuntimeError) as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.update_data(product_name=name, product_unit=unit, product_sku=sku)
    await state.set_state(POSWizard.product_price)
    await message.answer(
        f"➕ <b>Tambah Produk · Langkah 2/3</b>\n\nNama: <b>{html.escape(name)}</b>\n"
        f"SKU otomatis: <code>{sku}</code>\nSatuan: {html.escape(unit)}\n\n"
        "Ketik harga jual. Harga modal boleh ditambahkan setelah |.\n"
        "Contoh cepat: <code>18000</code>\nContoh lengkap: <code>18000 | 9000</code>",
        parse_mode="HTML",
    )


@router.message(POSWizard.product_price)
async def product_price_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    try:
        price_parts = [part.strip() for part in (message.text or "").split("|", 1)]
        price = parse_nonnegative_money(price_parts[0])
        cost = parse_nonnegative_money(price_parts[1]) if len(price_parts) == 2 else 0
        if price <= 0:
            raise ValueError("Harga jual harus lebih dari 0.")
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))} Contoh: <code>18000</code>.", parse_mode="HTML")
        return
    await state.update_data(product_price=price, product_cost=cost)
    await state.set_state(POSWizard.product_stock)
    await message.answer(
        "➕ <b>Tambah Produk · Langkah 3/3</b>\n\nKetik stok awal. Batas minimum boleh ditambahkan setelah |.\n"
        "Contoh cepat: <code>20</code>\nContoh lengkap: <code>20 | 5</code>",
        parse_mode="HTML",
    )


@router.message(POSWizard.product_stock)
async def product_stock_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    try:
        stock_parts = [part.strip() for part in (message.text or "").split("|", 1)]
        stock = parse_nonnegative_decimal(stock_parts[0])
        minimum = parse_nonnegative_decimal(stock_parts[1]) if len(stock_parts) == 2 else Decimal("0")
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.update_data(product_stock=str(stock), product_min=str(minimum))
    data = await state.get_data()
    await state.set_state(POSWizard.product_confirm)
    await message.answer(
        f"📦 <b>Periksa Produk</b>\n\nNama: <b>{html.escape(data['product_name'])}</b>\n"
        f"SKU: <code>{data['product_sku']}</code>\nHarga: <b>{rupiah(data['product_price'])}</b>\n"
        f"Harga modal: {rupiah(data['product_cost'])}\n"
        f"Stok: {qty(stock)} {html.escape(data['product_unit'])}\nBatas minimum: {qty(minimum)}\n\n"
        "Belum ada data yang disimpan.",
        reply_markup=confirm_keyboard("product"),
        parse_mode="HTML",
    )


@router.message(POSWizard.stock_product)
async def stock_product_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    identity = await active_identity(message)
    if identity is None:
        return
    try:
        _, product, candidates = await pos.resolve_product(identity, message.text or "")
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    if not product:
        if candidates:
            names = "\n".join(f"• {html.escape(item['name'])}" for item in candidates[:8])
            await message.answer(f"🔎 Nama belum spesifik. Ketik salah satu nama berikut:\n{names}", parse_mode="HTML")
        else:
            await message.answer("⚠️ Produk tidak ditemukan. Ketik nama lain.")
        return
    await state.update_data(stock_sku=product["sku"], stock_name=product["name"], stock_unit=product["unit"])
    await state.set_state(POSWizard.stock_qty)
    await message.answer(
        f"🧮 <b>Atur Stok · Langkah 2/3</b>\n\n{html.escape(product['name'])}\n"
        f"Stok sekarang: <b>{qty(product['stock_on_hand'])} {html.escape(product['unit'])}</b>\n\n"
        "Ketik jumlah stok fisik terbaru.",
        parse_mode="HTML",
    )


@router.message(POSWizard.stock_qty)
async def stock_qty_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    try:
        stock = parse_nonnegative_decimal(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.update_data(stock_qty=str(stock))
    await state.set_state(POSWizard.stock_note)
    await message.answer("🧮 <b>Atur Stok · Langkah 3/3</b>\n\nKetik alasan perubahan. Contoh: <code>hasil hitung gudang</code>", parse_mode="HTML")


@router.message(POSWizard.stock_note)
async def stock_note_step(message: Message, state: FSMContext):
    if wizard_cancelled(message.text):
        await cancel_wizard_message(message, state)
        return
    note = (message.text or "").strip()
    if len(note) < 2:
        await message.answer("⚠️ Alasan terlalu pendek. Contoh: <code>hasil hitung gudang</code>", parse_mode="HTML")
        return
    await state.update_data(stock_note=note[:300])
    data = await state.get_data()
    await state.set_state(POSWizard.stock_confirm)
    await message.answer(
        f"🧮 <b>Periksa Perubahan Stok</b>\n\nProduk: <b>{html.escape(data['stock_name'])}</b>\n"
        f"Stok fisik baru: <b>{qty(data['stock_qty'])} {html.escape(data['stock_unit'])}</b>\n"
        f"Alasan: {html.escape(note[:300])}\n\nBelum ada data yang disimpan.",
        reply_markup=confirm_keyboard("stock"),
        parse_mode="HTML",
    )
