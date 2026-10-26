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
  // `merge: true`: YAML 1.2 core has no `<<` merge key by default, and the
  // Langfuse services share an env anchor through it. Without this the shared
  // keys would be invisible to every env assertion below and they would pass
  // vacuously.
  const compose = parse(
    readFileSync(path.join(__dirname, "..", "..", "..", "docker-compose.yml"), "utf-8"),
    { merge: true }
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

  it("nest is told explicitly whether it runs the lanes", () => {
    // Phase 4 §4.2. env.ts defaults LANES_ENABLED to true, so omitting it here
    // would still work -- and that is the problem: the ONE line an operator
    // edits to split this into an API-only node plus a lane node (plan §0
    // principle 1) would not exist in the file they are editing. A default
    // that has to be discovered by reading TypeScript is a default nobody
    // finds under pressure.
    expect(compose.services.nest.environment.LANES_ENABLED).toBeDefined();
  });

  it("every service has a healthcheck, so `up` failing is visible", () => {
    for (const name of ["nest", "parser", "postgres", "redis"]) {
      expect(compose.services[name].healthcheck).toBeDefined();
    }
  });

  it("every long-running localization service has a healthcheck", () => {
    for (const name of [
      "temporal",
      "temporal-ui",
      "minio",
      "clickhouse",
      "langfuse-web",
      "langfuse-worker",
    ]) {
      expect(compose.services[name].healthcheck).toBeDefined();
    }
  });
});

/**
 * Video-localization infra (plan 261026-0147, phase 00). The services below
 * hold a Postgres superuser-adjacent credential, object storage roots and a
 * session secret. Each rule here exists because one wrong line in compose
 * turns a private tool into a network-reachable one, silently.
 */
