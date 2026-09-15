// omi-guard.opencode.js — OpenCode plugin installed by omind.
//
// Enforces omind's harness-agnostic guard on OpenCode. Before every tool runs,
// it asks `omind guard adapter --harness opencode` and THROWS on a hard-rule
// deny (the destructive/forge deny set), which aborts the tool. The per-turn
// CONSULT GATE is intentionally NOT enforced here: its turn-boundary + consult
// signals depend on OpenCode's MCP tool naming, which omind hasn't verified
// live, so enforcing it could wedge a session. The absolute hard blocks have no
// such dependency. Fail-open on any adapter/parse error — a broken guard must
// never break OpenCode.
//
// __OMIND_BIN__ and __OMI_DIR__ are substituted by omind at install.

const OMIND = "__OMIND_BIN__";
const OMI_DIR = "__OMI_DIR__";

export const OmiGuard = async ({ $ }) => {
  return {
    "tool.execute.before": async (input, output) => {
      try {
        const tool = (input && input.tool) || "";
        const args = (output && output.args) || {};
        const command = typeof args.command === "string" ? args.command : "";
        const session = (input && input.sessionID) || "";
        const payload = JSON.stringify({
          tool: tool,
          command: command,
          session: session,
          is_omi_consult: false,
        });
        const res = await $`printf '%s' ${payload} | ${OMIND} guard adapter --harness opencode --omi-dir ${OMI_DIR}`
          .quiet()
          .nothrow();
        const verdict = JSON.parse((res.stdout || "").toString().trim() || "{}");
        // Enforce only real hard-rule denies — never the consult gate or its
        // mid-turn re-arm (the omi-gate* family), whose consult signals aren't
        // verified live on OpenCode.
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
  };
};
