// omi-guard.dsh.js — DSH (DeepSeek Harness) guard plugin installed by omind.
//
// Enforces omind's harness-agnostic guard on DSH. Before every tool runs, it
// asks `omind guard adapter --harness deepseek` and denies on ANY block verdict
// (hard-rule deny OR the per-turn consult-gate), returning a PreToolDecision
// that stops the tool before dispatch.
//
// Unlike the OpenCode plugin, the consult-gate IS enforced here — DSH's MCP tool
// naming follows the standard `mcp__<server>__<tool>` convention, which omind has
// verified, so the gate can safely demand an OMI consult before non-memory tool
// execution. The absolute hard blocks (destructive commands, forge attacks,
// capability side-effects, git-rule violations) have no such dependency and are
// always enforced.
//
// Also hooks into:
// - agent/session-start → call `omind hook SessionStart` to prime the session
//   with identity, workflow, and standing operator-instruction notes, injected
//   via agent.inject() so the model sees them on its first turn.
// - agent/pre-step (turn boundary) → call `omind guard preflight` which resets
//   the per-turn consult gate AND injects the one relevant memory for the turn's
//   task (the DSH analogue of Claude's UserPromptSubmit handler).
// - tools/post-execute → call `omind hook PostToolUse` to append a journal
//   bullet recording the tool + target + outcome.
// - agent/disposed → call `omind hook Stop` to close the journal entry for the
//   session and check the loop guard / unwritten-work detector.
//
// Fail-OPEN on any adapter/parse error — a broken guard must never wedge the
// agent. Navigation/listing tools (which surface no note content) are allowed
// without consulting so an agent can't clear the gate by listing notes.
//
// __OMIND_BIN__, __OMI_DIR__, __OMI_VAULT__, __OMI_FOLDER__ are substituted by
// omind at install time (see DeepseekProvisioner.install_guard in agents.py).

import { spawn } from "node:child_process";

const OMIND = "__OMIND_BIN__";
const OMI_DIR = "__OMI_DIR__";
const OMI_VAULT = "__OMI_VAULT__";
const OMI_FOLDER = "__OMI_FOLDER__";

// Substrings that prefix an OMI consult tool across MCP naming conventions.
const OMI_TOOL_PREFIXES = ["mcp__omi__", "mcp_omi__"];

// Navigation/listing tools surface NO note content — allow without consulting
// (an agent could otherwise clear the gate by listing notes every turn).
// Keep in sync with omi-guard.sh and omi-guard-hermes.sh.
const NAVIGATE_TOOLS = [
    "mcp__omi__list-notes",
    "mcp__omi__list-tags",
    "mcp__omi__graph-neighbors",
    "mcp__omi__graph-path",
    "mcp__omi__graph-orphans",
    "mcp__omi__graph-dangling",
    "mcp__omi__graph-stats",
    "mcp__omi__graph-export",
    "mcp__omi__backlinks",
    "mcp_omi__list-notes",
    "mcp_omi__list-tags",
    "mcp_omi__graph-neighbors",
    "mcp_omi__graph-path",
    "mcp_omi__graph-orphans",
    "mcp_omi__graph-dangling",
    "mcp_omi__graph-stats",
    "mcp_omi__graph-export",
    "mcp_omi__backlinks",
];
const NAVIGATE_PREFIXES = ["mcp__omi__list-", "mcp__omi__graph-", "mcp__omi__backlinks"];

// Vault writes are acts, not consults — let them through to the guard check.
// (They follow a read/search in an honest flow, so the turn's consult already
// exists.) Keep in sync with omi-guard.sh.
const OMI_WRITE_TOOLS = new Set([
    "mcp__omi__create-note",
    "mcp__omi__edit-note",
    "mcp__omi__delete-note",
    "mcp__omi__restore-note",
    "mcp_omi__create-note",
    "mcp_omi__edit-note",
    "mcp_omi__delete-note",
    "mcp_omi__restore-note",
]);

// Reserved basenames whose read MUST NOT clear the gate — they are "relevant
// to everything", making them the gate-dodge. Keep in sync with
// omind.paths.NON_CONSULT_FILENAMES.
const SCAFFOLD_BASENAMES = new Set(["index.md", "MEMORY.md", "Memory Template.md"]);

// Per-session state: track the turn number (for gate reset on change) and the
// current turn's prompt (for guard context).
const sessionState = new Map();

export const name = "omind-guard";
export const inject = ["tools"];

