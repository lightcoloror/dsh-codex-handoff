"""A small per-user receipt watcher. It launches only the official MCP adapter,
never Codex generation, and never edits the running DeepSeek Harness."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,ctypes,hashlib,json,os,subprocess,sys,time,uuid
import cli
from desktop_mcp import DesktopMcp,DesktopMcpError
from event_wakeup import Wakeups,pending_groups

ROOT=Path(__file__).resolve().parent
CONTROL=ROOT/'.state/event-control'
COMMANDS=CONTROL/'commands'
CONFIG=COMMANDS/'config.json'
HEALTH=CONTROL/'health.json'
PID=CONTROL/'pid.json'
STOP=COMMANDS/'stop.json'

def source_hashes():
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ['event_worker.py','event_wakeup.py','desktop_mcp.py','followup_backoff.py','cli.py']}

class Changes:
    def __init__(self,paths):
        self.paths=[Path(x) for x in paths];self.handles=[]
        if os.name!='nt':return
        from ctypes import wintypes
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.FindFirstChangeNotificationW.argtypes=[wintypes.LPCWSTR,wintypes.BOOL,wintypes.DWORD];self.k.FindFirstChangeNotificationW.restype=wintypes.HANDLE
        self.k.FindNextChangeNotification.argtypes=[wintypes.HANDLE];self.k.FindNextChangeNotification.restype=wintypes.BOOL
        self.k.FindCloseChangeNotification.argtypes=[wintypes.HANDLE]
        self.k.WaitForMultipleObjects.argtypes=[wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE),wintypes.BOOL,wintypes.DWORD]
        self.k.WaitForMultipleObjects.restype=wintypes.DWORD
        self.handle_type=wintypes.HANDLE
        for path in self.paths:
            path.mkdir(parents=True,exist_ok=True)
            h=self.k.FindFirstChangeNotificationW(str(path),True,0x1|0x2|0x10)
            if h==ctypes.c_void_p(-1).value:raise OSError(ctypes.get_last_error(),'Directory change watcher failed',str(path))
            self.handles.append(h)
    def wait(self,seconds):
        if os.name!='nt':time.sleep(min(seconds,1));return True
        array=(self.handle_type*len(self.handles))(*self.handles)
        result=self.k.WaitForMultipleObjects(len(self.handles),array,False,max(1,int(seconds*1000)))
        if result==258:return False
        if result<len(self.handles):
            if not self.k.FindNextChangeNotification(self.handles[result]):raise OSError(ctypes.get_last_error(),'Directory re-arm failed')
            return True
        raise OSError(ctypes.get_last_error(),'Directory watcher wait failed')
    def close(self):
        for h in self.handles:self.k.FindCloseChangeNotification(h)
        self.handles=[]

def lock_file():
    CONTROL.mkdir(parents=True,exist_ok=True)
    f=(CONTROL/'worker.lock').open('a+b')
    if f.tell()==0:f.write(b'0');f.flush()
    f.seek(0)
    if os.name=='nt':
        import msvcrt
        try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:f.close();return None
    else:
        import fcntl
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:f.close();return None
    return f

def status():
    held=lock_file()
    running=held is None
    if held:held.close()
    process=cli.load(PID) if PID.exists() else None
    return {'running':running,'config':cli.load(CONFIG) if CONFIG.exists() else None,
            'process':process,'health':cli.load(HEALTH) if HEALTH.exists() else None,
            'source_matches_running_snapshot':bool(process and process.get('source_hashes')==source_hashes()),
            'model_controller_started':False}

def ensure():
    current=status()
    if current['running']:
        if not current['source_matches_running_snapshot']:
            return {**current,'required_action':'Gracefully stop this owned watcher and ensure it again to load changed source; do not start a duplicate.'}
        return current
    if not CONFIG.exists() or not cli.load(CONFIG).get('enabled'):raise ValueError('Event watcher not configured/enabled by its owner')
    log=(CONTROL/'worker.log').open('a',encoding='utf8')
    args=[sys.executable,'-X','utf8','-B',str(Path(__file__)),'run']
    proc=subprocess.Popen(args,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
        creationflags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0)
    log.close()
    end=time.monotonic()+5
    while time.monotonic()<end:
        value=status()
        if (value['running'] and value.get('process',{}).get('pid')==proc.pid
            and (value.get('health') or {}).get('run_id')==value['process']['run_id']
            and value['health'].get('status') in ['watching','needs_attention']):return value
        if proc.poll() is not None:raise RuntimeError('Event watcher exited; inspect its owned log')
        time.sleep(.1)
    raise RuntimeError('Event watcher startup not confirmed; do not launch another instance blindly')

def stop():
    current=status()
    if not current['running']:return {'stopped':True,'already_stopped':True}
    cli.save(STOP,{'run_id':current['process']['run_id'],'requested_at':cli.now()})
    return {'stop_requested':True,'pid':current['process']['pid']}

def run():
    singleton=lock_file()
    if singleton is None:return
    conf=cli.load(CONFIG)
    if conf.get('enabled') is not True:singleton.close();return
    context=conf['caller_context'];assert str(uuid.UUID(context))==context
    run_id=uuid.uuid4().hex
    cli.save(PID,{'pid':os.getpid(),'run_id':run_id,'started_at':cli.now(),'caller_context':context,'model_process_started':False,'source_hashes':source_hashes()})
    mcp=None;watch=None;next_due=0;rows=[];errors=[];needs_scan=True;generation=0
    try:
        # Non-model connectivity verification once per worker startup.
        mcp=DesktopMcp(context,CONTROL/'native-logs')
        verified=mcp.read_thread(context)['thread']['id']==context
        if not verified:raise RuntimeError('Registered native caller context not verified')
        wake=Wakeups(mcp)
        watch=Changes([ROOT/'.state/jobs',ROOT/'.state/followups',COMMANDS,Path.home()/'.codex/automations'])
        while True:
            current_conf=cli.load(CONFIG)
            if not current_conf.get('enabled'):break
            if STOP.exists() and cli.load(STOP).get('run_id')==run_id:break
            now=time.monotonic()
            if needs_scan or now>=next_due:
                # Arm notifications before scanning, so a concurrent publish is
                # either in this snapshot or leaves a signalled watcher.
                errors=[];cfg=cli.config();workspaces=[cfg['workspace_path']]+[x['workspace_path'] for x in cfg.get('legacy_workspaces',[])]
                groups=pending_groups(ROOT/'.state/jobs',workspaces,current_conf.get('allowed_sources'),errors)
                rows=[]
                try:wake.consume_finished(groups)
                except (OSError,ValueError,DesktopMcpError) as e:errors.append({'phase':'cleanup','error':str(e)})
                for source,messages in groups.items():
                    try:rows.append(wake.process(source,messages))
                    except (OSError,ValueError,KeyError,DesktopMcpError) as e:
                        errors.append({'source':source,'error':str(e)});rows.append({'source':source,'state':'blocked','retry_after_seconds':60})
                delays=[r.get('retry_after_seconds',60) for r in rows]
                if wake.cleanup_retry_after is not None:delays.append(wake.cleanup_retry_after)
                next_due=time.monotonic()+min(delays or [86400])
                generation+=1;needs_scan=False
            attention=bool(errors) or any(r['state'] in ['uncertain','blocked','source_status_unknown'] for r in rows)
            cli.save(HEALTH,{'checked_at':cli.now(),'status':'needs_attention' if attention else 'watching','pid':os.getpid(),'run_id':run_id,
                'caller_context_verified':True,'generation':generation,'sources':rows,'errors':errors,
                'allowed_sources':current_conf.get('allowed_sources'),'model_processes_started':0,
                'mechanism':'directory change notifications + official native MCP; no Harness reload or network listener'})
            changed=watch.wait(min(30,max(.1,next_due-time.monotonic())))
            if changed:
                # Health/pid/log writes are outside the watched command folder.
                # A tiny debounce coalesces atomic rename notifications.
                time.sleep(.05)
                needs_scan=True
    except BaseException as e:
        cli.save(HEALTH,{'checked_at':cli.now(),'status':'stopped_error','error':str(e),'pid':os.getpid(),'run_id':run_id,'model_processes_started':0})
        raise
    finally:
        if watch:watch.close()
        if mcp:mcp.close()
        singleton.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['status','ensure','stop','run']);a=parser.parse_args()
    if a.action=='run':run()
    else:print(json.dumps({'status':status,'ensure':ensure,'stop':stop}[a.action](),ensure_ascii=False,indent=2))
