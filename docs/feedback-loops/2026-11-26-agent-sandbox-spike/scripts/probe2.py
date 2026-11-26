import socket,sys
def tcp(h,p):
    s=socket.socket(); s.settimeout(2)
    try: s.connect((h,p)); return "OPEN"
    except Exception as e: return type(e).__name__
    finally: s.close()
T={"other":(sys.argv[1],8080),"caddy":("10.213.77.2",8443),"egress":("10.213.77.3",3128),"host-gw:22":("10.213.77.1",22),"host-ip:22":("SANDBOX_HOST",22),"devbox:22":("BOW_HOST",22),"metadata":("169.254.169.254",80),"internet":("1.1.1.1",443)}
print({k:tcp(*v) for k,v in T.items()})
