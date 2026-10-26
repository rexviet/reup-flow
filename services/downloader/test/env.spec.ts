import { parseEnv } from "../src/config/env";

const API_KEY_SENTINEL = "sk-canary-do-not-leak-9f3ac21e";

function validEnv(overrides: Record<string, string | undefined> = {}) {
  const base: Record<string, string> = {
    API_KEY: API_KEY_SENTINEL,
    DATABASE_URL: "postgres://user:pass@localhost:5432/reup",
    REDIS_URL: "redis://localhost:6379",
    PARSER_BASE_URL: "http://parser:80",
    PROBE_SEC_USER_ID: "MS4wLjABAAAA_probe",
    DATA_DIR: "/data",
    DOWNLOAD_CONCURRENCY: "6",
    ENUMERATE_SLEEP_MS_MIN: "3000",
    ENUMERATE_SLEEP_MS_MAX: "5000",
    LEASE_SECONDS: "300",
    MAX_ATTEMPTS: "3",
    URL_TTL_SECONDS: "7200",
    DISK_RESERVE_BYTES: "10737418240",
    EST_BYTES_PER_VIDEO: "31457280",
    PORT: "3000",
  };
  const merged: Record<string, string | undefined> = { ...base, ...overrides };
  for (const key of Object.keys(merged)) {
    if (merged[key] === undefined) delete merged[key];
  }
  return merged as NodeJS.ProcessEnv;
}

