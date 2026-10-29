import base64, hashlib, httpx, sys
from urllib.parse import urlparse, parse_qs
B="http://localhost:8765/api"; c=httpx.Client(base_url=B, timeout=30)
email, pw = "live@example.com", "Test1234!"
print("register", c.post("/auth/register", json={"email":email,"password":pw,"name":"Live"}).status_code)
def login(p): r=c.post("/auth/jwt/login", data={"username":email,"password":p}); return r.json()["access_token"]
jwt=login(pw); H=lambda t:{"Authorization":f"Bearer {t}"}
orgs=c.get("/users/whoami",headers=H(jwt)).json()["organizations"]
if not orgs:
    c.post("/organizations",json={"name":"Live"},headers=H(jwt)); orgs=c.get("/users/whoami",headers=H(jwt)).json()["organizations"]
org=orgs[0]["id"]; OH={**H(jwt),"X-Organization-Id":org}
mk=lambda n,sc: c.post("/oauth/clients",json={"name":n,"redirect_uris":["http://localhost:4173/cb"],"scopes":sc},headers=OH).json()
a,b=mk("A","app"),mk("B","app mcp")
v="d"*64; ch=base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
appr=lambda h,cid,sc: c.post("/oauth/authorize",headers=h,json={"client_id":cid,"redirect_uri":"http://localhost:4173/cb","scope":sc,"code_challenge":ch})
r=appr(H(jwt),a["client_id"],"app"); print("consent via login JWT:", r.status_code)
code=parse_qs(urlparse(r.json()["redirect_url"]).query)["code"][0]
tok=c.post("/oauth/token",data={"grant_type":"authorization_code","code":code,"client_id":a["client_id"],"code_verifier":v,"redirect_uri":"http://localhost:4173/cb"}).json()
print("OAuth token on app API:", c.get("/users/whoami",headers=H(tok["access_token"])).status_code)
print("#17 consent for client B via client A's OAuth token:", appr(H(tok["access_token"]),b["client_id"],"app mcp").status_code)
print("#14 password change:", c.patch("/users/me",json={"password":"Changed1234!"},headers=H(jwt)).status_code)
print("#14 old OAuth access token:", c.get("/users/whoami",headers=H(tok["access_token"])).status_code)
r=c.post("/oauth/token",data={"grant_type":"refresh_token","refresh_token":tok["refresh_token"],"client_id":a["client_id"]}); print("#14 old refresh token:", r.status_code, r.json().get("error"))
print("new login works:", c.get("/users/whoami",headers=H(login("Changed1234!"))).status_code)
