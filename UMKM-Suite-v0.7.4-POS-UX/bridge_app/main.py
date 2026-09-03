import asyncio
import calendar
import difflib
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
from datetime import date, datetime, timedelta, time as dt_time
from decimal import Decimal
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request

from bridge_app import ai, db, exporter, pos
from bridge_app.menu_ux import (
    numbered_choice, parse_product_edit_request, selected_menu_action,
    should_yield_to_free_text,
)
from bridge_app.permissions import PermissionDenied, has_permission
from bridge_app.pos_parser import (
    normalize_sku, parse_nonnegative_decimal, parse_nonnegative_money, parse_pipe_fields,
    parse_human_sale_items, parse_quick_sale_request, parse_sale_items,
)


JAKARTA = ZoneInfo("Asia/Jakarta")
UTC = ZoneInfo("UTC")

GOWA_URL = os.getenv("GOWA_URL", "http://gowa:3000").rstrip("/")
GOWA_USER = os.environ["GOWA_BASIC_AUTH_USER"]
GOWA_PASSWORD = os.environ["GOWA_BASIC_AUTH_PASSWORD"]
GOWA_DEVICE_ID = os.getenv("GOWA_DEVICE_ID", "umkm-main")
WEBHOOK_SECRET = os.environ["GOWA_WEBHOOK_SECRET"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MASTER_ID = int(os.environ["MASTER_TELEGRAM_ID"])
REPLY_DELAY = float(os.getenv("WA_MIN_REPLY_INTERVAL_SECONDS", "1.2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("umkm-whatsapp")

app = FastAPI(title="UMKM GOWA Bridge", version="0.7.4-pos-ux")

_worker_task = None
_reminder_task = None
_last_send: dict[str, float] = {}
_send_lock = asyncio.Lock()


def rupiah(value: int) -> str:
    n = int(value or 0)
    sign = "-" if n < 0 else ""
    return sign + "Rp{:,.0f}".format(abs(n)).replace(",", ".")


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    received = signature.removeprefix("sha256=").strip()
    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def normalize_jid(value: str | None) -> str:
    return (value or "").strip()


def json_object(value) -> dict:
    # Normalisasi JSON/JSONB asyncpg menjadi dict.
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON payload database tidak valid") from exc
        if isinstance(decoded, dict):
            return decoded
        raise ValueError("JSON payload database bukan object")
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Tipe JSON payload tidak didukung: {type(value).__name__}"
        ) from exc


async def _pace(jid: str):
    # Bukan anti-ban; hanya mencegah aplikasi membalas bertubi-tubi karena bug.
    async with _send_lock:
        loop = asyncio.get_running_loop()
        now = loop.time()
        last = _last_send.get(jid, 0.0)
        wait = REPLY_DELAY - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send[jid] = loop.time()


async def gowa_send_text(jid: str, message: str):
    await _pace(jid)
    headers = {"X-Device-Id": GOWA_DEVICE_ID}
    auth = httpx.BasicAuth(GOWA_USER, GOWA_PASSWORD)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{GOWA_URL}/send/message",
            json={"phone": jid, "message": message[:12000]},
            headers=headers,
            auth=auth,
        )
        response.raise_for_status()


async def gowa_chat_presence(jid: str, action: str):
    """Tampilkan indikator mengetik secara best-effort."""
    if action not in {"start", "stop"}:
        return
    headers = {"X-Device-Id": GOWA_DEVICE_ID}
    auth = httpx.BasicAuth(GOWA_USER, GOWA_PASSWORD)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"{GOWA_URL}/send/chat-presence",
                json={"phone": jid, "action": action},
                headers=headers,
                auth=auth,
            )
            response.raise_for_status()
    except Exception:
        log.debug("Gagal mengubah chat presence untuk %s", jid, exc_info=True)


async def gowa_send_file(jid: str, path: str, caption: str):
    await _pace(jid)
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    headers = {"X-Device-Id": GOWA_DEVICE_ID}
    auth = httpx.BasicAuth(GOWA_USER, GOWA_PASSWORD)
    async with httpx.AsyncClient(timeout=90) as client:
        with p.open("rb") as fh:
            response = await client.post(
                f"{GOWA_URL}/send/file",
                data={"phone": jid, "caption": caption[:1000]},
                files={"file": (p.name, fh, mime)},
                headers=headers,
                auth=auth,
            )
        response.raise_for_status()


async def telegram_notify_master(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            json={
                "chat_id": MASTER_ID,
                "text": text,
            },
        )
        response.raise_for_status()


ROLE_LABELS = {
    "OWNER": "Pemilik",
    "ADMIN": "Administrator",
    "FINANCE": "Keuangan",
    "CASHIER": "Kasir",
    "INVENTORY": "Petugas Stok",
    "VIEWER": "Pemantau",
}

MAIN_MENU_CATALOG = (
    ("income", "💰 Catat Pemasukan", "finance.write"),
    ("expense", "💸 Catat Pengeluaran", "finance.write"),
    ("finance_summary", "📊 Ringkasan Keuangan", "finance.read"),
    ("reminders", "⏰ Kelola Pengingat", None),
    ("export", "📤 Export Laporan", "finance.export"),
    ("ai", "🤖 Tanya Asisten AI", None),
    ("business", "🏪 Pengaturan & Akun", None),
    ("help", "❓ Bantuan & Contoh", None),
    ("pos", "🛒 POS & Stok", "pos.read"),
)

POS_MENU_CATALOG = (
    ("sale", "🧾 Penjualan Baru", "pos.sell"),
    ("products", "📦 Daftar Produk", "pos.read"),
    ("product_add", "➕ Tambah Produk", "product.write"),
    ("product_edit", "✏️ Edit Produk", "product.write"),
    ("stock_set", "🧮 Atur Stok Fisik", "stock.write"),
    ("report", "📊 Laporan POS", "pos.read"),
    ("staff", "👥 Kelola Karyawan", "staff.read"),
    ("low_stock", "⚠️ Stok Rendah", "pos.read"),
)

BUSINESS_MENU_CATALOG = (
    ("rename", "✏️ Ubah Nama Usaha", "business.write"),
    ("link_telegram", "🔗 Hubungkan dengan Telegram", None),
)


def role_label(role: str | None) -> str:
    value = str(role or "-").upper()
    return ROLE_LABELS.get(value, value)


def visible_menu_items(role: str | None, catalog) -> list[tuple[str, str, str | None]]:
    return [
        item for item in catalog
        if item[2] is None or has_permission(role, item[2])
    ]


def numbered_menu(items) -> str:
    return "\n".join(
        f"{index}️⃣  {label}" for index, (_, label, _) in enumerate(items, 1)
    )


def help_text(business_name: str | None = None, role: str | None = None) -> str:
    business = business_name or "Usaha Anda"
    menu = visible_menu_items(role, MAIN_MENU_CATALOG)
    return (
        "👋 *SELAMAT DATANG*\n"
        "🤖 *ASISTEN UMKM INDONESIA*\n"
        f"🏪 {business}\n"
        f"👤 {role_label(role)}\n\n"
        "Pilih menu dengan membalas angka:\n\n"
        + numbered_menu(menu) + "\n\n"
        "0️⃣  🏠 Menu Utama\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚡ *Mode Cepat*\n"
        "Anda juga bisa langsung mengetik:\n"
        "_keluar 50rb beli bensin_\n"
        "_pemasukan 1,5jt servis mesin_\n"
        "_jual 1 susu tunai_\n"
        "_berapa pengeluaran bulan ini?_\n\n"
        "💡 Produk dapat ditulis memakai nama atau SKU.\n"
        "Ketik *0* atau *MENU* kapan saja untuk kembali."
    )


def help_examples() -> str:
    return (
        "❓ *BANTUAN & CONTOH*\n\n"
        "💰 Pemasukan: `pemasukan 50000 penjualan kopi`\n"
        "💸 Pengeluaran: `keluar 75rb beli bensin`\n"
        "⏰ Pengingat: `ingatkan besok jam 9 bayar listrik`\n"
        "🔁 Berulang: `ingatkan setiap tanggal 1 jam 9 bayar listrik`\n"
        "📊 Analisis: `berapa pengeluaran bulan ini?`\n\n"
        "📤 Pilih menu *Export Laporan* untuk membuat Excel/PDF.\n"
        "Ketik *MENU* untuk kembali."
    )


def first_choice(text: str) -> str | None:
    raw = (text or "").strip().lower()
    if not raw:
        return None

    cleaned = (
        raw.replace("1️⃣", "1").replace("2️⃣", "2")
        .replace("3️⃣", "3").replace("4️⃣", "4")
        .replace("5️⃣", "5").replace("6️⃣", "6")
        .replace("7️⃣", "7").replace("8️⃣", "8")
        .replace("9️⃣", "9")
        .replace("0️⃣", "0")
    )
    m = re.match(r"^\s*([0-9])(?:\s|[.)-]|$)", cleaned)
    if m:
        return m.group(1)

    aliases = {
        "menu": "0", "/menu": "0", "start": "0", "/start": "0",
        "home": "0", "kembali": "0", "back": "0",
        "pemasukan": "1", "masuk": "1", "income": "1",
        "catat pemasukan": "1",
        "pengeluaran": "2", "keluar": "2", "expense": "2",
        "catat pengeluaran": "2",
        "ringkasan": "3", "ringkasan keuangan": "3",
        "summary": "3", "saldo": "3",
        "pengingat": "4", "buat pengingat": "4", "reminder": "4",
        "export": "5", "export laporan": "5", "laporan": "5",
        "ai": "6", "asisten": "6", "asisten ai": "6",
        "tanya ai": "6", "tanya asisten ai": "6",
        "pos": "9", "stok": "9", "kasir": "9", "pos dan stok": "9",
        "usaha": "7", "usaha saya": "7",
        "pengaturan": "7", "pengaturan usaha": "7",
        "help": "8", "bantuan": "8", "panduan": "8",
    }
    return aliases.get(raw)


def navigation_alias(text: str) -> str | None:
    """Navigasi global berbasis kata dan typo ringan."""
    raw = (text or "").strip().lower()
    if not raw:
        return None

    normalized = re.sub(r"[^a-z0-9/ ]+", " ", raw)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    aliases = {
        "menu": "0", "/menu": "0", "start": "0", "/start": "0",
        "home": "0", "kembali": "0", "back": "0",
        "pemasukan": "1", "catat pemasukan": "1",
        "pengeluaran": "2", "catat pengeluaran": "2",
        "ringkasan": "3", "ringkasan keuangan": "3",
        "pengingat": "4", "buat pengingat": "4",
        "export": "5", "export laporan": "5",
        "asisten ai": "6", "tanya ai": "6", "tanya asisten ai": "6",
        "usaha": "7", "usaha saya": "7",
        "pengaturan": "7", "pengaturan usaha": "7",
        "bantuan": "8", "help": "8", "panduan": "8",
        "pos": "9", "pos dan stok": "9", "kasir": "9", "stok": "9",
    }
    if normalized in aliases:
        return aliases[normalized]

    typo_aliases = {
        "mesnu": "0", "mneu": "0", "meun": "0", "men": "0",
        "ringaksan": "3",
        "pengigat": "4", "pengingatn": "4",
        "asisten ia": "6", "asistent ai": "6",
        "pengaturan usha": "7", "pengaturan usah": "7",
    }
    if normalized in typo_aliases:
        return typo_aliases[normalized]

    if len(normalized) <= 24 and not any(ch.isdigit() for ch in normalized):
        close = difflib.get_close_matches(
            normalized, list(aliases.keys()), n=1, cutoff=0.86
        )
        if close:
            return aliases[close[0]]

    return None


def parse_money_input(text: str) -> int | None:
    raw = (text or "").lower().strip().replace("rp", "").replace(" ", "")
    raw = raw.replace(",", ".")
    multiplier = 1
    for suffix, mult in (
        ("ribu", 1_000), ("rb", 1_000),
        ("juta", 1_000_000), ("jt", 1_000_000),
    ):
        if raw.endswith(suffix):
            multiplier = mult
            raw = raw[:-len(suffix)]
            break

    try:
        if multiplier > 1:
            if not re.fullmatch(r"\d+(\.\d+)?", raw):
                return None
            value = int(float(raw) * multiplier)
        else:
            digits = re.sub(r"[^\d]", "", raw)
            if not digits:
                return None
            value = int(digits)
    except Exception:
        return None

    if value <= 0 or value > 999_999_999_999:
        return None
    return value



MONTHS_ID = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2,
    "maret": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mei": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "agustus": 8, "agu": 8, "agt": 8,
    "september": 9, "sep": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "desember": 12, "des": 12,
}

WEEKDAYS_ID = {
    "senin": 0,
    "selasa": 1,
    "rabu": 2,
    "kamis": 3,
    "jumat": 4,
    "jum'at": 4,
    "sabtu": 5,
    "minggu": 6,
}

WEEKDAY_NAMES = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


def parse_time_input(text: str) -> dt_time | None:
    raw = (text or "").strip().lower()

    # Dalam kalimat panjang, angka tanggal/nominal tidak boleh dianggap jam.
    m = re.search(r"\bjam\s*([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\b", raw)
    if not m:
        m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", raw)
    if not m and re.fullmatch(r"(?:[01]?\d|2[0-3])", raw):
        m = re.fullmatch(r"([01]?\d|2[0-3])", raw)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0) if m.lastindex and m.lastindex >= 2 else 0
    return dt_time(hour=hour, minute=minute)

def parse_date_input(text: str) -> date | None:
    raw = (text or "").strip().lower()
    today = datetime.now(JAKARTA).date()
    if re.search(r"\b(hari ini|today)\b", raw):
        return today
    if re.search(r"\b(besok|tomorrow)\b", raw):
        return today + timedelta(days=1)
    if re.search(r"\blusa\b", raw):
        return today + timedelta(days=2)

    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", raw)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    m = re.search(
        r"\b(\d{1,2})\s+"
        r"(januari|jan|februari|feb|maret|mar|april|apr|mei|juni|jun|juli|jul|agustus|agu|agt|september|sep|oktober|okt|november|nov|desember|des)"
        r"(?:\s+(\d{4}))?\b",
        raw,
    )
    if m:
        day = int(m.group(1))
        month = MONTHS_ID[m.group(2)]
        year = int(m.group(3) or today.year)
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if m.group(3) is None and candidate < today:
            try:
                candidate = date(year + 1, month, day)
            except ValueError:
                return None
        return candidate

    return None


def parse_weekday_input(text: str) -> int | None:
    raw = (text or "").strip().lower()
    if raw in WEEKDAYS_ID:
        return WEEKDAYS_ID[raw]
    choice = first_choice(raw)
    if choice and choice in {"1", "2", "3", "4", "5", "6", "7"}:
        return int(choice) - 1
    return None


def _combine_local(d: date, t: dt_time) -> datetime:
    return datetime.combine(d, t, tzinfo=JAKARTA)


def next_daily_due(clock: dt_time) -> datetime:
    now = datetime.now(JAKARTA)
    candidate = _combine_local(now.date(), clock)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_weekly_due(weekday: int, clock: dt_time) -> datetime:
    now = datetime.now(JAKARTA)
    delta = (weekday - now.weekday()) % 7
    candidate = _combine_local(now.date() + timedelta(days=delta), clock)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def next_monthly_due(day: int, clock: dt_time) -> datetime:
    now = datetime.now(JAKARTA)
    year, month = now.year, now.month
    for _ in range(24):
        last_day = calendar.monthrange(year, month)[1]
        candidate = _combine_local(date(year, month, min(day, last_day)), clock)
        if candidate > now:
            return candidate
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    raise ValueError("Tidak dapat menghitung jadwal bulanan")


