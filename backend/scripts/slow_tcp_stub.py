"""A TCP endpoint that accepts and then stalls: models a slow / dropping
database server for connection-test repros without real credentials.

psycopg2 connects, sends its startup packet, and blocks waiting for the
server's reply; after DELAY seconds the stub closes the socket and the client
errors out. Each test therefore costs ~DELAY seconds — a *deterministic,
optimistic* stand-in for a slow or unreachable warehouse (a firewalled host
with no RST is far worse: psycopg2 has no connect_timeout here, so it waits
the OS TCP timeout, ~2 minutes).

Usage: python scripts/slow_tcp_stub.py [delay_seconds] [port]
Defaults: 1.0s on 127.0.0.1:55445.
"""
import socket
import sys
import threading
import time

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 55445

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", PORT))
s.listen(100)
print(f"slow stub on 127.0.0.1:{PORT} delay={DELAY}s", flush=True)


def handle(conn):
    try:
        time.sleep(DELAY)
        conn.close()
    except Exception:
        pass


while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
