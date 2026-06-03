// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Aaron K. Clark
"use strict";

const state = {
  notes: [],
  tags: [],
  query: "",
  activeTag: null,
  current: null, // filename
  mode: "empty", // empty | view | edit | raw | new
};

const $ = (sel) => document.querySelector(sel);
const listEl = $("#note-list");
const tagBarEl = $("#tag-bar");
const contentEl = $("#content");
const countEl = $("#note-count");
const searchEl = $("#search");
const toastEl = $("#toast");

// ---- API ------------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ---- Helpers --------------------------------------------------------------

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.remove("show"), 2600);
}

const stem = (filename) => filename.replace(/\.md$/i, "");

function noteByName(name) {
  const lower = name.toLowerCase();
  return state.notes.find(
    (n) => stem(n.filename).toLowerCase() === lower || n.title.toLowerCase() === lower,
  );
}

// Turn [[wikilinks]] and #tags into markup, then render markdown.
function renderMarkdown(md) {
  let src = md.replace(/\[\[([^\]]+)\]\]/g, (_, name) => {
    const target = name.trim();
    const exists = !!noteByName(target);
    const cls = exists ? "wikilink" : "wikilink missing";
    return `<a class="${cls}" data-note="${escapeHtml(target)}">${escapeHtml(target)}</a>`;
  });
  // Inline #tags (not Markdown headings: require a leading space or open paren).
  src = src.replace(
    /(^|[\s(])#([A-Za-z0-9_][A-Za-z0-9_/-]*)/g,
    (_, pre, tag) => `${pre}<span class="hash-tag" data-tag="${escapeHtml(tag)}">#${escapeHtml(tag)}</span>`,
  );
  return marked.parse(src, { gfm: true, breaks: false });
}

// ---- Sidebar --------------------------------------------------------------

function filteredNotes() {
  const q = state.query.trim().toLowerCase();
  return state.notes.filter((n) => {
    if (state.activeTag && !n.tags.includes(state.activeTag)) return false;
    if (!q) return true;
    return (
      n.title.toLowerCase().includes(q) ||
      n.summary.toLowerCase().includes(q) ||
      n.tags.some((t) => t.toLowerCase().includes(q))
    );
  });
}

function renderTagBar() {
  tagBarEl.innerHTML = "";
  state.tags.forEach((tag) => {
    const chip = document.createElement("span");
    chip.className = "tag-chip" + (state.activeTag === tag ? " active" : "");
    chip.textContent = "#" + tag;
    chip.dataset.tag = tag;
    chip.addEventListener("click", () => {
      state.activeTag = state.activeTag === tag ? null : tag;
      renderSidebar();
    });
    tagBarEl.appendChild(chip);
  });
}

function renderSidebar() {
  renderTagBar();
  const notes = filteredNotes();
  listEl.innerHTML = "";
  if (notes.length === 0) {
    listEl.innerHTML = `<li class="px-2 py-6 text-center font-mono text-[10px] uppercase tracking-widest text-ink-faint">no cards match</li>`;
  }
  notes.forEach((n, i) => {
    const li = document.createElement("li");
    li.className = "index-card" + (n.filename === state.current ? " active" : "");
    li.style.animationDelay = `${Math.min(i, 12) * 28}ms`;
    li.dataset.name = n.filename;
    li.innerHTML = `
      <div class="ic-title">${escapeHtml(n.title)}</div>
      <div class="ic-meta">${escapeHtml(n.created || "undated")}${
        n.tags.length ? " · " + n.tags.slice(0, 3).map((t) => "#" + escapeHtml(t)).join(" ") : ""
      }</div>
      ${n.summary ? `<div class="ic-snippet">${escapeHtml(n.summary)}</div>` : ""}`;
    li.addEventListener("click", () => openNote(n.filename));
    listEl.appendChild(li);
  });
  const total = state.notes.length;
  const shown = notes.length;
  countEl.textContent = shown === total ? `${total} card${total === 1 ? "" : "s"}` : `${shown}/${total}`;
}

// ---- Main pane ------------------------------------------------------------

function renderEmpty() {
  state.mode = "empty";
  state.current = null;
  renderSidebar();
  contentEl.innerHTML = `
    <div class="empty">
      <div class="card-glyph"></div>
      <div class="empty-title">The catalog is open.</div>
      <p class="font-mono text-[11px] uppercase tracking-[0.2em]">
        Select a card &nbsp;·&nbsp; or press <span class="text-stamp">+ New Card</span>
      </p>
    </div>`;
}

async function openNote(name) {
  try {
    const data = await api("GET", `/api/notes/${encodeURIComponent(name)}`);
    state.current = name;
    state.mode = "view";
    renderSidebar();
    renderView(data);
  } catch (e) {
    toast(e.message);
  }
}

