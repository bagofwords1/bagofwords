"""Stream the fields actually emitted by TShark into one Parquet per capture."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import signal
import subprocess
import shutil
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic

from app.data_sources.fast.parquet_crypto import encryption_properties, decryption_properties
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
    pq.write_table(
        pa.Table.from_pylist(rows, schema=schema),
        path,
        compression="zstd",
        encryption_properties=encryption_properties(),
    )


class DecodeError(ValueError):
    """A capture could not be decoded.

    `reason` is shown to users in `indexed_files`. `retryable` is False when the
    same file version would fail the same way again (malformed input, a resource
    limit), so indexing does not spend the decode budget on it every refresh.
    """

    def __init__(self, reason, retryable=False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


# Must match _tshark_worker.SANDBOX_UNAVAILABLE_EXIT / CPU_LIMIT_SECONDS.
SANDBOX_UNAVAILABLE_EXIT = 78
CPU_LIMIT_SECONDS = 300
WALL_LIMIT_SECONDS = 600


def _decode_failure(returncode, killed):
    if returncode == SANDBOX_UNAVAILABLE_EXIT:
        return DecodeError("The packet decoder sandbox is unavailable on this host", retryable=True)
    if killed == "time" or returncode == -signal.SIGXCPU:
        return DecodeError(
            f"Decoding exceeded the time limit ({CPU_LIMIT_SECONDS} s CPU, {WALL_LIMIT_SECONDS} s total)"
        )
    if killed == "memory" or returncode in (-signal.SIGABRT, -signal.SIGKILL):
        return DecodeError("Decoding exceeded the memory limit")
    if returncode < 0:
        return DecodeError(f"The decoder crashed on this capture (signal {-returncode})")
    return DecodeError(f"TShark could not decode this capture (exit {returncode})")


def _watch_decoder(proc, done, killed):
    deadline = monotonic() + WALL_LIMIT_SECONDS
    while not done.wait(1):
        if monotonic() >= deadline:
            killed["reason"] = "time"
            proc.kill()
            return
        # Darwin does not implement RLIMIT_AS. Enforce the resident memory
        # ceiling from the trusted parent while the decoder runs in Seatbelt.
        if sys.platform == "darwin":
            rss = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(proc.pid)], capture_output=True).stdout.strip()
            if rss.isdigit() and int(rss) > 2 * 1024 * 1024:
                killed["reason"] = "memory"
                proc.kill()
                return


def convert_capture(source: Path, capture_id: str, destination: Path, checkpoint=None):
    """Small batches allow a new field even in the very last packet to join the schema."""
    tshark = shutil.which("tshark")
    if not tshark:
        raise FileNotFoundError(2, "TShark is not installed", "tshark")
    launcher = Path(__file__).with_name("_tshark_worker.py")
    # Stage next to the destination (the connection's private cache directory),
    # not in the system temp dir, which may be a small shared tmpfs.
    with TemporaryDirectory(dir=destination.parent) as spool, source.open("rb") as capture:
        with subprocess.Popen(
            [sys.executable, "-I", "-S", str(launcher), str(Path(tshark).resolve())],
            stdin=capture,
            cwd=spool,
            env={"HOME": spool, "WIRESHARK_CONFIG_DIR": spool, "LC_ALL": "C", "TZ": "UTC"},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as proc:
            done = Event()
            killed = {}
            watchdog = Thread(target=_watch_decoder, args=(proc, done, killed), daemon=True)
            watchdog.start()
            parts, rows = [], []
            batch_bytes = 0
            incomplete = False
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
            except etree.XMLSyntaxError:
                # Usually the decoder died mid-stream and its exit status says
                # why. If it is still running, its output itself was unusable.
                incomplete = True
                if proc.poll() is None:
                    killed["reason"] = "parser"
                    proc.kill()
            except BaseException:
                proc.kill()
                raise
            finally:
                proc.stdout.close()
                returncode = proc.wait()
                done.set()
                watchdog.join()
            if killed.get("reason") == "parser" or (incomplete and not returncode):
                raise DecodeError("TShark returned an incomplete decode of this capture")
            if returncode or killed:
                raise _decode_failure(returncode, killed.get("reason"))
        if not parts:
            _write_batch(rows, destination)
            return
        if rows:
            part = Path(spool) / "last.parquet"
            _write_batch(rows, part)
            parts.append(str(part))
        decrypt = decryption_properties()
        schema = pa.unify_schemas([pq.read_schema(p, decryption_properties=decrypt) for p in parts])
        with pq.ParquetWriter(
            destination, schema, compression="zstd", encryption_properties=encryption_properties()
        ) as writer:
            for part in parts:
                with pq.ParquetFile(part, decryption_properties=decrypt) as reader:
                    for batch in reader.iter_batches():
                        arrays = [
                            batch.column(f.name) if f.name in batch.schema.names else pa.nulls(len(batch), f.type)
                            for f in schema
                        ]
                        writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
                if checkpoint:
                    checkpoint()
