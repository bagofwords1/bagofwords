// Minimal compressing reverse proxy for performance loops — mirrors the
// production Caddyfile (`encode gzip` on everything except event streams), so
// byte counts and transfer times in a sandbox match a real deployment instead
// of uvicorn's uncompressed FileResponse.
//
//   node tools/agent/gzip_proxy.mjs <listen_port> <target_port>
import http from 'node:http';
import zlib from 'node:zlib';

const [listen, target] = process.argv.slice(2).map(Number);
const COMPRESSIBLE = /text\/|javascript|json|css|svg|xml|html/i;

http.createServer((req, res) => {
  const up = http.request({ host: '127.0.0.1', port: target, method: req.method, path: req.url, headers: req.headers }, (ur) => {
    const type = ur.headers['content-type'] || '';
    const accepts = /\bgzip\b/.test(req.headers['accept-encoding'] || '');
    const stream = /event-stream/i.test(type) || /event-stream/i.test(req.headers.accept || '');
    if (accepts && !stream && !ur.headers['content-encoding'] && COMPRESSIBLE.test(type) && ur.statusCode !== 304 && ur.statusCode !== 204) {
      const headers = { ...ur.headers, 'content-encoding': 'gzip', vary: 'Accept-Encoding' };
      delete headers['content-length'];
      res.writeHead(ur.statusCode, headers);
      ur.pipe(zlib.createGzip({ level: 5 })).pipe(res);
    } else {
      res.writeHead(ur.statusCode, ur.headers);
      ur.pipe(res);
    }
  });
  up.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
  req.pipe(up);
}).listen(listen, '127.0.0.1', () => console.log(`gzip proxy :${listen} -> :${target}`));