function metaRow(fields) {
  const parts = [];
  if (fields.created) parts.push(`<span class="date-stamp">${escapeHtml(fields.created)}</span>`);
  if (fields.related_to)
    parts.push(`<span><span class="label">rel</span> ${escapeHtml(fields.related_to)}</span>`);
  if (fields.tags && fields.tags.length)
    parts.push(
      `<span><span class="label">tags</span> ${fields.tags
        .map((t) => `<span class="hash-tag" data-tag="${escapeHtml(t)}">#${escapeHtml(t)}</span>`)
        .join(" ")}</span>`,
    );
  return parts.length ? `<div class="meta-row">${parts.join("")}</div>` : "";
}

function renderView(data) {
  const f = data.fields;
  contentEl.innerHTML = `
    <article class="sheet">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="sheet-eyebrow">Memory&nbsp;·&nbsp;${escapeHtml(data.filename)}</div>
          <h2 class="sheet-title">${escapeHtml(f.title || stem(data.filename))}</h2>
        </div>
        <div class="seg shrink-0">
          <button data-act="edit" class="active">Edit</button>
          <button data-act="raw">Raw</button>
        </div>
      </div>
      ${metaRow(f)}
      <div class="prose-omi mt-5">${renderMarkdown(data.raw)}</div>
      <div class="mt-8 flex justify-end gap-2 border-t border-rule pt-4">
        <button class="btn btn-danger" data-act="delete">Delete card</button>
      </div>
    </article>`;
  contentEl.querySelector('[data-act="edit"]').onclick = () => openEdit(data);
  contentEl.querySelector('[data-act="raw"]').onclick = () => openRaw(data);
  contentEl.querySelector('[data-act="delete"]').onclick = () => deleteNote(data.filename);
  wireInlineLinks();
}

function wireInlineLinks() {
  contentEl.querySelectorAll(".wikilink").forEach((a) => {
    a.addEventListener("click", () => {
      const target = noteByName(a.dataset.note);
      if (target) openNote(target.filename);
      else toast(`No card titled "${a.dataset.note}" yet`);
    });
  });
  contentEl.querySelectorAll(".hash-tag").forEach((s) => {
    s.addEventListener("click", () => {
      state.activeTag = s.dataset.tag;
      searchEl.value = "";
      state.query = "";
      renderSidebar();
      toast(`Filtered to #${s.dataset.tag}`);
    });
  });
}

// ---- Structured form ------------------------------------------------------

const linesToList = (text) =>
  text.split("\n").map((s) => s.trim()).filter(Boolean);

function parseActionLines(text) {
  return linesToList(text).map((line) => {
    const m = line.match(/^\[([ xX])\]\s*(.*)$/);
    if (m) return { text: m[2].trim(), done: m[1].toLowerCase() === "x" };
    return { text: line, done: false };
  });
}

function actionsToText(items) {
  return (items || []).map((it) => `[${it.done ? "x" : " "}] ${it.text}`).join("\n");
}

function formMarkup(f, { isNew }) {
  return `
    <article class="sheet">
      <div class="sheet-eyebrow">${isNew ? "New memory card" : "Editing · " + escapeHtml(state.current)}</div>
      <input id="f-title" class="title-input mt-1" placeholder="Card title…" value="${escapeHtml(f.title || "")}" />

      <div class="mt-5 grid grid-cols-2 gap-4">
        <div>
          <label class="field-label">Created</label>
          <input id="f-created" type="date" class="field-input mono" value="${escapeHtml(f.created || "")}" />
        </div>
        <div>
          <label class="field-label">Related to</label>
          <input id="f-related" class="field-input" value="${escapeHtml(f.related_to || "")}" placeholder="optional" />
        </div>
      </div>

      <div class="mt-4">
        <label class="field-label">Tags</label>
        <input id="f-tags" class="field-input mono" value="${escapeHtml((f.tags || []).join(" "))}" placeholder="omi memory project" />
        <div class="field-hint">space or comma separated · leading # optional</div>
      </div>

      <div class="mt-4">
        <label class="field-label">Summary</label>
        <textarea id="f-summary" class="field-textarea" rows="2" placeholder="One or two sentences.">${escapeHtml(f.summary || "")}</textarea>
      </div>

      <div class="mt-4">
        <label class="field-label">Details</label>
        <textarea id="f-details" class="field-textarea" rows="7">${escapeHtml(f.details || "")}</textarea>
      </div>

      <div class="mt-4 grid grid-cols-2 gap-4">
        <div>
          <label class="field-label">Connections</label>
          <textarea id="f-connections" class="field-textarea mono" rows="4" placeholder="Related Concept">${escapeHtml((f.connections || []).join("\n"))}</textarea>
          <div class="field-hint">one [[wikilink]] target per line</div>
        </div>
        <div>
          <label class="field-label">Action items</label>
          <textarea id="f-actions" class="field-textarea mono" rows="4" placeholder="[ ] do the thing">${escapeHtml(actionsToText(f.action_items))}</textarea>
          <div class="field-hint">one per line · prefix [x] if done</div>
        </div>
      </div>

      <div class="mt-4">
        <label class="field-label">References</label>
        <textarea id="f-references" class="field-textarea mono" rows="3" placeholder="Source: …">${escapeHtml((f.references || []).join("\n"))}</textarea>
        <div class="field-hint">one per line</div>
      </div>

      <div class="mt-7 flex items-center justify-between border-t border-rule pt-4">
        <div>${isNew ? "" : '<button class="btn btn-danger" data-act="delete">Delete</button>'}</div>
        <div class="flex gap-2">
          <button class="btn btn-ghost" data-act="cancel">Cancel</button>
          <button class="btn btn-primary" data-act="save">${isNew ? "File card" : "Save"}</button>
        </div>
      </div>
    </article>`;
}

