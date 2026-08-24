import { readFileSync } from "node:fs";
import * as path from "node:path";
import { parse } from "yaml";

/**
 * Executable guard for the two docker-compose.yml constraints that plan §0.2
 * calls out as "vỡ âm thầm nếu ai xoá" -- they break silently if deleted.
 *
 * Until this file existed they were enforced by comments only: a grep across
 * src/, test/, .github/ and scripts/ found the root compose referenced nowhere
 * executable, so deleting either one failed no test and no CI job. Per plan §0
 * principle 4 a comment is documentation, not a guard. The DoD line "curl the
 * parser from the host fails" is a manual step nobody repeats.
 */
describe("docker-compose.yml load-bearing constraints", () => {
  const compose = parse(
    readFileSync(path.join(__dirname, "..", "..", "..", "docker-compose.yml"), "utf-8")
  );

  it("parser publishes NO host ports", () => {
    // The parser is an unauthenticated scraper. A `ports:` mapping here exposes
    // it to whatever network the host is on. It must be reachable only from
    // other containers on the internal network.
    expect(compose.services.parser.ports).toBeUndefined();
  });

  it("parser waits for redis to be HEALTHY, not merely started", () => {
    // A started-but-not-yet-accepting Redis is indistinguishable from a missing
    // one at the parser: cookie_provider silently falls back to
    // DOUYIN_COOKIE_FALLBACK. No error, no crash, just a stale cookie.
    expect(compose.services.parser.depends_on.redis.condition).toBe("service_healthy");
  });

  it("nest does not receive the parser's cookie fallback or the db password", () => {
    // `env_file: .env` would hand the orchestrator a live Douyin session cookie
    // it never reads. Allowlist only what config/env.ts validates.
    expect(compose.services.nest.env_file).toBeUndefined();
    const nestEnv = Object.keys(compose.services.nest.environment ?? {});
    expect(nestEnv).not.toContain("DOUYIN_COOKIE_FALLBACK");
    expect(nestEnv).not.toContain("POSTGRES_PASSWORD");
  });

  it("every service has a healthcheck, so `up` failing is visible", () => {
    for (const name of ["nest", "parser", "postgres", "redis"]) {
      expect(compose.services[name].healthcheck).toBeDefined();
    }
  });
});
