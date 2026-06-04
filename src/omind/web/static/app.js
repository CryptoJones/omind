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
  lang: "en",
};

// ---- i18n -----------------------------------------------------------------

const LANGS = [
  { code: "en", name: "English" },
  { code: "es", name: "Español" },
  { code: "fr", name: "Français" },
  { code: "ar", name: "العربية" },
  { code: "ru", name: "Русский" },
  { code: "zh", name: "中文" },
];
const RTL_LANGS = new Set(["ar"]);

const I18N = {
  en: {
    tagline: "memory", search: "search…", theme: "Theme", language: "Language",
    new: "+ New", noMatches: "no matches", notes: "notes",
    emptyTitle: "No memory selected", emptyHint: "Pick a note · or press {new}",
    edit: "Edit", raw: "Raw", form: "Form", delete: "Delete", cancel: "Cancel",
    save: "Save", create: "Create", saveRaw: "Save source", newNote: "New note",
    editing: "Editing", rawMarkdown: "Source", titlePlaceholder: "Title…",
    created: "Created", relatedTo: "Related to", rel: "rel", tags: "Tags",
    tagsShort: "tags", summary: "Summary", details: "Details",
    connections: "Connections", actionItems: "Action items", references: "References",
    optional: "optional", summaryPlaceholder: "One or two sentences.",
    connectionsPlaceholder: "Related Concept", actionsPlaceholder: "[ ] do the thing",
    referencesPlaceholder: "Source: …",
    tagsHint: "space or comma separated · leading # optional",
    connectionsHint: "one [[wikilink]] target per line",
    actionsHint: "one per line · prefix [x] if done", refsHint: "one per line",
    needTitle: "A note needs a title.", createdToast: "Created.",
    savedToast: "Saved.", deletedToast: "Deleted.", loadError: "Couldn't load notes.",
    noNoteYet: 'No note "{name}" yet', filteredTo: "Filtered to #{tag}",
    confirmDelete: 'Delete "{name}"? This removes the file.',
  },
  es: {
    tagline: "memoria", search: "buscar…", theme: "Tema", language: "Idioma",
    new: "+ Nuevo", noMatches: "sin resultados", notes: "notas",
    emptyTitle: "Ninguna nota seleccionada", emptyHint: "Elige una nota · o pulsa {new}",
    edit: "Editar", raw: "Fuente", form: "Formulario", delete: "Eliminar",
    cancel: "Cancelar", save: "Guardar", create: "Crear", saveRaw: "Guardar fuente",
    newNote: "Nueva nota", editing: "Editando", rawMarkdown: "Fuente",
    titlePlaceholder: "Título…", created: "Creada", relatedTo: "Relacionada con",
    rel: "rel", tags: "Etiquetas", tagsShort: "etiq", summary: "Resumen",
    details: "Detalles", connections: "Conexiones", actionItems: "Tareas",
    references: "Referencias", optional: "opcional",
    summaryPlaceholder: "Una o dos frases.", connectionsPlaceholder: "Concepto relacionado",
    actionsPlaceholder: "[ ] hacer la tarea", referencesPlaceholder: "Fuente: …",
    tagsHint: "separadas por espacio o coma · # inicial opcional",
    connectionsHint: "un destino [[wikilink]] por línea",
    actionsHint: "una por línea · prefijo [x] si está hecha", refsHint: "una por línea",
    needTitle: "La nota necesita un título.", createdToast: "Creada.",
    savedToast: "Guardada.", deletedToast: "Eliminada.",
    loadError: "No se pudieron cargar las notas.",
    noNoteYet: "Aún no existe la nota «{name}»", filteredTo: "Filtrado por #{tag}",
    confirmDelete: "¿Eliminar «{name}»? Esto borra el archivo.",
  },
  fr: {
    tagline: "mémoire", search: "rechercher…", theme: "Thème", language: "Langue",
    new: "+ Nouveau", noMatches: "aucun résultat", notes: "notes",
    emptyTitle: "Aucune note sélectionnée",
    emptyHint: "Choisissez une note · ou appuyez sur {new}",
    edit: "Modifier", raw: "Source", form: "Formulaire", delete: "Supprimer",
    cancel: "Annuler", save: "Enregistrer", create: "Créer",
    saveRaw: "Enregistrer la source", newNote: "Nouvelle note", editing: "Modification",
    rawMarkdown: "Source", titlePlaceholder: "Titre…", created: "Créée",
    relatedTo: "Liée à", rel: "liée", tags: "Étiquettes", tagsShort: "étiq",
    summary: "Résumé", details: "Détails", connections: "Connexions",
    actionItems: "Actions", references: "Références", optional: "facultatif",
    summaryPlaceholder: "Une ou deux phrases.", connectionsPlaceholder: "Concept lié",
    actionsPlaceholder: "[ ] faire la tâche", referencesPlaceholder: "Source : …",
    tagsHint: "séparées par espace ou virgule · # initial facultatif",
    connectionsHint: "une cible [[wikilink]] par ligne",
    actionsHint: "une par ligne · préfixe [x] si terminée", refsHint: "une par ligne",
    needTitle: "Une note doit avoir un titre.", createdToast: "Créée.",
    savedToast: "Enregistrée.", deletedToast: "Supprimée.",
    loadError: "Impossible de charger les notes.",
    noNoteYet: "Aucune note « {name} » pour l'instant", filteredTo: "Filtré sur #{tag}",
    confirmDelete: "Supprimer « {name} » ? Cela efface le fichier.",
  },
  ar: {
    tagline: "ذاكرة", search: "بحث…", theme: "السمة", language: "اللغة",
    new: "+ جديد", noMatches: "لا نتائج", notes: "ملاحظات",
    emptyTitle: "لم يتم تحديد أي مذكرة", emptyHint: "اختر مذكرة · أو اضغط {new}",
    edit: "تحرير", raw: "المصدر", form: "نموذج", delete: "حذف", cancel: "إلغاء",
    save: "حفظ", create: "إنشاء", saveRaw: "حفظ المصدر", newNote: "مذكرة جديدة",
    editing: "تحرير", rawMarkdown: "المصدر", titlePlaceholder: "العنوان…",
    created: "تاريخ الإنشاء", relatedTo: "مرتبطة بـ", rel: "صلة", tags: "وسوم",
    tagsShort: "وسوم", summary: "ملخص", details: "تفاصيل", connections: "روابط",
    actionItems: "مهام", references: "مراجع", optional: "اختياري",
    summaryPlaceholder: "جملة أو جملتان.", connectionsPlaceholder: "مفهوم ذو صلة",
    actionsPlaceholder: "[ ] أنجز المهمة", referencesPlaceholder: "المصدر: …",
    tagsHint: "مفصولة بمسافة أو فاصلة · الرمز # اختياري",
    connectionsHint: "هدف [[wikilink]] واحد لكل سطر",
    actionsHint: "واحدة لكل سطر · ابدأ بـ [x] إذا اكتملت", refsHint: "واحد لكل سطر",
    needTitle: "المذكرة تحتاج إلى عنوان.", createdToast: "تم الإنشاء.",
    savedToast: "تم الحفظ.", deletedToast: "تم الحذف.",
    loadError: "تعذّر تحميل الملاحظات.", noNoteYet: "لا توجد مذكرة «{name}» بعد",
    filteredTo: "تمت التصفية حسب #{tag}",
    confirmDelete: "حذف «{name}»؟ سيؤدي ذلك إلى حذف الملف.",
  },
  ru: {
    tagline: "память", search: "поиск…", theme: "Тема", language: "Язык",
    new: "+ Создать", noMatches: "нет совпадений", notes: "заметок",
    emptyTitle: "Заметка не выбрана", emptyHint: "Выберите заметку · или нажмите {new}",
    edit: "Правка", raw: "Исходный", form: "Форма", delete: "Удалить",
    cancel: "Отмена", save: "Сохранить", create: "Создать",
    saveRaw: "Сохранить исходник", newNote: "Новая заметка", editing: "Редактирование",
    rawMarkdown: "Исходный код", titlePlaceholder: "Заголовок…", created: "Создана",
    relatedTo: "Связана с", rel: "связь", tags: "Теги", tagsShort: "теги",
    summary: "Краткое описание", details: "Подробности", connections: "Связи",
    actionItems: "Задачи", references: "Источники", optional: "необязательно",
    summaryPlaceholder: "Одно-два предложения.", connectionsPlaceholder: "Связанное понятие",
    actionsPlaceholder: "[ ] сделать дело", referencesPlaceholder: "Источник: …",
    tagsHint: "через пробел или запятую · # в начале необязателен",
    connectionsHint: "одна цель [[wikilink]] на строку",
    actionsHint: "по одной на строку · префикс [x], если выполнено",
    refsHint: "по одному на строку", needTitle: "Заметке нужен заголовок.",
    createdToast: "Создано.", savedToast: "Сохранено.", deletedToast: "Удалено.",
    loadError: "Не удалось загрузить заметки.", noNoteYet: "Заметки «{name}» пока нет",
    filteredTo: "Фильтр по #{tag}", confirmDelete: "Удалить «{name}»? Файл будет удалён.",
  },
  zh: {
    tagline: "记忆", search: "搜索…", theme: "主题", language: "语言",
    new: "+ 新建", noMatches: "无匹配项", notes: "条笔记",
    emptyTitle: "未选择任何笔记", emptyHint: "选择一条笔记 · 或点击 {new}",
    edit: "编辑", raw: "源码", form: "表单", delete: "删除", cancel: "取消",
    save: "保存", create: "创建", saveRaw: "保存源码", newNote: "新建笔记",
    editing: "正在编辑", rawMarkdown: "源码", titlePlaceholder: "标题…",
    created: "创建日期", relatedTo: "相关", rel: "相关", tags: "标签",
    tagsShort: "标签", summary: "摘要", details: "详情", connections: "关联",
    actionItems: "待办事项", references: "参考", optional: "可选",
    summaryPlaceholder: "一两句话。", connectionsPlaceholder: "相关概念",
    actionsPlaceholder: "[ ] 要做的事", referencesPlaceholder: "来源：…",
    tagsHint: "用空格或逗号分隔 · 开头的 # 可选",
    connectionsHint: "每行一个 [[wikilink]] 目标",
    actionsHint: "每行一条 · 已完成的加前缀 [x]", refsHint: "每行一条",
    needTitle: "笔记需要一个标题。", createdToast: "已创建。", savedToast: "已保存。",
    deletedToast: "已删除。", loadError: "无法加载笔记。", noNoteYet: "尚无笔记“{name}”",
    filteredTo: "已按 #{tag} 筛选", confirmDelete: "删除“{name}”？这将移除该文件。",
  },
};

