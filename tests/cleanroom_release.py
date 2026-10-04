"""Offline clean-room verification for one unpacked Windows release."""
import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('package',type=Path)
    parser.add_argument('scratch',type=Path)
    args=parser.parse_args()
    package=args.package.resolve();scratch=args.scratch.resolve()
    if scratch.exists():shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    local=scratch/'localappdata';local.mkdir()
    os.environ['LOCALAPPDATA']=str(local)
    os.environ['CODEX_HOME']=str(scratch/'codex-home')
    client=package/'client';runtime=package/'runtime'/'python.exe'
    required=(package/'AgentHarness.exe',runtime,client/'app_bridge.py',client/'mcp_adapter.py',package/'VERSION')
    if not all(path.is_file() for path in required):raise RuntimeError('release is incomplete')
    if (package/'VERSION').read_text(encoding='utf-8').strip()!='7.0.5':raise RuntimeError('wrong release version')
    sys.path.insert(0,str(client))
    for source in client.glob('*.py'):py_compile.compile(str(source),doraise=True)

    from app_bridge import AppBridge, MCP_DELEGATE_SCOPES
    from account_identity import generate_identity
    from collaboration_store import Store
    from common import write_json
    from service_install import install_bundle

    bridge=AppBridge()
    expected_root=(local/'AgentHarness'/'app-v6').resolve()
    if bridge.root!=expected_root or bridge._host_state_root()!=expected_root/'host-data':
        raise RuntimeError('fresh data root is not package independent')
    store=Store(bridge._host_state_root())
    if not (expected_root/'host-data'/'collaboration.sqlite3').is_file() or not store.service_id():
        raise RuntimeError('fresh host database was not created in stable state')

    installed=install_bundle(package,local/'AgentHarness'/'service-runtime')
    if (installed/'client'/'VERSION').read_text(encoding='utf-8').strip()!='7.0.5':
        raise RuntimeError('installed runtime is not this release')
    forbidden=('D:\\files\\distributed-agent','76bae1b6d36fa2193','1f5521c2dfeede28','63e73b1e7906b18d')
    for source in (installed/'client').glob('*.py'):
        text=source.read_text(encoding='utf-8')
        if any(value in text for value in forbidden):raise RuntimeError('old installation reference in '+source.name)

    identity=generate_identity();identity['account_id']='cleanroom-account'
    grant=scratch/'new-grant.json'
    write_json(grant,{'version':1,'profile_id':'cleanroom-profile','username':'cleanroom',
        'identity':identity,'scopes':list(MCP_DELEGATE_SCOPES),'servers':['http://127.0.0.1:1']})
    requests='\n'.join((
        json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'cleanroom','version':'1'}}}),
        json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})))+'\n'
    result=subprocess.run([str(installed/'runtime'/'python.exe'),'-X','utf8',
        str(installed/'client'/'mcp_adapter.py'),'--grant',str(grant)],input=requests,
        text=True,encoding='utf-8',capture_output=True,timeout=20)
    if result.returncode:raise RuntimeError('fresh MCP failed: '+result.stderr[-500:])
    responses=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    tools=next(item for item in responses if item.get('id')==2)['result']['tools']
    if len(tools)!=6:raise RuntimeError('fresh MCP tool catalog is incomplete')
    print(json.dumps({'version':'7.0.5','compiled_modules':len(list(client.glob('*.py'))),
                      'mcp_tools':len(tools),'host_data':str(bridge._host_state_root()),
                      'installed_bundle':installed.name},ensure_ascii=False))


if __name__=='__main__':main()
