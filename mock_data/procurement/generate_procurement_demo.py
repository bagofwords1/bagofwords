#!/usr/bin/env python3
"""
Mock procurement demo generator.

Creates:
  - pdfs/            10 purchase-order / order-form PDFs (varied designs)
  - procurement.db   SQLite database (vendors, agreements, POs, line items,
                     invoices, documents) with file references to the PDFs

Run from this directory:  python3 generate_procurement_demo.py
All data is fictional and deterministic (no randomness).
"""

import os
import sqlite3

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(BASE_DIR, "pdfs")
DB_PATH = os.path.join(BASE_DIR, "procurement.db")

BUYER = {
    "name": "Acme Analytics Inc.",
    "address": "500 Market Street, Suite 1200",
    "city": "San Francisco, CA 94105",
    "phone": "+1 (415) 555-0142",
    "email": "procurement@acmeanalytics.example",
    "ship_to": "Acme Analytics Inc. — IT Receiving, 500 Market Street, Dock B, San Francisco, CA 94105",
}

# --------------------------------------------------------------------------
# Dataset (fictional)
# --------------------------------------------------------------------------

VENDORS = [
    # key, name, category, contact_name, contact_email, phone, address, website
    ("dell", "Dell Technologies", "Hardware", "Maria Gonzales", "maria.gonzales@dell.example", "+1 (512) 555-0180", "One Dell Way, Round Rock, TX 78682", "dell.example"),
    ("cdw", "CDW Corporation", "IT Reseller", "James O'Neill", "james.oneill@cdw.example", "+1 (847) 555-0121", "200 N Milwaukee Ave, Vernon Hills, IL 60061", "cdw.example"),
    ("msft", "Microsoft Corporation", "Software", "Priya Raman", "priya.raman@microsoft.example", "+1 (425) 555-0164", "One Microsoft Way, Redmond, WA 98052", "microsoft.example"),
    ("aws", "Amazon Web Services", "Cloud", "Derek Holt", "derek.holt@aws.example", "+1 (206) 555-0117", "410 Terry Ave N, Seattle, WA 98109", "aws.example"),
    ("atlassian", "Atlassian Pty Ltd", "SaaS", "Sophie Nguyen", "sophie.nguyen@atlassian.example", "+61 2 5550 1830", "341 George Street, Sydney NSW 2000, Australia", "atlassian.example"),
    ("cisco", "Cisco Systems (via Ingram Micro)", "Networking", "Robert Chen", "robert.chen@ingram.example", "+1 (714) 555-0195", "3351 Michelson Dr, Irvine, CA 92612", "cisco.example"),
    ("okta", "Okta Inc.", "Identity / SaaS", "Hannah Park", "hannah.park@okta.example", "+1 (415) 555-0136", "100 First Street, San Francisco, CA 94105", "okta.example"),
    ("shi", "SHI International", "IT Reseller", "Luis Alvarez", "luis.alvarez@shi.example", "+1 (732) 555-0158", "290 Davidson Ave, Somerset, NJ 08873", "shi.example"),
    ("lenovo", "Lenovo Global Technology", "Hardware", "Anke Fischer", "anke.fischer@lenovo.example", "+1 (919) 555-0173", "8001 Development Dr, Morrisville, NC 27560", "lenovo.example"),
    ("zoom", "Zoom Video Communications", "SaaS", "Tom Becker", "tom.becker@zoom.example", "+1 (888) 555-0109", "55 Almaden Blvd, San Jose, CA 95113", "zoom.example"),
]

AGREEMENTS = [
    # key, vendor_key, number, title, type, start, end, renewal_date,
    # auto_renew, notice_days, annual_value, status
    ("agr_dell", "dell", "AGR-2023-001", "Dell Master Supply Agreement", "MSA", "2023-08-01", "2026-07-31", "2026-07-31", 1, 60, 240000.00, "active"),
    ("agr_cdw", "cdw", "AGR-2024-008", "CDW Preferred Pricing Agreement", "Pricing Agreement", "2024-03-01", "2027-02-28", "2027-02-28", 1, 90, 150000.00, "active"),
    ("agr_msft", "msft", "AGR-2024-002", "Microsoft Enterprise Agreement (EA)", "Enterprise Agreement", "2024-09-01", "2027-08-31", "2026-09-01", 0, 30, 385000.00, "active"),
    ("agr_aws", "aws", "AGR-2025-003", "AWS Enterprise Discount Program", "Cloud Commit", "2025-01-15", "2028-01-14", "2028-01-14", 0, 90, 600000.00, "active"),
    ("agr_atl", "atlassian", "AGR-2024-004", "Atlassian Cloud Premium Subscription", "SaaS Subscription", "2024-11-01", "2026-10-31", "2026-10-31", 1, 30, 68000.00, "active"),
    ("agr_cisco", "cisco", "AGR-2023-005", "Cisco SmartNet Support & Resale Agreement", "Support Agreement", "2023-06-15", "2026-06-14", "2026-06-14", 1, 60, 95000.00, "renewal_due"),
    ("agr_okta", "okta", "AGR-2025-006", "Okta Workforce Identity Subscription", "SaaS Subscription", "2025-04-01", "2026-03-31", "2026-03-31", 1, 45, 88400.00, "renewed"),
    ("agr_shi", "shi", "AGR-2023-010", "Adobe VIP Licensing via SHI", "VIP Licensing", "2023-10-01", "2026-09-30", "2026-09-30", 1, 30, 54000.00, "active"),
    ("agr_lenovo", "lenovo", "AGR-2025-009", "Lenovo Device Purchase Framework", "Framework Agreement", "2025-02-01", "2027-01-31", "2027-01-31", 0, 60, 180000.00, "active"),
    ("agr_zoom", "zoom", "AGR-2024-007", "Zoom One Business Annual Plan", "SaaS Subscription", "2024-08-15", "2026-08-14", "2026-08-14", 1, 30, 32400.00, "active"),
]

