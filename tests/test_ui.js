const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(
  path.join(__dirname, "../assets/web/index.html"),
  "utf8",
);

assert.match(html, /浏览器工作台已经停用/);
assert.match(html, /Foolship 桌面应用/);
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
assert.match(account, /在另一台电脑上安装/);
assert.match(account, /ai-setup-prompt/);
assert.match(account, /复制给那台机器的 AI/);
for (const platform of ["linux", "windows", "macos"])
  assert.match(account, new RegExp(`\\["${platform}",`));
assert.match(account, /startsWith\("https:\/\/"\)/);

console.log("UI retirement page checks passed");
