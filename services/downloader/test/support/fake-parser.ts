import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';

export interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
}

export type Responder = (req: IncomingMessage, res: ServerResponse) => void;

/**
 * A real HTTP server standing in for the parser fork.
 *
 * Not a mock of undici: the things this phase has to get right -- envelope
 * unwrapping, status-code classification, timeouts, and above all "one call
 * makes exactly one request" -- are properties of an actual request/response
 * exchange. A stubbed transport would let a retry loop or a wrong route pass
 * unnoticed, which is precisely what the no-retry test exists to catch.
 */
export class FakeParser {
  private server: Server | null = null;
  private readonly sockets = new Set<import('node:net').Socket>();
  readonly requests: RecordedRequest[] = [];
  private responder: Responder = (_req, res) => {
    res.writeHead(500).end('no responder installed');
  };

  async start(): Promise<void> {
    this.server = createServer((req, res) => {
      const url = new URL(req.url ?? '/', 'http://localhost');
      this.requests.push({
        method: req.method ?? 'GET',
        path: url.pathname,
        query: url.searchParams,
      });
      this.responder(req, res);
    });
    // The timeout test deliberately leaves a request unanswered. Without
    // tracking and destroying sockets here, `server.close()` waits for that
    // connection forever and the suite hangs in afterAll rather than failing.
    this.server.on('connection', (socket) => {
      this.sockets.add(socket);
      socket.on('close', () => this.sockets.delete(socket));
    });
    await new Promise<void>((resolve) => this.server!.listen(0, '127.0.0.1', resolve));
  }

  async stop(): Promise<void> {
    if (!this.server) return;
    const server = this.server;
    this.server = null;
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
    await new Promise<void>((resolve, reject) =>
      server.close((err) => (err ? reject(err) : resolve())),
    );
  }

  get baseUrl(): string {
    const { port } = this.server!.address() as AddressInfo;
    return `http://127.0.0.1:${port}`;
  }

  reset(): void {
    this.requests.length = 0;
  }

  /** Replies with the parser's success envelope: `{code, router, data}`. */
  respondWithEnvelope(data: unknown, code = 200): void {
    this.responder = (req, res) => {
      const router = new URL(req.url ?? '/', 'http://localhost').pathname;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ code, router, data }));
    };
  }

  /**
   * Replies exactly as the fork does on ANY internal exception: HTTP 400
   * whose body carries the static, information-free `ErrorResponseModel`.
   * See app/api/models/APIResponseModel.py -- `message` is a class default
   * and `str(e)` is never interpolated into it.
   */
  respondWithParserError(status = 400): void {
    this.responder = (req, res) => {
      const url = new URL(req.url ?? '/', 'http://localhost');
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(
        JSON.stringify({
          detail: {
            code: status,
            message: 'An error occurred.',
            support: 'Please contact us on Github: https://github.com/Evil0ctal/Douyin_TikTok_Download_API',
            time: '2026-08-25 00:00:00',
            router: url.pathname,
            params: Object.fromEntries(url.searchParams),
          },
        }),
      );
    };
  }

  respondWith(responder: Responder): void {
    this.responder = responder;
  }

  /** Accepts the request and never answers -- the client's timeout must fire. */
  respondNever(): void {
    this.responder = () => {
      /* intentionally silent */
    };
  }
}