# Each PO: key, vendor_key, agreement_key, po_number, doc_title, order_date,
# delivery_date, payment_terms, requested_by, department, status, tax_rate,
# style, filename, notes, line items [(sku, description, category, qty, unit_price)]
POS = [
    (
        "po1", "dell", "agr_dell", "PO-2025-0841", "PURCHASE ORDER",
        "2025-08-18", "2025-09-05", "Net 30", "Dana Whitfield", "IT Operations",
        "received", 0.0850, "band_blue", "PO-2025-0841_Dell.pdf",
        "Laptop refresh — engineering team, wave 1 of 3. Ref agreement AGR-2023-001.",
        [
            ("LAT-5450-I7", "Dell Latitude 5450 — i7/32GB/1TB, 3yr ProSupport", "Laptops", 25, 1849.00),
            ("DOCK-WD22TB4", "Dell WD22TB4 Thunderbolt Dock", "Accessories", 25, 289.00),
            ("MON-P2723DE", "Dell P2723DE 27\" QHD USB-C Monitor", "Monitors", 25, 419.00),
            ("PS-3Y-UPG", "ProSupport Plus upgrade, 3 years", "Support", 25, 129.00),
        ],
    ),
    (
        "po2", "cdw", "agr_cdw", "PO-2025-0902", "PURCHASE ORDER",
        "2025-09-22", "2025-10-10", "Net 45", "Marcus Lee", "Infrastructure",
        "received", 0.0000, "minimal_gray", "PO-2025-0902_CDW.pdf",
        "Wireless network expansion — HQ floors 10-12.",
        [
            ("AP-CW9166I", "Cisco CW9166I Wi-Fi 6E Access Point", "Networking", 18, 1095.00),
            ("SW-C9300L-24", "Cisco Catalyst 9300L 24-port PoE+ Switch", "Networking", 4, 4250.00),
            ("CBL-CAT6A-50", "Cat6A patch cables, 50-pack", "Cabling", 6, 189.00),
            ("SVC-INSTALL", "Professional installation services (per day)", "Services", 5, 1600.00),
        ],
    ),
    (
        "po3", "msft", "agr_msft", "OF-2025-1104", "ORDER FORM",
        "2025-11-04", "2025-12-01", "Net 30", "Dana Whitfield", "IT Operations",
        "invoiced", 0.0000, "form_boxed", "OF-2025-1104_Microsoft.pdf",
        "Annual EA true-up — Microsoft 365 & Azure AD P2. Ref agreement AGR-2024-002; next anniversary 2026-09-01.",
        [
            ("M365-E5", "Microsoft 365 E5 license (annual, per user)", "Software Licenses", 320, 684.00),
            ("M365-E3", "Microsoft 365 E3 license (annual, per user)", "Software Licenses", 150, 432.00),
            ("AADP2", "Entra ID P2 add-on (annual, per user)", "Software Licenses", 120, 108.00),
            ("PBI-PRO", "Power BI Pro (annual, per user)", "Software Licenses", 85, 120.00),
        ],
    ),
    (
        "po4", "aws", "agr_aws", "PO-2026-0117", "PURCHASE ORDER",
        "2026-01-17", "2026-02-01", "Net 30", "Marcus Lee", "Infrastructure",
        "invoiced", 0.0000, "dark_header", "PO-2026-0117_AWS.pdf",
        "Q1 2026 committed cloud spend under EDP AGR-2025-003.",
        [
            ("EDP-Q1-COMMIT", "EDP quarterly commit — compute & storage", "Cloud", 1, 135000.00),
            ("SUP-ENT", "Enterprise Support (quarterly)", "Support", 1, 12500.00),
        ],
    ),
    (
        "po5", "atlassian", "agr_atl", "OF-2025-1119", "ORDER FORM",
        "2025-11-19", "2025-11-19", "Due on receipt", "Sara Kim", "Engineering",
        "paid", 0.0000, "accent_side", "OF-2025-1119_Atlassian.pdf",
        "Cloud Premium renewal + seat expansion. Auto-renews 2026-10-31 per AGR-2024-004.",
        [
            ("JIRA-PREM-400", "Jira Software Cloud Premium — 400 users (annual)", "SaaS", 1, 33600.00),
            ("CONF-PREM-400", "Confluence Cloud Premium — 400 users (annual)", "SaaS", 1, 22400.00),
            ("JSM-PREM-50", "Jira Service Management Premium — 50 agents (annual)", "SaaS", 1, 11800.00),
        ],
    ),
    (
        "po6", "cisco", "agr_cisco", "PO-2026-0233", "PURCHASE ORDER",
        "2026-02-23", "2026-03-20", "Net 60", "Marcus Lee", "Infrastructure",
        "approved", 0.0875, "dense_landscape", "PO-2026-0233_Cisco.pdf",
        "Core switch refresh + SmartNet co-term to 2026-06-14 (AGR-2023-005 renewal due).",
        [
            ("C9500-24Y4C", "Catalyst 9500 24-port 25G core switch", "Networking", 2, 21500.00),
            ("C9500-NW-A", "Network Advantage license, 9500", "Software Licenses", 2, 3400.00),
            ("SNT-8X5XNBD", "SmartNet 8x5xNBD co-term (per chassis, 12 mo)", "Support", 2, 1980.00),
            ("SFP-25G-SR-S", "25G SFP28 SR transceiver", "Networking", 16, 310.00),
            ("PWR-C4-950WAC", "950W AC power supply, spare", "Networking", 2, 745.00),
            ("STACK-T1-1M", "StackWise cable 1m", "Networking", 4, 95.00),
        ],
    ),
    (
        "po7", "okta", "agr_okta", "OF-2026-0301", "SUBSCRIPTION ORDER FORM",
        "2026-03-01", "2026-04-01", "Net 30", "Dana Whitfield", "Security",
        "invoiced", 0.0000, "form_saas", "OF-2026-0301_Okta.pdf",
        "12-month renewal of Workforce Identity, term 2026-04-01 to 2027-03-31 (AGR-2025-006).",
        [
            ("OKTA-SSO", "Single Sign-On (per user / month x 12)", "SaaS", 520, 24.00),
            ("OKTA-MFA", "Adaptive MFA (per user / month x 12)", "SaaS", 520, 36.00),
            ("OKTA-LCM", "Lifecycle Management (per user / month x 12)", "SaaS", 520, 48.00),
            ("OKTA-SUPP", "Premier Support (annual)", "Support", 1, 8500.00),
        ],
    ),
    (
        "po8", "shi", "agr_shi", "PO-2026-0412", "PURCHASE ORDER",
        "2026-04-12", "2026-04-20", "Net 30", "Sara Kim", "Design",
        "invoiced", 0.0625, "serif_classic", "PO-2026-0412_SHI.pdf",
        "Adobe VIP renewal via SHI (AGR-2023-010), VIP anniversary 2026-09-30.",
        [
            ("ADB-CC-ALL", "Adobe Creative Cloud All Apps (VIP, annual, per seat)", "Software Licenses", 45, 899.88),
            ("ADB-ACROPRO", "Adobe Acrobat Pro DC (VIP, annual, per seat)", "Software Licenses", 60, 179.88),
            ("ADB-STOCK", "Adobe Stock 40 assets/mo (annual)", "Software Licenses", 10, 359.88),
        ],
    ),
    (
        "po9", "lenovo", "agr_lenovo", "PO-2026-0508", "PURCHASE ORDER",
        "2026-05-08", "2026-06-02", "Net 30", "Dana Whitfield", "IT Operations",
        "approved", 0.0850, "two_column", "PO-2026-0508_Lenovo.pdf",
        "New-hire hardware kits, H2 2026 intake, under framework AGR-2025-009.",
        [
            ("TP-X1C-G12", "ThinkPad X1 Carbon Gen 12 — Ultra 7/32GB/1TB", "Laptops", 20, 2149.00),
            ("TB-DOCK-40B0", "ThinkPad Thunderbolt 4 Dock", "Accessories", 20, 269.00),
            ("MON-T27H", "ThinkVision T27h-30 27\" QHD USB-C", "Monitors", 20, 379.00),
            ("KB-TRACK-II", "ThinkPad TrackPoint Keyboard II", "Accessories", 20, 99.00),
            ("PREM-3Y", "Premier Support, 3 years", "Support", 20, 189.00),
        ],
    ),
    (
        "po10", "zoom", "agr_zoom", "OF-2026-0615", "RENEWAL ORDER",
        "2026-06-15", "2026-08-15", "Due on receipt", "Tom Osei", "Workplace",
        "pending_approval", 0.0000, "receipt", "OF-2026-0615_Zoom.pdf",
        "Annual renewal — Zoom One Business + Webinar 1000. Renewal date 2026-08-14 (AGR-2024-007).",
        [
            ("ZM-ONE-BIZ", "Zoom One Business (annual, per host)", "SaaS", 140, 199.90),
            ("ZM-WEB-1000", "Zoom Webinar 1000 attendees (annual)", "SaaS", 3, 3400.00),
            ("ZM-ROOMS", "Zoom Rooms license (annual, per room)", "SaaS", 12, 499.00),
        ],
    ),
]

