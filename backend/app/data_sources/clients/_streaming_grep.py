"""Bounded text scanning for filesystem and S3 streams.

Byte budgets are soft at line boundaries: at most one MAX_LINE_BYTES line and
`after` context lines are read past a budget. Oversized lines are reported as
incomplete coverage, never silently searched only at their beginning.
"""
from __future__ import annotations

import base64
import json
import time
from collections import deque

import regex

from app.data_sources.clients._file_source_common import GlobScopeError
from app.data_sources.clients._grep_common import _clip, _scope_hash, compile_pattern

CHUNK_BYTES = 64 * 1024
MAX_LINE_BYTES = 1024 * 1024


class ScanStopped(Exception):
    pass


class FileChanged(Exception):
    pass


class Lines:
    def __init__(self, stream, offset, check):
        self.stream, self.offset, self.check = stream, offset, check
        self.buffer = b''
        self.eof = False
        self.bytes_read = 0

    def next(self):
        while True:
            self.check()
            end = self.buffer.find(b'\n')
            if end >= 0:
                end += 1
                if end > MAX_LINE_BYTES:
                    raise ScanStopped('line_too_long')
                raw, self.buffer = self.buffer[:end], self.buffer[end:]
                break
            if len(self.buffer) > MAX_LINE_BYTES:
                raise ScanStopped('line_too_long')
            if self.eof:
                if not self.buffer:
                    return None
                raw, self.buffer = self.buffer, b''
                break
            data = self.stream.read(CHUNK_BYTES)
            initial = self.offset == 0 and self.bytes_read == 0
            self.bytes_read += len(data)
            if initial and b"\x00" in data[:8192]:
                raise ScanStopped('binary')
            self.eof = not data
            self.buffer += data
        start = self.offset
        self.offset += len(raw)
        return start, self.offset, raw.rstrip(b'\r\n').decode('utf-8', errors='replace')


