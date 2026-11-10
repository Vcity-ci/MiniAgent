// No-dependency LaTeX math renderer.
// Markdown owns the document; this module only renders protected math
// segments into transparent DOM surfaces. It never parses HTML or Markdown.

const MAX_TEX_LENGTH = 6000;
const MAX_NODES = 3000;

const SYMBOLS = {
  times: "×",
  cdot: "·",
  div: "÷",
  pm: "±",
  mp: "∓",
  approx: "≈",
  neq: "≠",
  leq: "≤",
  le: "≤",
  geq: "≥",
  ge: "≥",
  lt: "<",
  gt: ">",
  infty: "∞",
  pi: "π",
  alpha: "α",
  beta: "β",
  gamma: "γ",
  delta: "δ",
  Delta: "Δ",
  epsilon: "ε",
  varepsilon: "ɛ",
  zeta: "ζ",
  eta: "η",
  theta: "θ",
  lambda: "λ",
  mu: "μ",
  nu: "ν",
  xi: "ξ",
  rho: "ρ",
  sigma: "σ",
  tau: "τ",
  phi: "ϕ",
  varphi: "φ",
  chi: "χ",
  psi: "ψ",
  omega: "ω",
  Gamma: "Γ",
  Theta: "Θ",
  Lambda: "Λ",
  Xi: "Ξ",
  Pi: "Π",
  Sigma: "Σ",
  Phi: "Φ",
  Psi: "Ψ",
  Omega: "Ω",
  sum: "∑",
  prod: "∏",
  int: "∫",
  oint: "∮",
  partial: "∂",
  nabla: "∇",
  ldots: "…",
  dots: "…",
  cdots: "⋯",
  vdots: "⋮",
  ddots: "⋱",
  rightarrow: "→",
  to: "→",
  leftarrow: "←",
  Rightarrow: "⇒",
  Longrightarrow: "⟹",
  Leftarrow: "⇐",
  leftrightarrow: "↔",
  Leftrightarrow: "⇔",
  mapsto: "↦",
  in: "∈",
  notin: "∉",
  subset: "⊂",
  subseteq: "⊆",
  supset: "⊃",
  supseteq: "⊇",
  cup: "∪",
  cap: "∩",
  emptyset: "∅",
  forall: "∀",
  exists: "∃",
  perp: "⊥",
  parallel: "∥",
  angle: "∠",
  degree: "°",
  circ: "∘",
  equiv: "≡",
  sim: "∼",
  propto: "∝",
  therefore: "∴",
  because: "∵",
  ldots_: "…",
};

const FUNCTIONS = new Set([
  "sin", "cos", "tan", "cot", "sec", "csc",
  "arcsin", "arccos", "arctan",
  "log", "ln", "exp", "lim", "max", "min", "sup", "inf",
  "det", "gcd", "deg", "dim", "ker", "Pr",
]);

const SPACES = {
  ",": "\u2009",
  ":": "\u2005",
  ";": "\u2005",
  "!": "",
  " ": " ",
  quad: "\u2003",
  qquad: "\u2003\u2003",
};

const STYLE_COMMANDS = {
  mathbf: "bold",
  mathit: "italic",
  mathrm: "roman",
  mathsf: "sans",
  mathtt: "mono",
  mathcal: "cal",
  mathbb: "bb",
  mathfrak: "frak",
};

const ACCENTS = new Set(["overline", "underline", "vec", "hat", "bar", "tilde", "dot", "ddot"]);

const ENVIRONMENTS = new Set([
  "aligned",
  "align",
  "cases",
  "matrix",
  "pmatrix",
  "bmatrix",
  "vmatrix",
  "Vmatrix",
  "array",
]);

const SKIP_COMMANDS = new Set([
  "left", "right", "middle",
  "big", "Big", "bigg", "Bigg",
  "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr",
  "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
  "limits", "nolimits", "nonumber", "notag",
]);

function addClass(node, name) {
  if (!name) return;
  if (node.classList) node.classList.add(name);
  else node.className = `${node.className || ""} ${name}`.trim();
}

function isLetter(ch) {
  return /[A-Za-z]/.test(ch);
}

function isOperator(ch) {
  return "=+-<>".includes(ch);
}

