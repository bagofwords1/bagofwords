"""Summarise what the SMTP sink received: who got which rows.

    python read_mail.py <mail_dir>

One line per (message, envelope recipient): the subject, the attachment type,
and the distinct row labels found in the attachment's extracted text (PDF via
pypdfium2), plus whether the body carried the owner's run summary.
"""
import email
import io
import pathlib
import re
import sys
from email import policy

LABEL = re.compile(r"\b(OWNER-ACCT|ALICE-ACCT|BOB-ACCT|CAROL-ACCT|PLAIN-ROW)-\d+\b")


def pdf_text(blob: bytes) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(io.BytesIO(blob))
    return " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


def labels(text: str) -> str:
    found = sorted(set(m.group(0) for m in LABEL.finditer(text)))
    if not found:
        return "-"
    by = {}
    for f in found:
        by.setdefault(f.rsplit("-", 1)[0], []).append(f)
    return " ".join(f"{k}x{len(v)}" for k, v in sorted(by.items()))


rows = []
for path in sorted(pathlib.Path(sys.argv[1]).glob("*.eml")):
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg["subject"])
    if "Welcome" in subject or "invited" in subject.lower():
        continue
    rcpts = (msg["X-Envelope-To"] or "").split(",")
    body = msg.get_body(preferencelist=("html", "plain"))
    body_text = body.get_content() if body else ""
    att_desc, att_labels = "none", "-"
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        ctype = part.get_content_type()
        att_desc = f"{ctype.split('/')[-1]} {len(data)}B"
        try:
            att_labels = labels(pdf_text(data)) if data[:4] == b"%PDF" else labels(data.decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            att_labels = f"unreadable ({e})"
    summary = "yes" if "OWNER-ACCT-1" in body_text else "no"
    for r in rcpts:
        rows.append((subject, r, att_desc, att_labels, summary, len(rcpts)))

print(f"{'subject':55} {'recipient':28} {'to#':>3} {'attachment':14} {'rows in attachment':34} owner-summary-in-body")
for subject, r, a, l, s, n in sorted(rows):
    print(f"{subject[:55]:55} {r:28} {n:>3} {a:14} {l:34} {s}")
print(f"\n{len(rows)} deliveries")
