import { INestApplication } from "@nestjs/common";
import { Test } from "@nestjs/testing";
import request from "supertest";
import { AppModule } from "../src/app.module";

describe("GET /health (e2e)", () => {
  let app: INestApplication;
  const originalEnv = { ...process.env };

  beforeAll(async () => {
    // AppModule now wires ConfigModule/DbModule/RedisModule for real (group
    // A), so booting it needs a complete, valid env pointing at the live
    // docker-compose stack -- the same one bootstrap-failure.spec.ts's
    // positive control uses.
    Object.assign(process.env, {
      API_KEY: "test-key-not-real",
      DATABASE_URL: "postgres://reup:reup@localhost:5432/reup",
      REDIS_URL: "redis://localhost:6379",
      PARSER_BASE_URL: "http://parser:80",
      PROBE_SEC_USER_ID: "probe-sec-user-id",
      // Lanes OFF. This suite boots the real AppModule against the live
      // compose stack's `public` schema; with lanes on, a 60s reaper would
      // start sweeping those tables and DATA_DIR as a side effect of asking an
      // HTTP question. See env.ts's LANES_ENABLED.
      LANES_ENABLED: "false",
    });

    const moduleRef = await Test.createTestingModule({
      imports: [AppModule],
    }).compile();

    app = moduleRef.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    await app.close();
    process.env = { ...originalEnv };
  });

  it("returns 200 with a status ok body and requires NO X-Api-Key header", () => {
    // /health must stay reachable with no header at all: docker-compose.yml's
    // healthcheck probes it with `wget` and no header (see
    // test/compose-constraints.spec.ts), which is why HealthController is
    // the one route marked @Public().
    return request(app.getHttpServer())
      .get("/health")
      .then((res) => {
        expect(res.status).toBe(200);
        expect(res.body).toEqual({ status: "ok" });
      });
  });
});
