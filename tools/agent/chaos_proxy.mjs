// TCP chaos proxy: listens on :3001, forwards to 127.0.0.1:3000.
// SIGUSR1 destroys every active connection (simulating a network drop /
// mobile connection loss) while continuing to accept new connections.
import net from 'node:net';

const LISTEN = Number(process.env.CHAOS_LISTEN || 3001);
const TARGET = Number(process.env.CHAOS_TARGET || 3000);
const sockets = new Set();

const server = net.createServer((client) => {
  const upstream = net.connect(TARGET, '127.0.0.1');
  sockets.add(client); sockets.add(upstream);
  client.pipe(upstream); upstream.pipe(client);
  const cleanup = () => { sockets.delete(client); sockets.delete(upstream); client.destroy(); upstream.destroy(); };
  client.on('error', cleanup); upstream.on('error', cleanup);
  client.on('close', cleanup); upstream.on('close', cleanup);
});

process.on('SIGUSR1', () => {
  console.log(`[chaos] severing ${sockets.size} sockets`);
  for (const s of sockets) s.destroy();
  sockets.clear();
});

server.listen(LISTEN, () => console.log(`[chaos] proxying :${LISTEN} -> :${TARGET} (pid ${process.pid})`));
