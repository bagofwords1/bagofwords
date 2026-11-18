#!/usr/bin/env python3
"""Measure PII detectors on BoW-style text (chat questions, data rows, and
analytics text with no PII), in English and Hebrew.

Each candidate is a Presidio analyzer service (tools/agent/presidio_service.py
or the official container). Spans are marked in the corpus as
``[[PERSON:Dana Cohen]]``; a detection counts when it overlaps a marked span of
the same type. Everything is synthetic.

    python tools/agent/pii_detector_eval.py \
        spacy-sm=http://127.0.0.1:5001 spacy-lg=http://127.0.0.1:5002 gliner-multi=http://127.0.0.1:5003

Prints precision / recall per entity and language, false positives on the
no-PII set, and latency. Use it to choose ``pii.presidio_model`` and each
policy's minimum score for your own data: swap in your own examples.
"""
import json
import re
import sys
import time
import urllib.request

EN = [
    "What did [[PERSON:Dana Whitfield]] buy last month?",
    "Show all invoices for [[PERSON:Marco Bellini]] and [[PERSON:Priya Raman]].",
    "Email the Q3 summary to [[PERSON:Tomás Herrera]] at [[ORGANIZATION:Northwind Traders]].",
    "How many customers live in [[LOCATION:Lisbon]] versus [[LOCATION:Porto]]?",
    "Compare revenue for [[ORGANIZATION:Acme Robotics]] and [[ORGANIZATION:Blue Harbor Logistics]] this year.",
    "Is [[PERSON:Akira Tanaka]] still the account owner for [[ORGANIZATION:Kestrel Foods]]?",
    "List employees who report to [[PERSON:Ingrid Solberg]] in the [[LOCATION:Oslo]] office.",
    "Why did orders from [[LOCATION:Bavaria]] drop after [[PERSON:Lukas Brandt]] left?",
    "CustomerId: 14 | FirstName: [[PERSON:Fatima]] | LastName: [[PERSON:Al-Sayed]] | City: [[LOCATION:Amman]] | Total: 39.62",
    "CustomerId: 27 | Name: [[PERSON:Olusegun Adeyemi]] | Company: [[ORGANIZATION:Lagos Freight Ltd]] | Country: [[LOCATION:Nigeria]]",
    "name,city,total\n[[PERSON:Hannah Greene]],[[LOCATION:Dublin]],120.5\n[[PERSON:Rafael Monteiro]],[[LOCATION:Recife]],88.0",
    "Top rep this quarter: [[PERSON:Chen Wei]] (42 deals), then [[PERSON:Sofia Lindqvist]] (37).",
    "Schedule a call with [[PERSON:Nadia Petrova]] about the [[ORGANIZATION:Helix Bio]] renewal.",
    "Which accounts did [[PERSON:Jamal Carter]] close in [[LOCATION:Toronto]]?",
    "The complaint came from [[PERSON:Mrs. Eleanor Voss]] of [[LOCATION:Cape Town]].",
    "Find tickets opened by [[PERSON:Yusuf Demir]] for [[ORGANIZATION:Anatolia Telecom]].",
    "Did [[PERSON:Grace Okafor]] approve the refund for the [[LOCATION:Nairobi]] branch?",
    "Row 3: [[PERSON:Pedro Alvarez]], [[LOCATION:Seville]], churn risk high",
    "Summarize the notes from [[PERSON:Dr. Amelia Hart]] on the [[ORGANIZATION:Riverside Clinic]] account.",
    "Who replaced [[PERSON:Viktor Novak]] as manager for [[LOCATION:Prague]]?",
    "Send [[PERSON:Aisha Khan]] the churn list for [[ORGANIZATION:Summit Insurance]].",
    "Customers named [[PERSON:O'Connor]] or [[PERSON:MacLeod]] in [[LOCATION:Glasgow]]",
    "What is the lifetime value of [[PERSON:Isabela Costa]]?",
    "Orders shipped to [[LOCATION:Rotterdam]] by [[ORGANIZATION:Van Dijk Transport]] in March",
    "employee: [[PERSON:Mateus Ribeiro]]; manager: [[PERSON:Clara Engel]]; site: [[LOCATION:Zurich]]",
]
HE = [
    "מה הזמינה [[PERSON:דנה כהן]] בחודש שעבר?",
    "הצג את כל החשבוניות של [[PERSON:יואב לוי]] ושל [[PERSON:נועה פרידמן]].",
    "כמה לקוחות גרים ב[[LOCATION:חיפה]] לעומת [[LOCATION:באר שבע]]?",
    "שלח את הסיכום הרבעוני ל[[PERSON:אבי מזרחי]] מחברת [[ORGANIZATION:אלקטרה מערכות]].",
    "האם [[PERSON:מיכל רוזן]] עדיין אחראית על החשבון של [[ORGANIZATION:תנובת הגליל]]?",
    "מי החליף את [[PERSON:עומר שפירא]] כמנהל הסניף ב[[LOCATION:ירושלים]]?",
    "לקוח: [[PERSON:רונית אברהם]] | עיר: [[LOCATION:נתניה]] | סכום: 412",
    "אילו עסקאות סגר [[PERSON:איתי גולן]] ב[[LOCATION:תל אביב]] השנה?",
    "התלונה הגיעה מ[[PERSON:שרה ביטון]] מ[[LOCATION:אשדוד]].",
    "השווה הכנסות בין [[ORGANIZATION:כרמל לוגיסטיקה]] ל[[ORGANIZATION:נגב אנרגיה]].",
    "קבע שיחה עם [[PERSON:תמר וייס]] לגבי החידוש.",
    "שם: [[PERSON:אליאור דהן]], מנהל: [[PERSON:גליה שטרן]], אתר: [[LOCATION:רעננה]]",
]
# Analytics text with nothing personal: every detection here is a false positive.
NEGATIVE = [
    "Show total revenue by Genre for the Customer table, top 10 only.",
    "SELECT c.Country, SUM(i.Total) FROM Invoice i JOIN Customer c ON c.CustomerId = i.CustomerId GROUP BY c.Country",
    "Columns: InvoiceId, CustomerId, InvoiceDate, BillingCity, BillingCountry, Total",
    "Plot monthly active users as a line chart and add a 7-day moving average.",
    "Why is the Q3 gross margin lower than Q2? Break it down by product line.",
    "Table Employee has columns EmployeeId, LastName, FirstName, Title, ReportsTo, HireDate.",
    "Create a dashboard with churn rate, ARPU and net revenue retention.",
    "Filter where Status = 'Active' and PlanTier in ('Pro', 'Enterprise').",
    "The Track table joins Album on AlbumId and Genre on GenreId.",
    "Use a bar chart, sort descending, and label the axes Revenue and Month.",
    "הצג הכנסות לפי חודש בטבלת ההזמנות, עשרת הגדולים בלבד.",
    "צור גרף עמודות של שיעור הנטישה לפי תוכנית.",
]
MARK = re.compile(r"\[\[([A-Z]+):(.*?)\]\]")
TYPES = ["PERSON", "LOCATION", "ORGANIZATION"]


