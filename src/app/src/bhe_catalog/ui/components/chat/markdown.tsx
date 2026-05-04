/**
 * Tiny markdown renderer for chat assistant messages.
 *
 * Why a custom renderer? The local network blocks `registry.npmjs.org`
 * (same constraint as pypi.org — see local-dev rule), so we can't add
 * `react-markdown` + `remark-gfm`. The LLM's markdown vocabulary is
 * narrow and predictable, so a hand-rolled subset parser is fine and
 * actually leaner than pulling in 300 KB of remark/rehype.
 *
 * Supported (in priority order — table support is the trigger for
 * this file existing):
 *   - GFM tables   `| col | col |  /  |---|---|  /  | a | b |`
 *   - Headers      `#`, `##`, `###` (h4-h6 mapped to h3 styling)
 *   - Bulleted     `- ` or `* ` at line start
 *   - Numbered     `1. ` `2. ` ...
 *   - Code blocks  ``` ``` ```
 *   - Bold         `**foo**`
 *   - Italic       `*foo*` (only inside text — collides with bold; we
 *                  consume bold first then look for single `*` runs)
 *   - Inline code  `` `foo` ``
 *   - Paragraphs   blank line separates
 *   - Hard breaks  (newline within a paragraph → <br/>)
 *
 * Explicitly NOT supported:
 *   - Links / images / blockquotes / strikethrough / task lists / HTML
 *     The LLM rarely emits these inside chat messages, and adding them
 *     bloats the parser without buying user-visible improvements. If
 *     a need arises, the right move is to `npm install react-markdown`
 *     once the registry blocker is resolved, not to extend this file.
 *
 * Streaming-safety:
 *   The parser MUST be robust to partial input — assistants stream
 *   tokens, so the content prop changes character-by-character. A
 *   half-written `**` run, an unclosed table row, or an incomplete
 *   code fence should all render *something* sensible without
 *   throwing. Strategy: every block parser falls back to "treat as
 *   paragraph" when the block doesn't fully match its expected shape.
 */
import { Fragment, type ReactNode } from "react";

// ---------------------------------------------------------------------------
// Block-level
// ---------------------------------------------------------------------------

type Block =
  | { type: "code"; lang: string | null; text: string }
  | { type: "heading"; level: number; text: string }
  | { type: "ulist"; items: string[] }
  | { type: "olist"; items: string[] }
  | { type: "table"; header: string[]; aligns: Array<"left" | "right" | "center" | null>; rows: string[][] }
  | { type: "para"; text: string };

const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const FENCE = /^```(\w*)\s*$/;

function splitTableRow(line: string): string[] {
  // Trim outer pipes (`| a | b |` → ` a | b `), then split on the
  // pipes between cells. Doesn't try to handle escaped pipes (`\|`)
  // because the model doesn't emit them inside chat tables.
  const trimmed = line.trim().replace(/^\||\|$/g, "");
  return trimmed.split("|").map((c) => c.trim());
}

function parseAligns(divider: string): Array<"left" | "right" | "center" | null> {
  return splitTableRow(divider).map((cell) => {
    const c = cell.trim();
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return null;
  });
}

function parseBlocks(src: string): Block[] {
  // Normalize line endings; preserve trailing newlines so streaming
  // mid-block doesn't accidentally drop the active line.
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Code fence — consume until matching ``` or EOF (streaming-safe).
    const fence = line.match(FENCE);
    if (fence) {
      const lang = fence[1] || null;
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++; // consume closing fence
      blocks.push({ type: "code", lang, text: buf.join("\n") });
      continue;
    }

    // Blank line — block separator; eat and continue.
    if (!line.trim()) {
      i++;
      continue;
    }

    // Heading — # to ###### (clamp deeper levels to 3).
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      blocks.push({
        type: "heading",
        level: Math.min(h[1].length, 3),
        text: h[2].trim(),
      });
      i++;
      continue;
    }

    // Table — header row followed immediately by a divider row. We
    // require both to be present; a lone `| a | b |` line without
    // the dashes-divider falls through to paragraph rendering, so
    // the model can talk about pipe-delimited data without
    // accidentally triggering table layout.
    if (
      TABLE_ROW.test(line) &&
      i + 1 < lines.length &&
      TABLE_DIVIDER.test(lines[i + 1])
    ) {
      const header = splitTableRow(line);
      const aligns = parseAligns(lines[i + 1]);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      blocks.push({ type: "table", header, aligns, rows });
      continue;
    }

    // Bulleted list — group consecutive `- ` / `* ` lines.
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      blocks.push({ type: "ulist", items });
      continue;
    }

    // Numbered list — group consecutive `N. ` lines.
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ type: "olist", items });
      continue;
    }

    // Paragraph — gather until blank line or block boundary.
    const paraLines: string[] = [line];
    i++;
    while (i < lines.length) {
      const next = lines[i];
      if (
        !next.trim() ||
        /^#{1,6}\s/.test(next) ||
        FENCE.test(next) ||
        /^\s*[-*]\s+/.test(next) ||
        /^\s*\d+\.\s+/.test(next) ||
        (TABLE_ROW.test(next) &&
          i + 1 < lines.length &&
          TABLE_DIVIDER.test(lines[i + 1]))
      ) {
        break;
      }
      paraLines.push(next);
      i++;
    }
    blocks.push({ type: "para", text: paraLines.join("\n") });
  }
  return blocks;
}

