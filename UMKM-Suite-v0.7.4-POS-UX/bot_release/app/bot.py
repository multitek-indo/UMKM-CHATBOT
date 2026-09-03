import asyncio
import html
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app import ai, db, exporter, pos
from app.pos_handlers import router as pos_router
from app.permissions import PermissionDenied, has_permission
from app.pos_parser import parse_human_sale_items, parse_quick_sale_request

TOKEN = os.environ["BOT_TOKEN"]
MASTER_ID = int(os.environ["MASTER_TELEGRAM_ID"])
JAKARTA = ZoneInfo("Asia/Jakarta")
UTC = ZoneInfo("UTC")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
router = Router()


def rupiah(value: int) -> str:
    sign = "-" if value < 0 else ""
    return sign + "Rp{:,.0f}".format(abs(value)).replace(",", ".")


def parse_amount(raw: str) -> int:
    s = raw.lower().strip().replace("rp", "").replace(" ", "")
    s = s.replace(",", ".")
    multiplier = 1
    for suffix, mult in (
        ("ribu", 1_000),
        ("rb", 1_000),
        ("juta", 1_000_000),
        ("jt", 1_000_000),
    ):
        if s.endswith(suffix):
            multiplier = mult
            s = s[:-len(suffix)]
            break

    if multiplier > 1:
        if not re.fullmatch(r"\d+(\.\d+)?", s):
            raise ValueError
        return int(float(s) * multiplier)

    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        raise ValueError
    return int(digits)


def telegram_payment(text: str) -> str | None:
    raw = re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()
    return {
        "cash": "CASH", "tunai": "CASH", "1": "CASH",
        "transfer": "TRANSFER", "tf": "TRANSFER", "2": "TRANSFER",
        "qris": "QRIS", "qr": "QRIS", "3": "QRIS",
        "card": "CARD", "kartu": "CARD", "debit": "CARD", "4": "CARD",
        "other": "OTHER", "lainnya": "OTHER", "5": "OTHER",
    }.get(raw)


