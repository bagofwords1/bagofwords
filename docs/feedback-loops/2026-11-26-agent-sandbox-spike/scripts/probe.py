import socket, sys, json
def tcp(h, p, t=3):
    s = socket.socket(); s.settimeout(t)
    try: s.connect((h, p)); return "OPEN"
    except Exception as e: return type(e).__name__
    finally: s.close()
def dns(n):
    try: return socket.gethostbyname(n)
    except Exception as e: return type(e).__name__
targets = {"other-sandbox:8080": ("OTHER", 8080), "caddy:8443": ("10.213.77.2", 8443), "egress:3128": ("10.213.77.3", 3128),
  "host-gw:22": ("10.213.77.1", 22), "host-ip:22": ("SANDBOX_HOST", 22), "host-ip:8443": ("SANDBOX_HOST", 8443), "devbox:22": ("BOW_HOST", 22),
  "metadata:80": ("169.254.169.254", 80), "internet 1.1.1.1:443": ("1.1.1.1", 443), "smtp 142.250.102.26:25": ("142.250.102.26", 25)}
other = sys.argv[1]
print({k: tcp(other if h == "OTHER" else h, p) for k, (h, p) in targets.items()}, "dns:", dns("pypi.org"))
