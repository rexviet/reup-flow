/**
 * Isolated jest config for SPIKE 2 (GATE). Deliberately NOT merged into the
 * service's jest.config.js: this suite needs a live docker compose stack
 * (see run.sh) and would hang/fail under a plain `pnpm test`. Invoke only
 * via `run.sh` or `npm run test:spike-cookie-swap`.
 */
/** @type {import('jest').Config} */
module.exports = {
  rootDir: __dirname,
  testEnvironment: "node",
  transform: {
    "^.+\\.ts$": ["ts-jest", { tsconfig: "<rootDir>/tsconfig.spike.json" }],
  },
  testMatch: ["<rootDir>/cookie-swap.spike-test.ts"],
  testTimeout: 60_000,
};
