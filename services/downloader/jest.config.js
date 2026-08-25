/** @type {import('jest').Config} */
module.exports = {
  rootDir: ".",
  testEnvironment: "node",
  transform: {
    "^.+\\.(t|j)s$": ["ts-jest", { tsconfig: "tsconfig.json" }],
  },
  testRegex: ".*\\.(spec|e2e-spec)\\.ts$",
  // Integration suites need the internet and a live Douyin, not just docker.
  // They run under jest.integration.config.js via `pnpm test:integration`.
  testPathIgnorePatterns: ["/node_modules/", "\\.integration\\.spec\\.ts$"],
  moduleFileExtensions: ["js", "json", "ts"],
  roots: ["<rootDir>/src", "<rootDir>/test"],
};
