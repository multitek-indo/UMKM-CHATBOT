"""Strict parsers shared by POS chat handlers."""
from decimal import Decimal, InvalidOperation
import re


SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,39}$")


def normalize_sku(value: str) -> str:
    sku = (value or "").strip().upper()
    if not SKU_RE.fullmatch(sku):
        raise ValueError("SKU harus 1-40 karakter: huruf, angka, titik, garis atau underscore.")
    return sku


def parse_positive_decimal(value: str, *, max_value: str = "999999999.999") -> Decimal:
    raw = (value or "").strip().replace(" ", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Angka tidak valid.") from exc
    if number <= 0 or number > Decimal(max_value):
        raise ValueError("Angka harus lebih dari 0 dan masih dalam batas sistem.")
    return number.quantize(Decimal("0.001"))


def parse_nonnegative_decimal(value: str, *, max_value: str = "999999999.999") -> Decimal:
    raw = (value or "").strip().replace(" ", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Angka tidak valid.") from exc
    if number < 0 or number > Decimal(max_value):
        raise ValueError("Angka harus 0 atau lebih dan masih dalam batas sistem.")
    return number.quantize(Decimal("0.001"))


def parse_nonnegative_money(value: str) -> int:
    raw = (value or "").lower().strip().replace("rp", "").replace(" ", "")
    raw = raw.replace(",", ".")
    multiplier = 1
    for suffix, mult in (("ribu", 1_000), ("rb", 1_000), ("juta", 1_000_000), ("jt", 1_000_000)):
        if raw.endswith(suffix):
            multiplier, raw = mult, raw[:-len(suffix)]
            break
    try:
        result = int(Decimal(raw) * multiplier)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Nominal tidak valid.") from exc
    if result < 0 or result > 999_999_999_999:
        raise ValueError("Nominal di luar batas sistem.")
    return result


def parse_sale_items(value: str) -> list[dict]:
    """Parse `SKU:qty, SKU2:qty`; duplicate SKUs are combined."""
    combined: dict[str, Decimal] = {}
    for part in (value or "").split(","):
        if not part.strip():
            continue
        bits = part.split(":", 1)
        if len(bits) != 2:
            raise ValueError("Format item harus SKU:jumlah, dipisahkan koma.")
        sku = normalize_sku(bits[0])
        qty = parse_positive_decimal(bits[1], max_value="999999.999")
        combined[sku] = combined.get(sku, Decimal("0")) + qty
    if not combined:
        raise ValueError("Minimal satu item penjualan diperlukan.")
    if len(combined) > 50:
        raise ValueError("Maksimal 50 SKU per transaksi.")
    return [{"sku": sku, "qty": qty} for sku, qty in combined.items()]


def _parse_human_sale_part(part: str) -> dict:
    """Parse exactly one sale item without treating words in its name as separators."""
    unit = r"(?:pcs?|cup|gelas|botol|pack|bungkus|kg|gram|gr|liter|ltr|meter|unit)"
    if ":" in part:
        parsed = parse_sale_items(part)
        if len(parsed) != 1:
            raise ValueError("Satu bagian hanya boleh berisi satu produk.")
        return parsed[0]
    match = re.fullmatch(
        rf"(.+?)\s+(?:x\s*)?([0-9]+(?:[.,][0-9]+)?)\s*{unit}?",
        part,
        flags=re.I,
    )
    if match:
        query, raw_qty = match.group(1), match.group(2)
    else:
        match = re.fullmatch(
            rf"([0-9]+(?:[.,][0-9]+)?)\s*{unit}?\s+(.+)",
            part,
            flags=re.I,
        )
        if not match:
            raise ValueError("Ketik seperti Susu 1 cup atau 2 Kopi Susu.")
        raw_qty, query = match.group(1), match.group(2)
    qty = parse_positive_decimal(raw_qty, max_value="999999.999")
    return {"product_query": query.strip(), "qty": qty}


def _split_human_sale_part(part: str) -> list[str]:
    """Split Indonesian `dan` only when both sides are independently valid items."""
    for match in re.finditer(r"\s+dan\s+", part, flags=re.I):
        left = part[:match.start()].strip()
        right = part[match.end():].strip()
        if not left or not right:
            continue
        try:
            _parse_human_sale_part(left)
            _parse_human_sale_part(right)
        except ValueError:
            continue
        return _split_human_sale_part(left) + _split_human_sale_part(right)
    return [part]


def parse_human_sale_items(value: str) -> list[dict]:
    """Parse SKU/name items separated by commas or the Indonesian word `dan`."""
    result = []
    for comma_part in (value or "").split(","):
        part = comma_part.strip()
        if not part:
            continue
        for item_part in _split_human_sale_part(part):
            result.append(_parse_human_sale_part(item_part))
    if not result:
        raise ValueError("Minimal satu produk diperlukan.")
    if len(result) > 50:
        raise ValueError("Maksimal 50 produk per transaksi.")
    return result


def parse_quick_sale_request(value: str):
    """Recognize common Indonesian sale sentences without external AI."""
    match = re.fullmatch(
        r"\s*(?:jual|penjualan(?:\s+pos)?|pos\s+(?:jual|penjualan))\s+(.+?)\s*",
        value or "",
        flags=re.I,
    )
    if not match:
        return None

    body = match.group(1).strip()
    payment = None
    payment_match = re.search(
        r"(?:\s+(?:pembayaran|bayar|via)\s+|\s+)"
        r"(cash|tunai|transfer|tf|qris|qr|card|kartu|debit|other|lainnya)\s*$",
        body,
        flags=re.I,
    )
    if payment_match:
        payment = {
            "cash": "CASH", "tunai": "CASH",
            "transfer": "TRANSFER", "tf": "TRANSFER",
            "qris": "QRIS", "qr": "QRIS",
            "card": "CARD", "kartu": "CARD", "debit": "CARD",
            "other": "OTHER", "lainnya": "OTHER",
        }[payment_match.group(1).casefold()]
        body = body[:payment_match.start()].strip()

    try:
        return parse_human_sale_items(body), payment
    except ValueError:
        return None


def parse_pipe_fields(value: str, expected: int | tuple[int, ...]) -> list[str]:
    fields = [field.strip() for field in (value or "").split("|")]
    allowed = {expected} if isinstance(expected, int) else set(expected)
    if len(fields) not in allowed or any(not field for field in fields):
        want = "/".join(str(x) for x in sorted(allowed))
        raise ValueError(f"Jumlah kolom harus {want}, dipisahkan tanda |.")
    return fields
