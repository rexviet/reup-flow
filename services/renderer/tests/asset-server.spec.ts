import * as fs from "node:fs";
import * as http from "node:http";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { startAssetServer, type AssetServer } from "../src/asset-server";

let dir: string;
const servers: AssetServer[] = [];

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "asset-server-test-"));
  fs.writeFileSync(path.join(dir, "clip.wav"), Buffer.from("0123456789"));
  fs.writeFileSync(path.join(dir, "font.ttf"), "font-bytes");
});

afterEach(async () => {
  await Promise.all(servers.splice(0).map((s) => s.close()));
  fs.rmSync(dir, { recursive: true, force: true });
});

async function serve(): Promise<AssetServer> {
  const server = await startAssetServer(dir);
  servers.push(server);
  return server;
}

function get(url: string, headers: http.OutgoingHttpHeaders = {}) {
  return new Promise<{ status: number; body: string; headers: http.IncomingHttpHeaders }>(
    (resolve, reject) => {
      http
        .get(url, { headers, agent: false }, (res) => {
          const chunks: Buffer[] = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () =>
            resolve({
              status: res.statusCode ?? 0,
              body: Buffer.concat(chunks).toString(),
              headers: res.headers,
            }),
          );
        })
        .on("error", reject);
    },
  );
}

function getRaw(port: number, rawPath: string) {
  return new Promise<{ status: number; body: string }>((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port, path: rawPath, agent: false }, (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () =>
          resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString() }),
        );
      })
      .on("error", reject);
  });
}

function portOf(server: AssetServer): number {
  return Number(new URL(server.baseUrl).port);
}

describe("the asset server", () => {
  it("serves a file in its directory under the job's token", async () => {
    const server = await serve();
    const res = await get(server.url("clip.wav"));
    expect(res.status).toBe(200);
    expect(res.body).toBe("0123456789");
    expect(res.headers["content-type"]).toBe("audio/wav");
  });

  it("answers 404 without the token, or with another one", async () => {
    const server = await serve();
    const origin = new URL(server.baseUrl).origin;
    expect((await get(`${origin}/clip.wav`)).status).toBe(404);
    expect((await get(`${origin}/${"0".repeat(48)}/clip.wav`)).status).toBe(404);
  });

  it("does not hand out a file that is not in the directory", async () => {
    const server = await serve();
    const token = server.baseUrl.split("/").pop();
    const outside = path.join(os.tmpdir(), "asset-server-secret.txt");
    fs.writeFileSync(outside, "secret");
    try {
      for (const attempt of [
        "../asset-server-secret.txt",
        "..%2fasset-server-secret.txt",
        encodeURIComponent(outside),
        "%2e%2e%2fasset-server-secret.txt",
      ]) {
        // The path is sent as written: a URL parser would tidy the `..` away first.
        const res = await getRaw(portOf(server), `/${token}/${attempt}`);
        expect([res.status, res.body]).toEqual([404, ""]);
      }
    } finally {
      fs.rmSync(outside, { force: true });
    }
  });

  it("serves byte ranges, which the browser asks for when it plays audio", async () => {
    const server = await serve();
    const res = await get(server.url("clip.wav"), { Range: "bytes=2-5" });
    expect(res.status).toBe(206);
    expect(res.body).toBe("2345");
    expect(res.headers["content-range"]).toBe("bytes 2-5/10");
    expect((await get(server.url("clip.wav"), { Range: "bytes=-3" })).body).toBe("789");
    expect((await get(server.url("clip.wav"), { Range: "bytes=50-60" })).status).toBe(416);
  });

  it("allows the composition page, which is on another origin, to read what it serves", async () => {
    const server = await serve();
    const res = await get(server.url("font.ttf"));
    expect(res.headers["access-control-allow-origin"]).toBe("*");
  });

  it("is reachable on loopback and on no other interface", async () => {
    const server = await serve();
    expect((await get(server.url("clip.wav"))).status).toBe(200);
    const external = Object.values(os.networkInterfaces())
      .flat()
      .find((i) => i && i.family === "IPv4" && !i.internal);
    // A machine with no external interface cannot show the difference; this one says so
    // instead of passing for something it did not check.
    expect(external).toBeDefined();
    await expect(
      get(`http://${external!.address}:${portOf(server)}/${server.baseUrl.split("/").pop()}/clip.wav`),
    ).rejects.toMatchObject({ code: "ECONNREFUSED" });
  });

  it("listens on a different random port, with a different token, each time", async () => {
    const [a, b] = [await serve(), await serve()];
    expect(portOf(a)).not.toBe(portOf(b));
    expect(a.baseUrl.split("/").pop()).not.toBe(b.baseUrl.split("/").pop());
    expect(a.baseUrl.split("/").pop()).toMatch(/^[0-9a-f]{48}$/);
  });

  it("stops listening when it is closed", async () => {
    const server = await serve();
    await server.close();
    await expect(get(server.url("clip.wav"))).rejects.toMatchObject({ code: "ECONNREFUSED" });
  });

  it("closes the file when a client goes away in the middle of it", async () => {
    fs.writeFileSync(path.join(dir, "big.mp4"), Buffer.alloc(16 * 1024 * 1024, 1));
    const server = await serve();
    const token = server.baseUrl.split("/").pop();
    const openFiles = () => fs.readdirSync("/dev/fd").length;
    const before = openFiles();
    await Promise.all(
      Array.from(
        { length: 20 },
        () =>
          new Promise<void>((resolve) => {
            const socket = net.connect(portOf(server), "127.0.0.1", () => {
              socket.write(`GET /${token}/big.mp4 HTTP/1.1\r\nHost: x\r\n\r\n`);
            });
            // Hang up as soon as the first bytes arrive, with most of the file unsent.
            socket.once("data", () => {
              socket.destroy();
              resolve();
            });
          }),
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 300));
    // Sockets and streams are gone; allow a little slack for the runtime's own handles.
    expect(openFiles() - before).toBeLessThanOrEqual(2);
  });
});
