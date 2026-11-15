// Installed only in the pinned Studio child. No vendor files are modified.
import { Server } from 'node:http';

const PORT = 3002;
const HOST = `127.0.0.1:${PORT}`;
const ORIGIN = `http://${HOST}`;
const emit = Server.prototype.emit;
const listen = Server.prototype.listen;

function values(request, name) {
  const found = [];
  for (let i = 0; i < request.rawHeaders.length; i += 2)
    if (request.rawHeaders[i].toLowerCase() === name)
      found.push(request.rawHeaders[i + 1]);
  return found;
}

export function allowed(request) {
  const hosts = values(request, 'host');
  const origins = values(request, 'origin');
  if (hosts.length !== 1 || hosts[0] !== HOST) return false;
  if (origins.length > 1 || (origins.length === 1 && origins[0] !== ORIGIN)) return false;
  if (request.headers['sec-fetch-site'] === 'cross-site') return false;
  return ['GET', 'HEAD', 'OPTIONS'].includes(request.method) || origins[0] === ORIGIN;
}

Server.prototype.emit = function (event, ...args) {
  if (['request', 'upgrade', 'checkContinue', 'checkExpectation'].includes(event)) {
    const [request, response] = args;
    if (request.socket.localPort === PORT && !allowed(request)) {
      if (event === 'upgrade') {
        response.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
      } else {
        response.writeHead(403, { 'Cache-Control': 'no-store', Connection: 'close' });
        response.end('Forbidden origin');
      }
      return true;
    }
  }
  return Reflect.apply(emit, this, [event, ...args]);
};

Server.prototype.listen = function (...args) {
  if (args[0] && typeof args[0] === 'object' && Number(args[0].port) === PORT) {
    args[0] = { ...args[0], host: '127.0.0.1' };
  } else if (Number(args[0]) === PORT) {
    args[0] = PORT;
    if (typeof args[1] === 'string') args[1] = '127.0.0.1';
    else args.splice(1, 0, '127.0.0.1');
  }
  return Reflect.apply(listen, this, args);
};