def recurrence_label(row) -> str:
    kind = row["recurrence_type"] or "once"
    due = row["due_at"].astimezone(JAKARTA)
    if kind == "daily":
        return f"Setiap hari · {due.strftime('%H:%M')} WIB"
    if kind == "weekly":
        wd = row["recurrence_weekday"]
        name = WEEKDAY_NAMES[int(wd)] if wd is not None and 0 <= int(wd) <= 6 else due.strftime('%A')
        return f"Setiap {name} · {due.strftime('%H:%M')} WIB"
    if kind == "monthly":
        day = row["recurrence_day"] or due.day
        return f"Setiap tanggal {day} · {due.strftime('%H:%M')} WIB"
    return due.strftime("%d-%m-%Y %H:%M WIB")


def clean_quick_reminder_message(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"(?i)^\s*((?:buatkan|buat)\s+)?(pengingat|ingatkan)\s*", "", value)
    value = re.sub(r"(?i)\bsetiap\s+tanggal\s+\d{1,2}\b", "", value)
    value = re.sub(r"(?i)\bsetiap\s+(hari|senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu)\b", "", value)
    value = re.sub(r"(?i)\b(?:tanggal\s+)?\d{1,2}\s+(?:januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)(?:\s+\d{4})?\b", "", value)
    value = re.sub(r"(?i)\b(?:hari\s+ini|besok|lusa)\b", "", value)
    value = re.sub(r"(?i)\bjam\s*\d{1,2}(?:[:.]\d{2})?\b", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ,.-")
    return value or "Pengingat"


def schedule_payload(
    message: str,
    due_local: datetime,
    recurrence_type: str = "once",
    recurrence_day: int | None = None,
    recurrence_weekday: int | None = None,
):
    return {
        "message": message,
        "reminder_at": due_local.isoformat(),
        "recurrence_type": recurrence_type,
        "recurrence_interval": 1,
        "recurrence_day": recurrence_day,
        "recurrence_weekday": recurrence_weekday,
        "recurrence_time": due_local.strftime("%H:%M:%S"),
    }


async def set_reminder_confirmation(jid: str, payload: dict, action_type: str = "reminder"):
    due_local = datetime.fromisoformat(payload["reminder_at"]).astimezone(JAKARTA)
    kind = payload.get("recurrence_type", "once")
    if kind == "daily":
        schedule = f"Setiap hari · {due_local.strftime('%H:%M')} WIB"
    elif kind == "weekly":
        wd = int(payload.get("recurrence_weekday", due_local.weekday()))
        schedule = f"Setiap {WEEKDAY_NAMES[wd]} · {due_local.strftime('%H:%M')} WIB"
    elif kind == "monthly":
        schedule = f"Setiap tanggal {payload.get('recurrence_day') or due_local.day} · {due_local.strftime('%H:%M')} WIB"
    else:
        schedule = due_local.strftime("%d-%m-%Y %H:%M WIB")

    await db.set_pending_action(jid, action_type, payload, ttl_minutes=20)
    title = "🤖 *KONFIRMASI PENGINGAT*" if action_type == "reminder" else "✏️ *KONFIRMASI PERUBAHAN JADWAL*"
    await gowa_send_text(
        jid,
        f"{title}\\n\\n"
        f"📝 {payload.get('message') or '-'}\\n"
        f"🗓 {schedule}\\n\\n"
        "1️⃣ SIMPAN\\n"
        "2️⃣ EDIT\\n"
        "0️⃣ BATAL",
    )


async def send_reminder_menu(jid: str):
    await db.set_pending_action(jid, "menu_reminder", {}, ttl_minutes=20)
    await gowa_send_text(
        jid,
        "⏰ *PENGINGAT*\\n\\n"
        "1️⃣ ➕ Buat Pengingat\\n"
        "2️⃣ 📋 Pengingat Aktif\\n"
        "3️⃣ ✏️ Edit Pengingat\\n"
        "4️⃣ 🗑 Hapus Pengingat\\n\\n"
        "0️⃣ 🏠 Menu Utama",
    )


async def send_active_reminders(jid: str, mode: str = "view"):
    rows = await db.list_active_reminders(jid, 20)
    if not rows:
        await db.set_pending_action(jid, "menu_reminder", {}, ttl_minutes=20)
        await gowa_send_text(
            jid,
            "📋 *PENGINGAT AKTIF*\\n\\nBelum ada pengingat aktif.\\n\\n"
            "1️⃣ Buat Pengingat\\n0️⃣ Menu Utama",
        )
        return

    lines = ["📋 *PENGINGAT AKTIF*", ""]
    ids = []
    for i, row in enumerate(rows, 1):
        ids.append(str(row["id"]))
        lines.append(f"{i}️⃣ *{row['message']}*")
        lines.append(f"   🗓 {recurrence_label(row)}")
        lines.append(f"   🔔 Berikutnya: {row['due_at'].astimezone(JAKARTA).strftime('%d-%m-%Y %H:%M')} WIB")
        lines.append("")

    if mode == "edit":
        await db.set_pending_action(jid, "reminder_edit_pick", {"ids": ids}, ttl_minutes=20)
        lines.append("Balas nomor pengingat yang ingin *diedit*.")
        lines.append("0️⃣ Kembali")
    elif mode == "delete":
        await db.set_pending_action(jid, "reminder_delete_pick", {"ids": ids}, ttl_minutes=20)
        lines.append("Balas nomor pengingat yang ingin *dihapus*.")
        lines.append("0️⃣ Kembali")
    else:
        await db.set_pending_action(jid, "menu_reminder", {}, ttl_minutes=20)
        lines.extend(["Ketik *PENGINGAT* untuk pilihan lainnya.", "0️⃣ Menu Utama"])

    await gowa_send_text(jid, "\\n".join(lines))


async def begin_reminder_schedule(jid: str, payload: dict):
    kind = payload["recurrence_type"]
    if kind == "once":
        await db.set_pending_action(jid, "reminder_once_date", payload, ttl_minutes=20)
        await gowa_send_text(
            jid,
            "📅 *TANGGAL PENGINGAT*\\n\\n"
            "Contoh: `01-09-2026`, `besok`, atau `lusa`\\n\\n"
            "0️⃣ Batal & kembali",
        )
    elif kind == "daily":
        await db.set_pending_action(jid, "reminder_time", payload, ttl_minutes=20)
        await gowa_send_text(jid, "🕘 Jam berapa setiap hari?\\nContoh: `09:00`\\n\\n0️⃣ Batal")
    elif kind == "weekly":
        await db.set_pending_action(jid, "reminder_weekday", payload, ttl_minutes=20)
        await gowa_send_text(
            jid,
            "📅 *PILIH HARI*\\n\\n"
            "1️⃣ Senin\\n2️⃣ Selasa\\n3️⃣ Rabu\\n4️⃣ Kamis\\n"
            "5️⃣ Jumat\\n6️⃣ Sabtu\\n7️⃣ Minggu\\n\\n0️⃣ Batal",
        )
    elif kind == "monthly":
        await db.set_pending_action(jid, "reminder_monthday", payload, ttl_minutes=20)
        await gowa_send_text(jid, "📅 Tanggal berapa setiap bulan?\\nKetik angka *1–31*.\\n\\n0️⃣ Batal")


async def try_quick_reminder(jid: str, text: str) -> bool:
    lower = (text or "").lower()
    if "pengingat" not in lower and "ingatkan" not in lower:
        return False

    if re.search(r"pengingat.{0,15}(aktif|saya)|daftar\s+pengingat", lower):
        await send_active_reminders(jid, "view")
        return True

    message = clean_quick_reminder_message(text)
    clock = parse_time_input(text)

    m = re.search(r"(?i)setiap\s+tanggal\s+(\d{1,2})", text)
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            await gowa_send_text(jid, "Tanggal bulanan harus 1–31.")
            return True
        if clock is None:
            await db.set_pending_action(
                jid, "quick_monthly_time", {"message": message, "day": day}, ttl_minutes=20
            )
            await gowa_send_text(jid, f"🕘 Jam berapa setiap tanggal *{day}*?\\nContoh: `09:00`")
            return True
        due = next_monthly_due(day, clock)
        await set_reminder_confirmation(jid, schedule_payload(message, due, "monthly", recurrence_day=day))
        return True

    if re.search(r"(?i)setiap\s+hari", text):
        if clock is None:
            await db.set_pending_action(jid, "quick_daily_time", {"message": message}, ttl_minutes=20)
            await gowa_send_text(jid, "🕘 Jam berapa setiap hari?\\nContoh: `09:00`")
            return True
        due = next_daily_due(clock)
        await set_reminder_confirmation(jid, schedule_payload(message, due, "daily"))
        return True

    for name, wd in WEEKDAYS_ID.items():
        if re.search(rf"(?i)setiap\s+{re.escape(name)}\b", text):
            if clock is None:
                await db.set_pending_action(
                    jid, "quick_weekly_time", {"message": message, "weekday": wd}, ttl_minutes=20
                )
                await gowa_send_text(jid, f"🕘 Jam berapa setiap *{WEEKDAY_NAMES[wd]}*?\\nContoh: `09:00`")
                return True
            due = next_weekly_due(wd, clock)
            await set_reminder_confirmation(jid, schedule_payload(message, due, "weekly", recurrence_weekday=wd))
            return True

    parsed_date = parse_date_input(text)
    if parsed_date:
        if clock is None:
            await db.set_pending_action(
                jid,
                "quick_once_time",
                {"message": message, "date": parsed_date.isoformat()},
                ttl_minutes=20,
            )
            await gowa_send_text(
                jid,
                f"🕘 Jam berapa pada tanggal *{parsed_date.strftime('%d-%m-%Y')}*?\\nContoh: `09:00`",
            )
            return True
        due = _combine_local(parsed_date, clock)
        if due <= datetime.now(JAKARTA):
            await gowa_send_text(jid, "Waktu tersebut sudah lewat. Gunakan waktu yang akan datang.")
            return True
        await set_reminder_confirmation(jid, schedule_payload(message, due, "once"))
        return True

    # Reminder intent sudah jelas tetapi jadwal belum lengkap: simpan konteks.
    await db.set_pending_action(jid, "quick_need_when", {"message": message}, ttl_minutes=20)
    await gowa_send_text(
        jid,
        "🗓 Kapan saya harus mengingatkan?\n\n"
        "Contoh:\n"
        "• `besok jam 9`\n"
        "• `1 September 2026 jam 09:00`\n"
        "• `setiap tanggal 1 jam 09:00`",
    )
    return True


async def send_main_menu(jid: str):
    await db.clear_pending_action(jid)
    current = await db.get_whatsapp_access(jid)
    role = current["role"] if current else None
    items = visible_menu_items(role, MAIN_MENU_CATALOG)
    await db.set_pending_action(
        jid, "menu_main", {"actions": [item[0] for item in items]}, ttl_minutes=20
    )
    await gowa_send_text(
        jid,
        help_text(
            current["business_name"] if current else None,
            role,
        ),
    )


async def send_export_menu(jid: str):
    await db.set_pending_action(jid, "menu_export", {}, ttl_minutes=20)
    await gowa_send_text(
        jid,
        "📤 *EXPORT LAPORAN*\n\n"
        "1️⃣  📗 Excel — Bulan Ini\n"
        "2️⃣  📕 PDF — Bulan Ini\n"
        "3️⃣  📗 Excel — Semua Data\n"
        "4️⃣  📕 PDF — Semua Data\n"
        "5️⃣  📅 Rentang Tanggal\n\n"
        "0️⃣  🏠 Menu Utama",
    )

async def send_business_menu(jid: str):
    current = await db.get_whatsapp_access(jid)
    role = str(current["role"] if current else "-").upper()
    items = visible_menu_items(role, BUSINESS_MENU_CATALOG)
    await db.set_pending_action(
        jid, "menu_business", {"actions": [item[0] for item in items]}, ttl_minutes=20
    )
    await gowa_send_text(
        jid,
        "🏪 *PENGATURAN & AKUN*\n\n"
        f"Nama usaha: *{current['business_name'] or '-'}*\n"
        f"WhatsApp: {current['phone_number'] or '-'}\n\n"
        + numbered_menu(items) + "\n"
        "0️⃣  🏠 Menu Utama",
    )


def _qty(value) -> str:
    return f"{Decimal(value):f}".rstrip("0").rstrip(".") or "0"


async def send_pos_menu(jid: str):
    access = await db.get_whatsapp_access(jid)
    role = str(access["role"] if access else "-").upper()
    items = visible_menu_items(role, POS_MENU_CATALOG)
    await db.set_pending_action(
        jid, "menu_pos", {"actions": [item[0] for item in items]}, ttl_minutes=20
    )
    await gowa_send_text(
        jid,
        "🛒 *POS & STOK v0.7.4*\n"
        f"👤 {role_label(role)}\n\n"
        "Pilih menu yang tersedia:\n\n"
        + numbered_menu(items) + "\n\n"
        "0️⃣ Menu Utama",
    )


async def send_staff_menu(jid: str):
    access = await db.get_whatsapp_access(jid)
    role = str(access["role"] if access else "-").upper()
    if not access or not has_permission(role, "staff.read"):
        raise PermissionDenied(f"Role {role} tidak memiliki izin mengelola karyawan.")
    await db.set_pending_action(jid, "menu_staff", {}, ttl_minutes=20)
    await gowa_send_text(
        jid,
        "👥 *KELOLA KARYAWAN*\n\n"
        "1️⃣  Daftar Karyawan\n"
        "2️⃣  Tambah Karyawan\n"
        "3️⃣  Ubah Role Karyawan\n"
        "4️⃣  Nonaktifkan Karyawan\n\n"
        "0️⃣ Menu Utama",
    )


async def wa_permission_or_reply(jid: str, permission: str):
    access = await db.get_whatsapp_access(jid)
    role = str(access["role"] if access else "-").upper()
    if not access or not has_permission(role, permission):
        await gowa_send_text(jid, f"⛔ Menu ini tidak tersedia untuk peran *{role_label(role)}*.\n\n0️⃣ Menu Utama")
        return None
    return access


def staff_role_choices(actor_role: str) -> dict[str, str]:
    choices = {"1": "CASHIER", "2": "INVENTORY", "3": "FINANCE", "4": "VIEWER"}
    if str(actor_role or "").upper() == "OWNER":
        choices["5"] = "ADMIN"
    return choices


def staff_role_prompt(actor_role: str) -> str:
    text = "1️⃣ KASIR — penjualan\n2️⃣ PETUGAS STOK — produk & stok\n3️⃣ KEUANGAN — pembukuan & laporan\n4️⃣ PEMANTAU — hanya melihat data"
    if str(actor_role or "").upper() == "OWNER":
        text += "\n5️⃣ ADMINISTRATOR — kelola usaha"
    return text


async def send_staff_list(jid: str, *, keep_menu: bool = True):
    ctx, rows = await pos.list_staff(jid)
    lines = [f"👥 *KARYAWAN · {ctx['business_name']}*", ""]
    for index, row in enumerate(rows, 1):
        channel = row["whatsapp_phone"] or row["telegram_user_id"] or "belum terhubung"
        status = "aktif" if row["is_active"] else "nonaktif"
        lines.append(f"{index}. *{row['full_name'] or '-'}* — {role_label(row['role'])}\n   {channel} · {status}")
    if not rows:
        lines.append("Belum ada karyawan.")
    lines.append("\n0️⃣ Menu Utama")
    await gowa_send_text(jid, "\n".join(lines))
    if keep_menu:
        await db.set_pending_action(jid, "menu_staff", {}, ttl_minutes=20)


async def begin_staff_pick(jid: str, mode: str):
    ctx, rows = await pos.list_staff(jid)
    candidates = [row for row in rows if row["role"] != "OWNER" and row["is_active"]]
    if ctx["role"] == "ADMIN":
        candidates = [row for row in candidates if row["role"] != "ADMIN"]
    if not candidates:
        await gowa_send_text(jid, "Belum ada karyawan yang dapat dikelola.\n\n0️⃣ Menu Utama")
        return
    items = []
    lines = ["Pilih nomor karyawan:", ""]
    for index, row in enumerate(candidates, 1):
        items.append({"id": str(row["id"]), "name": row["full_name"] or "-", "role": row["role"]})
        lines.append(f"{index}️⃣  {row['full_name'] or '-'} — {role_label(row['role'])}")
    state = "guided_staff_role_pick" if mode == "role" else "guided_staff_remove_pick"
    await db.set_pending_action(jid, state, {"staff": items}, ttl_minutes=20)
    await gowa_send_text(jid, "\n".join(lines) + "\n\n0️⃣ Batal")


def serializable_sale_items(items) -> list[dict]:
    clean = []
    for item in (items or [])[:50]:
        query = str(
            item.get("product_query") or item.get("query") or
            item.get("name") or item.get("sku") or ""
        ).strip()
        if not query or item.get("qty") is None:
            continue
        clean.append({"product_query": query, "qty": str(item["qty"])})
    return clean


def payment_method_from_text(text: str) -> str | None:
    raw = re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()
    mapping = {
        "1": "CASH", "cash": "CASH", "tunai": "CASH",
        "2": "TRANSFER", "transfer": "TRANSFER", "tf": "TRANSFER",
        "3": "QRIS", "qris": "QRIS", "qr": "QRIS",
        "4": "CARD", "card": "CARD", "kartu": "CARD", "debit": "CARD",
        "5": "OTHER", "other": "OTHER", "lainnya": "OTHER", "lain lain": "OTHER",
    }
    return mapping.get(raw)


def sale_preview_text(ctx: dict, preview: dict) -> str:
    lines = ["🧾 *KONFIRMASI PENJUALAN*", f"🏪 {ctx['business_name']}", ""]
    for index, item in enumerate(preview["items"], 1):
        lines.append(
            f"{index}. *{item['name']}* × {_qty(item['qty'])} {item['unit']}\n"
            f"   {rupiah(item['unit_price'])} × {_qty(item['qty'])} = *{rupiah(item['line_total'])}*\n"
            f"   Sisa stok: {_qty(item['stock_after'])} {item['unit']}"
        )
    lines.extend([
        "", "━━━━━━━━━━━━━━━━━━", f"*TOTAL: {rupiah(preview['total'])}*",
        f"Pembayaran: *{preview['payment_method']}*", "",
        "1️⃣ SIMPAN", "2️⃣ EDIT", "0️⃣ BATAL",
    ])
    return "\n".join(lines)


def product_confirmation_text(payload: dict) -> str:
    return (
        f"📦 *KONFIRMASI PRODUK*\n\nSKU otomatis: `{payload['sku']}`\n"
        f"Nama: *{payload['name']}*\nHarga jual: *{rupiah(payload['sell_price'])}*\n"
        f"Stok awal: {payload['opening_stock']} {payload['unit']}\n"
        f"Harga modal: {rupiah(payload['cost_price'])}\n"
        f"Batas stok minimum: {payload['min_stock']}\n\n"
        "1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL"
    )


def product_update_confirmation_text(payload: dict) -> str:
    return (
        "✏️ *KONFIRMASI EDIT PRODUK*\n\n"
        f"Produk: *{payload['name']}* (`{payload['sku']}`)\n"
        f"Harga jual: *{rupiah(payload['sell_price'])}*\n"
        f"Harga modal: {rupiah(payload['cost_price'])}\n"
        f"Satuan: {payload['unit']}\n"
        f"Stok minimum: {payload['min_stock']}\n\n"
        "1️⃣ SIMPAN\n2️⃣ EDIT LAGI\n0️⃣ BATAL"
    )


async def begin_sale_preview(jid: str, items, payment_method: str | None = None):
    """Resolve names inside the tenant and keep an editable WhatsApp sale draft."""
    draft_items = serializable_sale_items(items)
    if not draft_items:
        await db.set_pending_action(
            jid, "sale_items_needed", {"payment_method": payment_method}, ttl_minutes=20
        )
        await gowa_send_text(
            jid,
            "🧾 Produk apa dan berapa jumlahnya?\n\n"
            "Contoh: `Susu 1 cup` atau `2 Kopi Susu`\n\n0️⃣ Batal",
        )
        return
    try:
        ctx, preview = await pos.prepare_sale_preview(jid, draft_items, payment_method)
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"⚠️ Penjualan belum dapat diproses: {exc}\n\n0️⃣ Menu Utama")
        return

    if preview["unresolved"]:
        unresolved = preview["unresolved"][0]
        state_payload = {
            "draft_items": draft_items,
            "payment_method": payment_method,
            "item_index": unresolved["item_index"],
            "query": unresolved["query"],
        }
        candidates = unresolved["candidates"]
        if candidates:
            state_payload["candidates"] = candidates
            await db.set_pending_action(jid, "sale_product_choice", state_payload, ttl_minutes=20)
            lines = [f"🔎 Saya menemukan beberapa produk untuk *{unresolved['query']}*:", ""]
            for index, product in enumerate(candidates, 1):
                lines.append(
                    f"{index}️⃣ *{product['name']}* — {rupiah(product['sell_price'])} · "
                    f"stok {_qty(product['stock_on_hand'])} {product['unit']}"
                )
            lines.append("\nBalas nomor produknya.\n0️⃣ Batal")
            await gowa_send_text(jid, "\n".join(lines))
        else:
            await db.set_pending_action(jid, "sale_product_replace", state_payload, ttl_minutes=20)
            await gowa_send_text(
                jid,
                f"⚠️ Produk *{unresolved['query']}* tidak ditemukan.\n\n"
                "Ketik nama/SKU lain, atau ketik *DAFTAR PRODUK*.\n0️⃣ Batal",
            )
        return

    if not preview["payment_method"]:
        await db.set_pending_action(
            jid, "sale_payment_needed", {"draft_items": draft_items}, ttl_minutes=20
        )
        await gowa_send_text(
            jid,
            "💳 *PILIH PEMBAYARAN*\n\n"
            "1️⃣ Tunai\n2️⃣ Transfer\n3️⃣ QRIS\n4️⃣ Kartu\n5️⃣ Lainnya\n\n"
            "Balas angka atau nama metode.\n0️⃣ Batal",
        )
        return

    final_items = [{"sku": item["sku"], "qty": item["qty"]} for item in preview["items"]]
    await db.set_pending_action(
        jid,
        "pos_sale",
        {"items": final_items, "payment_method": preview["payment_method"], "preview": preview},
        ttl_minutes=10,
    )
    await gowa_send_text(jid, sale_preview_text(ctx, preview))


