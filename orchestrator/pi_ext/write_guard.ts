/**
 * Bounded-write guard — a Pi extension that BLOCKS out-of-scope tool calls
 * before they touch the filesystem.
 *
 * Verified against the installed Pi 0.84.2 extension API:
 *   - `pi.on("tool_call", handler)` fires before the tool executes and can
 *     block by returning `{ block: true, reason }` (docs/extensions.md).
 *   - Built-in tools are exactly: read, ls, find, grep, write, edit, bash
 *     (dist/core/tools/*.js). `write` and `edit` both take `path`.
 *
 * `--tools` selects tool NAMES, and `cwd` is not a sandbox. This hook is what
 * actually confines writes to the task's worktree and declared write_set.
 *
 * Policy is deny-by-default and mirrors orchestrator/write_guard.py exactly.
 * The two implementations are held in agreement by a shared case table
 * (tests/data/write_guard_cases.json).
 *
 * Configuration arrives as non-secret environment variables:
 *   ORCHESTRATOR_WRITE_ROOT      absolute path of the task worktree
 *   ORCHESTRATOR_WRITE_SET       JSON array of repo-relative owned paths
 *   ORCHESTRATOR_ALLOW_SHELL     "1" to permit bash (default: refuse)
 */

import { existsSync, lstatSync, realpathSync } from "node:fs";
import { isAbsolute, join, normalize, resolve, sep } from "node:path";

export const READ_ONLY_TOOLS = new Set(["read", "ls", "find", "grep"]);
export const MUTATION_TOOLS = new Set(["write", "edit"]);
export const SHELL_TOOLS = new Set(["bash"]);

export interface Verdict {
  allowed: boolean;
  reason: string;
  denial: string | null;
  resolved: string;
}

const allow = (reason: string, resolved = ""): Verdict => ({
  allowed: true,
  reason,
  denial: null,
  resolved,
});

const deny = (reason: string, denial: string, resolved = ""): Verdict => ({
  allowed: false,
  reason,
  denial,
  resolved,
});

/**
 * Resolve symlinks as far as the path exists, keeping the missing tail literal.
 * A file about to be created does not exist yet, but a symlinked PARENT still
 * has to be followed, or `worktree/link-to-etc/passwd` would slip through.
 */
function resolveExistingAncestor(path: string): string {
  const remainder: string[] = [];
  let current = normalize(path);
  for (;;) {
    let exists = existsSync(current);
    if (!exists) {
      try {
        lstatSync(current);
        exists = true; // a broken symlink still counts as present
      } catch {
        exists = false;
      }
    }
    if (exists) {
      return remainder.length === 0
        ? realpathSync(current)
        : join(realpathSync(current), ...remainder.slice().reverse());
    }
    const parent = normalize(join(current, ".."));
    if (parent === current) {
      return normalize(path);
    }
    remainder.push(current.slice(parent.endsWith(sep) ? parent.length : parent.length + 1));
    current = parent;
  }
}

function covered(relative: string, writeSet: string[]): boolean {
  for (const declared of writeSet) {
    const entry = declared.trim().replace(/\/+$/, "");
    if (!entry) continue;
    if (relative === entry) return true;
    if (relative.startsWith(entry + "/")) return true;
  }
  return false;
}

function toPosix(value: string): string {
  return value.split(sep).join("/");
}

export function checkPath(
  raw: string,
  root: string,
  writeSet: string[],
  cwd?: string,
): Verdict {
  if (!raw || !raw.trim()) return deny("empty path", "EMPTY_PATH");

  const realRoot = existsSync(root) ? realpathSync(root) : normalize(root);
  const base = cwd ?? realRoot;
  const absolute = isAbsolute(raw) ? normalize(raw) : resolve(base, raw);
  const resolved = resolveExistingAncestor(absolute);

  const prefix = realRoot.endsWith(sep) ? realRoot : realRoot + sep;
  const inside = resolved === realRoot || resolved.startsWith(prefix);
  if (!inside) {
    const lexical = normalize(absolute);
    const insideLexically = lexical === realRoot || lexical.startsWith(prefix);
    return deny(
      `'${raw}' resolves to ${resolved} which is outside the task worktree ${realRoot}`,
      insideLexically ? "SYMLINK_ESCAPE" : "OUTSIDE_WORKTREE",
      resolved,
    );
  }

  const relative = toPosix(resolved.slice(realRoot.length).replace(/^[/\\]/, ""));
  if (relative === "" || relative === ".") {
    return deny("the worktree root itself is not writable", "NOT_IN_WRITE_SET", resolved);
  }
  if (!covered(relative, writeSet)) {
    return deny(
      `'${relative}' is not covered by the declared write_set ${JSON.stringify(writeSet)}`,
      "NOT_IN_WRITE_SET",
      resolved,
    );
  }
  return allow(`'${relative}' is inside the worktree and owned by this task`, resolved);
}

/** Deny-by-default: an unrecognised tool is blocked, never waved through. */
export function checkToolCall(
  toolName: string,
  input: Record<string, unknown>,
  root: string,
  writeSet: string[],
  allowShell = false,
  cwd?: string,
): Verdict {
  if (READ_ONLY_TOOLS.has(toolName)) return allow(`${toolName} is read-only`);
  if (SHELL_TOOLS.has(toolName)) {
    return allowShell
      ? allow("shell explicitly permitted for this stage")
      : deny("the implementer stage runs without shell access", "TOOL_NOT_ALLOWED");
  }
  if (!MUTATION_TOOLS.has(toolName)) {
    return deny(
      `tool '${toolName}' is not on the bounded implementer allowlist`,
      "TOOL_NOT_ALLOWED",
    );
  }
  const raw = (input?.path ?? input?.file_path) as string | undefined;
  if (!raw) {
    return deny(`${toolName} call carries no path argument`, "NO_PATH_ARGUMENT");
  }
  return checkPath(String(raw), root, writeSet, cwd);
}

function loadPolicy() {
  const root = process.env.ORCHESTRATOR_WRITE_ROOT ?? "";
  const allowShell = process.env.ORCHESTRATOR_ALLOW_SHELL === "1";
  let writeSet: string[] = [];
  try {
    const parsed = JSON.parse(process.env.ORCHESTRATOR_WRITE_SET ?? "[]");
    if (Array.isArray(parsed)) writeSet = parsed.map(String);
  } catch {
    writeSet = [];
  }
  return { root, writeSet, allowShell };
}

export default function (pi: {
  on: (event: string, handler: (event: any, ctx: any) => any) => void;
}) {
  const { root, writeSet, allowShell } = loadPolicy();

  pi.on("tool_call", async (event: any) => {
    // Fail closed: an unconfigured guard blocks every mutation rather than
    // degrading to "no restrictions".
    if (!root) {
      if (READ_ONLY_TOOLS.has(event.toolName)) return undefined;
      return {
        block: true,
        reason:
          "orchestrator write guard is not configured (ORCHESTRATOR_WRITE_ROOT unset); " +
          "refusing every mutation. Return STOP_NEEDS_ORCHESTRATOR.",
      };
    }
    const verdict = checkToolCall(
      event.toolName,
      event.input ?? {},
      root,
      writeSet,
      allowShell,
    );
    if (verdict.allowed) return undefined;
    return {
      block: true,
      reason:
        `BLOCKED BY ORCHESTRATOR WRITE GUARD [${verdict.denial}]: ${verdict.reason}. ` +
        "Do not retry with another path. If this task genuinely needs to write here, " +
        "stop and return STOP_NEEDS_ORCHESTRATOR.",
    };
  });
}