function appendText(parent, value, doc) {
  if (!value) return;
  parent.appendChild(doc.createTextNode(String(value)));
}

function skipSpaces(state) {
  while (state.index < state.text.length && /\s/.test(state.text[state.index])) {
    state.index += 1;
  }
}

function readCommandName(state) {
  state.index += 1; // backslash
  if (state.index >= state.text.length) return "\\";
  if (!isLetter(state.text[state.index])) {
    const command = state.text[state.index];
    state.index += 1;
    return command;
  }
  const start = state.index;
  while (state.index < state.text.length && isLetter(state.text[state.index])) {
    state.index += 1;
  }
  if (state.text[state.index] === "*") state.index += 1;
  return state.text.slice(start, state.index);
}

function readRawGroup(state) {
  skipSpaces(state);
  if (state.text[state.index] !== "{") {
    if (state.index >= state.text.length) return "";
    const char = state.text[state.index];
    state.index += 1;
    return char;
  }
  state.index += 1;
  let depth = 1;
  let value = "";
  while (state.index < state.text.length) {
    const ch = state.text[state.index];
    if (ch === "\\" && state.index + 1 < state.text.length) {
      value += ch + state.text[state.index + 1];
      state.index += 2;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      value += ch;
      state.index += 1;
      continue;
    }
    if (ch === "}") {
      depth -= 1;
      state.index += 1;
      if (depth === 0) return value;
      value += ch;
      continue;
    }
    value += ch;
    state.index += 1;
  }
  return value;
}

function readOptionalBracket(state) {
  skipSpaces(state);
  if (state.text[state.index] !== "[") return null;
  state.index += 1;
  let depth = 1;
  let value = "";
  while (state.index < state.text.length) {
    const ch = state.text[state.index];
    if (ch === "[") depth += 1;
    if (ch === "]") {
      depth -= 1;
      state.index += 1;
      if (depth === 0) return value;
      value += ch;
      continue;
    }
    value += ch;
    state.index += 1;
  }
  return value;
}

function parseSequence(state, stop = null) {
  const nodes = [];
  if (state.depth > 32) {
    const rest = state.text.slice(state.index);
    state.index = state.text.length;
    if (rest) nodes.push({ type: "text", value: rest });
    return nodes;
  }
  state.depth += 1;
  while (state.index < state.text.length) {
    if (state.count > MAX_NODES) {
      nodes.push({ type: "text", value: state.text.slice(state.index) });
      state.index = state.text.length;
      break;
    }
    const ch = state.text[state.index];
    if (stop && ch === stop) {
      state.index += 1;
      break;
    }
    if (ch === "}") {
      state.index += 1;
      break;
    }
    if (/\s/.test(ch)) {
      state.index += 1;
      continue;
    }
    if (ch === "~") {
      state.index += 1;
      state.count += 1;
      nodes.push({ type: "space", value: "\u00a0" });
      continue;
    }
    if (ch === "^" || ch === "_") {
      state.index += 1;
      const argument = parseScriptArgument(state);
      attachScript(nodes, ch === "^" ? "sup" : "sub", argument);
      continue;
    }
    if (ch === "&") {
      state.index += 1;
      state.count += 1;
      nodes.push({ type: "space", value: "\u2003" });
      continue;
    }
    if (ch === "\\") {
      if (state.text[state.index + 1] === "\\") {
        state.index += 2;
        state.count += 1;
        nodes.push({ type: "linebreak" });
        continue;
      }
      const command = parseCommand(state);
      if (command) {
        state.count += 1;
        nodes.push(command);
      }
      continue;
    }
    if (ch === "{") {
      state.index += 1;
      const group = parseSequence(state, "}");
      nodes.push({ type: "row", nodes: group });
      continue;
    }
    state.index += 1;
    state.count += 1;
    if (isOperator(ch)) nodes.push({ type: "text", value: ch, op: true });
    else nodes.push({ type: "text", value: ch });
  }
  state.depth -= 1;
  return nodes;
}

function parseScriptArgument(state) {
  skipSpaces(state);
  if (state.index >= state.text.length) return [];
  if (state.text[state.index] === "{") {
    state.index += 1;
    return parseSequence(state, "}");
  }
  if (state.text[state.index] === "\\") {
    const command = parseCommand(state);
    return command ? [command] : [];
  }
  const ch = state.text[state.index];
  state.index += 1;
  return [{ type: "text", value: ch, op: false }];
}

