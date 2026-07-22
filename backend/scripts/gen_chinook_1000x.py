#!/usr/bin/env python
"""Build a huge Chinook: duplicate Invoice + InvoiceLine 1000x.

Produces ~412k invoices / ~2.24M invoice lines with distinct primary keys and
per-copy shifted invoice dates, keeping all FKs (Customer, Track) valid.
One planted needle row (a memo track title) proves grep-ability later.

Usage: python scripts/gen_chinook_1000x.py [SRC] [DST] [COPIES]
"""
import shutil
import sqlite3
import sys
import time

SRC = sys.argv[1] if len(sys.argv) > 1 else "tests/config/chinook.sqlite"
DST = sys.argv[2] if len(sys.argv) > 2 else "/tmp/chinook_1000x.sqlite"
COPIES = int(sys.argv[3]) if len(sys.argv) > 3 else 1000

t0 = time.time()
shutil.copy(SRC, DST)
con = sqlite3.connect(DST)
con.executescript(
    """
    PRAGMA journal_mode=OFF;
    PRAGMA synchronous=OFF;
    PRAGMA cache_size=-200000;
    """
)

(max_inv,) = con.execute("SELECT max(InvoiceId) FROM Invoice").fetchone()
(max_line,) = con.execute("SELECT max(InvoiceLineId) FROM InvoiceLine").fetchone()

# Copy k (1..COPIES-1) offsets ids by k*step and shifts dates back k days so
# time-window slicing has real spread.
for k in range(1, COPIES):
    con.execute(
        """
        INSERT INTO Invoice (InvoiceId, CustomerId, InvoiceDate, BillingAddress, BillingCity,
                             BillingState, BillingCountry, BillingPostalCode, Total)
        SELECT InvoiceId + ?, CustomerId, datetime(InvoiceDate, '-' || ? || ' days'),
               BillingAddress, BillingCity, BillingState, BillingCountry, BillingPostalCode, Total
        FROM Invoice WHERE InvoiceId <= ?
        """,
        (k * max_inv, k, max_inv),
    )
    con.execute(
        """
        INSERT INTO InvoiceLine (InvoiceLineId, InvoiceId, TrackId, UnitPrice, Quantity)
        SELECT InvoiceLineId + ?, InvoiceId + ?, TrackId, UnitPrice, Quantity
        FROM InvoiceLine WHERE InvoiceLineId <= ?
        """,
        (k * max_line, k * max_inv, max_line),
    )
    if k % 200 == 0:
        con.commit()
        print(f"  copy {k}/{COPIES} ({time.time()-t0:.1f}s)")

# Planted needle: one unique track referenced by exactly one late invoice line.
con.execute("INSERT INTO Track (TrackId, Name, MediaTypeId, GenreId, Composer, Milliseconds, Bytes, UnitPrice) "
            "VALUES (999999, 'ZZZ-NEEDLE-TRACK-424242', 1, 1, 'Nobody', 1000, 1000, 0.99)")
con.execute("INSERT INTO InvoiceLine (InvoiceLineId, InvoiceId, TrackId, UnitPrice, Quantity) "
            "VALUES (?, ?, 999999, 0.99, 1)", (COPIES * max_line + 1, max_inv))
con.commit()

inv = con.execute("SELECT count(*) FROM Invoice").fetchone()[0]
lines = con.execute("SELECT count(*) FROM InvoiceLine").fetchone()[0]
con.execute("VACUUM")
con.close()
import os
print(f"DONE {DST}: {inv} invoices, {lines} invoice lines, "
      f"{os.path.getsize(DST)//(1<<20)} MB in {time.time()-t0:.1f}s")