describe("docker-compose.yml localization infra", () => {
  const root = path.join(__dirname, "..", "..", "..");
  const compose = parse(readFileSync(path.join(root, "docker-compose.yml"), "utf-8"), {
    merge: true,
  });
  type Service = {
    image?: string;
    ports?: string[];
    restart?: string | boolean;
    env_file?: unknown;
    environment?: Record<string, unknown>;
    depends_on: Record<string, { condition: string }>;
    healthcheck?: unknown;
    command?: unknown;
    mem_limit?: unknown;
    shm_size?: unknown;
  };
  const services: Record<string, Service> = compose.services;

  // Services that predate this plan keep their own, separately reasoned ports.
  const LEGACY = new Set(["nest", "parser", "postgres", "redis", "web"]);
  const added = Object.keys(services).filter((n) => !LEGACY.has(n));

  const LANGFUSE_SHARED = [
    "DATABASE_URL",
    "CLICKHOUSE_URL",
    "CLICKHOUSE_MIGRATION_URL",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_CLUSTER_ENABLED",
    "REDIS_CONNECTION_STRING",
    "LANGFUSE_S3_EVENT_UPLOAD_BUCKET",
    "LANGFUSE_S3_EVENT_UPLOAD_REGION",
    "LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT",
    "LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID",
    "LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY",
    "LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE",
    "SALT",
    "ENCRYPTION_KEY",
    "TELEMETRY_ENABLED",
    "TZ",
  ];
  const LOCALIZATION_COMMON = [
    "TEMPORAL_ADDRESS",
    "TEMPORAL_NAMESPACE",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
  ];
  const LOCALIZATION_S3 = [
    "S3_ENDPOINT",
    "S3_REGION",
    "S3_BUCKET",
    "S3_FORCE_PATH_STYLE",
    "LOC_S3_ACCESS_KEY_ID",
    "LOC_S3_SECRET_ACCESS_KEY",
  ];
  const RENDERER_S3 = [
    "S3_ENDPOINT",
    "S3_REGION",
    "S3_BUCKET",
    "S3_FORCE_PATH_STYLE",
    "RENDER_S3_ACCESS_KEY_ID",
    "RENDER_S3_SECRET_ACCESS_KEY",
  ];
  // The exact environment each added service may receive. A service gets what it
  // reads and nothing else; a new variable is a deliberate edit to this table.
  const ENV_ALLOWLIST: Record<string, string[]> = {
    "db-init": [
      "PGHOST",
      "PGUSER",
      "PGPASSWORD",
      "POSTGRES_DB",
      "TEMPORAL_DB_PASSWORD",
      "LANGFUSE_DB_PASSWORD",
      "LOCALIZATION_DB_PASSWORD",
    ],
    "temporal-admin-tools": ["SQL_HOST", "SQL_USER", "SQL_PASSWORD"],
    temporal: [
      "DB",
      "DB_PORT",
      "POSTGRES_SEEDS",
      "POSTGRES_USER",
      "POSTGRES_PWD",
      "DBNAME",
      "VISIBILITY_DBNAME",
      "TZ",
    ],
    "temporal-namespace-init": ["TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_RETENTION"],
    "temporal-ui": ["TEMPORAL_ADDRESS", "TEMPORAL_CORS_ORIGINS"],
    minio: ["MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "TZ"],
    "minio-init": [
      "MINIO_ENDPOINT",
      "MINIO_ROOT_USER",
      "MINIO_ROOT_PASSWORD",
      "S3_ACCESS_KEY_ID",
      "S3_SECRET_ACCESS_KEY",
      "LOC_S3_ACCESS_KEY_ID",
      "LOC_S3_SECRET_ACCESS_KEY",
      "RENDER_S3_ACCESS_KEY_ID",
      "RENDER_S3_SECRET_ACCESS_KEY",
      "LANGFUSE_S3_ACCESS_KEY_ID",
      "LANGFUSE_S3_SECRET_ACCESS_KEY",
    ],
    clickhouse: ["CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD", "TZ"],
    "langfuse-web": [
      ...LANGFUSE_SHARED,
      "NEXTAUTH_URL",
      "NEXTAUTH_SECRET",
      "AUTH_DISABLE_SIGNUP",
      "LANGFUSE_INIT_ORG_ID",
      "LANGFUSE_INIT_ORG_NAME",
      "LANGFUSE_INIT_PROJECT_ID",
      "LANGFUSE_INIT_PROJECT_NAME",
      "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
      "LANGFUSE_INIT_PROJECT_SECRET_KEY",
      "LANGFUSE_INIT_USER_EMAIL",
      "LANGFUSE_INIT_USER_NAME",
      "LANGFUSE_INIT_USER_PASSWORD",
    ],
    "langfuse-worker": LANGFUSE_SHARED,
    "localization-starter": [
      ...LOCALIZATION_COMMON,
      "LOC_DATABASE_URL",
      "S3_BUCKET",
      "TARGET_LANG",
      "VOICE_PROVIDER",
      "VOICE_ID",
      "VOICE_SETTINGS_JSON",
      "VOICESTUDIO_URL",
      "VOICESTUDIO_API_KEY",
      "VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS",
      "MAX_OPEN_WORKFLOWS",
      "OUTBOX_MAX_ATTEMPTS",
    ],
    "worker-agents": [
      ...LOCALIZATION_COMMON,
      ...LOCALIZATION_S3,
      "LOC_DATABASE_URL",
      "OPENROUTER_BASE_URL",
      "OPENROUTER_API_KEY",
      "LOCALIZER_MODEL",
      "DUBBER_MODEL",
      "OCR_MODEL",
      "OCR_BBOX_FORMAT",
      "VOICESTUDIO_URL",
      "VOICESTUDIO_API_KEY",
      "VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS",
      "STT_MODEL",
      "TTS_MODEL",
      "STT_CONCURRENCY",
      "TTS_CONCURRENCY",
      "LLM_CONCURRENCY",
      "IO_CONCURRENCY",
    ],
    "worker-media": [
      ...LOCALIZATION_COMMON,
      ...LOCALIZATION_S3,
      "MEDIA_CONCURRENCY",
      "LOUDNESS_LUFS",
      "TRUE_PEAK_DBTP",
      "CAPTION_FONT_PATH",
      "COVER_COLOR",
      "COVER_OPACITY",
    ],
    "worker-render": [
      ...LOCALIZATION_COMMON,
      ...RENDERER_S3,
      "RENDER_CONCURRENCY",
      "CAPTION_FONT_PATH",
    ],
  };

  it("the table above covers every service this plan added, and nothing else is unaccounted for", () => {
    // A service added later without an allowlist entry would be un-checked by
    // the env test below. Make adding one a loud, deliberate edit.
    expect(added.sort()).toEqual(Object.keys(ENV_ALLOWLIST).sort());
  });

  it("only temporal-ui, langfuse-web and the MinIO console publish a port", () => {
    const publishers = added.filter((n) => (services[n].ports ?? []).length > 0).sort();
    expect(publishers).toEqual(["langfuse-web", "minio", "temporal-ui"]);
  });

  it("no new service binds 0.0.0.0: every published port is pinned to 127.0.0.1", () => {
    for (const name of added) {
      for (const mapping of services[name].ports ?? []) {
        const text = String(mapping);
        expect(`${name}: ${text}`).toMatch(/^[a-z-]+: 127\.0\.0\.1:\d+:\d+$/);
        expect(text).not.toContain("0.0.0.0");
      }
    }
  });

  it("langfuse-web does not collide with nest on host port 3000", () => {
    const hostPorts = (services["langfuse-web"].ports ?? []).map((m) => m.split(":")[1]);
    expect(hostPorts).not.toContain("3000");
  });

  it("Temporal, ClickHouse and the MinIO S3 API publish nothing", () => {
    expect(services.temporal.ports).toBeUndefined();
    expect(services.clickhouse.ports).toBeUndefined();
    // minio publishes only its console; 9000 (S3) must not appear.
    const minioPorts = (services.minio.ports ?? []).map(String);
    expect(minioPorts.some((p) => /:9000$/.test(p))).toBe(false);
  });

  it("each added service receives exactly its own environment and never an env_file", () => {
    for (const name of added) {
      expect(services[name].env_file).toBeUndefined();
      const env = Object.keys(services[name].environment ?? {}).sort();
      expect({ name, env }).toEqual({ name, env: [...ENV_ALLOWLIST[name]].sort() });
    }
  });

  it("no added service is handed a credential that belongs to another system", () => {
    // Value-level, so renaming the key cannot dodge the allowlist above.
    const forbiddenEverywhere = ["DOUYIN_COOKIE_FALLBACK", "API_KEY"];
    for (const name of added) {
      const values = JSON.stringify(services[name].environment ?? {});
      for (const secret of forbiddenEverywhere) expect(`${name}:${values}`).not.toContain("${" + secret + "}");
      // The Postgres superuser password is db-init's alone.
      if (name !== "db-init") {
        expect(`${name}:${values}`).not.toContain("${POSTGRES_PASSWORD");
      }
    }
    // The MinIO root credential never reaches a Langfuse or Temporal container.
    for (const name of ["langfuse-web", "langfuse-worker", "temporal", "temporal-ui", "clickhouse"]) {
      expect(JSON.stringify(services[name].environment ?? {})).not.toContain("MINIO_ROOT");
    }
    // The session secret and the seeded login stay out of the worker.
    const workerEnv = Object.keys(services["langfuse-worker"].environment ?? {});
    expect(workerEnv).not.toContain("NEXTAUTH_SECRET");
    expect(workerEnv).not.toContain("LANGFUSE_INIT_USER_PASSWORD");
  });

  it("each localization process gets only the credentials it uses", () => {
    const keys = (name: string) => Object.keys(services[name].environment ?? {});
    // The starter only reads Postgres and starts workflows: no object store, no LLM.
    for (const k of ["LOC_S3_ACCESS_KEY_ID", "LOC_S3_SECRET_ACCESS_KEY", "OPENROUTER_API_KEY"]) {
      expect(keys("localization-starter")).not.toContain(k);
    }
    // The media worker runs Demucs: no provider key and no database.
    for (const k of ["OPENROUTER_API_KEY", "VOICESTUDIO_API_KEY", "LOC_DATABASE_URL"]) {
      expect(keys("worker-media")).not.toContain(k);
    }
    // The renderer reads and writes work/* with its own user, runs a browser, and has no
    // use for a provider, the database or the localization user's wider access.
    for (const k of [
      "LOC_S3_ACCESS_KEY_ID",
      "LOC_S3_SECRET_ACCESS_KEY",
      "OPENROUTER_API_KEY",
      "VOICESTUDIO_API_KEY",
      "LOC_DATABASE_URL",
    ]) {
      expect(keys("worker-render")).not.toContain(k);
    }
    // And its key goes nowhere else.
    for (const name of ["localization-starter", "worker-agents", "worker-media"]) {
      expect(keys(name)).not.toContain("RENDER_S3_ACCESS_KEY_ID");
      expect(keys(name)).not.toContain("RENDER_S3_SECRET_ACCESS_KEY");
    }
    // Nobody but the downloader holds the downloader's object-store key.
    for (const name of ["localization-starter", "worker-agents", "worker-media", "worker-render"]) {
      expect(keys(name)).not.toContain("S3_ACCESS_KEY_ID");
      expect(keys(name)).not.toContain("S3_SECRET_ACCESS_KEY");
    }
  });

  it("workers have a sized /tmp and time to finish what they are running when stopped", () => {
    type Svc = { tmpfs?: string[]; stop_grace_period?: string };
    for (const name of ["worker-agents", "worker-media", "worker-render"]) {
      const svc = services[name] as unknown as Svc;
      // Without a size limit a runaway extraction fills the host's disk.
      expect(svc.tmpfs ?? []).toEqual([expect.stringMatching(/^\/tmp:size=\d+[mg],/)]);
      // The worker's own graceful shutdown is 60 s; docker's default stop is 10 s.
      expect(parseInt(String(svc.stop_grace_period), 10)).toBeGreaterThan(60);
    }
    // The render worker lets a running render finish for up to 10 minutes (GRACEFUL_SHUTDOWN
    // in services/renderer/src/worker.ts); docker must wait longer than that.
    const render = services["worker-render"] as unknown as Svc;
    expect(parseInt(String(render.stop_grace_period), 10)).toBeGreaterThan(600);
    const starter = services["localization-starter"] as unknown as Svc;
    expect(starter.stop_grace_period).toBeDefined();
  });

  it("each localization process runs one role of the same image", () => {
    const commands = ["localization-starter", "worker-agents", "worker-media"].map((n) =>
      JSON.stringify((services[n] as { command?: unknown }).command)
    );
    expect(commands).toEqual([
      JSON.stringify(["python", "-m", "localization.starter.outbox_starter"]),
      JSON.stringify(["python", "-m", "localization.workers.run", "agents"]),
      JSON.stringify(["python", "-m", "localization.workers.run", "media"]),
    ]);
  });

  it("the render worker has the shared memory Chromium needs and a ceiling on all memory", () => {
    // Docker's default /dev/shm is 64 MB.
    const shm = String(services["worker-render"].shm_size);
    const megabytes = /^(\d+)(m|mb|g|gb)$/i.exec(shm);
    expect({ shm, ok: megabytes !== null }).toEqual({ shm, ok: true });
    const [, amount, unit] = megabytes!;
    expect(Number(amount) * (unit.toLowerCase().startsWith("g") ? 1024 : 1)).toBeGreaterThanOrEqual(512);
    expect(services["worker-render"].mem_limit).toBeDefined();
  });

  it("the render worker's concurrency comes from RENDER_CONCURRENCY and defaults to one", () => {
    expect(services["worker-render"].environment?.RENDER_CONCURRENCY).toBe(
      "${RENDER_CONCURRENCY:-1}",
    );
  });

  it("the layout stage and the renderer read the caption font from the same setting", () => {
    const media = services["worker-media"].environment?.CAPTION_FONT_PATH;
    const render = services["worker-render"].environment?.CAPTION_FONT_PATH;
    expect(media).toBeDefined();
    expect(media).toBe(render);
  });

  it("the renderer image is pinned: base images by digest, Chrome by checksum", () => {
    const dockerfile = readFileSync(
      path.join(root, "services", "renderer", "Dockerfile"),
      "utf-8",
    );
    const bases = dockerfile.split("\n").filter((l) => /^FROM /.test(l));
    expect(bases.length).toBeGreaterThanOrEqual(2);
    for (const line of bases) expect(line).toMatch(/@sha256:[0-9a-f]{64}/);
    // The browser is downloaded in the image and refused unless its hash matches.
    expect(dockerfile).toMatch(/CHROME_SHA256_X64=[0-9a-f]{64}/);
    expect(dockerfile).toMatch(/CHROME_SHA256_ARM64=[0-9a-f]{64}/);
    expect(dockerfile).toContain("sha256sum -c -");
    expect(dockerfile).toContain("--frozen-lockfile");
  });

  it("every added image is pinned by digest, including the one inside the MinIO init Dockerfile", () => {
    for (const name of added) {
      const image: string | undefined = services[name].image;
      if (image === undefined) continue; // minio-init is built, checked below
      expect({ name, image }).toEqual({ name, image: expect.stringMatching(/@sha256:[0-9a-f]{64}$/) });
    }
    const dockerfile = readFileSync(path.join(root, "infra", "minio", "Dockerfile"), "utf-8");
    // Every image the Dockerfile pulls: the base, plus the mc binary copied in.
    const pulls = dockerfile.split("\n").filter((l) => /^(FROM|COPY --from=)/.test(l));
    expect(pulls.length).toBeGreaterThanOrEqual(2);
    for (const line of pulls) expect(line).toMatch(/@sha256:[0-9a-f]{64}/);
  });

  it("one-shot init services never restart, and dependents wait for them to COMPLETE", () => {
    for (const name of ["db-init", "temporal-admin-tools", "temporal-namespace-init", "minio-init"]) {
      expect(String(services[name].restart)).toBe("no");
    }
    // `service_started` on an init container is satisfied the instant it
    // launches, i.e. before it has done anything.
    expect(services["temporal-admin-tools"].depends_on["db-init"].condition).toBe("service_completed_successfully");
    expect(services.temporal.depends_on["temporal-admin-tools"].condition).toBe("service_completed_successfully");
    expect(services["langfuse-web"].depends_on["db-init"].condition).toBe("service_completed_successfully");
    expect(services["langfuse-web"].depends_on["minio-init"].condition).toBe("service_completed_successfully");
  });

  it("redis refuses eviction and loads the ACL file", () => {
    const command = JSON.stringify(services.redis.command);
    expect(command).toContain("--maxmemory-policy noeviction");
    expect(command).toContain("--aclfile");
  });

  it("clickhouse has a memory ceiling", () => {
    expect(services.clickhouse.mem_limit).toBeDefined();
  });
});
