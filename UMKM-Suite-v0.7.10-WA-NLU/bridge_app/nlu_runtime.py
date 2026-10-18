"""Read-first NLU integration for the audited UMKM WhatsApp bridge.

Never executes SQL constructed by an LLM, never writes financial/POS rows, and
never clears an existing guided/confirmation state to answer a side question.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from . import nlu_hub
from .permissions import PermissionDenied, has_permission, require_permission

log=logging.getLogger('umkm.nlu')


def _range(period):
    if period is None:
        return 'belum dipilih'
    if period.start is None:
        return 'Semua data'
    return f'{period.start:%d-%m-%Y} s.d. {period.end:%d-%m-%Y}'


async def _history(m,jid):
    try:
        return await m.ai_memory.read(jid)
    except Exception:
        log.exception('Conversation history unavailable')
        return []


async def _remember(m,jid,text,reply):
    try:
        await m.ai_memory.save(jid,text,reply)
    except Exception:
        log.exception('Could not save NLU report reference (best-effort)')


async def _financial(m,jid,task):
    """Reports are calculated from tenant-scoped posted transactions, not model text."""
    if task.kind=='finance_months':
        async with m.db.tenant_tx(jid) as (conn,ctx):
            require_permission(ctx,'finance.read')
            rows=await conn.fetch(
                """SELECT date_trunc('month',transaction_date)::date AS ym,COUNT(*)::int AS total
                   FROM transactions WHERE tenant_id=$1 AND status='posted'
                   GROUP BY 1 ORDER BY 1 DESC LIMIT 24""",ctx['tenant_id'])
        if not rows:
            reply='Belum ada transaksi berstatus *posted* yang tercatat untuk usaha ini.'
        else:
            reply='Saya cek langsung catatan usaha. Periode yang memiliki transaksi:\n' + '\n'.join(
                f"• {r['ym']:%m-%Y}: {r['total']} transaksi" for r in rows
            )+'\n\nMau saya rinci salah satu bulan atau buatkan PDF?'
        await m.gowa_send_text(jid,reply)
        await _remember(m,jid,task.text,reply)
        return

    period=task.period
    if period is None:
        await m.gowa_send_text(jid,'Bisa, Bos. Datanya mau periode mana? Misalnya *September 2026*, *bulan kemarin*, atau *bulan ini*.')
        return
    async with m.db.tenant_tx(jid) as (conn,ctx):
        require_permission(ctx,'finance.read')
        totals=await conn.fetchrow(
            """SELECT COUNT(*)::int AS total,
                      COALESCE(SUM(amount) FILTER (WHERE type='income'),0) AS income,
                      COALESCE(SUM(amount) FILTER (WHERE type='expense'),0) AS expense
               FROM transactions
               WHERE tenant_id=$1 AND status='posted'
                 AND ($2::date IS NULL OR transaction_date >= $2::date)
                 AND ($3::date IS NULL OR transaction_date <= $3::date)""",
            ctx['tenant_id'],period.start,period.end,
        )
        recent=[]
        if task.kind=='finance_details':
            recent=await conn.fetch(
                """SELECT transaction_date, type, amount, category, description
                   FROM transactions
                   WHERE tenant_id=$1 AND status='posted'
                     AND ($2::date IS NULL OR transaction_date >= $2::date)
                     AND ($3::date IS NULL OR transaction_date <= $3::date)
                   ORDER BY transaction_date DESC,created_at DESC LIMIT 12""",
                ctx['tenant_id'],period.start,period.end,
            )
    income=int(totals['income'] or 0); expense=int(totals['expense'] or 0)
    count=int(totals['total'] or 0)
    label='Semua periode' if period.start is None else _range(period)
    reply=(f'📊 *Pembukuan {ctx["business_name"]}*\n'
           f'Periode: {label}\n\n'
           f'• Transaksi tercatat: *{count}*\n'
           f'• Pemasukan: *{m.rupiah(income)}*\n'
           f'• Pengeluaran: *{m.rupiah(expense)}*\n'
           f'• Selisih pembukuan: *{m.rupiah(income-expense)}*\n')
    if task.kind=='finance_details':
        if recent:
            reply+='\n*Rincian terbaru:*\n'
            for row in recent:
                label_type='Masuk' if row['type']=='income' else 'Keluar'
                category=str(row['category'] or 'Lainnya')[:50]
                note=str(row['description'] or '')[:60]
                reply+=f"• {row['transaction_date']:%d-%m-%Y} {label_type} {m.rupiah(row['amount'])} — {category} ({note})\n"
            if count>len(recent):
                reply+=f'… {count-len(recent)} transaksi lain. PDF memuat seluruh data periode tersebut.\n'
        else:
            reply+='\nTidak ada transaksi posted pada periode tersebut.\n'
    if task.kind=='finance_summary' and count==0:
        reply+='\nTidak ditemukan transaksi posted pada periode tersebut.\n'
    reply+='\nCatatan: selisih pembukuan bukan saldo kas atau laba bersih. Untuk laporan, ketik *PDF-nya*. '
    await m.gowa_send_text(jid,reply)
    await _remember(m,jid,task.text,reply)


async def _stock(m,jid,task):
    if task.kind=='stock_item':
        if not task.query:
            await m.gowa_send_text(jid,'Produk apa yang mau saya periksa, Bos? Sebutkan nama atau SKU-nya.')
            return
        _,product,candidates=await m.pos.resolve_product(jid,task.query)
        if product:
            await m.gowa_send_text(jid,
                f"📦 *{product['name']}* (`{product['sku']}`)\n"
                f"Stok: *{m._qty(product['stock_on_hand'])} {product['unit']}*\n"
                f"Harga: *{m.rupiah(product['sell_price'])}*")
        elif candidates:
            choices='\n'.join(f"• {row['name']} (`{row['sku']}`)" for row in candidates[:8])
            await m.gowa_send_text(jid,'Ada beberapa produk yang mirip, Bos. Pilih salah satu:\n'+choices)
        else:
            await m.gowa_send_text(jid,f'Produk *{task.query[:90]}* belum saya temukan di katalog usaha.')
        return
    low=task.kind=='stock_low'
    ctx,rows=await m.pos.list_products(jid,low_stock_only=low,limit=50)
    if not rows:
        await m.gowa_send_text(jid,'Tidak ada produk yang cocok di hasil pemeriksaan.' if low else 'Katalog produk masih kosong.')
        return
    title='Stok rendah' if low else 'Daftar produk'
    lines=[f'📦 *{title} — {ctx["business_name"]}*']
    for row in rows[:25]:
        lines.append(f"• {row['name']} (`{row['sku']}`): {m._qty(row['stock_on_hand'])} {row['unit']} · {m.rupiah(row['sell_price'])}")
    if len(rows)>25:
        lines.append(f'… masih ada {len(rows)-25} baris pada sampel hasil.')
    await m.gowa_send_text(jid,'\n'.join(lines))


async def _pos_summary(m,jid,task):
    per=task.period
    ctx,row,low=await m.pos.pos_summary(jid,per.start,per.end)
    await m.gowa_send_text(jid,
         f'📊 *POS {ctx["business_name"]}*\nPeriode: {_range(per)}\n'
         f'Transaksi: *{row["sales_count"]}*\nOmzet: *{m.rupiah(row["revenue"])}*\n'
         f'Laba kotor: *{m.rupiah(row["gross_profit"])}*\nStok rendah: *{low}*')


async def _read(m,jid,task,access):
    role=access['role']
    kind=task.kind
    if kind.startswith('finance_'):
        if not has_permission(role,'finance.read'):
            await m.gowa_send_text(jid,'⛔ Peran akun ini tidak diizinkan membaca laporan keuangan.')
            return
        await _financial(m,jid,task)
    elif kind in {'export_pdf','export_excel'}:
        if not has_permission(role,'finance.export'):
            await m.gowa_send_text(jid,'⛔ Peran akun ini tidak diizinkan mengekspor laporan keuangan.')
            return
        if not task.period:
            await m.gowa_send_text(jid,'Mau PDF untuk bulan apa, Bos? Misalnya *PDF September 2026*.')
            return
        command='EXPORT PDF' if kind=='export_pdf' else 'EXPORT EXCEL'
        if task.period.start is None:
            command+=' SEMUA'
        await m.gowa_send_text(jid,f'Siap, saya siapkan laporan {"PDF" if kind=="export_pdf" else "Excel"} periode {_range(task.period)} dari database.')
        await m.handle_export(jid,command,task.period.start,task.period.end)
        await _remember(m,jid,task.text,'Laporan dikirim. Periode: '+_range(task.period))
    elif kind=='export_need_period':
        fmt = 'Excel' if 'excel' in nlu_hub.normalize(task.text) or 'xlsx' in nlu_hub.normalize(task.text) else 'PDF'
        reply=f'Tentu. {fmt}-nya untuk periode mana, Bos? Contoh: *bulan kemarin*, *Agustus 2026*, atau *semua data*.'
        await m.gowa_send_text(jid,reply)
        await _remember(m,jid,task.text,reply)
    elif kind=='scope_clarify':
        reply='Mau lihat semua data *keuangan*, *POS*, atau *pengingat*, Bos? Untuk seluruh PDF pembukuan, ketik *PDF semua data*.'
        await m.gowa_send_text(jid,reply)
        await _remember(m,jid,task.text,reply)
    elif kind in {'stock_low','stock_item','products_list'}:
        if not has_permission(role,'pos.read'):
            await m.gowa_send_text(jid,'⛔ Peran akun ini tidak diizinkan mengakses data stok.')
            return
        await _stock(m,jid,task)
    elif kind=='pos_summary':
        if not has_permission(role,'pos.read'):
            await m.gowa_send_text(jid,'⛔ Peran akun ini tidak diizinkan melihat laporan POS.')
            return
        await _pos_summary(m,jid,task)
    elif kind=='reminder_list':
        # Reuses the existing tenant-scoped read helper but deliberately does
        # NOT invoke send_active_reminders, which would overwrite a draft state.
        rows = await m.db.list_active_reminders(jid,20)
        if not rows:
            reply='Bos, belum ada pengingat *aktif* yang tersimpan. Saya tidak membuat pengingat baru.'
        else:
            lines=[f'📋 *Pengingat aktif ({len(rows)} ditampilkan)*']
            for idx,row in enumerate(rows,1):
                due=row['due_at'].astimezone(m.JAKARTA).strftime('%d-%m-%Y %H:%M')
                label=str(row.get('message') or '')[:200] if isinstance(row,dict) else str(row['message'])[:200]
                lines.append(f'{idx}. {label} — {due} WIB')
            reply='\n'.join(lines)
            if len(rows)==20:
                reply+='\nHanya 20 pengingat paling dekat yang ditampilkan.'
            reply+='\n\nIni adalah pengingat aktif; bukan riwayat pengingat yang sudah selesai/dibatalkan.'
        await m.gowa_send_text(jid,reply)
        await _remember(m,jid,task.text,reply)
    elif kind=='staff_list':
        if not has_permission(role,'staff.read'):
            await m.gowa_send_text(jid,'⛔ Peran akun ini tidak diizinkan melihat data karyawan.')
            return
        # Current helper is a read-only staff listing; keep_menu=False means
        # no new menu state, so an active guided draft remains untouched.
        await m.send_staff_list(jid,keep_menu=False)
    elif kind=='period_clarify':
        # A bare period is not permission to select a financial report.
        reply='Siap. Untuk periode *'+_range(task.period)+'*, Bos mau lihat *keuangan* atau *penjualan POS*?'
        await m.gowa_send_text(jid,reply)
        await _remember(m,jid,task.text,reply)
    elif kind=='greeting':
        role = str(access.get('role') or '').upper()
        if role == 'OWNER':
            reply='Halo, Bos! Mau saya cek keuangan, penjualan, stok, atau bantu catat transaksi hari ini? Tinggal bilang saja.'
        else:
            reply='Halo! Saya siap membantu sesuai akses akun Anda. Mau cek stok, penjualan, atau lihat menu?'
        await m.gowa_send_text(jid,reply)
    elif kind=='capabilities':
        ctx=await m.db.ai_business_context(jid)
        reply=m.assistant_profile.capabilities_text(ctx,'whatsapp')
        await m.gowa_send_text(jid,reply)
        await _remember(m,jid,task.text,reply)


async def _write_draft(m,jid,task,access):
    kind=task.kind
    role=access['role']
    if kind in {'income','expense'}:
        if not has_permission(role,'finance.write'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan mencatat transaksi keuangan.')
            return
        numbers=re.findall(r'(?<![\w])(?:rp\.?\s*)?\d[\d.,]*(?:\s*(?:ribu|rb|juta|jt))?(?!\w)',task.text,flags=re.I)
        if len(numbers)!=1:
            await m.start_guided_transaction(jid,'income' if kind=='income' else 'expense')
            return
        amount=m.parse_money_input(numbers[0])
        if amount is None:
            await m.gowa_send_text(jid,'Nominalnya belum jelas. Coba tulis, misalnya *catat pengeluaran 50rb bensin*.')
            return
        raw=nlu_hub.normalize(task.text)
        # Historical posting dates require a dedicated date-aware entry flow;
        # never silently backdate or misrepresent the transaction date.
        if re.search(r'\b(kemarin|minggu lalu|bulan lalu|tanggal|tahun lalu)\b',raw):
            await m.gowa_send_text(jid,'Saya menangkap rujukan tanggal lampau. Fitur input cepat mencatat tanggal saat diproses. Agar tidak salah tanggal, transaksi ini belum saya buat. Sebutkan apakah ingin memakai tanggal hari ini atau gunakan pencatatan yang mendukung tanggal khusus.')
            return
        desc=re.sub(r'\b(catat|pemasukan|pengeluaran|pendapatan|uang masuk|uang keluar|tolong|mohon|saya|ya|dong)\b',' ',raw)
        desc=desc.replace(numbers[0].casefold(),' ')
        desc=re.sub(r'\s+',' ',desc).strip(' ,.-')[:250]
        desc=desc or ('Pemasukan' if kind=='income' else 'Pengeluaran')
        typ='income' if kind=='income' else 'expense'
        payload={'tx_type':typ,'amount':amount,'description':desc,'category':'Lainnya'}
        await m.db.set_pending_action(jid,'transaction',payload,ttl_minutes=15)
        await m.gowa_send_text(jid,
            f'Baik, saya buat *draft {"pemasukan" if kind=="income" else "pengeluaran"}* dulu. Belum tersimpan.\n\n'
            f'Nominal: *{m.rupiah(amount)}*\nKeterangan: {desc}\n'
            'Kategori: Lainnya (bisa diedit)\n\n*1 SIMPAN · 2 EDIT · 0 BATAL*')
        return
    if kind=='sale':
        if not has_permission(role,'pos.sell'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan membuat penjualan POS.')
            return
        await m.handle_ai(jid,task.text)
        return
    if kind=='product_add':
        if not has_permission(role,'product.write'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan menambah produk.')
            return
        await m.db.set_pending_action(jid,'menu_pos',{'actions':['product_add']},ttl_minutes=20)
        await m.handle_menu_state(jid,'1')
        return
    if kind=='product_edit':
        if not has_permission(role,'product.write'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan mengubah produk.')
            return
        await m.begin_product_edit(jid)
        return
    if kind=='stock_adjust':
        if not has_permission(role,'stock.write'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan mengubah stok.')
            return
        await m.db.set_pending_action(jid,'menu_pos',{'actions':['stock_set']},ttl_minutes=20)
        await m.handle_menu_state(jid,'1')
        return
    if kind=='staff_manage':
        if not has_permission(role,'staff.write'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan mengelola karyawan.')
            return
        await m.send_staff_menu(jid)
        return
    if kind=='business_settings':
        if not has_permission(role,'business.write'):
            await m.gowa_send_text(jid,'⛔ Peran ini tidak diizinkan mengubah pengaturan usaha.')
            return
        await m.send_business_menu(jid)
        return
    if kind=='reminder_create':
        if await m.try_quick_reminder(jid,task.text):
            return
        await m.send_reminder_menu(jid)
        return


async def _side_free_question(m,jid,text,access):
    if not m.ai.is_configured():
        await m.gowa_send_text(jid,'Saya bisa melanjutkan draft yang tadi, tetapi jawaban bebas dari AI sedang tidak tersedia. Draft tetap aman.')
        return
    try:
        ctx=await m.db.ai_business_context(jid)
        history=await _history(m,jid)
        res=await m.ai.analyze_message(text,ctx,datetime.now(m.JAKARTA).isoformat(),dialogue=history,channel='whatsapp')
        if res.intent in {'general_question','finance_question','capabilities'} and not res.needs_clarification:
            answer=res.answer or 'Saya belum punya informasi yang cukup untuk menjawab secara pasti.'
        else:
            answer='Saya memahami pertanyaannya, tetapi tidak akan menjalankan tindakan baru saat draft masih aktif.'
    except Exception:
        log.exception('Side question AI unavailable')
        answer='Saya belum bisa menjawab pertanyaan tersebut sekarang. Draft Anda tetap tersimpan.'
    await m.gowa_send_text(jid,answer)
    await _remember(m,jid,text,answer)


async def maybe_handle(jid,text,m):
    """Return True only when NLU safely handled the complete incoming message."""
    # Zero, confirmation, and menu clicks must stay on their original fast path.
    # This also prevents extra DB queries and never changes existing semantics.
    simple=nlu_hub.normalize(text)
    if simple in {'0','1','2','3','4','5','6','7','8','9','simpan','save','ya','iya',
                  'batal','batalkan','batalin','hapus','edit','ubah','menu','/menu','kembali'}:
        return False
    pending=await m.db.get_pending_action(jid)
    history=await _history(m,jid)
    tasks=nlu_hub.plan(text,datetime.now(m.JAKARTA).date(),history)
    if not tasks:
        return False
    if tasks[0].kind=='too_many':
        await m.gowa_send_text(jid,'Saya menangkap banyak permintaan sekaligus. Tolong batasi maksimal tiga tugas per pesan supaya tidak ada tindakan yang tertukar.')
        return True
    reads=[t for t in tasks if t.kind in nlu_hub.READ_ONLY or t.kind in {'export_need_period','period_clarify','greeting'}]
    writes=[t for t in tasks if t.kind in nlu_hub.WRITE]
    unknown=[t for t in tasks if t.kind=='unknown']
    others=[t for t in tasks if t.kind in nlu_hub.OPEN]
    if pending and not nlu_hub.is_side_question(text,tasks):
        return False
    if not pending and not reads and not writes and not others:
        return False

    access=await m.db.get_whatsapp_access(jid)
    if not access or access['access_status']!='active':
        return False

    # Any read-only side question may be answered without touching the active
    # action: no set_pending_action, clear_pending_action or menu redirection.
    if pending:
        if reads:
            for item in reads:
                try:
                    await _read(m,jid,item,access)
                except (PermissionDenied, PermissionError,ValueError,LookupError) as exc:
                    await m.gowa_send_text(jid,f'⚠️ Tidak dapat membaca data: {exc}')
                except Exception:
                    log.exception('Read-only side request failed')
                    await m.gowa_send_text(jid,'Maaf, pemeriksaan data belum berhasil; draft tetap tersimpan.')
        if unknown and nlu_hub.is_side_question(text,tasks):
            await _side_free_question(m,jid,unknown[0].text,access)
        if writes or others:
            await m.gowa_send_text(jid,'Saya menangkap tugas baru itu, tetapi *draft sebelumnya masih aktif*. Simpan atau batalkan draft tadi lebih dulu agar tidak tertukar.')
        await m.gowa_send_text(jid,'✅ *Draft/langkah sebelumnya tetap aktif.* Lanjutkan jawaban yang tadi, atau ketik *batalkan*.')
        return True

    # If two or more writes are requested, ask for explicit selection, never
    # silently drop an instruction or overwrite the first draft with the next.
    if len(writes)>1:
        labels='\n'.join(f'{i}. {t.text[:120]}' for i,t in enumerate(writes,1))
        await m.gowa_send_text(jid,'Saya menangkap beberapa tindakan yang mengubah data:\n'+labels+'\n\nAgar aman, tuliskan *satu tindakan dulu*. Setelah konfirmasi, lanjutkan yang berikutnya.')
        return True

    for item in reads:
        try:
            await _read(m,jid,item,access)
        except (PermissionDenied, PermissionError,ValueError,LookupError) as exc:
            await m.gowa_send_text(jid,f'⚠️ Tidak dapat menjalankan permintaan: {exc}')
        except Exception:
            log.exception('NLU read error')
            await m.gowa_send_text(jid,'⚠️ Pemeriksaan data gagal. Saya tidak akan mengarang angka.')
    if unknown:
        if len(tasks)==1:
            return False
        await m.gowa_send_text(jid,'Ada satu bagian yang belum saya pahami: '+unknown[0].text[:140]+'\nSilakan ulangi bagian itu secara terpisah.')
    if writes:
        try:
            await _write_draft(m,jid,writes[0],access)
        except (PermissionDenied,PermissionError,ValueError,LookupError) as exc:
            await m.gowa_send_text(jid,f'⚠️ Tidak dapat membuka alur tindakan: {exc}')
        except Exception:
            log.exception('NLU write draft error')
            await m.gowa_send_text(jid,'⚠️ Draft belum bisa dibuat. Tidak ada transaksi yang saya simpan.')
    if others:
        for item in others:
            if item.kind=='pos_menu':
                await m.send_pos_menu(jid)
            elif item.kind=='reminder_menu':
                await m.send_reminder_menu(jid)
            elif item.kind=='export_menu':
                await m.send_export_menu(jid)
    return True
