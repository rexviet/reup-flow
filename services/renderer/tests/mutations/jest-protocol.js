#!/usr/bin/env node
// Runs jest and reports in the shape scripts/lib/mutation-protocol.sh reads from pytest:
// one `FAILED <file>::<test title>` line per failing test and a `N passed, M failed`
// summary. The protocol decides "caught" from those lines, so a mutation counts only when
// the tests that exist to catch it are among the failures.
//
//   node tests/mutations/jest-protocol.js [-rfE] <test path>... [jest args]
//
// A path that names an integration spec runs under the integration config.
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const args = process.argv.slice(2).filter((a) => a !== "-rfE");
const integration = args.some((a) => a.includes("integration-spec"));
const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "jest-protocol-")), "result.json");

const run = spawnSync(
  "pnpm",
  [
    "exec",
    "jest",
    ...(integration ? ["-c", "jest.integration.config.js"] : []),
    ...args,
    "--forceExit",
    "--json",
    `--outputFile=${out}`,
  ],
  { stdio: ["ignore", "ignore", "inherit"], env: process.env },
);

let report;
try {
  report = JSON.parse(fs.readFileSync(out, "utf8"));
} catch {
  // jest died before it could report (a compile error, say): no test evidence.
  console.log("jest produced no report");
  process.exit(run.status || 1);
}

for (const suite of report.testResults) {
  const file = path.relative(process.cwd(), suite.name);
  if (suite.status === "failed" && suite.assertionResults.length === 0) {
    console.log(`ERROR ${file}: the suite did not run: ${suite.message.split("\n")[0]}`);
  }
  for (const test of suite.assertionResults) {
    if (test.status === "failed") console.log(`FAILED ${file}::${test.title}`);
  }
}
console.log(`${report.numPassedTests} passed, ${report.numFailedTests} failed`);
process.exit(report.numFailedTests > 0 || !report.success ? 1 : 0);
