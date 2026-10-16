// Browser behavior test using a small DOM model; no service, account, or network access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const html = fs.readFileSync(
  path.join(__dirname, "../assets/web/index.html"),
  "utf8",
);
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = "";
    this.value = "";
    this.textContent = "";
    this.style = {};
    this.open = false;
    this.checked = false;
    this.classList = {
      add: (c) => {
        this.className += " " + c;
      },
      remove: (c) => {
        this.className = this.className
          .split(" ")
          .filter((x) => x !== c)
          .join(" ");
      },
      toggle: (c, on) => {
        const has = this.className.split(" ").includes(c);
        if (on === undefined) on = !has;
        if (on && !has) this.classList.add(c);
        if (!on) this.classList.remove(c);
      },
    };
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = [...nodes];
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
}
function walk(node) {
  return [node, ...node.children.flatMap(walk)];
}
const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
const dom = Object.fromEntries(ids.map((id) => [id, new Element()]));
dom.password.tagName = "INPUT";
dom.username.tagName = "INPUT";
const document = {
  activeElement: null,
  getElementById: (id) => dom[id],
  createElement: (tag) => new Element(tag),
};
class FormDataMock {
  constructor(form) {
    this.rows = walk(form)
      .filter((n) => n.name && (n.type !== "checkbox" || n.checked))
      .map((n) => [n.name, n.type === "checkbox" ? "on" : n.value]);
  }
  [Symbol.iterator]() {
    return this.rows[Symbol.iterator]();
  }
}
const user = { id: "human-1", username: "Alice" };
const view = {
  project: {
    id: "project-1",
    name: "Demo",
    owner_id: user.id,
    admin_agent_id: "agent-1",
    admin_agent_name: "Lead",
    paused: 0,
  },
  users: [user, { id: "human-2", username: "Bob" }],
  members: [
    {
      id: "agent-1",
      name: "Lead",
      owner_id: user.id,
      node: "mine",
      model: "test",
      effort: "medium",
      timeout: 120,
      enabled: 1,
      role: "admin",
      status: "idle",
    },
    {
      id: "agent-2",
      name: "Peer",
      owner_id: "human-2",
      node: "other",
      model: "test",
      effort: "high",
      timeout: 120,
      enabled: 1,
      role: "peer",
      status: "idle",
    },
  ],
  messages: [
    {
      id: 1,
      sender_id: "agent-2",
      sender_name: "Peer",
      recipient_id: "agent-1",
      recipient_name: "Lead",
      channel: "direct",
      created_at: 1,
      body: "<img src=x onerror=alert(1)>",
    },
  ],
  tasks: [
    {
      id: "task-1",
      agent_id: "agent-1",
      title: "My task",
      state: "running",
      created_at: 1,
    },
    {
      id: "task-2",
      agent_id: "agent-2",
      title: "Other task",
      state: "running",
      created_at: 1,
    },
  ],
  executions: [
    {
      id: "exec-1",
      agent_id: "agent-1",
      template: "check",
      state: "awaiting_owner",
    },
    {
      id: "exec-2",
      agent_id: "agent-2",
      template: "other",
      state: "awaiting_owner",
    },
  ],
};
const snapshot = {
  user,
  projects: [view.project],
  users: view.users,
  nodes: [{ name: "mine", owner_id: user.id }],
};
let loggedIn = false,
  bootstrap = true;
const calls = [],
  intervals = [];
async function fetch(url, options = {}) {
  calls.push({ url, ...options });
  let data;
  if (url === "/api/session")
    data = {
      csrf: loggedIn ? "auth-csrf" : "anon-csrf",
      user: loggedIn ? user : null,
      bootstrap_required: bootstrap,
    };
  else if (url === "/api/snapshot") data = snapshot;
  else if (url.startsWith("/api/project")) data = view;
  else {
    const body = JSON.parse(options.body);
    if (["bootstrap", "login", "register-user"].includes(body.action)) {
      loggedIn = true;
      bootstrap = false;
      data = { user, csrf: "auth-csrf" };
    } else if (body.action === "logout") {
      loggedIn = false;
      data = { user: null, csrf: "anon-csrf" };
    } else if (body.action === "invite")
      data = { invite: "one-use-invite", expires_in: 86400 };
    else data = { updated: true };
  }
  return { ok: true, json: async () => JSON.parse(JSON.stringify(data)) };
}
const context = vm.createContext({
  document,
  fetch,
  FormData: FormDataMock,
  setInterval: (fn) => intervals.push(fn),
  console,
  Date,
  Map,
  JSON,
  encodeURIComponent,
});
vm.runInContext(script, context, { filename: "web/index.html" });
const flush = async () => {
  for (let i = 0; i < 5; i++)
    await new Promise((resolve) => setImmediate(resolve));
};
function textNodes(root, text) {
  return walk(root).filter((n) => n.textContent === text);
}
function actionCalls(name) {
  return calls.filter(
    (c) => c.url === "/api/action" && JSON.parse(c.body).action === name,
  );
}
function getField(form, name) {
  return walk(form).find((n) => n.name === name);
}

