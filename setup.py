"""Pair one explicitly configured workspace with the installed bridge (Windows)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import uuid

import endpoint_transport as transport

PACKAGE = Path(__file__).resolve().parent
CLIENT_FILES = ['cli.py','endpoint_transport.py','foreground_run.py','stage_chain.py','turn_usage.py',
                'followup_backoff.py','event_worker.py','event_wakeup.py','desktop_mcp.py',
                'vendor-manifest.json','routing-policy.json','followup-schedule.json','AGENT_DISCOVERY.md']


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    temporary.replace(path)


def setup(home, workspace, executable):
    home=Path(home).expanduser().absolute();workspace=Path(workspace).expanduser().absolute()
    executable=Path(executable).expanduser().absolute()
    if (not transport.same_path(home,home.resolve()) or not transport.same_path(workspace,workspace.resolve())
            or not workspace.is_dir() or not executable.is_file()):
        raise ValueError('Use existing, unlinked workspace and Harness executable paths')
    if home.is_relative_to(workspace) or workspace.is_relative_to(home):
        raise ValueError('Host data and DeepSeek workspace must be separate sibling directories')
    endpoint=home/'.state/endpoint.json'
    observed=transport.load(endpoint)
    cfg={'base_url':observed['base_url'],'token_file':str(home/'.client/bridge.token'),
         'endpoint_discovery':{'file':str(endpoint),'expected_executable':str(executable),
                               'expected_module':(PACKAGE/'plugin.mjs').as_uri()}}
    observed=transport.discover(cfg)
    entries=[w for w in observed.get('workspaces',[]) if transport.same_path(w['workspace_path'],workspace)]
    if len(entries)!=1:
        raise ValueError('Register this workspace in Harness and configure this plugin workspacePaths first')
    cfg.update(entries[0])
    url=transport.local_url(observed['base_url'])
    with socket.create_connection((url.hostname,url.port),timeout=5) as peer:
        transport.verify_peer(peer,observed)  # No bearer sent before OS identity verification.
    manifest=transport.load(PACKAGE/'vendor-manifest.json')
    files=list(CLIENT_FILES)
    for group,data in manifest.items():
        for rel,digest in data['files'].items():
            source=PACKAGE/'vendor'/group/rel
            if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise ValueError('Vendor integrity failed')
            files.append('vendor/'+group+'/'+rel)
    # Refuse code replacement; upgrades need a reviewed new client directory.
    for rel in files:
        target=home/rel;source=PACKAGE/rel
        if target.exists() and target.read_bytes()!=source.read_bytes():
            raise ValueError('Existing client file differs; preserve it and use a reviewed upgrade: '+rel)
    config_file=home/'.client/config.json'
    if config_file.exists():
        previous=transport.load(config_file)
        if ({k:v for k,v in previous.items() if k!='base_url'}!={k:v for k,v in cfg.items() if k!='base_url'}
                or transport.local_url(previous['base_url']).path!=url.path):
            raise ValueError('Existing pairing differs; no implicit workspace or module replacement')
        cfg['base_url']=previous['base_url']  # Port changes alone never rotate a pairing.
    tokens_file=home/'.state/forward/tokens.json';before=tokens_file.read_bytes() if tokens_file.exists() else None
    store=json.loads(before) if before else {'version':1,'tokens':{}}
    alias='codex-handoff-default';prior=store.get('tokens',{}).get(alias)
    token_file=Path(cfg['token_file'])
    if prior:
        token=token_file.read_text(encoding='utf8').strip()
        if (hashlib.sha256(token.encode()).hexdigest()!=prior['hash']
                or prior.get('workspaceIds')!=[cfg['workspace_id']] or prior.get('allowWorkspaceCreate') is not False):
            raise ValueError('Existing scoped credential differs; refusing rotation or scope change')
    else:
        if token_file.exists():raise ValueError('Unreconciled existing token file; refusing overwrite')
        token='dshb_'+secrets.token_urlsafe(32)
        store.setdefault('tokens',{})[alias]={'alias':alias,'hash':hashlib.sha256(token.encode()).hexdigest(),
            'workspaceIds':[cfg['workspace_id']],'allowWorkspaceCreate':False}
    for rel in files:
        target=home/rel;target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():shutil.copyfile(PACKAGE/rel,target)
    if not prior:
        if (tokens_file.read_bytes() if tokens_file.exists() else None)!=before:
            raise ValueError('Concurrent credential-store edit; inspect before continuing')
        token_file.parent.mkdir(parents=True,exist_ok=True)
        # Private POSIX mode also helps users inspecting the package on other systems.
        fd=os.open(token_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf8') as f:f.write(token+'\n')
        write(tokens_file,store)
    write(config_file,cfg)
    return {'paired':True,'workspace':str(workspace),'cli':[sys.executable,'-X','utf8','-B',str(home/'cli.py')],
            'credential_scope':'one_registered_workspace','auto_approve':False,'model_started':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home',default=str(Path.home()/'.dsh/codex-handoff'))
    p.add_argument('--workspace',required=True)
    p.add_argument('--harness-executable',required=True)
    args=p.parse_args()
    print(json.dumps(setup(args.home,args.workspace,args.harness_executable),ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,RuntimeError,ValueError,KeyError) as exc:
        print(json.dumps({'paired':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(2)
