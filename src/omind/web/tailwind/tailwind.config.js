// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Aaron K. Clark
//
// Build inputs for the vendored Tailwind stylesheet. End users never run this;
// the compiled output (../static/vendor/tailwind.css) is committed so `omind
// serve` works fully offline. Regenerate after changing utility classes with:
//
//   cd src/omind/web/tailwind
//   npx -y tailwindcss@3.4.17 -c tailwind.config.js -i input.css \
//       -o ../static/vendor/tailwind.css --minify

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["../static/index.html", "../static/app.js"],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Bricolage Grotesque"', "system-ui", "sans-serif"],
        body: ["Newsreader", "Georgia", "serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      colors: {
        paper: { DEFAULT: "var(--bg)", 100: "var(--bg-elev)", card: "var(--surface)" },
        ink: { DEFAULT: "var(--text)", soft: "var(--text-soft)", faint: "var(--text-faint)" },
        stamp: "var(--accent)",
        rule: "var(--border)",
      },
    },
  },
};