# invoices: po_key, invoice_number, invoice_date, due_date, fraction of PO
# total (fractions per PO must sum to <= 1), status, paid_date, payment_method
INVOICES = [
    ("po1", "INV-DL-58201", "2025-09-06", "2025-10-06", 1.00, "paid", "2025-10-01", "ACH"),
    ("po2", "INV-CDW-77410", "2025-10-12", "2025-11-26", 0.60, "paid", "2025-11-20", "ACH"),
    ("po2", "INV-CDW-77592", "2025-11-02", "2025-12-17", 0.40, "paid", "2025-12-15", "ACH"),
    ("po3", "INV-MS-330184", "2025-12-01", "2025-12-31", 1.00, "paid", "2025-12-29", "Wire"),
    ("po4", "INV-AWS-Q1-2026", "2026-02-01", "2026-03-03", 1.00, "paid", "2026-02-27", "Wire"),
    ("po5", "INV-ATL-90233", "2025-11-19", "2025-11-19", 1.00, "paid", "2025-11-19", "Credit Card"),
    ("po6", "INV-IM-44127", "2026-03-25", "2026-05-24", 0.50, "pending", None, None),
    ("po7", "INV-OKTA-2026-118", "2026-04-01", "2026-05-01", 1.00, "paid", "2026-04-28", "ACH"),
    ("po8", "INV-SHI-660412", "2026-04-21", "2026-05-21", 1.00, "overdue", None, None),
    ("po9", "INV-LEN-30518", "2026-06-03", "2026-07-03", 0.50, "pending", None, None),
]


def money(x):
    return f"${x:,.2f}"


def po_totals(po):
    items = po[15]
    subtotal = round(sum(q * p for _, _, _, q, p in items), 2)
    tax = round(subtotal * po[11], 2)
    return subtotal, tax, round(subtotal + tax, 2)


def vendor_by_key(key):
    return next(v for v in VENDORS if v[0] == key)


def agreement_by_key(key):
    return next(a for a in AGREEMENTS if a[0] == key)


# --------------------------------------------------------------------------
# PDF rendering — shared platypus pieces
# --------------------------------------------------------------------------

def para(text, font="Helvetica", size=9, color=colors.black, leading=None, bold=False, align=0):
    return Paragraph(
        text,
        ParagraphStyle(
            "p", fontName=(font + "-Bold") if bold and "-" not in font else font,
            fontSize=size, textColor=color, leading=leading or size * 1.35, alignment=align,
        ),
    )


def items_table_data(po, font, size=8.5):
    head = ["#", "SKU", "Description", "Qty", "Unit Price", "Amount"]
    rows = [head]
    for i, (sku, desc, _cat, qty, price) in enumerate(po[15], 1):
        rows.append([str(i), sku, para(desc, font=font, size=size), str(qty), money(price), money(qty * price)])
    return rows


def totals_rows(po):
    subtotal, tax, total = po_totals(po)
    rows = [("Subtotal", money(subtotal))]
    if tax:
        rows.append((f"Sales Tax ({po[11]*100:.2f}%)", money(tax)))
    rows.append(("TOTAL", money(total)))
    return rows


def meta_block(po, agreement, extra=()):
    data = [
        ("PO Number", po[3]),
        ("Order Date", po[5]),
        ("Delivery By", po[6]),
        ("Payment Terms", po[7]),
        ("Agreement Ref", agreement[2]),
        ("Renewal Date", agreement[7]),
    ]
    data.extend(extra)
    return data


def std_addresses(vendor, font="Helvetica", size=8.5):
    v = para(
        f"<b>VENDOR</b><br/>{vendor[1]}<br/>{vendor[6]}<br/>Attn: {vendor[3]}<br/>{vendor[4]}",
        font=font, size=size,
    )
    b = para(
        f"<b>BILL TO</b><br/>{BUYER['name']}<br/>{BUYER['address']}<br/>{BUYER['city']}<br/>{BUYER['email']}",
        font=font, size=size,
    )
    s = para(f"<b>SHIP TO</b><br/>{BUYER['ship_to']}", font=font, size=size)
    return v, b, s


def build_platypus(path, story, pagesize=LETTER, margins=0.65):
    doc = SimpleDocTemplate(
        path, pagesize=pagesize,
        leftMargin=margins * inch, rightMargin=margins * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=os.path.splitext(os.path.basename(path))[0],
    )
    doc.build(story)


# ---- style 1: colored band header, grid table (classic corporate) --------

def render_band(po, vendor, agreement, path, accent):
    font = "Helvetica"
    story = []
    band = Table(
        [[para(f"<b>{vendor[1]}</b>", size=15, color=colors.white),
          para(f"<b>{po[4]}</b><br/>{po[3]}", size=12, color=colors.white, align=2)]],
        colWidths=[4.4 * inch, 2.8 * inch],
    )
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(band)
    story.append(Spacer(1, 14))

    v, b, s = std_addresses(vendor)
    meta = meta_block(po, agreement)
    meta_t = Table([[para(f"<b>{k}:</b>", size=8), para(str(val), size=8)] for k, val in meta],
                   colWidths=[1.0 * inch, 1.4 * inch])
    meta_t.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2)]))
    top = Table([[v, b, meta_t]], colWidths=[2.5 * inch, 2.3 * inch, 2.4 * inch])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(top)
    story.append(Spacer(1, 8))
    story.append(para(s.text if hasattr(s, "text") else "", size=8.5))
    story.append(Spacer(1, 12))

    t = Table(items_table_data(po, font), colWidths=[0.35 * inch, 1.15 * inch, 3.15 * inch, 0.5 * inch, 1.0 * inch, 1.05 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9aa5b1")),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    tot = Table([[k, v2] for k, v2 in totals_rows(po)], colWidths=[1.6 * inch, 1.2 * inch], hAlign="RIGHT")
    tot.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, accent),
        ("TEXTCOLOR", (0, -1), (-1, -1), accent),
    ]))
    story.append(tot)
    story.append(Spacer(1, 18))
    story.append(para(f"<b>Notes:</b> {po[14]}", size=8, color=colors.HexColor("#444444")))
    story.append(Spacer(1, 24))
    sig = Table([[para("_______________________<br/>Authorized by (Buyer)", size=8),
                  para("_______________________<br/>Accepted by (Vendor)", size=8)]],
                colWidths=[3.0 * inch, 3.0 * inch])
    story.append(sig)
    build_platypus(path, story)


# ---- style 2: minimalist, hairlines + zebra rows --------------------------

