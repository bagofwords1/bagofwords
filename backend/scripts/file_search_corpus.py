"""Synthetic file corpus + golden queries for file-search tests and benchmarks.

Builds a directory tree that looks like a company file share (contracts,
invoices, policies, reports, logs, spreadsheets, Hebrew documents) and plants
facts in it whose location is known, so a search can be scored: for each
golden query we know which file must come back.

Everything is deterministic for a given seed. No external libraries beyond
openpyxl (already a dependency): PDFs and DOCX files are written by hand.

Used by:
- tests/e2e/test_file_fulltext_search.py (small trees built with the writers)
- scripts/file_search_bench.py           (the large benchmark corpus)
"""
from __future__ import annotations

import json
import random
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

# ------------------------------------------------------------------ writers


def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf(path: Path, pages: Sequence[Sequence[str]]) -> None:
    """Write a text PDF with one entry of ``pages`` per page (a list of lines).

    Latin-1 text only (Helvetica, no embedded font). The text is real,
    selectable PDF text — PDFium extracts it page by page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"")  # 1: catalog (filled below)
    add(b"")  # 2: pages   (filled below)
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for lines in pages:
        ops = ["BT", "/F1 10 Tf", "50 760 Td", "12 TL"]
        for line in lines:
            ops.append(f"({_pdf_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font, content)
        ))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids)
                  + b"] /Count %d >>" % len(page_ids))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_docx(path: Path, paragraphs: Iterable[str]) -> None:
    """Write a minimal valid .docx (any language — text is UTF-8 XML)."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(p)}</w:t></w:r></w:p>' for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def write_xlsx(path: Path, sheets: Dict[str, Sequence[Sequence]]) -> None:
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(list(row))
    wb.save(path)


# --------------------------------------------------------------- vocabulary

# Everyday business vocabulary. Sampled with a Zipf-like skew so some words
# are everywhere (as in real documents) and others are rare.
_VOCAB = """
agreement account amount annual approval audit balance bank benefit billing board
budget business buyer capital cash change charge claim client clause company
compliance condition contract control cost credit customer data date deadline
delivery department deposit detail director discount document draft employee
estimate evidence exchange expense facility fee finance forecast fund goods
guarantee holder income insurance interest inventory invoice issue item labor
legal liability license limit loan location management manager margin market
material meeting member method notice number obligation offer office officer
operation order owner partner party payment period plan policy position price
process product profit project property provider purchase quality quarter rate
receipt record region report request requirement resource result revenue review
risk role sale schedule section security seller service shipment site staff
standard statement stock supplier supply support system target tax team term
total training transfer unit value vendor warehouse warranty work year
access action activity addition address advance agency analysis answer area
assessment asset authority average base basis batch branch building capacity
category center channel code committee communication component contact content
copy country coverage criteria currency cycle damage decision degree demand
design development device difference direction distribution division effect
effort element energy engineering entry environment equipment error event
example exception execution experience factor failure feature field figure
form format framework function growth guide history impact improvement index
industry information input inspection instruction integration interval
knowledge language layer level line list machine maintenance measure message
model module network objective option output overview package page parameter
pattern percent performance phase point practice priority problem procedure
program progress proposal purpose range reason reference release response
rule sample scope sequence session setting signal solution source space
specification stage status step storage strategy structure subject summary
table task technology test tool topic track transaction trend type update
usage user version volume window
""".split()

_HEBREW_VOCAB = """
הסכם חשבון סכום אישור ביקורת יתרה תקציב עסק לקוח סעיף חברה תנאי עלות אשראי
תאריך משלוח מחלקה פירוט מנהל מסמך עובד הוצאה כספים תחזית ביטוח ריבית מלאי
חשבונית פריט אחריות רישיון הלוואה ניהול שוק חומר פגישה הודעה מספר התחייבות
הצעה משרד הזמנה בעלים שותף תשלום תקופה תוכנית מדיניות מחיר תהליך מוצר רווח
פרויקט נכס ספק רכישה איכות רבעון קבלה אזור דוח בקשה דרישה משאב תוצאה הכנסה
סקירה סיכון תפקיד מכירה שירות אתר צוות תקן מערכת יעד מס הדרכה העברה יחידה ערך
""".split()


def _zipf_sampler(rng: random.Random, words: Sequence[str]):
    weights = [1.0 / (i + 1) ** 0.9 for i in range(len(words))]
    order = list(words)
    rng.shuffle(order)

    def sample(n: int) -> List[str]:
        return rng.choices(order, weights=weights, k=n)

    return sample


# ------------------------------------------------------------------- corpus


