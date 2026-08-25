// `./bootstrap` installs the stdio redaction patch at module scope, so this
// import must stay FIRST and must not be reordered by a formatter or an
// "organise imports" action: everything below it is allowed to write to
// stderr, and nothing may do so before the patch is in place.
import { createApp } from "./bootstrap";
import { EnvValidationError } from "./config/env";

async function bootstrap(): Promise<void> {
  const { app, env } = await createApp();
  // PORT: non-negative integer <= 65535, validated by env.ts. 0 means "let
  // the OS pick a free ephemeral port" and must stay valid (see the
  // bootstrap positive control in bootstrap-failure.spec.ts).
  await app.listen(env.PORT);
}

bootstrap().catch((err: unknown) => {
  if (err instanceof EnvValidationError) {
    process.stderr.write(`${err.message}\n`);
  } else {
    // Deliberately NO explicit redact() call on this path. DbModule's and
    // RedisModule's boot probes (src/db/db.module.ts, src/redis/redis.module.ts)
    // embed the live DSN -- including the password -- in their thrown
    // message ON PURPOSE (plan §0.3: the defence against a secret in an
    // error is the redaction LAYER, never call-site discipline). The ONLY
    // thing standing between that message and this terminal is the stdio
    // patch installed by ./bootstrap at import time. That is the point: it
    // makes the redaction layer load-bearing rather than decorative, which
    // is what test/bootstrap-failure.spec.ts's D-3 case actually proves by
    // driving a real ECONNREFUSED through this exact path. Do not add a
    // redact() call here -- doing so would make the stdio patch redundant
    // for this one path and remove the only realistic, unmocked test of it.
    const message = err instanceof Error ? (err.stack ?? err.message) : String(err);
    process.stderr.write(`Fatal error during bootstrap: ${message}\n`);
  }
  process.exit(1);
});