def render_minimal(po, vendor, agreement, path):
    font = "Helvetica"
    gray = colors.HexColor("#6b7280")
    story = [
        para("PURCHASE ORDER", size=22, color=colors.HexColor("#111827")),
        Spacer(1, 2),
        para(f"{po[3]}  ·  issued {po[5]}  ·  delivery by {po[6]}", size=9, color=gray),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#111827")),
        Spacer(1, 12),
    ]
    v, b, _ = std_addresses(vendor)
    right = para(
        f"<b>TERMS</b><br/>Payment: {po[7]}<br/>Requested by: {po[8]} ({po[9]})<br/>"
        f"Agreement: {agreement[2]}<br/>Agreement renews: {agreement[7]}",
        size=8.5,
    )
    story.append(Table([[v, b, right]], colWidths=[2.4 * inch, 2.4 * inch, 2.4 * inch],
                       style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])))
    story.append(Spacer(1, 16))

    t = Table(items_table_data(po, font), colWidths=[0.35 * inch, 1.2 * inch, 3.2 * inch, 0.5 * inch, 0.95 * inch, 1.0 * inch])
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#111827")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for r in range(2, len(po[15]) + 1, 2):
        style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f3f4f6")))
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Spacer(1, 10))
    tot = Table([[k, v2] for k, v2 in totals_rows(po)], colWidths=[1.8 * inch, 1.2 * inch], hAlign="RIGHT")
    tot.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 11),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#111827")),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]))
    story.append(tot)
    story.append(Spacer(1, 20))
    story.append(para(po[14], size=8, color=gray))
    build_platypus(path, story)


# ---- style 3: boxed order form (Microsoft EA) ------------------------------

def render_form_boxed(po, vendor, agreement, path):
    c = rl_canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER
    ink = colors.HexColor("#1f2937")
    accent = colors.HexColor("#0f6cbd")
    x0, x1 = 0.6 * inch, w - 0.6 * inch

    c.setFillColor(accent)
    c.rect(x0, h - 1.05 * inch, x1 - x0, 0.45 * inch, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(x0 + 10, h - 0.92 * inch, f"{po[4]} — {vendor[1]}")
    c.setFont("Helvetica", 9)
    c.drawRightString(x1 - 10, h - 0.90 * inch, f"Form No. {po[3]}")

    def field_box(x, y, wd, ht, label, value, vsize=9, vfont="Helvetica"):
        c.setStrokeColor(colors.HexColor("#9ca3af"))
        c.setLineWidth(0.7)
        c.rect(x, y - ht, wd, ht, stroke=1, fill=0)
        c.setFillColor(colors.HexColor("#6b7280"))
        c.setFont("Helvetica", 6.3)
        c.drawString(x + 4, y - 9, label.upper())
        c.setFillColor(ink)
        c.setFont(vfont, vsize)
        c.drawString(x + 4, y - ht + 6, value)

    y = h - 1.35 * inch
    bw = (x1 - x0 - 20) / 3
    field_box(x0, y, bw, 34, "Customer", BUYER["name"])
    field_box(x0 + bw + 10, y, bw, 34, "Enrollment / Agreement", f"{agreement[2]} ({agreement[4]})")
    field_box(x0 + 2 * (bw + 10), y, bw, 34, "Order Date", po[5])
    y -= 44
    field_box(x0, y, bw, 34, "Agreement Term", f"{agreement[5]} to {agreement[6]}")
    field_box(x0 + bw + 10, y, bw, 34, "Next Anniversary / Renewal", agreement[7], vfont="Helvetica-Bold")
    field_box(x0 + 2 * (bw + 10), y, bw, 34, "Payment Terms", po[7])
    y -= 44
    field_box(x0, y, 2 * bw + 10, 34, "Bill To", f"{BUYER['name']}, {BUYER['address']}, {BUYER['city']}")
    field_box(x0 + 2 * (bw + 10), y, bw, 34, "Requested By", f"{po[8]} — {po[9]}")

    y -= 58
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, y, "LICENSED PRODUCTS")
    y -= 6
    cols = [x0, x0 + 0.95 * inch, x0 + 4.35 * inch, x0 + 4.95 * inch, x1 - 1.05 * inch, x1]
    c.setFillColor(colors.HexColor("#e5effa"))
    c.rect(x0, y - 16, x1 - x0, 16, stroke=0, fill=1)
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 7.5)
    for cx, htxt in zip(cols, ["PART NO.", "PRODUCT", "QTY", "UNIT PRICE", "EXTENDED"]):
        c.drawString(cx + 3, y - 12, htxt)
    y -= 16
    c.setFont("Helvetica", 8.2)
    for sku, desc, _cat, qty, price in po[15]:
        y -= 16
        c.setStrokeColor(colors.HexColor("#d1d5db"))
        c.line(x0, y, x1, y)
        c.setFillColor(ink)
        c.drawString(cols[0] + 3, y + 4, sku)
        c.drawString(cols[1] + 3, y + 4, desc[:64])
        c.drawRightString(cols[3] - 6, y + 4, str(qty))
        c.drawRightString(cols[4] - 4, y + 4, money(price))
        c.drawRightString(cols[5] - 3, y + 4, money(qty * price))
    c.setStrokeColor(colors.HexColor("#9ca3af"))
    c.rect(x0, y, x1 - x0, 16 * (len(po[15]) + 1) + 16, stroke=1, fill=0)

    subtotal, tax, total = po_totals(po)
    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(accent)
    c.drawRightString(x1 - 3, y, f"TOTAL DUE: {money(total)}")
    c.setFillColor(ink)
    c.setFont("Helvetica", 7.5)
    y -= 26
    c.drawString(x0, y, "Notes: " + po[14])
    y -= 40
    c.setStrokeColor(ink)
    for label, sx in (("Customer signature", x0), ("Vendor signature", x0 + 3.5 * inch)):
        c.line(sx, y, sx + 2.6 * inch, y)
        c.setFont("Helvetica", 7)
        c.drawString(sx, y - 10, label + " / date")
    c.setFont("Helvetica-Oblique", 6.5)
    c.setFillColor(colors.HexColor("#9ca3af"))
    c.drawString(x0, 0.45 * inch, "This order form is governed by the referenced enterprise agreement. Fictional document for demo purposes.")
    c.save()


# ---- style 4: dark header invoice-like (AWS) -------------------------------