def user_menu(support: bool = False, role: str | None = None):
    visible = lambda permission: role is None or has_permission(role, permission)
    buttons = []
    if visible("finance.write"):
        buttons.extend(["➕ Pemasukan", "➖ Pengeluaran"])
    if visible("finance.read"):
        buttons.append("📊 Ringkasan")
    buttons.append("⏰ Pengingat")
    if visible("finance.export"):
        buttons.append("📤 Export Data")
    buttons.append("🤖 Asisten AI")
    if visible("pos.read"):
        buttons.append("🛒 POS & Stok")
    buttons.append("🏪 Usaha Saya")
    keyboard = [
        [KeyboardButton(text=value) for value in buttons[index:index + 2]]
        for index in range(0, len(buttons), 2)
    ]
    if support:
        keyboard.append([KeyboardButton(text="👑 Kembali ke MASTER")])
    else:
        keyboard.append([KeyboardButton(text="🔗 Hubungkan WhatsApp")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder=(
            "Mode troubleshooting..." if support else "Pilih menu..."
        ),
    )


def master_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Pemilik Pending"), KeyboardButton(text="🏪 Semua Usaha")],
            [KeyboardButton(text="🛠 Kelola Usaha"), KeyboardButton(text="ℹ️ Status Sistem")],
            [KeyboardButton(text="➕ Tambah Pemilik Usaha")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Menu MASTER...",
    )


class ApproveUser(StatesGroup):
    business_name = State()


class PrecreateUser(StatesGroup):
    telegram_id = State()
    full_name = State()
    business_name = State()


class RenameBusiness(StatesGroup):
    name = State()


class MasterRenameBusiness(StatesGroup):
    name = State()


class AddTransaction(StatesGroup):
    amount = State()
    description = State()
    confirm = State()


class AddReminder(StatesGroup):
    message = State()
    due = State()


class ExportFinance(StatesGroup):
    start_date = State()
    end_date = State()
    choose_format = State()


class AIConfirmReminder(StatesGroup):
    confirm = State()


class AIConfirmSale(StatesGroup):
    confirm = State()


class AICompleteSale(StatesGroup):
    items = State()
    payment = State()
    product = State()


async def ensure_access(message: Message):
    tg = message.from_user
    await db.ensure_user(tg.id, tg.full_name if tg else None)
    return await db.get_access(tg.id)


async def require_normal_user(message: Message):
    access = await ensure_access(message)
    if access["is_master"]:
        await message.answer("Gunakan menu MASTER.", reply_markup=master_menu())
        return None
    if access["access_status"] != "active":
        await pending_message(message, access)
        return None
    if not access["active_tenant_id"]:
        await message.answer(
            "Akun Anda sudah aktif tetapi belum memiliki usaha. Hubungi MASTER."
        )
        return None
    return access


async def pending_message(message: Message, access):
    status = access["access_status"]
    if status == "pending":
        text = (
            "⏳ <b>Akses belum disetujui</b>\n\n"
            "Akun Anda sudah tercatat, tetapi belum dapat menggunakan fitur.\n"
            "MASTER harus menyetujui akun dan menentukan nama usaha terlebih dahulu.\n\n"
            f"Telegram ID Anda: <code>{message.from_user.id}</code>"
        )
    elif status == "suspended":
        text = (
            "⛔ <b>Akses sedang dinonaktifkan</b>\n\n"
            "Silakan hubungi administrator."
        )
    else:
        text = (
            "🚫 <b>Akses tidak tersedia</b>\n\n"
            "Akun ini telah dihapus/dinonaktifkan oleh administrator."
        )
    await message.answer(text, parse_mode="HTML")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    access = await ensure_access(message)

    if access["is_master"]:
        await message.answer(
            "👑 <b>MASTER ADMIN</b>\n\n"
            "Anda dapat menyetujui user baru, membuat user, menonaktifkan user, "
            "dan mengelola nama usaha.",
            reply_markup=master_menu(),
            parse_mode="HTML",
        )
        return

    if access["access_status"] != "active":
        await pending_message(message, access)
        return

    if not access["active_tenant_id"]:
        await message.answer(
            "Akun aktif tetapi belum memiliki usaha. Hubungi MASTER."
        )
        return

    if access["support_mode"]:
        channels = await db.get_user_channels(MASTER_ID, access["id"])
        tg_value = access["telegram_user_id"] if access["telegram_user_id"] is not None else "-"
        wa_value = channels["whatsapp_phone"] if channels and channels["whatsapp_phone"] else "-"
        await message.answer(
            "🛠 <b>MODE TROUBLESHOOTING AKTIF</b>\n\n"
            f"Anda sedang menggunakan bot sebagai user:\n"
            f"👤 <b>{html.escape(access['full_name'] or 'Tanpa nama')}</b>\n"
            f"🏪 <b>{html.escape(access['business_name'] or '-')}</b>\n"
            f"Telegram ID: <code>{tg_value}</code>\n"
            f"WhatsApp: <code>{wa_value}</code>\n\n"
            "Semua menu di bawah diarahkan ke tenant user tersebut. "
            "Perubahan yang dilakukan MASTER akan dicatat di audit log.",
            reply_markup=user_menu(support=True, role=access["role"]),
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"👋 Selamat datang.\n\n🏪 <b>{access['business_name']}</b>",
        reply_markup=user_menu(role=access["role"]),
        parse_mode="HTML",
    )


@router.message(Command("menu"))
async def show_menu_command(message: Message, state: FSMContext):
    await state.clear()
    access = await ensure_access(message)

    if access["is_master"]:
        await message.answer(
            "👑 Menu MASTER.",
            reply_markup=master_menu(),
        )
        return

    if access["access_status"] != "active":
        await pending_message(message, access)
        return

    if not access["active_tenant_id"]:
        await message.answer("Akun aktif tetapi belum memiliki usaha. Hubungi MASTER.")
        return

    if access["support_mode"]:
        await message.answer(
            "🛠 <b>MODE TROUBLESHOOTING</b>\n"
            f"👤 {html.escape(access['full_name'] or 'Tanpa nama')}\n"
            f"🏪 {html.escape(access['business_name'] or '-')}",
            reply_markup=user_menu(support=True, role=access["role"]),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"🏪 <b>{html.escape(access['business_name'] or '-')}</b>\n\nMenu diperbarui.",
            reply_markup=user_menu(role=access["role"]),
            parse_mode="HTML",
        )


@router.message(Command("export"))
async def export_command(message: Message, state: FSMContext):
    await export_finance_menu(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    access = await db.get_access(message.from_user.id)
    await message.answer(
        "Operasi dibatalkan.",
        reply_markup=(
            master_menu()
            if access and access["is_master"]
            else user_menu(
                support=bool(access and access["support_mode"]),
                role=access["role"] if access else None,
            )
        ),
    )


async def show_users(message: Message, pending_only=False, active_only=False):
    if message.from_user.id != MASTER_ID:
        return
    rows = await db.master_list_users(message.from_user.id)
    rows = [r for r in rows if not r["is_master"]]
    if pending_only:
        rows = [r for r in rows if r["access_status"] == "pending"]
    if active_only:
        rows = [r for r in rows if r["access_status"] == "active" and r["business_name"]]

    if not rows:
        await message.answer(
            "Tidak ada user pending." if pending_only else "Belum ada user."
        )
        return

    if pending_only:
        title = "⏳ <b>PEMILIK USAHA PENDING</b>"
    elif active_only:
        title = "🛠 <b>PILIH USAHA UNTUK DIKELOLA</b>"
    else:
        title = "🏪 <b>SEMUA USAHA / OWNER</b>"
    await message.answer(title, parse_mode="HTML")

    for row in rows[:50]:
        business = row["business_name"] or "-"
        status = row["access_status"]
        icon = {
            "pending": "⏳",
            "active": "✅",
            "suspended": "⛔",
            "deleted": "🚫",
        }.get(status, "•")

        buttons = []
        if status == "pending":
            buttons.append(
                InlineKeyboardButton(
                    text="✅ Setujui",
                    callback_data=f"adm_approve:{row['user_id']}",
                )
            )
        elif status == "active":
            buttons.append(
                InlineKeyboardButton(
                    text="⛔ Suspend",
                    callback_data=f"adm_suspend:{row['user_id']}",
                )
            )
        elif status == "suspended":
            buttons.append(
                InlineKeyboardButton(
                    text="✅ Aktifkan",
                    callback_data=f"adm_activate:{row['user_id']}",
                )
            )

        if row["business_name"]:
            buttons.append(
                InlineKeyboardButton(
                    text="✏️ Nama Usaha",
                    callback_data=f"adm_rename:{row['user_id']}",
                )
            )

        management_row = list(buttons)
        action_row = []

        if status == "active" and row["business_name"]:
            action_row.append(
                InlineKeyboardButton(
                    text="🛠 Login sebagai User",
                    callback_data=f"adm_support:{row['user_id']}",
                )
            )

        action_row.append(
            InlineKeyboardButton(
                text="🗑 Hapus Akses",
                callback_data=f"adm_delete:{row['user_id']}",
            )
        )

        keyboard_rows = []
        if management_row:
            keyboard_rows.append(management_row)
        keyboard_rows.append(action_row)

        kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        channels = await db.get_user_channels(message.from_user.id, row["user_id"])
        tg_value = row["telegram_user_id"] if row["telegram_user_id"] is not None else "-"
        wa_value = channels["whatsapp_phone"] if channels and channels["whatsapp_phone"] else "-"
        await message.answer(
            f"{icon} <b>{html.escape(row['full_name'] or 'Tanpa nama')}</b>\n"
            f"Telegram ID: <code>{tg_value}</code>\n"
            f"WhatsApp: <code>{wa_value}</code>\n"
            f"Usaha: <b>{html.escape(business)}</b>\n"
            f"Role: <b>{html.escape(row['role'] or 'CALON OWNER')}</b>\n"
            f"Status: <b>{status.upper()}</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )


@router.message(F.text.in_({"⏳ User Pending", "⏳ Pemilik Pending"}))
async def pending_users(message: Message):
    if message.from_user.id == MASTER_ID:
        await show_users(message, pending_only=True)


@router.message(F.text.in_({"👥 Semua User", "🏪 Semua Usaha"}))
async def all_users(message: Message):
    if message.from_user.id == MASTER_ID:
        await show_users(message, pending_only=False)


@router.message(F.text.in_({"🛠 Login User", "🛠 Kelola Usaha"}))
async def support_user_list(message: Message):
    if message.from_user.id == MASTER_ID:
        await show_users(message, active_only=True)


@router.callback_query(F.data.startswith("adm_support:"))
async def support_login(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return

    target_user_id = callback.data.split(":", 1)[1]

    try:
        support = await db.master_start_support(
            callback.from_user.id,
            target_user_id,
        )
    except Exception:
        logging.exception("Gagal memulai mode troubleshooting")
        await callback.answer(
            "Tidak dapat login sebagai user tersebut.",
            show_alert=True,
        )
        return

    await state.clear()
    channels = await db.get_user_channels(callback.from_user.id, target_user_id)
    tg_value = support["target_telegram_user_id"] if support["target_telegram_user_id"] is not None else "-"
    wa_value = channels["whatsapp_phone"] if channels and channels["whatsapp_phone"] else "-"
    await callback.message.answer(
        "🛠 <b>MODE TROUBLESHOOTING AKTIF</b>\n\n"
        f"👤 User: <b>{html.escape(support['target_full_name'] or 'Tanpa nama')}</b>\n"
        f"🏪 Usaha: <b>{html.escape(support['business_name'] or '-')}</b>\n"
        f"Telegram ID: <code>{tg_value}</code>\n"
        f"WhatsApp: <code>{wa_value}</code>\n\n"
        "MASTER sekarang dapat menggunakan menu user untuk troubleshooting.\n"
        "⚠️ Jika Anda membuat transaksi, reminder, mengubah nama usaha, "
        "atau tindakan lain, perubahan tersebut masuk ke data user dan "
        "dicatat sebagai tindakan MASTER.",
        reply_markup=user_menu(support=True),
        parse_mode="HTML",
    )
    await callback.answer("Mode troubleshooting aktif")


@router.message(F.text == "👑 Kembali ke MASTER")
@router.message(Command("master"))
async def support_logout(message: Message, state: FSMContext):
    if message.from_user.id != MASTER_ID:
        await message.answer("Perintah ini hanya untuk MASTER.")
        return

    await state.clear()
    support = await db.master_get_support(message.from_user.id)
    if support:
        await db.master_stop_support(message.from_user.id)
        await message.answer(
            "✅ Mode troubleshooting selesai.\n"
            "Anda kembali sebagai MASTER.",
            reply_markup=master_menu(),
        )
    else:
        await message.answer(
            "👑 Anda sudah berada pada mode MASTER.",
            reply_markup=master_menu(),
        )


@router.callback_query(F.data.startswith("adm_approve:"))
async def approve_begin(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    await state.clear()
    await state.update_data(target_user_id=user_id)
    await state.set_state(ApproveUser.business_name)
    await callback.message.answer(
        "Masukkan <b>nama usaha</b> untuk user ini.\n"
        "User baru akan aktif setelah nama usaha disimpan.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ApproveUser.business_name)
async def approve_save(message: Message, state: FSMContext):
    if message.from_user.id != MASTER_ID:
        return
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Nama usaha harus 2–100 karakter.")
        return
    data = await state.get_data()
    await db.master_approve_user(
        message.from_user.id,
        data["target_user_id"],
        name,
    )
    await state.clear()
    await message.answer(
        f"✅ User disetujui.\n🏪 Usaha: <b>{name}</b>\n\n"
        "User sekarang dapat menjalankan /start.",
        reply_markup=master_menu(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm_suspend:"))
async def suspend_user(callback: CallbackQuery):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    await db.master_set_user_status(callback.from_user.id, user_id, "suspended")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("⛔ Akses user dinonaktifkan.")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_activate:"))
async def activate_user(callback: CallbackQuery):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    await db.master_set_user_status(callback.from_user.id, user_id, "active")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Akses user diaktifkan kembali.")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_delete:"))
async def delete_user(callback: CallbackQuery):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⚠️ Ya, hapus akses",
                callback_data=f"adm_delete_confirm:{user_id}",
            ),
            InlineKeyboardButton(text="Batal", callback_data="adm_noop"),
        ]]
    )
    await callback.message.answer(
        "Data transaksi tidak akan dihapus.\n"
        "Hanya akses user yang dicabut (soft delete).",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_delete_confirm:"))
async def delete_user_confirm(callback: CallbackQuery):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    await db.master_set_user_status(callback.from_user.id, user_id, "deleted")
    await callback.message.edit_text(
        "🗑 Akses user dihapus.\nHistori transaksi tetap tersimpan."
    )
    await callback.answer()


@router.callback_query(F.data == "adm_noop")
async def noop(callback: CallbackQuery):
    await callback.message.edit_text("Dibatalkan.")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rename:"))
async def master_rename_begin(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MASTER_ID:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    await state.clear()
    await state.update_data(target_user_id=user_id)
    await state.set_state(MasterRenameBusiness.name)
    await callback.message.answer("Masukkan nama usaha baru:")
    await callback.answer()


@router.message(MasterRenameBusiness.name)
async def master_rename_save(message: Message, state: FSMContext):
    if message.from_user.id != MASTER_ID:
        return
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Nama usaha harus 2–100 karakter.")
        return
    data = await state.get_data()
    await db.master_rename_user_business(
        message.from_user.id,
        data["target_user_id"],
        name,
    )
    await state.clear()
    await message.answer(
        f"✅ Nama usaha diubah menjadi <b>{name}</b>.",
        reply_markup=master_menu(),
        parse_mode="HTML",
    )


@router.message(F.text.in_({"➕ Tambah User", "➕ Tambah Pemilik Usaha"}))
async def precreate_begin(message: Message, state: FSMContext):
    if message.from_user.id != MASTER_ID:
        return
    await state.clear()
    await state.set_state(PrecreateUser.telegram_id)
    await message.answer(
        "Masukkan <b>Telegram numeric ID</b> pemilik usaha.\n\n"
        "Akun ini otomatis menjadi <b>OWNER</b> dan administrator usaha sendiri.\n"
        "Pemilik tetap harus membuka bot dan menjalankan /start "
        "agar Telegram mengizinkan bot berinteraksi dengannya.",
        parse_mode="HTML",
    )


@router.message(PrecreateUser.telegram_id)
async def precreate_tgid(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Telegram ID harus berupa angka.")
        return
    tg_id = int(raw)
    if tg_id == MASTER_ID:
        await message.answer("ID tersebut adalah MASTER.")
        return
    await state.update_data(target_tg_id=tg_id)
    await state.set_state(PrecreateUser.full_name)
    await message.answer("Masukkan nama pemilik usaha:")


@router.message(PrecreateUser.full_name)
async def precreate_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Nama harus 2–100 karakter.")
        return
    await state.update_data(full_name=name)
    await state.set_state(PrecreateUser.business_name)
    await message.answer("Masukkan nama usahanya:")


@router.message(PrecreateUser.business_name)
async def precreate_business(message: Message, state: FSMContext):
    business = (message.text or "").strip()
    if len(business) < 2 or len(business) > 100:
        await message.answer("Nama usaha harus 2–100 karakter.")
        return
    data = await state.get_data()
    await db.master_precreate_user(
        message.from_user.id,
        data["target_tg_id"],
        data["full_name"],
        business,
    )
    await state.clear()
    await message.answer(
        "✅ Pemilik usaha dibuat dan diaktifkan sebagai <b>OWNER</b>.\n\n"
        f"Nama: <b>{data['full_name']}</b>\n"
        f"Telegram ID: <code>{data['target_tg_id']}</code>\n"
        f"Usaha: <b>{business}</b>\n\n"
        "Pemilik dapat membuka bot, menjalankan /start, lalu mengelola usaha dan karyawan sendiri.",
        reply_markup=master_menu(),
        parse_mode="HTML",
    )


@router.message(F.text == "ℹ️ Status Sistem")
async def status_system(message: Message):
    if message.from_user.id != MASTER_ID:
        return
    rows = await db.master_list_users(message.from_user.id)
    counts = {"pending": 0, "active": 0, "suspended": 0}
    for r in rows:
        if not r["is_master"] and r["access_status"] in counts:
            counts[r["access_status"]] += 1
    await message.answer(
        "ℹ️ <b>Status Platform</b>\n\n"
        f"✅ Aktif: {counts['active']}\n"
        f"⏳ Pending: {counts['pending']}\n"
        f"⛔ Suspended: {counts['suspended']}\n"
        f"🤖 AI: <b>{html.escape(ai.public_status())}</b>\n"
        f"👑 MASTER ID: <code>{MASTER_ID}</code>",
        parse_mode="HTML",
    )


@router.message(F.text == "🔗 Hubungkan WhatsApp")
@router.message(Command("linkwa"))
async def link_whatsapp(message: Message):
    if message.from_user.id == MASTER_ID:
        await message.answer(
            "Fitur link channel digunakan oleh user biasa. "
            "Keluar dari mode MASTER/troubleshooting lalu gunakan akun user."
        )
        return

    access = await require_normal_user(message)
    if not access:
        return

    try:
        code, expires_at = await db.create_whatsapp_link_code(message.from_user.id)
    except Exception:
        logging.exception("Gagal membuat kode link WhatsApp")
        await message.answer("Tidak dapat membuat kode link saat ini. Coba lagi.")
        return

    channels = await db.get_user_channels(message.from_user.id)
    if channels and channels["whatsapp_phone"]:
        await message.answer(
            "✅ Akun ini sudah terhubung dengan WhatsApp:\n"
            f"<code>{channels['whatsapp_phone']}</code>",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "🔗 <b>HUBUNGKAN WHATSAPP</b>\n\n"
        "1. Buka nomor WhatsApp Anda dan chat ke Asisten UMKM.\n"
        "2. Pilih <b>7 · Pengaturan & Akun</b>.\n"
        "3. Pilih <b>2 · Hubungkan dengan Telegram</b>.\n"
        "4. Masukkan kode berikut:\n\n"
        f"<code>{code}</code>\n\n"
        "⏳ Kode berlaku sekitar 10 menit dan hanya dapat digunakan sekali.\n"
        "Jangan bagikan kode ini kepada orang lain.",
        parse_mode="HTML",
    )


@router.message(F.text == "🏪 Usaha Saya")
async def own_business(message: Message):
    access = await require_normal_user(message)
    if not access:
        return
    kb = None
    if has_permission(access["role"], "business.write"):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✏️ Ubah Nama Usaha", callback_data="user_rename_business")
            ]]
        )
    await message.answer(
        f"🏪 <b>{access['business_name']}</b>\n\n"
        "Data usaha Anda terisolasi dari user/tenant lain.",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "user_rename_business")
async def own_rename_begin(callback: CallbackQuery, state: FSMContext):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    if not has_permission(access["role"], "business.write"):
        await callback.answer("Role Anda tidak boleh mengubah nama usaha.", show_alert=True)
        return
    await state.clear()
    await state.set_state(RenameBusiness.name)
    await callback.message.answer("Masukkan nama usaha baru:")
    await callback.answer()


@router.message(RenameBusiness.name)
async def own_rename_save(message: Message, state: FSMContext):
    access = await require_normal_user(message)
    if not access:
        return
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Nama usaha harus 2–100 karakter.")
        return
    await db.rename_own_business(message.from_user.id, name)
    await state.clear()
    await message.answer(
        f"✅ Nama usaha diubah menjadi <b>{name}</b>.",
        reply_markup=user_menu(support=(message.from_user.id == MASTER_ID)),
        parse_mode="HTML",
    )


async def tx_begin(message: Message, state: FSMContext, tx_type: str):
    access = await require_normal_user(message)
    if not access:
        return
    if not has_permission(access["role"], "finance.write"):
        await message.answer("⛔ Role Anda tidak boleh menulis transaksi keuangan manual.")
        return
    await state.clear()
    await state.update_data(tx_type=tx_type)
    await state.set_state(AddTransaction.amount)
    label = "pemasukan" if tx_type == "income" else "pengeluaran"
    await message.answer(
        f"Masukkan nominal {label}.\nContoh: 150000, 150rb, 1.5jt"
    )


@router.message(F.text == "➕ Pemasukan")
async def income_begin(message: Message, state: FSMContext):
    await tx_begin(message, state, "income")


@router.message(F.text == "➖ Pengeluaran")
async def expense_begin(message: Message, state: FSMContext):
    await tx_begin(message, state, "expense")


@router.message(AddTransaction.amount)
async def tx_amount(message: Message, state: FSMContext):
    try:
        amount = parse_amount(message.text or "")
    except ValueError:
        await message.answer("Nominal tidak dikenali.")
        return
    if amount <= 0 or amount > 999_999_999_999:
        await message.answer("Nominal tidak valid.")
        return
    await state.update_data(amount=amount)
    await state.set_state(AddTransaction.description)
    await message.answer("Masukkan keterangan transaksi:")


@router.message(AddTransaction.description)
async def tx_description(message: Message, state: FSMContext):
    description = (message.text or "").strip()
    if len(description) < 2:
        await message.answer("Keterangan terlalu pendek.")
        return
    data = await state.get_data()
    await state.update_data(description=description)
    await state.set_state(AddTransaction.confirm)
    label = "PEMASUKAN" if data["tx_type"] == "income" else "PENGELUARAN"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Simpan", callback_data="tx_confirm"),
            InlineKeyboardButton(text="❌ Batal", callback_data="tx_cancel"),
        ]]
    )
    await message.answer(
        f"🧾 <b>Konfirmasi {label}</b>\n\n"
        f"Nominal: <b>{rupiah(data['amount'])}</b>\n"
        f"Keterangan: {description}",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(AddTransaction.confirm, F.data == "tx_cancel")
async def tx_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Transaksi dibatalkan.")
    await callback.message.answer("Kembali ke menu.", reply_markup=user_menu(support=(callback.from_user.id == MASTER_ID)))
    await callback.answer()


@router.callback_query(AddTransaction.confirm, F.data == "tx_confirm")
async def tx_confirm(callback: CallbackQuery, state: FSMContext):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Akses tidak aktif", show_alert=True)
        return
    data = await state.get_data()
    row = await db.create_transaction(
        callback.from_user.id,
        data["tx_type"],
        data["amount"],
        data["description"],
        data.get("category", "Umum"),
    )
    await state.clear()
    label = "Pemasukan" if row["type"] == "income" else "Pengeluaran"
    await callback.message.edit_text(
        "✅ <b>Transaksi tersimpan</b>\n\n"
        f"{label}: <b>{rupiah(int(row['amount']))}</b>\n"
        f"Keterangan: {row['description']}",
        parse_mode="HTML",
    )
    await callback.message.answer("Silakan pilih menu.", reply_markup=user_menu(support=(callback.from_user.id == MASTER_ID)))
    await callback.answer()


MONTH_NAMES_ID = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


def _month_period(target: date):
    start = date(target.year, target.month, 1)
    if target.month == 12:
        next_month = date(target.year + 1, 1, 1)
    else:
        next_month = date(target.year, target.month + 1, 1)
    end = next_month - timedelta(days=1)
    label = f"{MONTH_NAMES_ID[target.month - 1]} {target.year}"
    return start, end, label


def _export_period_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗓 Bulan Ini", callback_data="export_period:this"),
                InlineKeyboardButton(text="🗓 Bulan Lalu", callback_data="export_period:last"),
            ],
            [
                InlineKeyboardButton(text="📚 Semua Data", callback_data="export_period:all"),
                InlineKeyboardButton(text="📅 Rentang Tanggal", callback_data="export_period:custom"),
            ],
            [InlineKeyboardButton(text="❌ Batal", callback_data="export_cancel")],
        ]
    )


