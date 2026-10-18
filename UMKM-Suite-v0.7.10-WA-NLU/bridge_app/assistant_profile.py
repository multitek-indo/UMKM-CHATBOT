"""Role-scoped feature registry and factual business brief for both channels.

No SQL, mutations or AI-generated numbers in this module.
"""
from decimal import Decimal, InvalidOperation
from .permissions import has_permission

# feature_key, public label, required permission, channels (must exist in checked source)
FEATURES = (
    ("export_finance", "Ekspor keuangan ke Excel/PDF", "finance.export", ("whatsapp", "telegram")),
    ("reminder_menu", "Buat/kelola pengingat", None, ("whatsapp", "telegram")),
    ("products_list", "Lihat daftar produk dan harga", "pos.read", ("whatsapp", "telegram")),
    ("low_stock", "Periksa daftar stok rendah", "pos.read", ("whatsapp", "telegram")),
    ("product_add", "Tambah produk lewat panduan", "product.write", ("whatsapp", "telegram")),
    ("product_edit", "Ubah detail produk lewat panduan", "product.write", ("whatsapp", "telegram")),
    ("stock_adjust", "Atur stok fisik lewat panduan", "stock.write", ("whatsapp", "telegram")),
    ("staff_list", "Lihat data karyawan", "staff.read", ("whatsapp", "telegram")),
    ("staff_manage", "Kelola karyawan lewat panduan", "staff.write", ("whatsapp", "telegram")),
    ("business_settings", "Pengaturan nama usaha", "business.write", ("whatsapp", "telegram")),
    ("pos_menu", "Buka POS dan stok", "pos.read", ("whatsapp", "telegram")),
    ("opname", "Stock opname lewat perintah Telegram", "opname.write", ("telegram",)),
)
FEATURE_KEYS = frozenset(x[0] for x in FEATURES)


def permitted(role: str | None, key: str | None, channel: str) -> bool:
    for feature, _label, permission, channels in FEATURES:
        if feature == key:
            return channel in channels and (permission is None or has_permission(role, permission))
    return False


def catalog(role: str | None, channel: str) -> list[dict[str, str]]:
    return [{"feature_key": key, "label": label} for key, label, _permission, _channels in FEATURES
            if permitted(role, key, channel)]


def capabilities_text(context: dict, channel: str) -> str:
    role = context.get("role")
    intro = "Bisa, Bos. Ini yang dapat saya bantu lewat akun Anda:" if role == "OWNER" else "Berikut yang tersedia untuk peran Anda:"
    direct = []
    if has_permission(role, "finance.read"):
        direct.append("Ringkasan serta pertanyaan data keuangan bulan ini")
    if has_permission(role, "finance.write"):
        direct.append("Draft pemasukan/pengeluaran, disimpan setelah konfirmasi")
    if has_permission(role, "pos.sell"):
        direct.append("Draft penjualan POS dengan pengecekan produk dan stok")
    if has_permission(role, "pos.read"):
        direct.append("Cek stok, harga, dan laporan POS")
    direct.append("Pengingat dan diskusi operasional usaha")
    guided = catalog(role, channel)
    direct_lines = "\n".join(f"• {item}" for item in direct)
    guided_lines = "\n".join(f"• {item['label']}" for item in guided)
    strong = "*" if channel == "whatsapp" else ""
    return intro + f"\n\n{strong}Bisa ditangani langsung:{strong}\n" + direct_lines + (f"\n\n{strong}Lewat alur aplikasi:{strong}\n" + guided_lines if guided_lines else "") + "\n\nCeritakan kebutuhan Anda dengan bahasa biasa; saya bantu ke langkah yang sesuai."


def _money(number) -> str:
    return "Rp" + f"{int(number):,}".replace(",", ".")


def _num(value) -> Decimal | None:
    try:
        num = Decimal(str(value))
        return num if num.is_finite() else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def business_brief(context: dict) -> str:
    """Deterministic summary of tenant/role-filtered data. Periods are explicit.

    Uses existing ai_business_context; catalog may be limited to first 150 products.
    """
    role = context.get("role")
    name = str(context.get("business_name") or "Usaha Anda")[:100]
    lines = [f"Saya rangkum data yang sudah tercatat untuk {name}, Bos:" if role == "OWNER"
             else f"Ringkasan data yang dapat Anda akses untuk {name}:"]
    any_data = False
    pos_today = context.get("pos_today")
    if isinstance(pos_today, dict) and pos_today.get("revenue") is not None:
        lines.append(f"• POS hari ini: {int(pos_today.get('sales_count') or 0)} penjualan, omzet {_money(pos_today['revenue'])}.")
        any_data = True
    pos_month = context.get("pos_month")
    if isinstance(pos_month, dict) and pos_month.get("revenue") is not None:
        lines.append(f"• POS bulan ini: {int(pos_month.get('sales_count') or 0)} penjualan, omzet {_money(pos_month['revenue'])}.")
        any_data = True
    if context.get("month_income") is not None and context.get("month_expense") is not None:
        income, expense = int(context["month_income"]), int(context["month_expense"])
        lines.append(f"• Pembukuan bulan ini: masuk {_money(income)}, keluar {_money(expense)}, selisih {_money(income - expense)}.")
        any_data = True
    products = context.get("product_catalog") or []
    if isinstance(products, list) and products:
        low = [row for row in products if isinstance(row, dict)
               and _num(row.get("stock_on_hand")) is not None
               and _num(row.get("min_stock")) is not None
               and _num(row["stock_on_hand"]) <= _num(row["min_stock"])]
        if low:
            examples = ", ".join(str(item.get("name") or item.get("sku") or "-")[:45] for item in low[:4])
            lines.append(f"• Pada {len(products)} produk yang diperiksa, setidaknya {len(low)} stok sudah di batas minimum: {examples}.")
        else:
            lines.append(f"• Dari {len(products)} produk yang diperiksa, belum terlihat stok di batas minimum.")
        any_data = True
    if not any_data:
        return "Data operasional belum tersedia untuk peran Anda. Saya bisa membantu pengingat dan menjelaskan fitur yang dapat Anda gunakan."
    lines.append("Catatan: selisih pembukuan bukan saldo kas atau laba bersih. Omzet POS tidak saya jumlahkan lagi ke pemasukan agar tidak menghitung ganda.")
    if products and any(_num(p.get("stock_on_hand")) is not None and _num(p.get("min_stock")) is not None
                        and _num(p["stock_on_hand"]) <= _num(p["min_stock"]) for p in products if isinstance(p,dict)):
        lines.append("Saran saya: periksa daftar stok rendah terlebih dahulu agar penjualan tidak terhambat.")
    elif context.get("month_balance") is not None and int(context["month_balance"]) < 0:
        lines.append("Saran saya: cek rincian pengeluaran bulan ini dan tentukan yang bisa dihemat.")
    else:
        lines.append("Mau saya bantu cek stok, penjualan, atau transaksi yang perlu ditindaklanjuti?")
    return "\n".join(lines)