@dataclass
class GoldenQuery:
    id: str
    kind: str            # what the query tests — see build_corpus
    query: str
    expected: List[str]  # relative paths; any of them in the results is a hit
    note: str = ""
    location: Dict = field(default_factory=dict)  # where the answer is, when known


@dataclass
class Corpus:
    root: str
    files: int
    bytes: int
    queries: List[GoldenQuery]

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"root": self.root, "files": self.files, "bytes": self.bytes,
             "queries": [asdict(q) for q in self.queries]},
            ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "Corpus":
        d = json.loads(path.read_text(encoding="utf-8"))
        return Corpus(root=d["root"], files=d["files"], bytes=d["bytes"],
                      queries=[GoldenQuery(**q) for q in d["queries"]])


_DEPARTMENTS = ["finance", "legal", "operations", "hr", "sales", "procurement", "it", "logistics"]
_DOC_KINDS = ["contract", "invoice", "policy", "report", "memo", "minutes", "proposal", "spec"]
# Invented words: they exist nowhere in the filler vocabulary, so a query for
# one has exactly one right answer.
_SYLLABLES = ["zor", "vek", "plim", "tash", "quon", "brel", "dax", "fenn", "gorv", "hul",
              "jask", "kreb", "lome", "mirt", "nolv", "prax", "rund", "sarn", "tulk", "wex"]


def _coined(rng: random.Random, used: set) -> str:
    while True:
        w = "".join(rng.sample(_SYLLABLES, 3))
        if w not in used:
            used.add(w)
            return w


def _sentence(sample, n: int) -> str:
    return " ".join(sample(n)).capitalize() + "."


def _paragraphs(rng: random.Random, sample, count: int) -> List[str]:
    return [" ".join(_sentence(sample, rng.randint(8, 16)) for _ in range(rng.randint(3, 6)))
            for _ in range(count)]


