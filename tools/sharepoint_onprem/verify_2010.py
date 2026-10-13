#!/usr/bin/env python3
"""Read-only smoke test; run with the deployed backend's environment and Python.

No provisioning, writes, credential echo, or response dumps. Lists.asmx and
Copy.asmx must be enabled. Supply a known file and independently computed hash.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', default=os.environ.get('SP_SITE_URL'))
    parser.add_argument('--library', default=os.environ.get('SP_LIBRARY', '*'))
    parser.add_argument('--folder', default=os.environ.get('SP_FOLDER', ''))
    parser.add_argument('--auth', choices=['ntlm', 'kerberos'], default='ntlm')
    parser.add_argument('--file', required=True, help='Scoped path or server-relative ID of a known file')
    parser.add_argument('--sha256', required=True, help='SHA-256 of the original file, computed independently')
    parser.add_argument('--min-files', type=int, default=1)
    parser.add_argument('--page-size', type=int, default=2, help='Small pages exercise continuation; 1–200')
    parser.add_argument('--require-paging', action='store_true', help='Fail unless a continuation request succeeds')
    parser.add_argument('--allow-http', action='store_true', help='Isolated HTTP lab only')
    args = parser.parse_args()
    if not args.site or not 1 <= args.page_size <= 200 or args.min_files < 0:
        parser.error('Set SP_SITE_URL or --site; page-size must be 1–200 and min-files nonnegative.')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', args.sha256):
        parser.error('--sha256 must be an independently computed 64-character hex digest.')
    if args.auth == 'ntlm' and not (os.environ.get('SP_USERNAME') and os.environ.get('SP_PASSWORD')):
        parser.error('Set SP_USERNAME and SP_PASSWORD in the environment. Do not pass passwords as arguments.')

    from app.data_sources.clients.sharepoint_onprem_client import SharepointOnpremClient
    client = SharepointOnpremClient(site_url=args.site, drive_name=args.library, folder_path=args.folder,
        api_mode='2010', recursive=True, allow_http=args.allow_http, kerberos=args.auth == 'kerberos',
        username=os.environ.get('SP_USERNAME'), password=os.environ.get('SP_PASSWORD'),
        principal=os.environ.get('SP_PRINCIPAL'))
    client._adapter.page_size = args.page_size
    # Hook the external HTTP boundary. Record counts only, never payloads/headers.
    counters = {'successful_soap_requests': 0, 'continuation_requests': 0}
    def observed(response, **kwargs):
        if response.status_code == 200 and response.request.headers.get('SOAPAction'):
            counters['successful_soap_requests'] += 1
            body = ET.fromstring(response.request.body)
            if any(n.tag.rsplit('}', 1)[-1] == 'Paging' for n in body.iter()):
                counters['continuation_requests'] += 1
    client._session().hooks['response'].append(observed)
    try:
        result = client.test_connection()
        if not result['success']:
            raise ValueError(result['message'])
        files = client.list_files()
        if len(files) < args.min_files:
            raise ValueError('Fewer files were listed than --min-files requires.')
        if len(files) >= client.max_catalog_objects:
            raise ValueError('The catalog cap was reached. Narrow --folder before checking completeness.')
        content, name, _ = client.read_raw_bytes(args.file)
        if hashlib.sha256(content).hexdigest().lower() != args.sha256.lower():
            raise ValueError('Downloaded bytes do not match the independently supplied SHA-256.')
        # Verify that the same known file is discoverable through filename search.
        matches = client.search_files(name)
        if not any(item['name'].casefold() == name.casefold() for item in matches):
            raise ValueError('Known file was downloaded but was not found by scoped filename search.')
        if args.require_paging and not counters['continuation_requests']:
            raise ValueError('No continuation request was observed; choose a folder with more items than page-size.')
        print(json.dumps({'success': True, 'api_mode': '2010', 'auth': args.auth,
            'files_listed': len(files), 'download_bytes': len(content), 'sha256_match': True,
            'filename_search': True, **counters}, indent=2))
    finally:
        if hasattr(client._local, 'session'):
            client._local.session.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print provider bodies, request headers, or a traceback with locals.
        print(json.dumps({'success': False, 'error_type': type(exc).__name__, 'message': str(exc)}))
        sys.exit(1)
