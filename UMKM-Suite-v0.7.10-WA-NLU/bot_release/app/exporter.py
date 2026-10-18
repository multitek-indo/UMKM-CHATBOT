from __future__ import annotations

import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import xlsxwriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

EXPORT_DIR = Path(os.environ.get("EXPORT_TMP_DIR", "/tmp/umkm-bot-exports"))
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    value = value.strip("._-")
    return value[:60] or "usaha"


def _rupiah(value: int | float) -> str:
    n = int(value or 0)
    sign = "-" if n < 0 else ""
    return f"{sign}Rp{abs(n):,}".replace(",", ".")


def _normalize_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        getter = row.get if hasattr(row, "get") else lambda k, d=None: row[k] if k in row else d
        amount = int(getter("amount", 0) or 0)
        tx_type = str(getter("type", "") or "")
        normalized.append(
            {
                "transaction_date": getter("transaction_date"),
                "type": tx_type,
                "amount": amount,
                "category": str(getter("category", "Umum") or "Umum"),
                "description": str(getter("description", "") or ""),
                "income": amount if tx_type == "income" else 0,
                "expense": amount if tx_type == "expense" else 0,
            }
        )
    return normalized


def _new_path(business_name: str, ext: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    prefix = _safe_filename(business_name)
    fd, path = tempfile.mkstemp(
        prefix=f"laporan_keuangan_{prefix}_{stamp}_",
        suffix=f".{ext}",
        dir=EXPORT_DIR,
    )
    os.close(fd)
    return Path(path)


def build_excel_report(
    business_name: str,
    period_label: str,
    generated_at: datetime,
    rows: Iterable[Any],
) -> str:
    data = _normalize_rows(rows)
    total_income = sum(r["income"] for r in data)
    total_expense = sum(r["expense"] for r in data)
    balance = total_income - total_expense

    path = _new_path(business_name, "xlsx")
    workbook = xlsxwriter.Workbook(str(path), {"constant_memory": False})
    workbook.set_properties(
        {
            "title": f"Laporan Keuangan - {business_name}",
            "subject": period_label,
            "author": "Asisten UMKM Indonesia",
            "company": business_name,
            "comments": "Dibuat otomatis oleh Asisten UMKM Indonesia",
        }
    )

    title_fmt = workbook.add_format(
        {"bold": True, "font_size": 18, "font_color": "#17365D", "align": "left"}
    )
    business_fmt = workbook.add_format(
        {"bold": True, "font_size": 12, "font_color": "#404040", "align": "left"}
    )
    label_fmt = workbook.add_format({"bold": True, "font_color": "#595959"})
    value_fmt = workbook.add_format({"font_color": "#262626"})
    money_fmt = workbook.add_format(
        {"num_format": 'Rp #,##0;[Red]-Rp #,##0', "bold": True, "font_size": 12}
    )
    kpi_label_fmt = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#17365D",
            "align": "center",
            "valign": "vcenter",
            "border": 1,
        }
    )
    kpi_value_fmt = workbook.add_format(
        {
            "bold": True,
            "font_size": 13,
            "num_format": 'Rp #,##0;[Red]-Rp #,##0',
            "align": "center",
            "valign": "vcenter",
            "border": 1,
        }
    )
    table_header_fmt = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#17365D",
            "align": "center",
            "valign": "vcenter",
            "border": 1,
        }
    )
    date_fmt = workbook.add_format({"num_format": "dd-mm-yyyy", "border": 1})
    text_fmt = workbook.add_format({"border": 1, "valign": "top"})
    type_fmt = workbook.add_format({"border": 1, "align": "center"})
    tx_money_fmt = workbook.add_format({"num_format": 'Rp #,##0;[Red]-Rp #,##0', "border": 1})
    note_fmt = workbook.add_format(
        {"italic": True, "font_color": "#7F7F7F", "font_size": 9, "text_wrap": True}
    )
    total_label_fmt = workbook.add_format(
        {"bold": True, "align": "right", "bg_color": "#D9EAF7", "border": 1}
    )
    total_money_fmt = workbook.add_format(
        {"bold": True, "num_format": 'Rp #,##0;[Red]-Rp #,##0', "bg_color": "#D9EAF7", "border": 1}
    )

    summary = workbook.add_worksheet("Ringkasan")
    summary.hide_gridlines(2)
    summary.set_column("A:A", 3)
    summary.set_column("B:B", 24)
    summary.set_column("C:D", 20)
    summary.set_column("E:E", 3)

    summary.merge_range("B2:D2", "LAPORAN KEUANGAN", title_fmt)
    summary.merge_range("B3:D3", business_name, business_fmt)
    summary.write("B5", "Periode", label_fmt)
    summary.write("C5", period_label, value_fmt)
    summary.write("B6", "Dibuat", label_fmt)
    summary.write("C6", generated_at.strftime("%d-%m-%Y %H:%M WIB"), value_fmt)

    summary.write("B9", "TOTAL PEMASUKAN", kpi_label_fmt)
    summary.write("C9", "TOTAL PENGELUARAN", kpi_label_fmt)
    summary.write("D9", "SALDO", kpi_label_fmt)
    summary.set_row(8, 25)

    # Formula mengacu ke sheet Transaksi seperti laporan proyek sebelumnya.
    end_excel_row = max(2, len(data) + 1)
    income_formula = f"=SUM(Transaksi!F2:F{end_excel_row})"
    expense_formula = f"=SUM(Transaksi!G2:G{end_excel_row})"
    summary.write_formula("B10", income_formula, kpi_value_fmt, total_income)
    summary.write_formula("C10", expense_formula, kpi_value_fmt, total_expense)
    summary.write_formula("D10", "=B10-C10", kpi_value_fmt, balance)
    summary.set_row(9, 28)

    summary.write("B13", "Jumlah Transaksi", label_fmt)
    summary.write_number("C13", len(data), value_fmt)
    summary.merge_range(
        "B16:D17",
        "Nilai ringkasan dihitung dari sheet Transaksi agar laporan mudah diperiksa. "
        "Sumber data: database keuangan tenant Anda pada Asisten UMKM Indonesia.",
        note_fmt,
    )

    tx = workbook.add_worksheet("Transaksi")
    tx.freeze_panes(1, 0)
    tx.autofilter(0, 0, max(1, len(data)), 6)
    tx.set_column("A:A", 7)
    tx.set_column("B:B", 14)
    tx.set_column("C:C", 15)
    tx.set_column("D:D", 20)
    tx.set_column("E:E", 42)
    tx.set_column("F:G", 18)
    tx.set_row(0, 24)

    headers = ["No", "Tanggal", "Jenis", "Kategori", "Keterangan", "Pemasukan", "Pengeluaran"]
    for col, header in enumerate(headers):
        tx.write(0, col, header, table_header_fmt)

    for idx, row in enumerate(data, start=1):
        excel_row = idx
        tx.write_number(excel_row, 0, idx, text_fmt)
        d = row["transaction_date"]
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            tx.write_datetime(excel_row, 1, datetime(d.year, d.month, d.day), date_fmt)
        else:
            tx.write(excel_row, 1, str(d or ""), text_fmt)
        tx.write(excel_row, 2, "Pemasukan" if row["type"] == "income" else "Pengeluaran", type_fmt)
        tx.write(excel_row, 3, row["category"], text_fmt)
        tx.write(excel_row, 4, row["description"], text_fmt)
        tx.write_number(excel_row, 5, row["income"], tx_money_fmt)
        tx.write_number(excel_row, 6, row["expense"], tx_money_fmt)

    total_row = len(data) + 2
    tx.merge_range(total_row, 0, total_row, 4, "TOTAL", total_label_fmt)
    if data:
        tx.write_formula(
            total_row,
            5,
            f"=SUM(F2:F{len(data)+1})",
            total_money_fmt,
            total_income,
        )
        tx.write_formula(
            total_row,
            6,
            f"=SUM(G2:G{len(data)+1})",
            total_money_fmt,
            total_expense,
        )
    else:
        tx.write_number(total_row, 5, 0, total_money_fmt)
        tx.write_number(total_row, 6, 0, total_money_fmt)

    tx.set_landscape()
    tx.fit_to_pages(1, 0)
    tx.set_margins(0.3, 0.3, 0.5, 0.5)
    tx.set_header(f"&C&BLAPORAN KEUANGAN - {_safe_filename(business_name)}")
    tx.set_footer("&LAsisten UMKM Indonesia&CPage &P / &N&R&D &T")

    workbook.close()
    return str(path)


