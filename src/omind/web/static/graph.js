// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Aaron K. Clark
//
// Interactive [[wikilink]] graph view for the omind web UI. Dependency-free:
// a small canvas force-directed layout (no d3 / no graph library), themed from
// the active [data-theme] CSS variables. Click a node to open that note.
(function () {
  "use strict";

  const cssVar = (name, fallback) =>
    (getComputedStyle(document.documentElement).getPropertyValue(name) || fallback).trim();

  // Degree -> color tier (accent = hubs, link = mid, faint = leaf, red = orphan).
  function paletteFor(deg, theme) {
    if (deg === 0) return "#ff5d5d";
    if (deg >= 8) return theme.accent;
    if (deg >= 4) return theme.link;
    if (deg >= 2) return theme.soft;
    return theme.faint;
  }

  async function render(container, opts) {
    opts = opts || {};
    const data = await fetch("api/graph").then((r) => r.json());

    // --- model -------------------------------------------------------------
    const index = new Map();
    const nodes = data.nodes.map((n, i) => {
      index.set(n.id, i);
      return { id: n.id, title: n.title || n.id.replace(/\.md$/i, ""), deg: 0,
               x: Math.cos(i) * 240 + (i % 7) * 13, y: Math.sin(i) * 240 + (i % 5) * 11,
               vx: 0, vy: 0, pinned: false };
    });
    const edges = [];
    for (const [s, d] of data.edges) {
      const a = index.get(s), b = index.get(d);
      if (a === undefined || b === undefined) continue;
      edges.push([a, b]);
      nodes[a].deg++; nodes[b].deg++;
    }
    const neighbors = nodes.map(() => new Set());
    for (const [a, b] of edges) { neighbors[a].add(b); neighbors[b].add(a); }

    // --- canvas + chrome ---------------------------------------------------
    container.innerHTML =
      '<div class="graph-wrap">' +
      '  <div class="graph-bar">' +
      '    <span class="graph-stat">' + nodes.length + " notes · " + edges.length + " links</span>" +
      '    <span class="graph-legend">' +
      '      <i style="background:' + cssVar("--accent", "#7d9bff") + '"></i>hub' +
      '      <i style="background:' + cssVar("--link", "#5fd0bf") + '"></i>linked' +
      '      <i style="background:#ff5d5d"></i>orphan' +
      "    </span>" +
      '    <button type="button" class="graph-reset btn">reset view</button>' +
      "  </div>" +
      '  <canvas class="graph-canvas"></canvas>' +
      "</div>";
    const canvas = container.querySelector(".graph-canvas");
    const ctx = canvas.getContext("2d");

    let theme = readTheme();
    function readTheme() {
      return {
        bg: cssVar("--bg", "#0c0e13"),
        edge: cssVar("--border-strong", "#313847"),
        text: cssVar("--text", "#e7eaf0"),
        accent: cssVar("--accent", "#7d9bff"),
        link: cssVar("--link", "#5fd0bf"),
        soft: cssVar("--text-soft", "#9aa3b2"),
        faint: cssVar("--text-faint", "#626c7d"),
      };
    }

    // --- view transform ----------------------------------------------------
    let scale = 1, ox = 0, oy = 0, W = 0, H = 0, dpr = window.devicePixelRatio || 1;
    function resize() {
      const r = canvas.getBoundingClientRect();
      W = r.width; H = r.height;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function fit() {
      // Percentile bounds so a few flung-out orphans can't blow up the scale.
      const xs = nodes.map((n) => n.x).sort((a, b) => a - b);
      const ys = nodes.map((n) => n.y).sort((a, b) => a - b);
      const q = (arr, p) => arr[Math.max(0, Math.floor((arr.length - 1) * p))];
      const minx = q(xs, 0.02), maxx = q(xs, 0.98);
      const miny = q(ys, 0.02), maxy = q(ys, 0.98);
      const gw = maxx - minx || 1, gh = maxy - miny || 1;
      scale = Math.max(0.05, Math.min(W / (gw + 120), H / (gh + 120), 2.2));
      ox = W / 2 - ((minx + maxx) / 2) * scale;
      oy = H / 2 - ((miny + maxy) / 2) * scale;
    }
    const sx = (x) => x * scale + ox;
    const sy = (y) => y * scale + oy;

    // --- force simulation --------------------------------------------------
    let alpha = 1;
    function step() {
      const REST = 64, KREP = 3000, KSPR = 0.045, GRAV = 0.03, VCAP = 30;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy || 0.01;
          const f = (KREP / d2) * alpha;
          const d = Math.sqrt(d2); dx /= d; dy /= d;
          a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
        }
        a.vx -= a.x * GRAV * alpha; a.vy -= a.y * GRAV * alpha;
      }
      for (const [i, j] of edges) {
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (d - REST) * KSPR * alpha;
        dx /= d; dy /= d;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
      for (const n of nodes) {
        if (n.pinned) { n.vx = n.vy = 0; continue; }
        n.vx *= 0.82; n.vy *= 0.82;
        const sp = Math.hypot(n.vx, n.vy);
        if (sp > VCAP) { n.vx = (n.vx / sp) * VCAP; n.vy = (n.vy / sp) * VCAP; }
        n.x += n.vx; n.y += n.vy;
      }
      alpha *= 0.985;
    }

    function radius(n) { return 3 + Math.min(9, Math.sqrt(n.deg) * 2.1); }

    // --- draw --------------------------------------------------------------
    let hover = -1;
    function draw() {
      ctx.fillStyle = theme.bg;
      ctx.fillRect(0, 0, W, H);
      // edges
      ctx.lineWidth = 1;
      for (const [i, j] of edges) {
        const lit = hover === i || hover === j;
        ctx.strokeStyle = lit ? theme.accent : theme.edge;
        ctx.globalAlpha = hover === -1 ? 0.55 : lit ? 0.9 : 0.12;
        ctx.beginPath();
        ctx.moveTo(sx(nodes[i].x), sy(nodes[i].y));
        ctx.lineTo(sx(nodes[j].x), sy(nodes[j].y));
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      // nodes
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        const dim = hover !== -1 && hover !== i && !neighbors[hover].has(i);
        ctx.globalAlpha = dim ? 0.25 : 1;
        ctx.fillStyle = paletteFor(n.deg, theme);
        ctx.beginPath();
        ctx.arc(sx(n.x), sy(n.y), radius(n) * (i === hover ? 1.5 : 1), 0, 6.2832);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      // labels: hubs always, hovered node emphatically
      ctx.font = '12px ui-monospace, Menlo, monospace';
      ctx.textBaseline = "middle";
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        if (n.deg < 12 && i !== hover) continue;
        if (hover !== -1 && hover !== i && !neighbors[hover].has(i)) continue;
        const label = n.title.length > 30 ? n.title.slice(0, 29) + "…" : n.title;
        const tx = sx(n.x) + radius(n) + 4, ty = sy(n.y);
        ctx.fillStyle = theme.bg; ctx.globalAlpha = 0.7;
        ctx.fillRect(tx - 2, ty - 7, ctx.measureText(label).width + 4, 14);
        ctx.globalAlpha = 1; ctx.fillStyle = i === hover ? theme.accent : theme.text;
        ctx.fillText(label, tx, ty);
      }
    }

    // --- main loop ---------------------------------------------------------
    let raf;
    function loop() {
      if (alpha > 0.02) step();
      draw();
      raf = requestAnimationFrame(loop);
    }

    // --- interaction -------------------------------------------------------
    function worldAt(clientX, clientY) {
      const r = canvas.getBoundingClientRect();
      return { x: (clientX - r.left - ox) / scale, y: (clientY - r.top - oy) / scale };
    }
    function pick(clientX, clientY) {
      const w = worldAt(clientX, clientY);
      let best = -1, bestD = 14 / scale;
      for (let i = 0; i < nodes.length; i++) {
        const d = Math.hypot(nodes[i].x - w.x, nodes[i].y - w.y);
        if (d < bestD + radius(nodes[i]) / scale) { bestD = d; best = i; }
      }
      return best;
    }
    let drag = null, moved = false;
    canvas.addEventListener("mousemove", (e) => {
      if (drag && drag.node >= 0) {
        const w = worldAt(e.clientX, e.clientY);
        nodes[drag.node].x = w.x; nodes[drag.node].y = w.y;
        nodes[drag.node].pinned = true; moved = true; alpha = Math.max(alpha, 0.3);
      } else if (drag) {
        ox += e.clientX - drag.px; oy += e.clientY - drag.py;
        drag.px = e.clientX; drag.py = e.clientY; moved = true;
      } else {
        const h = pick(e.clientX, e.clientY);
        if (h !== hover) { hover = h; canvas.style.cursor = h >= 0 ? "pointer" : "grab"; }
      }
    });
    canvas.addEventListener("mousedown", (e) => {
      const node = pick(e.clientX, e.clientY);
      drag = { node, px: e.clientX, py: e.clientY }; moved = false;
    });
    window.addEventListener("mouseup", (e) => {
      if (drag && !moved && drag.node >= 0 && opts.openNote) opts.openNote(nodes[drag.node].id);
      if (drag && drag.node >= 0) nodes[drag.node].pinned = false;
      drag = null;
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const k = Math.exp(-e.deltaY * 0.0012);
      ox = mx - (mx - ox) * k; oy = my - (my - oy) * k; scale *= k;
    }, { passive: false });
    container.querySelector(".graph-reset").addEventListener("click", () => { fit(); alpha = 0.6; });

    const ro = new ResizeObserver(() => { resize(); });
    ro.observe(canvas);
    const onTheme = () => { theme = readTheme(); };
    const mo = new MutationObserver(onTheme);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    // Settle the layout to near-rest BEFORE fitting, so the view matches where
    // the nodes actually end up (otherwise they drift out of frame after fit).
    requestAnimationFrame(() => {
      resize();
      let guard = 0;
      while (alpha > 0.02 && guard++ < 600) step();
      fit();
      draw();   // paint the settled layout immediately, before the rAF loop
      loop();
    });

    return { destroy() { cancelAnimationFrame(raf); ro.disconnect(); mo.disconnect(); } };
  }

  window.OmindGraph = { render };
})();