describe("parseEnv", () => {
  it("parses a valid full env and coerces numbers to type number", () => {
    const env = parseEnv(validEnv());

    expect(env.DOWNLOAD_CONCURRENCY).toBe(6);
    expect(typeof env.DOWNLOAD_CONCURRENCY).toBe("number");
    expect(env.ENUMERATE_SLEEP_MS_MIN).toBe(3000);
    expect(typeof env.ENUMERATE_SLEEP_MS_MIN).toBe("number");
    expect(env.API_KEY).toBe(API_KEY_SENTINEL);
  });

  it("throws naming DATABASE_URL when it is missing", () => {
    const env = validEnv({ DATABASE_URL: undefined });

    expect(() => parseEnv(env)).toThrow(/DATABASE_URL/);
  });

  it("throws naming all missing vars at once, not just the first", () => {
    const env = validEnv({
      DATABASE_URL: undefined,
      REDIS_URL: undefined,
      PARSER_BASE_URL: undefined,
    });

    let message = "";
    try {
      parseEnv(env);
      throw new Error("expected parseEnv to throw");
    } catch (err) {
      message = (err as Error).message;
    }

    expect(message).toMatch(/DATABASE_URL/);
    expect(message).toMatch(/REDIS_URL/);
    expect(message).toMatch(/PARSER_BASE_URL/);
  });

  it.each(["0", "-1", "abc"])(
    "throws when DOWNLOAD_CONCURRENCY=%s",
    (value) => {
      const env = validEnv({ DOWNLOAD_CONCURRENCY: value });
      expect(() => parseEnv(env)).toThrow(/DOWNLOAD_CONCURRENCY/);
    }
  );

  it("throws when ENUMERATE_SLEEP_MS_MIN > ENUMERATE_SLEEP_MS_MAX", () => {
    const env = validEnv({
      ENUMERATE_SLEEP_MS_MIN: "6000",
      ENUMERATE_SLEEP_MS_MAX: "5000",
    });

    expect(() => parseEnv(env)).toThrow(/ENUMERATE_SLEEP_MS_MIN/);
  });

  it("applies defaults when optional vars are absent", () => {
    const env = validEnv({
      DATA_DIR: undefined,
      DOWNLOAD_CONCURRENCY: undefined,
      ENUMERATE_SLEEP_MS_MIN: undefined,
      ENUMERATE_SLEEP_MS_MAX: undefined,
      LEASE_SECONDS: undefined,
      MAX_ATTEMPTS: undefined,
      URL_TTL_SECONDS: undefined,
      DISK_RESERVE_BYTES: undefined,
      EST_BYTES_PER_VIDEO: undefined,
      PORT: undefined,
    });

    const parsed = parseEnv(env);

    expect(parsed.DATA_DIR).toBe("/data");
    expect(parsed.DOWNLOAD_CONCURRENCY).toBe(6);
    expect(parsed.ENUMERATE_SLEEP_MS_MIN).toBe(3000);
    expect(parsed.ENUMERATE_SLEEP_MS_MAX).toBe(5000);
    expect(parsed.LEASE_SECONDS).toBe(300);
    expect(parsed.MAX_ATTEMPTS).toBe(3);
    expect(parsed.URL_TTL_SECONDS).toBe(7200);
    expect(parsed.DISK_RESERVE_BYTES).toBe(10737418240);
    expect(parsed.EST_BYTES_PER_VIDEO).toBe(31457280);
    expect(parsed.PORT).toBe(3000);
  });

  // FG-2: the previous version of this test supplied a *valid* API_KEY, so
  // zod never raised an issue whose path was API_KEY -- meaning no
  // realistic per-issue leak formatter could ever trip the assertion.
  // Confirmed by mutation: interpolating `issue.received` into the message
  // left this green because there was no API_KEY issue to leak from.
  // Fixed by (a) giving API_KEY a real shape constraint (env.ts) so an
  // invalid API_KEY produces a genuine issue, and (b) sweeping every
  // validated field below instead of asserting on API_KEY alone.
  it("never echoes the API_KEY value in the thrown error, even when API_KEY itself is invalid", () => {
    const invalidApiKey = `${API_KEY_SENTINEL}!!not-allowed-chars!!`;
    const env = validEnv({
      API_KEY: invalidApiKey,
      DATABASE_URL: undefined,
      DOWNLOAD_CONCURRENCY: "abc",
    });

    let message = "";
    try {
      parseEnv(env);
      throw new Error("expected parseEnv to throw");
    } catch (err) {
      message = (err as Error).message;
    }

    expect(message).toMatch(/API_KEY/);
    expect(message).not.toContain(invalidApiKey);
    expect(message).not.toContain(API_KEY_SENTINEL);
  });

  // FG-2 sweep: give every validated field (not just API_KEY) an invalid,
  // uniquely-identifiable sentinel value in a single parse, and assert none
  // of those raw values ever appear in the thrown message -- only the field
  // names and static messages may. This is what actually catches a future
  // per-issue leak formatter (e.g. `${path}: ${message} (received: ${raw})`)
  // regardless of which field it happens to be applied to.
  it("never echoes any raw invalid value in the thrown error (mutation sweep across all validated fields)", () => {
    const sentinels: Record<string, string> = {
      API_KEY: "leak-marker-api-key!!bad-chars!!",
      DOWNLOAD_CONCURRENCY: "leak-marker-download-concurrency",
      ENUMERATE_SLEEP_MS_MIN: "leak-marker-enumerate-sleep-min",
      ENUMERATE_SLEEP_MS_MAX: "leak-marker-enumerate-sleep-max",
      LEASE_SECONDS: "leak-marker-lease-seconds",
      MAX_ATTEMPTS: "leak-marker-max-attempts",
      URL_TTL_SECONDS: "leak-marker-url-ttl-seconds",
      DISK_RESERVE_BYTES: "leak-marker-disk-reserve-bytes",
      EST_BYTES_PER_VIDEO: "leak-marker-est-bytes-per-video",
      PORT: "leak-marker-port",
    };
    const env = validEnv(sentinels);

    let message = "";
    try {
      parseEnv(env);
      throw new Error("expected parseEnv to throw");
    } catch (err) {
      message = (err as Error).message;
    }

    for (const [field, sentinel] of Object.entries(sentinels)) {
      expect(message).toMatch(new RegExp(field));
      expect(message).not.toContain(sentinel);
    }
  });
});

