import json, sys, tempfile, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_harness"))
from http.server import ThreadingHTTPServer
from panel import handler_for
from app_bridge import AppBridge
from collaboration_store import Store
import webview

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(root / "service"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:" + str(server.server_port)
    bridge = AppBridge(root / "app")

    class API:
        def invoke(self, action, data=None):
            return bridge.invoke(action, data)

    window = webview.create_window(
        "Production isolated GUI validation",
        url=str(Path(__file__).resolve().parents[1] / "assets" / "web" / "app.html"),
        js_api=API(),
        width=1220,
        height=820,
    )
    bridge.window = window
    report = {}

    def wait(expression):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = window.evaluate_js(expression)
            if result:
                return result
            time.sleep(0.1)
        raise RuntimeError("GUI wait failed: " + expression)

    def validate():
        try:
            wait("document.querySelectorAll('.auth-choice').length===3")
            window.evaluate_js(
                "Array.from(document.querySelectorAll('.auth-choice')).find(b=>b.querySelector('strong').textContent==='创建账户').click()"
            )
            wait("document.querySelector('#login form input')?.id")
            window.evaluate_js(
                "(()=>{let inputs=document.querySelectorAll('#login form input');inputs[0].value='隔离图形验收';inputs[1].value='';inputs[2].value="
                + json.dumps(url)
                + ";document.querySelector('#login form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));})()"
            )
            wait("document.querySelector('#content h1')?.textContent==='控制台'")
            Store(root / "service").claim_local_host(bridge.identity["account_id"])
            window.evaluate_js("refresh(true)")
            wait("S.dash.services.some(s=>s.hosted_by_me)")
            report["registered_dashboard"] = True
            report["compact_navigation"] = bool(
                window.evaluate_js(
                    "getComputedStyle(document.querySelector('.sidebar')).width==='64px' && !!document.querySelector('#accountNav')"
                )
            )
            window.evaluate_js(
                "Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='＋ 添加员工').click()"
            )
            report["default_machine"] = bool(wait("document.querySelector('#modalBody select')?.value"))
            window.evaluate_js(
                "document.querySelector('#modalBody input').value='GUI员工';Array.from(document.querySelectorAll('#modalActions button')).find(b=>b.textContent==='添加员工').click()"
            )
            wait("document.querySelector('#content')?.textContent.includes('GUI员工')")
            report["employee_created"] = True
            window.evaluate_js(
                "Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='＋ 新建项目').click();document.querySelector('#modalBody input').value='图形验收项目';document.querySelector('#modalBody select').value="
                + json.dumps(url)
                + ";Array.from(document.querySelectorAll('#modalActions button')).find(b=>b.textContent==='创建并进入').click()"
            )
            wait("document.querySelector('#content h1')?.textContent==='图形验收项目'")
            report["project_entered"] = True
            report["two_column_project"] = bool(
                window.evaluate_js(
                    "document.querySelectorAll('.conversation-layout > *').length===2 && !document.querySelector('.project-rail')"
                )
            )
            wait("!polling")
            window.evaluate_js(
                "polling=true;window.__panelProbe={pending:true};S.view.tasks=Array.from({length:40},(_,i)=>({id:'scroll-'+i,title:'任务 '+i,state:'needs_review'}));renderProject();"
                "requestAnimationFrame(()=>{const t=document.querySelector('#projectTasks');t.open=true;void t.offsetHeight;t.scrollTop=80;document.querySelector('#projectMembers').open=true;renderProject();"
                "requestAnimationFrame(()=>{window.__panelProbe={ok:document.querySelector('#projectMembers').open&&document.querySelector('#projectTasks').open&&document.querySelector('#projectTasks').scrollTop>0};polling=false;});});true"
            )
            wait("!window.__panelProbe.pending")
            report["project_panel_state_preserved"] = bool(window.evaluate_js("window.__panelProbe.ok"))
            if not report["project_panel_state_preserved"]:
                raise RuntimeError("Expanded panels and scroll position were not preserved")
            report["default_admin_value"] = bool(
                window.evaluate_js(
                    "(()=>{const s=document.querySelector('[aria-label=\"呼叫 Agent\"]');s.selectedIndex=0;return s.value==='';})()"
                )
            )
            if not report["default_admin_value"]:
                raise RuntimeError("Default administrator option must send no explicit mention ID")
            window.evaluate_js(
                "document.querySelector('.composer textarea').value='<script>原样文本</script>';document.querySelector('.composer').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"
            )
            wait("document.querySelector('.bubble p')?.textContent==='<script>原样文本</script>'")
            report["message_safe_text"] = True
            window.evaluate_js(
                "(()=>{const d=new DataTransfer();d.items.add(new File(['attachment test'],'GUI附件.txt',{type:'text/plain'}));"
                "const input=document.querySelector('.composer input[type=file]');input.files=d.files;input.dispatchEvent(new Event('change'));})()"
            )
            wait(
                "document.querySelector('.attachment-draft')?.textContent.includes('GUI附件.txt') && !S.drafts.values().next().value.uploading"
            )
            window.evaluate_js(
                "document.querySelector('.composer').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"
            )
            wait("document.querySelector('.messages .attachment-card')?.textContent.includes('GUI附件.txt')")
            report["file_attachment_sent"] = True
            window.evaluate_js(
                "(()=>{const raw=atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8X8AAAAASUVORK5CYII=');"
                "const d=new DataTransfer();d.items.add(new File([Uint8Array.from(raw,c=>c.charCodeAt(0))],'截图.png',{type:'image/png'}));"
                "const e=new Event('paste',{bubbles:true,cancelable:true});Object.defineProperty(e,'clipboardData',{value:d});document.querySelector('.composer textarea').dispatchEvent(e);})()"
            )
            wait(
                "document.querySelector('.attachment-draft')?.textContent.includes('截图.png') && !S.drafts.values().next().value.uploading"
            )
            window.evaluate_js(
                "document.querySelector('.composer').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"
            )
            wait("Array.from(document.querySelectorAll('.attachment-card button')).some(b=>b.textContent==='预览图片')")
            window.evaluate_js(
                "Array.from(document.querySelectorAll('.attachment-card button')).find(b=>b.textContent==='预览图片').click()"
            )
            wait("document.querySelector('#modalBody img')?.naturalWidth===1")
            report["pasted_screenshot_preview"] = True
            window.evaluate_js("closeModal();true")
            report["selection_preserved"] = bool(
                window.evaluate_js(
                    "(()=>{const p=document.querySelector('.bubble p'),box=document.querySelector('.messages'),r=document.createRange(),s=window.getSelection();r.selectNodeContents(p);s.removeAllRanges();s.addRange(r);S.dirty=false;renderProject();const ok=document.querySelector('.messages')===box&&S.dirty;s.removeAllRanges();return ok})()"
                )
            )
            window.evaluate_js("document.querySelector('#settingsNav').click()")
            wait("document.querySelector('#teamServiceCard')?.textContent.includes('协作服务')")
            report["service_settings"] = True
            window.evaluate_js("document.querySelector('#accountNav').click()")
            wait("document.querySelector('#content h1')?.textContent==='账户'")
            report["account_page"] = True
            window.evaluate_js("document.querySelector('#settingsNav').click()")
            wait("document.querySelector('#teamServiceCard')?.textContent.includes('协作服务')")
            window.evaluate_js(
                "Array.from(document.querySelectorAll('#teamServiceCard button')).find(b=>b.textContent==='检测连接').click()"
            )
            wait("document.querySelector('#toast')?.textContent.startsWith('连接成功：')")
            report["account_endpoint_test"] = True
            window.evaluate_js("document.querySelector('#dashboardNav').click()")
            wait("document.querySelector('#content h1')?.textContent==='控制台'")
            window.evaluate_js(
                "Array.from(document.querySelectorAll('#content .agent button')).find(b=>b.textContent==='删除').click()"
            )
            wait("document.querySelector('#modalActions')?.textContent.includes('确认删除员工')")
            window.evaluate_js(
                "Array.from(document.querySelectorAll('#modalActions button')).find(b=>b.textContent==='确认删除员工').click()"
            )
            wait("(refresh(true), !S.dash.employees.some(e=>e.name==='GUI员工'))")
            report["employee_deleted"] = True
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            try:
                runtime = bridge._runtime()
                runtime.stop()
                deadline = time.monotonic() + 20
                while runtime.status()["running"] and time.monotonic() < deadline:
                    time.sleep(0.1)
            except Exception:
                pass
            Path("build/gui-flow-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            window.destroy()

    window.events.loaded += validate
    webview.start(gui="cocoa" if sys.platform == "darwin" else "edgechromium")
    server.shutdown()
    server.server_close()
    print(json.dumps(report, ensure_ascii=False))
    if "error" in report:
        sys.exit(1)
