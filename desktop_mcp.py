"""Programmatic client for the installed official desktop MCP stdio adapter.

No LLM is launched. The caller context is the real registered chat which granted
this host process its task-scoped delegation. Native turn identities are never
supplied or copied; the official adapter creates MCP request correlations.
"""
from pathlib import Path
import json,os,queue,subprocess,threading,time,uuid

class DesktopMcpError(RuntimeError):
    def __init__(self,message,*,request_sent=False):
        self.request_sent=request_sent
        super().__init__(message)

def plugin_directory():
    root=Path.home()/'.codex/plugins/cache/openai-bundled/codex-app-tools'
    candidates=[p for p in root.iterdir() if p.is_dir() and (p/'.mcp.json').is_file() and (p/'server.mjs').is_file()]
    if not candidates:raise DesktopMcpError('Installed official codex-app-tools adapter is unavailable')
    def version(p):return tuple(int(x) if x.isdigit() else 0 for x in p.name.split('.'))
    return max(candidates,key=version)

class DesktopMcp:
    def __init__(self,caller_context,log_dir,*,timeout=30,plugin=None):
        if str(uuid.UUID(caller_context))!=caller_context:raise ValueError('Real registered caller context UUID required')
        self.context=caller_context;self.timeout=timeout;self.seq=0;self.lock=threading.Lock();self.replies=queue.Queue();self.closed=False
        self.plugin=Path(plugin) if plugin else plugin_directory()
        cfg=json.loads((self.plugin/'.mcp.json').read_text(encoding='utf-8-sig'))['mcpServers']['codex_app']
        self.log_dir=Path(log_dir);self.log_dir.mkdir(parents=True,exist_ok=True)
        self.stderr=(self.log_dir/('native-mcp-'+uuid.uuid4().hex+'.stderr.log')).open('w',encoding='utf8')
        env={**os.environ,**cfg.get('env',{})}
        self.proc=subprocess.Popen([cfg['command'],*cfg.get('args',[])],cwd=cfg.get('cwd',str(self.plugin)),env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,text=True,encoding='utf8',bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        threading.Thread(target=self._reader,daemon=True).start()
        try:
            self.rpc('initialize',{'protocolVersion':'2025-03-26','clientInfo':{'name':'deepseek-handoff-host','version':'1'},'capabilities':{}})
            self.notify('notifications/initialized')
            self.tools={x['name'] for x in self.rpc('tools/list',{}).get('tools',[])}
        except BaseException:self.close();raise

    def _reader(self):
        for line in self.proc.stdout:
            try:self.replies.put(json.loads(line))
            except ValueError:continue
        self.replies.put({'_eof':True})

    def notify(self,method,params=None):
        value={'jsonrpc':'2.0','method':method}
        if params is not None:value['params']=params
        self.proc.stdin.write(json.dumps(value)+'\n');self.proc.stdin.flush()

    def rpc(self,method,params):
        with self.lock:
            if self.closed:raise DesktopMcpError('Desktop MCP client closed')
            self.seq+=1;rid=self.seq
            value={'jsonrpc':'2.0','id':rid,'method':method,'params':params}
            sent=False
            try:
                self.proc.stdin.write(json.dumps(value)+'\n');self.proc.stdin.flush();sent=True
                end=time.monotonic()+self.timeout
                while True:
                    remaining=end-time.monotonic()
                    if remaining<=0:raise TimeoutError('Desktop MCP response deadline')
                    reply=self.replies.get(timeout=remaining)
                    if reply.get('_eof'):raise EOFError('Desktop MCP stream ended')
                    if reply.get('id')!=rid:continue
                    if 'error' in reply:raise DesktopMcpError(json.dumps(reply['error'],ensure_ascii=False),request_sent=sent)
                    return reply['result']
            except DesktopMcpError:raise
            except (OSError,EOFError,TimeoutError,queue.Empty) as e:
                raise DesktopMcpError(str(e) or type(e).__name__,request_sent=sent) from e

    def call(self,name,arguments):
        if name not in self.tools:raise DesktopMcpError('Required native tool not exposed: '+name)
        result=self.rpc('tools/call',{'name':name,'arguments':arguments,
            '_meta':{'openai/threadId':self.context,'openai/toolCallId':'dsh-host-'+uuid.uuid4().hex}})
        if result.get('isError'):
            texts=[x.get('text','') for x in result.get('content',[]) if x.get('type')=='text']
            raise DesktopMcpError('\n'.join(texts)[:1500],request_sent=True)
        return result

    def read_thread(self,thread):
        result=self.call('read_thread',{'threadId':thread,'turnLimit':1,'includeOutputs':False,'maxOutputCharsPerItem':6000})
        for block in result.get('content',[]):
            if block.get('type')!='text':continue
            try:value=json.loads(block.get('text',''))
            except ValueError:continue
            if isinstance(value,dict) and value.get('thread',{}).get('id')==thread:return value
        raise DesktopMcpError('Native read_thread returned no matching serialized thread',request_sent=True)

    def close(self):
        if self.closed:return
        self.closed=True
        try:self.proc.stdin.close()
        except OSError:pass
        try:self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:self.proc.terminate();self.proc.wait(timeout=5)
        self.stderr.close()

    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
