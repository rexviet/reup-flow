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
    API_KEY: z
      .string()
      .min(1, "API_KEY is required")
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
  })
  .superRefine((val, ctx) => {
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

/**
 * Env vars whose values are secrets and must never reach stderr/logs
 * verbatim. Used only by {@link redactSecrets}.
 */
const REDACTABLE_ENV_KEYS = [
  "API_KEY",
  "DATABASE_URL",
  "REDIS_URL",
  "DOUYIN_COOKIE_FALLBACK",
] as const;

/**
 * Boundary redactor (D-3) for main.ts's non-EnvValidationError bootstrap
 * catch-all. Infra clients (DB/Redis, wired up starting phase 1) commonly
 * embed the connection string -- including the password -- in a connection
 * error's message or stack. This scrubs known-secret env values before the
 * error is written to stderr. This is a boundary stopgap only: phase 3's
 * logger layer replaces it with the real redaction implementation.
 */
export function redactSecrets(
  text: string,
  source: NodeJS.ProcessEnv = process.env
): string {
  return REDACTABLE_ENV_KEYS.reduce((acc, key) => {
    const value = source[key];
    if (!value) return acc;
    return acc.split(value).join("[redacted]");
  }, text);
}
