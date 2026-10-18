"""Small, deterministic Indonesian-language safety guard for WhatsApp dialogs.

This module never writes to a database or calls an AI provider. It separates
natural cancellation/edit/confirmation language from the execution of actions.
Only the existing authorized backend may commit a transaction.
"""
from __future__ import annotations

import re
import unicodedata


_DESTRUCTIVE_ACTIONS = frozenset({'pos_staff_remove', 'reminder_cancel'})
_DRAFT_ACTIONS = frozenset({'transaction', 'pos_sale'})


def normalize_chat(text: str) -> str:
    raw = str(text or '')
    for digit in '0123456789':
        raw = raw.replace(f'{digit}️⃣', digit)
    raw = unicodedata.normalize('NFKC', raw).casefold().strip()
    raw = re.sub(r'\s+', ' ', raw)
    # Cosmetic punctuation only; retain question mark to avoid executing
    # hypothetical questions such as "Kalau dibatalkan bagaimana?".
    return raw.strip(' \t\n.,!')


def _strip_soft_prefixes(text: str) -> str:
    result = text
    # Soft natural-language openers are not commands themselves.
    pattern = re.compile(
        r'^(?:eh|ehh|waduh|tolong|mohon|bos|mas|mbak|saya|aku|'
        r'oke|ok|ya|iya|udah|sudah|sip|deh|jadi|mau|ingin|pengen|minta)\b[\s,]*'
    )
    for _ in range(5):
        updated = pattern.sub('', result, count=1).strip()
        if updated == result:
            break
        result = updated
    return result


def _matches_cancel(text: str) -> bool:
    t = _strip_soft_prefixes(normalize_chat(text))
    if not t or '?' in t or t.startswith(('kalau ', 'jika ', 'misal ', 'apakah ', 'bagaimana ', 'gimana ')):
        return False
    if t.startswith(('jangan batal', 'jangan dibatal', 'tidak usah batal', 'gak usah batal',
                     'nggak usah batal', 'jgn batal')):
        return False
    if re.fullmatch(
        r'(?:batal|batalkan|batalin|dibatalkan|dibatalin|batlin|batln|btalkan|btlkan|btal|cancel|cancelkan)'
        r'(?: (?:aja|saja|dulu|ya|dong|ini|itu|yang tadi|transaksi|transaksinya|'
        r'draft|pengeluaran|pengeluarannya|pemasukan|pemasukannya))*',
        t,
    ):
        return True
    if re.fullmatch(
        r'(?:nggak|ngga|gak|ga|tidak|ndak|enggak|ora|mboten)'
        r'(?: jadi| dadi| dados)(?: (?:aja|saja|dulu|ya|deh|dong|yang tadi|transaksi))*',
        t,
    ):
        return True
    if re.fullmatch(
        r'(?:jangan|jgn|ga usah|gak usah|nggak usah|ngga usah|tidak usah)'
        r' (?:di?simpan|di?catat|diproses|lanjut|lanjutkan)(?: (?:aja|saja|dulu|ya|deh|dong|yang tadi|transaksi))*',
        t,
    ):
        return True
    if t in {'stop', 'stop dulu', 'urungkan', 'urungkan saja', 'ora dadi', 'mboten dados'}:
        return True
    return False


def is_cancellation(text: str) -> bool:
    """Conservative cancellation for guided forms; excludes naked numeric 0."""
    return _matches_cancel(text)


def classify_confirmation(text: str, action_type: str) -> str:
    """Return cancel, edit, confirm, or unknown; never execute anything."""
    t = normalize_chat(text)
    if not t or '?' in t:
        return 'unknown'
    if _matches_cancel(text) or t in {'tidak', 'no', 'nggak', 'gak', 'ga', 'ndak'}:
        return 'cancel'

    short = _strip_soft_prefixes(t)
    if short in {'jangan hapus', 'jgn hapus', 'tidak usah hapus', 'nggak usah hapus', 'gak usah hapus'}:
        return 'cancel' if action_type in _DESTRUCTIVE_ACTIONS else 'unknown'

    # "hapus" on a draft must NEVER approve an expense, sale, stock change, etc.
    # For a draft it safely cancels; only a pending remove/cancel action may
    # interpret "hapus" as approving that very same deletion operation.
    if short in {'hapus', 'hapus saja', 'hapus aja', 'hapus yang tadi', 'hapus draft', 'hapus transaksi'}:
        if action_type in _DRAFT_ACTIONS:
            return 'cancel'
        if action_type in _DESTRUCTIVE_ACTIONS:
            return 'confirm'
        return 'unknown'
    if short in {'hapus karyawan', 'hapus pengingat', 'lanjut hapus', 'konfirmasi hapus'}:
        return 'confirm' if action_type in _DESTRUCTIVE_ACTIONS else 'unknown'

    if t in {'2', 'edit', 'ubah', 'ganti', 'perbaiki', 'koreksi', 'edit dulu', 'ubah dulu',
             'tolong edit', 'tolong ubah', 'saya mau edit', 'saya mau ubah'}:
        return 'edit'
    if t in {'1', 'simpan', 'save', 'ya', 'iya', 'yes', 'setuju', 'saya setuju',
             'simpan aja', 'simpan saja', 'simpan ya', 'tolong simpan',
             'oke simpan', 'ok simpan', 'iya simpan', 'ya simpan',
             'lanjut simpan', 'konfirmasi simpan'}:
        return 'confirm'

    return 'unknown'


# Parse ONLY the number part after a clear correction marker. The existing
# application's parse_money_input validates the monetary amount.
_AMOUNT = re.compile(
    r'(?<![\w])(?:rp\.?\s*)?\d+(?:[.,]\d+)*(?:\s*(?:ribu|rb|juta|jt))?(?!\w)',
    flags=re.I,
)
_CORRECTION = re.compile(
    r'\b(?:ubah|ganti|edit|koreksi|revisi|salah|seharusnya|harusnya|'
    r'maksudku|maksud saya|nominal(?:nya)?|jumlah(?:nya)?|nilai(?:nya)?|jadi)\b',
    flags=re.I,
)


def extract_amount_correction(text: str) -> str | None:
    """Return one explicit replacement amount, otherwise None (no change)."""
    t = normalize_chat(text)
    if not t or '?' in t or _matches_cancel(t) or not _CORRECTION.search(t):
        return None
    found = [m.group(0).strip() for m in _AMOUNT.finditer(t)]
    # Never infer a new amount when old and new amounts were both mentioned.
    if len(found) != 1:
        return None
    return found[0]
