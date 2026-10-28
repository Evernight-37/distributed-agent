const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(
  path.join(__dirname, "../assets/web/index.html"),
  "utf8",
);

assert.match(html, /浏览器工作台已经停用/);
assert.match(html, /Agent Harness 桌面应用/);
assert.match(html, /Codex/);
assert.ok(!/<form\b/i.test(html), "retired Hub page has no credential form");
assert.ok(!/password/i.test(html), "retired Hub page asks for no password");
assert.ok(
  !/<script\b/i.test(html),
  "retired Hub page executes no browser application code",
);

const account = fs.readFileSync(
  path.join(__dirname, "../assets/web/app/account-settings.js"),
  "utf8",
);
assert.match(account, /添加 Linux 服务器/);
assert.match(account, /linux-setup-prompt/);
assert.match(account, /复制给服务器 Codex/);
assert.match(account, /startsWith\("https:\/\/"\)/);

console.log("UI retirement page checks passed");
