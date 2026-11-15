"""Stream the fields actually emitted by TShark into one Parquet per capture."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Timer

import duckdb
from lxml import etree
import pyarrow as pa
import pyarrow.parquet as pq


FRAME_SCHEMA = pa.schema(
    [
        ("capture_id", pa.string()),
        ("frame_number", pa.int64()),
        ("epoch_seconds", pa.decimal128(24, 9)),
        ("frame_bytes", pa.int64()),
    ]
)


def _packet_row(packet, capture_id):
    values = defaultdict(list)
    for field in packet.iter("field"):
        name = field.get("name")
        if name:
            values[name].append(field.get("show", field.get("value", "")))
    # Even usually-single protocol fields can repeat (e.g. tunneled IP).
    # Use the same list type in every batch and capture for each dynamic field.
    return {
        **values,
        "capture_id": capture_id,
        "frame_number": int(values["frame.number"][0]),
        "epoch_seconds": Decimal(values["frame.time_epoch"][0]) if values.get("frame.time_epoch") else None,
        "frame_bytes": int(values["frame.len"][0]),
    }


def _write_batch(rows, path):
    fields = sorted({key for row in rows for key in row} - set(FRAME_SCHEMA.names))
    schema = pa.schema([*FRAME_SCHEMA, *(pa.field(f, pa.list_(pa.string())) for f in fields)])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")


def convert_capture(source: Path, capture_id: str, destination: Path, checkpoint=None):
    """Small batches allow a new field even in the very last packet to join the schema."""
    with TemporaryDirectory(dir=destination.parent) as spool:
        with subprocess.Popen(
            ["tshark", "-n", "-r", str(source), "-T", "pdml"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as proc:
            timer = Timer(3600, proc.kill)
            timer.daemon = True
            timer.start()
            parts, rows = [], []
            batch_bytes = 0
            try:
                for _, packet in etree.iterparse(
                    proc.stdout, events=("end",), tag="packet", no_network=True, resolve_entities=False
                ):
                    row = _packet_row(packet, capture_id)
                    rows.append(row)
                    batch_bytes += sum(len(v) for vals in row.values() if isinstance(vals, list) for v in vals)
                    packet.clear()
                    # Drop already consumed siblings from the parser's root.
                    while packet.getprevious() is not None:
                        del packet.getparent()[0]
                    if len(rows) >= 1024 or batch_bytes >= 4 * 1024 * 1024:
                        part = Path(spool) / f"{len(parts)}.parquet"
                        _write_batch(rows, part)
                        parts.append(str(part))
                        rows.clear()
                        batch_bytes = 0
                        if checkpoint:
                            checkpoint()
            except etree.XMLSyntaxError as exc:
                proc.kill()
                raise ValueError("TShark did not return a complete capture decode") from exc
            except BaseException:
                proc.kill()
                raise
            finally:
                proc.stdout.close()
                returncode = proc.wait()
                timer.cancel()
            if returncode:
                raise ValueError(f"TShark could not decode this capture (exit {returncode})")
        if not parts:
            _write_batch(rows, destination)
            return
        if rows:
            part = Path(spool) / "last.parquet"
            _write_batch(rows, part)
            parts.append(str(part))
        with duckdb.connect() as con:
            con.read_parquet(parts, union_by_name=True).write_parquet(str(destination), compression="zstd")