export function apply(ctx) {
    // --- Per-turn gate reset + proactive memory (DSH analogue of UserPromptSubmit) ---
    ctx.on("agent/pre-step", async (payload, next) => {
        try {
            const agent = payload && payload.agent;
            const turn = (payload && typeof payload.turn === "number") ? payload.turn : 0;
            if (agent) {
                const sid = String(agent.id || "");
                const state = sessionState.get(sid) || { lastTurn: -1, prompt: "", primed: false };
                if (turn > state.lastTurn) {
                    state.lastTurn = turn;
                    const messages = (payload && payload.messages) || [];
                    state.prompt = extractPrompt(messages);
                    sessionState.set(sid, state);
                    // `omind guard preflight` resets the per-turn consult gate AND
                    // recalls one compact relevant memory for the turn's task.
                    const { stdout } = await spawnOmind(
                        ["guard", "preflight", "--omi-dir", OMI_DIR],
                        { session_id: sid, prompt: state.prompt },
                    );
                    const context = parsePriming(stdout);
                    if (context && typeof agent.inject === "function") {
                        injectContext(agent, context);
                    }
                }
            }
        } catch {
            /* fail open — never wedge a pre-step on guard plumbing */
        }
        return next();
    });

    // --- Session start: prime with OMI standing-operator instructions ---
    ctx.on("agent/session-start", async (payload) => {
        try {
            const agent = payload && payload.agent;
            const sid = String(agent?.id || "");
            if (!sid) return;
            // `omind hook SessionStart` emits identity, workflow, hard rules, and
            // recent-memory titles — the always-on context, not turn-specific
            // retrieval (which preflight handles at agent/pre-step).
            const { stdout } = await spawnOmind(
                ["hook", "SessionStart", "--vault", OMI_VAULT, "--folder", OMI_FOLDER],
                { session_id: sid, cwd: process.cwd() },
            );
            const context = parsePriming(stdout);
            if (context && agent && typeof agent.inject === "function") {
                injectContext(agent, context);
            }
        } catch {
            /* fail open */
        }
    });

    // --- Pre-execute: enforce the omind guard (hard blocks + consult-gate) ---
    ctx.on("tools/pre-execute", async (exec, next) => {
        try {
            const verdict = await checkTool(exec);
            if (verdict && !verdict.allow) {
                const reason = (verdict.reason || `Blocked by ${verdict.rule_id || "omind"}`).trim();
                return { kind: "deny", reason };
            }
        } catch {
            /* fail open — a broken guard must never wedge the agent */
        }
        return next();
    });

    // --- Post-execute: journal the tool outcome ---
    ctx.on("tools/post-execute", async (exec, result, next) => {
        try {
            await journalTool(exec, result);
        } catch {
            /* fail open */
        }
        return next();
    });

    // --- Session end: record the stop ---
    ctx.on("agent/disposed", async (payload) => {
        try {
            const agent = payload && payload.agent;
            const sid = String(agent?.id || "");
            if (sid) {
                await spawnOmind(
                    ["hook", "Stop", "--vault", OMI_VAULT, "--folder", OMI_FOLDER],
                    { session_id: sid },
                );
            }
        } catch {
            /* fail open */
        }
    });
}

// --- core helpers ---

/** Decide whether to allow a tool call. Returns the parsed verdict (or null to
 * allow without calling the adapter). */
async function checkTool(exec) {
    const tool = (exec && exec.name) || "";

    // Navigation/listing tools: allow without consulting (anti-dodge).
    if (isNavigateTool(tool)) return null;

    // Scaffolding reads under the OMI folder: allow without consulting.
    if (isScaffoldRead(tool, exec)) return null;

    // Build the guard event from the DSH exec and ask the core.
    const event = buildGuardEvent(exec);
    const { stdout } = await spawnOmind(
        ["guard", "adapter", "--harness", "deepseek", "--omi-dir", OMI_DIR],
        event,
    );
    try {
        return JSON.parse(stdout.trim() || "{}");
    } catch {
        return null; // fail open on parse errors
    }
}

/** Journal a tool execution by piping it to `omind hook PostToolUse`. */
async function journalTool(exec, result) {
    const event = buildGuardEvent(exec);
    event.hook_event_name = "PostToolUse";
    event.tool_response = normalizeResponse(result);
    await spawnOminor(
        ["hook", "PostToolUse", "--vault", OMI_VAULT, "--folder", OMI_FOLDER],
        event,
    );
}

