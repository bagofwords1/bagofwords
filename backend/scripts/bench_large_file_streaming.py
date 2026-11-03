"""Credential-free GB-scale scan; run from backend with PYTHONPATH=.

The S3 boundary serves a real disk file through get_object/Range/IfMatch.
This validates client streaming, not AWS latency, IAM, or service behavior.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time
import tracemalloc

from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.data_sources.clients.s3_client import S3Client


class FileS3:
    def __init__(self, path):
        self.path, self.bodies = path, []
    def head_object(self, **kwargs):
        stat = self.path.stat()
        return {'ContentLength': stat.st_size, 'ETag': str(stat.st_mtime_ns)}
    def get_object(self, **kwargs):
        assert kwargs.get('IfMatch') == self.head_object()['ETag']
        body = self.path.open('rb')
        offset = int(kwargs.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
        body.seek(offset)
        self.bodies.append(body)
        return {'Body': body, 'ContentRange': f'bytes {offset}-{self.path.stat().st_size-1}/{self.path.stat().st_size}', **self.head_object()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mb', type=int, default=1024)
    parser.add_argument('--source', choices=['network', 's3'], required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='bow-streaming-') as root:
        path = Path(root) / 'events.log'
        line = b'x' * 8191 + b'\n'
        block = line * 128
        with path.open('wb') as output:
            for _ in range(args.mb):
                output.write(block)
            output.write(b'FINAL_NEEDLE\n')
        del block, line
        if args.source == 'network':
            client = NetworkDirClient(root_path=root)
        else:
            client = S3Client(bucket='benchmark', prefix='')
            client._s3 = FileS3(path)
        # Import/warm scanner before measuring scan-only allocations.
        client.grep_files('FINAL_NEEDLE', file_ids=['events.log'], max_bytes_per_file=1024)
        tracemalloc.start()
        started = time.monotonic()
        cursor, pages, hits, transferred = None, 0, [], 0
        while True:
            result = client.grep_files('FINAL_NEEDLE', is_regex=False,
                                      file_ids=['events.log'], cursor=cursor)
            assert not result['skipped_files'], result
            pages += 1
            hits.extend(result['matches'])
            transferred += result['bytes_read']
            cursor = result['next_cursor']
            if not cursor:
                assert result['coverage_complete']
                break
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(hits) == 1 and hits[0]['line_no'] == args.mb * 128 + 1
        assert peak < 32 * 1024 * 1024, peak
        if args.source == 's3':
            assert all(b.closed for b in client._s3.bodies)
        print(json.dumps(dict(source=args.source, file_bytes=path.stat().st_size,
                              pages=pages, hits=len(hits), bytes_read=transferred,
                              peak_scan_mib=round(peak/1024/1024, 3),
                              seconds=round(time.monotonic()-started, 2))))


if __name__ == '__main__':
    main()