function attachScript(nodes, kind, argument) {
  const base = nodes.length ? nodes.pop() : { type: "text", value: "" };
  if (base.type === "scripts") {
    base[kind] = argument;
    nodes.push(base);
    return;
  }
  nodes.push({
    type: "scripts",
    base,
    sup: kind === "sup" ? argument : null,
    sub: kind === "sub" ? argument : null,
  });
}

function readArgument(state) {
  skipSpaces(state);
  if (state.index >= state.text.length) return [];
  if (state.text[state.index] === "{") {
    state.index += 1;
    return parseSequence(state, "}");
  }
  if (state.text[state.index] === "\\") {
    const command = parseCommand(state);
    return command ? [command] : [];
  }
  const ch = state.text[state.index];
  state.index += 1;
  return [{ type: "text", value: ch, op: false }];
}

function parseCommand(state) {
  const command = readCommandName(state);

  if (command === "\\") return { type: "linebreak" };
  if (command === "{" || command === "}" || command === "%" ||
      command === "_" || command === "&" || command === "$" || command === "#") {
    return { type: "text", value: command };
  }
  if (Object.prototype.hasOwnProperty.call(SPACES, command)) {
    return { type: "space", value: SPACES[command] };
  }
  if (SKIP_COMMANDS.has(command)) return null;
  if (command === "text" || command === "operatorname" || command === "mbox") {
    return { type: "textbox", value: readRawGroup(state), kind: command };
  }
  if (command === "color") {
    readRawGroup(state);
    return null;
  }
  if (command === "frac" || command === "dfrac" || command === "tfrac") {
    const numerator = readArgument(state);
    const denominator = readArgument(state);
    return { type: "frac", numerator, denominator };
  }
  if (command === "sqrt") {
    const index = readOptionalBracket(state);
    const body = readArgument(state);
    return { type: "sqrt", index: index === null ? null : parseMath(index).nodes, body };
  }
  if (Object.prototype.hasOwnProperty.call(STYLE_COMMANDS, command)) {
    return { type: "style", style: STYLE_COMMANDS[command], body: readArgument(state) };
  }
  if (ACCENTS.has(command)) {
    return { type: "accent", accent: command, body: readArgument(state) };
  }
  if (command === "begin" || command === "end") {
    readRawGroup(state);
    return { type: "text", value: command };
  }
  if (FUNCTIONS.has(command)) {
    return { type: "function", name: command };
  }
  if (Object.prototype.hasOwnProperty.call(SYMBOLS, command)) {
    return { type: "text", value: SYMBOLS[command], op: ["times", "cdot", "div", "pm", "mp", "approx", "neq", "leq", "geq", "le", "ge"].includes(command) };
  }
  return { type: "text", value: command };
}

function parseEnvironment(source) {
  const match = source.match(/^\\begin\{([A-Za-z*]+)\}([\s\S]*)\\end\{\1\}$/);
  if (!match || !ENVIRONMENTS.has(match[1])) return null;
  const environment = match[1];
  let body = match[2];
  if (environment === "array") {
    body = body.replace(/^\s*\{[^}]*\}/, "");
  }
  const rows = body.split(/\\\\/).map((row) =>
    row.split("&").map((cell) => parseMath(cell))
  );
  return { type: "array", environment, rows };
}

export function parseMath(source) {
  const text = String(source ?? "").replace(/\r\n?/g, "\n").trim();
  if (!text) return { type: "row", nodes: [] };
  const environment = parseEnvironment(text);
  if (environment) return environment;
  const state = { text, index: 0, count: 0, depth: 0 };
  return { type: "row", nodes: parseSequence(state) };
}

function appendNodes(parent, nodes, doc) {
  for (const node of nodes) appendNode(parent, node, doc);
}

function appendBaseline(parent, nodes, doc) {
  const wrap = doc.createElement("span");
  wrap.className = "math-baseline";
  appendNodes(wrap, nodes, doc);
  parent.appendChild(wrap);
}

