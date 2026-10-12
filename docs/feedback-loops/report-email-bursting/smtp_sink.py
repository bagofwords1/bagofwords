"""Local SMTP sink for the bursting feedback loop.

Accepts every message on 127.0.0.1:<port> and writes it verbatim to
<out_dir>/<n>.eml. No relay, no auth, nothing leaves the machine.

    python smtp_sink.py 2531 /path/to/mail
"""
import pathlib
import sys
import time

from aiosmtpd.controller import Controller


class Sink:
    def __init__(self, out: pathlib.Path):
        self.out = out
        self.n = 0

    async def handle_DATA(self, server, session, envelope):
        self.n += 1
        path = self.out / f"{int(time.time() * 1000)}-{self.n:04d}.eml"
        rcpts = ",".join(envelope.rcpt_tos)
        path.write_bytes(f"X-Envelope-To: {rcpts}\r\n".encode() + envelope.content)
        return "250 OK"


if __name__ == "__main__":
    port, out = int(sys.argv[1]), pathlib.Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    ctl = Controller(Sink(out), hostname="127.0.0.1", port=port)
    ctl.start()
    print(f"smtp sink on 127.0.0.1:{port} -> {out}", flush=True)
    try:
        while True:
            time.sleep(3600)
    finally:
        ctl.stop()
