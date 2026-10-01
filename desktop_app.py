"""Beginner friendly native control panel for an Agent Harness worker.

This file intentionally contains no execution policy or shell integration.  The
small DesktopRuntime adapter owns networking and persistent configuration.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from desktop_runtime import DesktopRuntime


class DesktopApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Agent Harness · 本机助手")
        self.root.geometry("780x650")
        self.root.minsize(740, 600)
        self.data_dir = tk.StringVar(value=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AgentHarness" / "worker"))
        self.runtime = DesktopRuntime(Path(self.data_dir.get()))
        self._jobs: queue.Queue = queue.Queue()
        self.busy = False
        self.shared_by_label = {}
        self._build()
        self._poll()
        self.refresh_status()

    def _build(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Agent Harness", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(outer, text="用按钮配对这台电脑并管理本机 Agent，不需要打开终端。", foreground="#555").pack(anchor="w", pady=(2, 14))

        workbench = ttk.Frame(outer)
        workbench.pack(fill="x", pady=(0, 12))
        ttk.Button(workbench, text="创建 / 打开我的工作台", command=lambda: self._run(self.runtime.start_workbench)).pack(side="left")
        ttk.Button(workbench, text="打开团队工作台", command=self.open_workbench).pack(side="left", padx=10)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill='both', expand=True, pady=(0, 12))
        connection_tab, project_tab = ttk.Frame(tabs, padding=12), ttk.Frame(tabs, padding=12)
        tabs.add(connection_tab, text='连接与状态')
        tabs.add(project_tab, text='项目与 Agent 设置')

        conn = ttk.LabelFrame(connection_tab, text="1  连接这台电脑", padding=12)
        conn.pack(fill="x", pady=(0, 10))
        ttk.Label(conn, text="团队提供的服务地址").grid(row=0, column=0, sticky="w")
        self.server = tk.StringVar(value="http://127.0.0.1:8765")
        self.server_entry = ttk.Entry(conn, textvariable=self.server)
        self.server_entry.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(conn, text="工作台中生成的机器配对码").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.code = tk.StringVar()
        self.code_entry = ttk.Entry(conn, textvariable=self.code, show="•")
        self.code_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(9, 0))
        ttk.Label(conn, text="配对码只用于首次接入，软件不会显示或记录令牌。", foreground="#666").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        conn.columnconfigure(1, weight=1)
        self.connect_button = ttk.Button(conn, text="配对并启动", command=self.pair_start)
        self.connect_button.grid(row=0, column=2, rowspan=2, padx=(8, 0), ipadx=8)

        local = ttk.LabelFrame(connection_tab, text="本机数据位置（通常不需要更改）", padding=12)
        local.pack(fill="x", pady=(0, 10))
        ttk.Entry(local, textvariable=self.data_dir, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(local, text="选择文件夹…", command=self.choose_data).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(local, text="选择旧目录可以继续原来的配对、项目和运行状态。", foreground="#666").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        local.columnconfigure(0, weight=1)

        existing = ttk.Frame(project_tab)
        existing.pack(fill='x', pady=(0, 8))
        self.existing_binding = tk.StringVar()
        self.binding_choices = ttk.Combobox(existing, textvariable=self.existing_binding, state='readonly')
        self.binding_choices.pack(side='left', fill='x', expand=True)
        ttk.Button(existing, text='载入已有 Agent 设置', command=self.edit_binding).pack(side='left', padx=8)
        self.bindings_by_label, self.local_projects = {}, []
        proj = ttk.LabelFrame(project_tab, text="2  选择项目与本机 Agent", padding=12)
        proj.pack(fill="x", pady=(0, 10))
        ttk.Label(proj, text="项目名称（英文或数字）").grid(row=0, column=0, sticky="w")
        self.project_name = tk.StringVar()
        ttk.Entry(proj, textvariable=self.project_name).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(proj, text="选择项目文件夹…", command=self.choose_project).grid(row=0, column=2)
        self.project_path = tk.StringVar()
        ttk.Entry(proj, textvariable=self.project_path).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(proj, text="共享项目").grid(row=2, column=0, sticky="w", pady=(9, 0))
        self.shared_project = tk.StringVar()
        self.shared_projects = ttk.Combobox(proj, textvariable=self.shared_project, state="readonly")
        self.shared_projects.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=(9, 0))
        ttk.Label(proj, text="模型").grid(row=3, column=0, sticky="w", pady=(9, 0))
        self.model = tk.StringVar(value="gpt-5.6-sol")
        ttk.Entry(proj, textvariable=self.model).grid(row=3, column=1, sticky="ew", padx=8, pady=(9, 0))
        ttk.Label(proj, text="思考强度").grid(row=4, column=0, sticky="w", pady=(9, 0))
        self.effort = tk.StringVar(value="medium")
        ttk.Combobox(proj, textvariable=self.effort, values=("low", "medium", "high", "xhigh", "max", "ultra"), state="readonly").grid(row=4, column=1, sticky="w", padx=8, pady=(9, 0))
        self.enable = tk.BooleanVar(value=False)
        self.discuss = tk.BooleanVar(value=True)
        ttk.Checkbutton(proj, text="启用本项目的 Agent（保存后才会运行）", variable=self.enable).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(proj, text="允许参与讨论", variable=self.discuss).grid(row=6, column=0, columnspan=3, sticky="w")
        self.review = tk.BooleanVar(value=False)
        self.implement = tk.BooleanVar(value=False)
        ttk.Checkbutton(proj, text="允许检查", variable=self.review).grid(row=6, column=1, sticky="e")
        ttk.Checkbutton(proj, text="允许修改（需独立 Git worktree）", variable=self.implement).grid(row=6, column=2, sticky="w")
        ttk.Button(proj, text="保存本机项目设置", command=self.save_project).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
        proj.columnconfigure(1, weight=1)

        status = ttk.LabelFrame(connection_tab, text="状态", padding=12)
        status.pack(fill="x", pady=(0, 10))
        self.status_text = tk.StringVar(value="正在读取…")
        ttk.Label(status, textvariable=self.status_text, wraplength=620).pack(anchor="w")
        controls = ttk.Frame(status)
        controls.pack(anchor="w", pady=(10, 0))
        ttk.Button(controls, text="刷新状态", command=self.refresh_status).pack(side="left")
        ttk.Button(controls, text="停止接收任务", command=self.stop).pack(side="left", padx=8)
        ttk.Button(controls, text="高级设置 / 作业批准", command=lambda: self._run(self.runtime.open_panel)).pack(side="left")
        ttk.Label(outer, text="关闭窗口后继续接收任务。“停止接收任务”不会终止已经启动的作业。", foreground="#666").pack(anchor="w")

    def choose_data(self):
        if self.busy: return
        value = filedialog.askdirectory(title="选择本机数据目录", initialdir=self.data_dir.get())
        if value:
            self.data_dir.set(value)
            self.runtime = DesktopRuntime(Path(value))
            self.refresh_status()

    def choose_project(self):
        value = filedialog.askdirectory(title="选择项目文件夹")
        if value:
            self.project_path.set(value)

    def _run(self, fn, done=None):
        if self.busy: return
        self.busy = True
        self.status_text.set("正在处理，请稍候…")
        def work():
            try:
                result = fn()
                self._jobs.put((done, result, None))
            except Exception as exc:
                self._jobs.put((done, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, result, error = self._jobs.get_nowait()
                self.busy = False
                if error:
                    self.status_text.set("操作未完成：" + error)
                    messagebox.showerror("操作未完成", error)
                elif done:
                    done(result)
                else:
                    self.status_text.set('操作完成。需要查看连接情况时，点“刷新状态”。')
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def pair_start(self):
        server, code, runtime = self.server.get().strip(), self.code.get().strip(), self.runtime
        def start():
            if not runtime.status().get("paired"):
                runtime.pair(server, code)
            return runtime.start()
        def finished(_):
            self.code.set("")
            self.refresh_status()
        self._run(start, finished)

    def open_workbench(self):
        server, runtime = self.server.get().strip(), self.runtime
        self._run(lambda: runtime.open_workbench(server))

    def save_project(self):
        name, path = self.project_name.get().strip(), self.project_path.get().strip()
        if not name or not path:
            messagebox.showwarning("还缺少信息", "请填写项目名称并选择项目文件夹。")
            return
        import re
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
            messagebox.showwarning("项目名称不合适", "项目名称请只使用英文、数字、短横线或下划线。")
            return
        selected = self.shared_by_label.get(self.shared_project.get())
        if not selected:
            messagebox.showwarning("还缺少信息", "请先连接机器并选择共享项目。列表为空时，先在工作台创建或加入项目，再刷新状态。")
            return
        model, effort, enabled = self.model.get().strip(), self.effort.get(), bool(self.enable.get())
        stages = []
        for key, flag in [('discuss', self.discuss), ('review', self.review), ('implement', self.implement)]:
            if flag.get(): stages.append(key)
        runtime = self.runtime
        def save():
            previous = next((b for b in runtime.fetch_state()['bindings'] if b['project_id'] == selected and b['backend_project'] == name), {})
            runtime.register_project(name, path, model, effort)
            binding = {"project_id": selected, "backend_project": name, "name": name, "model": model, "effort": effort, "timeout": previous.get('timeout', 120), "enabled": enabled, "stages": stages}
            return runtime.save_binding(binding)
        self._run(save, lambda _: self.status_text.set("已保存。本机权限已生效，可回工作台选择管理员并开始群聊。"))

    def refresh_status(self):
        runtime = self.runtime
        self._run(lambda: (runtime.status(), self._projects(runtime)), self._show_status)

    @staticmethod
    def _projects(runtime):
        try:
            return runtime.fetch_state()
        except Exception:
            return {'shared_projects': [], 'projects': [], 'bindings': []}

    def _show_status(self, result):
        data, snapshot = result
        projects = snapshot['shared_projects']
        self.local_projects = snapshot['projects']
        self.bindings_by_label = {b['backend_project'] + ' · ' + b['project_id'][:8]: b for b in snapshot['bindings']}
        self.binding_choices['values'] = list(self.bindings_by_label)
        state = '未配对' if not data['paired'] else '正在停止，等待当前调度轮次结束' if data.get('stopping') else '已停止接收' if not data['running'] else '已连接' if data['online'] else '正在连接工作台'
        self.status_text.set(state + (' · ' + data['node'] if data['node'] else ''))
        self.shared_by_label = {p.get('name', '') + ' (' + p['id'][:8] + ')': p['id'] for p in projects}
        self.shared_projects["values"] = list(self.shared_by_label)
        if len(projects) == 1 and not self.shared_project.get(): self.shared_project.set(next(iter(self.shared_by_label)))
        self.connect_button.configure(text='启动接收' if data['paired'] else '配对并启动')
        if data['server']: self.server.set(data['server'])
        self.server_entry.configure(state='readonly' if data['paired'] else 'normal')
        self.code_entry.configure(state='disabled' if data['paired'] else 'normal')

    def edit_binding(self):
        binding = self.bindings_by_label.get(self.existing_binding.get())
        if not binding: return
        project = next((p for p in self.local_projects if p['name'] == binding['backend_project']), {})
        self.project_name.set(binding['backend_project'])
        self.project_path.set(project.get('remote_path', ''))
        self.model.set(binding['model']); self.effort.set(binding['effort'])
        self.enable.set(binding['enabled'])
        for key, flag in [('discuss', self.discuss), ('review', self.review), ('implement', self.implement)]: flag.set(key in binding['stages'])
        self.shared_project.set(next((label for label, value in self.shared_by_label.items() if value == binding['project_id']), ''))

    def stop(self):
        self._run(self.runtime.stop, lambda _: self.refresh_status())


def main():
    import sys
    root = tk.Tk()
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-report':
        # Packaging check, with no enrollment or background/model startup.
        import json
        root.withdraw()
        original = DesktopApp.refresh_status
        DesktopApp.refresh_status = lambda self: None
        try:
            app = DesktopApp(root)
            root.update_idletasks()
            report = {'gui_built': True, 'required_width': root.winfo_reqwidth(),
                      'required_height': root.winfo_reqheight(), 'explicit_enable_default': app.enable.get(),
                      'shared_project_default': app.shared_project.get()}
            Path(sys.argv[2]).write_text(json.dumps(report), encoding='utf-8')
        finally:
            DesktopApp.refresh_status = original
            root.destroy()
        return
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