def render_dark_header(po, vendor, agreement, path):
    c = rl_canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER
    dark = colors.HexColor("#161e2d")
    orange = colors.HexColor("#ec7211")
    c.setFillColor(dark)
    c.rect(0, h - 1.5 * inch, w, 1.5 * inch, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(0.7 * inch, h - 0.75 * inch, vendor[1])
    c.setFillColor(orange)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.7 * inch, h - 1.05 * inch, po[4])
    c.setFillColor(colors.white)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 0.7 * inch, h - 0.65 * inch, f"PO No: {po[3]}")
    c.drawRightString(w - 0.7 * inch, h - 0.82 * inch, f"Date: {po[5]}")
    c.drawRightString(w - 0.7 * inch, h - 0.99 * inch, f"Terms: {po[7]}")
    c.drawRightString(w - 0.7 * inch, h - 1.16 * inch, f"Agreement: {agreement[2]}")

    y = h - 1.95 * inch
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(0.7 * inch, y, "SOLD BY")
    c.drawString(3.4 * inch, y, "BILL TO")
    c.setFont("Helvetica", 8.5)
    for i, line in enumerate([vendor[1], vendor[6], f"Attn: {vendor[3]}"]):
        c.drawString(0.7 * inch, y - 13 - i * 12, line)
    for i, line in enumerate([BUYER["name"], BUYER["address"], BUYER["city"]]):
        c.drawString(3.4 * inch, y - 13 - i * 12, line)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(6.0 * inch, y, "COMMIT PERIOD")
    c.setFont("Helvetica", 8.5)
    c.drawString(6.0 * inch, y - 13, f"{agreement[5]} –")
    c.drawString(6.0 * inch, y - 25, agreement[6])
    c.drawString(6.0 * inch, y - 37, f"Renews: {agreement[7]}")

    y -= 70
    c.setFillColor(orange)
    c.rect(0.7 * inch, y - 4, w - 1.4 * inch, 2, stroke=0, fill=1)
    y -= 22
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica-Bold", 8)
    c.drawString(0.72 * inch, y, "CODE")
    c.drawString(2.0 * inch, y, "DESCRIPTION")
    c.drawRightString(5.9 * inch, y, "QTY")
    c.drawRightString(6.9 * inch, y, "UNIT")
    c.drawRightString(w - 0.72 * inch, y, "AMOUNT")
    c.setFont("Helvetica", 9)
    for sku, desc, _cat, qty, price in po[15]:
        y -= 20
        c.drawString(0.72 * inch, y, sku)
        c.drawString(2.0 * inch, y, desc)
        c.drawRightString(5.9 * inch, y, str(qty))
        c.drawRightString(6.9 * inch, y, money(price))
        c.drawRightString(w - 0.72 * inch, y, money(qty * price))
        c.setStrokeColor(colors.HexColor("#e5e7eb"))
        c.line(0.7 * inch, y - 6, w - 0.7 * inch, y - 6)

    subtotal, tax, total = po_totals(po)
    y -= 40
    c.setFillColor(dark)
    c.roundRect(w - 3.3 * inch, y - 34, 2.6 * inch, 44, 6, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica", 8)
    c.drawString(w - 3.15 * inch, y - 4, "TOTAL COMMITTED SPEND")
    c.setFont("Helvetica-Bold", 15)
    c.drawString(w - 3.15 * inch, y - 24, money(total))
    c.setFillColor(colors.HexColor("#374151"))
    c.setFont("Helvetica", 7.5)
    c.drawString(0.7 * inch, y - 60, "Notes: " + po[14])
    c.setFont("Helvetica-Oblique", 6.5)
    c.setFillColor(colors.HexColor("#9ca3af"))
    c.drawString(0.7 * inch, 0.45 * inch, "Fictional purchase order generated for demo purposes.")
    c.save()


# ---- style 5: left accent sidebar (Atlassian) ------------------------------

def render_accent_side(po, vendor, agreement, path):
    c = rl_canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER
    blue = colors.HexColor("#1868db")
    navy = colors.HexColor("#101214")
    c.setFillColor(blue)
    c.rect(0, 0, 0.35 * inch, h, stroke=0, fill=1)

    x0 = 0.75 * inch
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(x0, h - 0.9 * inch, po[4])
    c.setFillColor(blue)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x0, h - 1.15 * inch, vendor[1])
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont("Helvetica", 9)
    c.drawString(x0, h - 1.35 * inch, f"{po[3]}  ·  {po[5]}")

    c.setFillColor(colors.HexColor("#f0f4ff"))
    c.roundRect(w - 3.1 * inch, h - 1.62 * inch, 2.4 * inch, 1.05 * inch, 8, stroke=0, fill=1)
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(w - 2.95 * inch, h - 0.82 * inch, "SUBSCRIPTION TERM")
    c.setFont("Helvetica", 8.5)
    c.drawString(w - 2.95 * inch, h - 0.98 * inch, f"{agreement[5]} → {agreement[6]}")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(w - 2.95 * inch, h - 1.18 * inch, "AUTO-RENEWS")
    c.setFont("Helvetica", 8.5)
    c.drawString(w - 2.95 * inch, h - 1.34 * inch, f"{agreement[7]} (per {agreement[2]})")
    c.setFont("Helvetica", 7.5)
    c.drawString(w - 2.95 * inch, h - 1.52 * inch, f"Payment: {po[7]}")

    y = h - 2.05 * inch
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x0, y, "CUSTOMER")
    c.setFont("Helvetica", 8.5)
    for i, line in enumerate([BUYER["name"], BUYER["address"], BUYER["city"], f"Technical contact: {po[8]} ({po[9]})"]):
        c.drawString(x0, y - 13 - i * 12, line)

    y -= 80
    row_h = 26
    c.setFillColor(blue)
    c.roundRect(x0, y - row_h + 6, w - x0 - 0.7 * inch, row_h - 4, 5, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x0 + 8, y - 10, "PRODUCT")
    c.drawRightString(w - 2.95 * inch, y - 10, "QTY")
    c.drawRightString(w - 1.75 * inch, y - 10, "ANNUAL PRICE")
    c.drawRightString(w - 0.8 * inch, y - 10, "AMOUNT")
    y -= row_h
    for i, (sku, desc, _cat, qty, price) in enumerate(po[15]):
        if i % 2 == 1:
            c.setFillColor(colors.HexColor("#f7f8fa"))
            c.roundRect(x0, y - row_h + 6, w - x0 - 0.7 * inch, row_h - 4, 5, stroke=0, fill=1)
        c.setFillColor(navy)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x0 + 8, y - 6, desc.split(" — ")[0])
        c.setFillColor(colors.HexColor("#6b7280"))
        c.setFont("Helvetica", 7)
        c.drawString(x0 + 8, y - 15, sku + ("  ·  " + desc.split(" — ", 1)[1] if " — " in desc else ""))
        c.setFillColor(navy)
        c.setFont("Helvetica", 9)
        c.drawRightString(w - 2.95 * inch, y - 10, str(qty))
        c.drawRightString(w - 1.75 * inch, y - 10, money(price))
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(w - 0.8 * inch, y - 10, money(qty * price))
        y -= row_h

    subtotal, tax, total = po_totals(po)
    y -= 14
    c.setFillColor(navy)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 2.05 * inch, y, "Total (USD):")
    c.setFillColor(blue)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(w - 0.8 * inch, y - 1, money(total))

    y -= 45
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont("Helvetica", 7.5)
    c.drawString(x0, y, "Notes: " + po[14])
    c.setFont("Helvetica-Oblique", 6.5)
    c.drawString(x0, 0.45 * inch, "Fictional order form generated for demo purposes.")
    c.save()


# ---- style 6: dense landscape with T&Cs (Cisco/Ingram) ---------------------