async def answer_stock_query(jid: str, query: str):
    try:
        _, product, candidates = await pos.resolve_product(jid, query)
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"⚠️ {exc}\n\n0️⃣ Menu Utama")
        return
    if product:
        await db.clear_pending_action(jid)
        await gowa_send_text(
            jid,
            f"📦 *{product['name']}* (`{product['sku']}`)\n"
            f"Stok: *{_qty(product['stock_on_hand'])} {product['unit']}*\n"
            f"Harga: *{rupiah(product['sell_price'])}*\n\n0️⃣ Menu Utama",
        )
        return
    await db.set_pending_action(jid, "stock_query_needed", {}, ttl_minutes=20)
    if candidates:
        lines = [f"🔎 Ada beberapa produk yang mirip dengan *{query}*:", ""]
        lines.extend(
            f"• *{item['name']}* — ketik `{item['sku']}`"
            for item in candidates
        )
        lines.append("\nKetik nama/SKU yang dimaksud.\n0️⃣ Batal")
        await gowa_send_text(jid, "\n".join(lines))
    else:
        await gowa_send_text(
            jid,
            f"⚠️ Produk *{query}* tidak ditemukan.\nKetik nama/SKU lain atau *DAFTAR PRODUK*.\n\n0️⃣ Batal",
        )


async def show_product_edit_fields(jid: str, product) -> None:
    payload = {
        "sku": str(product["sku"]),
        "name": str(product["name"]),
        "sell_price": int(product["sell_price"]),
        "cost_price": int(product["cost_price"]),
        "unit": str(product["unit"]),
        "min_stock": str(product["min_stock"]),
    }
    await db.set_pending_action(
        jid, "guided_pos_product_edit_field", payload, ttl_minutes=20
    )
    await gowa_send_text(
        jid,
        "✏️ *EDIT PRODUK — PILIH DATA*\n\n"
        f"Produk: *{payload['name']}* (`{payload['sku']}`)\n\n"
        "1️⃣ Nama produk\n"
        "2️⃣ Harga jual\n"
        "3️⃣ Harga modal\n"
        "4️⃣ Satuan\n"
        "5️⃣ Batas stok minimum\n\n"
        "0️⃣ Batal",
    )


async def begin_product_edit(jid: str, query: str | None = None) -> None:
    if not await wa_permission_or_reply(jid, "product.write"):
        return
    query = (query or "").strip()
    if not query:
        await db.set_pending_action(
            jid, "guided_pos_product_edit_pick", {}, ttl_minutes=20
        )
        await gowa_send_text(
            jid,
            "✏️ *EDIT PRODUK — LANGKAH 1/2*\n\n"
            "Ketik nama atau SKU produk yang akan diubah.\n"
            "Contoh: *Susu*\n\n0️⃣ Batal",
        )
        return
    try:
        _, product, candidates = await pos.resolve_product(jid, query)
    except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
        await gowa_send_text(jid, f"⚠️ {exc}\n\n0️⃣ Batal")
        return
    if product:
        _, full_product = await pos.get_product(jid, product["sku"])
        if not full_product:
            await gowa_send_text(jid, "⚠️ Produk tidak lagi tersedia.\n\n0️⃣ Batal")
            return
        await show_product_edit_fields(jid, full_product)
        return
    if candidates:
        await db.set_pending_action(
            jid,
            "guided_pos_product_edit_choice",
            {"candidates": candidates},
            ttl_minutes=20,
        )
        lines = [f"🔎 Ada beberapa produk mirip *{query}*:", ""]
        for index, item in enumerate(candidates, 1):
            lines.append(f"{index}️⃣ *{item['name']}* (`{item['sku']}`)")
        lines.append("\nBalas nomor produk.\n0️⃣ Batal")
        await gowa_send_text(jid, "\n".join(lines))
        return
    await db.set_pending_action(jid, "guided_pos_product_edit_pick", {}, ttl_minutes=20)
    await gowa_send_text(
        jid,
        f"⚠️ Produk *{query}* tidak ditemukan.\n"
        "Ketik nama/SKU lain atau *DAFTAR PRODUK*.\n\n0️⃣ Batal",
    )


async def handle_pos_command(jid: str, text: str) -> bool:
    raw = re.sub(r"(?i)^\s*pos\s*", "", text or "").strip()
    upper = raw.upper()
    try:
        if upper in {"", "MENU", "BANTUAN"}:
            await send_pos_menu(jid)
            return True

        if upper == "PRODUK" or upper == "STOK RENDAH":
            low_only = upper == "STOK RENDAH"
            ctx, rows = await pos.list_products(jid, low_stock_only=low_only)
            if not rows:
                await gowa_send_text(jid, "Belum ada produk yang cocok.\n\n0️⃣ Menu Utama")
                return True
            lines = [f"📦 *{'STOK RENDAH' if low_only else 'DAFTAR PRODUK'}*", f"🏪 {ctx['business_name']}", ""]
            for row in rows:
                mark = " ⚠️" if row["stock_on_hand"] <= row["min_stock"] else ""
                lines.append(f"• `{row['sku']}` — {row['name']} · {_qty(row['stock_on_hand'])} {row['unit']} · {rupiah(row['sell_price'])}{mark}")
            await gowa_send_text(jid, "\n".join(lines) + "\n\n0️⃣ Menu Utama")
            return True

        if upper in {"KARYAWAN", "STAF", "STAFF"}:
            await send_staff_menu(jid)
            return True

        match = re.match(r"(?i)^STOK\s+(.+)$", raw)
        if match:
            _, row, candidates = await pos.resolve_product(jid, match.group(1))
            if not row:
                if candidates:
                    choices = ", ".join(f"{item['name']} (`{item['sku']}`)" for item in candidates)
                    raise LookupError("Produk belum spesifik. Pilihan: " + choices)
                raise LookupError("Produk tidak ditemukan.")
            await gowa_send_text(jid, f"📦 `{row['sku']}` — *{row['name']}*\nStok: *{_qty(row['stock_on_hand'])} {row['unit']}*\nMinimum: {_qty(row['min_stock'])}\nHarga jual: {rupiah(row['sell_price'])}\n\n0️⃣ Menu Utama")
            return True

        match = re.match(r"(?is)^(?:JUAL|PENJUALAN)\s+(.+)$", raw)
        if match:
            quick_sale = parse_quick_sale_request(f"jual {match.group(1)}")
            if quick_sale:
                items, method = quick_sale
                await begin_sale_preview(jid, items, method)
                return True
            if "|" in match.group(1):
                item_text, method = parse_pipe_fields(match.group(1), 2)
                items = parse_human_sale_items(item_text)
                await begin_sale_preview(jid, items, payment_method_from_text(method) or method)
                return True
            await db.set_pending_action(jid, "sale_items_needed", {}, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "🧾 Sebutkan produk dan jumlahnya.\n\n"
                "Contoh: *Susu 1 cup* atau *2 Kopi dan 1 Roti*.\n\n0️⃣ Batal",
            )
            return True

        match = re.match(r"(?is)^TAMBAH\s+PRODUK\s+(.+)$", raw)
        if match:
            sku, name, sell, cost, opening, unit, minimum = parse_pipe_fields(match.group(1), 7)
            _, row = await pos.add_product(
                jid, sku, name, parse_nonnegative_money(sell), parse_nonnegative_money(cost),
                parse_nonnegative_decimal(opening), unit, parse_nonnegative_decimal(minimum),
            )
            await gowa_send_text(jid, f"✅ Produk `{row['sku']}` tersimpan. Stok {_qty(row['stock_on_hand'])}, harga {rupiah(row['sell_price'])}.\n\n0️⃣ Menu Utama")
            return True

        match = re.match(r"(?is)^BATAL\s+(.+)$", raw)
        if match:
            fields = parse_pipe_fields(match.group(1), (1, 2))
            reason = fields[1] if len(fields) == 2 else "Dibatalkan melalui WhatsApp"
            _, sale, repeated = await pos.void_sale(jid, fields[0], reason)
            label = "ℹ️ Invoice sudah void" if repeated else "✅ Penjualan dibatalkan"
            await gowa_send_text(jid, f"{label}: `{sale['invoice_no']}`.\nStok dikembalikan dan pemasukan di-void.\n\n0️⃣ Menu Utama")
            return True

        match = re.match(r"(?is)^SET\s+STOK\s+(.+)$", raw)
        if match:
            sku, amount, note = parse_pipe_fields(match.group(1), 3)
            _, row, delta = await pos.set_stock(jid, sku, parse_nonnegative_decimal(amount), note)
            await gowa_send_text(jid, f"✅ Stok `{row['sku']}` menjadi {_qty(row['stock_on_hand'])}; perubahan {_qty(delta)}.\n\n0️⃣ Menu Utama")
            return True

        if upper in {"LAPORAN", "LAPORAN HARI INI", "LAPORAN BULAN"}:
            today = date.today()
            start = today.replace(day=1) if upper == "LAPORAN BULAN" else today
            ctx, row, low = await pos.pos_summary(jid, start, today)
            await gowa_send_text(jid, f"📊 *LAPORAN POS*\n🏪 {ctx['business_name']}\nPeriode: {start:%d-%m-%Y} s.d. {today:%d-%m-%Y}\nTransaksi: *{row['sales_count']}*\nOmzet: *{rupiah(row['revenue'])}*\nLaba kotor: *{rupiah(row['gross_profit'])}*\nSKU stok rendah: *{low}*\n\n0️⃣ Menu Utama")
            return True

        await gowa_send_text(jid, "⚠️ Perintah POS belum dikenali.\n\nKetik *POS* untuk melihat contoh.")
        return True
    except (PermissionDenied, PermissionError) as exc:
        await gowa_send_text(jid, f"⛔ {exc}\n\n0️⃣ Menu Utama")
        return True
    except (ValueError, LookupError) as exc:
        await gowa_send_text(jid, f"⚠️ {exc}\n\nKetik *POS* untuk melihat format.\n0️⃣ Menu Utama")
        return True

