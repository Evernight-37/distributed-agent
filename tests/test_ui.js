const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

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

const project = fs.readFileSync(
  path.join(__dirname, "../assets/web/app/project.js"),
  "utf8",
);
const dashboard = fs.readFileSync(
  path.join(__dirname, "../assets/web/app/dashboard.js"),
  "utf8",
);
assert.doesNotMatch(
  dashboard + project,
  /"employee-(?:create|configure|dispatch)"/,
);

async function checkProjectAgentForms() {
  const server = "https://team.example";
  const view = {
    project: { id: "project-a" },
    server,
    workspaces: [],
    members: [],
  };
  const state = {
    project: "project-a",
    server,
    view,
    dash: {
      user: { id: "operator" },
      devices: [
        { node: "worker-a", name: "设备 A", server, last_seen: 1 },
        { node: "not-paired", name: "未接入", server },
        {
          node: "other-service",
          name: "其他服务",
          server: "https://else.example",
          last_seen: 1,
        },
      ],
    },
  };
  function element(tag, _style, text) {
    return {
      tag,
      text,
      children: [],
      append(...nodes) {
        this.children.push(...nodes);
      },
      replaceChildren(...nodes) {
        this.children = nodes;
      },
    };
  }
  let actionBar;
  let modalBody;
  let fields;
  const calls = [];
  const context = vm.createContext({
    S: state,
    FoolshipDesktop: { render: {}, components: {}, events: {} },
    el: element,
    button: (label, click) => ({ label, click }),
    field: (parent, label, value, options) => {
      const input = { value, options };
      fields.set(label, input);
      parent.append(input);
      return input;
    },
    modal: (_title, render) => {
      actionBar = element("footer");
      modalBody = element("main");
      fields = new Map();
      render(modalBody, actionBar);
    },
    closeModal() {},
    note() {},
    notify() {},
    deviceName: () => "设备 A",
    refresh: async () => {},
    api: async (action, payload) => {
      calls.push([action, JSON.parse(JSON.stringify(payload))]);
      if (action === "workspace-configure") return { pending: true };
      if (action === "project")
        return { ...view, workspaces: [{ node: "worker-a", status: "ready" }] };
      return {};
    },
  });
  vm.runInContext(project, context);
  function findIn(node, label) {
    if (node.label === label) return node;
    for (const child of node.children || []) {
      const found = findIn(child, label);
      if (found) return found;
    }
  }
  const findButton = (label) =>
    actionBar.children.find((item) => item.label === label);
  await context.openProjectAgentForm();
  const machine = fields.get("执行机器");
  assert.deepEqual(
    Array.from(machine.options, ([node]) => node),
    ["", "worker-a"],
  );
  machine.value = "worker-a";
  machine.onchange();
  fields.get("Agent 名称").value = "项目审阅员";
  fields.get("模型 ID").value = "deepseek-flash";
  const create = findButton("创建 Agent");
  assert.equal(create.disabled, true);
  await assert.rejects(create.click(), /工作目录/);
  assert.deepEqual(calls, []);
  await findIn(modalBody, "使用默认目录").click();
  assert.deepEqual(calls[0], [
    "workspace-configure",
    { project: "project-a", node: "worker-a", server },
  ]);
  assert.equal(create.disabled, true);
  await findIn(modalBody, "检查配置状态").click();
  assert.deepEqual(calls[1], ["project", { id: "project-a" }]);
  assert.equal(create.disabled, false);
  await findButton("创建 Agent").click();
  assert.deepEqual(calls[2], [
    "project-agent-create",
    {
      project: "project-a",
      node: "worker-a",
      name: "项目审阅员",
      model: "deepseek-flash",
      effort: "medium",
      enabled: true,
      stages: ["discuss", "review"],
      timeout: 600,
      strict_worktree: false,
      attachment_policy: "ask",
      network: { mode: "machine" },
    },
  ]);

  const agent = {
    id: "agent-a",
    name: "项目审阅员",
    node: "worker-a",
    project_agent_settings: {
      model: "project-model",
      stages: ["review"],
      timeout: 450,
    },
  };
  await context.openProjectAgentForm(agent);
  assert.equal(fields.has("执行机器"), false);
  assert.equal(fields.get("模型 ID").value, "project-model");
  fields.get("接收项目任务").value = "false";
  await findButton("保存设置").click();
  assert.equal(calls[3][0], "project-agent-configure");
  assert.equal(calls[3][1].agent_id, "agent-a");
  assert.equal(calls[3][1].enabled, false);
  assert.deepEqual(calls[3][1].stages, ["review"]);

  context.confirmPermissionRequest(
    "project-a",
    agent,
    { id: "grant-one", reason: "核对工作区外日志" },
    true,
  );
  assert.equal(calls.length, 4);
  await findButton("确认批准").click();
  assert.deepEqual(calls[4], [
    "permission-decide",
    { project: "project-a", request_id: "grant-one", approve: true },
  ]);
}

checkProjectAgentForms().then(
  () => console.log("UI retirement and project Agent checks passed"),
  (error) => {
    console.error(error);
    process.exitCode = 1;
  },
);
