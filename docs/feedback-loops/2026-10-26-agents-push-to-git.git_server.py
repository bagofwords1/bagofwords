"""Tiny smart-HTTP(S) git server for the agents-push-to-git loop.

Serves every bare repo under ROOT through `git http-backend` (push enabled per
repo with `git config http.receivepack true`). BOW only takes a PAT over
https://, so pass a cert + key to serve TLS and start the backend with
GIT_SSL_CAINFO=<cert> so its git trusts it.

    python3 <this file> ROOT PORT [CERT KEY]
"""
import os, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = sys.argv[1]; PORT = int(sys.argv[2])
class H(BaseHTTPRequestHandler):
    def _run(self):
        path, _, qs = self.path.partition('?')
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        env = dict(os.environ, GIT_PROJECT_ROOT=ROOT, GIT_HTTP_EXPORT_ALL='1', REQUEST_METHOD=self.command,
                   PATH_INFO=path, QUERY_STRING=qs, CONTENT_TYPE=self.headers.get('Content-Type', ''),
                   CONTENT_LENGTH=str(len(body)), REMOTE_USER='bow', REMOTE_ADDR='127.0.0.1',
                   HTTP_CONTENT_ENCODING=self.headers.get('Content-Encoding', ''))
        out = subprocess.run(['git', 'http-backend'], input=body, env=env, capture_output=True).stdout
        head, _, rest = out.partition(b'\r\n\r\n')
        status = 200; hdrs = []
        for line in head.decode().split('\r\n'):
            k, _, v = line.partition(':')
            if k.lower() == 'status': status = int(v.split()[0])
            elif k: hdrs.append((k, v.strip()))
        self.send_response(status)
        for k, v in hdrs: self.send_header(k, v)
        self.send_header('Content-Length', str(len(rest))); self.end_headers(); self.wfile.write(rest)
    do_GET = do_POST = _run
import ssl
srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
if len(sys.argv) > 4:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(sys.argv[3], sys.argv[4])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
