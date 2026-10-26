/**
 * Suites that run the real renderer (Chromium, ffmpeg), a real object store and a local
 * Temporal server. Kept out of `pnpm test` so an ordinary red run always means a
 * regression and not a missing service.
 *
 *   pnpm test:integration
 */
/** @type {import('jest').Config} */
module.exports = {
  rootDir: ".",
  testEnvironment: "node",
  transform: {
    "^.+\\.(t|j)sx?$": ["ts-jest", { tsconfig: "tests/tsconfig.json" }],
  },
  testRegex: ".*\\.integration-spec\\.ts$",
  moduleFileExtensions: ["js", "json", "ts", "tsx"],
  roots: ["<rootDir>/tests"],
  testTimeout: 300000,
  // One real render at a time: Chromium and ffmpeg are what this service limits.
  maxWorkers: 1,
};
