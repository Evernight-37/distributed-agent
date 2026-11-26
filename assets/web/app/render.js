/* Markdown, math, links and code blocks for chat messages.
 *
 * Rendering user- and agent-authored text is the one place in this UI that
 * produces HTML, so the pipeline is fixed: mask code fences, extract math,
 * render markdown, sanitize with DOMPurify, insert, then restore math with
 * KaTeX. If a library is missing the message falls back to plain text.
 */
const FoolshipRender = (() => {
  const MATH = (index) => "%%FSMATH" + index + "%%";
  const MATH_RE = /%%FSMATH(\d+)%%/g;
  const CODE = (index) => "%%FSCODE" + index + "%%";
  const CODE_RE = /%%FSCODE(\d+)%%/g;
  const FENCE = /```[\s\S]*?```/g;

  function maskCode(text) {
    const kept = [];
    const masked = text.replace(FENCE, (block) => {
      kept.push(block);
      return CODE(kept.length - 1);
    });
    return { masked, kept };
  }

  function unmaskCode(text, kept) {
    return text.replace(CODE_RE, (_, index) => kept[Number(index)] ?? "");
  }

  /* Split text into plain and math segments.

     Block math is `$$…$$` or `\[…\]`; inline math is `\(…\)` or `$…$`. An opening
     `$` must be followed by a non-space and the closing one preceded by one, so
     "costs $5 and $6" stays prose.
   */
  function segments(text) {
    const out = [];
    let index = 0;
    const pattern =
      /(\$\$[\s\S]+?\$\$)|(\\\[[\s\S]+?\\\])|(\\\([\s\S]+?\\\))|(\$(?!\s)(?:[^$\n\\]|\\.)*?[^\s$\\]\$)/g;
    for (const match of text.matchAll(pattern)) {
      if (match.index > index)
        out.push({ text: text.slice(index, match.index) });
      const raw = match[0];
      const block = raw.startsWith("$$") || raw.startsWith("\\[");
      // "$$", "\\[" and "\\(" use two-character delimiters; "$" uses one.
      const edge = raw.startsWith("$") && !raw.startsWith("$$") ? 1 : 2;
      out.push({ math: raw.slice(edge, -edge), display: block, raw });
      index = match.index + raw.length;
    }
    if (index < text.length) out.push({ text: text.slice(index) });
    return out;
  }

  function safeHref(raw) {
    try {
      const url = new URL(raw, window.location.href);
      return ["http:", "https:", "mailto:"].includes(url.protocol)
        ? url.href
        : null;
    } catch {
      return null;
    }
  }

  function decorate(root) {
    root.querySelectorAll("a[href]").forEach((anchor) => {
      const href = safeHref(anchor.getAttribute("href"));
      if (!href) {
        anchor.replaceWith(document.createTextNode(anchor.textContent || ""));
        return;
      }
      anchor.setAttribute("href", href);
      anchor.setAttribute("target", "_blank");
      anchor.setAttribute("rel", "noopener noreferrer");
    });
    root.querySelectorAll("pre > code").forEach((block) => {
      const panel = document.createElement("div");
      panel.className = "code-panel";
      const language = /language-([\w+-]+)/.exec(block.className || "");
      const label = document.createElement("span");
      label.className = "code-label";
      label.textContent = language ? language[1] : t("代码", "Code");
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "code-copy";
      copy.textContent = t("复制", "Copy");
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(block.textContent || "");
          copy.textContent = t("已复制", "Copied");
          setTimeout(() => {
            copy.textContent = t("复制", "Copy");
          }, 1200);
        } catch {
          copy.textContent = t("复制失败", "Copy failed");
        }
      });
      panel.append(label, copy);
      block.parentElement.before(panel);
    });
  }

  function renderMath(root, math) {
    if (typeof katex === "undefined" || !math.length) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const targets = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (parent && parent.closest("code, pre")) continue;
      if (node.nodeValue && node.nodeValue.includes("%%FSMATH"))
        targets.push(node);
    }
    for (const node of targets) {
      const parts = [];
      let last = 0;
      for (const match of node.nodeValue.matchAll(MATH_RE)) {
        if (match.index > last)
          parts.push(
            document.createTextNode(node.nodeValue.slice(last, match.index)),
          );
        const spec = math[Number(match[1])];
        if (!spec) continue;
        const span = document.createElement("span");
        span.className = spec.display ? "math math-block" : "math";
        try {
          katex.render(spec.math, span, {
            throwOnError: false,
            displayMode: spec.display,
          });
        } catch {
          span.textContent = spec.raw;
        }
        parts.push(span);
        last = match.index + match[0].length;
      }
      if (last < node.nodeValue.length)
        parts.push(document.createTextNode(node.nodeValue.slice(last)));
      node.replaceWith(...parts);
    }
  }

  function render(container, text) {
    const value = String(text ?? "");
    container.classList.add("message-body");
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
      container.textContent = value;
      return;
    }
    const { masked, kept } = maskCode(value);
    const math = [];
    let staged = "";
    for (const part of segments(masked)) {
      if (part.text !== undefined) {
        staged += part.text;
      } else {
        math.push(part);
        staged += MATH(math.length - 1);
      }
    }
    const html = marked.parse(unmaskCode(staged, kept), {
      gfm: true,
      breaks: true,
    });
    const clean = DOMPurify.sanitize(html, {
      FORBID_TAGS: ["style", "iframe", "object", "embed", "form"],
      FORBID_ATTR: ["style"],
    });
    const holder = document.createElement("div");
    holder.innerHTML = clean;
    decorate(holder);
    container.textContent = "";
    container.append(...holder.childNodes);
    renderMath(container, math);
  }

  return { render, segments };
})();