async def start_guided_transaction(jid: str, tx_type: str):
    label = "PEMASUKAN" if tx_type == "income" else "PENGELUARAN"
    icon = "💰" if tx_type == "income" else "💸"
    state = "guided_income_amount" if tx_type == "income" else "guided_expense_amount"
    await db.set_pending_action(jid, state, {}, ttl_minutes=15)
    await gowa_send_text(
        jid,
        f"{icon} *CATAT {label} — LANGKAH 1/2*\n\n"
        "Masukkan *nominal* saja.\n\n"
        "Contoh: `50000`, `75rb`, atau `1,5jt`\n\n"
        "0️⃣ Batal & kembali ke Menu",
    )


async def dispatch_main_action(jid: str, action: str) -> bool:
    access = await db.get_whatsapp_access(jid)
    item = next((entry for entry in MAIN_MENU_CATALOG if entry[0] == action), None)
    if not item:
        return False
    if item[2] and (not access or not has_permission(access["role"], item[2])):
        await gowa_send_text(
            jid,
            f"⛔ Menu ini tidak tersedia untuk peran *{role_label(access['role'] if access else '-')}*.\n\n0️⃣ Menu Utama",
        )
        return True
    if action == "income":
        await start_guided_transaction(jid, "income")
    elif action == "expense":
        await start_guided_transaction(jid, "expense")
    elif action == "finance_summary":
        await send_summary(jid)
    elif action == "reminders":
        await send_reminder_menu(jid)
    elif action == "export":
        await send_export_menu(jid)
    elif action == "ai":
        await db.set_pending_action(jid, "menu_ai", {}, ttl_minutes=20)
        await gowa_send_text(
            jid,
            "🤖 *ASISTEN AI*\n\nTulis dengan bahasa biasa.\nContoh:\n"
            "• *jual 2 kopi dan 1 roti bayar QRIS*\n"
            "• *berapa stok kopi susu?*\n"
            "• *laporan POS hari ini*\n"
            "• *bagaimana kondisi keuangan bulan ini?*\n\n"
            "Aksi tulis hasil AI selalu meminta konfirmasi.\n\n0️⃣ Kembali",
        )
    elif action == "business":
        await send_business_menu(jid)
    elif action == "help":
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, help_examples())
    elif action == "pos":
        await send_pos_menu(jid)
    return True


