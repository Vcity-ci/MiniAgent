import test from "node:test";
import assert from "node:assert/strict";

import {
  isExternalUrl,
  normalizeExternalUrl,
  parseMarkdown,
  renderMarkdownInto,
} from "./markdown.js";
import { parseMath } from "./math.js";

function makeDocument() {
  class TextNode {
    constructor(text) {
      this.nodeType = 3;
      this._text = String(text);
    }
    get textContent() {
      return this._text;
    }
    set textContent(value) {
      this._text = String(value);
    }
  }
  class Element {
    constructor(tagName) {
      this.nodeType = 1;
      this.tagName = tagName;
      this.ownerDocument = document;
      this.children = [];
      this.attributes = {};
      this.className = "";
      this._text = "";
    }
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    }
    getAttribute(name) {
      return this.attributes[name];
    }
    appendChild(child) {
      this.children.push(child);
      return child;
    }
    set textContent(value) {
      this.children = [];
      this._text = String(value);
    }
    get textContent() {
      if (this.children.length) return this.children.map((child) => child.textContent).join("");
      return this._text;
    }
    get classList() {
      const element = this;
      return {
        add(name) {
          element.className = `${element.className || ""} ${name}`.trim();
        },
      };
    }
  }
  const document = {
    createElement(tagName) {
      return new Element(tagName);
    },
    createTextNode(text) {
      return new TextNode(text);
    },
  };
  return document;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function serialize(node) {
  if (node.nodeType === 3) return escapeHtml(node.textContent);
  const attrs = Object.entries(node.attributes)
    .map(([key, value]) => ` ${key}="${escapeHtml(value)}"`)
    .join("");
  const className = node.className ? ` class="${escapeHtml(node.className)}"` : "";
  return `<${node.tagName}${className}${attrs}>${node.children
    .map((child) => serialize(child))
    .join("")}${escapeHtml(node._text || "")}</${node.tagName}>`;
}

function render(text) {
  const document = makeDocument();
  const container = document.createElement("div");
  renderMarkdownInto(container, text, { document });
  return serialize(container);
}

test("parse headings, lists, tables, and code", () => {
  const blocks = parseMarkdown(
    "## 标题\n\n- 一\n- 二\n\n| 名称 | 数值 |\n|---|---:|\n| CPI | 0.7% |\n\n```js\nconst x = 1;\n```"
  );
  assert.equal(blocks[0].type, "heading");
  assert.equal(blocks[1].type, "list");
  assert.equal(blocks[1].items.length, 2);
  assert.equal(blocks[2].type, "table");
  assert.equal(blocks[2].align[1], "right");
  assert.equal(blocks[3].type, "code");
  assert.equal(blocks[3].lang, "js");
});

test("render bold, links, autolink, table, and code", () => {
  const html = render(
    "**粗体** 和 [链接](https://example.com/path) 与 https://example.org/a。\n\n| 名称 | 数值 |\n|---|---:|\n| CPI | 0.7% |\n\n```js\nconsole.log(1)\n```"
  );
  assert.match(html, /<strong>粗体<\/strong>/);
  assert.match(html, /<a class="external-link" href="https:\/\/example.com\/path"/);
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /href="https:\/\/example.org\/a"/);
  assert.match(html, /<table>/);
  assert.match(html, /<th class="align-left">名称<\/th>/);
  assert.match(html, /<td class="align-right">0.7%<\/td>/);
  assert.match(html, /<pre><code class="language-js">console.log\(1\)<\/code><\/pre>/);
  assert.ok(!html.includes("**粗体**"));
});

test("raw HTML and unsafe links are not executable", () => {
  const html = render(
    "<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))\n\n![alt](https://evil.example/x.png)"
  );
  assert.ok(!html.includes("<img"));
  assert.match(html, /&lt;img/);
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(!html.includes("<img"));
});

test("url policy", () => {
  assert.equal(normalizeExternalUrl("javascript:alert(1)"), null);
  assert.equal(normalizeExternalUrl("data:text/html,x"), null);
  assert.equal(normalizeExternalUrl("file:///etc/passwd"), null);
  assert.equal(normalizeExternalUrl("https://example.com/a"), "https://example.com/a");
  assert.equal(isExternalUrl("https://example.com/a", "http://127.0.0.1:8000/"), true);
  assert.equal(isExternalUrl("/api/health", "http://127.0.0.1:8000/"), false);
});

