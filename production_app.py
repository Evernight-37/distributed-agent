"""Native production shell: a trusted local view and a narrow Python bridge."""
import argparse
import json
import os
import threading
import time
from pathlib import Path
from desktop_runtime import assets
from app_bridge import AppBridge

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data');parser.add_argument('--smoke-report');args=parser.parse_args()
    import webview
    bridge=AppBridge(args.data)
    class API:
        def invoke(self,action,data=None): return bridge.invoke(action,data)
    window=webview.create_window('Agent Harness · 协作工作台',url=str(assets()/'web'/'app.html'),js_api=API(),width=1220,height=820,min_size=(800,600))
    bridge.window=window
    def closing():
        if bridge.allow_window_close or args.smoke_report: return True
        # WinForms raises closing on its UI thread. Queue JavaScript after the
        # cancellation so a synchronous WebView call cannot deadlock the form.
        threading.Thread(target=lambda: window.evaluate_js('window.requestAppClose && window.requestAppClose()'), daemon=True).start()
        return False
    window.events.closing += closing
    window.events.closed += bridge.close
    def loaded():
        if args.smoke_report:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                result=window.evaluate_js("({title:document.title,login:!!document.getElementById('login'),bridge:typeof window.pywebview.api.invoke,heading:document.querySelector('#login h1')?.textContent,inputs:document.querySelectorAll('#login input').length})")
                if result.get('heading'):break
                time.sleep(.05)
            Path(args.smoke_report).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
            window.destroy()
    window.events.loaded += loaded
    webview.start(gui='edgechromium',debug=False)
if __name__=='__main__':main()
