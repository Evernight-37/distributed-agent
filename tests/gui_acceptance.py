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
            window.evaluate_js(
                "S.view.tasks=Array.from({length:40},(_,i)=>({id:'scroll-'+i,title:'任务 '+i,state:'needs_review'}));renderProject();let t=document.querySelector('#projectTasks');t.open=true;t.scrollTop=80;document.querySelector('#projectMembers').open=true;renderProject()"
            )
            wait(
                "document.querySelector('#projectMembers')?.open && document.querySelector('#projectTasks')?.open && document.querySelector('#projectTasks')?.scrollTop>0"
            )
            report["project_panel_state_preserved"] = True
            window.evaluate_js(
                "document.querySelector('.composer textarea').value='<script>原样文本</script>';document.querySelector('.composer').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"
            )
            wait("document.querySelector('.bubble p')?.textContent==='<script>原样文本</script>'")
            report["message_safe_text"] = True
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
    webview.start(gui="edgechromium")
    server.shutdown()
    server.server_close()
    print(json.dumps(report, ensure_ascii=False))
    if "error" in report:
        sys.exit(1)