def render_dense_landscape(po, vendor, agreement, path):
    pagesize = landscape(LETTER)
    story = []
    head = Table(
        [[para(f"<b>{vendor[1]}</b><br/>{vendor[6]}", size=8.5),
          para(f"<b>PURCHASE ORDER</b>", size=14, align=1),
          para(f"<b>PO No:</b> {po[3]}<br/><b>Date:</b> {po[5]}<br/><b>Terms:</b> {po[7]}<br/><b>Ship by:</b> {po[6]}", size=8, align=2)]],
        colWidths=[3.3 * inch, 3.0 * inch, 3.3 * inch],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(head)
    story.append(Spacer(1, 8))

    v, b, s = std_addresses(vendor, size=8)
    info = para(
        f"<b>AGREEMENT / SUPPORT</b><br/>{agreement[3]}<br/>Ref: {agreement[2]} · expires {agreement[6]}<br/>"
        f"<b>Renewal due: {agreement[7]}</b> ({agreement[9]}-day notice)", size=8)
    addr = Table([[v, b, info]], colWidths=[3.2 * inch, 3.2 * inch, 3.2 * inch])
    addr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("BOX", (0, 0), (-1, -1), 0.5, colors.black),
                              ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
                              ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(addr)
    story.append(Spacer(1, 8))

    rows = [["LN", "PART NUMBER", "DESCRIPTION", "CATEGORY", "QTY", "UOM", "UNIT PRICE", "EXT PRICE"]]
    for i, (sku, desc, cat, qty, price) in enumerate(po[15], 1):
        rows.append([str(i), sku, para(desc, size=7.5), cat, str(qty), "EA", money(price), money(qty * price)])
    subtotal, tax, total = po_totals(po)
    t = Table(rows, colWidths=[0.35 * inch, 1.35 * inch, 3.4 * inch, 1.1 * inch, 0.5 * inch, 0.5 * inch, 1.15 * inch, 1.25 * inch], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9d9d9")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)
    tot = Table(
        [["", f"Subtotal: {money(subtotal)}   Tax ({po[11]*100:.2f}%): {money(tax)}   TOTAL: {money(total)}"]],
        colWidths=[4.5 * inch, 5.1 * inch])
    tot.setStyle(TableStyle([("ALIGN", (1, 0), (1, 0), "RIGHT"), ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
                             ("FONTSIZE", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
    story.append(tot)
    story.append(Spacer(1, 8))
    terms = (
        "TERMS & CONDITIONS: (1) This purchase order is governed by agreement %s between Buyer and Vendor. "
        "(2) SmartNet support contracts must be co-terminated to the agreement renewal date of %s. "
        "(3) Partial shipments permitted with prior written notice. (4) Invoices must reference the PO number; "
        "unreferenced invoices will be returned. (5) Title and risk of loss pass upon delivery at Buyer's dock. "
        "(6) %s. All amounts in USD. Fictional document for demo purposes."
    ) % (agreement[2], agreement[7], po[14])
    story.append(para(terms, size=6.5, color=colors.HexColor("#333333")))
    build_platypus(path, story, pagesize=pagesize, margins=0.55)


# ---- style 7: SaaS subscription order form (Okta) --------------------------

def render_form_saas(po, vendor, agreement, path):
    font = "Helvetica"
    teal = colors.HexColor("#00297a")
    story = [
        Table([[para(f"<b>{vendor[1]}</b>", size=16, color=teal),
                para("SUBSCRIPTION<br/>ORDER FORM", size=11, color=colors.HexColor("#6b7280"), align=2)]],
              colWidths=[4.6 * inch, 2.6 * inch]),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=2.2, color=teal),
        Spacer(1, 12),
    ]
    grid = [
        ("Order Form No.", po[3], "Order Date", po[5]),
        ("Customer", BUYER["name"], "Customer Contact", f"{po[8]} ({po[9]})"),
        ("Subscription Term", "2026-04-01 to 2027-03-31", "Billing Frequency", "Annual, in advance"),
        ("Governing Agreement", f"{agreement[2]} — {agreement[3]}", "Payment Terms", po[7]),
        ("Renewal Type", "Auto-renewal (45-day opt-out notice)", "Renewal Date", agreement[7]),
    ]
    gt = Table([[para(f"<b>{a}</b>", size=8, color=teal), para(b, size=8.5),
                 para(f"<b>c</b>".replace("c", cc), size=8, color=teal), para(d, size=8.5)]
                for a, b, cc, d in grid],
               colWidths=[1.35 * inch, 2.35 * inch, 1.35 * inch, 2.15 * inch])
    gt.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c7d2fe")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2ff")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2ff")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(gt)
    story.append(Spacer(1, 14))
    story.append(para("<b>SUBSCRIPTION SERVICES</b>", size=10, color=teal))
    story.append(Spacer(1, 4))
    t = Table(items_table_data(po, font), colWidths=[0.35 * inch, 1.1 * inch, 3.15 * inch, 0.55 * inch, 1.0 * inch, 1.05 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), teal),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7ff")]),
        ("LINEBELOW", (0, -1), (-1, -1), 1, teal),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))
    tot = Table([[k, v2] for k, v2 in totals_rows(po)], colWidths=[2.2 * inch, 1.2 * inch], hAlign="RIGHT")
    tot.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, -1), (-1, -1), 11),
        ("TEXTCOLOR", (0, -1), (-1, -1), teal),
    ]))
    story.append(tot)
    story.append(Spacer(1, 14))
    story.append(para(f"<b>Notes:</b> {po[14]}", size=8))
    story.append(Spacer(1, 20))
    story.append(para(
        "By signing below, Customer agrees to purchase the subscription services listed above pursuant to the "
        "governing agreement referenced herein. This order is non-cancellable and fees are non-refundable. "
        "<i>Fictional document for demo purposes.</i>", size=7, color=colors.HexColor("#6b7280")))
    story.append(Spacer(1, 26))
    story.append(Table([[para("_________________________<br/><b>Customer</b> — name, title, date", size=8),
                         para("_________________________<br/><b>Vendor</b> — name, title, date", size=8)]],
                       colWidths=[3.2 * inch, 3.2 * inch]))
    build_platypus(path, story)


# ---- style 8: traditional serif (SHI/Adobe) --------------------------------

def render_serif(po, vendor, agreement, path):
    font = "Times-Roman"
    story = [
        para(BUYER["name"].upper(), font=font, size=17, align=1),
        para(f"{BUYER['address']} · {BUYER['city']} · {BUYER['phone']}", font=font, size=8.5, align=1, color=colors.HexColor("#444444")),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=2, color=colors.black),
        HRFlowable(width="100%", thickness=0.5, color=colors.black, spaceBefore=2),
        Spacer(1, 10),
        para("PURCHASE ORDER", font=font, size=13, align=1, bold=True),
        Spacer(1, 12),
    ]
    v, b, _ = std_addresses(vendor, font=font, size=9)
    meta = para(
        f"<b>PO Number:</b> {po[3]}<br/><b>Date:</b> {po[5]}<br/><b>Terms:</b> {po[7]}<br/>"
        f"<b>VIP Agreement:</b> {agreement[2]}<br/><b>Anniversary:</b> {agreement[7]}",
        font=font, size=9,
    )
    story.append(Table([[v, meta]], colWidths=[3.6 * inch, 3.4 * inch],
                       style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])))
    story.append(Spacer(1, 14))

    t = Table(items_table_data(po, font, size=9), colWidths=[0.35 * inch, 1.2 * inch, 3.15 * inch, 0.5 * inch, 1.0 * inch, 1.0 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
        ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
        ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 8))
    tot = Table([[para(k, font=font, size=9.5, align=2, bold=(k == "TOTAL")),
                  para(v2, font=font, size=9.5, align=2, bold=(k == "TOTAL"))]
                 for k, v2 in totals_rows(po)],
                colWidths=[1.8 * inch, 1.2 * inch], hAlign="RIGHT")
    story.append(tot)
    story.append(Spacer(1, 16))
    story.append(para(f"Remarks: {po[14]}", font=font, size=8.5))
    story.append(Spacer(1, 30))
    story.append(para("Approved by: ____________________________          Date: ______________", font=font, size=9))
    build_platypus(path, story)


