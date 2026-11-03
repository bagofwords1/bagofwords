"""Append-aware local text stream with a compact, portable content checkpoint.

Hash complete 64 KiB blocks into a SHA-256 chain; retain only the partial block.
Unchanged files resume by seeking. If metadata changed because the file grew,
verify the consumed prefix in bounded reads before accepting the append. This
extra integrity I/O is time/cancellation bounded, and is reported separately
from scan bytes. It is necessary to distinguish appends from overwrite+append.
"""
import hashlib
import json
import os

from app.data_sources.clients._streaming_grep import FileChanged

BLOCK = 64 * 1024
EMPTY = bytes(32)


def stat_identity(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


class LocalGrepStream:
    def __init__(self, stream, path, offset, version, check):
        self.verification_bytes = 0
        try:
            self._initialize(stream, path, offset, version, check)
        except Exception as exc:
            # Initialization can stop during prefix verification, before the
            # context manager yields the stream to the scanner.
            exc.verification_bytes = self.verification_bytes
            raise

    def _initialize(self, stream, path, offset, version, check):
        self.stream, self.path, self.check = stream, path, check
        self.stat = stat_identity(os.fstat(stream.fileno()))
        self.size = self.stat[2]
        self.offset = offset
        self.chain, self.tail, self.buffer = EMPTY, b'', b''
        self.verification_bytes = 0
        if version:
            try:
                previous = json.loads(version)
                if previous['format'] != 1 or previous['offset'] != offset:
                    raise ValueError()
                old = previous['stat']
                if old[:2] != self.stat[:2] or self.size < old[2]:
                    raise ValueError()
                self.chain = bytes.fromhex(previous['chain'])
                if len(self.chain) != 32:
                    raise ValueError()
                if old != self.stat:
                    if self.size <= old[2]:
                        raise ValueError()
                    self.verify(previous['digest'], offset)
                tail_start = offset - offset % BLOCK
                self.tail = os.pread(stream.fileno(), offset - tail_start, tail_start)
                self.verification_bytes += len(self.tail)
                if hashlib.sha256(self.chain + self.tail).hexdigest() != previous['digest']:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise FileChanged()
        elif offset:
            raise FileChanged()
        stream.seek(offset)

    def read(self, size):
        self.check()
        # A scan has a fixed EOF, even when a writer appends concurrently.
        data = self.stream.read(min(size, max(0, self.size - self.stream.tell())))
        self.buffer += data
        return data

    def commit(self, offset):
        count = offset - self.offset
        if count < 0 or count > len(self.buffer):
            raise ValueError('Invalid streaming checkpoint position')
        self.tail += self.buffer[:count]
        self.buffer = self.buffer[count:]
        while len(self.tail) >= BLOCK:
            self.chain = hashlib.sha256(self.chain + self.tail[:BLOCK]).digest()
            self.tail = self.tail[BLOCK:]
        self.offset = offset

    def checkpoint(self, offset):
        if offset != self.offset:
            raise ValueError('Checkpoint must follow consumed text')
        return json.dumps(dict(format=1, stat=self.stat, offset=offset,
                               chain=self.chain.hex(),
                               digest=hashlib.sha256(self.chain + self.tail).hexdigest()))

    def verify(self, expected_digest, offset):
        chain, pos = EMPTY, 0
        while pos + BLOCK <= offset:
            self.check()
            block = os.pread(self.stream.fileno(), BLOCK, pos)
            self.verification_bytes += len(block)
            if len(block) != BLOCK:
                raise FileChanged()
            chain = hashlib.sha256(chain + block).digest()
            pos += BLOCK
        self.check()
        tail = os.pread(self.stream.fileno(), offset-pos, pos)
        self.verification_bytes += len(tail)
        if len(tail) != offset-pos or hashlib.sha256(chain + tail).hexdigest() != expected_digest:
            raise FileChanged()

    def validate(self):
        current = stat_identity(os.fstat(self.stream.fileno()))
        if stat_identity(self.path.stat())[:2] != self.stat[:2]:
            raise FileChanged()
        if current != self.stat:
            if current[:2] != self.stat[:2] or current[2] <= self.size:
                raise FileChanged()
            self.verify(hashlib.sha256(self.chain + self.tail).hexdigest(), self.offset)