/** Build the guard-adapter event JSON that `normalize_action` understands. */
function buildGuardEvent(exec) {
    const tool = (exec && exec.name) || "";
    const args = (exec && exec.arguments) || {};
    const sid = exec && exec.agent ? String(exec.agent.id || "") : "";
    const state = sessionState.get(sid);
    const prompt = (state && state.prompt) || "";

    let command = "";
    let file_path = "";

    if (typeof args === "object" && args !== null) {
        if (typeof args.command === "string" && args.command) command = args.command;
        else if (Array.isArray(args.args)) {
            const joined = args.args.map(String).join(" ");
            if (joined) command = joined;
        }
        if (typeof args.path === "string" && args.path) file_path = args.path;
        else if (typeof args.file_path === "string" && args.file_path) file_path = args.file_path;
    } else if (typeof args === "string") {
        command = args;
    }

    // Detect whether this clears the consult gate.
    // OMI consult tools (mcp__omi__*) clear the gate via the adapter's
    // normalize_action; Read-under-OMI-dir needs explicit flagging.
    let isOmiConsult = OMI_TOOL_PREFIXES.some((p) => tool.startsWith(p));
    if (!isOmiConsult && file_path && file_path.includes(OMI_DIR) && !isScaffoldPath(file_path)) {
        isOmiConsult = true;
    }

    const ev = {
        tool_name: tool,
        tool_input: {},
        session_id: sid,
        prompt: prompt,
        is_omi_consult: isOmiConsult,
    };
    if (command) ev.tool_input.command = command;
    if (file_path) ev.tool_input.file_path = file_path;

    if (isOmiConsult) {
        ev.consult_target = file_path || command || "";
        ev.consult_kind = file_path ? "read" : "search";
    }

    return ev;
}

function isNavigateTool(tool) {
    if (!tool) return false;
    if (NAVIGATE_TOOLS.includes(tool)) return true;
    return NAVIGATE_PREFIXES.some((p) => tool.startsWith(p));
}

function isScaffoldRead(tool, exec) {
    // Only DSH's own file-read tools qualify — not arbitrary MCP tools.
    if (OMI_TOOL_PREFIXES.some((p) => tool.startsWith(p))) return false;
    const args = (exec && exec.arguments) || {};
    const fp =
        (typeof args === "object" && args !== null) ?
            (args.path || args.file_path || "") : "";
    if (!fp) return false;
    return isScaffoldPath(fp) && fp.includes(OMI_DIR);
}

function isScaffoldPath(filePath) {
    const base = filePath.split(/[/\\]/).pop() || "";
    return SCAFFOLD_BASENAMES.has(base);
}

function extractPrompt(messages) {
    // DSH messages carry role + content (string or content block array).
    for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        if (!msg || msg.role !== "user") continue;
        const content = msg.content;
        if (typeof content === "string") return content;
        if (Array.isArray(content)) {
            const textBlock = content.find((c) => c && c.type === "text" && typeof c.text === "string");
            if (textBlock) return textBlock.text;
        }
    }
    return "";
}

/** Parse the priming/preflight JSON output and return the additionalContext string. */
function parsePriming(stdout) {
    const trimmed = stdout.trim();
    if (!trimmed) return "";
    try {
        const parsed = JSON.parse(trimmed);
        // omind hook SessionStart / guard preflight emit:
        //   {"hookSpecificOutput": {"hookEventName": ..., "additionalContext": "..."}}
        // or guard preflight may emit a bare {"context": "..."} for Hermes compat.
        const ctx = parsed.hookSpecificOutput || parsed;
        const additional = ctx && typeof ctx.additionalContext === "string" ? ctx.additionalContext
            : typeof parsed.additionalContext === "string" ? parsed.additionalContext
            : typeof parsed.context === "string" ? parsed.context
            : "";
        if (additional) return additional;
    } catch {
        /* not JSON — fall through to raw-text */
    }
    return trimmed;
}

/** Normalize a DSH tool result for the omind hook's PostToolUse journal recorder. */
function normalizeResponse(result) {
    if (!result) return { is_error: false };
    const isError = result.isError === true;
    return {
        is_error: isError,
        ...(isError ? { error: { message: "tool execution failed" } } : {}),
    };
}

/** Inject a context string as a user message via the agent's inject queue. */
function injectContext(agent, text) {
    try {
        agent.inject({
            role: "user",
            content: [{ type: "text", text: text }],
            source: { kind: "plugin", plugin: "omind-guard" },
        });
    } catch {
        /* if inject() rejects, the agent will simply miss the priming this turn */
    }
}

/** Spawn the omind binary, pipe a JSON event to stdin, collect stdout/stderr. */
function spawnOminor(args, stdinObj) {
    return new Promise((resolve, reject) => {
        const child = spawn(OMIND, args, {
            stdio: ["pipe", "pipe", "pipe"],
            timeout: 15_000,
            env: { ...process.env, OMINDEX_PROFILE: "economy" },
        });
        let stdout = "";
        let stderr = "";
        const input = JSON.stringify(stdinObj || {});
        child.stdin.write(input, (err) => {
            if (err) child.stdin.end();
        });
        child.stdin.end();
        child.stdout.on("data", (d) => (stdout += d));
        child.stderr.on("data", (d) => (stderr += d));
        const timer = setTimeout(() => {
            child.kill("SIGKILL");
            reject(new Error(`omind ${args.join(" ")} timed out`));
        }, 15_000);
        child.on("close", (code) => {
            clearTimeout(timer);
            resolve({ stdout, stderr, code });
        });
        child.on("error", (err) => {
            clearTimeout(timer);
            reject(err);
        });
    });
}
