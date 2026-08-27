import "reflect-metadata";
import { NestFactory } from "@nestjs/core";
import type { INestApplication } from "@nestjs/common";
import { AppModule } from "./app.module";
import { parseEnv, type Env } from "./config/env";
import { installStdioRedaction } from "./redaction/stdio-redaction";
import { redactor } from "./redaction/redactor";
import { RedactingLogger } from "./logging/redact.logger";

// Installed at MODULE scope, not inside createApp(), so it runs as soon as
// this module's own top-level code executes -- before `parseEnv`, before
// `NestFactory.create` runs, before this function's caller has done
// anything else. It is NOT installed before AppModule's dependency graph
// exists: TypeScript hoists all `require`s to the top of the compiled file,
// so `./app.module` (and everything it imports) is already evaluated by the
// time this statement runs -- there is no ordering guarantee against
// something written during that module evaluation itself. What this DOES
// guarantee is that every write from this process's OWN CODE RUNNING
// afterwards -- `console.log`, a bare `process.stderr.write`, a third-party
// library's own debug output, and the Nest logger created below -- goes
// through the pattern mechanism (known Douyin/TikTok cookie field shapes,
// see redactor.ts). See plan §0.3 and the phase-1 lesson -- the historical
// leak was a `print()`, which a logger-only redaction test let straight
// through.
installStdioRedaction();

/**
 * Builds the application exactly as production does.
 *
 * This lives here rather than inline in main.ts for one reason: it is the
 * bootstrap that test/redaction/cookie-leak.e2e-spec.ts's child process
 * runs. A harness that re-implemented these steps would still pass every
 * assertion after somebody reordered or deleted one of them in main.ts --
 * which is precisely the class of regression that test exists to catch. One
 * definition, two callers, no drift.
 *
 * main.ts keeps ownership of what it alone can own: `listen()` and the
 * top-level crash handler.
 */
export async function createApp(): Promise<{ app: INestApplication; env: Env }> {
  // Fail fast: validate configuration before the Nest container is even
  // created, so a missing/invalid var crashes at startup, not on first job.
  const env = parseEnv();

  // Register every known secret VALUE with the redactor now that the values
  // exist. This is the exact-value mechanism (redactor.ts's registerSecret),
  // independent of the pattern mechanism above -- it is what catches a DSN
  // password or the raw API key verbatim, neither of which has any
  // cookie-shaped `field=value` form for the pattern to recognise.
  // DOUYIN_COOKIE_FALLBACK is read straight off process.env (not env.*)
  // because it is a parser-only variable this service never validates or
  // depends on -- see docker-compose.yml's comment on why the `nest`
  // container is not even given it in production. Registering it here covers
  // the case where it reaches this process's environment anyway, e.g. via a
  // shared `.env` in local dev.
  redactor.registerSecret(env.API_KEY);
  redactor.registerSecret(env.DATABASE_URL);
  redactor.registerSecret(env.REDIS_URL);
  if (process.env.DOUYIN_COOKIE_FALLBACK) {
    redactor.registerSecret(process.env.DOUYIN_COOKIE_FALLBACK);
  }

  const app = await NestFactory.create(AppModule, {
    logger: new RedactingLogger("Bootstrap"),
  });
  configureApp(app, env.CORS_ALLOWED_ORIGINS);
  return { app, env };
}

/**
 * Everything that must be done to a built app before it serves traffic.
 *
 * Split out of `createApp` for the same reason `createApp` itself exists: a
 * test that needs the production wiring with ONE provider swapped has to build
 * its own testing module, and it must be able to apply this configuration
 * without re-implementing it. One definition, two callers, no drift -- exactly
 * the argument in `createApp`'s own comment.
 *
 * `enableShutdownHooks()` is the load-bearing line. Without it Nest installs
 * no signal listeners at all, so `SIGTERM` kills the process outright:
 * `onApplicationShutdown` never fires, `LaneSupervisor` never stops a lane,
 * and every claim the lanes were holding keeps its lease for the full
 * `LEASE_SECONDS`. The restarted process then sits next to work it is not
 * allowed to touch for five minutes -- which is precisely the symptom §4.2's
 * DoD line forbids, and it is invisible in every in-process test because
 * those call `app.close()` directly. `test/lanes/sigterm-shutdown.e2e-spec.ts`
 * drives a real signal at a real child process to cover it.
 */
export function configureApp(
  app: INestApplication,
  corsAllowedOrigins: readonly string[] = [],
): void {
  app.enableShutdownHooks();

  // Empty by default so the only caller that does NOT serve a browser
  // (test/support/lane-shutdown-child.ts) keeps working unchanged, and so a
  // deployment that fronts this API with a same-origin proxy can turn CORS
  // off entirely by setting CORS_ALLOWED_ORIGINS= rather than by editing code.
  if (corsAllowedOrigins.length === 0) return;

  app.enableCors({
    // The allowlist itself, not `true`. See env.ts's comment: reflecting any
    // Origin would hand this API -- and the plaintext key the page carries --
    // to any site the operator's browser happens to visit.
    origin: [...corsAllowedOrigins],
    // `X-Api-Key` is the whole reason a preflight happens at all: it is not a
    // CORS-safelisted request header, so omitting it here would let the
    // preflight succeed and then have the browser block the real request for
    // sending a header the response never allowed.
    allowedHeaders: ["X-Api-Key", "Content-Type"],
    methods: ["GET", "POST"],
    // No credentials: this API authenticates by header, never by cookie, so
    // there is nothing for the browser to attach and nothing to allow.
    credentials: false,
  });
}