# ---- style 9: two-column modern (Lenovo) -----------------------------------

def render_two_column(po, vendor, agreement, path):
    c = rl_canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER
    red = colors.HexColor("#e2231a")
    ink = colors.HexColor("#1a1a1a")

    # left rail
    rail_w = 2.2 * inch
    c.setFillColor(colors.HexColor("#f4f4f4"))
    c.rect(0, 0, rail_w, h, stroke=0, fill=1)
    c.setFillColor(red)
    c.rect(0, h - 0.28 * inch, rail_w, 0.28 * inch, stroke=0, fill=1)
    x = 0.35 * inch
    y = h - 0.85 * inch
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x, y, "PURCHASE")
    c.drawString(x, y - 16, "ORDER")
    c.setFillColor(red)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y - 38, po[3])

    def rail_field(label, value, yy):
        c.setFillColor(colors.HexColor("#8a8a8a"))
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(x, yy, label.upper())
        c.setFillColor(ink)
        c.setFont("Helvetica", 8.2)
        for i, ln in enumerate(value if isinstance(value, list) else [value]):
            c.drawString(x, yy - 11 - i * 10, ln)

    rail_field("Order date", po[5], y - 70)
    rail_field("Deliver by", po[6], y - 100)
    rail_field("Payment terms", po[7], y - 130)
    rail_field("Requested by", [po[8], po[9]], y - 160)
    rail_field("Vendor", [vendor[1], vendor[6][:34], vendor[6][34:] or " "], y - 200)
    rail_field("Framework agreement", [agreement[2], f"Term ends {agreement[6]}", f"Renewal {agreement[7]}"], y - 250)
    rail_field("Ship to", [BUYER["name"], "IT Receiving, Dock B", "500 Market Street", "San Francisco, CA 94105"], y - 305)

    # right content
    rx = rail_w + 0.35 * inch
    ry = h - 0.9 * inch
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(rx, ry, vendor[1])
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont("Helvetica", 8.5)
    c.drawString(rx, ry - 15, "New-hire hardware kits — H2 2026 intake")
    ry -= 45

    c.setFillColor(red)
    c.rect(rx, ry, w - rx - 0.5 * inch, 1.5, stroke=0, fill=1)
    ry -= 18
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(rx, ry, "ITEM")
    c.drawRightString(w - 2.15 * inch, ry, "QTY")
    c.drawRightString(w - 1.35 * inch, ry, "UNIT")
    c.drawRightString(w - 0.55 * inch, ry, "TOTAL")
    for sku, desc, _cat, qty, price in po[15]:
        ry -= 26
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(ink)
        c.drawString(rx, ry + 4, desc.split(" — ")[0])
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.HexColor("#8a8a8a"))
        c.drawString(rx, ry - 5, sku + ("  ·  " + desc.split(" — ", 1)[1] if " — " in desc else ""))
        c.setFillColor(ink)
        c.setFont("Helvetica", 9)
        c.drawRightString(w - 2.15 * inch, ry, str(qty))
        c.drawRightString(w - 1.35 * inch, ry, money(price))
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(w - 0.55 * inch, ry, money(qty * price))
        c.setStrokeColor(colors.HexColor("#e5e5e5"))
        c.line(rx, ry - 10, w - 0.5 * inch, ry - 10)

    subtotal, tax, total = po_totals(po)
    ry -= 40
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 1.35 * inch, ry, "Subtotal")
    c.drawRightString(w - 0.55 * inch, ry, money(subtotal))
    ry -= 14
    c.drawRightString(w - 1.35 * inch, ry, f"Tax ({po[11]*100:.2f}%)")
    c.drawRightString(w - 0.55 * inch, ry, money(tax))
    ry -= 20
    c.setFillColor(red)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - 1.65 * inch, ry, "TOTAL")
    c.drawRightString(w - 0.55 * inch, ry, money(total))

    ry -= 45
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont("Helvetica", 7.5)
    c.drawString(rx, ry, "Notes: " + po[14][:95])
    c.setFont("Helvetica-Oblique", 6.5)
    c.drawString(rx, 0.4 * inch, "Fictional purchase order generated for demo purposes.")
    c.save()


# ---- style 10: receipt / courier renewal notice (Zoom) ----------------------

def render_receipt(po, vendor, agreement, path):
    c = rl_canvas.Canvas(path, pagesize=LETTER)
    w, h = LETTER
    cx = w / 2
    box_w = 4.6 * inch
    x0 = cx - box_w / 2
    ink = colors.HexColor("#20242b")

    c.setStrokeColor(ink)
    c.setLineWidth(1)
    c.rect(x0 - 14, 1.2 * inch, box_w + 28, h - 2.4 * inch, stroke=1, fill=0)

    y = h - 1.7 * inch
    c.setFillColor(ink)
    c.setFont("Courier-Bold", 14)
    c.drawCentredString(cx, y, vendor[1].upper())
    y -= 16
    c.setFont("Courier", 8.5)
    c.drawCentredString(cx, y, vendor[6])
    y -= 22
    c.setFont("Courier-Bold", 11)
    c.drawCentredString(cx, y, f"* {po[4]} *")
    y -= 14

    def dashed(yy):
        c.setDash(2, 2)
        c.line(x0, yy, x0 + box_w, yy)
        c.setDash()

    dashed(y)
    y -= 16
    c.setFont("Courier", 8.5)
    for label, val in [
        ("Order No", po[3]), ("Order date", po[5]), ("Customer", BUYER["name"]),
        ("Account", "ACME-77201"), ("Agreement", agreement[2]),
        ("Current term ends", agreement[6]), ("RENEWAL DATE", agreement[7]),
        ("New term", "2026-08-15 to 2027-08-14"), ("Payment", po[7]),
    ]:
        c.setFont("Courier-Bold" if label == "RENEWAL DATE" else "Courier", 8.5)
        c.drawString(x0, y, f"{label:<18}: {val}")
        y -= 13
    y -= 4
    dashed(y)
    y -= 16

    c.setFont("Courier-Bold", 8.5)
    c.drawString(x0, y, f"{'ITEM':<26}{'QTY':>4}{'PRICE':>12}")
    y -= 13
    c.setFont("Courier", 8.5)
    for sku, desc, _cat, qty, price in po[15]:
        name = desc.split(" (")[0][:26]
        c.drawString(x0, y, f"{name:<26}{qty:>4}{money(qty*price):>12}")
        y -= 11
        c.setFont("Courier", 7)
        c.drawString(x0 + 10, y, sku)
        c.setFont("Courier", 8.5)
        y -= 13
    dashed(y)
    y -= 16
    subtotal, tax, total = po_totals(po)
    c.setFont("Courier-Bold", 10)
    c.drawString(x0, y, f"{'TOTAL DUE (USD)':<24}{money(total):>18}")
    y -= 18
    dashed(y)
    y -= 16
    c.setFont("Courier", 7.5)
    for ln in [
        "Status: PENDING APPROVAL",
        f"Approver: {po[8]}, {po[9]}",
        "Renews automatically unless cancelled",
        "30 days before the renewal date.",
        "",
        "Fictional renewal order - demo only.",
    ]:
        c.drawCentredString(cx, y, ln)
        y -= 11
    c.save()


