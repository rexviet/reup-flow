/**
 * Child-process harness for test/redaction/cookie-leak.e2e-spec.ts.
 *
 * Deliberately NOT a `.spec.ts`: jest's testRegex only collects
 * `*.spec.ts`/`*.e2e-spec.ts`, so this file is run only by the spec that
 * spawns it.
 *
 * Why a real child process rather than a jest spy on `process.stdout.write`:
 * the claim under test is "this secret never leaves the process", and the
 * only unmockable observation of that is the bytes on the child's actual
 * stdout/stderr pipes. A spy sits INSIDE the process and can only see the
 * writes it was installed in front of -- which is exactly how the phase-1
 * leak survived a green suite (a `print()` the logger-watching test never
 * looked at). `test/bootstrap-failure.spec.ts` uses the same technique for
 * the same reason.
 *
 * Boots through `src/bootstrap.ts` -- the same code path main.ts uses -- so
 * that reordering or deleting a bootstrap step (installing the stdio patch,
 * registering a secret value) is caught here rather than passing because the
 * harness happened to redo the steps correctly on its own.
 */
import { appendFileSync } from "node:fs";
import { createApp } from "../../src/bootstrap";

/** Set by the spec. Pushed through the real endpoint, so only the
 * exact-value registry can scrub it -- it has no `field=value` cookie shape
 * for the pattern mechanism to recognise. */
const OPAQUE_COOKIE = process.env.LEAK_OPAQUE_COOKIE ?? "";
/** Set by the spec. Never registered anywhere, so only the field=value
 * pattern can scrub it. */
const SHAPED_COOKIE = process.env.LEAK_SHAPED_COOKIE ?? "";

/**
 * FIX 4 (review): the spec used to read response bodies back out of THIS
 * process's stdout, which `console.log` had already routed through the
 * installed stdio patch by the time the spec ever saw it -- so "the body is
 * absent from stdout" proved the stdio patch works, not that the HTTP layer
 * never put the cookie in a response in the first place. Those are
 * different claims: a handler that echoed the cookie straight into a
 * response body would still look clean here, because the redaction that
 * caught it was never the one under test.
 *
 * `fs.writeFileSync` (via `appendFileSync` below) bypasses `process.stdout`/
 * `process.stderr` entirely -- the stdio patch only wraps those two write
 * functions, never the filesystem -- so a file written this way holds
 * exactly what the HTTP layer handed back, unredacted by anything in this
 * process. Only written when the spec opts in via `LEAK_BODY_FILE`, so a
 * normal harness run (no env var set) behaves exactly as before.
 */
const BODY_FILE = process.env.LEAK_BODY_FILE;

function recordRawBody(label: string, body: string): void {
  if (!BODY_FILE) return;
  appendFileSync(BODY_FILE, `${label}: ${body}\n`);
}

async function main(): Promise<void> {
  const { app } = await createApp();
  await app.listen(0);
  const base = await app.getUrl();
  const apiKey = process.env.API_KEY ?? "";

  // 1. Push a cookie through the REAL route, exactly as the extension will.
  const pushRes = await fetch(`${base}/internal/cookie`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": apiKey },
    body: JSON.stringify({ platform: "douyin", cookie: OPAQUE_COOKIE }),
  });
  const pushBody = await pushRes.text();
  recordRawBody("push", pushBody);
  console.log(`push status=${pushRes.status} body=${pushBody}`);

  // 2. Positive controls for the two output funnels. These are the point of
  //    the harness: without a deliberate leak attempt the spec would be
  //    vacuously green and would prove only that nothing happened to log a
  //    cookie today. `console.log` goes through stdout, the bare write goes
  //    through stderr, and each canary is scrubbable by exactly ONE of the
  //    redactor's two mechanisms -- so dropping either mechanism, or
  //    no-oping the stdio patch, turns this red.
  console.log(`stdout canary: ${OPAQUE_COOKIE}`);
  process.stderr.write(`stderr canary: ${SHAPED_COOKIE}\n`);

  // 3. The status endpoint must not carry the value in ANY field.
  const statusRes = await fetch(`${base}/internal/cookie/status`, {
    headers: { "x-api-key": apiKey },
  });
  const statusBody = await statusRes.text();
  recordRawBody("status", statusBody);
  console.log(`status body=${statusBody}`);

  // 4. A validation failure must not echo the cookie back either. A zod
  //    error that interpolated the received value would put the cookie in a
  //    400 body -- and from there into whatever logs the caller keeps.
  const badRes = await fetch(`${base}/internal/cookie`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": apiKey },
    body: JSON.stringify({ platform: 12345, cookie: OPAQUE_COOKIE }),
  });
  const badBody = await badRes.text();
  recordRawBody("bad-request", badBody);
  console.log(`bad-request status=${badRes.status} body=${badBody}`);

  await app.close();
  process.exit(0);
}

main().catch((err: unknown) => {
  // Errors go through the same patched stderr as everything else -- if a
  // secret rides out on a stack trace, the spec catches it here too.
  process.stderr.write(`harness failed: ${String(err instanceof Error ? err.stack : err)}\n`);
  process.exit(1);
});
