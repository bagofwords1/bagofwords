"""Build a real Outlook `.msg` file from scratch, for tests.

No library in the dependency set can WRITE an MSG (python-oxmsg and olefile
only read), and no permissively licensed sample is at hand, so this is a
minimal, spec-driven writer: an OLE2 compound file ([MS-CFB], v3, 512-byte
sectors, mini stream for small streams) holding the MAPI property streams
[MS-OXMSG] expects — subject/sender/recipients/body/html body, by-value
attachments and an embedded message. python-oxmsg parses the result exactly
like a message Outlook saved, which is what makes a test on it a test of the
real `.msg` path rather than of a mock.

Only what the tests need is implemented; it is not a general MSG writer.
"""
from __future__ import annotations

import struct
from datetime import UTC, datetime

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF
SECTOR = 512
MINI_SECTOR = 64
MINI_CUTOFF = 4096


# ------------------------------------------------------------------ CFB

class _Entry:
    def __init__(self, name: str, kind: int, data: bytes = b""):
        self.name = name
        self.kind = kind          # 1 storage, 2 stream, 5 root
        self.data = data
        self.children: list[_Entry] = []
        self.index = -1
        self.start = ENDOFCHAIN
        self.child = NOSTREAM
        self.right = NOSTREAM


def _flatten(root: _Entry) -> list[_Entry]:
    out: list[_Entry] = []

    def visit(e: _Entry):
        e.index = len(out)
        out.append(e)
        # Children are chained through right-sibling pointers (a degenerate
        # but valid directory tree; readers walk left/right/child blindly).
        for c in e.children:
            visit(c)
    visit(root)
    for e in out:
        if e.children:
            e.child = e.children[0].index
            for a, b in zip(e.children, e.children[1:], strict=False):
                a.right = b.index
    return out


def _pad(data: bytes, size: int) -> bytes:
    return data + b"\x00" * (-len(data) % size)