def _export_format_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📗 Excel (.xlsx)", callback_data="export_format:xlsx"),
                InlineKeyboardButton(text="📕 PDF (.pdf)", callback_data="export_format:pdf"),
            ],
            [InlineKeyboardButton(text="❌ Batal", callback_data="export_cancel")],
        ]
    )


def _export_download_name(period_label: str, extension: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", period_label).strip("_") or "laporan"
    return f"Laporan_Keuangan_{slug}.{extension}"


async def _show_export_format(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(ExportFinance.choose_format)
    await message.answer(
        "📤 <b>Pilih Format Export</b>\n\n"
        f"Periode: <b>{html.escape(data['period_label'])}</b>\n\n"
        "Tersedia 2 format:",
        reply_markup=_export_format_keyboard(),
        parse_mode="HTML",
    )


@router.message(
    F.text.in_({
        "📤 Export Data",
        "Export",
        "export",
        "Export Data",
        "export data",
        "EXPORT",
    })
)
async def export_finance_menu(message: Message, state: FSMContext):
    access = await require_normal_user(message)
    if not access:
        return
    if not has_permission(access["role"], "finance.export"):
        await message.answer("⛔ Role Anda tidak boleh mengekspor laporan keuangan.")
        return
    await state.clear()
    await message.answer(
        "📤 <b>Export Data Keuangan</b>\n\n"
        "Pilih periode data yang ingin diexport.",
        reply_markup=_export_period_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("export_period:"))
async def export_choose_period(callback: CallbackQuery, state: FSMContext):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return

    choice = callback.data.split(":", 1)[1]
    today = datetime.now(JAKARTA).date()

    if choice == "this":
        start, end, label = _month_period(today)
        await state.update_data(start_date=start.isoformat(), end_date=end.isoformat(), period_label=label)
        await _show_export_format(callback.message, state)
    elif choice == "last":
        previous_day = date(today.year, today.month, 1) - timedelta(days=1)
        start, end, label = _month_period(previous_day)
        await state.update_data(start_date=start.isoformat(), end_date=end.isoformat(), period_label=label)
        await _show_export_format(callback.message, state)
    elif choice == "all":
        await state.update_data(start_date=None, end_date=None, period_label="Semua Data")
        await _show_export_format(callback.message, state)
    elif choice == "custom":
        await state.set_state(ExportFinance.start_date)
        await callback.message.answer(
            "📅 Masukkan <b>tanggal awal</b> dengan format:\n"
            "<code>DD-MM-YYYY</code>\n\nContoh: <code>01-08-2026</code>",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(ExportFinance.start_date)
async def export_custom_start(message: Message, state: FSMContext):
    access = await require_normal_user(message)
    if not access:
        return
    try:
        start = datetime.strptime((message.text or "").strip(), "%d-%m-%Y").date()
    except ValueError:
        await message.answer("Format tanggal salah. Gunakan DD-MM-YYYY, contoh 01-08-2026.")
        return

    await state.update_data(start_date=start.isoformat())
    await state.set_state(ExportFinance.end_date)
    await message.answer(
        "📅 Masukkan <b>tanggal akhir</b> dengan format:\n"
        "<code>DD-MM-YYYY</code>",
        parse_mode="HTML",
    )


@router.message(ExportFinance.end_date)
async def export_custom_end(message: Message, state: FSMContext):
    access = await require_normal_user(message)
    if not access:
        return
    try:
        end = datetime.strptime((message.text or "").strip(), "%d-%m-%Y").date()
    except ValueError:
        await message.answer("Format tanggal salah. Gunakan DD-MM-YYYY.")
        return

    data = await state.get_data()
    start = date.fromisoformat(data["start_date"])
    if end < start:
        await message.answer("Tanggal akhir tidak boleh lebih kecil dari tanggal awal.")
        return

    label = f"{start.strftime('%d-%m-%Y')} s.d. {end.strftime('%d-%m-%Y')}"
    await state.update_data(end_date=end.isoformat(), period_label=label)
    await _show_export_format(message, state)


@router.callback_query(F.data.startswith("export_format:"))
async def export_generate(callback: CallbackQuery, state: FSMContext):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return

    data = await state.get_data()
    period_label = data.get("period_label")
    if not period_label:
        await callback.answer("Periode export sudah kedaluwarsa. Buka menu Export Data lagi.", show_alert=True)
        await state.clear()
        return

    fmt = callback.data.split(":", 1)[1]
    if fmt not in {"xlsx", "pdf"}:
        await callback.answer("Format tidak valid", show_alert=True)
        return

    start_raw = data.get("start_date")
    end_raw = data.get("end_date")
    start = date.fromisoformat(start_raw) if start_raw else None
    end = date.fromisoformat(end_raw) if end_raw else None

    await callback.answer()
    status = await callback.message.answer(
        "⏳ Menyiapkan laporan keuangan...\n"
        f"Periode: {period_label}"
    )

    file_path = None
    try:
        ctx, rows = await db.finance_export_data(callback.from_user.id, start, end)
        generated_at = datetime.now(JAKARTA)

        if fmt == "xlsx":
            file_path = await asyncio.to_thread(
                exporter.build_excel_report,
                ctx["business_name"],
                period_label,
                generated_at,
                rows,
            )
            download_name = _export_download_name(period_label, "xlsx")
            format_name = "Excel"
        else:
            file_path = await asyncio.to_thread(
                exporter.build_pdf_report,
                ctx["business_name"],
                period_label,
                generated_at,
                rows,
            )
            download_name = _export_download_name(period_label, "pdf")
            format_name = "PDF"

        await callback.message.answer_document(
            document=FSInputFile(file_path, filename=download_name),
            caption=(
                f"Laporan Keuangan - {ctx['business_name']}\n"
                f"Periode: {period_label}\n"
                f"Format: {format_name}\n"
                f"Jumlah transaksi: {len(rows)}"
            ),
        )
        await db.log_finance_export(
            callback.from_user.id,
            fmt,
            period_label,
            len(rows),
        )
        await status.edit_text("✅ Laporan berhasil dibuat dan dikirim.")
        await state.clear()
    except Exception:
        logging.exception("Gagal membuat export keuangan")
        await status.edit_text(
            "❌ Gagal membuat laporan. Silakan coba lagi atau hubungi administrator."
        )
    finally:
        if file_path:
            exporter.remove_export(file_path)


@router.callback_query(F.data == "export_cancel")
async def export_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Export dibatalkan.", reply_markup=user_menu(support=(callback.from_user.id == MASTER_ID)))
    await callback.answer()


@router.message(F.text == "📊 Ringkasan")
async def summary(message: Message):
    access = await require_normal_user(message)
    if not access:
        return
    if not has_permission(access["role"], "finance.read"):
        await message.answer("⛔ Role Anda tidak boleh melihat ringkasan keuangan.")
        return
    ctx, totals = await db.monthly_summary(message.from_user.id)
    income = totals["income"]
    expense = totals["expense"]
    balance = income - expense
    await message.answer(
        f"📊 <b>Ringkasan Bulan Ini</b>\n"
        f"🏪 {ctx['business_name']}\n\n"
        f"💰 Pemasukan: <b>{rupiah(income)}</b>\n"
        f"💸 Pengeluaran: <b>{rupiah(expense)}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💵 Selisih: <b>{rupiah(balance)}</b>",
        reply_markup=user_menu(support=(message.from_user.id == MASTER_ID)),
        parse_mode="HTML",
    )


@router.message(F.text == "🤖 Asisten AI")
async def ai_help(message: Message):
    access = await require_normal_user(message)
    if not access:
        return
    if not ai.is_configured():
        await message.answer(
            "🤖 <b>Asisten AI belum dikonfigurasi</b>\n\n"
            "Administrator perlu memasang GROQ_API_KEY di server terlebih dahulu.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "🤖 <b>Asisten AI aktif</b>\n\n"
        "Anda bisa langsung mengetik dengan bahasa biasa, misalnya:\n\n"
        "• <code>catat pemasukan 50000 dari penjualan kopi</code>\n"
        "• <code>keluar 250rb beli bensin</code>\n"
        "• <code>ingatkan besok jam 9 bayar listrik</code>\n"
        "• <code>berapa pengeluaran saya bulan ini?</code>\n"
        "• <code>apa transaksi terakhir saya?</code>\n"
        "• <code>jual 2 KOPI bayar QRIS</code>\n"
        "• <code>berapa stok SKU KOPI?</code>\n"
        "• <code>laporan POS hari ini</code>\n"
        "• <code>bagaimana cara mengatur arus kas usaha kecil?</code>\n\n"
        "Untuk transaksi dan pengingat, bot tetap meminta konfirmasi sebelum menyimpan.",
        parse_mode="HTML",
    )


@router.message(F.text == "⏰ Pengingat")
async def reminders_menu(message: Message):
    access = await require_normal_user(message)
    if not access:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Buat Pengingat", callback_data="reminder_add")],
            [InlineKeyboardButton(text="📋 Daftar Pengingat", callback_data="reminder_list")],
        ]
    )
    await message.answer("⏰ <b>Pengingat</b>", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "reminder_add")
async def reminder_add(callback: CallbackQuery, state: FSMContext):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddReminder.message)
    await callback.message.answer("Ketik isi pengingat:")
    await callback.answer()


