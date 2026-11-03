"""Review regressions through public clients; clocks/filesystem are external boundaries."""
from types import SimpleNamespace
import pytest
from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.data_sources.clients.base import SearchStopped


def test_append_between_pages_preserves_matches_and_context(tmp_path):
    path = tmp_path / 'live.log'
    path.write_bytes(b'HIT first\n' + b'ordinary\n' * 1000 + b'HIT second\n')
    client = NetworkDirClient(root_path=str(tmp_path))
    first = client.grep_files('HIT', file_ids=['live.log'], max_bytes_per_file=1024, before=1)
    with path.open('ab') as out:
        out.write(b'HIT appended\n')
    matches, cursor = list(first['matches']), first['next_cursor']
    while cursor:
        page = client.grep_files('HIT', file_ids=['live.log'], cursor=cursor, before=1)
        assert not page['skipped_files']
        matches.extend(page['matches'])
        cursor = page['next_cursor']
    assert [m['line'] for m in matches] == ['HIT first', 'HIT second', 'HIT appended']
    assert matches[-1]['before'] == ['HIT second']


def test_append_during_scan_keeps_snapshot_matches(tmp_path):
    path = tmp_path / 'live.log'
    path.write_bytes(b'HIT first\nordinary\nHIT second\n')
    calls = 0
    def append_once():
        nonlocal calls
        calls += 1
        if calls == 5:
            with path.open('ab') as out:
                out.write(b'HIT appended\n')
        return False
    result = NetworkDirClient(root_path=str(tmp_path)).grep_files(
        'HIT', file_ids=['live.log'], cancel_check=append_once)
    assert not result['skipped_files']
    assert [m['line'] for m in result['matches']] == ['HIT first', 'HIT second']


def test_search_result_limit_returns_normal_bounded_results(tmp_path):
    for i in range(4):
        (tmp_path / f'match-{i}.txt').write_text('ordinary')
    result = NetworkDirClient(root_path=str(tmp_path)).search_files('match', max_results=2)
    assert len(result) == 2


def test_directory_search_shares_total_byte_budget(tmp_path):
    # More than 100 MB across individually small files: each file alone fits.
    block = b'x' * 8191 + b'\n'
    for i in range(7):
        with (tmp_path / f'{i}.log').open('wb') as output:
            for _ in range(2048):
                output.write(block)
    with pytest.raises(SearchStopped) as stopped:
        NetworkDirClient(root_path=str(tmp_path)).search_files('absent')
    assert stopped.value.stop_reason == 'byte_budget'


def test_deadline_limited_regex_returns_resumable_time_budget(tmp_path, monkeypatch):
    import app.data_sources.clients._streaming_grep as scanner
    (tmp_path / 'live.log').write_bytes(b'a' * 100000 + b'!\n')
    # Fix wall time so a real expensive regex receives a sub-50ms allowance.
    ticks = iter([0.0])
    monkeypatch.setattr(scanner, 'time', SimpleNamespace(monotonic=lambda: next(ticks, 0.099)))
    result = NetworkDirClient(root_path=str(tmp_path)).grep_files(
        '(a+)+$', file_ids=['live.log'], time_budget_seconds=0.1)
    assert result['stop_reason'] == 'time_budget'
    assert result['next_cursor'] and not result['skipped_files']


@pytest.mark.parametrize('mutation', ['overwrite', 'overwrite_and_append', 'truncate', 'rotate'])
def test_resume_rejects_changes_to_consumed_prefix(tmp_path, mutation):
    path = tmp_path / 'live.log'
    path.write_bytes(b'HIT original\n' + b'ordinary\n' * 40000)
    client = NetworkDirClient(root_path=str(tmp_path))
    first = client.grep_files('HIT', file_ids=['live.log'], max_bytes_per_file=150000)
    assert first['next_cursor']
    if mutation in ('overwrite', 'overwrite_and_append'):
        with path.open('r+b') as output:
            output.seek(70000)  # Neither the header nor the checkpoint boundary.
            output.write(b'changed!')
        if mutation.endswith('append'):
            with path.open('ab') as output:
                output.write(b'new content\n')
    elif mutation == 'truncate':
        with path.open('r+b') as output:
            output.truncate(200000)  # Still beyond cursor, but shorter than original file.
    else:
        path.rename(tmp_path / 'rotated.log')
        path.write_bytes(b'ordinary\n' * 50000)
    result = client.grep_files('HIT', file_ids=['live.log'], cursor=first['next_cursor'])
    assert not result['matches']
    assert result['skipped_files'][0]['reason'] == 'file_changed'


def test_append_validation_reads_consumed_prefix_only_when_changed(tmp_path):
    path = tmp_path / 'live.log'
    path.write_bytes(b'ordinary\n' * 50000 + b'HIT\n')
    client = NetworkDirClient(root_path=str(tmp_path))
    first = client.grep_files('HIT', file_ids=['live.log'], max_bytes_per_file=150000)
    unchanged = client.grep_files('HIT', file_ids=['live.log'], max_bytes_per_file=150000,
                                 cursor=first['next_cursor'])
    assert unchanged['verification_bytes'] < 65536
    with path.open('ab') as output:
        output.write(b'HIT appended\n')
    grown = client.grep_files('HIT', file_ids=['live.log'], cursor=unchanged['next_cursor'])
    assert grown['verification_bytes'] >= 300000
    assert [m['line'] for m in grown['matches']] == ['HIT', 'HIT appended']


def test_overwrite_during_scan_discards_matches(tmp_path):
    path = tmp_path / 'live.log'
    path.write_bytes(b'HIT first\nordinary\nHIT second\n')
    calls = 0
    def overwrite_once():
        nonlocal calls
        calls += 1
        if calls == 5:
            with path.open('r+b') as output:
                output.write(b'BAD first\n')
                output.seek(0, 2)
                output.write(b'HIT appended\n')
        return False
    result = NetworkDirClient(root_path=str(tmp_path)).grep_files(
        'HIT', file_ids=['live.log'], cancel_check=overwrite_once)
    assert not result['matches']
    assert result['skipped_files'][0]['reason'] == 'file_changed'


def test_search_returns_filename_hits_after_content_budget(tmp_path):
    (tmp_path / 'early.log').write_bytes(b'ordinary\n' * 10000)
    (tmp_path / 'needle.log').write_text('irrelevant')
    client = NetworkDirClient(root_path=str(tmp_path))
    with pytest.raises(SearchStopped) as stopped:
        client.search_files('needle', max_total_bytes=1024)
    assert stopped.value.stop_reason == 'byte_budget'
    assert [f['id'] for f in stopped.value.files] == ['needle.log']


def test_append_verification_can_be_cancelled_without_losing_checkpoint(tmp_path):
    path = tmp_path / 'live.log'
    path.write_bytes(b'ordinary\n' * 50000 + b'HIT\n')
    client = NetworkDirClient(root_path=str(tmp_path))
    first = client.grep_files('HIT', file_ids=['live.log'], max_bytes_per_file=150000)
    with path.open('ab') as output:
        output.write(b'continued\n')
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        return checks > 2
    stopped = client.grep_files('HIT', file_ids=['live.log'], cursor=first['next_cursor'], cancel_check=cancel)
    assert stopped['stop_reason'] == 'cancelled' and stopped['next_cursor']
    assert stopped['verification_bytes'] >= 65536
    resumed = client.grep_files('HIT', file_ids=['live.log'], cursor=stopped['next_cursor'])
    assert resumed['total_matches'] == 1 and not resumed['skipped_files']