def build_pdf_report(
    business_name: str,
    period_label: str,
    generated_at: datetime,
    rows: Iterable[Any],
) -> str:
    data = _normalize_rows(rows)
    total_income = sum(r["income"] for r in data)
    total_expense = sum(r["expense"] for r in data)
    balance = total_income - total_expense

    path = _new_path(business_name, "pdf")
    page_size = landscape(A4)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=page_size,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Laporan Keuangan - {business_name}",
        author="Asisten UMKM Indonesia",
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#17365D"),
        spaceAfter=2 * mm,
    )
    business = ParagraphStyle(
        "Business",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#404040"),
    )
    meta = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#606060"),
    )
    cell = ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        alignment=TA_LEFT,
    )
    cell_center = ParagraphStyle("CellCenter", parent=cell, alignment=TA_CENTER)
    cell_right = ParagraphStyle("CellRight", parent=cell, alignment=TA_RIGHT)
    header_cell = ParagraphStyle(
        "HeaderCell",
        parent=cell_center,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )
    small = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor("#777777"),
    )

    story = [
        Paragraph("LAPORAN KEUANGAN", title),
        Paragraph(_escape_pdf(business_name), business),
        Spacer(1, 2 * mm),
        Paragraph(f"Periode: {_escape_pdf(period_label)}", meta),
        Paragraph(f"Dibuat: {generated_at.strftime('%d-%m-%Y %H:%M WIB')}", meta),
        Spacer(1, 4 * mm),
    ]

    kpi_data = [
        ["TOTAL PEMASUKAN", "TOTAL PENGELUARAN", "SALDO", "JUMLAH TRANSAKSI"],
        [_rupiah(total_income), _rupiah(total_expense), _rupiah(balance), str(len(data))],
    ]
    kpi = Table(kpi_data, colWidths=[62 * mm, 62 * mm, 62 * mm, 45 * mm], rowHeights=[9 * mm, 10 * mm])
    kpi.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17365D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("FONTSIZE", (0, 1), (-1, 1), 10),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C6D1")),
            ]
        )
    )
    story.extend([kpi, Spacer(1, 5 * mm)])

    table_data = [[
        Paragraph("No", header_cell),
        Paragraph("Tanggal", header_cell),
        Paragraph("Jenis", header_cell),
        Paragraph("Kategori", header_cell),
        Paragraph("Keterangan", header_cell),
        Paragraph("Pemasukan", header_cell),
        Paragraph("Pengeluaran", header_cell),
    ]]

    if data:
        for idx, row in enumerate(data, start=1):
            d = row["transaction_date"]
            if isinstance(d, datetime):
                d = d.date()
            date_text = d.strftime("%d-%m-%Y") if isinstance(d, date) else str(d or "")
            table_data.append(
                [
                    Paragraph(str(idx), cell_center),
                    Paragraph(date_text, cell_center),
                    Paragraph("Pemasukan" if row["type"] == "income" else "Pengeluaran", cell_center),
                    Paragraph(_escape_pdf(row["category"]), cell),
                    Paragraph(_escape_pdf(row["description"]), cell),
                    Paragraph(_rupiah(row["income"]) if row["income"] else "-", cell_right),
                    Paragraph(_rupiah(row["expense"]) if row["expense"] else "-", cell_right),
                ]
            )
    else:
        table_data.append(["", "", "", "", Paragraph("Tidak ada transaksi pada periode ini.", cell_center), "", ""])

    table = Table(
        table_data,
        repeatRows=1,
        colWidths=[10 * mm, 24 * mm, 27 * mm, 34 * mm, 83 * mm, 32 * mm, 32 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17365D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7D1D9")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 4 * mm),
            Paragraph(
                "Sumber data: database keuangan tenant Anda pada Asisten UMKM Indonesia. "
                "Laporan ini dibuat otomatis dari transaksi berstatus posted.",
                small,
            ),
        ]
    )

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(12 * mm, 7 * mm, "Asisten UMKM Indonesia")
        canvas.drawRightString(page_size[0] - 12 * mm, 7 * mm, f"Halaman {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return str(path)


def _escape_pdf(text: Any) -> str:
    value = str(text or "")
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def remove_export(path: str | os.PathLike[str]) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass
