import { spawnSync } from "node:child_process";
import * as path from "node:path";

/**
 * Proves fail-fast for real: spawns the actual entrypoint as a child
 * process with a deliberately incomplete env and asserts the *process*
 * exits non-zero with the missing var named on stderr. An in-process throw
 * assertion is not sufficient here -- the claim under test is "the process
 * crashes at startup", which only a real exit code can prove.
 */
describe("bootstrap fail-fast (real process)", () => {
  it("exits non-zero and names the missing var on stderr", () => {
    const entry = path.join(__dirname, "..", "src", "main.ts");
    const cwd = path.join(__dirname, "..");

    const childEnv: NodeJS.ProcessEnv = {
      ...process.env,
      API_KEY: "test-key-not-real",
      REDIS_URL: "redis://localhost:6379",
      PARSER_BASE_URL: "http://parser:80",
      PROBE_SEC_USER_ID: "probe-sec-user-id",
      // DATABASE_URL deliberately omitted.
    };
    delete childEnv.DATABASE_URL;

    const result = spawnSync(
      process.execPath,
      ["-r", "ts-node/register/transpile-only", entry],
      {
        cwd,
        env: childEnv,
        encoding: "utf-8",
        timeout: 20000,
      }
    );

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/DATABASE_URL/);
  });
});

/**
 * Positive control for the test above.
 *
 * Without this, `bootstrap-failure.spec.ts` is a false green: a main.ts that
 * exits 1 unconditionally satisfies "exits non-zero and names the missing
 * var" just as well as a correct one does. Verified by mutation -- injecting
 * an unconditional `throw` into bootstrap() left all 11 tests passing until
 * this case existed. The claim being defended is two-sided: crash on bad
 * config, AND boot on good config.
 */
describe("bootstrap succeeds (real process, positive control)", () => {
  it("stays alive and listening with a complete env", async () => {
    const { spawn } = await import("node:child_process");
    const entry = path.join(__dirname, "..", "src", "main.ts");
    const cwd = path.join(__dirname, "..");

    const child = spawn(
      process.execPath,
      ["-r", "ts-node/register/transpile-only", entry],
      {
        cwd,
        env: {
          ...process.env,
          API_KEY: "test-key-not-real",
          DATABASE_URL: "postgres://reup:reup@localhost:5432/reup",
          REDIS_URL: "redis://localhost:6379",
          PARSER_BASE_URL: "http://parser:80",
          PROBE_SEC_USER_ID: "probe-sec-user-id",
          // Port 0 = let the OS pick a free one, so this never collides with
          // a dev server or with CI running suites in parallel.
          PORT: "0",
        },
        stdio: ["ignore", "pipe", "pipe"],
      }
    );

    let stderr = "";
    child.stderr.on("data", (c: Buffer) => (stderr += c.toString()));

    const outcome = await new Promise<"exited" | "alive">((resolve) => {
      const timer = setTimeout(() => resolve("alive"), 8000);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve("exited");
      });
    });

    child.kill("SIGKILL");

    expect({ outcome, stderr }).toEqual({ outcome: "alive", stderr: "" });
  }, 20000);
});

/**
 * D-3: the non-EnvValidationError bootstrap catch-all in main.ts used to
 * write `err.stack` to stderr unfiltered. Phase 1 wires up real DB/Redis
 * clients at bootstrap, and a connection error commonly embeds the DSN --
 * including the password -- in its message/stack. Since that real trigger
 * doesn't exist yet, main.ts exposes an explicit, env-gated test-only fault
 * injection point (`__TEST_ONLY_SIMULATE_BOOTSTRAP_ERROR__`) that throws a
 * generic Error embedding DATABASE_URL, exercising the exact catch-all path
 * a real connection error would hit. This spawns the real process so a
 * removed redactor call is provably caught (mutation-verified: deleting the
 * `redactSecrets(...)` call in main.ts's catch-all leaves the password in
 * stderr and turns this test red).
 */
describe("bootstrap catch-all redacts secrets on non-config errors (D-3, real process)", () => {
  it("redacts the DATABASE_URL password from a simulated bootstrap error", () => {
    const entry = path.join(__dirname, "..", "src", "main.ts");
    const cwd = path.join(__dirname, "..");
    // Obviously fake + low entropy on purpose: this string lands in git
    // history, and a realistic-looking one trips gitleaks' generic-api-key.
    // It only has to be unique enough to search stderr for.
    const password = "fake-placeholder-password-unique";
    const databaseUrl = `postgres://reup:${password}@localhost:5432/reup`;

    const result = spawnSync(
      process.execPath,
      ["-r", "ts-node/register/transpile-only", entry],
      {
        cwd,
        env: {
          ...process.env,
          API_KEY: "test-key-not-real",
          DATABASE_URL: databaseUrl,
          REDIS_URL: "redis://localhost:6379",
          PARSER_BASE_URL: "http://parser:80",
          PROBE_SEC_USER_ID: "probe-sec-user-id",
          __TEST_ONLY_SIMULATE_BOOTSTRAP_ERROR__: "1",
        },
        encoding: "utf-8",
        timeout: 20000,
      }
    );

    expect(result.status).not.toBe(0);
    expect(result.stderr).not.toContain(password);
    expect(result.stderr).not.toContain(databaseUrl);
    expect(result.stderr).toContain("[redacted]");
    expect(result.stderr).toMatch(/ECONNREFUSED/);
  });
});