def write_cfb(root: _Entry) -> bytes:
    entries = _flatten(root)

    # Small streams go to the mini stream; big ones get regular sectors.
    mini_data = b""
    mini_chain: list[int] = []
    for e in entries:
        if e.kind == 2 and 0 < len(e.data) < MINI_CUTOFF:
            e.start = len(mini_data) // MINI_SECTOR
            padded = _pad(e.data, MINI_SECTOR)
            n = len(padded) // MINI_SECTOR
            mini_chain += [e.start + i + 1 for i in range(n - 1)] + [ENDOFCHAIN]
            mini_data += padded
    minifat_bytes = _pad(b"".join(struct.pack("<I", x) for x in mini_chain), SECTOR)
    n_minifat = len(minifat_bytes) // SECTOR if mini_chain else 0

    dir_bytes = b""  # filled after sector numbers are known; size is known now
    n_dir = (len(entries) * 128 + SECTOR - 1) // SECTOR

    big = [e for e in entries if e.kind == 2 and len(e.data) >= MINI_CUTOFF]
    mini_stream = _pad(mini_data, SECTOR)
    n_mini_stream = len(mini_stream) // SECTOR
    n_big = sum(len(_pad(e.data, SECTOR)) // SECTOR for e in big)

    # FAT sizing is circular (the FAT covers its own sectors); iterate.
    n_fat = 1
    while True:
        total = n_fat + n_dir + n_minifat + n_mini_stream + n_big
        need = (total * 4 + SECTOR - 1) // SECTOR
        if need <= n_fat:
            break
        n_fat = need
    assert n_fat <= 109, "fixture too large for the header DIFAT"

    fat: list[int] = []
    sectors: list[bytes] = []

    def chain(n: int, payload: bytes, special: int | None = None) -> int:
        """Append n sectors holding payload, FAT-chained; returns first sector."""
        first = len(sectors)
        for i in range(n):
            fat.append(special if special is not None else (first + i + 1 if i < n - 1 else ENDOFCHAIN))
            sectors.append(payload[i * SECTOR:(i + 1) * SECTOR])
        return first

    fat_first = chain(n_fat, b"\x00" * (n_fat * SECTOR), special=0xFFFFFFFD)  # FATSECT marker
    dir_first = chain(n_dir, b"\x00" * (n_dir * SECTOR))
    minifat_first = chain(n_minifat, minifat_bytes) if n_minifat else ENDOFCHAIN
    mini_first = chain(n_mini_stream, mini_stream) if n_mini_stream else ENDOFCHAIN
    for e in big:
        padded = _pad(e.data, SECTOR)
        e.start = chain(len(padded) // SECTOR, padded)

    root.start = mini_first
    root_size = len(mini_data)

    def entry_bytes(e: _Entry) -> bytes:
        name = e.name.encode("utf-16-le")
        size = root_size if e.kind == 5 else (len(e.data) if e.kind == 2 else 0)
        return struct.pack(
            "<64sHBBIII16sIQQIQ",
            name + b"\x00\x00", len(name) + 2, e.kind, 1,
            NOSTREAM, e.right, e.child, b"\x00" * 16, 0, 0, 0,
            e.start if (e.kind == 5 or size) else ENDOFCHAIN, size,
        )

    dir_bytes = _pad(b"".join(entry_bytes(e) for e in entries), SECTOR)
    for i in range(n_dir):
        sectors[dir_first + i] = dir_bytes[i * SECTOR:(i + 1) * SECTOR]
    fat_bytes = _pad(b"".join(struct.pack("<I", x) for x in fat), SECTOR)
    # Unused FAT slots must read FREESECT, not zero.
    used = len(fat) * 4
    fat_bytes = fat_bytes[:used] + b"\xff" * (len(fat_bytes) - used)
    for i in range(n_fat):
        sectors[fat_first + i] = fat_bytes[i * SECTOR:(i + 1) * SECTOR]

    difat = [fat_first + i for i in range(n_fat)] + [FREESECT] * (109 - n_fat)
    header = struct.pack(
        "<8s16sHHHHH6sIIIIIIIII",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\x00" * 16,
        0x003E, 0x0003, 0xFFFE, 9, 6, b"\x00" * 6, 0,
        n_fat, dir_first, 0, MINI_CUTOFF, minifat_first, n_minifat, ENDOFCHAIN, 0,
    ) + b"".join(struct.pack("<I", x) for x in difat)
    assert len(header) == SECTOR
    return header + b"".join(sectors)


# ------------------------------------------------------------------ MSG

PT_LONG, PT_BOOLEAN, PT_SYSTIME, PT_UNICODE, PT_BINARY = 0x0003, 0x000B, 0x0040, 0x001F, 0x0102


def _filetime(dt: datetime) -> int:
    return int((dt - datetime(1601, 1, 1, tzinfo=UTC)).total_seconds() * 10_000_000)


def _props_stream(header: bytes, fixed: list[tuple[int, int, int]], variable: list[tuple[int, int, int]]) -> bytes:
    """`__properties_version1.0`: header + 16-byte entries.
    fixed: (prop id, type, value); variable: (prop id, type, byte length)."""
    out = [header]
    for pid, ptype, value in fixed:
        out.append(struct.pack("<IIQ", (pid << 16) | ptype, 0x6, value & 0xFFFFFFFFFFFFFFFF))
    for pid, ptype, size in variable:
        out.append(struct.pack("<IIII", (pid << 16) | ptype, 0x6, size + (2 if ptype == PT_UNICODE else 0), 0))
    return b"".join(out)


def _add_var(storage: _Entry, pid: int, ptype: int, data: bytes, variable: list) -> None:
    storage.children.append(_Entry(f"__substg1.0_{pid:04X}{ptype:04X}", 2, data))
    variable.append((pid, ptype, len(data)))


def _u(s: str) -> bytes:
    return s.encode("utf-16-le")


def _message_storage(name: str, kind: int, *, embedded: bool, subject: str, sender_name: str,
                     sender_email: str, to: list[tuple[str, str]],
                     cc: list[tuple[str, str]] | None = None, body: str = "",
                     html: bytes | None = None, attachments: list[dict] | None = None,
                     sent: datetime | None = None) -> _Entry:
    cc = cc or []
    attachments = attachments or []
    sent = sent or datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    st = _Entry(name, kind)
    fixed: list = [(0x340D, PT_LONG, 0x00040000 | 0x00000001),  # STORE_UNICODE_OK | ENTRYID_UNIQUE
                   (0x0039, PT_SYSTIME, _filetime(sent)), (0x0E06, PT_SYSTIME, _filetime(sent)),
                   (0x0E07, PT_LONG, 0x1)]  # MSGFLAG_READ
    var: list = []
    _add_var(st, 0x001A, PT_UNICODE, _u("IPM.Note"), var)
    _add_var(st, 0x0037, PT_UNICODE, _u(subject), var)
    _add_var(st, 0x0E1D, PT_UNICODE, _u(subject), var)
    _add_var(st, 0x0C1A, PT_UNICODE, _u(sender_name), var)
    _add_var(st, 0x0C1F, PT_UNICODE, _u(sender_email), var)
    _add_var(st, 0x5D01, PT_UNICODE, _u(sender_email), var)  # sender SMTP
    _add_var(st, 0x0C1E, PT_UNICODE, _u("SMTP"), var)
    _add_var(st, 0x0E04, PT_UNICODE, _u("; ".join(n for n, _ in to)), var)
    _add_var(st, 0x0E03, PT_UNICODE, _u("; ".join(n for n, _ in cc)), var)
    _add_var(st, 0x1000, PT_UNICODE, _u(body), var)
    if html is not None:
        _add_var(st, 0x1013, PT_BINARY, html, var)
    _add_var(st, 0x1035, PT_UNICODE, _u(f"<{abs(hash(subject))}@fixture.example.com>"), var)

    recipients = [(n, e, 1) for n, e in to] + [(n, e, 2) for n, e in cc]
    for i, (rname, remail, rtype) in enumerate(recipients):
        r = _Entry(f"__recip_version1.0_#{i:08X}", 1)
        rvar: list = []
        _add_var(r, 0x3001, PT_UNICODE, _u(rname), rvar)
        _add_var(r, 0x3003, PT_UNICODE, _u(remail), rvar)
        _add_var(r, 0x39FE, PT_UNICODE, _u(remail), rvar)
        _add_var(r, 0x3002, PT_UNICODE, _u("SMTP"), rvar)
        r.children.insert(0, _Entry("__properties_version1.0", 2, _props_stream(
            b"\x00" * 8, [(0x0C15, PT_LONG, rtype), (0x3000, PT_LONG, i)], rvar)))
        st.children.append(r)

    for i, att in enumerate(attachments):
        a = _Entry(f"__attach_version1.0_#{i:08X}", 1)
        avar: list = []
        afixed: list = [(0x0E21, PT_LONG, i), (0x7FFE, PT_BOOLEAN, 0)]
        if "message" in att:
            afixed.append((0x3705, PT_LONG, 5))  # ATTACH_EMBEDDED_MSG
            inner = att["message"]
            emb = _message_storage("__substg1.0_3701000D", 1, embedded=True, **inner)
            a.children.append(emb)
            _add_var(a, 0x3001, PT_UNICODE, _u(inner["subject"]), avar)
            _add_var(a, 0x3704, PT_UNICODE, _u(att.get("name", "message.msg")), avar)
            _add_var(a, 0x3707, PT_UNICODE, _u(att.get("name", "message.msg")), avar)
        else:
            afixed.append((0x3705, PT_LONG, 1))  # ATTACH_BY_VALUE
            _add_var(a, 0x3701, PT_BINARY, att["data"], avar)
            _add_var(a, 0x3704, PT_UNICODE, _u(att["name"]), avar)
            _add_var(a, 0x3707, PT_UNICODE, _u(att["name"]), avar)
            _add_var(a, 0x3703, PT_UNICODE, _u("." + att["name"].rsplit(".", 1)[-1]), avar)
            if att.get("mime"):
                _add_var(a, 0x370E, PT_UNICODE, _u(att["mime"]), avar)
        a.children.insert(0, _Entry("__properties_version1.0", 2, _props_stream(b"\x00" * 8, afixed, avar)))
        st.children.append(a)

    header = struct.pack("<8sIIII", b"\x00" * 8, len(recipients), len(attachments), len(recipients), len(attachments))
    if not embedded:
        header += b"\x00" * 8
    st.children.insert(0, _Entry("__properties_version1.0", 2, _props_stream(header, fixed, var)))
    if not embedded:
        nameid = _Entry("__nameid_version1.0", 1)
        for sid in ("00020102", "00030102", "00040102"):
            nameid.children.append(_Entry(f"__substg1.0_{sid}", 2, b""))
        st.children.insert(1, nameid)
    return st


def build_msg(*, subject: str, sender_name: str, sender_email: str,
              to: list[tuple[str, str]], cc: list[tuple[str, str]] | None = None,
              body: str = "", html: bytes | None = None,
              attachments: list[dict] | None = None,
              sent: datetime | None = None) -> bytes:
    """Bytes of an Outlook .msg.

    ``attachments`` items are either ``{"name", "data", "mime"}`` (a file) or
    ``{"name", "message": {<the same keyword arguments as build_msg>}}`` (an
    embedded message)."""
    root = _message_storage(
        "Root Entry", 5, embedded=False, subject=subject, sender_name=sender_name,
        sender_email=sender_email, to=to, cc=cc, body=body, html=html,
        attachments=attachments, sent=sent,
    )
    return write_cfb(root)
