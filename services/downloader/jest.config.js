/** @type {import('jest').Config} */
module.exports = {
  rootDir: ".",
  testEnvironment: "node",
  transform: {
    "^.+\\.(t|j)s$": ["ts-jest", { tsconfig: "tsconfig.json" }],
  },
  testRegex: ".*\\.(spec|e2e-spec)\\.ts$",
  moduleFileExtensions: ["js", "json", "ts"],
  roots: ["<rootDir>/src", "<rootDir>/test"],
};
