"""Context-aware Indonesian NLU router for the UMKM WhatsApp bridge.

Pure functions only. No network, persistence, permission checks or business writes.
The caller must authorize all read/write actions and keep draft confirmation explicit.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Sequence

MONTHS = {
    'januari': 1, 'jan': 1, 'februari': 2, 'feb': 2, 'maret': 3, 'mar': 3,
    'april': 4, 'apr': 4, 'mei': 5, 'juni': 6, 'jun': 6,
    'juli': 7, 'jul': 7, 'agustus': 8, 'agt': 8, 'agu': 8,
    'september': 9, 'sep': 9, 'oktober': 10, 'okt': 10,
    'november': 11, 'nov': 11, 'desember': 12, 'des': 12,
}

# Only conservative, common spellings; never rewrite arbitrary business names.
TYPO = {
    'keuangn':'keuangan', 'keungn':'keuangan', 'keuanggan':'keuangan',
    'pengluaran':'pengeluaran', 'pengeluran':'pengeluaran',
    'pemassukan':'pemasukan', 'pemasukkan':'pemasukan',
    'catet':'catat', 'catetin':'catat', 'catatin':'catat', 'catatkan':'catat',
    'brp':'berapa', 'brrp':'berapa', 'brapa':'berapa',
    'bln':'bulan', 'buln':'bulan', 'kemaren':'kemarin',
    'laporn':'laporan', 'laprannya':'laporannya',
    'tranaksi':'transaksi', 'transksi':'transaksi',
    'stokk':'stok', 'stoek':'stok', 'prodak':'produk',
    'pdef':'pdf', 'expot':'export', 'ekspor':'export',
    'bikinin':'buat', 'buatin':'buat', 'tolongin':'tolong',
    'batalkan':'batal', 'batalin':'batal', 'tambahkan':'tambah',
    'keuangannya':'keuangan', 'keuanganya':'keuangan', 'keuanganya?':'keuangan',
    'keungannya':'keuangan', 'keunganya':'keuangan', 'keungn':'keuangan',
    'pengeluarannya':'pengeluaran', 'pengeluaranya':'pengeluaran',
    'pemasukannya':'pemasukan', 'pemasukanya':'pemasukan',
    'keungnnya':'keuangan', 'laporanya':'laporan', 'laporannya':'laporan',
    'bulannya':'bulan', 'kemarinn':'kemarin', 'kmren':'kemarin',
    'keuagan':'keuangan', 'keuagn':'keuangan', 'pengluarn':'pengeluaran',
    'pengigat':'pengingat', 'pengingatn':'pengingat', 'penginget':'pengingat',
    'remnder':'reminder', 'remindernya':'reminder', 'pengingatnya':'pengingat',
    'duit':'uang', 'brapa':'berapa', 'tlong':'tolong',
}

QUESTION = re.compile(r'\b(berapa|apa|apakah|bagaimana|gimana|gmn|kenapa|mengapa|siapa|kapan|mana|tolong cek|cek|lihat|tampilkan|tunjukkan|rincian)\b|\?',re.I)
COMMAND = re.compile(r'\b(catat|masukkan|masukin|buat|bikin|jual|bayar|ubah|tambah|atur|hapus|kirim|export|download|unduh|berikan|kasih)\b',re.I)
READ_ONLY = frozenset({'finance_summary','finance_months','finance_details','export_pdf','export_excel','reminder_list','scope_clarify',
                        'pos_summary','stock_low','stock_item','products_list','staff_list','capabilities'})
WRITE = frozenset({'income','expense','sale','reminder_create','product_add','product_edit','stock_adjust',
                   'staff_manage','business_settings','reminder_manage'})
OPEN = frozenset({'pos_menu','reminder_menu','export_menu'})

@dataclass(frozen=True)
class Period:
    start: date | None
    end: date | None
    label: str
    explicit: bool = True

@dataclass(frozen=True)
class Task:
    kind: str
    text: str
    period: Period | None = None
    query: str | None = None


def normalize(text: str) -> str:
    t = str(text or '').casefold().strip()
    for digit in '0123456789':
        t = t.replace(digit+'️⃣', digit)
    t = re.sub(r'\s+', ' ', t)
    # Do not rewrite product/SKU names: aliases apply only to exact tokens.
    t = re.sub(r'\b[\w]+\b', lambda m: TYPO.get(m.group(), m.group()), t)
    return t


def _span(y: int, m: int) -> Period:
    end = calendar.monthrange(y, m)[1]
    full = ('Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember')[m-1]
    return Period(date(y,m,1),date(y,m,end), f'{full} {y}')


def period_from_text(text: str, now: date) -> Period | None:
    t = normalize(text)
    if re.search(r'\b(bulan (kemarin|lalu|sebelumnya)|bln lalu)\b',t):
        last_day=now.replace(day=1)-timedelta(days=1)
        return _span(last_day.year, last_day.month)
    if re.search(r'\b(bulan ini|bulan sekarang|bulan berjalan)\b',t):
        return _span(now.year,now.month)
    month_regex = r'\b('+'|'.join(sorted(MONTHS,key=len,reverse=True))+r')(?:\s+(20\d{2}))?\b'
    m = re.search(month_regex,t)
    if m:
        num=MONTHS[m.group(1)]; year=int(m.group(2)) if m.group(2) else (now.year-1 if num>now.month else now.year)
        return _span(year,num)
    if re.search(r'\b(hari ini|sekarang|today)\b',t):
        return Period(now,now,'Hari ini')
    if re.search(r'\b(kemarin|yesterday)\b',t):
        day=now-timedelta(days=1)
        return Period(day,day,'Kemarin')
    if re.search(r'\b(minggu ini)\b',t):
        return Period(now-timedelta(days=now.weekday()),now,'Minggu ini')
    if re.search(r'\b(tahun ini)\b',t):
        return Period(date(now.year,1,1),date(now.year,12,31),f'Tahun {now.year}')
    if re.search(r'\b(?:semua|seluruh)\s+(?:data|transaksi|pembukuan|laporan|catatan|periode|riwayat)\b|\b(?:dari awal|semua tahun|all data|keseluruhan data)\b',t):
        return Period(None,None,'Semua data')
    return None


def previous_period(dialogue: Sequence[dict], now: date) -> Period | None:
    """Resolve only the latest *backend-authored* report reference.

    This is bounded by the tenant/user conversation memory TTL and role. Do
    not mine arbitrary assistant answers for dates; LLM text isn't a report.
    """
    recent = [str(e.get('content') or '') for e in (dialogue or [])[-12:]
              if e.get('role') == 'assistant']
    if not recent:
        return None
    content = recent[-1]
    if not (content.startswith('📊 *Pembukuan ') or
            content.startswith('Laporan dikirim. Periode: ')):
        return None
    if re.search(r'Periode:\s*(?:Semua data|Semua periode)\b', content, re.I):
        return Period(None, None, 'Semua data', explicit=False)
    found = re.search(r'Periode:\s*(\d{2}-\d{2}-\d{4})\s+s\.d\.\s*(\d{2}-\d{2}-\d{4})', content)
    if not found:
        return None
    from datetime import datetime
    try:
        start = datetime.strptime(found.group(1), '%d-%m-%Y').date()
        end = datetime.strptime(found.group(2), '%d-%m-%Y').date()
    except ValueError:
        return None
    if start <= end and start <= now and (end-start).days < 370:
        return Period(start, end, f'{start:%d-%m-%Y} s.d. {end:%d-%m-%Y}', explicit=False)
    return None


def _awaiting_export_scope(dialogue: Sequence[dict]) -> str | None:
    """Recognize a period-choice prompt emitted by our read-only export path."""
    last = (dialogue or [])[-1:] 
    if not last or last[0].get('role') != 'assistant':
        return None
    message = normalize(str(last[0].get('content') or ''))
    if message.startswith('tentu. pdf-nya untuk periode mana') or message.startswith('mau pdf untuk bulan apa'):
        return 'export_pdf'
    if message.startswith('tentu. excel-nya untuk periode mana') or message.startswith('mau excel untuk bulan apa'):
        return 'export_excel'
    return None


def _reminder_listing(text: str) -> bool:
    """Listing must take precedence over reminder creation and menu wizard."""
    if not re.search(r'\b(pengingat|reminder)\b',text):
        return False
    if re.search(r'\b(buat|bikin|buatkan|pasang|atur|set|tambah|hapus|edit|ubah|ganti|ingatkan|jadwalkan|batalkan|batal)\b',text):
        return False
    return bool(re.search(r'\b(apa saja|apa aja|apa yang|yang tersimpan|yang tercatat|yang aktif|tersimpan|tercatat|aktif|daftar|list|lihat|cek|tampilkan|punya|saya|sudah ada|masih ada)\b',text))


def split_clauses(text: str) -> list[str]:
    raw=str(text or '').strip()
    # Natural connectors split only if a new *independent* action begins next.
    splitter=re.compile(r'\s*(?:[;\n]+|\b(?:terus|lalu|habis itu|kemudian|dan)\b)\s*',re.I)
    pieces=[]; pos=0
    for match in splitter.finditer(raw):
        # 'bulan lalu' is a period expression, not two independent actions.
        if re.fullmatch(r'lalu', match.group().strip(), re.I):
            previous = normalize(raw[:match.start()]).split()
            if previous and previous[-1] in {'bulan', 'minggu', 'tahun', 'hari'}:
                continue
        nxt=normalize(raw[match.end():]).lstrip(' ,')
        if not nxt:
            continue
        if re.match(r'^(?:tolong\s+|mohon\s+|sekaligus\s+|coba\s+)?(?:catat|pengeluaran|pemasukan|cek|berapa|bagaimana|buat|bikin|export|pdf|excel|tampilkan|lihat|kirim|berikan|ingatkan|tambah|ubah|stok|daftar|laporan)\b',nxt):
            part=raw[pos:match.start()].strip(' ,')
            if part:
                pieces.append(part)
            pos=match.end()
    tail=raw[pos:].strip(' ,')
    if tail:
        pieces.append(tail)
    return pieces[:5]


def classify(text: str, now: date, dialogue: Sequence[dict] | None=None) -> Task | None:
    t=normalize(text)
    if not t or t.isdigit() or t in {'1','2','0','menu','batal','hapus','iya','ya','simpan'}:
        return None
    period=period_from_text(t,now)
    # Resolve the answer to an explicit PDF/Excel scope question before other
    # intents. This prevents 'semua data' falling into generic AI chat.
    requested_format = _awaiting_export_scope(dialogue or [])
    if requested_format and period is not None and re.fullmatch(
        r'(?:untuk\s+)?(?:semua(?:\s+(?:data|pembukuan|transaksi|laporan|catatan|periode))?|seluruh(?:\s+(?:data|pembukuan|transaksi|laporan|catatan|periode))?|bulan\s+\w+|(?:januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)(?:\s+20\d{2})?|tahun\s+ini|dari\s+awal|all\s+data)(?:\s+(?:ya|aja|saja|dong))?[?.!]*', t):
        return Task(requested_format,text,period)
    # Explanations of feature mechanics must not trigger an export or a write.
    if re.match(r'^(?:bagaimana|gimana|cara|kenapa|apa)\s+(?:cara\s+)?(?:buat|bikin|export|simpan|catat|hapus)\b',t):
        return None
    is_export=bool(re.search(r'\b(pdf|excel|xlsx|export|unduh|download)\b',t))
    is_finance=bool(re.search(r'\b(keuangan|pembukuan|pemasukan|pengeluaran|uang masuk|uang keluar|transaksi|saldo|kas|arus kas|rekap uang)\b',t))
    # If we JUST asked whether a bare period means finance or POS, a short
    # choice inherits that specific period from the user's previous message.
    history = list(dialogue or [])[-2:]
    if len(history)==2 and history[0].get('role')=='user' and history[1].get('role')=='assistant':
        last_answer = str(history[1].get('content') or '')
        if 'Bos mau lihat *keuangan* atau *penjualan POS*?' in last_answer:
            selected = period_from_text(str(history[0].get('content') or ''),now)
            if selected is not None:
                if t in {'keuangan','laporan keuangan','pembukuan'}:
                    return Task('finance_summary',text,selected)
                if t in {'pos','penjualan','penjualan pos','omzet'}:
                    return Task('pos_summary',text,selected)
    # A direct period correction can inherit topic only from a recent FINANCE
    # conversation, never from arbitrary AI-produced monetary claims.
    if re.fullmatch(r'(?:untuk |yang |maksud saya |eh |maksudnya )*(bulan (?:kemarin|lalu|ini)|september(?: 20\d{2})?|agustus(?: 20\d{2})?|[a-z]+ 20\d{2})(?: ya| dong| saja| aja)?[?.!]*',t):
        if period and _finance_topic(dialogue or []):
            return Task('finance_summary',text,period)
        if period:
            return Task('period_clarify',text,period)
    if is_export and (re.search(r'\b(keuangan|pembukuan|laporan|transaksi|pdf|excel|export)\b',t)):
        # Even a bare 'berikan pdf' inherits the latest verified backend
        # finance report. Otherwise ask; NEVER default silently to this month.
        if period is None and not re.search(r'\b(semua|seluruh|dari awal)\b',t):
            period=previous_period(dialogue or [],now)
        if period is None:
            return Task('export_need_period',text)
        return Task('export_pdf' if 'pdf' in t else 'export_excel',text,period)
    if _reminder_listing(t):
        return Task('reminder_list',text)
    if t in {'semua data','seluruh data','semua pembukuan','seluruh pembukuan','semua laporan','semua transaksi','semua'}:
        # A bare scope outside an export clarification is ambiguous.
        return Task('scope_clarify',text,period)
    if re.search(r'\b(bulan apa (saja|aja)|bulan mana (saja|aja)|periode apa (saja|aja))\b',t) and (is_finance or 'data' in t):
        return Task('finance_months',text)
    if re.search(r'\b(berikan data(nya)?|kasih data(nya)?|tampilkan data(nya)?|daftar transaksi|rincian transaksi|detail transaksi|transaksi apa saja|data (yang )?tercatat)\b',t):
        return Task('finance_details',text,period or previous_period(dialogue or [],now))
    if (is_finance or ('laporan' in t and period)) and (QUESTION.search(t) or period is not None or re.search(r'\b(ringkasan|laporan|rekap|rekapan)\b',t)):
        if not re.search(r'\b(jual|penjualan pos|omzet pos)\b',t):
            return Task('finance_summary',text,period or _span(now.year,now.month))
    if re.search(r'\b(stok (rendah|tipis|menipis|habis|minimum)|barang (yang )?(hampir habis|stoknya (tipis|menipis)))\b',t):
        return Task('stock_low',text)
    if re.search(r'\b(daftar|list|semua|lihat|tampilkan)\s+(?:data\s+)?produk\b',t):
        return Task('products_list',text)
    if re.search(r'\b(stok|stock|harga)\b',t) and QUESTION.search(t):
        q=re.sub(r'\b(?:berapa|stoknya|stok|stock|harga|barang|produk|yang|ada|sisa|tersedia|saat ini|tolong|cek|lihat|dong|sekarang|nih|ya|di toko|di gudang|tinggal)\b',' ',t)
        q=re.sub(r'[^\w\s-]',' ',q).strip()
        return Task('stock_item',text,query=q if q else None)
    if re.search(r'\b(daftar|list|lihat|tampilkan)\s+(?:data\s+)?(?:karyawan|staff|staf)\b',t):
        return Task('staff_list',text)
    if re.search(r'\b(omzet|penjualan|laporan pos|hasil pos|transaksi pos)\b',t) and (QUESTION.search(t) or 'laporan' in t):
        return Task('pos_summary',text,period or Period(now,now,'Hari ini'))
    if re.search(r'\b(bisa apa|bisa bantu apa|fitur apa|apa saja yang bisa|kamu bisa apa)\b',t):
        return Task('capabilities',text)
    if re.search(r'\b(tambah|buat|daftar)\s+(?:sebuah\s+)?produk\s*(baru)?\b',t):
        return Task('product_add',text)
    if re.search(r'\b(edit|ubah|ganti|perbarui|koreksi)\s+(?:harga|nama|detail|data)?\s*produk\b',t):
        return Task('product_edit',text)
    if re.search(r'\b(atur|set|ubah|sesuaikan|koreksi|opname)\s+(?:jumlah\s+)?stok\b',t):
        return Task('stock_adjust',text)
    if re.search(r'\b(kelola|tambah|undang|edit|ubah|hapus)\s+(?:data\s+)?(?:karyawan|staff|staf)\b',t):
        return Task('staff_manage',text)
    if re.search(r'\b(ubah|ganti|kelola|pengaturan)\s+(?:nama\s+)?usaha\b',t):
        return Task('business_settings',text)
    if re.search(r'\b(daftar|lihat|cek)\s+pengingat\b',t):
        return Task('reminder_list',text)
    if re.search(r'\b(buat|atur|pasang|ingatkan|pengingat)\b',t) and re.search(r'\b(ingatkan|pengingat|reminder)\b',t):
        return Task('reminder_create',text)
    if ((re.search(r'\b(catat|masuk(in|kan)?|tulis|rekam)\b',t) and re.search(r'\b(pemasukan|pendapatan|uang masuk)\b',t)) or re.match(r'^(?:pemasukan|pendapatan)\s+\d',t)) and not QUESTION.search(t):
        return Task('income',text)
    if ((re.search(r'\b(catat|masuk(in|kan)?|tulis|rekam)\b',t) and re.search(r'\b(pengeluaran|biaya|uang keluar)\b',t)) or re.match(r'^(?:pengeluaran|biaya)\s+\d',t)) and not QUESTION.search(t):
        return Task('expense',text)
    if re.search(r'\b(jual|penjualan)\b',t) and not QUESTION.search(t):
        return Task('sale',text)
    if re.search(r'\b(kelola|menu|buka)\s+pos\b',t):
        return Task('pos_menu',text)
    if re.search(r'\b(buka|pilih)\s+export\b',t):
        return Task('export_menu',text)
    if re.fullmatch(r'(halo+|hai+|hi+|pagi|siang|sore|malam|assalamualaikum|assalamu alaikum|selamat (?:pagi|siang|sore|malam))[!. ]*',t):
        return Task('greeting',text)
    return None


def _finance_topic(dialogue: Sequence[dict]) -> bool:
    # Inspect only the latest short exchange; an ancient unrelated question
    # must not silently select a financial report.
    entries = list(dialogue or [])[-4:]
    for entry in reversed(entries):
        body = normalize(str(entry.get('content') or ''))
        if re.search(r'\b(keuangan|pembukuan|pemasukan|pengeluaran|saldo|transaksi|laporan|pdf)\b',body):
            return True
        if 'Periode:' in str(entry.get('content') or '') and 'pembukuan' in body:
            return True
    return False

def plan(text: str, now: date, dialogue: Sequence[dict] | None=None) -> list[Task]:
    pieces=split_clauses(text)
    if len(pieces)>3:
        return [Task('too_many',text)]
    tasks = [classify(p,now,dialogue) or Task('unknown',p) for p in pieces]
    # 'ringkasan September dan PDF-nya' must send the SAME verified period.
    # We propagate only in the current user message; not inferred from LLM.
    source = None
    result = []
    for task in tasks:
        if task.period is not None and task.kind not in {'period_clarify'}:
            source = task.period
        if task.kind == 'export_need_period' and source is not None:
            task=replace(task,kind='export_pdf' if 'pdf' in normalize(task.text) else 'export_excel',period=source)
        result.append(task)
    return result


def is_side_question(text: str, tasks: Sequence[Task]) -> bool:
    # Only interrupt an active wizard for an explicit request or question.
    t=normalize(text)
    if not tasks or t.isdigit() or t in {'simpan','iya','ya','hapus','edit','batal'}:
        return False
    if all(x.kind in READ_ONLY or x.kind in {'export_need_period'} for x in tasks):
        return True
    # For otherwise-unrecognized wording, only question-marked or question-word
    # messages are a side question. Never accidentally consume a form value.
    return bool(QUESTION.match(t) or '?' in t)
