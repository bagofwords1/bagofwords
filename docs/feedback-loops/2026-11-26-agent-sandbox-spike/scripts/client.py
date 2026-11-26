import sys, time, hashlib, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen"))
import grpc, httpx, ssl
import process_pb2 as pb, process_pb2_grpc as pbg

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ["SANDBOX_HOST"] + ":8443"  # the sandbox server's address
CA = open(os.path.join(HERE, "ca.pem"), "rb").read()  # read from the host with `caddy-host.sh ca`

def channel():
    return grpc.secure_channel(HOST, grpc.ssl_channel_credentials(root_certificates=CA),
        options=[("grpc.keepalive_time_ms", 30000), ("grpc.max_receive_message_length", 64 << 20)])

def md(sid, tok): return (("x-sandbox-id", sid), ("authorization", f"Bearer {tok}"))

def http(sid, tok):
    ctx = ssl.create_default_context(cadata=CA.decode())
    return httpx.Client(base_url=f"https://{HOST}", verify=ctx, timeout=300,
                        headers={"X-Sandbox-Id": sid, "Authorization": f"Bearer {tok}"})

def start(stub, sid, tok, cmd, on=None, cancel_after=None, env=None):
    t0 = time.time(); pid = None; out = []; code = None
    call = stub.Start(pb.StartRequest(config=pb.ProcessConfig(command=cmd, env_vars=env or {})), metadata=md(sid, tok))
    for ev in call:
        k = ev.WhichOneof("event")
        if k == "init": pid = ev.init.process_id
        elif k in ("stdout", "stderr"):
            data = getattr(ev, k); out.append((time.time() - t0, k, data))
            if on: on(time.time() - t0, k, data)
        elif k == "exit": code = ev.exit.exit_code
        if cancel_after and code is None and time.time() - t0 > cancel_after:
            stub.SendSignal(pb.SendSignalRequest(process_id=pid, signal=pb.SIGNAL_SIGTERM), metadata=md(sid, tok)); cancel_after = None
    return code, out, time.time() - t0

if __name__ == "__main__":
    sid, tok, test = sys.argv[1], sys.argv[2], sys.argv[3]
    stub = pbg.ProcessServiceStub(channel())
    if test in ("exec", "pexec"):
        P = "http://10.213.77.3:3128"
        env = {k: P for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")} if test == "pexec" else {}
        t0 = time.time()
        r = stub.Execute(pb.ExecuteRequest(config=pb.ProcessConfig(command=sys.argv[4:], env_vars=env)), metadata=md(sid, tok), timeout=900)
        print(f"({time.time()-t0:.1f}s)", end=" ")
        print("exit", r.exit_code); print(r.stdout.decode(errors="replace")[-3000:]); print(r.stderr.decode(errors="replace")[-3000:], file=sys.stderr)
    elif test == "live":
        code, out, dt = start(stub, sid, tok, ["sh", "-c", "for i in 1 2 3 4 5; do echo tick $i; sleep 1; done; echo err >&2; exit 3"],
                              on=lambda t, k, d: print(f"  +{t:.2f}s {k} {d!r}"))
        print("exit", code, f"{dt:.2f}s")
    elif test == "cancel":
        code, out, dt = start(stub, sid, tok, ["sh", "-c", "trap 'echo got-term; exit 9' TERM; while true; do echo run; sleep 1; done"], cancel_after=3)
        print("exit", code, f"{dt:.2f}s", [o[2] for o in out][-2:])
    elif test == "badtoken":
        try: stub.Execute(pb.ExecuteRequest(config=pb.ProcessConfig(command=["true"])), metadata=md(sid, "wrong"), timeout=10); print("UNEXPECTED ok")
        except grpc.RpcError as e: print("grpc", e.code())
        print("rest", http(sid, "wrong").get("/v1/files/").status_code)
    elif test == "long":
        n = int(sys.argv[4])
        code, out, dt = start(stub, sid, tok, ["sh", "-c", f"i=0; while [ $i -lt {n} ]; do echo t$i; i=$((i+30)); sleep 30; done; echo done"])
        print("long exit", code, f"{dt:.1f}s chunks={len(out)} last={out[-1][2]!r}")
    elif test == "quiet":   # no output at all for N seconds: does an idle stream survive?
        n = int(sys.argv[4]); code, out, dt = start(stub, sid, tok, ["sh", "-c", f"sleep {n}; echo done"]); print("quiet exit", code, f"{dt:.1f}s", out[-1][2])
    elif test == "files":
        c = http(sid, tok); size = 100 * 1024 * 1024
        blob = os.urandom(1024 * 1024) * 100; h = hashlib.sha256(blob).hexdigest()
        t = time.time(); r = c.put("/v1/files/big/in.bin", content=blob, headers={"Content-Type": "application/octet-stream"}); print("PUT", r.status_code, f"{time.time()-t:.1f}s")
        r = stub.Execute(pb.ExecuteRequest(config=pb.ProcessConfig(command=["sha256sum", "big/in.bin"])), metadata=md(sid, tok)); print("sha match in sandbox:", r.stdout.decode().split()[0] == h)
        t = time.time(); hh = hashlib.sha256(); n = 0
        with c.stream("GET", "/v1/files/big/in.bin") as r:
            for ch in r.iter_bytes(1 << 20): hh.update(ch); n += len(ch)
        print("GET", r.status_code, n == size, hh.hexdigest() == h, f"{time.time()-t:.1f}s")
        c.put("/v1/files/outputs/a/b/c.txt?parents=true", content=b"hi"); c.put("/v1/files/outputs/top.csv", content=b"x,y\n")
        r = c.get("/v1/files/outputs?recursive=true"); print("LIST", r.status_code, json.dumps(r.json())[:900])
        print("DELETE", c.delete("/v1/files/big?recursive=true").status_code)
