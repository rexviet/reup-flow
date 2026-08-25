import { type DynamicModule, Global, Module } from "@nestjs/common";
import { type Env, parseEnv } from "./env";

/**
 * Injection token for the parsed, validated `Env` object. A plain string
 * token (not a class) because `Env` is a `zod`-inferred type, not a class
 * with runtime identity to key an injection on.
 */
export const ENV = "ENV";

/**
 * @Global() so every feature module (jobs, videos, cookie -- groups B/C)
 * can `@Inject(ENV)` without importing this module explicitly everywhere.
 *
 * The default provider calls `parseEnv()` inside the factory, not at module
 * class evaluation time, so construction happens exactly once, at Nest's
 * own DI-container bootstrap -- which is also where the existing fail-fast
 * contract in main.ts expects configuration errors to surface (see
 * `EnvValidationError` in env.ts and the bootstrap catch-all in main.ts).
 */
@Global()
@Module({
  providers: [{ provide: ENV, useFactory: (): Env => parseEnv() }],
  exports: [ENV],
})
export class ConfigModule {
  /**
   * Test-only override: injects a fixed `Env` instead of parsing
   * `process.env`. Lets a guard/module test hold every downstream env
   * dependency constant without needing a full, valid process environment.
   */
  static withEnv(env: Env): DynamicModule {
    return {
      module: ConfigModule,
      global: true,
      providers: [{ provide: ENV, useValue: env }],
      exports: [ENV],
    };
  }
}