async def handle_menu_state(jid: str, text: str) -> bool:
    pending = await db.get_pending_action(jid)
    if not pending:
        return False

    action_type = pending["action_type"]
    managed_prefixes = (
        "menu_", "guided_", "reminder_", "quick_", "export_", "pos_", "sale_", "stock_"
    )
    if not action_type.startswith(managed_prefixes):
        return False

    raw = (text or "").strip()
    lower = raw.lower()
    choice = first_choice(raw)
    payload = json_object(pending["payload"])

    zero_is_data = action_type in {
        "guided_pos_product_cost", "guided_pos_product_stock",
        "guided_pos_product_min", "guided_pos_stock_qty",
    } or (
        action_type == "guided_pos_product_edit_value"
        and payload.get("edit_field") in {"cost_price", "min_stock"}
    )
    if lower in {"menu", "/menu", "start", "/start", "kembali", "back"} or (choice == "0" and not zero_is_data):
        await send_main_menu(jid)
        return True

    # Numbered menus are shortcuts, not modal locks. A sentence typed while a
    # menu is visible must continue to the normal POS/AI routing pipeline.
    if should_yield_to_free_text(action_type, raw):
        await db.clear_pending_action(jid)
        return False

    if action_type == "menu_main":
        action = selected_menu_action(payload, raw)
        if action:
            return await dispatch_main_action(jid, action)
        await gowa_send_text(
            jid,
            f"⚠️ Pilih angka *1–{len(payload.get('actions') or [])}* atau *0*.",
        )
        return True

    # Friendly POS main menu.
    if action_type == "menu_pos":
        action = selected_menu_action(payload, raw)
        if action == "sale":
            if not await wa_permission_or_reply(jid, "pos.sell"):
                return True
            await db.set_pending_action(jid, "guided_pos_sale_items", {}, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "🧾 *PENJUALAN — LANGKAH 1/2*\n\n"
                "Ketik nama produk dan jumlah.\n"
                "Contoh satu produk: `Susu 1 cup`\n"
                "Contoh beberapa: `Kopi Susu 2 dan Roti Bakar 1`\n\n0️⃣ Batal",
            )
            return True
        if action == "products":
            await db.clear_pending_action(jid)
            await handle_pos_command(jid, "POS PRODUK")
            return True
        if action == "product_add":
            if not await wa_permission_or_reply(jid, "product.write"):
                return True
            await db.set_pending_action(jid, "guided_pos_product_quick_name", {}, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "➕ *TAMBAH PRODUK — 1/3*\n\nKetik nama produk. SKU dibuat otomatis.\n"
                "Satuan boleh ditulis setelah tanda |.\nContoh: *Kopi Susu | cup*\n\n0️⃣ Batal",
            )
            return True
        if action == "product_edit":
            await begin_product_edit(jid)
            return True
        if action == "stock_set":
            if not await wa_permission_or_reply(jid, "stock.write"):
                return True
            await db.set_pending_action(jid, "guided_pos_stock_sku", {}, ttl_minutes=20)
            await gowa_send_text(jid, "📦 *ATUR STOK FISIK — 1/3*\n\nKetik nama atau SKU produk.\nContoh: `Kopi Susu`\n\n0️⃣ Batal")
            return True
        if action == "report":
            if not await wa_permission_or_reply(jid, "pos.read"):
                return True
            await db.set_pending_action(jid, "guided_pos_report", {}, ttl_minutes=20)
            await gowa_send_text(jid, "📊 *LAPORAN POS*\n\n1️⃣ Hari Ini\n2️⃣ Bulan Ini\n\n0️⃣ Batal")
            return True
        if action == "staff":
            try:
                await send_staff_menu(jid)
            except PermissionDenied as exc:
                await gowa_send_text(jid, f"⛔ {exc}\n\n0️⃣ Menu Utama")
            return True
        if action == "low_stock":
            await db.clear_pending_action(jid)
            await handle_pos_command(jid, "POS STOK RENDAH")
            return True
        await gowa_send_text(
            jid,
            f"⚠️ Pilih angka *1–{len(payload.get('actions') or [])}* atau *0*.",
        )
        return True

    # Friendly employee management menu.
    if action_type == "menu_staff":
        if choice == "1":
            await send_staff_list(jid)
            return True
        if choice == "2":
            if not await wa_permission_or_reply(jid, "staff.write"):
                return True
            await db.set_pending_action(jid, "guided_staff_name", {}, ttl_minutes=20)
            await gowa_send_text(jid, "➕ *TAMBAH KARYAWAN — 1/2*\n\nKetik nama karyawan.\nContoh: *Budi Kasir*\n\n0️⃣ Batal")
            return True
        if choice == "3":
            if await wa_permission_or_reply(jid, "staff.write"):
                await begin_staff_pick(jid, "role")
            return True
        if choice == "4":
            if await wa_permission_or_reply(jid, "staff.write"):
                await begin_staff_pick(jid, "remove")
            return True
        await gowa_send_text(jid, "⚠️ Pilih *1–4* atau *0*.")
        return True

    # Stateful sale completion used by both the guided menu and AI.
    if action_type == "sale_items_needed":
        quick_sale = parse_quick_sale_request(raw)
        if quick_sale:
            items, quick_payment = quick_sale
            await begin_sale_preview(
                jid, items, quick_payment or payload.get("payment_method")
            )
            return True
        try:
            items = parse_human_sale_items(raw)
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}\n\n0️⃣ Batal")
            return True
        await begin_sale_preview(jid, items, payload.get("payment_method"))
        return True

    if action_type == "sale_payment_needed":
        quick_sale = parse_quick_sale_request(raw)
        if quick_sale:
            items, quick_payment = quick_sale
            await begin_sale_preview(jid, items, quick_payment)
            return True
        method = payment_method_from_text(raw)
        if not method:
            await gowa_send_text(jid, "⚠️ Pilih *1–5*, atau ketik tunai/transfer/QRIS/kartu/lainnya.")
            return True
        await begin_sale_preview(jid, payload.get("draft_items") or [], method)
        return True

    if action_type == "sale_product_choice":
        try:
            selected = int(re.sub(r"[^0-9]", "", raw))
            product = payload["candidates"][selected - 1]
            item_index = int(payload["item_index"])
            draft_items = list(payload["draft_items"])
            draft_items[item_index] = {
                "product_query": product["sku"],
                "qty": draft_items[item_index]["qty"],
            }
        except (ValueError, IndexError, KeyError, TypeError):
            await gowa_send_text(jid, "⚠️ Nomor produk tidak valid. Pilih nomor yang tersedia.")
            return True
        await begin_sale_preview(jid, draft_items, payload.get("payment_method"))
        return True

    if action_type == "sale_product_replace":
        if lower in {"daftar produk", "produk", "list produk"}:
            ctx, rows = await pos.list_products(jid, limit=50)
            lines = [f"📦 *PRODUK · {ctx['business_name']}*", ""]
            lines.extend(
                f"• *{row['name']}* (`{row['sku']}`) · {rupiah(row['sell_price'])}"
                for row in rows
            )
            lines.append("\nKetik nama/SKU pengganti.\n0️⃣ Batal")
            await gowa_send_text(jid, "\n".join(lines))
            return True
        draft_items = list(payload.get("draft_items") or [])
        try:
            item_index = int(payload["item_index"])
            draft_items[item_index] = {
                "product_query": raw,
                "qty": draft_items[item_index]["qty"],
            }
        except (ValueError, IndexError, KeyError, TypeError):
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, "⚠️ Draft transaksi tidak valid. Silakan mulai kembali.")
            return True
        await begin_sale_preview(jid, draft_items, payload.get("payment_method"))
        return True

    if action_type == "stock_query_needed":
        if lower in {"daftar produk", "produk", "list produk"}:
            ctx, rows = await pos.list_products(jid, limit=50)
            lines = [f"📦 *PRODUK · {ctx['business_name']}*", ""]
            lines.extend(f"• *{row['name']}* (`{row['sku']}`)" for row in rows)
            lines.append("\nKetik nama/SKU yang ingin dicek.\n0️⃣ Batal")
            await gowa_send_text(jid, "\n".join(lines))
            return True
        await answer_stock_query(jid, raw)
        return True

    # Guided sale.
    if action_type == "guided_pos_sale_items":
        quick_sale = parse_quick_sale_request(raw)
        if quick_sale:
            items, quick_payment = quick_sale
            await begin_sale_preview(jid, items, quick_payment)
            return True
        try:
            items = parse_human_sale_items(raw)
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}\nContoh: `Susu 1 cup, Roti Bakar 2`\n\n0️⃣ Batal")
            return True
        serializable = serializable_sale_items(items)
        await db.set_pending_action(jid, "guided_pos_sale_payment", {"items": serializable}, ttl_minutes=20)
        await gowa_send_text(jid, "💳 *PENJUALAN — LANGKAH 2/2*\n\nPilih pembayaran:\n1️⃣ Tunai\n2️⃣ Transfer\n3️⃣ QRIS\n4️⃣ Kartu\n5️⃣ Lainnya\n\n0️⃣ Batal")
        return True

    if action_type == "guided_pos_sale_payment":
        method = payment_method_from_text(raw)
        if not method:
            await gowa_send_text(jid, "⚠️ Pilih *1–5*, atau ketik tunai/transfer/QRIS/kartu/lainnya.")
            return True
        await begin_sale_preview(jid, payload["items"], method)
        return True

    # Guided product creation.
    if action_type == "guided_pos_product_quick_name":
        name_parts = [part.strip() for part in raw.split("|", 1)]
        name = name_parts[0]
        unit = name_parts[1] if len(name_parts) == 2 else "pcs"
        if len(name) < 2 or len(name) > 160:
            await gowa_send_text(jid, "⚠️ Nama produk harus 2–160 karakter.")
            return True
        if not 1 <= len(unit) <= 30:
            await gowa_send_text(jid, "⚠️ Satuan harus 1–30 karakter. Contoh: `Kopi Susu | cup`.")
            return True
        try:
            sku = await pos.suggest_product_sku(jid, name)
        except (PermissionDenied, PermissionError, ValueError, RuntimeError) as exc:
            await gowa_send_text(jid, f"⚠️ Tidak dapat membuat SKU: {exc}")
            return True
        quick_payload = {
            "sku": sku, "name": name, "unit": unit,
            "cost_price": 0, "min_stock": "0", "quick_flow": True,
        }
        await db.set_pending_action(jid, "guided_pos_product_sell", quick_payload, ttl_minutes=20)
        await gowa_send_text(
            jid,
            f"➕ *TAMBAH PRODUK — 2/3*\n\nNama: *{name}*\nSKU otomatis: `{sku}`\n"
            f"Satuan: *{unit}*\n\nKetik harga jual. Harga modal boleh ditambahkan setelah |.\n"
            "Contoh cepat: `18000`\nContoh lengkap: `18000 | 9000`",
        )
        return True

    if action_type == "guided_pos_product_sku":
        try:
            sku = normalize_sku(raw)
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}")
            return True
        await db.set_pending_action(jid, "guided_pos_product_name", {"sku": sku}, ttl_minutes=20)
        await gowa_send_text(jid, f"➕ *TAMBAH PRODUK — 2/7*\nSKU: `{sku}`\n\nKetik nama produk.\nContoh: *Kopi Susu*")
        return True

    if action_type == "guided_pos_product_name":
        if len(raw) < 2 or len(raw) > 160:
            await gowa_send_text(jid, "⚠️ Nama produk harus 2–160 karakter.")
            return True
        payload["name"] = raw
        payload.update({"unit": "pcs", "cost_price": 0, "min_stock": "0", "quick_flow": True})
        await db.set_pending_action(jid, "guided_pos_product_sell", payload, ttl_minutes=20)
        await gowa_send_text(jid, "➕ *TAMBAH PRODUK — 2/3*\n\nKetik harga jual. Harga modal opsional.\nContoh: `18000 | 9000`")
        return True

    if action_type in {"guided_pos_product_sell", "guided_pos_product_cost"}:
        try:
            if action_type.endswith("sell"):
                price_parts = [part.strip() for part in raw.split("|", 1)]
                amount = parse_nonnegative_money(price_parts[0])
                supplied_cost = parse_nonnegative_money(price_parts[1]) if len(price_parts) == 2 else None
            else:
                amount = parse_nonnegative_money(raw)
                supplied_cost = None
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}\nContoh: `18000` atau `18000 | 9000`")
            return True
        if action_type.endswith("sell"):
            if amount <= 0:
                await gowa_send_text(jid, "⚠️ Harga jual harus lebih dari 0. Contoh: `18000` atau `18rb`.")
                return True
            payload["sell_price"] = amount
            if supplied_cost is not None:
                payload["cost_price"] = supplied_cost
            if payload.get("quick_flow"):
                await db.set_pending_action(jid, "guided_pos_product_stock", payload, ttl_minutes=20)
                await gowa_send_text(
                    jid,
                    "➕ *TAMBAH PRODUK — 3/3*\n\nKetik stok awal. Batas minimum boleh ditambahkan setelah |.\n"
                    "Contoh cepat: `20`\nContoh lengkap: `20 | 5`\n\n"
                    "Nilai `0` berarti stok awal kosong. Ketik *MENU* untuk batal.",
                )
            elif supplied_cost is None:
                await db.set_pending_action(jid, "guided_pos_product_cost", payload, ttl_minutes=20)
                await gowa_send_text(jid, "➕ *TAMBAH PRODUK — 3/6*\n\nKetik harga modal. Jika tidak ada, ketik `0`.")
            else:
                await db.set_pending_action(jid, "guided_pos_product_stock", payload, ttl_minutes=20)
                await gowa_send_text(jid, "➕ *TAMBAH PRODUK — 4/6*\n\nKetik stok awal.\nContoh: `20`")
        else:
            payload["cost_price"] = amount
            await db.set_pending_action(jid, "guided_pos_product_stock", payload, ttl_minutes=20)
            await gowa_send_text(jid, "➕ *TAMBAH PRODUK — 4/6*\n\nKetik stok awal.\nContoh: `20`")
        return True

    if action_type == "guided_pos_product_stock":
        try:
            stock_parts = [part.strip() for part in raw.split("|", 1)]
            amount = parse_nonnegative_decimal(stock_parts[0])
            supplied_min = parse_nonnegative_decimal(stock_parts[1]) if len(stock_parts) == 2 else None
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}")
            return True
        payload["opening_stock"] = str(amount)
        if supplied_min is not None:
            payload["min_stock"] = str(supplied_min)
        if payload.get("quick_flow"):
            await db.set_pending_action(jid, "pos_product_create", payload, ttl_minutes=10)
            await gowa_send_text(jid, product_confirmation_text(payload))
            return True
        await db.set_pending_action(jid, "guided_pos_product_unit", payload, ttl_minutes=20)
        await gowa_send_text(jid, "➕ *TAMBAH PRODUK — 5/6*\n\nKetik satuan.\nContoh: `pcs`, `cup`, `kg`, atau `meter`")
        return True

    if action_type == "guided_pos_product_unit":
        if not 1 <= len(raw) <= 30:
            await gowa_send_text(jid, "⚠️ Satuan harus 1–30 karakter.")
            return True
        payload["unit"] = raw
        await db.set_pending_action(jid, "guided_pos_product_min", payload, ttl_minutes=20)
        await gowa_send_text(jid, "➕ *TAMBAH PRODUK — 6/6*\n\nKetik batas stok minimum. Jika tidak diperlukan, ketik `0`.\nContoh: `5`")
        return True

    if action_type == "guided_pos_product_min":
        try:
            minimum = parse_nonnegative_decimal(raw)
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}")
            return True
        payload["min_stock"] = str(minimum)
        await db.set_pending_action(jid, "pos_product_create", payload, ttl_minutes=10)
        await gowa_send_text(jid, product_confirmation_text(payload))
        return True

    # Guided product editing. Only the selected field changes; the remaining
    # values are carried from the tenant-scoped product record.
    if action_type == "guided_pos_product_edit_pick":
        if lower in {"daftar produk", "produk", "list produk"}:
            ctx, rows = await pos.list_products(jid, limit=50)
            lines = [f"📦 *PRODUK · {ctx['business_name']}*", ""]
            lines.extend(f"• *{row['name']}* (`{row['sku']}`)" for row in rows)
            lines.append("\nKetik nama/SKU yang akan diedit.\n0️⃣ Batal")
            await gowa_send_text(jid, "\n".join(lines))
            return True
        await begin_product_edit(jid, raw)
        return True

    if action_type == "guided_pos_product_edit_choice":
        try:
            index = int(re.sub(r"[^0-9]", "", raw)) - 1
            selected = payload["candidates"][index]
        except (ValueError, IndexError, KeyError, TypeError):
            await gowa_send_text(jid, "⚠️ Pilih nomor produk yang tersedia.")
            return True
        await begin_product_edit(jid, selected["sku"])
        return True

    if action_type == "guided_pos_product_edit_field":
        fields = {
            "1": ("name", "nama baru", payload["name"]),
            "2": ("sell_price", "harga jual baru", rupiah(payload["sell_price"])),
            "3": ("cost_price", "harga modal baru", rupiah(payload["cost_price"])),
            "4": ("unit", "satuan baru", payload["unit"]),
            "5": ("min_stock", "batas stok minimum baru", payload["min_stock"]),
        }
        if choice not in fields:
            await gowa_send_text(jid, "⚠️ Pilih *1–5* atau *0*.")
            return True
        field, label, current = fields[choice]
        payload["edit_field"] = field
        await db.set_pending_action(
            jid, "guided_pos_product_edit_value", payload, ttl_minutes=20
        )
        examples = {
            "name": "Contoh: *Susu Cokelat*",
            "sell_price": "Contoh: *18000* atau *18rb*",
            "cost_price": "Contoh: *9000* atau *0*",
            "unit": "Contoh: *cup*, *pcs*, atau *kg*",
            "min_stock": "Contoh: *5* atau *0*",
        }
        await gowa_send_text(
            jid,
            f"✏️ Ketik *{label}*.\nNilai sekarang: *{current}*\n"
            f"{examples[field]}\n\n0️⃣ Batal",
        )
        return True

    if action_type == "guided_pos_product_edit_value":
        field = payload.get("edit_field")
        try:
            if field == "name":
                if not 2 <= len(raw) <= 160:
                    raise ValueError("Nama produk harus 2–160 karakter.")
                payload["name"] = raw
            elif field in {"sell_price", "cost_price"}:
                amount = parse_nonnegative_money(raw)
                if field == "sell_price" and amount <= 0:
                    raise ValueError("Harga jual harus lebih dari 0.")
                payload[field] = amount
            elif field == "unit":
                if not 1 <= len(raw) <= 30:
                    raise ValueError("Satuan harus 1–30 karakter.")
                payload["unit"] = raw
            elif field == "min_stock":
                payload["min_stock"] = str(parse_nonnegative_decimal(raw))
            else:
                raise ValueError("Pilihan data edit tidak valid.")
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}\nSilakan ketik ulang atau *0* untuk batal.")
            return True
        payload.pop("edit_field", None)
        await db.set_pending_action(jid, "pos_product_update", payload, ttl_minutes=10)
        await gowa_send_text(jid, product_update_confirmation_text(payload))
        return True

    # Guided stock adjustment.
    if action_type == "guided_pos_stock_sku":
        _, product, candidates = await pos.resolve_product(jid, raw)
        if not product:
            if candidates:
                options = "\n".join(
                    f"• *{item['name']}* — ketik `{item['sku']}`"
                    for item in candidates
                )
                await gowa_send_text(jid, f"🔎 Produk belum spesifik. Pilih salah satu:\n\n{options}\n\n0️⃣ Batal")
            else:
                await gowa_send_text(jid, "⚠️ Produk tidak ditemukan. Ketik nama/SKU lain atau 0 untuk batal.")
            return True
        sku = product["sku"]
        await db.set_pending_action(jid, "guided_pos_stock_qty", {"sku": sku, "name": product["name"]}, ttl_minutes=20)
        await gowa_send_text(jid, f"📦 *ATUR STOK — 2/3*\n{product['name']} (`{sku}`)\nStok sekarang: *{_qty(product['stock_on_hand'])}*\n\nKetik jumlah stok fisik terbaru.")
        return True

    if action_type == "guided_pos_stock_qty":
        try:
            amount = parse_nonnegative_decimal(raw)
        except ValueError as exc:
            await gowa_send_text(jid, f"⚠️ {exc}")
            return True
        payload["new_stock"] = str(amount)
        await db.set_pending_action(jid, "guided_pos_stock_note", payload, ttl_minutes=20)
        await gowa_send_text(jid, "📦 *ATUR STOK — 3/3*\n\nKetik alasan perubahan.\nContoh: *hasil hitung gudang*")
        return True

    if action_type == "guided_pos_stock_note":
        if len(raw) < 2:
            await gowa_send_text(jid, "⚠️ Alasan terlalu pendek.")
            return True
        payload["note"] = raw[:300]
        await db.set_pending_action(jid, "pos_stock_set", payload, ttl_minutes=10)
        await gowa_send_text(jid, f"📦 *KONFIRMASI STOK*\n\nProduk: *{payload['name']}* (`{payload['sku']}`)\nStok baru: *{payload['new_stock']}*\nAlasan: {payload['note']}\n\n1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL")
        return True

    if action_type == "guided_pos_report":
        if choice not in {"1", "2"}:
            await gowa_send_text(jid, "⚠️ Pilih *1* atau *2*.")
            return True
        await db.clear_pending_action(jid)
        await handle_pos_command(jid, "POS LAPORAN HARI INI" if choice == "1" else "POS LAPORAN BULAN")
        return True

    # Guided staff invitation and maintenance.
    if action_type == "guided_staff_name":
        if len(raw) < 2 or len(raw) > 160:
            await gowa_send_text(jid, "⚠️ Nama harus 2–160 karakter.")
            return True
        access = await db.get_whatsapp_access(jid)
        actor_role = access["role"] if access else ""
        await db.set_pending_action(jid, "guided_staff_role", {"name": raw, "actor_role": actor_role}, ttl_minutes=20)
        await gowa_send_text(jid, f"👤 *TAMBAH KARYAWAN — 2/2*\n\nPilih role:\n{staff_role_prompt(actor_role)}\n\n0️⃣ Batal")
        return True

    if action_type == "guided_staff_role":
        roles = staff_role_choices(payload.get("actor_role") or "")
        if choice not in roles:
            await gowa_send_text(jid, "⚠️ Pilih role yang tersedia pada menu.")
            return True
        payload["role"] = roles[choice]
        await db.set_pending_action(jid, "pos_staff_invite", payload, ttl_minutes=10)
        await gowa_send_text(jid, f"👤 *KONFIRMASI KARYAWAN*\n\nNama: *{payload['name']}*\nPeran: *{role_label(payload['role'])}*\n\n1️⃣ BUAT UNDANGAN\n2️⃣ EDIT\n0️⃣ BATAL")
        return True

    if action_type in {"guided_staff_role_pick", "guided_staff_remove_pick"}:
        try:
            selected = int(re.sub(r"[^0-9]", "", raw))
            staff = payload["staff"][selected - 1]
        except (ValueError, IndexError, KeyError):
            await gowa_send_text(jid, "⚠️ Nomor karyawan tidak valid.")
            return True
        if action_type == "guided_staff_role_pick":
            access = await db.get_whatsapp_access(jid)
            actor_role = access["role"] if access else ""
            await db.set_pending_action(jid, "guided_staff_role_value", {"staff": staff, "actor_role": actor_role}, ttl_minutes=20)
            await gowa_send_text(jid, f"Ubah role *{staff['name']}* menjadi:\n{staff_role_prompt(actor_role)}\n\n0️⃣ Batal")
        else:
            await db.set_pending_action(jid, "pos_staff_remove", {"staff": staff}, ttl_minutes=10)
            await gowa_send_text(jid, f"⚠️ Nonaktifkan *{staff['name']}* ({staff['role']})?\n\n1️⃣ NONAKTIFKAN\n0️⃣ BATAL")
        return True

    if action_type == "guided_staff_role_value":
        roles = staff_role_choices(payload.get("actor_role") or "")
        if choice not in roles:
            await gowa_send_text(jid, "⚠️ Pilih role yang tersedia pada menu.")
            return True
        payload["role"] = roles[choice]
        await db.set_pending_action(jid, "pos_staff_role", payload, ttl_minutes=10)
        await gowa_send_text(jid, f"👤 Ubah *{payload['staff']['name']}* dari {role_label(payload['staff']['role'])} menjadi *{role_label(payload['role'])}*?\n\n1️⃣ SIMPAN\n0️⃣ BATAL")
        return True

    # Guided transactions.
    if action_type in {"guided_income_amount", "guided_expense_amount"}:
        amount = parse_money_input(raw)
        if amount is None:
            await gowa_send_text(jid, "⚠️ Nominal belum dikenali. Contoh: *50000*, *75rb*, atau *1,5jt*.\n\n0️⃣ Menu Utama")
            return True
        tx_type = "income" if action_type == "guided_income_amount" else "expense"
        next_state = "guided_income_desc" if tx_type == "income" else "guided_expense_desc"
        await db.set_pending_action(jid, next_state, {"amount": amount}, ttl_minutes=20)
        await gowa_send_text(
            jid,
            "📝 *LANGKAH 2/2 — KETERANGAN*\n\n"
            f"Nominal: *{rupiah(amount)}*\n\n"
            "Ketik keterangannya.\nContoh: *penjualan sparepart* atau *beli bensin*.\n\n0️⃣ Batal",
        )
        return True

    if action_type in {"guided_income_desc", "guided_expense_desc"}:
        if len(raw) < 2:
            await gowa_send_text(jid, "Keterangan terlalu pendek. Silakan ketik kembali.")
            return True
        amount = int(payload["amount"])
        tx_type = "income" if action_type == "guided_income_desc" else "expense"
        await db.clear_pending_action(jid)
        phrase = f"catat {'pemasukan' if tx_type == 'income' else 'pengeluaran'} {amount} {raw}"
        await handle_ai(jid, phrase)
        return True

    # Reminder main menu.
    if action_type == "menu_reminder":
        if choice == "1":
            await db.set_pending_action(jid, "reminder_type", {}, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "➕ *BUAT PENGINGAT*\n\nPilih jenis jadwal:\n\n"
                "1️⃣ Sekali\n2️⃣ Setiap Hari\n3️⃣ Setiap Minggu\n4️⃣ Setiap Bulan\n\n0️⃣ Kembali",
            )
            return True
        if choice == "2":
            await send_active_reminders(jid, "view")
            return True
        if choice == "3":
            await send_active_reminders(jid, "edit")
            return True
        if choice == "4":
            await send_active_reminders(jid, "delete")
            return True
        await gowa_send_text(jid, "⚠️ Pilih *1–4* atau *0*.")
        return True

    if action_type in {"reminder_type", "reminder_edit_type"}:
        kinds = {"1": "once", "2": "daily", "3": "weekly", "4": "monthly"}
        if choice not in kinds:
            await gowa_send_text(jid, "⚠️ Pilih *1*, *2*, *3*, *4*, atau *0*.")
            return True
        new_payload = dict(payload)
        new_payload["recurrence_type"] = kinds[choice]
        if action_type == "reminder_type":
            await db.set_pending_action(jid, "reminder_message", new_payload, ttl_minutes=20)
            await gowa_send_text(jid, "📝 Apa yang ingin diingatkan?\nContoh: *bayar listrik Rp100.000*\n\n0️⃣ Batal")
        else:
            new_payload["mode"] = "edit"
            await begin_reminder_schedule(jid, new_payload)
        return True

    if action_type == "reminder_message":
        if len(raw) < 2:
            await gowa_send_text(jid, "Isi pengingat terlalu pendek.")
            return True
        payload["message"] = raw[:500]
        await begin_reminder_schedule(jid, payload)
        return True

    if action_type == "reminder_once_date":
        d = parse_date_input(raw)
        if not d:
            await gowa_send_text(jid, "⚠️ Tanggal tidak dikenali. Contoh: *01-09-2026* atau *besok*.")
            return True
        payload["date"] = d.isoformat()
        await db.set_pending_action(jid, "reminder_time", payload, ttl_minutes=20)
        await gowa_send_text(jid, f"🕘 Jam berapa pada *{d.strftime('%d-%m-%Y')}*?\nContoh: `09:00`")
        return True

    if action_type == "reminder_weekday":
        wd = parse_weekday_input(raw)
        if wd is None:
            await gowa_send_text(jid, "⚠️ Pilih hari 1–7 atau ketik nama hari, mis. *Senin*.")
            return True
        payload["recurrence_weekday"] = wd
        await db.set_pending_action(jid, "reminder_time", payload, ttl_minutes=20)
        await gowa_send_text(jid, f"🕘 Jam berapa setiap *{WEEKDAY_NAMES[wd]}*?\nContoh: `09:00`")
        return True

    if action_type == "reminder_monthday":
        try:
            day = int(re.sub(r"[^0-9]", "", raw))
        except Exception:
            day = 0
        if not 1 <= day <= 31:
            await gowa_send_text(jid, "⚠️ Ketik tanggal *1–31*.")
            return True
        payload["recurrence_day"] = day
        await db.set_pending_action(jid, "reminder_time", payload, ttl_minutes=20)
        await gowa_send_text(jid, f"🕘 Jam berapa setiap tanggal *{day}*?\nContoh: `09:00`")
        return True

    if action_type == "reminder_time":
        clock = parse_time_input(raw)
        if not clock:
            await gowa_send_text(jid, "⚠️ Jam tidak dikenali. Contoh: *09:00* atau *jam 9*.")
            return True
        kind = payload["recurrence_type"]
        if kind == "once":
            d = date.fromisoformat(payload["date"])
            due = _combine_local(d, clock)
            if due <= datetime.now(JAKARTA):
                await gowa_send_text(jid, "⚠️ Waktu tersebut sudah lewat. Masukkan waktu yang akan datang.")
                return True
        elif kind == "daily":
            due = next_daily_due(clock)
        elif kind == "weekly":
            due = next_weekly_due(int(payload["recurrence_weekday"]), clock)
        else:
            due = next_monthly_due(int(payload["recurrence_day"]), clock)

        schedule = schedule_payload(
            payload.get("message") or "Pengingat",
            due,
            kind,
            payload.get("recurrence_day"),
            payload.get("recurrence_weekday"),
        )
        if payload.get("mode") == "edit":
            schedule["reminder_id"] = payload["reminder_id"]
            await set_reminder_confirmation(jid, schedule, "reminder_schedule_update")
        else:
            await set_reminder_confirmation(jid, schedule, "reminder")
        return True

    # Quick reminder continuation (fixes clarification context loss).
    if action_type == "quick_need_when":
        await db.clear_pending_action(jid)
        combined = f"ingatkan {payload.get('message') or 'Pengingat'} {raw}"
        if await try_quick_reminder(jid, combined):
            return True
        await db.set_pending_action(jid, "quick_need_when", payload, ttl_minutes=20)
        await gowa_send_text(
            jid,
            "⚠️ Jadwal belum dikenali. Contoh:\n"
            "• `besok jam 9`\n"
            "• `1 September 2026 jam 09:00`\n"
            "• `setiap tanggal 1 jam 09:00`",
        )
        return True

    if action_type in {"quick_monthly_time", "quick_daily_time", "quick_weekly_time", "quick_once_time"}:
        clock = parse_time_input(raw)
        if not clock:
            await gowa_send_text(jid, "⚠️ Jam tidak dikenali. Contoh: *09:00* atau *jam 9*.")
            return True
        message = payload.get("message") or "Pengingat"
        if action_type == "quick_monthly_time":
            day = int(payload["day"])
            due = next_monthly_due(day, clock)
            schedule = schedule_payload(message, due, "monthly", recurrence_day=day)
        elif action_type == "quick_daily_time":
            due = next_daily_due(clock)
            schedule = schedule_payload(message, due, "daily")
        elif action_type == "quick_weekly_time":
            wd = int(payload["weekday"])
            due = next_weekly_due(wd, clock)
            schedule = schedule_payload(message, due, "weekly", recurrence_weekday=wd)
        else:
            d = date.fromisoformat(payload["date"])
            due = _combine_local(d, clock)
            if due <= datetime.now(JAKARTA):
                await gowa_send_text(jid, "⚠️ Waktu tersebut sudah lewat. Masukkan waktu yang akan datang.")
                return True
            schedule = schedule_payload(message, due, "once")
        await set_reminder_confirmation(jid, schedule)
        return True

    # Edit/delete reminder selection.
    if action_type in {"reminder_edit_pick", "reminder_delete_pick"}:
        try:
            idx = int(re.sub(r"[^0-9]", "", raw)) - 1
        except Exception:
            idx = -1
        ids = payload.get("ids") or []
        if idx < 0 or idx >= len(ids):
            await gowa_send_text(jid, f"⚠️ Pilih nomor 1–{len(ids)} atau 0.")
            return True
        reminder_id = ids[idx]
        row = await db.get_reminder(jid, reminder_id)
        if not row:
            await gowa_send_text(jid, "Pengingat sudah tidak tersedia.")
            await send_reminder_menu(jid)
            return True
        if action_type == "reminder_delete_pick":
            await db.set_pending_action(
                jid, "reminder_cancel",
                {"reminder_id": reminder_id, "message": row["message"]},
                ttl_minutes=20,
            )
            await gowa_send_text(
                jid,
                "🗑 *HAPUS PENGINGAT?*\n\n"
                f"📝 {row['message']}\n🗓 {recurrence_label(row)}\n\n"
                "1️⃣ HAPUS\n0️⃣ BATAL",
            )
        else:
            await db.set_pending_action(jid, "reminder_edit_menu", {"reminder_id": reminder_id}, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "✏️ *EDIT PENGINGAT*\n\n"
                f"📝 {row['message']}\n🗓 {recurrence_label(row)}\n\n"
                "1️⃣ Edit Isi\n2️⃣ Edit Jadwal\n0️⃣ Kembali",
            )
        return True

    if action_type == "reminder_edit_menu":
        if choice == "1":
            await db.set_pending_action(jid, "reminder_edit_message_value", payload, ttl_minutes=20)
            await gowa_send_text(jid, "📝 Ketik isi pengingat yang baru.\n\n0️⃣ Batal")
            return True
        if choice == "2":
            current = await db.get_reminder(jid, payload["reminder_id"])
            edit_payload = {
                "reminder_id": payload["reminder_id"],
                "message": current["message"] if current else "Pengingat",
            }
            await db.set_pending_action(jid, "reminder_edit_type", edit_payload, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "🗓 *JADWAL BARU*\n\n1️⃣ Sekali\n2️⃣ Setiap Hari\n3️⃣ Setiap Minggu\n4️⃣ Setiap Bulan\n\n0️⃣ Batal",
            )
            return True
        await gowa_send_text(jid, "⚠️ Pilih *1*, *2*, atau *0*.")
        return True

    if action_type == "reminder_edit_message_value":
        if len(raw) < 2:
            await gowa_send_text(jid, "Isi terlalu pendek.")
            return True
        await db.set_pending_action(
            jid, "reminder_message_update",
            {"reminder_id": payload["reminder_id"], "message": raw[:500]},
            ttl_minutes=20,
        )
        await gowa_send_text(jid, f"✏️ Ubah isi menjadi:\n*{raw[:500]}*\n\n1️⃣ SIMPAN\n0️⃣ BATAL")
        return True

    # Export menu + custom range.
    if action_type == "menu_export":
        options = {
            "1": ("EXPORT EXCEL", "⏳ Menyiapkan Excel bulan ini..."),
            "2": ("EXPORT PDF", "⏳ Menyiapkan PDF bulan ini..."),
            "3": ("EXPORT EXCEL SEMUA", "⏳ Menyiapkan Excel semua data..."),
            "4": ("EXPORT PDF SEMUA", "⏳ Menyiapkan PDF semua data..."),
        }
        if choice in options:
            command, notice = options[choice]
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, notice)
            await handle_export(jid, command)
            return True
        if choice == "5":
            await db.set_pending_action(jid, "export_range_start", {}, ttl_minutes=20)
            await gowa_send_text(jid, "📅 Tanggal awal?\nFormat: *DD-MM-YYYY*\nContoh: `01-08-2026`")
            return True
        await gowa_send_text(jid, "⚠️ Pilih *1–5* atau *0*.")
        return True

    if action_type == "export_range_start":
        d = parse_date_input(raw)
        if not d:
            await gowa_send_text(jid, "⚠️ Tanggal awal tidak valid. Gunakan *DD-MM-YYYY*.")
            return True
        await db.set_pending_action(jid, "export_range_end", {"start": d.isoformat()}, ttl_minutes=20)
        await gowa_send_text(jid, "📅 Tanggal akhir?\nFormat: *DD-MM-YYYY*")
        return True

    if action_type == "export_range_end":
        d = parse_date_input(raw)
        start = date.fromisoformat(payload["start"])
        if not d or d < start:
            await gowa_send_text(jid, "⚠️ Tanggal akhir harus valid dan tidak boleh sebelum tanggal awal.")
            return True
        await db.set_pending_action(
            jid, "export_range_format",
            {"start": start.isoformat(), "end": d.isoformat()}, ttl_minutes=20
        )
        await gowa_send_text(jid, "📤 Pilih format:\n\n1️⃣ Excel\n2️⃣ PDF\n0️⃣ Batal")
        return True

    if action_type == "export_range_format":
        if choice not in {"1", "2"}:
            await gowa_send_text(jid, "⚠️ Pilih *1*, *2*, atau *0*.")
            return True
        start = date.fromisoformat(payload["start"])
        end = date.fromisoformat(payload["end"])
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, "⏳ Menyiapkan laporan rentang tanggal...")
        await handle_export(jid, "EXPORT PDF" if choice == "2" else "EXPORT EXCEL", start, end)
        return True

    # Business/account menu.
    if action_type == "menu_business":
        action = selected_menu_action(payload, raw)
        if action == "rename":
            if not await wa_permission_or_reply(jid, "business.write"):
                return True
            await db.set_pending_action(jid, "menu_business_rename", {}, ttl_minutes=20)
            await gowa_send_text(jid, "✏️ Ketik nama usaha baru.\nContoh: *Toko Jaya Elektronik*\n\n0️⃣ Batal")
            return True
        if action == "link_telegram":
            await db.set_pending_action(jid, "menu_link_telegram", {"attempts": 0}, ttl_minutes=20)
            await gowa_send_text(
                jid,
                "🔗 *HUBUNGKAN TELEGRAM*\n\n"
                "1. Buka bot Telegram dengan akun user.\n"
                "2. Pilih *🔗 Hubungkan WhatsApp* atau ketik `/linkwa`.\n"
                "3. Masukkan kode 6 digit yang diberikan Telegram di sini.\n\n"
                "0️⃣ Batal",
            )
            return True
        await gowa_send_text(
            jid,
            f"⚠️ Pilih angka *1–{len(payload.get('actions') or [])}* atau *0*.",
        )
        return True

    if action_type == "menu_business_rename":
        name = raw.strip()
        if len(name) < 2 or len(name) > 100:
            await gowa_send_text(jid, "Nama usaha harus 2–100 karakter.")
            return True
        await db.set_pending_action(jid, "rename_business", {"business_name": name}, ttl_minutes=20)
        await gowa_send_text(jid, f"✏️ Nama baru: *{name}*\n\n1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL")
        return True

    if action_type == "menu_link_telegram":
        code = re.sub(r"[^0-9]", "", raw)
        if len(code) != 6:
            await gowa_send_text(jid, "⚠️ Kode harus *6 digit*. Masukkan ulang atau 0 untuk batal.")
            return True
        attempts = int(payload.get("attempts", 0)) + 1
        try:
            result = await db.link_whatsapp_to_telegram(jid, code)
        except LookupError:
            if attempts >= 5:
                await db.clear_pending_action(jid)
                await gowa_send_text(jid, "❌ Kode salah/kedaluwarsa terlalu banyak kali. Buat kode baru dari Telegram.\n\n0️⃣ Menu Utama")
            else:
                await db.set_pending_action(jid, "menu_link_telegram", {"attempts": attempts}, ttl_minutes=20)
                await gowa_send_text(jid, f"❌ Kode salah atau kedaluwarsa. Sisa percobaan: {5-attempts}.")
            return True
        except ValueError as exc:
            code_name = str(exc)
            if "WA_ACCOUNT_HAS_DATA" in code_name:
                msg = "⚠️ Akun WhatsApp ini sudah memiliki transaksi/pengingat sendiri. Penyatuan otomatis diblokir agar tidak mencampur data. Hubungi MASTER untuk migrasi aman."
            elif "TARGET_ALREADY_HAS_WHATSAPP" in code_name:
                msg = "⚠️ Akun Telegram tersebut sudah terhubung dengan nomor WhatsApp lain."
            else:
                msg = "⚠️ Akun belum dapat dihubungkan secara otomatis."
            await gowa_send_text(jid, msg + "\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        await gowa_send_text(
            jid,
            "✅ *AKUN BERHASIL DIHUBUNGKAN*\n\n"
            f"👤 {result.get('full_name') or '-'}\n"
            f"🏪 {result.get('business_name') or '-'}\n\n"
            "Sekarang Telegram dan WhatsApp memakai user/tenant yang sama.\n\n0️⃣ Menu Utama",
        )
        return True

    if action_type == "menu_ai":
        await db.clear_pending_action(jid)
        await handle_ai(jid, raw)
        return True

    if action_type == "menu_pos":
        await db.clear_pending_action(jid)
        await handle_pos_command(jid, raw if raw.lower().startswith("pos") else f"POS {raw}")
        return True

    return False