@router.message(AddReminder.message)
async def reminder_message(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Isi pengingat terlalu pendek.")
        return
    await state.update_data(message=text)
    await state.set_state(AddReminder.due)
    await message.answer(
        "Masukkan tanggal dan jam:\n<b>DD-MM-YYYY HH:MM</b>",
        parse_mode="HTML",
    )


@router.message(AddReminder.due)
async def reminder_due(message: Message, state: FSMContext):
    try:
        local_dt = datetime.strptime(
            (message.text or "").strip(),
            "%d-%m-%Y %H:%M",
        ).replace(tzinfo=JAKARTA)
    except ValueError:
        await message.answer("Format salah. Gunakan DD-MM-YYYY HH:MM")
        return
    if local_dt <= datetime.now(JAKARTA):
        await message.answer("Waktu pengingat harus di masa depan.")
        return
    data = await state.get_data()
    row = await db.create_reminder(
        message.from_user.id,
        data["message"],
        local_dt.astimezone(UTC),
    )
    await state.clear()
    await message.answer(
        "✅ <b>Pengingat dibuat</b>\n\n"
        f"📝 {row['message']}\n"
        f"🗓 {local_dt.strftime('%d-%m-%Y %H:%M')} WIB",
        reply_markup=user_menu(support=(message.from_user.id == MASTER_ID)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "reminder_list")
async def reminder_list(callback: CallbackQuery):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Tidak diizinkan", show_alert=True)
        return
    rows = await db.list_pending_reminders(callback.from_user.id)
    if not rows:
        await callback.message.answer("Belum ada pengingat aktif.")
        await callback.answer()
        return
    lines = ["⏰ <b>Pengingat Aktif</b>\n"]
    for i, row in enumerate(rows, 1):
        local_dt = row["due_at"].astimezone(JAKARTA)
        lines.append(
            f"{i}. {row['message']}\n"
            f"   🗓 {local_dt.strftime('%d-%m-%Y %H:%M')} WIB"
        )
    await callback.message.answer("\n\n".join(lines), parse_mode="HTML")
    await callback.answer()


@router.callback_query(AIConfirmReminder.confirm, F.data == "ai_reminder_confirm")
async def ai_reminder_confirm(callback: CallbackQuery, state: FSMContext):
    access = await db.get_access(callback.from_user.id)
    if not access or access["access_status"] != "active" or access["is_master"]:
        await callback.answer("Akses tidak aktif", show_alert=True)
        return

    data = await state.get_data()
    due_iso = data.get("reminder_at")
    reminder_message = data.get("reminder_message")

    try:
        due_local = datetime.fromisoformat(due_iso)
        if due_local.tzinfo is None:
            due_local = due_local.replace(tzinfo=JAKARTA)
        due_local = due_local.astimezone(JAKARTA)
    except Exception:
        await state.clear()
        await callback.message.edit_text("❌ Waktu pengingat tidak valid.")
        await callback.answer()
        return

    if due_local <= datetime.now(JAKARTA):
        await state.clear()
        await callback.message.edit_text("❌ Waktu pengingat sudah lewat.")
        await callback.answer()
        return

    row = await db.create_reminder(
        callback.from_user.id,
        reminder_message,
        due_local.astimezone(UTC),
    )
    await state.clear()

    await callback.message.edit_text(
        "✅ <b>Pengingat dibuat</b>\n\n"
        f"📝 {html.escape(row['message'])}\n"
        f"🗓 {due_local.strftime('%d-%m-%Y %H:%M')} WIB",
        parse_mode="HTML",
    )
    await callback.message.answer("Silakan pilih menu.", reply_markup=user_menu(support=(callback.from_user.id == MASTER_ID)))
    await callback.answer()


@router.callback_query(AIConfirmReminder.confirm, F.data == "ai_reminder_cancel")
async def ai_reminder_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Pengingat dibatalkan.")
    await callback.message.answer("Silakan pilih menu.", reply_markup=user_menu(support=(callback.from_user.id == MASTER_ID)))
    await callback.answer()


async def show_telegram_sale_preview(message: Message, state: FSMContext, items, payment: str | None):
    clean_items = []
    for item in (items or [])[:50]:
        query = str(item.get("product_query") or item.get("query") or item.get("name") or item.get("sku") or "").strip()
        if query and item.get("qty") is not None:
            clean_items.append({"product_query": query, "qty": str(item["qty"])})
    if not clean_items:
        await state.clear()
        await state.update_data(ai_sale_payment=payment)
        await state.set_state(AICompleteSale.items)
        await message.answer("🧾 Ketik produk dan jumlah.\nContoh: <code>Susu 1 cup, Roti Bakar 2</code>", parse_mode="HTML")
        return
    try:
        ctx, preview = await pos.prepare_sale_preview(message.chat.id, clean_items, payment)
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await state.clear()
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    if preview["unresolved"]:
        unresolved = preview["unresolved"][0]
        candidates = unresolved["candidates"]
        if not candidates:
            await state.clear()
            await state.update_data(ai_sale_payment=payment)
            await state.set_state(AICompleteSale.items)
            await message.answer(
                f"⚠️ Produk <b>{html.escape(unresolved['query'])}</b> tidak ditemukan.\n"
                "Ketik kembali produk memakai nama atau SKU yang terdaftar.",
                parse_mode="HTML",
            )
            return
        await state.clear()
        await state.update_data(
            ai_sale_items=clean_items,
            ai_sale_payment=payment,
            ai_unresolved_index=unresolved["item_index"],
            ai_candidates=candidates,
        )
        await state.set_state(AICompleteSale.product)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{item['name']} · {rupiah(item['sell_price'])}", callback_data=f"ai_product_{index}")]
            for index, item in enumerate(candidates)
        ] + [[InlineKeyboardButton(text="❌ Batal", callback_data="ai_sale_cancel")]])
        await message.answer(
            f"🔎 Ada beberapa produk yang mirip dengan <b>{html.escape(unresolved['query'])}</b>. Pilih produknya:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return
    if not preview["payment_method"]:
        await state.clear()
        await state.update_data(ai_sale_items=clean_items)
        await state.set_state(AICompleteSale.payment)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 Tunai", callback_data="ai_pay_CASH"), InlineKeyboardButton(text="📱 QRIS", callback_data="ai_pay_QRIS")],
            [InlineKeyboardButton(text="🏦 Transfer", callback_data="ai_pay_TRANSFER"), InlineKeyboardButton(text="💳 Kartu", callback_data="ai_pay_CARD")],
            [InlineKeyboardButton(text="Lainnya", callback_data="ai_pay_OTHER"), InlineKeyboardButton(text="❌ Batal", callback_data="ai_sale_cancel")],
        ])
        await message.answer("💳 Pilih metode pembayaran:", reply_markup=kb)
        return

    final_items = [{"sku": item["sku"], "qty": item["qty"]} for item in preview["items"]]
    await state.clear()
    await state.update_data(ai_sale_items=final_items, ai_sale_payment=preview["payment_method"], ai_sale_preview=preview)
    await state.set_state(AIConfirmSale.confirm)
    lines = [f"🤖 <b>Konfirmasi Penjualan · {html.escape(ctx['business_name'])}</b>", ""]
    for item in preview["items"]:
        lines.append(
            f"• <b>{html.escape(item['name'])}</b> × {item['qty']} {html.escape(item['unit'])} = "
            f"<b>{rupiah(item['line_total'])}</b>\n  Sisa stok: {item['stock_after']} {html.escape(item['unit'])}"
        )
    lines.extend(["", f"<b>TOTAL: {rupiah(preview['total'])}</b>", f"Pembayaran: <b>{preview['payment_method']}</b>"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Simpan", callback_data="ai_sale_confirm"),
        InlineKeyboardButton(text="✏️ Edit", callback_data="ai_sale_edit"),
        InlineKeyboardButton(text="❌ Batal", callback_data="ai_sale_cancel"),
    ]])
    await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.message(AICompleteSale.items)