def run_streaming_grep(*, candidates, open_stream, pattern, is_regex=True,
                       ignore_case=False, before=0, after=0, max_matches=100,
                       max_matches_per_file=50, max_files=500,
                       max_bytes_per_file=20_000_000, scope_key='', cursor=None,
                       time_budget_seconds=60, cancel_check=None,
                       first_match_per_file=False, max_total_bytes=100_000_000):
    # Keep existing syntax validation; use the timeout-capable regex engine.
    compile_pattern(pattern, is_regex=is_regex, ignore_case=ignore_case)
    rx = regex.compile(pattern if is_regex else regex.escape(pattern),
                       regex.IGNORECASE if ignore_case else 0)
    before, after = max(0, min(before, 20)), max(0, min(after, 20))
    scope = _scope_hash(f'{scope_key}|{before}|{after}|{first_match_per_file}')
    state = {}
    if cursor:
        try:
            if len(cursor) > 128000:
                raise ValueError()
            state = json.loads(base64.urlsafe_b64decode(cursor))
            valid = (
                state['h'] == scope and state['format'] == 1
                and isinstance(state['f'], str) and isinstance(state['v'], str)
                and isinstance(state['o'], int) and state['o'] >= 0
                and isinstance(state['l'], int) and state['l'] >= 0
                and isinstance(state['b'], list) and len(state['b']) <= before
                and all(isinstance(s, str) and len(s) <= 500 for s in state['b'])
            )
            if not valid:
                raise ValueError()
        except Exception:
            raise ValueError('Invalid or incompatible grep cursor; restart the same pattern and scope.')
    result = dict(matches=[], total_matches=0, files_scanned=0, files_with_matches=0,
                  skipped_files=[], truncated=False, stop_reason='complete',
                  next_cursor=None, bytes_read=0, verification_bytes=0, coverage_complete=True)
    deadline = time.monotonic() + max(0.001, time_budget_seconds)

    def check():
        if cancel_check and cancel_check():
            raise ScanStopped('cancelled')
        if time.monotonic() >= deadline:
            raise ScanStopped('time_budget')

    def stop(entry, offset, line, context, version, reason):
        encoded_version = version.checkpoint(offset) if hasattr(version, 'checkpoint') else version
        payload = dict(format=1, h=scope, f=entry['id'], o=offset, l=line,
                       b=list(context), v=encoded_version)
        result.update(truncated=True, coverage_complete=False, stop_reason=reason,
                      next_cursor=base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode())

    ordered = sorted({e['id']: e for e in candidates}.values(), key=lambda e: e['id'])
    if state and not any(e['id'] == state['f'] for e in ordered):
        result['skipped_files'].append(dict(file_id=state['f'], reason='file_changed'))
    for entry in ordered:
        fid = entry['id']
        if state and fid < state['f']:
            continue
        resumed = state if state.get('f') == fid else {}
        offset, line = resumed.get('o', 0), resumed.get('l', 0)
        context = deque(resumed.get('b', []), maxlen=before)
        version = resumed.get('v', '')
        if entry.get('skip_reason'):
            result['skipped_files'].append(dict(file_id=fid, reason=entry['skip_reason']))
            continue
        if result['bytes_read'] >= max_total_bytes:
            stop(entry, offset, line, context, version, 'byte_budget')
            break
        if result['files_scanned'] >= max_files:
            stop(entry, offset, line, context, version, 'max_files')
            break
        emitted, reader, stream = 0, None, None
        try:
            check()
            with open_stream(entry, offset, version, check=check) as (stream, version, size):
                if offset > size:
                    raise FileChanged()
                reader = Lines(stream, offset, check)
                pending = deque()
                result['files_scanned'] += 1
                start_offset = offset
                while offset < size or pending:
                    check()
                    if offset - start_offset >= max_bytes_per_file or result['bytes_read'] + reader.bytes_read >= max_total_bytes:
                        stop(entry, offset, line, context, version, 'byte_budget')
                        break
                    if len(result['matches']) >= max_matches or emitted >= max_matches_per_file:
                        stop(entry, offset, line, context, version, 'max_matches')
                        break
                    row = pending.popleft() if pending else reader.next()
                    if row is None:
                        break
                    _, end, text = row
                    if line == 0 and '\x00' in text[:8192]:
                        raise ScanStopped('binary')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ScanStopped('time_budget')
                    allowance = min(0.05, remaining)
                    try:
                        hit = rx.search(text, timeout=allowance)
                    except TimeoutError:
                        # A deadline-shortened allowance says nothing about a
                        # pattern's intrinsic cost. Preserve this line for retry.
                        reason = 'time_budget' if allowance < 0.05 or time.monotonic() >= deadline else 'regex_timeout'
                        raise ScanStopped(reason)
                    if hit:
                        # Look ahead for context without consuming those lines for
                        # matching. On resume, byte position remains before them.
                        while len(pending) < after:
                            following = reader.next()
                            if following is None:
                                break
                            pending.append(following)
                        clipped, truncated = _clip(text)
                        result['matches'].append(dict(file_id=fid, path=entry.get('path', fid),
                            line_no=line+1, line=clipped, line_truncated=truncated,
                            before=list(context), after=[_clip(r[2])[0] for r in list(pending)[:after]]))
                        emitted += 1
                    context.append(_clip(text)[0])
                    offset, line = end, line+1
                    if hasattr(stream, 'commit'):
                        stream.commit(offset)
                    if hit and first_match_per_file:
                        break
        except (GlobScopeError, FileChanged, ScanStopped, OSError) as exc:
            if stream is None:
                result['verification_bytes'] += getattr(exc, 'verification_bytes', 0)
            reason = ('access_denied' if isinstance(exc, GlobScopeError) else
                      'file_changed' if isinstance(exc, FileChanged) else
                      str(exc) if isinstance(exc, ScanStopped) else 'unreadable')
            if reason in ('time_budget', 'cancelled'):
                stop(entry, offset, line, context, version, reason)
            else:
                result['skipped_files'].append(dict(file_id=fid, reason=reason))
                if reason == 'file_changed':
                    result['matches'] = [m for m in result['matches'] if m['file_id'] != fid]
                    emitted = 0
                    result.update(next_cursor=None, stop_reason='complete')
        except Exception:
            result['skipped_files'].append(dict(file_id=fid, reason='unreadable'))
        finally:
            if reader:
                result['bytes_read'] += reader.bytes_read
            if stream:
                result['verification_bytes'] += getattr(stream, 'verification_bytes', 0)
        if emitted:
            result['files_with_matches'] += 1
        if result['next_cursor']:
            break
    result['total_matches'] = len(result['matches'])
    if result['skipped_files']:
        result['coverage_complete'] = False
    return result