async def handle_main_choice(jid: str, text: str) -> bool:
    choice = first_choice(text)
    if choice is None:
        return False
    if choice == "0":
        await send_main_menu(jid); return True
    legacy_actions = {
        "1": "income", "2": "expense", "3": "finance_summary",
        "4": "reminders", "5": "export", "6": "ai",
        "7": "business", "8": "help", "9": "pos",
    }
    return await dispatch_main_action(jid, legacy_actions.get(choice, ""))

async def pending_response(jid: str, access):
    status = access["access_status"]
    if status == "pending":
        if access["master_notified_at"] is None:
            try:
                await telegram_notify_master(
                    "🟢 USER WHATSAPP BARU\n\n"
                    f"Nama: {access['display_name'] or '-'}\n"
                    f"WhatsApp: {access['phone_number'] or access['whatsapp_jid']}\n"
                    "Status: PENDING\n\n"
                    "Buka menu ⏳ User Pending pada Telegram MASTER, "
                    "lalu pilih ✅ Setujui dan tentukan nama usaha."
                )
                await db.mark_master_notified(jid)
            except Exception:
                log.exception("Gagal mengirim notifikasi pending ke MASTER")

        await gowa_send_text(
            jid,
            "⏳ *AKUN BARU*\n\n"
            "Jika Anda *pemilik usaha*, MASTER akan menyetujui akun dan "
            "membuat ruang usaha Anda.\n\n"
            "Jika Anda *karyawan*, minta kode undangan dari pemilik usaha lalu kirim:\n"
            "`GABUNG 12345678`\n\n"
            f"Nomor terdaftar: {access['phone_number'] or '-'}",
        )
    elif status == "suspended":
        await gowa_send_text(
            jid,
            "⛔ Akses Anda sedang dinonaktifkan oleh administrator.",
        )
    else:
        await gowa_send_text(
            jid,
            "🚫 Akses akun ini tidak tersedia. Hubungi administrator.",
        )


