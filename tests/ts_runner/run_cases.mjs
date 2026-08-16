/**
 * Runs the shared write-guard case table against the TypeScript guard and
 * prints one JSON verdict per case.
 *
 * This exists so the pre-write enforcement that actually runs inside Pi is
 * tested, not just its Python twin. It imports the guard's pure functions
 * directly — no Pi process, no model, no network, no cost.
 *
 *   node --experimental-strip-types run_cases.mjs <cases.json> <worktree-root>
 */

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const guardPath = resolve(here, "..", "..", "orchestrator", "pi_ext", "write_guard.ts");
const { checkPath, checkToolCall } = await import(pathToFileURL(guardPath).href);

const [, , casesFile, root] = process.argv;
const table = JSON.parse(readFileSync(casesFile, "utf-8"));
const writeSet = table.write_set;

const results = [];
for (const testCase of table.cases) {
  const raw = testCase.absolute ? testCase.path : testCase.path;
  const verdict = checkPath(raw, root, writeSet, root);
  results.push({ name: testCase.name, allowed: verdict.allowed, denial: verdict.denial });
}
for (const testCase of table.tool_cases) {
  const verdict = checkToolCall(testCase.tool, testCase.input, root, writeSet, false, root);
  results.push({ name: testCase.name, allowed: verdict.allowed, denial: verdict.denial });
}
process.stdout.write(JSON.stringify(results));
void join;
