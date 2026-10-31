import { z } from "zod";

/**
 * Every numeric env var must be a positive integer. `z.coerce.number()`
 * turns non-numeric strings into `NaN`, which still has typeof "number" and
 * so slips past a bare `z.number()` check -- the `.int()` refinement below is
 * what actually rejects it.
 */
function positiveInt(label: string) {
  return z.coerce
    .number({ invalid_type_error: `${label} must be a positive integer` })
    .int(`${label} must be a positive integer`)
    .positive(`${label} must be a positive integer`);
}

/**
 * PORT is the one numeric var that legitimately allows 0 (meaning "let the
 * OS pick a free ephemeral port" -- relied on by the bootstrap positive
 * control in bootstrap-failure.spec.ts). So it is a non-negative integer
 * bounded by the valid TCP port range, not a positive integer like the
 * other numeric vars.
 */
function boundedInt(label: string, min: number, max: number) {
  const message = `${label} must be an integer between ${min} and ${max}`;
  return z.coerce
    .number({ invalid_type_error: message })
    .int(message)
    .min(min, message)
    .max(max, message);
}

/**
 * An optional string where an empty value means "not set". docker-compose
 * interpolates an unset `${VAR:-}` to `""`, and a local-backend deployment
 * must not fail validation over credentials it never uses; the s3 backend's
 * superRefine still rejects them when they are actually needed.
 */
function blankAsUnset() {
  return z
    .string()
    .optional()
    .transform((value) => (value === undefined || value === "" ? undefined : value));
}

/**
 * An env var read as a boolean.
 *
 * NOT `z.coerce.boolean()`, which is a trap here: it applies JavaScript
 * truthiness, so the string `"false"` -- the single most likely way anybody
 * writes this in a compose file or a `.env` -- coerces to `true`. So does
 * `"0"`, and so does `"no"`. A var whose whole purpose is to turn the lanes
 * off would silently leave them on.
 *
 * An explicit allowlist instead, and an unrecognised value is an error rather
 * than a default: a typo'd `LANES_ENABLE=flase` must fail at boot, where
 * `parseEnv` reports it, not six hours later when someone wonders why this
 * node is processing jobs.
 */
