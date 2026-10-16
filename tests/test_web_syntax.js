// Parse every inline application script. This catches malformed bundled UI
// even when a behavior test exercises only one of the browser surfaces.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const webRoot = path.join(__dirname, "../assets/web");
for (const name of fs
  .readdirSync(webRoot)
  .filter((name) => name.endsWith(".html"))) {
  const html = fs.readFileSync(path.join(webRoot, name), "utf8");
  const scripts = [
    ...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g),
  ];
  for (const [index, match] of scripts.entries()) {
    new vm.Script(match[1], { filename: `${name}:inline-${index + 1}` });
  }
}

console.log("web syntax tests passed");