def build_corpus(root: Path, n_files: int = 2000, seed: int = 7, per_kind: int = 15) -> Corpus:
    """Generate ``n_files`` filler files plus planted answers for the golden
    queries (``per_kind`` queries of each kind).

    Query kinds — each is a way people actually look for a file:

    - ``name``         words from the file's NAME (the control group: any index
                       should already get these right).
    - ``rare_word``    a distinctive word that appears ONCE in an ordinary
                       document — not among its most frequent words.
    - ``id_in_body``   an ID like ``PO-48213`` that appears only inside a file.
    - ``deep``         a fact far into a long document (past the first 200k
                       characters / page 200 of a PDF).
    - ``phrase``       an exact phrase; other files contain the same words
                       scattered.
    - ``topic``        the one document that is ABOUT a subject, among many
                       that mention it once. Must rank in the top results.
    - ``question``     a natural-language question whose distinctive words are
                       in one file, with common words everywhere.
    - ``sheet``        a value on a later sheet of a workbook.
    - ``hebrew``       a Hebrew word searched in its bare form while the
                       document has it with an attached particle (ב/ה/ל…).
    """
    rng = random.Random(seed)
    sample = _zipf_sampler(rng, _VOCAB)
    he_sample = _zipf_sampler(rng, _HEBREW_VOCAB)
    root.mkdir(parents=True, exist_ok=True)
    used: set = set()
    queries: List[GoldenQuery] = []
    total_bytes = 0
    made = 0

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def filler_path(i: int, ext: str) -> Path:
        dept = _DEPARTMENTS[i % len(_DEPARTMENTS)]
        kind = _DOC_KINDS[(i // 7) % len(_DOC_KINDS)]
        return root / dept / f"{2018 + i % 8}" / f"{kind}_{i:06d}.{ext}"

    def write_text_file(p: Path, paragraphs: List[str]) -> None:
        nonlocal total_bytes, made
        p.parent.mkdir(parents=True, exist_ok=True)
        data = "\n\n".join(paragraphs).encode("utf-8")
        p.write_bytes(data)
        total_bytes += len(data)
        made += 1

    def write_doc(p: Path, paragraphs: List[str]) -> None:
        """Write ``paragraphs`` in the format the extension asks for."""
        nonlocal total_bytes, made
        if p.suffix == ".pdf":
            lines: List[str] = []
            for para in paragraphs:
                words = para.split()
                for j in range(0, len(words), 14):
                    lines.append(" ".join(words[j:j + 14]))
                lines.append("")
            pages = [lines[j:j + 55] for j in range(0, len(lines), 55)] or [[""]]
            write_pdf(p, pages)
        elif p.suffix == ".docx":
            write_docx(p, paragraphs)
        else:
            write_text_file(p, paragraphs)
            return
        total_bytes += p.stat().st_size
        made += 1

    # ---- planted answers ------------------------------------------------
    n = per_kind

    # name: distinctive words in the file name only.
    for k in range(n):
        w1, w2 = _coined(rng, used), _coined(rng, used)
        p = root / "sales" / "accounts" / f"{w1}_{w2}_account_plan.txt"
        write_text_file(p, _paragraphs(rng, sample, 4))
        queries.append(GoldenQuery(f"name-{k}", "name", f"{w1} {w2} account plan", [rel(p)]))

    # rare_word: one occurrence in the middle of an ordinary document.
    for k in range(n):
        word = _coined(rng, used)
        ext = ["txt", "docx", "pdf"][k % 3]
        p = root / "operations" / "notes" / f"site_visit_{k:03d}.{ext}"
        paras = _paragraphs(rng, sample, 14)
        mid = len(paras) // 2
        paras[mid] += f" The inspector flagged the {word} assembly for replacement."
        write_doc(p, paras)
        queries.append(GoldenQuery(f"rare-{k}", "rare_word", f"{word} assembly", [rel(p)],
                                   note="one occurrence mid-document"))

    # id_in_body: identifiers that exist only inside a file.
    for k in range(n):
        prefix = rng.choice(["PO", "INV", "CASE", "REQ", "SKU"])
        ident = f"{prefix}-{rng.randint(10000, 99999)}"
        ext = ["txt", "pdf", "docx"][k % 3]
        p = root / "procurement" / "orders" / f"order_batch_{k:03d}.{ext}"
        paras = _paragraphs(rng, sample, 10)
        paras[rng.randrange(2, 9)] += f" Reference {ident} was approved for release."
        write_doc(p, paras)
        queries.append(GoldenQuery(f"id-{k}", "id_in_body", ident, [rel(p)]))

    # deep: a fact past the keyword tier's sample (200k chars / 200 pages).
    for k in range(n):
        word = _coined(rng, used)
        if k % 2 == 0:
            p = root / "legal" / "archive" / f"master_agreement_{k:03d}.txt"
            paras = _paragraphs(rng, sample, 520)  # ~260k characters
            paras[-3] += f" Annex {word} governs termination of the reseller schedule."
            write_text_file(p, paras)
            loc = {"unit": "bytes"}
        else:
            p = root / "legal" / "archive" / f"master_agreement_{k:03d}.pdf"
            pages = []
            for page_no in range(1, 241):
                lines = [" ".join(sample(12)) for _ in range(30)]
                if page_no == 231:
                    lines[10] = f"Annex {word} governs termination of the reseller schedule."
                pages.append(lines)
            write_pdf(p, pages)
            total_bytes += p.stat().st_size
            made += 1
            loc = {"page": 231}
        queries.append(GoldenQuery(f"deep-{k}", "deep", f"annex {word} termination", [rel(p)],
                                   note="past the first 200k characters / 200 pages", location=loc))

    # phrase: exact phrase in one file; the same words scattered in others.
    phrase_words = ["liquidated", "damages", "ceiling", "waiver", "escrow", "holdback",
                    "indemnity", "carveout", "exclusivity", "window", "novation", "consent"]
    for k in range(n):
        a, b, c = rng.sample(phrase_words, 3)
        phrase = f"{a} {b} {c}"
        p = root / "legal" / "clauses" / f"clause_note_{k:03d}.txt"
        paras = _paragraphs(rng, sample, 8)
        paras[4] += f" The parties agreed a {phrase} for this engagement."
        write_text_file(p, paras)
        for d in range(6):  # distractors: same words, never adjacent
            dp = root / "legal" / "clauses" / f"clause_misc_{k:03d}_{d}.txt"
            dparas = _paragraphs(rng, sample, 8)
            for i, w in enumerate((a, b, c)):
                dparas[1 + 2 * i] += f" See the {w} schedule."
            write_text_file(dp, dparas)
        queries.append(GoldenQuery(f"phrase-{k}", "phrase", f'"{phrase}"', [rel(p)]))

    # topic: one document about the subject among many that mention it once.
    topics = ["cybersecurity", "sustainability", "onboarding", "relocation", "restructuring",
              "decommissioning", "whistleblowing", "telecommuting", "benchmarking", "outsourcing",
              "rebranding", "digitization", "divestiture", "localization", "recertification",
              "remediation", "consolidation", "reforestation", "electrification", "modernization"]
    for k in range(min(n, len(topics))):
        topic = topics[k]
        p = root / "hr" / "handbook" / f"guideline_{k:03d}.docx"
        paras = []
        for _ in range(10):
            words = sample(60)
            for pos in rng.sample(range(60), 6):
                words[pos] = topic
            paras.append(" ".join(words).capitalize() + ".")
        write_doc(p, paras)
        for d in range(25):  # each mentions the topic once
            dp = root / "hr" / "misc" / f"update_{k:03d}_{d:02d}.txt"
            dparas = _paragraphs(rng, sample, 6)
            dparas[rng.randrange(6)] += f" A short note on {topic} follows next quarter."
            write_text_file(dp, dparas)
        queries.append(GoldenQuery(f"topic-{k}", "topic", topic, [rel(p)],
                                   note="must rank in the top results among 25 passing mentions"))

    # question: a natural question; the distinctive words are in one file.
    for k in range(n):
        vendor, part = _coined(rng, used), _coined(rng, used)
        p = root / "procurement" / "vendors" / f"vendor_terms_{k:03d}.txt"
        paras = _paragraphs(rng, sample, 9)
        paras[5] += (f" Late delivery by {vendor.capitalize()} Ltd of the {part} components incurs "
                     f"a penalty of two percent per week.")
        write_text_file(p, paras)
        queries.append(GoldenQuery(
            f"question-{k}", "question",
            f"what is the penalty for late delivery of {part} components by {vendor}", [rel(p)]))

    # sheet: a value on the third sheet of a workbook.
    for k in range(n):
        code = f"CC-{rng.randint(1000, 9999)}-{_coined(rng, used)[:4].upper()}"
        p = root / "finance" / "workbooks" / f"cost_centers_{k:03d}.xlsx"
        sheets = {
            "Summary": [["center", "owner", "budget"]] + [[f"C{i}", sample(1)[0], rng.randint(1, 900)] for i in range(40)],
            "Detail": [["line", "amount"]] + [[sample(1)[0], rng.randint(1, 900)] for i in range(60)],
            "Overrides": [["code", "reason"]] + [[f"CC-{rng.randint(1000, 9999)}", sample(1)[0]] for i in range(30)]
                         + [[code, "manual override approved"]],
        }
        write_xlsx(p, sheets)
        total_bytes += p.stat().st_size
        made += 1
        queries.append(GoldenQuery(f"sheet-{k}", "sheet", code, [rel(p)], location={"sheet": "Overrides"}))

    # hebrew: bare word searched, the document has it with a particle attached.
    he_targets = ["בוררות", "שיפוי", "ערבות", "פיצוי", "עיכבון", "המחאה", "שעבוד", "חילוט",
                  "נאמנות", "זיכיון", "התיישנות", "קיזוז", "ערעור", "גישור", "פשרה", "הפרה",
                  "מכרז", "תמלוגים", "שכירות", "בעלות"]
    particles = ["ב", "ל", "ה", "ו", "מ", "ש", "כ", "וה", "של", "בה"]
    for k in range(min(n, len(he_targets))):
        word = he_targets[k]
        pref = particles[k % len(particles)]
        p = root / "legal" / "hebrew" / f"heskem_{k:03d}.docx"
        paras = [" ".join(he_sample(40)) + "." for _ in range(8)]
        paras[3] += f" הצדדים הסכימו כי המחלוקת תידון {pref}{word} בלבד."
        write_doc(p, paras)
        queries.append(GoldenQuery(f"hebrew-{k}", "hebrew", word, [rel(p)],
                                   note=f"document has '{pref}{word}'"))

    # ---- filler -----------------------------------------------------------
    i = 0
    while made < n_files:
        r = rng.random()
        if r < 0.70:
            write_text_file(filler_path(i, "txt" if r < 0.55 else "md"),
                            _paragraphs(rng, sample, rng.randint(4, 12)))
        elif r < 0.85:
            write_doc(filler_path(i, "docx"), _paragraphs(rng, sample, rng.randint(4, 12)))
        elif r < 0.97:
            write_doc(filler_path(i, "pdf"), _paragraphs(rng, sample, rng.randint(6, 18)))
        else:
            p = filler_path(i, "csv")
            p.parent.mkdir(parents=True, exist_ok=True)
            rows = ["item,owner,amount"] + [
                f"{sample(1)[0]},{sample(1)[0]},{rng.randint(1, 9000)}" for _ in range(rng.randint(20, 200))]
            data = "\n".join(rows).encode()
            p.write_bytes(data)
            total_bytes += len(data)
            made += 1
        i += 1

    return Corpus(root=str(root), files=made, bytes=total_bytes, queries=queries)
