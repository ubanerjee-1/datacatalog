/**
 * Browser-native PDF export for markdown articles.
 *
 * Opens a freshly rendered, print-friendly copy of an article in a new
 * window and triggers the browser's print dialog with "Save as PDF"
 * preselected. No PDF library, no canvas snapshots — the output is real
 * selectable text, identical to what's on screen.
 *
 * Why a new window vs. inline `@media print`?
 *   - The article lives inside the app shell (sidebars, cards, headers).
 *     Hiding everything with a print stylesheet is brittle and easy to
 *     break the next time someone restructures a layout.
 *   - A blank window gives us a clean canvas with our own minimal print
 *     CSS, so the PDF looks consistent across pages and routes.
 */

import { renderMarkdown } from "./markdown";

// Self-contained print stylesheet. Uses pt units throughout because the
// output is paper, not screen. The numbers are tuned for letter-size at
// 0.6in margins — adjust @page below if that changes.
const PRINT_STYLES = `
  @page { margin: 0.6in; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    color: #111;
    line-height: 1.55;
    font-size: 11pt;
    margin: 0;
  }
  .doc-meta {
    font-size: 9pt;
    color: #666;
    margin-bottom: 18pt;
    padding-bottom: 8pt;
    border-bottom: 1px solid #ddd;
  }
  h1 {
    font-size: 22pt;
    margin: 0 0 10pt;
    page-break-after: avoid;
  }
  h2 {
    font-size: 15pt;
    margin: 20pt 0 8pt;
    padding-bottom: 4pt;
    border-bottom: 1px solid #ddd;
    page-break-after: avoid;
  }
  h3 {
    font-size: 12pt;
    margin: 14pt 0 6pt;
    page-break-after: avoid;
  }
  h4 { font-size: 11pt; margin: 10pt 0 4pt; }
  p { margin: 6pt 0; }
  ul, ol { margin: 6pt 0; padding-left: 22pt; }
  li { margin: 2pt 0; }
  table {
    border-collapse: collapse;
    margin: 8pt 0;
    font-size: 10pt;
    width: 100%;
    page-break-inside: avoid;
  }
  th, td {
    border: 1px solid #999;
    padding: 4pt 6pt;
    text-align: left;
    vertical-align: top;
  }
  th { background: #f0f0f0; font-weight: 600; }
  code {
    background: #f4f4f4;
    padding: 1pt 3pt;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 9.5pt;
    border-radius: 2pt;
  }
  pre {
    background: #f4f4f4;
    padding: 8pt;
    border-radius: 3pt;
    overflow-x: hidden;
    font-size: 9.5pt;
    white-space: pre-wrap;
    page-break-inside: avoid;
  }
  pre code { background: transparent; padding: 0; }
  blockquote {
    border-left: 3px solid #ccc;
    padding-left: 10pt;
    color: #555;
    margin: 8pt 0;
    font-style: italic;
  }
  a { color: #0366d6; text-decoration: underline; }
  hr { border: none; border-top: 1px solid #ddd; margin: 12pt 0; }
  img { max-width: 100%; }
`;

function escapeHtmlAttr(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export interface ExportArticleOptions {
  title: string;
  bodyMarkdown: string;
  /** Optional subtitle line shown above the article body (e.g. timestamp,
   *  author, version). Plain text — will be HTML-escaped. */
  meta?: string;
}

/**
 * Render `bodyMarkdown` to HTML and open a new window with the print dialog.
 * The browser's "Save as PDF" option in that dialog produces a real-text PDF.
 *
 * Throws if the popup is blocked so callers can surface a toast.
 */
export function exportArticleAsPdf(opts: ExportArticleOptions): void {
  const { title, bodyMarkdown, meta } = opts;
  const bodyHtml = renderMarkdown(bodyMarkdown);
  // The document title is what most browsers use as the default save-as-PDF
  // filename — give them something descriptive.
  const safeTitle = title.replace(/[<>]/g, "").trim() || "Article";

  // NOTE: do NOT pass "noopener" / "noreferrer" here. Those features make
  // window.open return null because the new window is intentionally
  // detached from the opener — and we need the reference to inject the
  // rendered HTML and trigger print().
  const win = window.open("", "_blank");
  if (!win) {
    throw new Error(
      "Could not open the print window. Disable your pop-up blocker and try again.",
    );
  }

  win.document.open();
  win.document.write(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>${escapeHtmlAttr(safeTitle)}</title>
<style>${PRINT_STYLES}</style>
</head>
<body>
${meta ? `<div class="doc-meta">${escapeHtmlAttr(meta)}</div>` : ""}
${bodyHtml}
</body>
</html>`);
  win.document.close();

  // Wait until the new document has laid out before invoking print(). On
  // Safari the load event fires reliably; on Chrome readyState is often
  // already "complete" right after document.close() — handle both.
  const trigger = () => {
    try {
      win.focus();
      win.print();
    } catch (e) {
      // Window is open and rendered; if print() fails the user can still
      // hit Cmd/Ctrl+P themselves.
      // eslint-disable-next-line no-console
      console.warn("Auto-print failed; user can press Cmd/Ctrl+P:", e);
    }
  };
  if (win.document.readyState === "complete") {
    setTimeout(trigger, 80);
  } else {
    win.addEventListener("load", () => setTimeout(trigger, 80), { once: true });
  }
}