STYLE_RENDERERS = {
    "band_blue": lambda po, v, a, p: render_band(po, v, a, p, colors.HexColor("#0076ce")),
    "minimal_gray": render_minimal,
    "form_boxed": render_form_boxed,
    "dark_header": render_dark_header,
    "accent_side": render_accent_side,
    "dense_landscape": render_dense_landscape,
    "form_saas": render_form_saas,
    "serif_classic": render_serif,
    "two_column": render_two_column,
    "receipt": render_receipt,
}


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE vendors (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT,
    contact_name    TEXT,
    contact_email   TEXT,
    phone           TEXT,
    address         TEXT,
    website         TEXT
);

CREATE TABLE agreements (
    id                  INTEGER PRIMARY KEY,
    vendor_id           INTEGER NOT NULL REFERENCES vendors(id),
    agreement_number    TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    agreement_type      TEXT,
    start_date          DATE NOT NULL,
    end_date            DATE NOT NULL,
    renewal_date        DATE,
    auto_renew          INTEGER NOT NULL DEFAULT 0,   -- boolean
    renewal_notice_days INTEGER,
    annual_value_usd    REAL,
    status              TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE purchase_orders (
    id              INTEGER PRIMARY KEY,
    po_number       TEXT NOT NULL UNIQUE,
    vendor_id       INTEGER NOT NULL REFERENCES vendors(id),
    agreement_id    INTEGER REFERENCES agreements(id),
    order_date      DATE NOT NULL,
    delivery_date   DATE,
    currency        TEXT NOT NULL DEFAULT 'USD',
    subtotal        REAL NOT NULL,
    tax             REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL,
    status          TEXT NOT NULL,                    -- pending_approval/approved/received/invoiced/paid
    payment_terms   TEXT,
    requested_by    TEXT,
    department      TEXT,
    notes           TEXT,
    pdf_path        TEXT                              -- relative path to the PO document
);

CREATE TABLE po_line_items (
    id          INTEGER PRIMARY KEY,
    po_id       INTEGER NOT NULL REFERENCES purchase_orders(id),
    line_no     INTEGER NOT NULL,
    sku         TEXT,
    description TEXT NOT NULL,
    category    TEXT,
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL,
    line_total  REAL NOT NULL
);

CREATE TABLE invoices (
    id              INTEGER PRIMARY KEY,
    invoice_number  TEXT NOT NULL UNIQUE,
    po_id           INTEGER NOT NULL REFERENCES purchase_orders(id),
    vendor_id       INTEGER NOT NULL REFERENCES vendors(id),
    invoice_date    DATE NOT NULL,
    due_date        DATE NOT NULL,
    amount          REAL NOT NULL,
    status          TEXT NOT NULL,                    -- paid / pending / overdue
    paid_date       DATE,
    payment_method  TEXT
);

-- generic file registry: every stored document, linked to its entity
CREATE TABLE documents (
    id          INTEGER PRIMARY KEY,
    file_name   TEXT NOT NULL,
    file_path   TEXT NOT NULL,                        -- relative to this directory
    file_type   TEXT NOT NULL DEFAULT 'pdf',
    doc_type    TEXT NOT NULL,                        -- purchase_order / order_form / agreement ...
    entity_type TEXT NOT NULL,                        -- purchase_order / agreement
    entity_id   INTEGER NOT NULL,
    uploaded_at DATE NOT NULL
);

CREATE VIEW upcoming_renewals AS
SELECT a.agreement_number, v.name AS vendor, a.title, a.renewal_date,
       a.auto_renew, a.renewal_notice_days, a.annual_value_usd, a.status
FROM agreements a JOIN vendors v ON v.id = a.vendor_id
ORDER BY a.renewal_date;

CREATE VIEW po_summary AS
SELECT po.po_number, v.name AS vendor, a.agreement_number, po.order_date,
       po.total, po.status, po.pdf_path,
       COALESCE(SUM(i.amount) FILTER (WHERE i.status = 'paid'), 0) AS paid_amount
FROM purchase_orders po
JOIN vendors v ON v.id = po.vendor_id
LEFT JOIN agreements a ON a.id = po.agreement_id
LEFT JOIN invoices i ON i.po_id = po.id
GROUP BY po.id;
"""


def build_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    vendor_ids, agreement_ids, po_ids = {}, {}, {}

    for key, name, cat, cn, ce, ph, addr, web in VENDORS:
        cur = con.execute(
            "INSERT INTO vendors (name, category, contact_name, contact_email, phone, address, website) VALUES (?,?,?,?,?,?,?)",
            (name, cat, cn, ce, ph, addr, web))
        vendor_ids[key] = cur.lastrowid

    for key, vkey, num, title, typ, start, end, renew, auto, notice, val, status in AGREEMENTS:
        cur = con.execute(
            "INSERT INTO agreements (vendor_id, agreement_number, title, agreement_type, start_date, end_date, "
            "renewal_date, auto_renew, renewal_notice_days, annual_value_usd, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vendor_ids[vkey], num, title, typ, start, end, renew, auto, notice, val, status))
        agreement_ids[key] = cur.lastrowid

    for po in POS:
        (key, vkey, akey, number, title, odate, ddate, terms, req, dept,
         status, _tax_rate, _style, fname, notes, items) = po
        subtotal, tax, total = po_totals(po)
        pdf_rel = f"pdfs/{fname}"
        cur = con.execute(
            "INSERT INTO purchase_orders (po_number, vendor_id, agreement_id, order_date, delivery_date, currency, "
            "subtotal, tax, total, status, payment_terms, requested_by, department, notes, pdf_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (number, vendor_ids[vkey], agreement_ids[akey], odate, ddate, "USD",
             subtotal, tax, total, status, terms, req, dept, notes, pdf_rel))
        po_id = cur.lastrowid
        po_ids[key] = po_id
        for i, (sku, desc, cat, qty, price) in enumerate(items, 1):
            con.execute(
                "INSERT INTO po_line_items (po_id, line_no, sku, description, category, quantity, unit_price, line_total) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (po_id, i, sku, desc, cat, qty, price, round(qty * price, 2)))
        doc_type = "order_form" if "ORDER" in title and "PURCHASE" not in title else "purchase_order"
        con.execute(
            "INSERT INTO documents (file_name, file_path, file_type, doc_type, entity_type, entity_id, uploaded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (fname, pdf_rel, "pdf", doc_type, "purchase_order", po_id, odate))

    for pkey, inum, idate, due, frac, status, paid_date, method in INVOICES:
        po = next(p for p in POS if p[0] == pkey)
        _s, _t, total = po_totals(po)
        con.execute(
            "INSERT INTO invoices (invoice_number, po_id, vendor_id, invoice_date, due_date, amount, status, paid_date, payment_method) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (inum, po_ids[pkey], vendor_ids[po[1]], idate, due, round(total * frac, 2), status, paid_date, method))

    con.commit()
    con.close()


def main():
    os.makedirs(PDF_DIR, exist_ok=True)
    for po in POS:
        vendor = vendor_by_key(po[1])
        agreement = agreement_by_key(po[2])
        style, fname = po[12], po[13]
        STYLE_RENDERERS[style](po, vendor, agreement, os.path.join(PDF_DIR, fname))
        print(f"  wrote pdfs/{fname}")
    build_db()
    print(f"  wrote {os.path.basename(DB_PATH)}")


if __name__ == "__main__":
    main()