function appendNode(parent, node, doc) {
  if (!node) return;
  if (node.type === "row") {
    appendNodes(parent, node.nodes, doc);
  } else if (node.type === "text") {
    if (node.op) {
      const span = doc.createElement("span");
      span.className = "math-op";
      span.textContent = node.value;
      parent.appendChild(span);
    } else {
      appendText(parent, node.value, doc);
    }
  } else if (node.type === "function") {
    const span = doc.createElement("span");
    span.className = "math-fn";
    span.textContent = node.name;
    parent.appendChild(span);
  } else if (node.type === "space") {
    const span = doc.createElement("span");
    span.className = "math-space";
    span.textContent = node.value;
    parent.appendChild(span);
  } else if (node.type === "textbox") {
    const span = doc.createElement("span");
    span.className = "math-text";
    span.textContent = node.value;
    parent.appendChild(span);
  } else if (node.type === "linebreak") {
    parent.appendChild(doc.createElement("br"));
  } else if (node.type === "scripts") {
    const wrap = doc.createElement("span");
    wrap.className = "math-scripts";
    appendBaseline(wrap, [node.base], doc);
    if (node.sup) {
      const sup = doc.createElement("sup");
      appendNodes(sup, node.sup, doc);
      wrap.appendChild(sup);
    }
    if (node.sub) {
      const sub = doc.createElement("sub");
      appendNodes(sub, node.sub, doc);
      wrap.appendChild(sub);
    }
    parent.appendChild(wrap);
  } else if (node.type === "frac") {
    const frac = doc.createElement("span");
    frac.className = "math-frac";
    const numerator = doc.createElement("span");
    numerator.className = "math-num";
    appendNodes(numerator, node.numerator, doc);
    const denominator = doc.createElement("span");
    denominator.className = "math-den";
    appendNodes(denominator, node.denominator, doc);
    frac.appendChild(numerator);
    frac.appendChild(denominator);
    parent.appendChild(frac);
  } else if (node.type === "sqrt") {
    const sqrt = doc.createElement("span");
    sqrt.className = "math-sqrt";
    const radical = doc.createElement("span");
    radical.className = "math-radical";
    radical.textContent = "√";
    sqrt.appendChild(radical);
    if (node.index && node.index.length) {
      const index = doc.createElement("span");
      index.className = "math-sqrt-index";
      appendNodes(index, node.index, doc);
      sqrt.appendChild(index);
    }
    const body = doc.createElement("span");
    body.className = "math-radicand";
    appendNodes(body, node.body, doc);
    sqrt.appendChild(body);
    parent.appendChild(sqrt);
  } else if (node.type === "style") {
    let container;
    if (node.style === "bold") container = doc.createElement("strong");
    else if (node.style === "italic") container = doc.createElement("em");
    else container = doc.createElement("span");
    addClass(container, `math-${node.style}`);
    appendNodes(container, node.body, doc);
    parent.appendChild(container);
  } else if (node.type === "accent") {
    const container = doc.createElement("span");
    container.className = `math-${node.accent}`;
    appendNodes(container, node.body, doc);
    parent.appendChild(container);
  } else if (node.type === "array") {
    const array = doc.createElement("span");
    array.className = `math-array math-${node.environment}`;
    for (const row of node.rows) {
      const rowNode = doc.createElement("span");
      rowNode.className = "math-row";
      for (const cell of row) {
        const cellNode = doc.createElement("span");
        cellNode.className = "math-cell";
        appendNodes(cellNode, cell.nodes, doc);
        rowNode.appendChild(cellNode);
      }
      array.appendChild(rowNode);
    }
    parent.appendChild(array);
  }
}

function rawFallback(container, text, doc) {
  const fallback = doc.createElement("span");
  fallback.className = "math-raw";
  fallback.textContent = text;
  container.appendChild(fallback);
}

export function renderMathInto(container, source, options = {}) {
  const doc = options.document || container.ownerDocument || document;
  container.textContent = "";
  addClass(container, "math-surface");
  addClass(container, options.display ? "math-display" : "math-inline");
  const text = String(source ?? "").trim();
  if (!text) return;
  if (text.length > MAX_TEX_LENGTH) {
    rawFallback(container, text, doc);
    return;
  }
  try {
    appendNode(container, parseMath(text), doc);
  } catch (_err) {
    rawFallback(container, text, doc);
  }
}
