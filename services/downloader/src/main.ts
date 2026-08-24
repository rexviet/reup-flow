import "reflect-metadata";
import { NestFactory } from "@nestjs/core";
import { AppModule } from "./app.module";
import { parseEnv, EnvValidationError, redactSecrets } from "./config/env";

async function bootstrap(): Promise<void> {
  // Fail fast: validate configuration before the Nest container is even
  // created, so a missing/invalid var crashes at startup, not on first job.
  const env = parseEnv();

  // Test-only fault injection (D-3): lets bootstrap-failure.spec.ts exercise
  // the non-EnvValidationError catch-all below -- the real trigger (a DB/
  // Redis connection error) doesn't exist until phase 1 wires up those
  // clients. Never set outside tests; guarded by an explicit, unmistakable
  // env var name so it can't fire by accident in any real environment.
  // Belt and braces: the runtime image sets NODE_ENV=production (see
  // services/downloader/Dockerfile), so this branch is unreachable in the
  // shipped container no matter what anyone puts in the environment.
  if (
    process.env.NODE_ENV !== "production" &&
    process.env.__TEST_ONLY_SIMULATE_BOOTSTRAP_ERROR__
  ) {
    throw new Error(
      `connect ECONNREFUSED 127.0.0.1:5432 -- connection string was ${env.DATABASE_URL}`
    );
  }

  const app = await NestFactory.create(AppModule);
  // PORT: non-negative integer <= 65535, validated by env.ts. 0 means "let
  // the OS pick a free ephemeral port" and must stay valid (see the
  // bootstrap positive control).
  await app.listen(env.PORT);
}

bootstrap().catch((err: unknown) => {
  if (err instanceof EnvValidationError) {
    process.stderr.write(`${err.message}\n`);
  } else {
    // D-3: redact known secrets (DB/Redis DSNs, API_KEY, cookie fallback)
    // before this reaches stderr -- connection errors commonly embed the
    // DSN password verbatim. Phase 3 replaces this with real logger-layer
    // redaction; this is a boundary stopgap for the interim.
    const message = err instanceof Error ? (err.stack ?? err.message) : String(err);
    process.stderr.write(`Fatal error during bootstrap: ${redactSecrets(message)}\n`);
  }
  process.exit(1);
});
