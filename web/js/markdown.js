// Safe, dependency-free Markdown renderer for assistant answers.
// All text is inserted through textContent; raw HTML is never parsed.
// Links are limited to http(s) and the UI intercepts external URLs.

import { renderMathInto } from "./math.js";

const DEFAULT_BASE = "http://127.0.0.1/";

export function normalizeExternalUrl(raw, base = null) {
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  if (!value) return null;
  const baseHref = base || (typeof location !== "undefined" ? location.href : DEFAULT_BASE);
  try {
    const url = new URL(value, baseHref);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password) return null;
    return url.href;
  } catch (_err) {
    return null;
  }
}

export function isExternalUrl(raw, base = null) {
  const normalized = normalizeExternalUrl(raw, base);
  if (!normalized) return false;
  const baseHref = base || (typeof location !== "undefined" ? location.href : DEFAULT_BASE);
  try {
    return new URL(normalized).origin !== new URL(baseHref).origin;
  } catch (_err) {
    return true;
  }
}

// ---------------------------------------------------------------------------
// Block parser (pure, testable without a DOM)

function isBlank(line) {
  return /^\s*$/.test(line);
}

function fenceStart(line) {
  const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
  if (!match) return null;
  return { marker: match[1], lang: match[2].trim().split(/\s+/)[0] || "" };
}

function isHeading(line) {
  return /^ {0,3}#{1,6}\s+/.test(line);
}

function isHr(line) {
  return /^ {0,3}(?:(\*[ \t]*){3,}|(-[ \t]*){3,}|(_[ \t]*){3,})$/.test(line);
}

function isQuote(line) {
  return /^ {0,3}>/.test(line);
}

function listMatch(line) {
  return (
    line.match(/^ {0,3}([-*+])\s+(.*)$/) ||
    line.match(/^ {0,3}(\d+)[.)]\s+(.*)$/)
  );
}