async def ai_sale_items_followup(message: Message, state: FSMContext):
    data = await state.get_data()
    quick_sale = parse_quick_sale_request(message.text or "")
    if quick_sale:
        items, quick_payment = quick_sale
        await show_telegram_sale_preview(
            message, state, items, quick_payment or data.get("ai_sale_payment")
        )
        return
    try:
        items = parse_human_sale_items(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await show_telegram_sale_preview(message, state, items, data.get("ai_sale_payment"))


@router.message(AICompleteSale.payment)
async def ai_sale_payment_followup(message: Message, state: FSMContext):
    quick_sale = parse_quick_sale_request(message.text or "")
    if quick_sale:
        items, payment = quick_sale
        await show_telegram_sale_preview(message, state, items, payment)
        return
    method = telegram_payment(message.text or "")
    if not method:
        await message.answer("⚠️ Ketik tunai, transfer, QRIS, kartu, atau lainnya.")
        return
    data = await state.get_data()
    await show_telegram_sale_preview(message, state, data.get("ai_sale_items") or [], method)


@router.callback_query(AICompleteSale.payment, F.data.startswith("ai_pay_"))
async def ai_sale_payment_pick(callback: CallbackQuery, state: FSMContext):
    method = callback.data.removeprefix("ai_pay_")
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=None)
    await show_telegram_sale_preview(callback.message, state, data.get("ai_sale_items") or [], method)
    await callback.answer()