describe("parseEnv required variables (FG-3)", () => {
  // FG-3: the previous suite had no case covering required-ness of API_KEY
  // or PROBE_SEC_USER_ID, nor the non-empty rule on DATA_DIR. Confirmed by
  // mutation: making both optional-with-""-default left the entire suite
  // green. This sweeps every required var individually.
  it.each([
    "API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "PARSER_BASE_URL",
    "PROBE_SEC_USER_ID",
  ])("throws naming %s when it is omitted", (key) => {
    const env = validEnv({ [key]: undefined });
    expect(() => parseEnv(env)).toThrow(new RegExp(key));
  });

  it("throws naming DATA_DIR when it is an empty string", () => {
    const env = validEnv({ DATA_DIR: "" });
    expect(() => parseEnv(env)).toThrow(/DATA_DIR/);
  });
});

describe("API_KEY minimum length (FIX 8, review)", () => {
  // A 1-character API_KEY used to pass the regex-only shape check
  // (`^[A-Za-z0-9_-]+$`) with nothing enforcing a floor at all -- trivially
  // brute-forceable, and short enough that
  // `src/redaction/redactor.ts`'s `registerSecret` would silently refuse to
  // register it (it requires `MIN_REGISTERED_SECRET_LENGTH`, 8, characters),
  // leaving the key both guessable AND unprotected by the exact-value
  // registry the moment it appeared in a log line.
  it.each(["a", "1234567890123456".slice(0, 15)])(
    "rejects an API_KEY under 16 characters (length %#)",
    (value) => {
      expect(() => parseEnv(validEnv({ API_KEY: value }))).toThrow(/API_KEY/);
    },
  );

  it("accepts an API_KEY of exactly 16 characters", () => {
    const sixteen = "abcdEFGH01234567".slice(0, 16);
    expect(parseEnv(validEnv({ API_KEY: sixteen })).API_KEY).toBe(sixteen);
  });
});

describe("parseEnv PORT (D-6)", () => {
  it.each(["-1", "65536", "abc", "3.5"])(
    "throws naming PORT when PORT=%s",
    (value) => {
      const env = validEnv({ PORT: value });
      expect(() => parseEnv(env)).toThrow(/PORT/);
    }
  );

  // PORT=0 must stay valid: it means "let the OS pick a free port" and the
  // bootstrap positive control (bootstrap-failure.spec.ts) relies on it.
  it("allows PORT=0", () => {
    const env = validEnv({ PORT: "0" });
    expect(parseEnv(env).PORT).toBe(0);
  });

  it("defaults PORT to 3000 when absent", () => {
    const env = validEnv({ PORT: undefined });
    expect(parseEnv(env).PORT).toBe(3000);
  });
});

describe("PARSER_BASE_URL", () => {
  // ParserClient builds every route with `new URL(path, PARSER_BASE_URL)`. A
  // value that is non-empty but not a URL passes a `min(1)` check and then
  // throws `TypeError [ERR_INVALID_URL]` on the first call -- at runtime, in a
  // lane, as an error type no `catch (ParserError)` will match. Fail at
  // bootstrap instead, which is what every other var here already does.
  it.each([
    ["a bare host:port", "parser:80"],
    ["a path only", "/api/douyin"],
    ["plain words", "not a url"],
  ])("rejects %s", (_label, value) => {
    expect(() => parseEnv(validEnv({ PARSER_BASE_URL: value }))).toThrow(/PARSER_BASE_URL/);
  });

  it.each([
    ["the compose service address", "http://parser:80"],
    ["a host with a trailing slash", "http://parser/"],
    ["https", "https://parser.internal:8443"],
  ])("accepts %s", (_label, value) => {
    expect(parseEnv(validEnv({ PARSER_BASE_URL: value })).PARSER_BASE_URL).toBe(value);
  });
});