function booleanFlag(label: string) {
  return z
    .union([z.boolean(), z.string()])
    .transform((value, ctx) => {
      if (typeof value === "boolean") return value;
      const normalised = value.trim().toLowerCase();
      if (["true", "1", "yes", "on"].includes(normalised)) return true;
      if (["false", "0", "no", "off"].includes(normalised)) return false;
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${label} must be one of true/false/1/0/yes/no/on/off`,
      });
      return z.NEVER;
    });
}

/**
 * Whether a value can serve as the base of `new URL(path, base)`.
 *
 * `z.string().url()` is not enough here, and the gap is not obvious:
 * `new URL("parser:80")` SUCCEEDS -- scheme `parser:`, path `80` -- so a
 * bare host:port passes a URL check and then throws ERR_INVALID_URL the
 * moment ParserClient resolves a route against it, inside a lane, as an error
 * type no parser-error handler matches. Requiring an http(s) scheme AND a
 * host is what actually rules that out.
 */
function isUsableAsBaseUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && url.host !== "";
  } catch {
    return false;
  }
}

export const envSchema = z
  .object({
    // Constrained to a plausible credential charset (not just "non-empty")
    // so that an invalid API_KEY produces a genuine zod issue on the
    // API_KEY path -- see the API_KEY-secrecy sweep in env.spec.ts.
    //
    // FIX 8 (review): floored at 16 characters, not just "present". A
    // 1-character API_KEY passed the charset check with nothing enforcing a
    // minimum length at all -- trivially brute-forceable, and short enough
    // that `redaction/redactor.ts`'s `registerSecret` would silently refuse
    // to register it (its own floor is `MIN_REGISTERED_SECRET_LENGTH`, 8),
    // leaving the key both guessable AND unprotected by the exact-value
    // registry the moment it appeared in a log line. 16 is comfortably above
    // that registry floor with headroom to spare.
    API_KEY: z
      .string()
      .min(16, "API_KEY must be at least 16 characters")
      .regex(
        /^[A-Za-z0-9_-]+$/,
        "API_KEY must contain only letters, digits, underscores, or hyphens"
      ),
    DATABASE_URL: z.string().min(1, "DATABASE_URL is required"),
    REDIS_URL: z.string().min(1, "REDIS_URL is required"),
    // A URL, not merely non-empty: ParserClient builds every route with
    // `new URL(path, PARSER_BASE_URL)`, and a value like `parser:80` passes a
    // length check only to throw ERR_INVALID_URL on the first call -- inside a
    // lane, as an error type no parser-error handler matches.
    PARSER_BASE_URL: z
      .string()
      .min(1, "PARSER_BASE_URL is required")
      .refine(isUsableAsBaseUrl, {
        message: "PARSER_BASE_URL must be an absolute http(s) URL with a host, e.g. http://parser:80",
      }),
    // Known-public Douyin profile id, used only by the phase-7 F4 probe.
    PROBE_SEC_USER_ID: z.string().min(1, "PROBE_SEC_USER_ID is required"),
    /**
     * Browser origins allowed to call this API cross-origin, comma-separated.
     *
     * `services/web` is served from a DIFFERENT origin than this API in every
     * configuration that exists: `pnpm dev` puts it on :5173, and the compose
     * `web` service puts it on :5173 via nginx while the browser still calls
     * this service on :3000. So every request the UI makes is cross-origin,
     * and because it carries `X-Api-Key` -- not a CORS-safelisted header --
     * the browser sends a preflight `OPTIONS` first, every time.
     *
     * Without this, Nest has no OPTIONS handler, the preflight 404s with no
     * `Access-Control-Allow-Origin`, and the browser blocks the real request.
     * curl never sees it: curl does not implement CORS. Phase 8's tests mock
     * `fetch`, so they cannot see it either -- the same blindness plan §5's
     * ninth rule was written about after phase 9's extension hit it.
     *
     * An explicit allowlist, never `origin: true` (which reflects any
     * Origin back): the API key travels in a header the page holds in
     * plaintext, so a reflected origin would let any site a browser visits
     * drive this API with it.
     */
    CORS_ALLOWED_ORIGINS: z
      .string()
      .default("http://localhost:5173")
      .transform((raw) =>
        raw
          .split(",")
          .map((origin) => origin.trim())
          .filter((origin) => origin.length > 0)
      ),
    /**
     * Where the workspace page reads workflows from. Read-only: the downloader
     * never starts or signals a workflow. The connection is lazy, so an
     * unreachable address costs the page its Temporal data, not the boot.
     */
    TEMPORAL_ADDRESS: z.string().min(1, "TEMPORAL_ADDRESS must not be empty").default("temporal:7233"),
    TEMPORAL_NAMESPACE: z.string().min(1, "TEMPORAL_NAMESPACE must not be empty").default("default"),
    DATA_DIR: z.string().min(1, "DATA_DIR must not be empty").default("/data"),
    DOWNLOAD_CONCURRENCY: positiveInt("DOWNLOAD_CONCURRENCY").default(6),
    ENUMERATE_SLEEP_MS_MIN: positiveInt("ENUMERATE_SLEEP_MS_MIN").default(3000),
    ENUMERATE_SLEEP_MS_MAX: positiveInt("ENUMERATE_SLEEP_MS_MAX").default(5000),
    LEASE_SECONDS: positiveInt("LEASE_SECONDS").default(300),
    MAX_ATTEMPTS: positiveInt("MAX_ATTEMPTS").default(3),
    URL_TTL_SECONDS: positiveInt("URL_TTL_SECONDS").default(7200),
    DISK_RESERVE_BYTES: positiveInt("DISK_RESERVE_BYTES").default(10737418240),
    EST_BYTES_PER_VIDEO: positiveInt("EST_BYTES_PER_VIDEO").default(31457280),
    PORT: boundedInt("PORT", 0, 65535).default(3000),
    /**
     * Whether THIS process runs the lanes (phase 4 §4.2). Default true, so
     * `docker compose up` needs no extra configuration to do any work.
     *
     * The switch is what makes plan §0 principle 1 -- "Postgres is the only
     * channel between the HTTP layer and a lane" -- cash out today rather
     * than in principle. With it false the process is an API-only node;
     * lanes run wherever it is true, in this process or another one, with no
     * logic changed either way. That was the property principle 1 was for.
     *
     * It is also how every `AppModule`-based e2e test avoids starting a
     * reaper against the live compose stack as a side effect of booting.
     */
    LANES_ENABLED: booleanFlag("LANES_ENABLED").default(true),
    /**
     * Where committed videos live. `local` keeps the DATA_DIR behaviour; `s3`
     * speaks the S3 API (MinIO locally, S3 or R2 in production) and needs the
     * S3_* group below, which the superRefine enforces only for that backend so
     * a local deployment configures nothing new.
     */
    STORAGE_BACKEND: z.enum(["local", "s3"]).default("local"),
    S3_ENDPOINT: blankAsUnset(),
    S3_REGION: z.string().min(1, "S3_REGION must not be empty").default("us-east-1"),
    S3_BUCKET: z.string().min(1, "S3_BUCKET must not be empty").default("reup-media"),
    S3_FORCE_PATH_STYLE: booleanFlag("S3_FORCE_PATH_STYLE").default(true),
    /**
     * Prefix every object key is written under. Non-empty, and it must sit
     * under `source/`: the downloader's credential is granted nothing outside
     * `source/*`, so any other prefix would boot cleanly and then fail every put
     * with AccessDenied. The trailing slash makes the prefix a directory
     * boundary (`source/a` must not also match `source/ab/`).
     */
    S3_KEY_PREFIX: z
      .string()
      .default("source/")
      .refine((v) => v.startsWith("source/") && v.endsWith("/"), {
        message: "S3_KEY_PREFIX must start with 'source/' and end with '/'",
      }),
    S3_ACCESS_KEY_ID: blankAsUnset(),
    S3_SECRET_ACCESS_KEY: blankAsUnset(),
    /**
     * Age past which an unfinished multipart upload is garbage. NOT the lease:
     * an upload's `Initiated` time never moves while it is still running, and a
     * second process (a migration script, the old pod during a rolling deploy)
     * is invisible to this process's in-flight registry.
     */
    S3_STALE_UPLOAD_MS: positiveInt("S3_STALE_UPLOAD_MS").default(86_400_000),
  })
  .superRefine((val, ctx) => {
    if (val.STORAGE_BACKEND === "s3") {
      for (const name of ["S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"] as const) {
        if (!val[name]) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: [name],
            message: `${name} is required when STORAGE_BACKEND=s3`,
          });
        }
      }
    }
    if (val.ENUMERATE_SLEEP_MS_MIN > val.ENUMERATE_SLEEP_MS_MAX) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["ENUMERATE_SLEEP_MS_MIN"],
        message: "ENUMERATE_SLEEP_MS_MIN must be <= ENUMERATE_SLEEP_MS_MAX",
      });
    }
  });

export type Env = z.infer<typeof envSchema>;

export class EnvValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EnvValidationError";
  }
}

/**
 * Parses and validates process.env (or a provided source) against the
 * downloader service's required configuration contract.
 *
 * Throws EnvValidationError listing every missing/invalid variable at once.
 * The error message is built ONLY from zod issue `path` + `message` --
 * never from the offending raw value -- so a bad API_KEY can never leak into
 * logs or crash output. Do not change this to interpolate `issue.received`
 * or similar without re-reading the API_KEY-secrecy test in env.spec.ts.
 */
export function parseEnv(source: NodeJS.ProcessEnv = process.env): Env {
  const result = envSchema.safeParse(source);
  if (!result.success) {
    const messages = result.error.issues.map((issue) => {
      const path = issue.path.join(".") || "(root)";
      return `${path}: ${issue.message}`;
    });
    throw new EnvValidationError(
      `Invalid environment configuration:\n${messages.join("\n")}`
    );
  }
  return result.data;
}