function t(key, vars) {
  const table = I18N[state.lang] || I18N.en;
  let s = table[key];
  if (s === undefined) s = I18N.en[key];
  if (s === undefined) s = key;
  if (vars) {
    for (const k in vars) s = s.split("{" + k + "}").join(vars[k]);
  }
  return s;
}

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
    /(^|[\s(])#([\p{L}\p{N}_][\p{L}\p{N}_/-]*)/gu,
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
    listEl.innerHTML = `<li class="px-2 py-6 text-center font-mono text-[10px] uppercase tracking-widest text-ink-faint">${escapeHtml(t("noMatches"))}</li>`;
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
  countEl.textContent = shown === total ? `${total} ${t("notes")}` : `${shown}/${total}`;
}

// ---- Main pane ------------------------------------------------------------

function renderEmpty() {
  state.mode = "empty";
  state.current = null;
  renderSidebar();
  contentEl.innerHTML = `
    <div class="empty">
      <div class="card-glyph"></div>
      <div class="empty-title">${escapeHtml(t("emptyTitle"))}</div>
      <p class="font-mono text-[11px] uppercase tracking-[0.18em]">
        ${t("emptyHint", { new: `<span class="text-stamp">${escapeHtml(t("new"))}</span>` })}
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
    parts.push(`<span><span class="label">${escapeHtml(t("rel"))}</span> ${escapeHtml(fields.related_to)}</span>`);
  if (fields.tags && fields.tags.length)
    parts.push(
      `<span><span class="label">${escapeHtml(t("tagsShort"))}</span> ${fields.tags
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
          <div class="sheet-eyebrow">${escapeHtml(data.filename)}</div>
          <h2 class="sheet-title">${escapeHtml(f.title || stem(data.filename))}</h2>
        </div>
        <div class="seg shrink-0">
          <button data-act="edit" class="active">${escapeHtml(t("edit"))}</button>
          <button data-act="raw">${escapeHtml(t("raw"))}</button>
        </div>
      </div>
      ${metaRow(f)}
      <div class="prose-omi mt-5">${renderMarkdown(data.raw)}</div>
      <div class="mt-8 flex justify-end gap-2 border-t border-rule pt-4">
        <button class="btn btn-danger" data-act="delete">${escapeHtml(t("delete"))}</button>
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
      else toast(t("noNoteYet", { name: a.dataset.note }));
    });
  });
  contentEl.querySelectorAll(".hash-tag").forEach((s) => {
    s.addEventListener("click", () => {
      state.activeTag = s.dataset.tag;
      searchEl.value = "";
      state.query = "";
      renderSidebar();
      toast(t("filteredTo", { tag: s.dataset.tag }));
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
  const eyebrow = isNew ? t("newNote") : `${t("editing")} · ${escapeHtml(state.current)}`;
  return `
    <article class="sheet">
      <div class="sheet-eyebrow">${eyebrow}</div>
      <input id="f-title" class="title-input mt-1" placeholder="${escapeHtml(t("titlePlaceholder"))}" value="${escapeHtml(f.title || "")}" />

      <div class="mt-5 grid grid-cols-2 gap-4">
        <div>
          <label class="field-label">${escapeHtml(t("created"))}</label>
          <input id="f-created" type="date" class="field-input mono" value="${escapeHtml(f.created || "")}" />
        </div>
        <div>
          <label class="field-label">${escapeHtml(t("relatedTo"))}</label>
          <input id="f-related" class="field-input" value="${escapeHtml(f.related_to || "")}" placeholder="${escapeHtml(t("optional"))}" />
        </div>
      </div>

      <div class="mt-4">
        <label class="field-label">${escapeHtml(t("tags"))}</label>
        <input id="f-tags" class="field-input mono" value="${escapeHtml((f.tags || []).join(" "))}" placeholder="omi memory project" />
        <div class="field-hint">${escapeHtml(t("tagsHint"))}</div>
      </div>

      <div class="mt-4">
        <label class="field-label">${escapeHtml(t("summary"))}</label>
        <textarea id="f-summary" class="field-textarea" rows="2" placeholder="${escapeHtml(t("summaryPlaceholder"))}">${escapeHtml(f.summary || "")}</textarea>
      </div>

      <div class="mt-4">
        <label class="field-label">${escapeHtml(t("details"))}</label>
        <textarea id="f-details" class="field-textarea" rows="7">${escapeHtml(f.details || "")}</textarea>
      </div>

      <div class="mt-4 grid grid-cols-2 gap-4">
        <div>
          <label class="field-label">${escapeHtml(t("connections"))}</label>
          <textarea id="f-connections" class="field-textarea mono" rows="4" placeholder="${escapeHtml(t("connectionsPlaceholder"))}">${escapeHtml((f.connections || []).join("\n"))}</textarea>
          <div class="field-hint">${escapeHtml(t("connectionsHint"))}</div>
        </div>
        <div>
          <label class="field-label">${escapeHtml(t("actionItems"))}</label>
          <textarea id="f-actions" class="field-textarea mono" rows="4" placeholder="${escapeHtml(t("actionsPlaceholder"))}">${escapeHtml(actionsToText(f.action_items))}</textarea>
          <div class="field-hint">${escapeHtml(t("actionsHint"))}</div>
        </div>
      </div>

      <div class="mt-4">
        <label class="field-label">${escapeHtml(t("references"))}</label>
        <textarea id="f-references" class="field-textarea mono" rows="3" placeholder="${escapeHtml(t("referencesPlaceholder"))}">${escapeHtml((f.references || []).join("\n"))}</textarea>
        <div class="field-hint">${escapeHtml(t("refsHint"))}</div>
      </div>

      <div class="mt-7 flex items-center justify-between border-t border-rule pt-4">
        <div>${isNew ? "" : `<button class="btn btn-danger" data-act="delete">${escapeHtml(t("delete"))}</button>`}</div>
        <div class="flex gap-2">
          <button class="btn btn-ghost" data-act="cancel">${escapeHtml(t("cancel"))}</button>
          <button class="btn btn-primary" data-act="save">${isNew ? escapeHtml(t("create")) : escapeHtml(t("save"))}</button>
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
    if (!fields.title) return toast(t("needTitle"));
    try {
      if (isNew) {
        const { filename } = await api("POST", "/api/notes", fields);
        await refresh();
        openNote(filename);
        toast(t("createdToast"));
      } else {
        const { filename } = await api("PUT", `/api/notes/${encodeURIComponent(state.current)}`, fields);
        await refresh();
        openNote(filename);
        toast(t("savedToast"));
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
        <div class="sheet-eyebrow">${escapeHtml(t("rawMarkdown"))} · ${escapeHtml(data.filename)}</div>
        <div class="seg">
          <button data-act="form">${escapeHtml(t("form"))}</button>
          <button class="active">${escapeHtml(t("raw"))}</button>
        </div>
      </div>
      <textarea id="f-raw" class="field-textarea mono mt-4" rows="22" spellcheck="false">${escapeHtml(data.raw)}</textarea>
      <div class="mt-5 flex justify-end gap-2 border-t border-rule pt-4">
        <button class="btn btn-ghost" data-act="cancel">${escapeHtml(t("cancel"))}</button>
        <button class="btn btn-primary" data-act="save">${escapeHtml(t("saveRaw"))}</button>
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
      toast(t("savedToast"));
    } catch (e) {
      toast(e.message);
    }
  };
}

// ---- Delete ---------------------------------------------------------------

async function deleteNote(name) {
  if (!confirm(t("confirmDelete", { name: stem(name) }))) return;
  try {
    await api("DELETE", `/api/notes/${encodeURIComponent(name)}`);
    await refresh();
    renderEmpty();
    toast(t("deletedToast"));
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

// ---- Theme switcher -------------------------------------------------------

const THEMES = ["midnight", "carbon", "dusk", "paper", "mint"];

function applyTheme(name) {
  const theme = THEMES.includes(name) ? name : "midnight";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("omind-theme", theme);
  } catch (_) {}
  document.querySelectorAll(".swatch").forEach((s) => {
    s.classList.toggle("active", s.dataset.theme === theme);
  });
}

function initTheme() {
  let saved = "midnight";
  try {
    saved = localStorage.getItem("omind-theme") || saved;
  } catch (_) {}
  applyTheme(saved);
  document.querySelectorAll(".swatch").forEach((s) => {
    s.addEventListener("click", () => applyTheme(s.dataset.theme));
  });
}
initTheme();

// ---- Language switcher ----------------------------------------------------

// Static chrome that lives outside the re-rendered panes.
function applyStaticI18n() {
  const tagline = $("#tagline");
  if (tagline) tagline.textContent = t("tagline");
  searchEl.placeholder = t("search");
  $("#new-btn").textContent = t("new");
  const tp = $("#theme-picker");
  if (tp) tp.title = t("theme");
  const ls = $("#lang-select");
  if (ls) ls.title = t("language");
}

// Re-render the active pane in the new language without dropping unsaved input
// in the editors (edit/new/raw are intentionally left untouched).
function rerenderForLang() {
  if (state.mode === "view" && state.current) openNote(state.current);
  else if (state.mode === "empty") renderEmpty();
  else renderSidebar();
}

function applyLang(code) {
  const lang = LANGS.some((l) => l.code === code) ? code : "en";
  state.lang = lang;
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL_LANGS.has(lang) ? "rtl" : "ltr";
  try {
    localStorage.setItem("omind-lang", lang);
  } catch (_) {}
  const sel = $("#lang-select");
  if (sel) sel.value = lang;
  applyStaticI18n();
  rerenderForLang();
}

function initI18n() {
  let saved = null;
  try {
    saved = localStorage.getItem("omind-lang");
  } catch (_) {}
  if (!saved) {
    const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
    saved = LANGS.some((l) => l.code === nav) ? nav : "en";
  }
  const sel = $("#lang-select");
  if (sel) {
    sel.innerHTML = LANGS.map(
      (l) => `<option value="${l.code}">${escapeHtml(l.name)}</option>`,
    ).join("");
    sel.addEventListener("change", () => applyLang(sel.value));
  }
  applyLang(saved);
}
initI18n();

(async function boot() {
  try {
    await refresh();
    renderEmpty();
  } catch (e) {
    contentEl.innerHTML = `<div class="empty"><div class="empty-title">${escapeHtml(
      t("loadError"),
    )}</div><p class="font-mono text-xs">${escapeHtml(e.message)}</p></div>`;
  }
})();
