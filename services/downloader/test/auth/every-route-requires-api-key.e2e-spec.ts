import { INestApplication } from "@nestjs/common";
import { Test } from "@nestjs/testing";
import request from "supertest";
import { AppModule } from "../../src/app.module";

/**
 * Phase-3 DoD: "missing/wrong `X-Api-Key` -> 401 on EVERY route, including
 * `/internal`."
 *
 * Written against the REAL `AppModule` -- the production wiring, with
 * `ApiKeyGuard` registered as `APP_GUARD` -- rather than a hand-assembled
 * testing module. A per-controller test can only ever prove that the
 * controllers somebody remembered to write a test for are protected; this
 * proves the wiring is.
 *
 * And it does not hardcode the route list. It asks the running router what
 * routes exist and sweeps all of them, so a controller added in phase 8 or 9
 * is covered the day it lands, by an author who never read this file. The
 * `EXPECTED_MINIMUM_ROUTES` guard below is what keeps that from degrading
 * into a vacuous pass if the router internals ever change shape and
 * enumeration silently returns nothing.
 */

interface DiscoveredRoute {
  method: string;
  path: string;
}

/**
 * Reads the Express router's own route table. This reaches into framework
 * internals on purpose -- it is the only way to enumerate what is actually
 * mounted, and the alternative (a hand-maintained list) is exactly the thing
 * that goes stale. Express 5 exposes `router`; Express 4 exposed `_router`.
 * Both are tried, and finding neither is a hard failure, never a skip.
 */
function discoverRoutes(app: INestApplication): DiscoveredRoute[] {
  const instance = app.getHttpAdapter().getInstance() as {
    router?: { stack?: unknown[] };
    _router?: { stack?: unknown[] };
  };
  const stack = instance.router?.stack ?? instance._router?.stack;
  if (!Array.isArray(stack)) {
    throw new Error(
      "could not enumerate routes from the Express router -- its internals changed. " +
        "Fix this helper; do NOT downgrade the test to a hardcoded route list.",
    );
  }

  const routes: DiscoveredRoute[] = [];
  for (const layer of stack as Array<{
    route?: { path?: unknown; methods?: Record<string, boolean> };
  }>) {
    const path = layer.route?.path;
    const methods = layer.route?.methods;
    if (typeof path !== "string" || !methods) continue;
    for (const [method, enabled] of Object.entries(methods)) {
      if (enabled) routes.push({ method: method.toUpperCase(), path });
    }
  }
  return routes;
}

/** `/health` is the ONLY intentionally public route: docker-compose.yml's
 * healthcheck probes it with `wget` and no header, and
 * test/compose-constraints.spec.ts pins that. Anything else appearing here
 * should be treated as a bug in the route, not as a line to add below. */
const PUBLIC_PATHS = new Set(["/health"]);

/** Routes known to exist as of phase 3: 5 jobs + 1 videos browse + 2 cookie
 * + 1 health. If enumeration returns fewer than this, it is broken, not
 * lucky. */
const EXPECTED_MINIMUM_ROUTES = 13;

/** Concrete values for path parameters, so requests reach the guard rather
 * than 404ing in the router. The guard runs before the handler either way,
 * but a 404 would mean the sweep silently stopped testing anything. */
function concrete(path: string): string {
  return path
    .replace(":id", "0f1b6b6e-0000-4000-8000-000000000000")
    .replace(":secUserId", "MS4wLjABAAAA-sentinel-fake-profile");
}

describe("every route requires X-Api-Key (real AppModule)", () => {
  let app: INestApplication;
  let routes: DiscoveredRoute[];
  const originalEnv = { ...process.env };

  beforeAll(async () => {
    // AppModule wires ConfigModule/DbModule/RedisModule for real, so booting
    // it needs a complete env pointing at the live docker-compose stack --
    // same as test/health.e2e-spec.ts. Redis db 9, not the default 0, so
    // nothing here can disturb the `douyin:cookie:current` key the running
    // parser reads.
    Object.assign(process.env, {
      API_KEY: "sweep-test-key-not-real",
      DATABASE_URL: process.env.TEST_DATABASE_URL ?? "postgres://reup:reup@localhost:5432/reup",
      REDIS_URL: "redis://localhost:6379/9",
      PARSER_BASE_URL: "http://parser:80",
      PROBE_SEC_USER_ID: "probe-sec-user-id",
      // Lanes OFF. This suite boots the real AppModule against the live
      // compose stack's `public` schema; with lanes on, a 60s reaper would
      // start sweeping those tables and DATA_DIR as a side effect of asking an
      // HTTP question. See env.ts's LANES_ENABLED.
      LANES_ENABLED: "false",
    });

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] }).compile();
    app = moduleRef.createNestApplication();
    await app.init();
    routes = discoverRoutes(app);
  }, 30000);

  afterAll(async () => {
    await app?.close();
    process.env = { ...originalEnv };
  });

  it("discovered the application's routes", () => {
    expect(routes).toContainEqual({ method: "GET", path: "/api/reviews" });
    expect(routes.length).toBeGreaterThanOrEqual(EXPECTED_MINIMUM_ROUTES);
    // The sweep is only meaningful if it actually reached /internal, which is
    // the half most likely to be assumed safe because "only the extension
    // calls it".
    expect(routes.some((r) => r.path.startsWith("/internal"))).toBe(true);
    expect(routes.some((r) => r.path.startsWith("/api"))).toBe(true);
  });

  it("answers 401 on every non-public route when the header is absent", async () => {
    const results: Array<{ route: string; status: number }> = [];
    for (const route of routes) {
      if (PUBLIC_PATHS.has(route.path)) continue;
      const agent = request(app.getHttpServer());
      const res = await agent[route.method.toLowerCase() as "get"](concrete(route.path)).send();
      results.push({ route: `${route.method} ${route.path}`, status: res.status });
    }
    // Asserted as one object so a failure names every offending route at
    // once rather than stopping at the first.
    expect(results.filter((r) => r.status !== 401)).toEqual([]);
  }, 30000);

  it("answers 401 on every non-public route when the header is wrong", async () => {
    const results: Array<{ route: string; status: number; body: string }> = [];
    const wrongKey = "wrong-key-sentinel-not-real";
    for (const route of routes) {
      if (PUBLIC_PATHS.has(route.path)) continue;
      const agent = request(app.getHttpServer());
      const res = await agent[route.method.toLowerCase() as "get"](concrete(route.path))
        .set("X-Api-Key", wrongKey)
        .send();
      results.push({
        route: `${route.method} ${route.path}`,
        status: res.status,
        body: JSON.stringify(res.body),
      });
    }
    expect(results.filter((r) => r.status !== 401)).toEqual([]);
    // A rejected key must never be echoed back to whoever sent it.
    expect(results.filter((r) => r.body.includes(wrongKey))).toEqual([]);
  }, 30000);

  it("leaves /health reachable with no header at all", async () => {
    const res = await request(app.getHttpServer()).get("/health");
    expect(res.status).toBe(200);
  });
});
