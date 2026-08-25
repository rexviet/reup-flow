import { execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);

const CONTAINER = 'reup-flow-phase02-parser-forward';
const HOST_PORT = 18_642;
const NETWORK = process.env.PARSER_TEST_NETWORK ?? 'reup-flow_default';
const PARSER_HOST = process.env.PARSER_TEST_HOST ?? 'parser';

/**
 * A TCP forwarder from 127.0.0.1 into the compose network, in `python:3.11-slim`.
 *
 * The parser deliberately publishes NO host ports -- it is an unauthenticated
 * scraper, and `test/compose-constraints.spec.ts` fails the build if anyone
 * adds a `ports:` mapping to it. That constraint is the reason this exists:
 * to talk to the real container from a host-run jest without weakening the
 * thing the constraint protects. The forwarder binds LOOPBACK ONLY and is
 * removed when the suite ends, so nothing outside this machine can reach the
 * parser through it either.
 *
 * `python:3.11-slim` because it is already on any machine that has built the
 * parser image; adding socat would mean a pull.
 */
const FORWARDER = `
import asyncio
async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
async def handle(local_reader, local_writer):
    remote_reader, remote_writer = await asyncio.open_connection("${PARSER_HOST}", 80)
    await asyncio.gather(pipe(local_reader, remote_writer), pipe(remote_reader, local_writer))
async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", ${HOST_PORT})
    async with server:
        await server.serve_forever()
asyncio.run(main())
`;

export const PARSER_FORWARD_URL = `http://127.0.0.1:${HOST_PORT}`;

export async function startParserForward(): Promise<void> {
  await stopParserForward();
  await exec('docker', [
    'run', '--rm', '-d',
    '--name', CONTAINER,
    '--network', NETWORK,
    '-p', `127.0.0.1:${HOST_PORT}:${HOST_PORT}`,
    'python:3.11-slim',
    'python3', '-c', FORWARDER,
  ]);

  const deadline = Date.now() + 20_000;
  for (;;) {
    try {
      const res = await fetch(`${PARSER_FORWARD_URL}/docs`, {
        signal: AbortSignal.timeout(3000),
      });
      if (res.ok) return;
    } catch {
      /* forwarder or parser not ready yet */
    }
    if (Date.now() > deadline) {
      throw new Error(
        `parser not reachable through ${PARSER_FORWARD_URL}. Is \`docker compose up\` running?`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

export async function stopParserForward(): Promise<void> {
  await exec('docker', ['rm', '-f', CONTAINER]).catch(() => undefined);
}

/**
 * Whether the parser has any Douyin cookie at all to work with.
 *
 * Synchronous so a suite can decide at REGISTRATION time which assertions it
 * is entitled to make. Every route except `get_sec_user_id` talks to a
 * Douyin API that needs a session; without one they all fail with the fork's
 * catch-all 400 -- which, being information-free (F4), looks exactly like a
 * banned cookie or a private profile.
 */
export function douyinCookieConfigured(): boolean {
  const run = (args: string[]): string => {
    try {
      return execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    } catch {
      return '';
    }
  };
  const fromRedis = run(['compose', 'exec', '-T', 'redis', 'redis-cli', 'GET', 'douyin:cookie:current']);
  if (fromRedis && fromRedis !== '' && fromRedis !== '(nil)') return true;
  const fromEnv = run([
    'compose', 'exec', '-T', 'parser',
    'python3', '-c', 'import os; print(len(os.environ.get("DOUYIN_COOKIE_FALLBACK","")))',
  ]);
  return Number(fromEnv) > 0;
}