@router.callback_query(AICompleteSale.product, F.data.startswith("ai_product_"))
async def ai_sale_product_pick(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        index = int(callback.data.removeprefix("ai_product_"))
        product = data["ai_candidates"][index]
        item_index = int(data["ai_unresolved_index"])
        items = list(data["ai_sale_items"])
        items[item_index] = {"product_query": product["sku"], "qty": items[item_index]["qty"]}
    except (ValueError, IndexError, KeyError, TypeError):
        await callback.answer("Pilihan tidak valid", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await show_telegram_sale_preview(callback.message, state, items, data.get("ai_sale_payment"))
    await callback.answer()


@router.callback_query(AIConfirmSale.confirm, F.data == "ai_sale_edit")
async def ai_sale_edit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AICompleteSale.items)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✏️ Ketik ulang produk dan jumlah.\nContoh: <code>Susu 1 cup, Roti Bakar 2</code>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(AICompleteSale.payment, F.data == "ai_sale_cancel")
@router.callback_query(AICompleteSale.product, F.data == "ai_sale_cancel")
async def ai_sale_completion_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Penjualan POS dibatalkan.")
    await callback.answer()


@router.callback_query(AIConfirmSale.confirm, F.data == "ai_sale_confirm")
async def ai_sale_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        _, sale, repeated = await pos.create_sale(
            callback.from_user.id,
            data.get("ai_sale_items") or [],
            data.get("ai_sale_payment") or "",
            "telegram",
            str(uuid.uuid4()),
        )
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await callback.answer("Penjualan gagal", show_alert=True)
        await callback.message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    await state.clear()
    preview = data.get("ai_sale_preview") or {}
    lines = ["✅ <b>Penjualan tersimpan</b>", f"Invoice: <code>{sale['invoice_no']}</code>", ""]
    for item in preview.get("items") or []:
        lines.append(f"• {html.escape(item['name'])} × {item['qty']} {html.escape(item['unit'])} = {rupiah(item['line_total'])}")
    lines.extend(["", f"<b>TOTAL: {rupiah(sale['total'])}</b>", f"Pembayaran: <b>{html.escape(sale['payment_method'])}</b>", "Stok dan pemasukan telah diperbarui."])
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AIConfirmSale.confirm, F.data == "ai_sale_cancel")
async def ai_sale_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Penjualan POS dibatalkan.")
    await callback.answer()


