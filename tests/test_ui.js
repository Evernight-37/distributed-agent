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
  assert.match(account, new RegExp(`\\[\\s*"${platform}"\\s*,`));
assert.match(account, /startsWith\("https:\/\/"\)/);

async function checkInterfaceLanguage() {
  const navLabel = {
    textContent: "设置",
    dataset: { en: "Settings", enTitle: "Settings", enLabel: "Settings" },
    attributes: { title: "设置", "aria-label": "设置" },
    getAttribute(name) {
      return this.attributes[name];
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };
  const document = {
    documentElement: {},
    querySelectorAll: () => [navLabel],
  };
  const navigator = { language: "zh-HK" };
  const context = vm.createContext({
    document,
    navigator,
    api: async (action, { preference }) => {
      assert.equal(action, "set-interface-language");
      return { preference, system_language: "zh-HK" };
    },
  });
  for (const script of ["i18n", "desktop-messages"]) {
    vm.runInContext(
      fs.readFileSync(
        path.join(__dirname, `../assets/web/app/${script}.js`),
        "utf8",
      ),
      context,
    );
  }
  context.applyInterfaceLanguage({
    preference: "system",
    system_language: "en-GB",
  });
  assert.equal(document.documentElement.lang, "en");
  assert.equal(navLabel.textContent, "Settings");
  assert.equal(navLabel.attributes["aria-label"], "Settings");
  assert.equal(context.t("首页", "Home"), "Home");
  assert.equal(context.localizeDesktopText("请先登录"), "Sign in first.");
  assert.equal(context.localizeDesktopText("原始日志"), "原始日志");
  await context.setInterfaceLanguage("zh-CN");
  assert.equal(context.interfaceLanguagePreference(), "zh-CN");
  assert.equal(document.documentElement.lang, "zh-CN");
  assert.equal(navLabel.textContent, "设置");
  assert.equal(navLabel.attributes.title, "设置");
  await context.setInterfaceLanguage("en");
  assert.equal(context.interfaceLocale(), "en");
  await context.setInterfaceLanguage("system");
  assert.equal(context.interfaceLocale(), "zh-CN");
  navigator.language = "en";
  context.applyInterfaceLanguage({ preference: "system" });
  assert.equal(context.interfaceLocale(), "en");
}

const project = fs.readFileSync(
  path.join(__dirname, "../assets/web/app/project.js"),
  "utf8",
);
const projectPanels = fs.readFileSync(
  path.join(__dirname, "../assets/web/app/project-panels.js"),
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
      setAttribute(name, value) {
        this[name] = value;
      },
    };
  }
  let actionBar;
  let modalBody;
  let fields;
  const calls = [];
  let failWorkOnce = false;
  const copied = [];
  const jumped = [];
  const context = vm.createContext({
    S: state,
    t: (chinese) => chinese,
    interfaceLocale: () => "zh-CN",
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
    copyText: async (value) => copied.push(value),
    requestAnimationFrame: (fn) => fn(),
    document: {
      getElementById: (id) => ({
        scrollIntoView: () => jumped.push(id),
        focus() {},
      }),
    },
    note() {},
    notify() {},
    deviceName: () => "设备 A",
    refresh: async () => {},
    api: async (action, payload) => {
      calls.push([action, JSON.parse(JSON.stringify(payload))]);
      if (action === "project-work" && failWorkOnce) {
        failWorkOnce = false;
        throw new Error("temporary failure");
      }
      if (action === "workspace-configure") return { pending: true };
      if (action === "project")
        return { ...view, workspaces: [{ node: "worker-a", status: "ready" }] };
      return {};
    },
  });
  vm.runInContext(projectPanels, context);
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
  const proxyMode = fields.get("模型请求代理");
  const proxyPort = fields.get("代理端口");
  assert.equal(proxyPort.disabled, true);
  assert.match(proxyPort.placeholder, /默认设置/);
  proxyMode.value = "custom";
  proxyMode.onchange();
  assert.equal(proxyPort.disabled, false);
  proxyPort.value = "7897";
  proxyMode.value = "direct";
  proxyMode.onchange();
  assert.equal(proxyPort.disabled, true);
  assert.equal(proxyPort.value, "");
  assert.match(proxyPort.placeholder, /直连/);
  proxyMode.value = "custom";
  proxyMode.onchange();
  assert.equal(proxyPort.value, "7897");
  proxyMode.value = "machine";
  proxyMode.onchange();
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

  context.editMcpApprovalPolicy("project-a", ["attachment"]);
  assert.deepEqual(
    modalBody.children.map((row) => row.children[0].checked),
    [false, true, false],
  );
  modalBody.children[0].children[0].checked = true;
  modalBody.children[1].children[0].checked = false;
  await findButton("保存审批权限").click();
  assert.deepEqual(calls[5], [
    "mcp-approval-policy",
    { project: "project-a", kinds: ["permission"] },
  ]);

  const admin = { id: "agent-admin", name: "管理员小蓝" };
  const traceView = {
    project: { id: "project-a", admin_agent_id: admin.id },
    capabilities: ["addressed_wake_v1"],
    members: [admin, agent],
    conversations: [{ id: "group:project-a", title: "项目大群" }],
    messages: [{ id: 42, conversation_id: "group:project-a" }],
    tasks: [{ id: "task-parent", title: "上游任务" }],
  };
  const options = (v) =>
    JSON.parse(JSON.stringify(context.groupMentionOptions(v)));
  assert.deepEqual(options(traceView), [
    ["", "普通聊天"],
    [admin.id, "交给管理员 · @管理员小蓝"],
    [agent.id, "@项目审阅员"],
  ]);
  assert.deepEqual(options({ ...traceView, capabilities: [] })[0], [
    "",
    "默认由管理员回复",
  ]);

  state.chatSelection = new Map([["project-a", "group:project-a"]]);
  const traced = {
    id: "task-traced",
    agent_id: agent.id,
    trace: {
      source_message_id: 42,
      parent_task_id: "task-parent",
      agent_name: agent.name,
      node: "worker-a",
      node_name: "设备 A",
      delivery_conversation_id: "group:project-a",
    },
  };
  context.openProjectTaskTrace(traceView, traced);
  assert.equal(fields.get("任务编号").value, "task-traced");
  assert.equal(fields.get("任务编号").readOnly, true);
  assert.ok(
    modalBody.children.some((item) => item.text === "负责人 · 项目审阅员"),
  );
  assert.ok(
    modalBody.children.some((item) => item.text === "执行设备 · 设备 A"),
  );
  assert.ok(findIn(modalBody, "交付会话 · 项目大群"));
  await findIn(modalBody, "查看来源消息").click();
  assert.deepEqual(jumped, ["project-message-42"]);
  await findButton("复制任务编号").click();
  assert.deepEqual(copied, ["task-traced"]);

  context.openProjectTaskTrace(traceView, {
    ...traced,
    trace: {
      ...traced.trace,
      source_message_id: null,
      delivery_conversation_id: "private-unavailable",
    },
  });
  assert.equal(findIn(modalBody, "查看来源消息"), undefined);
  assert.ok(!JSON.stringify(modalBody).includes("private-unavailable"));
  findIn(modalBody, "查看上游任务").click();
  assert.equal(fields.get("任务编号").value, "task-parent");

  context.refreshProjectWork = async () => {};
  const workView = {
    ...traceView,
    members: [admin, agent],
  };
  const card = context.projectWorkCard(
    workView,
    "work",
    {
      id: "work-card-a",
      title: "发布候选版本",
      status: "delivered",
      revision: 3,
      agent: admin.id,
      instructions: "生成候选版本",
      acceptance: "测试全部通过",
      deliverable: "可安装包",
      deliveries: [
        { message_id: 103, summary: "构建 103 已完成" },
        { message_id: 104, summary: "构建 104 已完成" },
      ],
    },
    { event: "delivered", message: { id: 103 } },
  );
  assert.equal(card["data-work-id"], "work-card-a");
  assert.match(JSON.stringify(card), /项目工作/);
  assert.match(JSON.stringify(card), /项目工作 · 已交付/);
  assert.match(JSON.stringify(card), /发布候选版本/);
  assert.match(JSON.stringify(card), /状态 · 已交付/);
  assert.match(JSON.stringify(card), /版本 · v3/);
  assert.match(JSON.stringify(card), /负责人 · 管理员小蓝/);
  assert.match(JSON.stringify(card), /任务目标/);
  assert.match(JSON.stringify(card), /验收标准/);
  assert.match(JSON.stringify(card), /预期交付/);
  assert.match(JSON.stringify(card), /本次交付/);
  assert.match(JSON.stringify(card), /构建 103 已完成/);
  assert.doesNotMatch(JSON.stringify(card), /构建 104 已完成/);
  assert.ok(findIn(card, "查看详情"));

  const waiting = context.projectWorkCard(
    workView,
    "work",
    { id: "work-card-a", title: "发布候选版本", status: "waiting" },
    { event: "waiting", message: { body: "请确认发布分支" } },
  );
  assert.match(JSON.stringify(waiting), /等待答复/);
  assert.match(JSON.stringify(waiting), /请确认发布分支/);

  const draftCard = context.projectWorkCard(workView, "draft", {
    id: "draft-card-a",
    title: "发布草稿",
    status: "draft",
    revision: 2,
    agent: agent.id,
    instructions: "先核对变更",
  });
  assert.match(JSON.stringify(draftCard), /工作草稿/);
  assert.match(JSON.stringify(draftCard), /状态 · 草稿/);
  assert.ok(findIn(draftCard, "审阅并确认"));

  context.openProjectWorkForm(workView, {
    conversation_id: "group:project-a",
    source_message_id: 42,
    task: { instructions: "检查这条消息" },
  });
  fields.get("标题").value = "核对发布清单";
  fields.get("负责人").value = admin.id;
  fields.get("验收标准").value = "清单完整";
  await findButton("创建").click();
  assert.equal(calls.at(-1)[0], "project-work");
  assert.equal(calls.at(-1)[1].action, "create");
  assert.equal(calls.at(-1)[1].agent, admin.id);
  assert.equal(calls.at(-1)[1].conversation_id, "group:project-a");
  assert.equal(calls.at(-1)[1].source_message_id, 42);
  assert.ok(calls.at(-1)[1].client_request_id);

  const draft = {
    id: "draft-a",
    revision: 7,
    title: "核对发布清单",
    instructions: "检查清单",
    agent: admin.id,
    stage: "implement",
    visibility: "group",
  };
  context.openProjectWorkForm(workView, { draft });
  fields.get("标题").value = "已修改但未保存";
  await assert.rejects(findButton("确认此版本").click(), /先保存新版本/);
  assert.notEqual(calls.at(-1)[1].action, "confirm");
  await findButton("保存新版本").click();
  assert.equal(calls.at(-1)[1].action, "revise");
  assert.equal(calls.at(-1)[1].draft_id, "draft-a");
  assert.equal(calls.at(-1)[1].revision, 7);

  context.openProjectWorkForm(workView, { draft });
  const confirm = findButton("确认此版本");
  failWorkOnce = true;
  await assert.rejects(confirm.click(), /temporary failure/);
  const retryId = calls.at(-1)[1].client_request_id;
  await confirm.click();
  assert.equal(calls.at(-1)[1].action, "confirm");
  assert.equal(calls.at(-1)[1].client_request_id, retryId);
  assert.deepEqual(
    { draft_id: calls.at(-1)[1].draft_id, revision: calls.at(-1)[1].revision },
    { draft_id: "draft-a", revision: 7 },
  );

  context.openProjectWorkDetail(workView, {
    id: "work-a",
    title: "发布工作",
    status: "delivered",
    revision: 4,
    agent: admin.id,
    instructions: "完成发布",
    deliverable: "发布包",
    acceptance: "检查通过",
    deliveries: [{ body: "已完成" }],
    inputs: [],
    executions: [],
  });
  findButton("修改范围").click();
  assert.equal(fields.get("负责人"), undefined);
  assert.equal(fields.get("阶段"), undefined);
  fields.get("交付物").value = "发布包和校验记录";
  await findButton("保存范围").click();
  assert.equal(calls.at(-1)[1].action, "update-scope");
  assert.equal(calls.at(-1)[1].work_id, "work-a");
  assert.equal(calls.at(-1)[1].revision, 4);
  assert.equal(calls.at(-1)[1].deliverable, "发布包和校验记录");
  assert.equal(calls.at(-1)[1].project, "project-a");

  context.openProjectWorkDetail(workView, {
    id: "work-a",
    title: "发布工作",
    status: "delivered",
    revision: 4,
    agent: admin.id,
    deliveries: [],
    inputs: [],
    executions: [],
  });
  await findButton("验收").click();
  assert.equal(calls.at(-1)[1].action, "accept");
  assert.equal(calls.at(-1)[1].work_id, "work-a");

  context.openProjectWorkForm(workView);
  fields.get("标题").value = "无消息来源的项目工作";
  fields.get("工作说明").value = "直接创建";
  fields.get("负责人").value = admin.id;
  await findButton("创建").click();
  assert.equal("source_message_id" in calls.at(-1)[1], false);
  assert.equal("conversation_id" in calls.at(-1)[1], false);
}

Promise.all([checkInterfaceLanguage(), checkProjectAgentForms()]).then(
  () =>
    console.log(
      "UI retirement, interface language and project Agent checks passed",
    ),
  (error) => {
    console.error(error);
    process.exitCode = 1;
  },
);
