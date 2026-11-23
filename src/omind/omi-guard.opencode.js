// omi-guard.opencode.js — OpenCode plugin installed by omind.
//
// Enforces omind's harness-agnostic guard on OpenCode. Before every tool runs,
// it asks `omind guard adapter --harness opencode` and THROWS on a hard-rule
// deny (the destructive/forge deny set), which aborts the tool. The consult
// gate is enforced too: its deny is `repo-work-read-git-rules`, not `omi-gate*`
// (#513). After an OMI MCP tool succeeds, the plugin calls `omind guard verify`
// with the Claude `mcp__omi__` tool name so a full read-note writes the
// session ack. Fail-open on any adapter/parse error — a broken guard must
// never break OpenCode.
//
// ONE FILE, TWO CLIENTS: V1 calls the named `OmiGuard` export (all versions)
// or the default export's `server()` (>= 1.18.29); V2 reads `id` + `setup()`
// and registers hooks through `ctx.tool.hook`. One copy in the plugin dir
// is enough: V1 discovers that directory natively, and V2 discovers it too
// (verified on 2.4.0, alongside its newer `plugins/`). Writing the same plugin
// to both would trigger V2's "Duplicate plugin ID" rejection.
// V2 removed the Bun shell helper the V1 template used, so both versions
// share a portable `node:child_process` spawn (works in Node and Bun).
//
// __OMIND_BIN__, __OMIND_ARGS__ and __OMI_DIR__ are substituted by omind at
// install. OMIND_ARGS is empty for a launcher and ["-m", "omind"] when OMIND is
// a Windows python.exe (#380); spawn takes them as separate argv entries.

import { spawn } from "node:child_process";

const OMIND = "__OMIND_BIN__";
const OMIND_ARGS = "__OMIND_ARGS__".split(" ").filter(Boolean);
const OMI_DIR = "__OMI_DIR__";

// OpenCode names the omi MCP server `omi_<tool>` (V2 also `omi__`, and Code
// Mode fires the inner tool as `omi_list-tags`). verify.py records a consult
// only for the Claude spelling `mcp__omi__<tool>`.
function claudeOmiTool(tool) {
  const match = String(tool || "").match(/^(?:omi__|omi_|mcp_omi_|mcp__omi_|mcp__omi__)(.+)$/);
  return match ? "mcp__omi__" + match[1] : "";
}

// Spawn omind with the JSON payload on stdin, resolve with its stdout. Never
// throws: a missing binary, a spawn error or an early close is a fail-open.
function spawnOmind(args, payload) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(OMIND, [...OMIND_ARGS, ...args], { stdio: ["pipe", "pipe", "ignore"] });
    } catch (_) {
      resolve("");
      return;
    }
    let out = "";
    let done = false;
    const finish = (text) => {
      if (done) return;
      done = true;
      resolve(text);
    };
    child.stdout?.on("data", (chunk) => {
      out += chunk.toString();
    });
    child.on("error", () => finish(""));
    child.on("close", () => finish(out));
    try {
      child.stdin?.end(String(payload));
    } catch (_) {
      child.kill?.();
      finish("");
    }
  });
}

// V2 hands one mutable event per hook; V1 hands (input, output). The `server()`
// shim below normalizes V1 into the V2 shape so the logic is shared verbatim.
// V2 fields observed on 2.4.0: tool, id (call id), sessionID, input, result.
const pending = new Map();

async function before(event, cwd) {
  try {
    const tool = String((event && event.tool) || "");
    const args = (event && event.input) || {};
    const command = typeof args.command === "string" ? args.command : "";
    const session = String((event && event.sessionID) || "");
    const callID = String((event && (event.id || event.callID)) || "");
    if (callID && claudeOmiTool(tool)) {
      pending.set(callID, { tool: tool, args: args, session: session });
    }
    const payload = JSON.stringify({
      tool: tool,
      command: command,
      session: session,
      // Repo-scoped note rules need the agent's shell cwd; the OpenCode server
      // runs from home, so the spawned adapter's own cwd is not enough (#517).
      cwd: cwd || String(process.cwd()),
      tool_input: args,
      is_omi_consult: Boolean(claudeOmiTool(tool)),
    });
    const stdout = await spawnOmind(
      ["guard", "adapter", "--harness", "opencode", "--omi-dir", OMI_DIR],
      payload
    );
    const verdict = JSON.parse((stdout || "").trim() || "{}");
    // omi-gate* is the mid-turn re-arm. repo-work-read-git-rules is the
    // git-rules consult gate (#513): throw it so the agent reads the note.
    // The after-hook records that read.
    if (
      verdict.allow === false &&
      verdict.rule_id &&
      !String(verdict.rule_id).startsWith("omi-gate")
    ) {
      throw new Error("OMI guard blocked this action: " + (verdict.reason || verdict.rule_id));
    }
  } catch (e) {
    // Propagate a deliberate block; fail open on everything else.
    if (e && typeof e.message === "string" && e.message.startsWith("OMI guard blocked")) {
      throw e;
    }
  }
}

async function after(event) {
  try {
    const callID = String((event && (event.id || event.callID)) || "");
    const remembered = callID ? pending.get(callID) : undefined;
    if (callID) pending.delete(callID);
    const tool = (remembered && remembered.tool) || String((event && event.tool) || "");
    const claude = claudeOmiTool(tool);
    if (!claude) return;
    const args = (remembered && remembered.args) || (event && event.input) || {};
    const session =
      (remembered && remembered.session) || String((event && event.sessionID) || "");
    const result = (event && event.result) || {};
    const body = result && result.output != null ? result.output : result;
    const payload = JSON.stringify({
      session_id: session,
      tool_name: claude,
      tool_input: args,
      tool_response: body,
    });
    await spawnOmind(["guard", "verify", "--omi-dir", OMI_DIR], payload);
  } catch (e) {
    // A verify failure must not hide a tool result the model already earned.
  }
}

// V1 hook shim: turn (input, output) into the V2 event shape.
function v1Event(input, output) {
  return {
    tool: (input && input.tool) || "",
    sessionID: (input && input.sessionID) || "",
    id: (input && input.callID) || "",
    input: (output && output.args) || {},
    result: output || {},
  };
}

// V1 >= 1.18.29 calls the default export's `server()`; older V1 only knows a
// named plugin-function export. Both share this flag so whichever entrypoint
// the client uses registers exactly one hook set. V2 ignores named exports.
let v1Registered = false;
function v1Hooks(directory) {
  if (v1Registered) return {};
  v1Registered = true;
  return {
    "tool.execute.before": async (input, output) =>
      before(v1Event(input, output), directory || ""),
    "tool.execute.after": async (input, output) => after(v1Event(input, output)),
  };
}

export const OmiGuard = async ({ directory } = {}) => v1Hooks(directory);

export default {
  id: "omind.omi-guard",
  // OpenCode V2: ctx.tool.hook replaces V1's returned hook map. The location
  // directory is the shell's cwd for sessions served by this plugin instance.
  async setup(ctx) {
    const cwd = (ctx && ctx.location && ctx.location.directory) || "";
    await ctx.tool.hook("execute.before", (event) => before(event, cwd));
    await ctx.tool.hook("execute.after", after);
  },
  // OpenCode V1 (>= 1.18.29): default-export object entrypoint. Its context
  // carries `directory` — the same cwd the adapter needs for note rules.
  async server({ directory } = {}) {
    return v1Hooks(directory);
  },
};