async def handle_ai_text(message: Message, state: FSMContext):
    access = await require_normal_user(message)
    if not access:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Kirim pertanyaan atau perintah dalam bentuk teks.")
        return

    quick_sale = parse_quick_sale_request(text)
    if quick_sale:
        if not has_permission(access["role"], "pos.sell"):
            await message.answer("⛔ Peran Anda tidak boleh mencatat penjualan POS.")
            return
        items, payment = quick_sale
        await show_telegram_sale_preview(message, state, items, payment)
        return

    if not ai.is_configured():
        await message.answer(
            "🤖 Asisten AI belum dikonfigurasi oleh administrator.",
            reply_markup=user_menu(support=(message.from_user.id == MASTER_ID)),
        )
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    provider = ai.PROVIDER
    model = ai.MODEL
    try:
        finance_context = await db.ai_business_context(message.from_user.id)
        result = await ai.analyze_message(
            text,
            finance_context,
            datetime.now(JAKARTA).isoformat(),
        )

        await db.log_ai_usage(
            message.from_user.id,
            provider,
            result.model,
            result.intent,
            result.prompt_tokens,
            result.completion_tokens,
            result.total_tokens,
            True,
            None,
        )
    except ai.AIConfigurationError as exc:
        logging.warning("AI configuration error: %s", exc)
        try:
            await db.log_ai_usage(
                message.from_user.id,
                provider,
                model,
                "configuration_error",
                success=False,
                error_code="configuration_error",
            )
        except Exception:
            logging.exception("Gagal menulis AI usage log")
        await message.answer(
            "⚠️ Konfigurasi layanan AI belum valid. Hubungi administrator."
        )
        return
    except ai.AIServiceError as exc:
        logging.warning("AI service error: %s", exc)
        try:
            await db.log_ai_usage(
                message.from_user.id,
                provider,
                model,
                "service_error",
                success=False,
                error_code=str(exc)[:120],
            )
        except Exception:
            logging.exception("Gagal menulis AI usage log")
        await message.answer(
            "⚠️ Layanan AI sedang tidak dapat digunakan. Coba lagi beberapa saat."
        )
        return
    except Exception:
        logging.exception("Unexpected AI handler error")
        await message.answer("⚠️ Terjadi kesalahan saat memproses permintaan AI.")
        return

    if result.intent == "create_sale":
        if not has_permission(access["role"], "pos.sell"):
            await message.answer("⛔ Role Anda tidak boleh mencatat penjualan POS.")
            return
        await show_telegram_sale_preview(message, state, result.sale_items or [], result.payment_method)
        return

    if result.intent == "stock_query":
        query = result.product_query or result.product_sku or ""
        if not query:
            await message.answer("📦 Produk apa yang ingin dicek? Ketik nama atau SKU.")
            return
        try:
            _, product, candidates = await pos.resolve_product(message.from_user.id, query)
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await message.answer(f"⚠️ {html.escape(str(exc))}")
            return
        if product:
            await message.answer(f"📦 <code>{product['sku']}</code> — {html.escape(product['name'])}\nStok: <b>{product['stock_on_hand']} {html.escape(product['unit'])}</b>\nHarga: <b>{rupiah(product['sell_price'])}</b>", parse_mode="HTML")
        elif candidates:
            await message.answer(
                "🔎 Ada beberapa produk yang mirip:\n" + "\n".join(
                    f"• {html.escape(item['name'])} — <code>{item['sku']}</code>"
                    for item in candidates
                ),
                parse_mode="HTML",
            )
        else:
            await message.answer(f"Produk {html.escape(query)} tidak ditemukan.", parse_mode="HTML")
        return

    if result.intent == "pos_summary":
        try:
            today = date.today()
            start = today.replace(day=1) if result.report_period == "month" else today
            ctx, row, low = await pos.pos_summary(message.from_user.id, start, today)
        except (PermissionDenied, PermissionError, LookupError, ValueError) as exc:
            await message.answer(f"⛔ {html.escape(str(exc))}")
            return
        label = "bulan ini" if result.report_period == "month" else "hari ini"
        await message.answer(f"📊 <b>POS {label} · {html.escape(ctx['business_name'])}</b>\nTransaksi: {row['sales_count']}\nOmzet: <b>{rupiah(row['revenue'])}</b>\nLaba kotor: <b>{rupiah(row['gross_profit'])}</b>\nStok rendah: {low}", parse_mode="HTML")
        return

    if result.needs_clarification:
        await message.answer(
            result.clarification or "Mohon lengkapi informasi yang diperlukan."
        )
        return

    if result.intent in {"create_income", "create_expense"}:
        if not has_permission(access["role"], "finance.write"):
            await message.answer("⛔ Role Anda tidak boleh menulis transaksi keuangan manual.")
            return
        tx_type = "income" if result.intent == "create_income" else "expense"
        description = result.description or (
            "Pemasukan" if tx_type == "income" else "Pengeluaran"
        )
        category = result.category or "Umum"

        await state.clear()
        await state.update_data(
            tx_type=tx_type,
            amount=result.amount,
            description=description,
            category=category,
        )
        await state.set_state(AddTransaction.confirm)

        label = "PEMASUKAN" if tx_type == "income" else "PENGELUARAN"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Simpan", callback_data="tx_confirm"),
                InlineKeyboardButton(text="❌ Batal", callback_data="tx_cancel"),
            ]]
        )

        await message.answer(
            f"🤖 Saya memahami perintah Anda sebagai:\n\n"
            f"🧾 <b>{label}</b>\n"
            f"Nominal: <b>{rupiah(result.amount)}</b>\n"
            f"Kategori: <b>{html.escape(category)}</b>\n"
            f"Keterangan: {html.escape(description)}\n\n"
            "Simpan transaksi ini?",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    if result.intent == "create_reminder":
        try:
            due_local = datetime.fromisoformat(result.reminder_at)
            if due_local.tzinfo is None:
                due_local = due_local.replace(tzinfo=JAKARTA)
            due_local = due_local.astimezone(JAKARTA)
        except Exception:
            await message.answer(
                "Saya memahami bahwa Anda ingin membuat pengingat, "
                "tetapi waktu yang dihasilkan tidak valid. "
                "Tuliskan tanggal/jam lebih jelas."
            )
            return

        if due_local <= datetime.now(JAKARTA):
            await message.answer(
                "Waktu pengingat yang dimaksud sudah lewat. "
                "Tuliskan waktu yang akan datang."
            )
            return

        reminder_message = result.reminder_message or result.description or text[:500]
        await state.clear()
        await state.update_data(
            reminder_message=reminder_message,
            reminder_at=due_local.isoformat(),
        )
        await state.set_state(AIConfirmReminder.confirm)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Buat Pengingat",
                    callback_data="ai_reminder_confirm",
                ),
                InlineKeyboardButton(
                    text="❌ Batal",
                    callback_data="ai_reminder_cancel",
                ),
            ]]
        )
        await message.answer(
            "🤖 <b>Konfirmasi Pengingat</b>\n\n"
            f"📝 {html.escape(reminder_message)}\n"
            f"🗓 <b>{due_local.strftime('%d-%m-%Y %H:%M')} WIB</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    if result.intent == "finance_summary":
        if not has_permission(access["role"], "finance.read"):
            await message.answer("⛔ Role Anda tidak boleh melihat ringkasan keuangan.")
            return
        income = finance_context["month_income"]
        expense = finance_context["month_expense"]
        balance = finance_context["month_balance"]
        await message.answer(
            f"📊 <b>Ringkasan Bulan Ini</b>\n"
            f"🏪 {html.escape(finance_context['business_name'])}\n\n"
            f"💰 Pemasukan: <b>{rupiah(income)}</b>\n"
            f"💸 Pengeluaran: <b>{rupiah(expense)}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"💵 Selisih: <b>{rupiah(balance)}</b>",
            parse_mode="HTML",
        )
        return

    if result.intent == "finance_question" and not has_permission(access["role"], "finance.read"):
        await message.answer("⛔ Role Anda tidak boleh mengakses data keuangan.")
        return

    if result.intent in {"finance_question", "general_question"}:
        await message.answer(
            result.answer or "Saya belum memiliki jawaban yang cukup dari data yang tersedia."
        )
        return

    await message.answer(
        result.clarification
        or result.answer
        or (
            "Saya belum memahami permintaan itu. Contoh: "
            "“catat pemasukan 50rb”, “keluar 25rb parkir”, "
            "atau “berapa pengeluaran bulan ini?”"
        )
    )


@router.message()
async def fallback(message: Message, state: FSMContext):
    access = await ensure_access(message)
    if access["is_master"]:
        await message.answer("Gunakan menu MASTER.", reply_markup=master_menu())
    elif access["access_status"] != "active":
        await pending_message(message, access)
    elif not access["active_tenant_id"]:
        await message.answer("Akun aktif tetapi belum memiliki usaha. Hubungi MASTER.")
    else:
        await handle_ai_text(message, state)


async def main():
    if not TOKEN or TOKEN == "ISI_TOKEN_BOTFATHER_DI_SINI":
        raise RuntimeError("BOT_TOKEN belum diisi")
    logging.info("AI status: %s", ai.public_status())
    await db.init_pool()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(pos_router)
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await db.close_pool()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
