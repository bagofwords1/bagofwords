"""Fake Oracle listener: accepts TCP connections and never sends a byte —
from the client's side, a login the server never finishes (the 10g symptom)."""
import socket, sys
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", int(sys.argv[1]))); srv.listen(16)
held = []
print("silent listener on", sys.argv[1], flush=True)
while True:
    c, a = srv.accept(); held.append(c); print("accepted", a, flush=True)