(async () => {
  await flush();
  assert.equal(
    dom.authSubmit.textContent,
    "创建账户",
    "first visitor gets bootstrap, not impossible login",
  );
  dom.username.value = "Alice";
  dom.password.value = "long-test-password";
  await dom.authForm.onsubmit({ preventDefault() {} });
  await flush();
  assert.equal(JSON.parse(actionCalls("bootstrap")[0].body).username, "Alice");
  assert.equal(dom.password.value, "", "password cleared after authentication");
  assert.equal(
    vm.runInContext("state.csrf", context),
    "auth-csrf",
    "authenticated CSRF rotation applied",
  );
  assert.ok(
    textNodes(dom.projectView, "<img src=x onerror=alert(1)>").length,
    "agent content rendered as text",
  );
  assert.ok(
    walk(dom.projectView).some((n) =>
      n.textContent.includes("Peer → Lead（私聊）"),
    ),
    "private route visible in human audit",
  );
  assert.ok(
    !script.includes("innerHTML"),
    "no HTML interpretation of agent-controlled strings",
  );

  // Draft survives server refresh and recipients map to actual IDs/null.
  const body = walk(dom.projectView).find((n) => n.tagName === "TEXTAREA");
  const recipient = walk(dom.projectView)
    .filter((n) => n.tagName === "SELECT")
    .at(-1);
  body.value = "Draft persists";
  body.oninput();
  recipient.value = "agent-2";
  recipient.onchange();
  document.activeElement = body;
  const before = calls.length;
  await intervals[0]();
  assert.equal(calls.length, before, "poll skips editing controls");
  document.activeElement = null;
  await textNodes(dom.projectView, "发送")[0].onclick();
  await flush();
  const sent = JSON.parse(actionCalls("message")[0].body);
  assert.equal(sent.recipient_id, "agent-2");
  assert.equal(sent.body, "Draft persists");
  assert.equal(actionCalls("message")[0].headers["X-CSRF-Token"], "auth-csrf");
  assert.equal(
    vm.runInContext("state.drafts.size", context),
    0,
    "success clears sent draft",
  );

  vm.runInContext("state.tab='members';render()", context);
  assert.equal(
    textNodes(dom.projectView, "查看本机设置").length,
    1,
    "only owner gets local console link",
  );
  assert.equal(
    textNodes(dom.projectView, "群主控制").length,
    1,
    "project owner gets group administration",
  );
  await textNodes(dom.projectView, "查看本机设置")[0].onclick();
  let form = walk(dom.dialogBody).find((n) => n.tagName === "FORM");
  await form.onsubmit({ preventDefault() {} });
  await flush();
  assert.equal(
    actionCalls("configure-agent").length,
    0,
    "shared panel cannot configure local model settings",
  );

  await textNodes(dom.projectView, "配对我的机器")[0].onclick();
  form = walk(dom.dialogBody).find((n) => n.tagName === "FORM");
  getField(form, "name").value = "New peer";
  // The fake service returns no code here; exercise the request shape only.
  await form.onsubmit({ preventDefault() {} });
  await flush();
  const enrollment = JSON.parse(actionCalls("enroll-device")[0].body);
  assert.equal(enrollment.name, "New peer");
  assert.equal(
    actionCalls("add-agent").length,
    0,
    "pairing does not submit a central agent configuration",
  );
  await textNodes(dom.projectView, "生成邀请")[0].onclick();
  form = walk(dom.dialogBody).find((n) => n.tagName === "FORM");
  assert.equal(getField(form, "invite").value, "one-use-invite");
  assert.equal(getField(form, "invite").readOnly, true);
  dom.dialog.close();
  await textNodes(dom.projectView, "暂停协作")[0].onclick();
  assert.equal(JSON.parse(actionCalls("pause-project")[0].body).paused, true);

  vm.runInContext("state.tab='tasks';render()", context);
  assert.equal(textNodes(dom.projectView, "取消我的 agent 任务").length, 1);
  assert.equal(textNodes(dom.projectView, "发送到本机确认").length, 1);
  await textNodes(dom.projectView, "发送到本机确认")[0].onclick();
  assert.equal(
    JSON.parse(actionCalls("execution-approve")[0].body).execution,
    "exec-1",
    "shared action only forwards the request to the local worker",
  );
  await textNodes(dom.projectView, "明确派发任务")[0].onclick();
  form = walk(dom.dialogBody).find((n) => n.tagName === "FORM");
  for (const [name, value] of Object.entries({
    title: "Review",
    instructions: "Check files",
    acceptance: "No regressions",
    validation: "Run tests",
    allowed_files: "x.py",
  }))
    getField(form, name).value = value;
  await form.onsubmit({ preventDefault() {} });
  await flush();
  const task = JSON.parse(actionCalls("task")[0].body);
  assert.equal(task.project, "project-1");
  assert.equal(task.agent, "agent-1");
  assert.equal(task.validation, "Run tests");

  // Non-owner can audit but gets no group administration controls.
  vm.runInContext(
    "state.user={id:'human-2',username:'Bob'};state.tab='members';render()",
    context,
  );
  assert.equal(textNodes(dom.projectView, "群主控制").length, 0);
  assert.equal(textNodes(dom.projectView, "查看本机设置").length, 1);
  await dom.logout.onclick();
  await flush();
  assert.equal(
    vm.runInContext("state.csrf", context),
    "anon-csrf",
    "logout rotates back to anonymous CSRF",
  );
  dom.authSwitch.onclick();
  dom.username.value = "Invited";
  dom.password.value = "long-test-password";
  dom.authInvite.value = "invite-token";
  await dom.authForm.onsubmit({ preventDefault() {} });
  await flush();
  assert.equal(
    JSON.parse(actionCalls("register-user")[0].body).invite,
    "invite-token",
    "registration sends invitation",
  );
  console.log(
    "UI behavior checks passed: bootstrap, CSRF, private audit, safe rendering, drafts, owner controls, task and execution actions.",
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