function splitTableRow(line) {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|") && !value.endsWith("\\|")) value = value.slice(0, -1);
  const cells = [];
  let current = "";
  let escaped = false;
  for (const ch of value) {
    if (escaped) {
      current += ch;
      escaped = false;
    } else if (ch === "\\") {
      escaped = true;
    } else if (ch === "|") {
      cells.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  cells.push(current.trim());
  return cells;
}

function tableAlignments(line) {
  const cells = splitTableRow(line);
  if (!cells.length) return null;
  const align = [];
  for (const cell of cells) {
    const value = cell.replace(/\s+/g, "");
    if (!/^:?-{3,}:?$/.test(value)) return null;
    if (value.startsWith(":") && value.endsWith(":")) align.push("center");
    else if (value.endsWith(":")) align.push("right");
    else if (value.startsWith(":")) align.push("left");
    else align.push("left");
  }
  return align;
}

function isTableStart(lines, index) {
  const header = lines[index] || "";
  const separator = lines[index + 1] || "";
  if (!header.includes("|") || !separator.includes("|")) return false;
  return Boolean(tableAlignments(separator));
}

function displayMathStart(line) {
  const text = line.trim();
  if (text.startsWith("$$")) return { kind: "dollar", rest: text.slice(2) };
  if (text.startsWith("\\[")) return { kind: "bracket", rest: text.slice(2) };
  const environment = text.match(
    /^\\begin\{(aligned|align|cases|matrix|pmatrix|bmatrix|vmatrix|Vmatrix|array)\}/
  );
  if (environment) return { kind: "environment", environment: environment[1] };
  return null;
}

function collectDisplayMath(lines, index) {
  const start = displayMathStart(lines[index]);
  if (!start) return null;

  if (start.kind === "dollar" || start.kind === "bracket") {
    const close = start.kind === "dollar" ? "$$" : "\\]";
    const inlineEnd = start.rest.indexOf(close);
    if (inlineEnd !== -1) {
      return {
        text: start.rest.slice(0, inlineEnd).trim(),
        next: index + 1,
      };
    }
    const parts = [start.rest];
    let cursor = index + 1;
    while (cursor < lines.length) {
      const end = lines[cursor].indexOf(close);
      if (end !== -1) {
        parts.push(lines[cursor].slice(0, end));
        return { text: parts.join("\n").trim(), next: cursor + 1 };
      }
      parts.push(lines[cursor]);
      cursor += 1;
    }
    return null;
  }

  const close = `\\end{${start.environment}}`;
  if (lines[index].includes(close)) {
    return { text: lines[index].trim(), next: index + 1 };
  }
  const parts = [lines[index]];
  let cursor = index + 1;
  while (cursor < lines.length) {
    parts.push(lines[cursor]);
    if (lines[cursor].includes(close)) {
      return { text: parts.join("\n").trim(), next: cursor + 1 };
    }
    cursor += 1;
  }
  return null;
}

function startsBlock(lines, index) {
  const line = lines[index];
  if (isBlank(line)) return true;
  if (fenceStart(line) || isHeading(line) || isHr(line) || isQuote(line)) return true;
  if (listMatch(line)) return true;
  if (displayMathStart(line)) return true;
  return isTableStart(lines, index);
}

function parseList(lines, start) {
  const first = listMatch(lines[start]);
  const ordered = /^\d/.test(first[1]);
  const items = [];
  let index = start;
  const ordinal = ordered ? Number.parseInt(first[1], 10) || 1 : 1;
  while (index < lines.length) {
    const match = listMatch(lines[index]);
    if (!match) break;
    const itemLines = [match[2]];
    index += 1;
    while (index < lines.length) {
      const next = lines[index];
      if (isBlank(next)) break;
      if (listMatch(next)) break;
      if (/^ {2,}/.test(next)) {
        itemLines.push(next.replace(/^\s{1,4}/, ""));
        index += 1;
        continue;
      }
      if (startsBlock(lines, index)) break;
      itemLines.push(next);
      index += 1;
    }
    items.push(itemLines.join("\n").trim());
  }
  return { type: "list", ordered, start: ordinal, items, next: index };
}

export function parseMarkdown(text) {
  const source = String(text ?? "").replace(/\r\n?/g, "\n");
  const lines = source.split("\n");
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (isBlank(line)) {
      index += 1;
      continue;
    }

    const fence = fenceStart(line);
    if (fence) {
      const marker = fence.marker;
      const char = marker[0];
      index += 1;
      const codeLines = [];
      while (index < lines.length) {
        const trimmed = lines[index].trim();
        if (
          trimmed.length >= marker.length &&
          trimmed.split("").every((ch) => ch === char)
        ) {
          index += 1;
          break;
        }
        codeLines.push(lines[index]);
        index += 1;
      }
      blocks.push({
        type: "code",
        lang: /^[A-Za-z0-9_+-]+$/.test(fence.lang) ? fence.lang : "",
        text: codeLines.join("\n"),
      });
      continue;
    }

    const heading = line.match(/^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      index += 1;
      continue;
    }

    if (isHr(line)) {
      blocks.push({ type: "hr" });
      index += 1;
      continue;
    }

    if (isQuote(line)) {
      const quoteLines = [];
      while (index < lines.length && isQuote(lines[index])) {
        quoteLines.push(lines[index].replace(/^ {0,3}>\s?/, ""));
        index += 1;
      }
      blocks.push({ type: "blockquote", blocks: parseMarkdown(quoteLines.join("\n")) });
      continue;
    }

    if (listMatch(line)) {
      const result = parseList(lines, index);
      blocks.push({ type: "list", ordered: result.ordered, start: result.start, items: result.items });
      index = result.next;
      continue;
    }

    if (isTableStart(lines, index)) {
      const header = splitTableRow(lines[index]);
      const align = tableAlignments(lines[index + 1]) || [];
      index += 2;
      const rows = [];
      while (index < lines.length && !isBlank(lines[index]) && lines[index].includes("|")) {
        if (isHeading(lines[index]) || isHr(lines[index])) break;
        const cells = splitTableRow(lines[index]);
        if (cells.length === 1) break;
        rows.push(cells);
        index += 1;
      }
      const columns = Math.min(Math.max(header.length, align.length, 1), 12);
      blocks.push({
        type: "table",
        header: header.slice(0, columns),
        align: align.slice(0, columns),
        rows: rows.slice(0, 200).map((row) => row.slice(0, columns)),
      });
      continue;
    }

    const displayMath = collectDisplayMath(lines, index);
    if (displayMath) {
      blocks.push({ type: "math", display: true, text: displayMath.text });
      index = displayMath.next;
      continue;
    }

    const paragraph = [];
    while (index < lines.length && !isBlank(lines[index]) && !startsBlock(lines, index)) {
      paragraph.push(lines[index]);
      index += 1;
    }
    if (!paragraph.length) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.join("\n").trim() });
  }
  return blocks;
}

