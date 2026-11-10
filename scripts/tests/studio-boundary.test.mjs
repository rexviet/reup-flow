import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { request } from 'node:http';
import { connect } from 'node:net';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

const guard = fileURLToPath(new URL('../studio-boundary.mjs', import.meta.url));
const origin = 'http://127.0.0.1:3002';
const call = (headers = {}, method = 'GET') => new Promise((resolve, reject) => {
  const r = request(origin, { method, headers }, response => {
    response.resume(); response.on('end', () => resolve(response.statusCode));
  });
  r.on('error', reject); r.end();
});

test('native HTTP requests and upgrades are guarded before vendor handlers; bind is loopback', async () => {
  const child = spawn(process.execPath, ['--import', guard, '--input-type=module', '-e', `
    import {createServer} from 'node:http';
    const server=createServer((req,res)=>res.end('vendor'));
    server.on('upgrade',(req,socket)=>socket.end('HTTP/1.1 418 Vendor reached\\r\\nConnection: close\\r\\nContent-Length: 0\\r\\n\\r\\n'));
    server.listen({port:3002,host:'0.0.0.0'},()=>console.log(JSON.stringify(server.address())));
  `], { stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    const [chunk] = await once(child.stdout, 'data');
    assert.equal(JSON.parse(chunk.toString()).address, '127.0.0.1');
    assert.equal(await call(), 200);
    assert.equal(await call({ Origin: origin }), 200);
    for (const headers of [{ Host: 'evil.invalid' }, { Origin: 'https://evil.invalid' },
      { Origin: 'null' }, { 'Sec-Fetch-Site': 'cross-site' }, { Origin: [origin, origin] }])
      assert.equal(await call(headers), 403);
    assert.equal(await call({}, 'POST'), 403);
    assert.equal(await call({ Origin: origin }, 'POST'), 200);
    for (const host of ['evil.invalid', '127.0.0.1:3002']) {
      const socket = connect(3002, '127.0.0.1');
      socket.on('connect', () => socket.write(`GET /ws HTTP/1.1\r\nHost: ${host}\r\nOrigin: https://evil.invalid\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n`));
      const [body] = await once(socket, 'data');
      assert.match(body.toString(), /^HTTP\/1.1 403/); socket.destroy();
    }
  } finally {
    child.kill('SIGTERM'); await once(child, 'exit');
  }
});
