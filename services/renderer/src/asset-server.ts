// Serves one job's temporary files to Remotion over http://127.0.0.1:<random port>/<token>/.
//
// Remotion reads media by URL, not by path. The server binds loopback only, listens on
// a port the OS picks, and answers only under a random per-job token, so another process
// on the machine cannot read the job's video by guessing a port. It serves exactly the
// files that were in the directory when it started and never follows a path out of it.
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as path from "node:path";
import { pipeline } from "node:stream";

export interface AssetServer {
  /** `http://127.0.0.1:<port>/<token>` with no trailing slash. */
  baseUrl: string;
  url(fileName: string): string;
  close(): Promise<void>;
}

const TYPES: Record<string, string> = {
  ".mp4": "video/mp4",
  ".wav": "audio/wav",
  ".ttf": "font/ttf",
};

function parseRange(header: string | undefined, size: number): [number, number] | "bad" | null {
  if (header === undefined) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!match || (match[1] === "" && match[2] === "")) return "bad";
  let start: number;
  let end: number;
  if (match[1] === "") {
    // A suffix range: the last N bytes.
    start = Math.max(0, size - Number(match[2]));
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] === "" ? size - 1 : Math.min(Number(match[2]), size - 1);
  }
  return start > end || start >= size ? "bad" : [start, end];
}

export async function startAssetServer(dir: string): Promise<AssetServer> {
  const token = randomBytes(24).toString("hex");
  const files = new Set(fs.readdirSync(dir));
  const prefix = `/${token}/`;

  const server = http.createServer((req, res) => {
    // Remotion's page (another origin) fetches the font and the audio from here.
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Headers", "Range");
    res.setHeader("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges");
    if (req.method === "OPTIONS") {
      res.writeHead(204).end();
      return;
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      res.writeHead(405).end();
      return;
    }
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    let name = "";
    if (url.pathname.startsWith(prefix)) {
      try {
        name = decodeURIComponent(url.pathname.slice(prefix.length));
      } catch {
        name = "";
      }
    }
    // A bare file name that was in the directory: no separators, no `..`.
    if (name === "" || name !== path.basename(name) || !files.has(name)) {
      res.writeHead(404).end();
      return;
    }
    const file = path.join(dir, name);
    const size = fs.statSync(file).size;
    const range = parseRange(req.headers.range, size);
    const headers: Record<string, string | number> = {
      "Content-Type": TYPES[path.extname(name)] ?? "application/octet-stream",
      "Accept-Ranges": "bytes",
      "Cache-Control": "no-store",
    };
    if (range === "bad") {
      res.writeHead(416, { ...headers, "Content-Range": `bytes */${size}` }).end();
      return;
    }
    const [start, end] = range ?? [0, size - 1];
    if (range) headers["Content-Range"] = `bytes ${start}-${end}/${size}`;
    headers["Content-Length"] = size === 0 ? 0 : end - start + 1;
    res.writeHead(range ? 206 : 200, headers);
    if (req.method === "HEAD" || size === 0) {
      res.end();
      return;
    }
    // `pipeline`, not `pipe`: a client that goes away mid-body must close the file too.
    pipeline(fs.createReadStream(file, { start, end }), res, () => undefined);
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const { port } = server.address() as AddressInfo;
  const baseUrl = `http://127.0.0.1:${port}/${token}`;
  return {
    baseUrl,
    url: (fileName) => `${baseUrl}/${encodeURIComponent(fileName)}`,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}