function gatherFields() {
  return {
    title: $("#f-title").value.trim(),
    created: $("#f-created").value.trim(),
    related_to: $("#f-related").value.trim(),
    tags: $("#f-tags").value.split(/[\s,]+/).map((s) => s.replace(/^#/, "").trim()).filter(Boolean),
    summary: $("#f-summary").value.trim(),
    details: $("#f-details").value.trim(),
    connections: linesToList($("#f-connections").value),
    action_items: parseActionLines($("#f-actions").value),
    references: linesToList($("#f-references").value),
  };
}

function openEdit(data) {
  state.mode = "edit";
  contentEl.innerHTML = formMarkup(data.fields, { isNew: false });
  wireForm({ isNew: false, original: data });
}

function openNew() {
  state.mode = "new";
  state.current = null;
  renderSidebar();
  const today = new Date().toISOString().slice(0, 10);
  contentEl.innerHTML = formMarkup({ created: today, tags: ["omi", "memory"] }, { isNew: true });
  wireForm({ isNew: true });
  $("#f-title").focus();
}

function wireForm({ isNew, original }) {
  contentEl.querySelector('[data-act="cancel"]').onclick = () =>
    isNew ? (state.current ? openNote(state.current) : renderEmpty()) : openNote(state.current);
  contentEl.querySelector('[data-act="save"]').onclick = async () => {
    const fields = gatherFields();
    if (!fields.title) return toast("A card needs a title.");
    try {
      if (isNew) {
        const { filename } = await api("POST", "/api/notes", fields);
        await refresh();
        openNote(filename);
        toast("Card filed.");
      } else {
        const { filename } = await api("PUT", `/api/notes/${encodeURIComponent(state.current)}`, fields);
        await refresh();
        openNote(filename);
        toast("Saved.");
      }
    } catch (e) {
      toast(e.message);
    }
  };
  const del = contentEl.querySelector('[data-act="delete"]');
  if (del) del.onclick = () => deleteNote(state.current);
}

// ---- Raw editor -----------------------------------------------------------

function openRaw(data) {
  state.mode = "raw";
  contentEl.innerHTML = `
    <article class="sheet">
      <div class="flex items-center justify-between">
        <div class="sheet-eyebrow">Raw markdown · ${escapeHtml(data.filename)}</div>
        <div class="seg">
          <button data-act="form">Form</button>
          <button class="active">Raw</button>
        </div>
      </div>
      <textarea id="f-raw" class="field-textarea mono mt-4" rows="22" spellcheck="false">${escapeHtml(data.raw)}</textarea>
      <div class="mt-5 flex justify-end gap-2 border-t border-rule pt-4">
        <button class="btn btn-ghost" data-act="cancel">Cancel</button>
        <button class="btn btn-primary" data-act="save">Save raw</button>
      </div>
    </article>`;
  contentEl.querySelector('[data-act="form"]').onclick = () => openEdit(data);
  contentEl.querySelector('[data-act="cancel"]').onclick = () => openNote(data.filename);
  contentEl.querySelector('[data-act="save"]').onclick = async () => {
    try {
      await api("PUT", `/api/notes/${encodeURIComponent(data.filename)}/raw`, {
        content: $("#f-raw").value,
      });
      await refresh();
      openNote(data.filename);
      toast("Saved.");
    } catch (e) {
      toast(e.message);
    }
  };
}

// ---- Delete ---------------------------------------------------------------

async function deleteNote(name) {
  if (!confirm(`Delete card "${stem(name)}"? This removes the file.`)) return;
  try {
    await api("DELETE", `/api/notes/${encodeURIComponent(name)}`);
    await refresh();
    renderEmpty();
    toast("Card removed.");
  } catch (e) {
    toast(e.message);
  }
}

// ---- Boot -----------------------------------------------------------------

async function refresh() {
  const [notes, tags] = await Promise.all([
    api("GET", "/api/notes"),
    api("GET", "/api/tags"),
  ]);
  state.notes = notes;
  state.tags = tags;
  renderSidebar();
}

searchEl.addEventListener("input", () => {
  state.query = searchEl.value;
  renderSidebar();
});
$("#new-btn").addEventListener("click", openNew);

(async function boot() {
  try {
    await refresh();
    renderEmpty();
  } catch (e) {
    contentEl.innerHTML = `<div class="empty"><div class="empty-title">Couldn't load the catalog.</div><p class="font-mono text-xs">${escapeHtml(
      e.message,
    )}</p></div>`;
  }
})();
