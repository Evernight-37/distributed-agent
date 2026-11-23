/* Structural checks for the message renderer: the libraries are vendored, the
 * shell loads them, and the math splitter keeps prose like "costs $5" intact. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

for (const [file, minimum] of [
  ["assets/web/vendor/marked.min.js", 20000],
  ["assets/web/vendor/purify.min.js", 10000],
  ["assets/web/vendor/katex/katex.min.js", 200000],
  ["assets/web/vendor/katex/katex.min.css", 10000],
]) {
  const size = fs.statSync(path.join(root, file)).size;
  assert.ok(size >= minimum, file + " is only " + size + " bytes");
}

const shell = read("assets/web/app.html");
for (const tag of [
  "vendor/marked.min.js",
  "vendor/purify.min.js",
  "vendor/katex/katex.min.js",
  "vendor/katex/katex.min.css",
  "app/render.js",
]) {
  assert.ok(shell.includes(tag), "the shell does not load " + tag);
}
assert.ok(
  shell.indexOf("vendor/marked.min.js") < shell.indexOf("app/render.js"),
  "vendored libraries must load before the renderer",
);

const source = read("assets/web/app/render.js");
assert.ok(
  source.includes("DOMPurify.sanitize"),
  "markdown output must be sanitized",
);
assert.ok(
  source.includes("noopener"),
  "links must be opened without a window reference",
);
assert.ok(
  source.includes('"http:", "https:", "mailto:"'),
  "only web and mail links may survive",
);

const factory = new Function(
  "window",
  "document",
  source + "\nreturn FoolshipRender;",
);
const renderer = factory({ location: { href: "http://127.0.0.1/" } }, {});
const { segments } = renderer;

const inline = segments("先验是 $p(\\theta)$ 这样");
assert.deepStrictEqual(
  inline.map((part) => (part.math === undefined ? "text" : "math")),
  ["text", "math", "text"],
);
assert.strictEqual(inline[1].math, "p(\\theta)");
assert.strictEqual(inline[1].display, false);

assert.deepStrictEqual(
  segments("costs $5 and $6 today").filter((part) => part.math),
  [],
);
assert.deepStrictEqual(
  segments("价格是 $500 元").filter((part) => part.math),
  [],
);

const block = segments("$$\\frac{a}{b}$$");
assert.strictEqual(block[0].display, true);
assert.strictEqual(block[0].math, "\\frac{a}{b}");

const bracketed = segments("\\[E = mc^2\\]");
assert.strictEqual(bracketed[0].display, true);

const parens = segments("行内 \\(x^2\\) 结束");
assert.strictEqual(parens[1].math, "x^2");
assert.strictEqual(parens[1].display, false);

console.log("web renderer checks passed");