test("unclosed fence and malformed table do not throw", () => {
  const blocks = parseMarkdown("```js\nconst x = 1;");
  assert.equal(blocks[0].type, "code");
  assert.equal(blocks[0].text, "const x = 1;");
  const html = render("| 不是表格\n| 这也不是 |\n普通段落");
  assert.match(html, /<p>/);
});

test("parse basic math AST", () => {
  const ast = parseMath("x^2+\\frac{1}{2}");
  assert.equal(ast.type, "row");
  assert.ok(ast.nodes.some((node) => node.type === "scripts"));
  assert.ok(ast.nodes.some((node) => node.type === "frac"));
});

test("render screenshot-style LaTeX without raw commands or brackets", () => {
  const source = String.raw`计算结果

1. 算术式

\[
(18\times7+4)/2 = (126+4)/2 = 130/2 = \mathbf{65}
\]

2. 导数

\[
f(x)=x^2+\sin(x) \quad\Longrightarrow\quad f'(x)=2x+\cos(x)
\]

在 \(x=3\) 处：

\[
f'(3)=2\cdot3+\cos(3)=6-0.98999\ldots\approx \mathbf{5.0100}
\]

注意：此处的角用弧度制，\(\cos(3)\approx -0.98999\)。`;
  const html = render(source);
  assert.match(html, /class="math-surface math-display"/);
  assert.match(html, /class="math-surface math-inline"/);
  assert.match(html, /<sup>2<\/sup>/);
  assert.match(html, /<strong class="math-bold">65<\/strong>/);
  assert.match(html, /<strong class="math-bold">5.0100<\/strong>/);
  assert.match(html, /×/);
  assert.match(html, /⟹/);
  assert.match(html, /·/);
  assert.match(html, /…/);
  assert.match(html, /≈/);
  assert.match(html, /<span class="math-fn">cos<\/span>/);
  assert.ok(!html.includes("\\["));
  assert.ok(!html.includes("\\]"));
  assert.ok(!html.includes("\\("));
  assert.ok(!html.includes("\\)"));
  assert.ok(!html.includes("\\times"));
  assert.ok(!html.includes("\\quad"));
  assert.ok(!html.includes("\\mathbf"));
  assert.ok(!html.includes("\\Longrightarrow"));
  assert.ok(!html.includes("\\cdot"));
  assert.ok(!html.includes("\\ldots"));
  assert.ok(!html.includes("\\approx"));
  assert.ok(!html.includes("\\cos"));
});

test("render fractions, roots, scripts, and aligned environments", () => {
  const source = String.raw`行内 \(\frac{1}{2}+\sqrt{x}+x_i^2\)

\[
\begin{aligned}
a &= b \\
c &= d
\end{aligned}
\]`;
  const html = render(source);
  assert.match(html, /class="math-frac"/);
  assert.match(html, /class="math-sqrt"/);
  assert.match(html, /<sup>2<\/sup>/);
  assert.match(html, /<sub>i<\/sub>/);
  assert.match(html, /class="math-array math-aligned"/);
  assert.match(html, /class="math-row"/);
});

test("code spans stay literal and currency is not math", () => {
  const html = render("`$x$` 和 $y$ 以及 $100");
  assert.match(html, /<code>\$x\$<\/code>/);
  assert.match(html, /class="math-surface math-inline">y<\/span>/);
  assert.ok(!html.includes('math-inline">100'));
});

test("display math delimiters and code fences stay separated", () => {
  const html = render("\\[x^2\\]\n\n$$\\frac{1}{2}$$\n\n```\n$x^2$\n```");
  assert.equal((html.match(/math-display/g) || []).length, 2);
  assert.match(html, /<pre><code>\$x\^2\$<\/code><\/pre>/);
});

test("sqrt index and text mode", () => {
  const html = render(String.raw`行内 \(\sqrt[3]{x}+\text{速度}\)`);
  assert.match(html, /class="math-sqrt-index"/);
  assert.match(html, /class="math-text">速度<\/span>/);
});

test("math HTML is text and unclosed delimiters do not throw", () => {
  const html = render(String.raw`\(<script>alert(1)</script>\)`);
  assert.ok(!html.includes("<script"));
  assert.match(html, /&lt;/);
  assert.ok(html.includes("script"));
  assert.doesNotThrow(() => render("\\[\nx^2"));
});
