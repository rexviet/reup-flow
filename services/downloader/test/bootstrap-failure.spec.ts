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
          // Lanes OFF. The subject here is BOOTSTRAP -- "a valid env produces
          // a process that starts and stays quiet" -- and this assertion
          // demands stderr be empty to the byte. With lanes on, the reaper
          // starts sweeping the live compose stack's `public` schema, which
          // has no tables (migrations are applied per-run into a test schema,
          // never into `public`), so every 60s tick correctly logs a failed
          // query and this control goes red for a reason that has nothing to
          // do with bootstrap. See env.ts's LANES_ENABLED.
          LANES_ENABLED: "false",
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
 * write `err.stack` to stderr unfiltered. Group A (phase 3) wired up real
 * DB/Redis clients at bootstrap (src/db/db.module.ts, src/redis/redis.module.ts),
 * each running an `OnModuleInit` probe that deliberately embeds its own DSN
 * in its thrown message on a connection failure (plan §0.3: the defence is
 * the redaction layer, not call-site discipline -- see the comment on that
 * throw in db.module.ts). This test points DATABASE_URL at a real, unused
 * port so `NestFactory.create(AppModule)` -- called for real, no fault
 * injection -- drives a genuine ECONNREFUSED through that exact path, with
 * main.ts's stdio patch (installed first, before anything else can write)
 * as the only thing standing between the DSN and stderr. Mutation-verified:
 * deleting either `redactor.registerSecret(env.DATABASE_URL)` in main.ts or
 * the DSN-in-message throw in db.module.ts leaves the password in stderr
 * and turns this test red.
 */
describe("bootstrap catch-all redacts secrets on non-config errors (D-3, real process)", () => {
  it("redacts the DATABASE_URL password from a genuine Postgres connection failure", () => {
    const entry = path.join(__dirname, "..", "src", "main.ts");
    const cwd = path.join(__dirname, "..");
    // Obviously fake + low entropy on purpose: this string lands in git
    // history, and a realistic-looking one trips gitleaks' generic-api-key.
    // It only has to be unique enough to search stderr for.
    const password = "fake-placeholder-password-unique";
    // Port 5433: nothing listens there in the compose stack (postgres is on
    // 5432), so this is a genuine ECONNREFUSED, not a mocked failure.
    const databaseUrl = `postgres://reup:${password}@localhost:5433/reup`;

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