// ---------------------------------------------------------------------------
// Inline-level
// ---------------------------------------------------------------------------

/**
 * Render inline markdown (bold/italic/inline-code) inside a string.
 *
 * We use a simple left-to-right scanner that consumes the LONGEST
 * marker first — `**bold**` before `*italic*`, otherwise the italic
 * pass would steal the `*` characters. Inline code is consumed first
 * so its contents are treated as literal (no further markdown applied
 * inside backticks).
 *
 * This is NOT a full CommonMark inline parser; pathological inputs
 * (mixed-up nesting, escaped specials) may render as literals. That's
 * fine for chat output where the model emits clean markdown.
 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  let buf = "";
  let n = 0;

  const flushBuf = () => {
    if (!buf) return;
    // Convert literal `\n` inside paragraphs to <br/>. The model
    // doesn't always insert blank lines between bullet items it
    // wants on separate lines.
    const parts = buf.split("\n");
    parts.forEach((part, idx) => {
      if (idx > 0) {
        out.push(<br key={`${keyPrefix}-br-${n++}`} />);
      }
      if (part) out.push(part);
    });
    buf = "";
  };

  while (i < text.length) {
    const ch = text[i];

    // Inline code. Greedy: consume up to the matching backtick.
    if (ch === "`") {
      const close = text.indexOf("`", i + 1);
      if (close > i) {
        flushBuf();
        out.push(
          <code
            key={`${keyPrefix}-code-${n++}`}
            className="rounded bg-muted-foreground/15 px-1 py-0.5 font-mono text-[0.85em]"
          >
            {text.slice(i + 1, close)}
          </code>,
        );
        i = close + 1;
        continue;
      }
    }

    // Bold (`**...**`). Consumed BEFORE italic to avoid the italic
    // parser eating the inner asterisks.
    if (ch === "*" && text[i + 1] === "*") {
      const close = text.indexOf("**", i + 2);
      if (close > i + 1) {
        flushBuf();
        const inner = text.slice(i + 2, close);
        out.push(
          <strong key={`${keyPrefix}-b-${n++}`} className="font-semibold">
            {renderInline(inner, `${keyPrefix}-bi-${n}`)}
          </strong>,
        );
        i = close + 2;
        continue;
      }
    }

    // Italic via `*foo*` — single asterisk, NOT preceded/followed
    // by another `*` (so it doesn't compete with bold).
    if (
      ch === "*" &&
      text[i + 1] !== "*" &&
      text[i - 1] !== "*"
    ) {
      // Find a closing single `*` that's not part of `**`.
      let j = i + 1;
      while (j < text.length) {
        if (text[j] === "*" && text[j - 1] !== "\\" && text[j + 1] !== "*") {
          break;
        }
        j++;
      }
      if (j < text.length && j > i + 1) {
        flushBuf();
        const inner = text.slice(i + 1, j);
        out.push(
          <em key={`${keyPrefix}-i-${n++}`} className="italic">
            {renderInline(inner, `${keyPrefix}-ii-${n}`)}
          </em>,
        );
        i = j + 1;
        continue;
      }
    }

    // Italic via `_foo_` — same shape as `*foo*` but with underscores.
    // We only fire when the `_` looks word-bounded so we don't
    // chop snake_case_identifiers in code-like prose.
    if (
      ch === "_" &&
      text[i - 1] !== "_" &&
      (i === 0 || /\s|^[(,.;:]/.test(text[i - 1] || " "))
    ) {
      const close = text.indexOf("_", i + 1);
      if (close > i + 1) {
        const after = text[close + 1] || " ";
        if (/\s|[)\.,;:!?]/.test(after)) {
          flushBuf();
          const inner = text.slice(i + 1, close);
          out.push(
            <em key={`${keyPrefix}-iu-${n++}`} className="italic">
              {renderInline(inner, `${keyPrefix}-iui-${n}`)}
            </em>,
          );
          i = close + 1;
          continue;
        }
      }
    }

    buf += ch;
    i++;
  }
  flushBuf();
  return out;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Markdown({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  if (!content) return null;
  const blocks = parseBlocks(content);

  return (
    <div className={className}>
      {blocks.map((b, idx) => {
        const key = `b-${idx}`;
        switch (b.type) {
          case "heading": {
            const sizes = ["text-base", "text-sm", "text-sm"];
            const Tag = (b.level === 1 ? "h3" : b.level === 2 ? "h4" : "h5") as
              | "h3"
              | "h4"
              | "h5";
            return (
              <Tag
                key={key}
                className={`mt-2 mb-1 font-semibold ${sizes[b.level - 1] ?? "text-sm"} first:mt-0`}
              >
                {renderInline(b.text, key)}
              </Tag>
            );
          }
          case "code":
            return (
              <pre
                key={key}
                className="my-1.5 overflow-x-auto rounded bg-muted-foreground/10 px-2 py-1.5 font-mono text-[11px] leading-snug first:mt-0 last:mb-0"
              >
                <code>{b.text}</code>
              </pre>
            );
          case "ulist":
            return (
              <ul
                key={key}
                className="my-1 list-disc space-y-0.5 pl-5 first:mt-0 last:mb-0"
              >
                {b.items.map((it, j) => (
                  <li key={`${key}-i-${j}`}>{renderInline(it, `${key}-i-${j}`)}</li>
                ))}
              </ul>
            );
          case "olist":
            return (
              <ol
                key={key}
                className="my-1 list-decimal space-y-0.5 pl-5 first:mt-0 last:mb-0"
              >
                {b.items.map((it, j) => (
                  <li key={`${key}-i-${j}`}>{renderInline(it, `${key}-i-${j}`)}</li>
                ))}
              </ol>
            );
          case "table": {
            // Map our align values to Tailwind text-alignment classes.
            const alignClass = (a: typeof b.aligns[number]) =>
              a === "right"
                ? "text-right"
                : a === "center"
                  ? "text-center"
                  : "text-left";
            return (
              <div
                key={key}
                className="my-1.5 overflow-x-auto first:mt-0 last:mb-0"
              >
                <table className="min-w-full border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-border">
                      {b.header.map((cell, j) => (
                        <th
                          key={j}
                          className={`px-2 py-1 font-semibold ${alignClass(b.aligns[j] ?? null)}`}
                        >
                          {renderInline(cell, `${key}-h-${j}`)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {b.rows.map((row, j) => (
                      <tr
                        key={j}
                        className="border-b border-border/50 last:border-b-0"
                      >
                        {row.map((cell, k) => (
                          <td
                            key={k}
                            className={`px-2 py-1 align-top ${alignClass(b.aligns[k] ?? null)}`}
                          >
                            {renderInline(cell, `${key}-r-${j}-${k}`)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }
          case "para":
            return (
              <p
                key={key}
                className="my-1 leading-relaxed first:mt-0 last:mb-0"
              >
                {renderInline(b.text, key)}
              </p>
            );
          default: {
            // Type-narrowing escape hatch — TS will warn here if a
            // new block kind is added without an arm above.
            const _exhaust: never = b;
            return <Fragment key={key}>{String(_exhaust)}</Fragment>;
          }
        }
      })}
    </div>
  );
}
