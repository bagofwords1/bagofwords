"""Public connector contracts; only the S3 transport is stubbed."""
import io
import hashlib
import pytest
from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.data_sources.clients.s3_client import S3Client

class BoundedBody(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size=-1):
        assert 0 <= size <= 1024 * 1024, "scan must never request an unbounded download"
        data = super().read(size)
        self.bytes_read += len(data)
        return data


class S3Transport:
    def __init__(self, data):
        self.data, self.bodies, self.ranges = data, [], []
    def head_object(self, **kwargs):
        return {'ContentLength': len(self.data), 'ETag': hashlib.md5(self.data).hexdigest()}
    def get_object(self, **kwargs):
        start = int(kwargs.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
        self.ranges.append(start)
        if 'IfMatch' in kwargs:
            assert kwargs['IfMatch'] == self.head_object()['ETag']
        body = BoundedBody(self.data[start:])
        self.bodies.append(body)
        return {'Body': body, 'ContentRange': f'bytes {start}-{len(self.data)-1}/{len(self.data)}', **self.head_object()}

@pytest.fixture(params=['network', 's3'])
def source(request, tmp_path):
    def make(data):
        if request.param == 'network':
            path = tmp_path / 'events.log'
            path.write_bytes(data)
            return NetworkDirClient(root_path=str(tmp_path)), lambda new: path.write_bytes(new), None
        client = S3Client(bucket='test', prefix='')
        transport = S3Transport(data)
        client._s3 = transport
        return client, lambda new: setattr(transport, 'data', new), transport
    return make

def test_large_text_resumes_without_losing_matches_or_context(source):
    data = (('ordinary line\n' * 300) + 'before\nHIT café\nafter\n') * 3
    client, _, transport = source(data.encode())
    cursor, found = None, []
    for _ in range(40):
        result = client.grep_files('HIT', file_ids=['events.log'], before=1, after=1,
                                   max_bytes_per_file=1024, cursor=cursor)
        assert not result['skipped_files']
        found.extend(result['matches'])
        cursor = result['next_cursor']
        if cursor is None:
            break
    assert cursor is None
    assert len(found) == 3
    assert len({m['line_no'] for m in found}) == 3
    assert all(m['before'] == ['before'] and m['after'] == ['after'] for m in found)
    if transport:
        assert any(start > 0 for start in transport.ranges)
        assert all(body.closed for body in transport.bodies)

def test_changed_file_cannot_resume_stale_cursor(source):
    client, replace, _ = source(b'ordinary\n' * 1000 + b'HIT\n')
    first = client.grep_files('HIT', file_ids=['events.log'], max_bytes_per_file=1024)
    assert first['next_cursor']
    replace(b'changed\n' * 1000 + b'HIT\n')
    result = client.grep_files('HIT', file_ids=['events.log'], cursor=first['next_cursor'])
    assert any(s['reason'] == 'file_changed' for s in result['skipped_files'])
    assert not result['matches']

@pytest.mark.parametrize('per_file,total', [(1, 100), (100, 1), (2, 2)])
def test_result_caps_resume_without_losing_adjacent_matches(source, per_file, total):
    client, _, _ = source(b'first\nHIT one\nHIT two\nHIT three\nlast\n')
    found, cursor = [], None
    for _ in range(10):
        result = client.grep_files('HIT', file_ids=['events.log'], before=1, after=1,
                                  max_matches_per_file=per_file, max_matches=total, cursor=cursor)
        found.extend(result['matches'])
        cursor = result['next_cursor']
        if not cursor:
            break
    assert [m['line_no'] for m in found] == [2, 3, 4]
    assert [m['before'] for m in found] == [['first'], ['HIT one'], ['HIT two']]
    assert [m['after'] for m in found] == [['HIT two'], ['HIT three'], ['last']]


def test_utf8_and_pattern_cross_transport_chunk_boundary(source):
    client, _, _ = source(('x' * 65535 + 'éHIT\r\nend').encode())
    result = client.grep_files('éHIT$', file_ids=['events.log'])
    assert result['total_matches'] == 1
    assert result['matches'][0]['line_no'] == 1
    assert result['matches'][0]['line_truncated']
    assert result['coverage_complete']


@pytest.mark.parametrize('data,reason', [(b'a\n\x00binary', 'binary'),
    (b'x' * (1024 * 1024 + 1) + b'HIT\n', 'line_too_long')])
def test_unscannable_content_is_explicit(source, data, reason):
    client, _, transport = source(data)
    result = client.grep_files('HIT', file_ids=['events.log'])
    assert not result['coverage_complete']
    assert result['skipped_files'][0]['reason'] == reason
    if transport:
        assert all(body.closed for body in transport.bodies)


def test_cancelled_scan_returns_checkpoint_and_closes_stream(source):
    client, _, transport = source(b'ordinary\n' * 20000)
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        return checks > 20
    result = client.grep_files('HIT', file_ids=['events.log'], cancel_check=cancel)
    assert result['stop_reason'] == 'cancelled'
    assert result['next_cursor']
    assert not result['coverage_complete']
    if transport:
        assert all(body.closed for body in transport.bodies)


def test_regex_timeout_is_explicit_and_bounded(source):
    client, _, _ = source(b'a' * 100000 + b'!\n')
    result = client.grep_files('(a+)+$', file_ids=['events.log'])
    assert not result['coverage_complete']
    assert result['skipped_files'][0]['reason'] == 'regex_timeout'


def test_network_search_reaches_past_old_index_and_file_caps(tmp_path):
    path = tmp_path / 'large.log'
    path.write_bytes(b'ordinary line\n' * 100000 + b'uniqueneedle\n')
    client = NetworkDirClient(root_path=str(tmp_path), max_file_mb=1)
    assert [e['id'] for e in client.search_files('uniqueneedle')] == ['large.log']
    meta = client.get_schemas()[0].metadata_json['network_dir']
    assert meta['content_coverage'] == 'prefix_sample'
    assert meta['sample_max_bytes'] == 200000
    assert 'uniqueneedle' not in meta['keywords']


def test_s3_index_samples_large_text_without_whole_download():
    client = S3Client(bucket='test', prefix='', max_file_mb=1)
    transport = S3Transport(b'valuablekeyword\n' + b'ordinary\n' * 200000)
    client._s3 = transport
    class Paginator:
        def paginate(self, **kwargs):
            yield {'Contents': [{'Key': 'large.log', 'Size': len(transport.data)}]}
    transport.get_paginator = lambda _: Paginator()
    meta = client.get_schemas()[0].metadata_json['s3']
    assert 'valuablekeyword' in meta['keywords']
    assert meta['content_coverage'] == 'prefix_sample'
    assert meta['sample_max_bytes'] == 200000
    assert sum(body.bytes_read for body in transport.bodies) <= 200000
    assert all(body.closed for body in transport.bodies)


def test_empty_file_completes(source):
    client, _, _ = source(b'')
    result = client.grep_files('HIT', file_ids=['events.log'])
    assert result['coverage_complete'] and not result['next_cursor']


def test_unicode_context_cursor_round_trips(source):
    lines = ['😀' * 500] * 20 + ['HIT one', 'HIT two', 'end']
    client, _, _ = source(('\n'.join(lines) + '\n').encode())
    first = client.grep_files('HIT', file_ids=['events.log'], before=20, max_matches=1)
    second = client.grep_files('HIT', file_ids=['events.log'], before=20,
                              max_matches=1, cursor=first['next_cursor'])
    assert second['matches'][0]['line_no'] == 22
    assert second['matches'][0]['before'] == lines[1:21]


def test_s3_null_version_does_not_hide_changed_content():
    client = S3Client(bucket='test', prefix='')
    transport = S3Transport(b'ordinary\n' * 1000)
    head = transport.head_object
    transport.head_object = lambda **kwargs: {**head(**kwargs), 'VersionId': 'null'}
    client._s3 = transport
    first = client.grep_files('HIT', file_ids=['events.log'], max_bytes_per_file=1024)
    transport.data = b'different' * 1000
    result = client.grep_files('HIT', file_ids=['events.log'], cursor=first['next_cursor'])
    assert result['skipped_files'][0]['reason'] == 'file_changed'


def test_deadline_is_resumable_and_closes_stream(source, monkeypatch):
    from types import SimpleNamespace
    import app.data_sources.clients._streaming_grep as scanner
    ticks = 0
    def clock():
        nonlocal ticks
        ticks += 1
        return ticks / 100
    client, _, transport = source(b'ordinary\n' * 1000)
    monkeypatch.setattr(scanner, 'time', SimpleNamespace(monotonic=clock))
    result = client.grep_files('HIT', file_ids=['events.log'], time_budget_seconds=0.3)
    assert result['stop_reason'] == 'time_budget' and result['next_cursor']
    if transport:
        assert all(body.closed for body in transport.bodies)


def test_catalog_cap_is_reported(tmp_path):
    for i in range(3):
        (tmp_path / f'{i}.txt').write_text('ordinary\n')
    client = NetworkDirClient(root_path=str(tmp_path), max_catalog_objects=2)
    result = client.grep_files('HIT')
    assert not result['coverage_complete']
    assert result['stop_reason'] == 'catalog_limit'


@pytest.mark.asyncio
async def test_async_cancellation_closes_s3_response():
    import asyncio
    import threading
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    class BlockingBody(io.BytesIO):
        def read(self, size=-1):
            started.set()
            assert release.wait(5)
            return super().read(size)
        def close(self):
            super().close()
            closed.set()
    transport = S3Transport(b'ordinary\n' * 1000)
    transport.get_object = lambda **kwargs: {'Body': BlockingBody(transport.data)}
    client = S3Client(bucket='test', prefix='')
    client._s3 = transport
    task = asyncio.create_task(client.agrep_files('HIT', file_ids=['events.log']))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(closed.wait, 5)


def test_s3_rejects_ignored_range_and_closes_body():
    client = S3Client(bucket='test', prefix='')
    transport = S3Transport(b'ordinary\n' * 1000)
    client._s3 = transport
    first = client.grep_files('HIT', file_ids=['events.log'], max_bytes_per_file=1024)
    get = transport.get_object
    def ignored_range(**kwargs):
        response = get(**kwargs)
        response.pop('ContentRange')
        return response
    transport.get_object = ignored_range
    result = client.grep_files('HIT', file_ids=['events.log'], cursor=first['next_cursor'])
    assert not result['coverage_complete']
    assert result['skipped_files'][0]['reason'] == 'unreadable'
    assert all(body.closed for body in transport.bodies)


def test_network_search_marks_budget_exhaustion_incomplete(tmp_path):
    from app.data_sources.clients.base import SearchStopped
    (tmp_path / 'large.log').write_bytes((b'x' * 8191 + b'\n') * 2600 + b'NEEDLE\n')
    client = NetworkDirClient(root_path=str(tmp_path))
    with pytest.raises(SearchStopped) as stopped:
        client.search_files('NEEDLE')
    assert stopped.value.stop_reason == 'byte_budget'
    assert not stopped.value.files