describe("LANES_ENABLED (phase 4 §4.2)", () => {
  it("defaults to true, so `docker compose up` does work without extra configuration", () => {
    expect(parseEnv(validEnv()).LANES_ENABLED).toBe(true);
  });

  it.each([["false"], ["FALSE"], ["0"], ["no"], ["off"], [" false "]])(
    "reads %p as false",
    (raw) => {
      // THE case this exists for. `z.coerce.boolean()` applies JavaScript
      // truthiness, under which the string "false" -- the single most likely
      // way anybody writes this in a compose file -- is `true`. So are "0" and
      // "no". A variable whose entire purpose is turning the lanes off would
      // have left them on, in the direction nobody checks, with the process
      // looking healthy the whole time.
      expect(parseEnv(validEnv({ LANES_ENABLED: raw })).LANES_ENABLED).toBe(false);
    },
  );

  it.each([["true"], ["TRUE"], ["1"], ["yes"], ["on"]])("reads %p as true", (raw) => {
    expect(parseEnv(validEnv({ LANES_ENABLED: raw })).LANES_ENABLED).toBe(true);
  });

  it.each([["flase"], ["maybe"], [""], ["2"]])("REJECTS the unrecognised value %p", (raw) => {
    // A typo must fail at boot, where parseEnv names the variable, rather than
    // silently taking a default. Either default is wrong here: `true` runs
    // lanes on a node meant to be API-only, `false` leaves a queue with nobody
    // draining it. Both are silent.
    expect(() => parseEnv(validEnv({ LANES_ENABLED: raw }))).toThrow(/LANES_ENABLED/);
  });
});

describe("storage backend configuration", () => {
  const S3 = {
    STORAGE_BACKEND: "s3",
    S3_ENDPOINT: "http://minio:9000",
    S3_ACCESS_KEY_ID: "downloader",
    S3_SECRET_ACCESS_KEY: "s3-secret-canary-0123456789",
  };

  it("defaults to the local backend and needs no S3 settings", () => {
    const parsed = parseEnv(validEnv());
    expect(parsed.STORAGE_BACKEND).toBe("local");
    expect(parsed.S3_KEY_PREFIX).toBe("source/");
    expect(parsed.S3_STALE_UPLOAD_MS).toBe(86_400_000);
  });

  it("treats the blank S3 values compose interpolates for an unset variable as unset", () => {
    const parsed = parseEnv(
      validEnv({ S3_ENDPOINT: "", S3_ACCESS_KEY_ID: "", S3_SECRET_ACCESS_KEY: "" }),
    );
    expect(parsed.S3_ENDPOINT).toBeUndefined();
    expect(parsed.S3_ACCESS_KEY_ID).toBeUndefined();
  });

  it("accepts a complete s3 configuration", () => {
    const parsed = parseEnv(validEnv(S3));
    expect(parsed.STORAGE_BACKEND).toBe("s3");
    expect(parsed.S3_BUCKET).toBe("reup-media");
    expect(parsed.S3_FORCE_PATH_STYLE).toBe(true);
  });

  it.each(["S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"])(
    "names %s when the s3 backend is selected without it",
    (key) => {
      expect(() => parseEnv(validEnv({ ...S3, [key]: undefined }))).toThrow(new RegExp(key));
    },
  );

  it.each([[""], ["work/"], ["output/x/"], ["source"], ["sourcex/"], ["source/a"]])(
    "rejects the S3_KEY_PREFIX %j",
    (prefix) => {
      expect(() => parseEnv(validEnv({ ...S3, S3_KEY_PREFIX: prefix }))).toThrow(/S3_KEY_PREFIX/);
    },
  );

  it("accepts a prefix nested under source/", () => {
    expect(parseEnv(validEnv({ ...S3, S3_KEY_PREFIX: "source/tenant-a/" })).S3_KEY_PREFIX).toBe(
      "source/tenant-a/",
    );
  });

  it("never echoes the S3 secret in a validation error", () => {
    let message = "";
    try {
      parseEnv(validEnv({ ...S3, S3_KEY_PREFIX: "work/", S3_STALE_UPLOAD_MS: "-1" }));
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toMatch(/S3_KEY_PREFIX/);
    expect(message).not.toContain(S3.S3_SECRET_ACCESS_KEY);
  });
});
