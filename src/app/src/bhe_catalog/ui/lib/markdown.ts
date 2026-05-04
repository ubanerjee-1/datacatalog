/**
 * Tiny markdown -> HTML renderer.
 *
 * Intentionally minimal so we don't pull in a 50KB+ dependency for what is
 * essentially documentation rendering. Supports: headings (#…######), bold,
 * italic, inline code, links, fenced code blocks, blockquotes, ordered and
 * unordered lists, hr, GFM pipe tables, paragraphs.
 *
 * If we need more (footnotes, math, syntax highlighting), swap in `marked`
 * or `react-markdown` — both ship with React 19 cleanly.
 */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// --- GFM pipe table helpers ------------------------------------------------

// Split a `| a | b | c |` row into ['a', 'b', 'c']. Honours backslash-escaped
// pipes (`\|`) so cells can contain literal pipe characters.
function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  const cells: string[] = [];
  let buf = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") {
      buf += "|";
      i++;
      continue;
    }
    if (ch === "|") {
      cells.push(buf.trim());
      buf = "";
      continue;
    }
    buf += ch;
  }
  cells.push(buf.trim());
  return cells;
}

// Header-separator row: each cell is dashes with optional leading/trailing
// colons for alignment. Requires at least one pipe so a single `---` line
// (horizontal rule) isn't misread as a table.
function isTableSeparator(line: string): boolean {
  if (!/\|/.test(line)) return false;
  const cells = splitTableRow(line);
  if (cells.length === 0) return false;
  return cells.every((c) => /^:?-{3,}:?$/.test(c));
}

// A line shaped like a table row. We require at least one pipe to avoid
// false positives.
function isTableRow(line: string): boolean {
  return /\|/.test(line) && /^\s*\|?.*\|.*$/.test(line);
}

function parseAlign(sepCell: string): "left" | "right" | "center" | null {
  const c = sepCell.trim();
  const left = c.startsWith(":");
  const right = c.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  if (left) return "left";
  return null;
}

function alignAttr(a: "left" | "right" | "center" | null): string {
  return a ? ` style="text-align:${a}"` : "";
}

function inline(text: string): string {
  let out = escapeHtml(text);
  // Inline code (escape first so we don't double-escape inside).
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  // Bold + italic
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  out = out.replace(/_([^_]+)_/g, "<em>$1</em>");
  // Links [text](url) — only http(s)/mailto/relative; reject javascript:
  out = out.replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_m, label, href) => {
      const safe = /^(https?:|mailto:|\/|#)/i.test(href) ? href : "#";
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    },
  );
  return out;
}

export function renderMarkdown(md: string): string {
  if (!md) return "";
  const lines = md.replace(/\r\n?/g, "\n").split("\n");
  const out: string[] = [];

  let i = 0;
  let inList: "ul" | "ol" | null = null;
  let inPara: string[] = [];
  let inBlockquote = false;

  const flushPara = () => {
    if (inPara.length) {
      out.push(`<p>${inline(inPara.join(" "))}</p>`);
      inPara = [];
    }
  };
  const closeList = () => {
    if (inList) {
      out.push(`</${inList}>`);
      inList = null;
    }
  };
  const closeBlockquote = () => {
    if (inBlockquote) {
      out.push("</blockquote>");
      inBlockquote = false;
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (/^```/.test(line)) {
      flushPara();
      closeList();
      closeBlockquote();
      const lang = line.replace(/^```/, "").trim();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : "";
      out.push(
        `<pre><code${langClass}>${escapeHtml(buf.join("\n"))}</code></pre>`,
      );
      continue;
    }

    // Heading
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara();
      closeList();
      closeBlockquote();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^\s*([-*_])\s*\1\s*\1[\s-*_]*$/.test(line)) {
      flushPara();
      closeList();
      closeBlockquote();
      out.push("<hr />");
      i++;
      continue;
    }

    // GFM pipe table: a header row followed immediately by a separator row.
    // Peek ahead to confirm before we commit to consuming lines as table.
    if (
      isTableRow(line) &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      flushPara();
      closeList();
      closeBlockquote();
      const headerCells = splitTableRow(line);
      const aligns = splitTableRow(lines[i + 1]).map(parseAlign);
      i += 2;
      const dataRows: string[][] = [];
      while (i < lines.length && isTableRow(lines[i])) {
        dataRows.push(splitTableRow(lines[i]));
        i++;
      }
      const thead = headerCells
        .map(
          (c, idx) =>
            `<th${alignAttr(aligns[idx] ?? null)}>${inline(c)}</th>`,
        )
        .join("");
      const tbody = dataRows
        .map(
          (row) =>
            "<tr>" +
            row
              .map(
                (c, idx) =>
                  `<td${alignAttr(aligns[idx] ?? null)}>${inline(c)}</td>`,
              )
              .join("") +
            "</tr>",
        )
        .join("");
      out.push(
        `<table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`,
      );
      continue;
    }

    // Blockquote
    const bq = /^>\s?(.*)$/.exec(line);
    if (bq) {
      flushPara();
      closeList();
      if (!inBlockquote) {
        out.push("<blockquote>");
        inBlockquote = true;
      }
      out.push(`<p>${inline(bq[1])}</p>`);
      i++;
      continue;
    } else if (inBlockquote && line.trim() === "") {
      closeBlockquote();
    }

    // Unordered list
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (ul) {
      flushPara();
      if (inList !== "ul") {
        closeList();
        out.push("<ul>");
        inList = "ul";
      }
      out.push(`<li>${inline(ul[1])}</li>`);
      i++;
      continue;
    }

    // Ordered list
    const ol = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (ol) {
      flushPara();
      if (inList !== "ol") {
        closeList();
        out.push("<ol>");
        inList = "ol";
      }
      out.push(`<li>${inline(ol[1])}</li>`);
      i++;
      continue;
    }

    closeList();

    if (line.trim() === "") {
      flushPara();
      i++;
      continue;
    }

    inPara.push(line);
    i++;
  }
  flushPara();
  closeList();
  closeBlockquote();
  return out.join("\n");
}
