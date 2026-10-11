import http.server, json, sys, datetime
class H(http.server.BaseHTTPRequestHandler):
    def _h(self):
        with open(sys.argv[2],'a') as f: f.write(json.dumps({"t":datetime.datetime.now().isoformat(),"m":self.command,"p":self.path})+"\n")
        self.send_response(200); self.send_header("content-type","application/json"); self.end_headers(); self.wfile.write(b'{"canary":"internal-secret"}')
    do_GET=do_POST=do_HEAD=_h
    def log_message(self,*a): pass
http.server.ThreadingHTTPServer(("0.0.0.0",int(sys.argv[1])),H).serve_forever()