// ---------------------------------------------------------------------------
// Inline renderer

function appendText(parent, text, doc) {
  if (!text) return;
  parent.appendChild(doc.createTextNode(String(text)));
}

function appendExternalLink(parent, href, label, doc, depth) {
  const normalized = normalizeExternalUrl(href);
  if (!normalized) {
    appendText(parent, label || href, doc);
    return;
  }
  const anchor = doc.createElement("a");
  anchor.setAttribute("href", normalized);
  anchor.setAttribute("target", "_blank");
  anchor.setAttribute("rel", "noopener noreferrer");
  anchor.className = "external-link";
  appendInline(anchor, label || normalized, doc, depth + 1);
  parent.appendChild(anchor);
}

function appendInlineMath(parent, tex, doc) {
  const surface = doc.createElement("span");
  renderMathInto(surface, tex, { display: false, document: doc });
  parent.appendChild(surface);
}

function matchInlineMath(rest) {
  if (rest.startsWith("\\(")) {
    const end = rest.indexOf("\\)", 2);
    if (end > 2) return { text: rest.slice(2, end), length: end + 2 };
    return null;
  }
  if (rest.startsWith("$") && !rest.startsWith("$$")) {
    const end = rest.indexOf("$", 1);
    if (end > 1) {
      const body = rest.slice(1, end);
      if (
        body.trim() &&
        !/^\s|\s$/.test(body) &&
        /[A-Za-z\\^_=+\-*/<>]/.test(body)
      ) {
        return { text: body, length: end + 1 };
      }
    }
  }
  return null;
}

