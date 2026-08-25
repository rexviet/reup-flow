import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';

export interface RecordedDownload {
  path: string;
  query: URLSearchParams;
  headers: Record<string, string | string[] | undefined>;
}

export type CdnResponder = (req: IncomingMessage, res: ServerResponse) => void;

/**
 * A real HTTP server standing in for `*.douyinvod.com`.
 *
 * A real server, not a stubbed transport, for the same reason `FakeParser` is
 * one: the things phase 6 has to get right are properties of an actual
 * request/response exchange. "One GET per attempt", "the Referer header
 * arrives", "a connection dropped mid-body is not a complete download" and
 * "a signed URL that has expired answers 403" cannot be observed through a
 * mock of undici -- and the truncation case in particular depends on what the
 * HTTP client does when a socket dies with bytes still owed.
 */
export class FakeCdn {
  private server: Server | null = null;
  private readonly sockets = new Set<import('node:net').Socket>();
  readonly downloads: RecordedDownload[] = [];
  private responder: CdnResponder = (_req, res) => {
    res.writeHead(500).end('no responder installed');
  };

  async start(): Promise<void> {
    this.server = createServer((req, res) => {
      const url = new URL(req.url ?? '/', 'http://localhost');
      this.downloads.push({
        path: url.pathname,
        query: url.searchParams,
        headers: req.headers,
      });
      this.responder(req, res);
    });
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

  /** A URL on this server that carries `token`, so a responder can gate on it. */
  url(path: string, token?: string): string {
    const url = new URL(path, this.baseUrl);
    if (token !== undefined) url.searchParams.set('token', token);
    return url.toString();
  }

  reset(): void {
    this.downloads.length = 0;
  }

  respondWith(responder: CdnResponder): void {
    this.responder = responder;
  }

  /** Serves `payload` whole, with a truthful Content-Length. */
  serveBytes(payload: Buffer): void {
    this.responder = (_req, res) => {
      res.writeHead(200, {
        'content-type': 'video/mp4',
        'content-length': String(payload.length),
      });
      res.end(payload);
    };
  }

  /**
   * Serves `payload` in chunks, pausing after the first until `gate` settles.
   *
   * The only way to observe a download WHILE it is in flight -- which is when
   * "is the lease still ours?", "was the job cancelled?" and "is the temp file
   * visible under the final name?" all have interesting answers.
   */
  serveGated(payload: Buffer, gate: Promise<unknown>, firstChunk = 1024): void {
    this.responder = (_req, res) => {
      res.writeHead(200, {
        'content-type': 'video/mp4',
        'content-length': String(payload.length),
      });
      res.write(payload.subarray(0, firstChunk));
      void gate.then(() => res.end(payload.subarray(firstChunk)));
    };
  }

  /**
   * Advertises `payload.length`, sends only `sent` bytes, then destroys the
   * socket -- a CDN connection dropped mid-body.
   *
   * This is the real shape of a truncated download, and it is deliberately
   * distinct from the "advertised more than it delivered, cleanly" case: here
   * the HTTP client itself sees a premature close, so the failure is caught by
   * the transport rather than by the byte-count check. Both have to be tested,
   * and the mutation suite uses the difference to prove which mechanism is
   * carrying which test (plan §5 rule 4).
   */
  serveTruncated(payload: Buffer, sent: number): void {
    this.responder = (_req, res) => {
      res.writeHead(200, {
        'content-type': 'video/mp4',
        'content-length': String(payload.length),
      });
      res.write(payload.subarray(0, sent));
      // No `end`. Destroy the socket so the client sees the body stop early.
      setTimeout(() => res.socket?.destroy(), 20);
    };
  }

  /**
   * Answers `status` -- 403 or 410 in practice -- with the small XML body a
   * CDN really returns for an expired signature.
   */
  serveRefusal(status: number): void {
    this.responder = (_req, res) => {
      const body = `<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>`;
      res.writeHead(status, {
        'content-type': 'application/xml',
        'content-length': String(Buffer.byteLength(body)),
      });
      res.end(body);
    };
  }

  /**
   * Serves `payload` only to URLs carrying `token`; everything else gets 403.
   *
   * Douyin's `play_addr` is a signed URL: a stale signature answers 403 -- or
   * 410 once the object itself is gone -- rather than failing to connect. That
   * is the shape F1's reactive re-resolve exists for, so the fixture has to have
   * it; a test where the old URL still works is testing an easier world than the
   * one the code runs in.
   *
   * `refusalStatus` is a parameter because a test claiming to cover both 403 and
   * 410 while the fixture quietly serves 403 twice is testing one case and
   * reporting two.
   */
  serveOnlyForToken(token: string, payload: Buffer, refusalStatus = 403): void {
    this.responder = (req, res) => {
      const url = new URL(req.url ?? '/', 'http://localhost');
      if (url.searchParams.get('token') !== token) {
        const body = `<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>`;
        res.writeHead(refusalStatus, { 'content-length': String(Buffer.byteLength(body)) });
        res.end(body);
        return;
      }
      res.writeHead(200, {
        'content-type': 'video/mp4',
        'content-length': String(payload.length),
      });
      res.end(payload);
    };
  }

  /** Accepts the request and never answers -- the client's timeout must fire. */
  respondNever(): void {
    this.responder = () => {
      /* intentionally silent */
    };
  }
}