def parse(marked):
    """-> (plain_text, [(start, end, type)])"""
    out, spans, pos = [], [], 0
    plain_len = 0
    for m in MARK.finditer(marked):
        out.append(marked[pos:m.start()])
        plain_len += m.start() - pos
        spans.append((plain_len, plain_len + len(m.group(2)), m.group(1)))
        out.append(m.group(2))
        plain_len += len(m.group(2))
        pos = m.end()
    out.append(marked[pos:])
    return "".join(out), spans


def analyze(url, text):
    req = urllib.request.Request(f"{url}/analyze", data=json.dumps(
        {"text": text, "language": "en", "entities": TYPES}).encode(), headers={"content-type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read())


def score(url, items, threshold):
    stats = {t: {"tp": 0, "fn": 0, "fp": 0} for t in TYPES}
    ms = []
    for marked in items:
        text, gold = parse(marked)
        t0 = time.time()
        found = [d for d in analyze(url, text) if d["score"] >= threshold and d["entity_type"] in TYPES]
        ms.append((time.time() - t0) * 1000)
        used = set()
        for gs, ge, gt in gold:
            hit = next((i for i, d in enumerate(found)
                        if d["entity_type"] == gt and d["start"] < ge and d["end"] > gs), None)
            if hit is None:
                stats[gt]["fn"] += 1
            else:
                stats[gt]["tp"] += 1
                used.add(hit)
        for i, d in enumerate(found):
            if i in used:
                continue
            # A second detection inside an already-credited gold span is not an error.
            if any(d["entity_type"] == gt and d["start"] < ge and d["end"] > gs for gs, ge, gt in gold):
                continue
            stats[d["entity_type"]]["fp"] += 1
    return stats, ms


def fmt(st):
    p = st["tp"] / (st["tp"] + st["fp"]) if st["tp"] + st["fp"] else float("nan")
    r = st["tp"] / (st["tp"] + st["fn"]) if st["tp"] + st["fn"] else float("nan")
    return f"P {p:4.2f} R {r:4.2f}"


def main():
    candidates = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a and not a.startswith("--"))
    threshold = float(next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--min-score=")), 0.5))
    if not candidates:
        sys.exit(__doc__)
    print(f"min score {threshold} · EN {len(EN)} texts · HE {len(HE)} texts · no-PII {len(NEGATIVE)} texts\n")
    print(f"{'model':<14}{'lang':<5}" + "".join(f"{t:<22}" for t in TYPES) + "no-PII false pos   median ms")
    for name, url in candidates.items():
        analyze(url, "warm up")
        neg, _ = score(url, NEGATIVE, threshold)
        neg_fp = sum(v["fp"] for v in neg.values())
        for lang, items in (("EN", EN), ("HE", HE)):
            st, ms = score(url, items, threshold)
            ms.sort()
            print(f"{name:<14}{lang:<5}" + "".join(f"{fmt(st[t]):<22}" for t in TYPES)
                  + f"{neg_fp if lang == 'EN' else '':<19}{ms[len(ms)//2]:.0f}")


if __name__ == "__main__":
    main()
