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
// __OMIND_BIN__, __OMIND_ARGS__ and __OMI_DIR__ are substituted by omind at
// install. OMIND_ARGS is empty for a launcher and ["-m", "omind"] when OMIND is
// a Windows python.exe (#380); Bun's $ spreads an array into separate args.

const OMIND = "__OMIND_BIN__";
const OMIND_ARGS = "__OMIND_ARGS__".split(" ").filter(Boolean);
const OMI_DIR = "__OMI_DIR__";

// OpenCode names the omi MCP server `omi_<tool>` (also `omi__`). verify.py
// records a consult only for the Claude spelling `mcp__omi__<tool>`.
function claudeOmiTool(tool) {
  const match = String(tool || "").match(/^(?:omi__|omi_|mcp_omi_|mcp__omi_|mcp__omi__)(.+)$/);
  return match ? "mcp__omi__" + match[1] : "";
}

export const OmiGuard = async ({ $ }) => {
  const pending = new Map();
  return {
    "tool.execute.before": async (input, output) => {
      try {
        const tool = (input && input.tool) || "";
        const args = (output && output.args) || {};
        const command = typeof args.command === "string" ? args.command : "";
        const session = (input && input.sessionID) || "";
        const callID = (input && input.callID) || "";
        if (callID && claudeOmiTool(tool)) {
          pending.set(callID, { tool: tool, args: args, session: session });
        }
        const payload = JSON.stringify({
          tool: tool,
          command: command,
          session: session,
          tool_input: args,
          is_omi_consult: Boolean(claudeOmiTool(tool)),
        });
        const res = await $`printf '%s' ${payload} | ${OMIND} ${OMIND_ARGS} guard adapter --harness opencode --omi-dir ${OMI_DIR}`
          .quiet()
          .nothrow();
        const verdict = JSON.parse((res.stdout || "").toString().trim() || "{}");
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
    },
    "tool.execute.after": async (input, output) => {
      try {
        const callID = (input && input.callID) || "";
        const remembered = callID ? pending.get(callID) : undefined;
        if (callID) pending.delete(callID);
        const tool = (remembered && remembered.tool) || (input && input.tool) || "";
        const claude = claudeOmiTool(tool);
        if (!claude) return;
        const args = (remembered && remembered.args) || {};
        const session = (remembered && remembered.session) || (input && input.sessionID) || "";
        const body = output && output.output != null ? output.output : "";
        const payload = JSON.stringify({
          session_id: session,
          tool_name: claude,
          tool_input: args,
          tool_response: body,
        });
        await $`printf '%s' ${payload} | ${OMIND} ${OMIND_ARGS} guard verify --omi-dir ${OMI_DIR}`
          .quiet()
          .nothrow();
      } catch (e) {
        // A verify failure must not hide a tool result the model already earned.
      }
    },
  };
};