async def handle_confirmation(jid: str, text: str) -> bool:
    pending = await db.get_pending_action(jid)
    if not pending:
        return False
    action_type = pending["action_type"]
    if action_type.startswith(("menu_", "guided_", "quick_", "export_")):
        return False
    if action_type in {
        "reminder_type", "reminder_message", "reminder_once_date",
        "reminder_weekday", "reminder_monthday", "reminder_time",
        "reminder_edit_pick", "reminder_delete_pick", "reminder_edit_menu",
        "reminder_edit_type", "reminder_edit_message_value",
    }:
        return False

    normalized = text.strip().lower().replace("1️⃣", "1").replace("2️⃣", "2").replace("0️⃣", "0")
    save_values = {"simpan", "save", "ya", "yes", "1", "hapus"}
    edit_values = {"edit", "ubah", "2"}
    cancel_values = {"batal", "cancel", "tidak", "no", "0"}

    if normalized in cancel_values:
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, "❌ Tindakan dibatalkan.\n\n0️⃣ Menu Utama")
        return True

    payload = json_object(pending["payload"])

    if normalized in edit_values:
        if action_type == "transaction":
            await start_guided_transaction(jid, "income" if payload.get("tx_type") == "income" else "expense")
            return True
        if action_type in {"reminder", "reminder_schedule_update"}:
            if action_type == "reminder_schedule_update":
                await db.set_pending_action(jid, "reminder_edit_type", {"reminder_id": payload["reminder_id"]}, ttl_minutes=20)
                await gowa_send_text(jid, "🗓 Pilih jadwal baru:\n1️⃣ Sekali\n2️⃣ Harian\n3️⃣ Mingguan\n4️⃣ Bulanan\n0️⃣ Batal")
            else:
                await send_reminder_menu(jid)
            return True
        if action_type == "rename_business":
            await db.set_pending_action(jid, "menu_business_rename", {}, ttl_minutes=20)
            await gowa_send_text(jid, "✏️ Ketik kembali nama usaha.\n\n0️⃣ Batal")
            return True
        if action_type == "pos_sale":
            await db.set_pending_action(jid, "guided_pos_sale_items", {}, ttl_minutes=20)
            await gowa_send_text(jid, "🧾 Ketik kembali produk. Contoh: `Susu 1 cup, Roti Bakar 2`\n\n0️⃣ Batal")
            return True
        if action_type == "pos_product_create":
            await db.set_pending_action(jid, "guided_pos_product_quick_name", {}, ttl_minutes=20)
            await gowa_send_text(jid, "📦 Ketik kembali nama produk. SKU akan dibuat otomatis.\n\n0️⃣ Batal")
            return True
        if action_type == "pos_product_update":
            await begin_product_edit(jid, payload.get("sku"))
            return True
        if action_type == "pos_stock_set":
            await db.set_pending_action(jid, "guided_pos_stock_sku", {}, ttl_minutes=20)
            await gowa_send_text(jid, "📦 Ketik kembali SKU produk.\n\n0️⃣ Batal")
            return True
        if action_type == "pos_staff_invite":
            await db.set_pending_action(jid, "guided_staff_name", {}, ttl_minutes=20)
            await gowa_send_text(jid, "👤 Ketik kembali nama karyawan.\n\n0️⃣ Batal")
            return True
        if action_type in {"pos_staff_role", "pos_staff_remove"}:
            await send_staff_menu(jid)
            return True

    if normalized not in save_values:
        await gowa_send_text(jid, "⚠️ Pilihan belum dikenali.\n\n1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL")
        return True

    if action_type == "pos_sale":
        try:
            _, sale, repeated = await pos.create_sale(
                jid,
                payload.get("items") or [],
                payload.get("payment_method") or "",
                "whatsapp",
                str(uuid.uuid4()),
            )
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Penjualan gagal: {exc}\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        preview = payload.get("preview") or {}
        lines = ["✅ *PENJUALAN TERSIMPAN*", f"Invoice: `{sale['invoice_no']}`", ""]
        for item in preview.get("items") or []:
            lines.append(f"• {item['name']} × {_qty(item['qty'])} {item['unit']} = {rupiah(item['line_total'])}")
        lines.extend([
            "", f"*TOTAL: {rupiah(sale['total'])}*",
            f"Pembayaran: *{sale['payment_method']}*",
            "Stok dan pemasukan sudah diperbarui.", "", "Ketik *POS* untuk transaksi berikutnya.", "0️⃣ Menu Utama",
        ])
        await gowa_send_text(jid, "\n".join(lines))
        return True

    if action_type == "pos_product_create":
        try:
            _, product = await pos.add_product(
                jid, payload["sku"], payload["name"],
                int(payload["sell_price"]), int(payload["cost_price"]),
                Decimal(payload["opening_stock"]), payload["unit"],
                Decimal(payload["min_stock"]),
            )
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Produk gagal disimpan: {exc}\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *PRODUK TERSIMPAN*\n\n`{product['sku']}` — *{product['name']}*\nHarga: {rupiah(product['sell_price'])}\nStok: {_qty(product['stock_on_hand'])} {product['unit']}\n\nKetik *POS* untuk kembali.\n0️⃣ Menu Utama")
        return True

    if action_type == "pos_product_update":
        try:
            _, product = await pos.update_product(
                jid,
                payload["sku"],
                name=payload["name"],
                sell_price=int(payload["sell_price"]),
                cost_price=int(payload["cost_price"]),
                unit=payload["unit"],
                min_stock=Decimal(payload["min_stock"]),
            )
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Produk gagal diperbarui: {exc}\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        await gowa_send_text(
            jid,
            "✅ *PRODUK DIPERBARUI*\n\n"
            f"`{product['sku']}` — *{product['name']}*\n"
            f"Harga: {rupiah(product['sell_price'])}\n"
            f"Satuan: {product['unit']}\n"
            f"Stok minimum: {_qty(product['min_stock'])}\n\n"
            "Ketik *POS* untuk kembali.\n0️⃣ Menu Utama",
        )
        return True

    if action_type == "pos_stock_set":
        try:
            _, product, delta = await pos.set_stock(
                jid, payload["sku"], Decimal(payload["new_stock"]), payload["note"]
            )
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Stok gagal diperbarui: {exc}\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *STOK DIPERBARUI*\n\n`{product['sku']}` — {product['name']}\nStok baru: *{_qty(product['stock_on_hand'])}*\nPerubahan: {_qty(delta)}\n\nKetik *POS* untuk kembali.\n0️⃣ Menu Utama")
        return True

    if action_type == "pos_staff_invite":
        try:
            ctx, invite, code = await pos.create_staff_invite(jid, payload["name"], payload["role"])
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Undangan gagal dibuat: {exc}\n\n0️⃣ Menu Utama")
            return True
        expires = invite["expires_at"].astimezone(JAKARTA).strftime("%H:%M WIB")
        await db.clear_pending_action(jid)
        await gowa_send_text(
            jid,
            f"✅ *UNDANGAN KARYAWAN DIBUAT*\n\n🏪 {ctx['business_name']}\n"
            f"👤 {payload['name']}\n🔐 Peran: *{role_label(payload['role'])}*\n\n"
            "Minta karyawan mengirim pesan berikut ke bot WhatsApp ini:\n\n"
            f"`GABUNG {code}`\n\n"
            f"Kode satu kali pakai dan berlaku sampai *{expires}*.\n"
            "MASTER tidak perlu menyetujui karyawan ini.\n\n0️⃣ Menu Utama",
        )
        return True

    if action_type == "pos_staff_role":
        try:
            await pos.set_staff_role_by_id(jid, payload["staff"]["id"], payload["role"])
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Role gagal diubah: {exc}\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ Peran *{payload['staff']['name']}* diubah menjadi *{role_label(payload['role'])}*.\n\n0️⃣ Menu Utama")
        return True

    if action_type == "pos_staff_remove":
        try:
            await pos.remove_staff_by_id(jid, payload["staff"]["id"])
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await db.clear_pending_action(jid)
            await gowa_send_text(jid, f"⚠️ Karyawan gagal dinonaktifkan: {exc}\n\n0️⃣ Menu Utama")
            return True
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *{payload['staff']['name']}* telah dinonaktifkan dari usaha ini.\n\n0️⃣ Menu Utama")
        return True

    if action_type == "transaction":
        row = await db.create_transaction(
            jid, payload["tx_type"], int(payload["amount"]),
            payload.get("description") or "Transaksi", payload.get("category") or "Umum"
        )
        await db.clear_pending_action(jid)
        label = "Pemasukan" if row["type"] == "income" else "Pengeluaran"
        await gowa_send_text(jid, f"✅ *TRANSAKSI TERSIMPAN*\n\n{label}: *{rupiah(int(row['amount']))}*\nKategori: {row['category']}\nKeterangan: {row['description']}\n\nKetik *MENU* untuk pilihan berikutnya.")
        return True

    if action_type == "reminder":
        due_local = datetime.fromisoformat(payload["reminder_at"])
        if due_local.tzinfo is None:
            due_local = due_local.replace(tzinfo=JAKARTA)
        due_local = due_local.astimezone(JAKARTA)
        recurrence_time = dt_time.fromisoformat(payload.get("recurrence_time") or due_local.strftime("%H:%M:%S"))
        row = await db.create_reminder(
            jid,
            payload["message"],
            due_local.astimezone(UTC),
            payload.get("recurrence_type", "once"),
            int(payload.get("recurrence_interval", 1)),
            payload.get("recurrence_day"),
            payload.get("recurrence_weekday"),
            recurrence_time,
        )
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *PENGINGAT DIBUAT*\n\n📝 {row['message']}\n🗓 {recurrence_label(row)}\n🔔 Berikutnya: {row['due_at'].astimezone(JAKARTA).strftime('%d-%m-%Y %H:%M')} WIB\n\nKetik *PENGINGAT* untuk mengelola jadwal.\n0️⃣ Menu")
        return True

    if action_type == "reminder_schedule_update":
        due_local = datetime.fromisoformat(payload["reminder_at"]).astimezone(JAKARTA)
        recurrence_time = dt_time.fromisoformat(payload.get("recurrence_time") or due_local.strftime("%H:%M:%S"))
        row = await db.update_reminder_schedule(
            jid, payload["reminder_id"], due_local.astimezone(UTC),
            payload.get("recurrence_type", "once"),
            int(payload.get("recurrence_interval", 1)),
            payload.get("recurrence_day"), payload.get("recurrence_weekday"),
            recurrence_time,
        )
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *JADWAL DIPERBARUI*\n\n📝 {row['message']}\n🗓 {recurrence_label(row)}\n\nKetik *PENGINGAT* untuk mengelola jadwal.\n0️⃣ Menu")
        return True

    if action_type == "reminder_message_update":
        row = await db.update_reminder_message(jid, payload["reminder_id"], payload["message"])
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *ISI PENGINGAT DIPERBARUI*\n\n📝 {row['message']}\n\nKetik *PENGINGAT* untuk mengelola jadwal.\n0️⃣ Menu")
        return True

    if action_type == "reminder_cancel":
        row = await db.cancel_reminder(jid, payload["reminder_id"])
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"🗑 *PENGINGAT DIHAPUS*\n\n{row['message']}\n\nKetik *PENGINGAT* untuk mengelola jadwal.\n0️⃣ Menu")
        return True

    if action_type == "rename_business":
        new_name = await db.rename_business(jid, payload["business_name"])
        await db.clear_pending_action(jid)
        await gowa_send_text(jid, f"✅ *NAMA USAHA DIPERBARUI*\n\n🏪 {new_name}\n\nKetik *PENGATURAN* untuk kembali.\n0️⃣ Menu")
        return True

    await db.clear_pending_action(jid)
    await gowa_send_text(jid, "⚠️ Tindakan lama tidak dikenali dan dibatalkan.\n\n0️⃣ Menu Utama")
    return True

async def send_summary(jid: str):
    ctx, totals = await db.monthly_summary(jid)
    income = totals["income"]
    expense = totals["expense"]
    balance = income - expense
    await gowa_send_text(
        jid,
        "📊 *RINGKASAN BULAN INI*\n"
        f"🏪 {ctx['business_name']}\n\n"
        f"💰 Pemasukan: *{rupiah(income)}*\n"
        f"💸 Pengeluaran: *{rupiah(expense)}*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💵 Selisih: *{rupiah(balance)}*\n\n"
        "Ketik *EXPORT* untuk membuat laporan.\n"
        "Ketik *ASISTEN AI* untuk bertanya.\n"
        "0️⃣ Menu Utama",
    )


def current_month_range():
    today = datetime.now(JAKARTA).date()
    start = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    end = next_month - timedelta(days=1)
    return start, end


async def handle_export(
    jid: str,
    text: str,
    custom_start: date | None = None,
    custom_end: date | None = None,
):
    lower = text.lower()
    fmt = "pdf" if "pdf" in lower else "xlsx"
    all_data = "semua" in lower or "all" in lower

    if custom_start and custom_end:
        start_date, end_date = custom_start, custom_end
        period = f"{start_date.strftime('%d-%m-%Y')} s.d. {end_date.strftime('%d-%m-%Y')}"
    elif all_data:
        start_date = None
        end_date = None
        period = "Semua Data"
    else:
        start_date, end_date = current_month_range()
        period = datetime.now(JAKARTA).strftime("%B %Y")

    ctx, rows = await db.finance_export_data(jid, start_date, end_date)
    generated_at = datetime.now(JAKARTA)
    if fmt == "pdf":
        path = exporter.build_pdf_report(ctx["business_name"], period, generated_at, rows)
        label = "PDF"
    else:
        path = exporter.build_excel_report(ctx["business_name"], period, generated_at, rows)
        label = "Excel"
    try:
        await gowa_send_file(
            jid, path,
            f"Laporan Keuangan - {ctx['business_name']}\nPeriode: {period}\nFormat: {label}\nJumlah transaksi: {len(rows)}",
        )
        await db.log_finance_export(jid, label.lower(), period, len(rows))
    finally:
        exporter.remove_export(path)

