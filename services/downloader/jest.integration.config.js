/**
 * Integration suites that need the docker-compose stack AND the public
 * internet (a live Douyin, a cookie that is not banned). Kept out of
 * `pnpm test` so an ordinary red run always means a real regression.
 *
 *   pnpm test:integration
 */
/** @type {import('jest').Config} */
module.exports = {
  rootDir: ".",
  testEnvironment: "node",
  transform: {
    "^.+\\.(t|j)s$": ["ts-jest", { tsconfig: "tsconfig.json" }],
  },
  testRegex: ".*\\.integration\\.spec\\.ts$",
  moduleFileExtensions: ["js", "json", "ts"],
  roots: ["<rootDir>/test"],
};
