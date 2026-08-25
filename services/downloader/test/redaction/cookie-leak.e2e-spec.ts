import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/**
 * Plan §0 principle 3, end to end: "a cookie never reaches a log, an error
 * message, or an API response."
 *
 * This is the phase-3 test that the mutation suite
 * (test/mutations/phase-03.sh) aims at. Everything it asserts is observed
 * from OUTSIDE a real child process running the real bootstrap, because the
 * failure this defends against is precisely the one an in-process spy cannot
 * see. Phase 1's leak was a `print()`; the suite was green because the test
 * watched the logging subsystem and nothing else. So here the observation
 * surface is the child's actual stdout and stderr pipes -- every byte the
 * process emitted, by any route.
 *
 * Two canaries, each scrubbable by exactly ONE of the redactor's two
 * mechanisms (src/redaction/redactor.ts):
 *
 *   - OPAQUE  -- pushed through POST /internal/cookie, so the exact-value
 *                registry knows it. Has no `field=value` shape, so the
 *                pattern mechanism cannot see it.
 *   - SHAPED  -- a `sessionid=...` string never registered anywhere, so only
 *                the pattern mechanism can see it.
 *
 * That split is what makes this one test kill three separate mutations:
 * dropping the registry, dropping the pattern, or no-oping the stdio patch.
 *
 * Both values carry a marker from .gitleaks.toml's allowlist ("sentinel",
 * "fake") -- mandatory for any credential-shaped literal in this repo, so
 * that a REAL cookie pasted into a fixture still turns the secret scan red.
 */
const OPAQUE_COOKIE = "sentinel-fake-opaque-cookie-8f2c41d0b7";
const SHAPED_COOKIE = "sessionid=sentinel-fake-shaped-cookie-0042";

describe("a pushed cookie never reaches stdout, stderr, or a response body (real process)", () => {
  let stdout: string;
  let stderr: string;
  let status: number | null;
  let bodyFileContents: string;

  beforeAll(() => {
    const harness = path.join(__dirname, "..", "support", "cookie-leak-harness.ts");
    const cwd = path.join(__dirname, "..", "..");
    // FIX 4 (review): a path OUTSIDE this process, read back after the child
    // exits. `fs.writeFileSync`/`appendFileSync` never go through
    // `process.stdout`/`process.stderr`, so this file holds exactly what the
    // HTTP layer handed back -- not what the stdio patch let survive. Unique
    // per run so parallel test workers cannot collide on the same path.
    const bodyFile = path.join(
      fs.mkdtempSync(path.join(os.tmpdir(), "cookie-leak-bodies-")),
      "bodies.log",
    );

    const result = spawnSync(
      process.execPath,
      ["-r", "ts-node/register/transpile-only", harness],
      {
        cwd,
        env: {
          ...process.env,
          API_KEY: "leak-harness-key-not-real",
          DATABASE_URL: "postgres://reup:reup@localhost:5432/reup",
          // Redis logical database 9, NOT the default 0. The compose stack's
          // parser reads `douyin:cookie:current` from db 0 on this same
          // server: pushing a canary there would swap the running stack's
          // real cookie for a fake one. ioredis reads the path segment as
          // the db index, so this is a genuine Redis, genuinely written to,
          // just not the one anything else is looking at.
          REDIS_URL: "redis://localhost:6379/9",
          PARSER_BASE_URL: "http://parser:80",
          PROBE_SEC_USER_ID: "probe-sec-user-id",
          LEAK_OPAQUE_COOKIE: OPAQUE_COOKIE,
          LEAK_SHAPED_COOKIE: SHAPED_COOKIE,
          LEAK_BODY_FILE: bodyFile,
        },
        encoding: "utf-8",
        timeout: 60000,
      },
    );

    stdout = result.stdout ?? "";
    stderr = result.stderr ?? "";
    status = result.status;
    bodyFileContents = fs.existsSync(bodyFile) ? fs.readFileSync(bodyFile, "utf-8") : "";
  }, 70000);

  it("runs the harness to completion", () => {
    // Guards every assertion below. A harness that crashed before it ever
    // logged a canary would satisfy "the cookie is absent from stdout"
    // perfectly, and prove nothing at all.
    expect({ status, stderr }).toMatchObject({ status: 0 });
    expect(stdout).toContain("push status=200");
    expect(stdout).toContain("stdout canary:");
    expect(stderr).toContain("stderr canary:");
    expect(stdout).toContain("status body=");
    expect(stdout).toContain("bad-request status=400");
    // Same guard, for the body file: a harness that silently skipped every
    // `recordRawBody` call would leave `bodyFileContents` empty, and the
    // response-body assertion below would then pass vacuously.
    expect(bodyFileContents).toContain("push:");
    expect(bodyFileContents).toContain("status:");
    expect(bodyFileContents).toContain("bad-request:");
  });

  it("keeps the registry-only canary out of stdout and stderr", () => {
    expect(stdout).not.toContain(OPAQUE_COOKIE);
    expect(stderr).not.toContain(OPAQUE_COOKIE);
  });

  it("keeps the pattern-only canary out of stdout and stderr", () => {
    expect(stdout).not.toContain(SHAPED_COOKIE);
    expect(stderr).not.toContain(SHAPED_COOKIE);
    // The bare value without its field name, too: `sessionid=[redacted]` is
    // the intended output, but a redactor that dropped only the field name
    // and left the value behind would still pass the assertion above.
    expect(stdout).not.toContain("sentinel-fake-shaped-cookie-0042");
    expect(stderr).not.toContain("sentinel-fake-shaped-cookie-0042");
  });

  it("proves the canaries were emitted and scrubbed, not merely absent", () => {
    // Without this, every assertion above is satisfied by a harness that
    // silently skipped its canary writes. `[redacted]` on both funnels is
    // the evidence that the secret was written AND intercepted.
    expect(stdout).toMatch(/stdout canary: \[redacted\]/);
    expect(stderr).toMatch(/stderr canary: sessionid=\[redacted\]/);
  });

  it("never returns the cookie in an API response body", () => {
    // FIX 4 (review): this used to read the bodies back out of `stdout`,
    // which is exactly the wrong observation surface for THIS claim -- by
    // the time `console.log` wrote that line, the stdio patch had already
    // scrubbed it, so the assertion was really "the stdio patch works",
    // already covered by the two canary tests above, and could not have
    // failed even if a handler echoed the cookie straight into a response.
    // Confirmed by mutation: `cookie.service.ts`'s `status()` rewired to
    // `last_ok_at: await this.redis.get(cookieCurrentKey(platform))` --
    // i.e. actually leaking the cookie value through the status response --
    // left this assertion green when read from `stdout`.
    //
    // `bodyFileContents` is written via `fs.writeFileSync`/`appendFileSync`
    // in the harness, which the stdio patch never touches (it only wraps
    // `process.stdout`/`process.stderr`), so it holds exactly what the HTTP
    // layer handed back. This is the assertion that can actually fail for
    // the reason its name claims.
    expect(bodyFileContents).not.toContain(OPAQUE_COOKIE);
    expect(bodyFileContents.length).toBeGreaterThan(0);
  });
});
