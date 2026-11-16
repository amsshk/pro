"""Download a business's report: CSV (orders), Excel (every table on its own sheet) or a ProAds-branded PDF.

`data` is built by the server: the Home dashboard for the period, the orders in it, and TikTok ad results.
No customer emails or phone numbers go into any file.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone

GREEN = "#5E9400"
INK = "#0A0A0A"
MUTED = "#6B6F66"
LINE = "#E3E6DD"
SOFT = "#F3F7EA"
LOGO = os.path.join(os.path.dirname(__file__), "web", "proads-logo.png")
PERIOD_DAYS = {"today": 1, "7": 7, "30": 30, "90": 90, "365": 365}
PERIOD_NAMES = {"today": "Today", "7": "Last 7 days", "30": "Last 30 days", "90": "Last 90 days", "365": "Last 12 months"}
ORDER_COLS = ["Date", "Order ID", "Came from", "Products", "Paid", "Refunded", "Kept", "Currency", "Payment source"]


def filename(business: str, period: str, ext: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (business or "business").lower()).strip("-") or "business"
    return f"proads-report-{safe}-{period}-{datetime.now(timezone.utc):%Y-%m-%d}.{ext}"


def period_label(data: dict) -> str:
    d = data.get("dashboard") or {}
    dates = d.get("dates") or []
    name = PERIOD_NAMES.get(str(d.get("period")), f"Last {d.get('days')} days")
    return f"{name} · {dates[0]} to {dates[-1]}" if dates else name


def _change(m: dict) -> str:
    c = m.get("change")
    return "" if c is None else f"{c * 100:+.0f}% vs previous"


def _money(v, cur: str) -> str:
    return "–" if v is None else f"{cur} {v:,.0f}"


def _summary_rows(data: dict) -> list[list]:
    d = data.get("dashboard") or {}
    rows = [[m["label"], m["value"], m["previous"], "" if m.get("change") is None else round(m["change"], 3)] for m in d.get("metrics") or []]
    if d.get("metrics"):
        rows.append(["Refund rate", d.get("refund_rate"), "", ""])
        if d.get("returning_rate") is not None:
            rows.append(["Returning customers", d.get("returning_rate"), "", ""])
    return rows


def _tiktok_rows(data: dict) -> list[list]:
    t = data.get("tiktok") or {}
    return [[a.get("campaign"), a.get("ad_name") or "", a.get("ad_id"), a.get("spend"), a.get("impressions"), a.get("clicks"),
             a.get("kept_orders"), a.get("kept"), a.get("cost_per_order"), (a.get("verdict") or {}).get("label", "")]
            for a in t.get("ads") or []]


TIKTOK_COLS = ["Campaign", "Ad", "Ad ID", "Spend", "Impressions", "Clicks", "Paid orders kept", "Money kept", "Cost per paid order", "Verdict"]


# ---------------------------------------------------------------- CSV
def to_csv(data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ORDER_COLS)
    for o in data.get("orders") or []:
        w.writerow([o[c] for c in ORDER_COLS])
    return ("﻿" + buf.getvalue()).encode("utf-8")           # BOM: Excel opens Arabic names correctly


# ---------------------------------------------------------------- Excel
def to_xlsx(data: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cur = data.get("currency", "AED")
    money_fmt = f'"{cur}" #,##0.00'
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=GREEN[1:])
    wb = Workbook()

    def sheet(ws, cols, rows, money=(), pct=(), start=1):
        for i, c in enumerate(cols, 1):
            cell = ws.cell(row=start, column=i, value=c)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center")
        for r, row in enumerate(rows, start + 1):
            for i, v in enumerate(row, 1):
                cell = ws.cell(row=r, column=i, value=v)
                if cols[i - 1] in money and isinstance(v, (int, float)):
                    cell.number_format = money_fmt
                if cols[i - 1] in pct and isinstance(v, (int, float)):
                    cell.number_format = "0.0%"
        for i, c in enumerate(cols, 1):
            longest = max([len(str(c))] + [len(str(row[i - 1] if row[i - 1] is not None else "")) for row in rows])
            ws.column_dimensions[get_column_letter(i)].width = min(max(12, longest + 2), 60)
        ws.freeze_panes = ws.cell(row=start + 1, column=1)

    ws = wb.active
    ws.title = "Summary"
    if os.path.exists(LOGO):
        img = XLImage(LOGO)
        img.width = img.height = 48
        ws.add_image(img, "A1")
    ws["B1"] = "ProAds sales report"
    ws["B1"].font = Font(bold=True, size=16)
    ws["B2"] = f"{data.get('business', '')} · {period_label(data)}"
    ws["B3"] = f"Made {datetime.now(timezone.utc):%d %b %Y %H:%M} UTC · pro.ezdevsoft.net"
    ws["B3"].font = Font(color="6B6F66", size=9)
    ws.row_dimensions[1].height = 24
    rows = _summary_rows(data)
    sheet(ws, ["Measure", "This period", "Previous period", "Change"], rows, start=5)
    for r, row in enumerate(rows, 6):
        label = row[0]
        if label in ("Refund rate", "Returning customers"):
            ws.cell(row=r, column=2).number_format = "0.0%"
        elif label != "Orders":
            ws.cell(row=r, column=2).number_format = money_fmt
            ws.cell(row=r, column=3).number_format = money_fmt
        ws.cell(row=r, column=4).number_format = "+0%;-0%;0%"
    ws.column_dimensions["A"].width = 24

    d = data.get("dashboard") or {}
    s = d.get("series") or {}
    daily = [[day, (s.get("sales") or {}).get("current", [0] * len(d.get("dates", [])))[i],
              (s.get("profit") or {}).get("current", [0] * len(d.get("dates", [])))[i],
              (s.get("orders") or {}).get("current", [0] * len(d.get("dates", [])))[i]] for i, day in enumerate(d.get("dates") or [])]
    sheet(wb.create_sheet("Daily"), ["Date", "Sales", "Real profit", "Orders"], daily, money=("Sales", "Real profit"))
    sheet(wb.create_sheet("Where sales came from"), ["Source", "Sales", "Orders"],
          [[x["source"], x["sales"], x["orders"]] for x in d.get("sources") or []], money=("Sales",))
    sheet(wb.create_sheet("Top products"), ["Product", "Sales", "Units"],
          [[x["product"], x["sales"], x["units"]] for x in d.get("products") or []], money=("Sales",))
    if (data.get("tiktok") or {}).get("ads"):
        sheet(wb.create_sheet("TikTok ads"), TIKTOK_COLS, _tiktok_rows(data), money=("Spend", "Money kept", "Cost per paid order"))
    sheet(wb.create_sheet("Orders"), ORDER_COLS, [[o[c] for c in ORDER_COLS] for o in data.get("orders") or []],
          money=("Paid", "Refunded", "Kept"))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------------------------------------------------------- PDF
def to_pdf(data: dict) -> bytes:
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

    cur = data.get("currency", "AED")
    d = data.get("dashboard") or {}
    green, ink, muted, line, soft = (colors.HexColor(c) for c in (GREEN, INK, MUTED, LINE, SOFT))
    st = {
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=ink),
        "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=13, textColor=muted),
        "right": ParagraphStyle("right", fontName="Helvetica", fontSize=9.5, leading=13, textColor=muted, alignment=TA_RIGHT),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=ink, spaceBefore=14, spaceAfter=6),
        "label": ParagraphStyle("label", fontName="Helvetica", fontSize=8.5, leading=11, textColor=muted),
        "big": ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=ink),
        "chg": ParagraphStyle("chg", fontName="Helvetica", fontSize=8.5, leading=11, textColor=green),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=11, textColor=ink),
        "note": ParagraphStyle("note", fontName="Helvetica", fontSize=8, leading=11, textColor=muted),
    }
    W = A4[0] - 36 * mm
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def footer(c, doc):
        c.saveState()
        c.setStrokeColor(line)
        c.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        c.setFont("Helvetica", 8)
        c.setFillColor(muted)
        c.drawString(18 * mm, 9.5 * mm, "Made with ProAds · pro.ezdevsoft.net")
        c.drawRightString(A4[0] - 18 * mm, 9.5 * mm, f"Page {doc.page}")
        c.restoreState()

    story = []
    logo = Image(LOGO, 13 * mm, 13 * mm) if os.path.exists(LOGO) else Paragraph("", st["sub"])
    brand = Table([[logo, Paragraph("<b>ProAds</b>", ParagraphStyle("b", fontName="Helvetica-Bold", fontSize=15, textColor=ink))]],
                  colWidths=[15 * mm, 40 * mm], style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    made = Paragraph(f"Sales report<br/>Made {datetime.now(timezone.utc):%d %b %Y}", st["right"])
    story.append(Table([[brand, made]], colWidths=[W * 0.5, W * 0.5],
                       style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(esc(data.get("business") or ""), st["h1"]))
    story.append(Paragraph(esc(period_label(data)) + ("" if d.get("live", True) else " · your sales data ends on " + esc(d.get("as_of", ""))), st["sub"]))
    story.append(Spacer(1, 6 * mm))

    metrics = d.get("metrics") or []
    if not metrics:
        story.append(Paragraph("No sales in ProAds yet, so there is nothing to report. Connect your store or upload your sales first.", st["cell"]))
    else:
        tiles = []
        for m in metrics:
            val = f"{m['value']:,.0f}" if m["key"] == "orders" else _money(m["value"], cur)
            tiles.append([Paragraph(esc(m["label"]), st["label"]), Paragraph(val, st["big"]), Paragraph(esc(_change(m)) or "&nbsp;", st["chg"])])
        tw = W / len(tiles)
        story.append(Table([[Table([[x] for x in t], style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                                             ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]) for t in tiles]],
                           colWidths=[tw] * len(tiles),
                           style=[("BACKGROUND", (0, 0), (-1, -1), soft), ("BOX", (0, 0), (-1, -1), 0.6, line),
                                  ("LINEAFTER", (0, 0), (-2, -1), 0.6, line), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                  ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        extra = [f"Refund rate {d.get('refund_rate', 0) * 100:.1f}%"]
        if d.get("returning_rate") is not None:
            extra.append(f"returning customers {d['returning_rate'] * 100:.0f}% of orders")
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(" · ".join(extra) + ". Real profit is sales minus refunds and product costs where ProAds knows them.", st["note"]))

        # daily sales chart
        sales = ((d.get("series") or {}).get("sales") or {}).get("current") or []
        dates = d.get("dates") or []
        if len(sales) > 1:
            H = 46 * mm
            dr = Drawing(W, H + 8 * mm)
            top = max(sales) or 1
            gap = W / len(sales)
            bw = max(gap * 0.68, 0.8)
            for i, v in enumerate(sales):
                h_ = (v / top) * H
                dr.add(Rect(i * gap + (gap - bw) / 2, 6 * mm, bw, max(h_, 0.4 if v else 0), fillColor=green, strokeColor=None))
            dr.add(Line(0, 6 * mm, W, 6 * mm, strokeColor=line, strokeWidth=0.6))
            for i in sorted({0, len(dates) // 2, len(dates) - 1}):
                anchor = "start" if i == 0 else "end" if i == len(dates) - 1 else "middle"
                dr.add(String(i * gap + gap / 2 if anchor == "middle" else (0 if i == 0 else W), 1.5 * mm,
                              datetime.fromisoformat(dates[i]).strftime("%d %b"), fontName="Helvetica", fontSize=7.5, fillColor=muted, textAnchor=anchor))
            dr.add(String(0, H + 7 * mm, f"Highest day {_money(top, cur)}", fontName="Helvetica", fontSize=7.5, fillColor=muted))
            story.append(KeepTogether([Paragraph("Sales per day", st["h2"]), dr]))

    def table(title, cols, rows, widths, right_from=1, note=None):
        if not rows:
            return
        body = [[Paragraph(f"<b>{esc(c)}</b>", st["label"]) for c in cols]] + \
               [[Paragraph(esc(v), st["cell"]) for v in r] for r in rows]
        t = Table(body, colWidths=[W * w for w in widths], repeatRows=1)
        style = [("LINEBELOW", (0, 0), (-1, 0), 0.8, green), ("LINEBELOW", (0, 1), (-1, -1), 0.4, line),
                 ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                 ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        for r in range(len(body)):
            for c in range(right_from, len(cols)):
                body[r][c].style = ParagraphStyle("r", parent=body[r][c].style, alignment=TA_RIGHT)
        t.setStyle(TableStyle(style))
        block = [Paragraph(esc(title), st["h2"]), t]
        if note:
            block += [Spacer(1, 1.5 * mm), Paragraph(esc(note), st["note"])]
        story.append(KeepTogether(block) if len(rows) <= 25 else block[0])
        if len(rows) > 25:
            story.extend(block[1:])

    table("Where sales came from", ["Source", "Orders", "Sales"],
          [[x["source"], f"{x['orders']:,}", _money(x["sales"], cur)] for x in d.get("sources") or []], [0.6, 0.15, 0.25])
    table("Top products", ["Product", "Units", "Sales"],
          [[x["product"], f"{x['units']:,}", _money(x["sales"], cur)] for x in d.get("products") or []], [0.6, 0.15, 0.25])
    t = data.get("tiktok") or {}
    if t.get("ads"):
        tc = t.get("currency", cur)
        table("TikTok ads", ["Campaign / ad", "Spend", "Clicks", "Paid orders", "Kept", "Cost per order", "Verdict"],
              [[f"{a.get('campaign') or ''} · {a.get('ad_name') or a.get('ad_id')}", _money(a.get("spend"), tc), f"{a.get('clicks') or 0:,}",
                f"{a.get('kept_orders') or 0:,}", _money(a.get("kept"), tc), _money(a.get("cost_per_order"), tc),
                (a.get("verdict") or {}).get("label", "–")] for a in t["ads"][:25]],
              [0.32, 0.11, 0.09, 0.1, 0.12, 0.12, 0.14],
              note="Paid orders and money kept are verified sales after refunds, matched to each ad by the ProAds tracking link. "
                   "“promote_” boosts can't carry the link, so their sales can't be matched.")
    n = len(data.get("orders") or [])
    if n:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(f"{n:,} orders in this period. Every order is in the Excel and CSV downloads.", st["note"]))

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=20 * mm,
                            title=f"ProAds report · {data.get('business', '')}", author="ProAds")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
