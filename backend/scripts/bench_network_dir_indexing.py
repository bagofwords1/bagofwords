#!/usr/bin/env python
"""Benchmark network_dir content indexing: cold index vs re-index of an
UNCHANGED directory, with and without the incremental `prior_catalog` skip.

Generates a corpus of REAL multi-page PDFs (matplotlib PDF backend — selectable
text, so pypdf extraction does real work) plus some txt/csv, then measures:

  run 1  cold index (every file extracted)
  run 2  re-index without a prior catalog — the pre-incremental behavior,
         which re-extracted everything on every scheduled reindex
  run 3  re-index WITH the prior catalog — what refresh_schema passes now;
         unchanged files reuse stored keywords/hashes (stat-only walk)

and asserts run 3 produces a catalog identical to run 1 (same files, same
keywords, same content hashes).

Usage:
    cd backend
    uv run python scripts/bench_network_dir_indexing.py /tmp/bench_corpus --pdfs 200
"""
from __future__ import annotations

import argparse
import csv
import random
import textwrap
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from app.data_sources.clients.network_dir_client import NetworkDirClient

VENDORS = ["Acme Corp", "Globex", "Initech", "Umbrella", "Soylent",
           "Stark Industries", "Wayne Enterprises", "Cyberdyne"]
CLAUSES = ["indemnity", "auto-renewal", "arbitration", "force majeure",
           "limitation of liability", "data protection"]
LOREM = (
    "The parties acknowledge that continued performance under this agreement "
    "requires timely delivery of services, quarterly reconciliation of invoices, "
    "and adherence to the governing service levels. "
)


def _make_pdf(path: Path, title: str, pages: int, rng: random.Random) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(path) as pdf:
        for p in range(pages):
            fig = plt.figure(figsize=(8.5, 11))
            fig.text(0.08, 0.94, f"{title} — page {p + 1}", fontsize=14, weight="bold")
            body = [
                f"Section {p + 1}.{i + 1}: {rng.choice(CLAUSES)} clause. "
                f"Contract value ${rng.randint(50_000, 5_000_000):,}. " + LOREM
                for i in range(6)
            ]
            fig.text(0.08, 0.88, "\n".join(textwrap.fill(t, width=95) for t in body),
                     fontsize=9, va="top")
            pdf.savefig(fig)
            plt.close(fig)


def generate_corpus(root: Path, pdfs: int, pages: int, others: int) -> int:
    rng = random.Random(1234)
    for i in range(pdfs):
        vendor = VENDORS[i % len(VENDORS)]
        slug = vendor.lower().replace(" ", "_")
        _make_pdf(root / f"contracts/{i // 200:02d}" / f"msa_{slug}_{i:04d}.pdf",
                  f"Master Services Agreement — {vendor} #{i:04d}", pages, rng)
        if (i + 1) % 100 == 0:
            print(f"  corpus: {i + 1}/{pdfs} PDFs")
    for i in range(others):
        if i % 2 == 0:
            p = root / "notes" / f"note_{i:03d}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"Meeting notes {i}: renewal pipeline review. " + LOREM * 3)
        else:
            p = root / "invoices" / f"invoice_{i:03d}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["invoice_id", "vendor", "amount"])
                for r in range(50):
                    w.writerow([f"INV-{i:03d}-{r:03d}", rng.choice(VENDORS),
                                rng.randint(100, 99999)])
    return sum(1 for f in root.rglob("*") if f.is_file())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--pdfs", type=int, default=200)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--others", type=int, default=50)
    args = ap.parse_args()

    root = Path(args.root)
    if root.exists() and any(root.rglob("*.pdf")):
        print(f"reusing existing corpus under {root}")
    else:
        root.mkdir(parents=True, exist_ok=True)
        n = generate_corpus(root, args.pdfs, args.pages, args.others)
        print(f"corpus ready: {n} files under {root}")

    client = NetworkDirClient(root_path=str(root), index_mode="content")

    t0 = time.perf_counter()
    tables1 = client.get_schemas()
    t1 = time.perf_counter() - t0
    print(f"run 1 (cold index):                    {t1:8.2f}s  files={len(tables1)}")

    t0 = time.perf_counter()
    client.get_schemas()
    t2 = time.perf_counter() - t0
    print(f"run 2 (unchanged, NO prior catalog):   {t2:8.2f}s   <- old behavior, every reindex")

    prior = {t.name: t.metadata_json for t in tables1}
    t0 = time.perf_counter()
    tables3 = client.get_schemas(prior_catalog=prior)
    t3 = time.perf_counter() - t0
    print(f"run 3 (unchanged, WITH prior catalog): {t3:8.2f}s   <- new behavior")
    if t3 > 0:
        print(f"speedup vs old rerun: {t2 / t3:,.1f}x")

    m1 = {t.name: (t.metadata_json or {}).get("network_dir", {}) for t in tables1}
    m3 = {t.name: (t.metadata_json or {}).get("network_dir", {}) for t in tables3}
    assert set(m1) == set(m3), "file sets differ"
    assert not [n for n in m1 if m1[n].get("keywords") != m3[n].get("keywords")], "keywords differ"
    assert not [n for n in m1 if m1[n].get("content_hash") != m3[n].get("content_hash")], "hashes differ"
    print("catalog equivalence: OK (same files, same keywords, same hashes)")


if __name__ == "__main__":
    main()