function appendInline(parent, text, doc, depth = 0) {
  if (depth > 12) {
    appendText(parent, text, doc);
    return;
  }
  let index = 0;
  while (index < text.length) {
    const rest = text.slice(index);

    if (rest.startsWith("\n")) {
      parent.appendChild(doc.createElement("br"));
      index += 1;
      continue;
    }

    const inlineMath = matchInlineMath(rest);
    if (inlineMath) {
      appendInlineMath(parent, inlineMath.text, doc);
      index += inlineMath.length;
      continue;
    }

    if (rest[0] === "\\" && index + 1 < text.length) {
      const next = text[index + 1];
      if ("\\`*_{}[]()#+-.!|>~".includes(next)) {
        appendText(parent, next, doc);
        index += 2;
        continue;
      }
    }

    if (rest[0] === "`") {
      const match = rest.match(/^(`+)([\s\S]*?)\1/);
      if (match) {
        const code = doc.createElement("code");
        code.textContent = match[2].replace(/^ /, "").replace(/ $/, "");
        parent.appendChild(code);
        index += match[0].length;
        continue;
      }
    }

    let matchedMarker = false;
    for (const marker of ["**", "__", "~~"]) {
      if (rest.startsWith(marker)) {
        const end = rest.indexOf(marker, 2);
        if (end > 2) {
          const tag = marker === "~~" ? "del" : "strong";
          const node = doc.createElement(tag);
          appendInline(node, rest.slice(2, end), doc, depth + 1);
          parent.appendChild(node);
          index += end + 2;
          matchedMarker = true;
          break;
        }
      }
    }
    if (matchedMarker) continue;

    if (rest[0] === "*" || rest[0] === "_") {
      const marker = rest[0];
      const end = rest.indexOf(marker, 1);
      if (end > 1) {
        const node = doc.createElement("em");
        appendInline(node, rest.slice(1, end), doc, depth + 1);
        parent.appendChild(node);
        index += end + 1;
        continue;
      }
    }

    if (rest[0] === "[") {
      const match = rest.match(/^\[([^\]]*)\]\(\s*([^\s)]+)(?:\s+["'][^"']*["'])?\s*\)/);
      if (match) {
        appendExternalLink(parent, match[2], match[1], doc, depth);
        index += match[0].length;
        continue;
      }
    }

    if (/^https?:\/\//i.test(rest)) {
      const match = rest.match(/^https?:\/\/[^\s<]+/i);
      if (match) {
        let raw = match[0];
        let trailing = "";
        while (/[.,;:!?，。；：！？)\]】}]$/.test(raw)) {
          trailing = raw.slice(-1) + trailing;
          raw = raw.slice(0, -1);
        }
        const normalized = normalizeExternalUrl(raw);
        if (normalized) {
          const anchor = doc.createElement("a");
          anchor.setAttribute("href", normalized);
          anchor.setAttribute("target", "_blank");
          anchor.setAttribute("rel", "noopener noreferrer");
          anchor.className = "external-link";
          anchor.textContent = raw;
          parent.appendChild(anchor);
        } else {
          appendText(parent, raw, doc);
        }
        appendText(parent, trailing, doc);
        index += match[0].length;
        continue;
      }
    }

    let next = index + 1;
    while (next < text.length) {
      const ch = text[next];
      if (
        ch === "\n" ||
        ch === "\\" ||
        ch === "`" ||
        ch === "*" ||
        ch === "_" ||
        ch === "~" ||
        ch === "[" ||
        ch === "$" ||
        (ch === "h" && /^https?:\/\//i.test(text.slice(next)))
      ) {
        break;
      }
      next += 1;
    }
    appendText(parent, text.slice(index, next), doc);
    index = next;
  }
}

// ---------------------------------------------------------------------------
// DOM renderer

function addClass(node, name) {
  if (!name) return;
  if (node.classList) node.classList.add(name);
  else node.className = `${node.className || ""} ${name}`.trim();
}

function appendBlocks(parent, blocks, doc, options) {
  for (const block of blocks) {
    if (block.type === "paragraph") {
      const paragraph = doc.createElement("p");
      appendInline(paragraph, block.text, doc, 0);
      parent.appendChild(paragraph);
    } else if (block.type === "heading") {
      const heading = doc.createElement(`h${Math.min(Math.max(block.level, 1), 6)}`);
      appendInline(heading, block.text, doc, 0);
      parent.appendChild(heading);
    } else if (block.type === "code") {
      const pre = doc.createElement("pre");
      const code = doc.createElement("code");
      if (block.lang) code.className = `language-${block.lang}`;
      code.textContent = block.text;
      pre.appendChild(code);
      parent.appendChild(pre);
    } else if (block.type === "hr") {
      parent.appendChild(doc.createElement("hr"));
    } else if (block.type === "math") {
      const math = doc.createElement("div");
      renderMathInto(math, block.text, { display: true, document: doc });
      parent.appendChild(math);
    } else if (block.type === "blockquote") {
      const quote = doc.createElement("blockquote");
      appendBlocks(quote, block.blocks, doc, options);
      parent.appendChild(quote);
    } else if (block.type === "list") {
      const list = doc.createElement(block.ordered ? "ol" : "ul");
      if (block.ordered && block.start !== 1) list.setAttribute("start", String(block.start));
      for (const item of block.items) {
        const li = doc.createElement("li");
        appendInline(li, item, doc, 0);
        list.appendChild(li);
      }
      parent.appendChild(list);
    } else if (block.type === "table") {
      const wrap = doc.createElement("div");
      wrap.className = "md-table-wrap";
      const table = doc.createElement("table");
      const thead = doc.createElement("thead");
      const headRow = doc.createElement("tr");
      for (let column = 0; column < block.header.length; column += 1) {
        const cell = doc.createElement("th");
        cell.className = `align-${block.align[column] || "left"}`;
        appendInline(cell, block.header[column], doc, 0);
        headRow.appendChild(cell);
      }
      thead.appendChild(headRow);
      table.appendChild(thead);
      if (block.rows.length) {
        const tbody = doc.createElement("tbody");
        for (const row of block.rows) {
          const tr = doc.createElement("tr");
          for (let column = 0; column < block.header.length; column += 1) {
            const cell = doc.createElement("td");
            cell.className = `align-${block.align[column] || "left"}`;
            appendInline(cell, row[column] || "", doc, 0);
            tr.appendChild(cell);
          }
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
      }
      wrap.appendChild(table);
      parent.appendChild(wrap);
    }
  }
}

export function renderMarkdownInto(container, text, options = {}) {
  const doc = options.document || container.ownerDocument || document;
  container.textContent = "";
  addClass(container, "md-content");
  if (options.compact) addClass(container, "md-compact");
  const source = String(text ?? "");
  if (!source.trim()) return;
  try {
    appendBlocks(container, parseMarkdown(source), doc, options);
  } catch (_err) {
    container.textContent = source;
    addClass(container, "md-fallback");
  }
}