async def handle_ai(jid: str, text: str):
    quick_sale = parse_quick_sale_request(text)
    if quick_sale:
        access = await db.get_whatsapp_access(jid)
        if not access or not has_permission(access["role"], "pos.sell"):
            await gowa_send_text(jid, "⛔ Peran Anda tidak boleh mencatat penjualan POS.\n\n0️⃣ Menu Utama")
            return
        items, payment = quick_sale
        await begin_sale_preview(jid, items, payment)
        return

    if not ai.is_configured():
        await gowa_send_text(
            jid,
            "🤖 Asisten AI belum dikonfigurasi oleh administrator.\n"
            "Fitur dasar tetap dapat digunakan.\n\n"
            "Fitur dasar tetap tersedia melalui *MENU*.",
        )
        return

    provider = ai.PROVIDER
    model = ai.MODEL

    await gowa_chat_presence(jid, "start")
    try:
        access = await db.get_whatsapp_access(jid)
        finance_context = await db.ai_business_context(jid)
        result = await ai.analyze_message(
            text,
            finance_context,
            datetime.now(JAKARTA).isoformat(),
        )
        await db.log_ai_usage(
            jid,
            provider,
            result.model,
            result.intent,
            result.prompt_tokens,
            result.completion_tokens,
            result.total_tokens,
            True,
            None,
        )
        await gowa_chat_presence(jid, "stop")
    except Exception as exc:
        await gowa_chat_presence(jid, "stop")
        log.exception("AI gagal")
        try:
            await db.log_ai_usage(
                jid,
                provider,
                model,
                "service_error",
                success=False,
                error_code=str(exc)[:120],
            )
        except Exception:
            pass
        await gowa_send_text(
            jid,
            "⚠️ Layanan AI sedang tidak dapat digunakan. Coba lagi beberapa saat.\n\n"
            "Ketik *MENU* untuk memakai fitur dasar.",
        )
        return

    # POS intents keep a draft so follow-up answers do not lose context.
    if result.intent == "create_sale":
        if not access or not has_permission(access["role"], "pos.sell"):
            await gowa_send_text(jid, "⛔ Role Anda tidak boleh mencatat penjualan POS.\n\n0️⃣ Menu Utama")
            return
        items = result.sale_items or []
        if not items:
            await db.set_pending_action(
                jid,
                "sale_items_needed",
                {"payment_method": result.payment_method},
                ttl_minutes=20,
            )
            await gowa_send_text(
                jid,
                "🧾 Produk apa dan berapa jumlahnya?\n\n"
                "Contoh: `Susu 1 cup` atau `2 Kopi Susu`\n\n0️⃣ Batal",
            )
            return
        await begin_sale_preview(jid, items, result.payment_method)
        return

    if result.intent == "stock_query":
        if not result.product_query:
            await db.set_pending_action(jid, "stock_query_needed", {}, ttl_minutes=20)
            await gowa_send_text(jid, "📦 Produk apa yang ingin dicek?\nKetik nama atau SKU.\n\n0️⃣ Batal")
            return
        await answer_stock_query(jid, result.product_query)
        return

    if result.intent == "pos_summary":
        try:
            today = date.today()
            start = today.replace(day=1) if result.report_period == "month" else today
            ctx, row, low = await pos.pos_summary(jid, start, today)
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await gowa_send_text(jid, f"⛔ {exc}\n\n0️⃣ Menu Utama")
            return
        period = "BULAN INI" if result.report_period == "month" else "HARI INI"
        await gowa_send_text(jid, f"📊 *POS {period}*\n🏪 {ctx['business_name']}\nTransaksi: *{row['sales_count']}*\nOmzet: *{rupiah(row['revenue'])}*\nLaba kotor: *{rupiah(row['gross_profit'])}*\nStok rendah: *{low}*\n\n0️⃣ Menu Utama")
        return

    if result.needs_clarification:
        await gowa_send_text(
            jid,
            (result.clarification or "Mohon lengkapi informasi yang diperlukan.")
            + "\n\n0️⃣ Menu Utama",
        )
        return

    if result.intent in {"create_income", "create_expense"}:
        if not access or not has_permission(access["role"], "finance.write"):
            await gowa_send_text(jid, "⛔ Role Anda tidak boleh menulis transaksi keuangan manual.\n\n0️⃣ Menu Utama")
            return
        tx_type = "income" if result.intent == "create_income" else "expense"
        description = result.description or (
            "Pemasukan" if tx_type == "income" else "Pengeluaran"
        )
        category = result.category or "Umum"

        await db.set_pending_action(
            jid,
            "transaction",
            {
                "tx_type": tx_type,
                "amount": result.amount,
                "description": description,
                "category": category,
            },
        )
        label = "PEMASUKAN" if tx_type == "income" else "PENGELUARAN"
        await gowa_send_text(
            jid,
            "🤖 KONFIRMASI TRANSAKSI\n\n"
            f"🧾 {label}\n"
            f"Nominal: {rupiah(result.amount)}\n"
            f"Kategori: {category}\n"
            f"Keterangan: {description}\n\n"
            "1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL",
        )
        return

    if result.intent == "create_reminder":
        try:
            due_local = datetime.fromisoformat(result.reminder_at)
            if due_local.tzinfo is None:
                due_local = due_local.replace(tzinfo=JAKARTA)
            due_local = due_local.astimezone(JAKARTA)
        except Exception:
            await gowa_send_text(
                jid,
                "Saya memahami Anda ingin membuat pengingat, "
                "tetapi tanggal/jam belum valid. Tuliskan waktunya lebih jelas.",
            )
            return

        if due_local <= datetime.now(JAKARTA):
            await gowa_send_text(
                jid,
                "Waktu pengingat tersebut sudah lewat. Gunakan waktu yang akan datang.",
            )
            return

        reminder_message = result.reminder_message or result.description or text[:500]
        await db.set_pending_action(
            jid,
            "reminder",
            {
                "message": reminder_message,
                "reminder_at": due_local.isoformat(),
            },
        )
        await gowa_send_text(
            jid,
            "🤖 KONFIRMASI PENGINGAT\n\n"
            f"📝 {reminder_message}\n"
            f"🗓 {due_local.strftime('%d-%m-%Y %H:%M')} WIB\n\n"
            "1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL",
        )
        return

    if result.intent == "finance_summary":
        if not access or not has_permission(access["role"], "finance.read"):
            await gowa_send_text(jid, "⛔ Role Anda tidak boleh melihat ringkasan keuangan.\n\n0️⃣ Menu Utama")
            return
        await send_summary(jid)
        return

    if result.intent == "finance_question" and (not access or not has_permission(access["role"], "finance.read")):
        await gowa_send_text(jid, "⛔ Role Anda tidak boleh mengakses data keuangan.\n\n0️⃣ Menu Utama")
        return

    if result.intent in {"finance_question", "general_question"}:
        await gowa_send_text(
            jid,
            (result.answer or "Saya belum memiliki jawaban yang cukup.")
            + "\n\n━━━━━━━━━━━━━━━━━━\n"
            "Ketik *ASISTEN AI* untuk bertanya lagi.\n"
            "Ketik *MENU* untuk kembali.",
        )
        return

    message = (
        result.clarification
        or result.answer
        or (
            "Saya belum memahami permintaan itu.\n"
            "Contoh: “catat pemasukan 50rb”, “keluar 25rb parkir”, "
            "atau “berapa pengeluaran bulan ini?”"
        )
    )
    await gowa_send_text(
        jid,
        message
        + "\n\n━━━━━━━━━━━━━━━━━━\n"
        "Ketik *BANTUAN* untuk melihat contoh.\n"
        "Ketik *MENU* untuk kembali.",
    )


async def process_message(jid: str, payload: dict):
    display_name = payload.get("sender_display_name") or payload.get("from_name") or jid.split("@", 1)[0]
    access = await db.ensure_whatsapp_user(jid, payload.get("from_lid"), display_name)

    text = str(payload.get("body") or "").strip()
    join_match = re.fullmatch(r"(?i)\s*GABUNG\s+([0-9\s-]+)\s*", text)
    if join_match:
        try:
            joined = await pos.claim_staff_invite(jid, join_match.group(1))
        except (PermissionDenied, PermissionError, ValueError, LookupError) as exc:
            await gowa_send_text(jid, f"⚠️ Tidak dapat bergabung: {exc}\n\nPeriksa kode dari pemilik usaha.")
            return
        await db.clear_pending_action(jid)
        await gowa_send_text(
            jid,
            "✅ *BERHASIL BERGABUNG*\n\n"
            f"👤 {joined['full_name']}\n🏪 {joined['business_name']}\n"
            f"🔐 Peran: *{role_label(joined['role'])}*\n\n"
            "Ketik *MENU* untuk mulai menggunakan fitur usaha.",
        )
        return

    if access["access_status"] != "active" or access["deleted_at"] is not None:
        await pending_response(jid, access)
        return
    if not access["active_tenant_id"]:
        await gowa_send_text(jid, "⚠️ Akun sudah aktif tetapi belum memiliki usaha. Hubungi administrator.")
        return

    if not text:
        await gowa_send_text(jid, "Kirim *MENU* untuk melihat fitur yang tersedia.")
        return

    # Navigation words override any current state; digits remain state-aware.
    nav = navigation_alias(text)
    if nav is not None:
        await db.clear_pending_action(jid)
        if nav == "0":
            await send_main_menu(jid)
        else:
            await handle_main_choice(jid, nav)
        return

    # Perintah aplikasi yang jelas tetap menang ketika user sedang berada di mode AI.
    current_state = await db.get_pending_action(jid)
    if current_state and current_state["action_type"] == "menu_ai":
        lower_pre = text.lower().strip()
        if re.search(r"pengingat.{0,20}(aktif|saya)|daftar\s+pengingat", lower_pre):
            await db.clear_pending_action(jid)
            await send_active_reminders(jid, "view")
            return
        if re.match(r"(?i)^\s*(buatkan\s+)?pengingat\b|^\s*ingatkan\b", text):
            await db.clear_pending_action(jid)
            if await try_quick_reminder(jid, text):
                return
        if re.match(r"(?i)^export\b", text):
            await db.clear_pending_action(jid)
            if any(x in lower_pre for x in {"excel", "xlsx", "pdf"}):
                await gowa_send_text(jid, "⏳ Menyiapkan laporan keuangan...")
                await handle_export(jid, text)
            else:
                await send_export_menu(jid)
            return

    if await handle_menu_state(jid, text):
        return
    if await handle_confirmation(jid, text):
        return
    if await handle_main_choice(jid, text):
        return

    lower = text.lower().strip()

    is_product_edit, product_query = parse_product_edit_request(text)
    if is_product_edit:
        await db.clear_pending_action(jid)
        await begin_product_edit(jid, product_query)
        return

    if lower == "pos" or lower.startswith("pos "):
        await db.clear_pending_action(jid)
        await handle_pos_command(jid, text)
        return

    if re.search(r"pengingat.{0,20}(aktif|saya)|daftar\s+pengingat", lower):
        await send_active_reminders(jid, "view")
        return

    if await try_quick_reminder(jid, text):
        return

    if lower in {"menu", "/menu", "start", "/start", "help", "bantuan", "halo", "hai", "hi"}:
        await send_main_menu(jid)
        return

    if re.match(r"(?i)^ubah\s+usaha\s+(.+)$", text):
        new_name = re.match(r"(?i)^ubah\s+usaha\s+(.+)$", text).group(1).strip()
        if len(new_name) < 2 or len(new_name) > 100:
            await gowa_send_text(jid, "Nama usaha harus 2–100 karakter.")
            return
        await db.set_pending_action(jid, "rename_business", {"business_name": new_name}, ttl_minutes=20)
        await gowa_send_text(jid, f"✏️ Nama baru: *{new_name}*\n\n1️⃣ SIMPAN\n2️⃣ EDIT\n0️⃣ BATAL")
        return

    if re.match(r"(?i)^export(\s+(excel|xlsx|pdf))?(\s+semua)?$", text):
        if not any(x in lower for x in {"excel", "xlsx", "pdf"}):
            await send_export_menu(jid)
            return
        await gowa_send_text(jid, "⏳ Menyiapkan laporan keuangan...")
        await handle_export(jid, text)
        return

    await handle_ai(jid, text)

async def event_worker():
    while True:
        try:
            event = await db.claim_next_event()
            if not event:
                await asyncio.sleep(1)
                continue
            try:
                payload = json_object(event["payload"])
                await process_message(event["whatsapp_jid"], payload)
                await db.mark_event_done(event["message_id"])
            except Exception as exc:
                log.exception("Gagal memproses WA event %s", event["message_id"])
                await db.mark_event_failed(
                    event["message_id"],
                    event["attempts"],
                    str(exc),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WA event worker error")
            await asyncio.sleep(3)


async def reminder_worker():
    while True:
        try:
            rows = await db.claim_due_reminders(20)
            for row in rows:
                try:
                    local_due = row["due_at"].astimezone(JAKARTA)
                    await gowa_send_text(
                        row["whatsapp_jid"],
                        "🔔 PENGINGAT\n\n"
                        f"{row['message']}\n\n"
                        f"🗓 {local_due.strftime('%d-%m-%Y %H:%M')} WIB",
                    )
                    await db.mark_reminder_sent(row["reminder_id"])
                except Exception:
                    log.exception("Gagal mengirim reminder WA %s", row["reminder_id"])
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WA reminder worker error")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup():
    global _worker_task, _reminder_task
    await db.init_pool()
    _worker_task = asyncio.create_task(event_worker())
    _reminder_task = asyncio.create_task(reminder_worker())
    log.info("UMKM WhatsApp bridge started")


@app.on_event("shutdown")
async def shutdown():
    for task in (_worker_task, _reminder_task):
        if task:
            task.cancel()
    await db.close_pool()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "umkm-gowa-bridge",
        "ai": ai.public_status(),
    }


@app.post("/webhooks/gowa")
async def gowa_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw, signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    if data.get("event") != "message":
        return {"ok": True, "ignored": "event"}

    payload = data.get("payload") or {}

    if payload.get("is_from_me") is True:
        return {"ok": True, "ignored": "from_me"}

    chat_id = normalize_jid(payload.get("chat_id"))
    sender = normalize_jid(payload.get("from"))

    if chat_id.endswith("@g.us"):
        return {"ok": True, "ignored": "group"}

    jid = sender if sender.endswith("@s.whatsapp.net") else chat_id
    if not jid or jid.endswith("@g.us"):
        return {"ok": True, "ignored": "unsupported_sender"}

    message_id = str(payload.get("id") or "").strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="missing message id")

    inserted = await db.enqueue_event(message_id, jid, payload)
    return {"ok": True, "queued": bool(inserted)}
