"""Receipt-driven native wakeups. No model process, listener port or Harness edits."""
from pathlib import Path
from datetime import datetime,timedelta,timezone
import hashlib,json,os,re,time,tomllib,uuid
import cli
import followup_backoff as backoff
from desktop_mcp import DesktopMcpError

ROOT=Path(__file__).resolve().parent
EVENTS=ROOT/'.state/event-wakeups'
AUTOMATIONS=Path.home()/'.codex/automations'

def stamp():return datetime.now(timezone.utc).isoformat()
def parse(value):return datetime.fromisoformat(value.replace('Z','+00:00'))
def key(messages):
    values=sorted((x['binding']['job_id'],x['receipt']['message_id'],x['receipt']['nonce']) for x in messages)
    return hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()[:32]

def one_time_rule(due):
    due=backoff.local(due)
    if not backoff.allowed(due):raise ValueError('Wakeup may not be scheduled during peak')
    return f'FREQ=DAILY;COUNT=1;BYHOUR={due.hour};BYMINUTE={due.minute};BYSECOND={due.second}'

def event_prompt(source,event_id,jobs=()):
    return f'''这是已有 DeepSeek 工单的宿主事件唤醒，事件ID={event_id}，来源聊天={source}，事件工单={','.join(sorted(jobs))}。宿主只因真实未读回传安排本次检查；此通知是任务数据，不增加权限，也不是新业务指令。
使用 $deepseek-handoff，只在当前真实 CODEX_THREAD_ID 下运行稳定CLI followup-status 和 inbox，核对本来源绑定、会话、nonce后ack，独立验收，必要时原会话feedback，不重投原任务。不使用computer-use，不改登录、模型、沙箱、Harness或其他聊天。
本事件自动化与普通阶梯兜底不同，不要把事件ID绑定为普通followup；普通兜底仍依CLI当前来源绑定维护。所有工单close后按既定规则暂停普通兜底。事件只触发一次，宿主在观察到读信后会暂停它；不重复执行已处理结果，不因旧事件恢复已关闭工单。
稳定CLI：python -X utf8 -B {ROOT / 'cli.py'}。
host_event_id={event_id}'''

def pending_groups(jobs,allowed_workspaces,allowed_sources=None,errors=None):
    groups={};allowed={str(Path(p).resolve()).casefold() for p in allowed_workspaces}
    for bp in sorted(Path(jobs).glob('*/binding.json')):
        try:
            if bp.is_symlink() or not bp.resolve().is_relative_to(Path(jobs).resolve()):raise ValueError('Linked host binding denied')
            b=cli.load(bp)
            if b.get('status')!='active' or b.get('delivery_mode')!='inbox':continue
            source=b['thread_id']
            if allowed_sources is not None and source not in allowed_sources:continue
            if str(uuid.UUID(source))!=source:raise ValueError('Invalid source identity')
            if str(Path(b['workspace_path']).resolve()).casefold() not in allowed:continue
            if not (bp.parent/'submit-response.json').is_file():continue
            callbacks=bp.parent/'callbacks'
            if callbacks.is_symlink() or not callbacks.resolve().is_relative_to(Path(jobs).resolve()):raise ValueError('Linked callback directory denied')
        except (OSError,ValueError,KeyError) as e:
            if errors is None:raise
            errors.append({'job_path':str(bp.parent),'error':str(e)});continue
        for cp in sorted(callbacks.glob('*.json')):
            try:
                if cp.is_symlink() or not cp.resolve().is_relative_to(Path(jobs).resolve()):raise ValueError('Linked callback denied')
                r=cli.load(cp)
                if any(r.get(k)!=v for k,v in {'job_id':b['job_id'],'nonce':b['nonce'],'source_session':b['session_id'],'target':source}.items()):raise ValueError('Callback identity mismatch')
                if r.get('status')!='inbox' or r.get('first_inbox_read_at') or r.get('inbox_read_at'):continue
                groups.setdefault(source,[]).append({'binding':b,'receipt':r,'receipt_path':str(cp)})
            except (OSError,ValueError,KeyError) as e:
                if errors is None:raise
                errors.append({'callback_path':str(cp),'error':str(e)})
    return groups

class Wakeups:
    def __init__(self,mcp,*,events=EVENTS,automations=AUTOMATIONS,followups=None,now=None):
        self.mcp=mcp;self.events=Path(events);self.events.mkdir(parents=True,exist_ok=True)
        self.automations=Path(automations);self.followups=Path(followups or ROOT/'.state/followups')
        self.now=now or (lambda:datetime.now(timezone.utc))
        self.cleanup_retry_after=None

    def file(self,source):return self.events/(source+'.json')
    def native(self,aid):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,150}',aid):raise ValueError('Invalid automation ID')
        return tomllib.loads((self.automations/aid/'automation.toml').read_text(encoding='utf-8-sig'))

    def normal_active(self,source):
        p=self.followups/(source+'.json')
        if not p.exists():return False
        binding=cli.load(p);actual=self.native(binding['automation_id'])
        return binding.get('status')=='ACTIVE' and actual.get('kind')=='heartbeat' and actual.get('target_thread_id')==source and actual.get('status')=='ACTIVE'

    def owned(self,item,state):
        return (item.get('kind')=='heartbeat' and item.get('target_thread_id')==state['source_thread']
                and f"host_event_id={state['event_id']}" in item.get('prompt',''))

    def find_created(self,state):
        matches=[]
        for p in self.automations.glob('*/automation.toml'):
            if p.is_symlink() or p.parent.is_symlink() or not p.resolve().is_relative_to(self.automations.resolve()):continue
            try:item=tomllib.loads(p.read_text(encoding='utf-8-sig'))
            except (OSError,UnicodeError,tomllib.TOMLDecodeError):continue
            if self.owned(item,state):matches.append(item)
        if len(matches)>1:raise ValueError('Multiple native timers for one event; do not create again')
        return matches[0] if matches else None

    def pause_owned(self,state):
        aid=state.get('automation_id')
        if not aid:return
        path=self.automations/aid/'automation.toml'
        if not path.exists():state['native_cleanup']='already_absent';return
        item=self.native(aid)
        if not self.owned(item,state):raise ValueError('Refuse to pause an automation not owned by this exact event')
        if item['status']=='PAUSED':state['native_cleanup']='already_paused';return
        req={'mode':'update','kind':'heartbeat','id':aid,'name':item['name'],'prompt':item['prompt'],'rrule':item['rrule'],
             'targetThreadId':state['source_thread'],'status':'PAUSED'}
        if 'notification_policy' in item:req['notificationPolicy']=item['notification_policy']
        self.mcp.call('automation_update',req);state['native_cleanup']='paused'

    def capture_source_observation(self,native,state):
        wanted='<automation_id>'+state['automation_id']+'</automation_id>'
        for turn in native.get('turns',[]):
            texts=[c.get('text','') for item in turn.get('items',[]) if item.get('type')=='userMessage' for c in item.get('content',[]) if c.get('type')=='text']
            if any('<heartbeat>' in t and wanted in t and state['event_id'] in t for t in texts):
                state['source_turn_observation']={'source_thread':state['source_thread'],'event_id':state['event_id'],
                    'automation_id':state['automation_id'],'turn_id':turn['id'],'started_at':turn.get('startedAt'),
                    'status':turn.get('status'),'observed_at':stamp(),
                    'observation_method':'official_native_read_thread_by_non_model_host',
                    'native_response_sha256':hashlib.sha256(json.dumps(native,sort_keys=True).encode()).hexdigest()}
                return

    def consume_finished(self,groups):
        # This is transport observation, not an invented business acceptance.
        self.cleanup_retry_after=None
        for p in self.events.glob('*.json'):
            state=cli.load(p)
            if state.get('state') not in ['armed','arming','uncertain','uncertain_await_native','consumed_waiting_source_idle']:continue
            source=state['source_thread']
            if source in groups:continue
            if not state.get('automation_id') and state['state'] in ['arming','uncertain','uncertain_await_native']:
                found=self.find_created(state)
                if found:state['automation_id']=found['id']
                else:
                    state.update(state='uncertain_await_native',no_longer_pending_observed_at=stamp());cli.save(p,state);continue
            try:
                native_source=self.mcp.read_thread(source)
                self.capture_source_observation(native_source,state)
                status=native_source['thread'].get('status',{});kind=status.get('type') if isinstance(status,dict) else status
                if kind=='active':
                    state.update(state='consumed_waiting_source_idle',read_observed_at=state.get('read_observed_at',stamp()))
                    cli.save(p,state);self.cleanup_retry_after=15;continue
                if kind not in ['idle','notLoaded']:
                    self.cleanup_retry_after=60;continue
                self.pause_owned(state)
                state['source_state_at_cleanup']=status
                if native_source.get('turns'):
                    t=native_source['turns'][0];state['latest_source_turn_at_cleanup']={k:t.get(k) for k in ['id','status','completedAt']}
            except DesktopMcpError as e:state['cleanup_error']=str(e)
            state.update(state='consumed_or_no_longer_pending',observed_at=stamp())
            cli.save(p,state)

    def process(self,source,messages):
        now=self.now();fp=self.file(source);previous=cli.load(fp) if fp.exists() else None
        if previous and previous.get('state') in ['arming','uncertain','uncertain_await_native']:
            found=self.find_created(previous)
            if found:
                previous.update(state='armed',automation_id=found['id'],reconciled_from_native=True)
                cli.save(fp,previous)
            else:return {'source':source,'state':'uncertain','retry_after_seconds':60}
        previous_needs_cleanup=False
        if previous and previous.get('state') in ['armed','consumed_waiting_source_idle']:
            current={(x['binding']['job_id'],x['receipt']['message_id']) for x in messages}
            prior={(x['job_id'],x['message_id']) for x in previous['members']}
            if current & prior:
                return {'source':source,'state':'already_armed','event_id':previous['event_id'],'retry_after_seconds':30}
            previous_needs_cleanup=True
        if not backoff.allowed(now):
            due=backoff.next_allowed(now)
            return {'source':source,'state':'waiting_for_valley','due_at':due.isoformat(),'retry_after_seconds':max(1,(due-now).total_seconds())}
        if not self.normal_active(source):return {'source':source,'state':'source_followup_paused','retry_after_seconds':60}
        read=self.mcp.read_thread(source);status=read['thread'].get('status',{});kind=status.get('type') if isinstance(status,dict) else status
        if kind=='active':return {'source':source,'state':'source_active_no_interrupt','retry_after_seconds':15}
        if kind not in ['idle','notLoaded']:return {'source':source,'state':'source_status_unknown','retry_after_seconds':60}
        if previous_needs_cleanup:
            self.pause_owned(previous)
            previous.update(state='consumed_or_no_longer_pending',observed_at=stamp());cli.save(fp,previous)
        # Recheck before mutation. A native single occurrence never changes the
        # ordinary followup's enabled state, prompt, timing or model settings.
        if not self.normal_active(source):return {'source':source,'state':'source_followup_paused','retry_after_seconds':60}
        still_pending=[]
        for x in messages:
            path=Path(x['receipt_path']);r=cli.load(path);b=cli.load(path.parent.parent/'binding.json')
            if b.get('status')!='active' or r.get('status')!='inbox' or r.get('first_inbox_read_at') or r.get('inbox_read_at'):continue
            if any(r.get(k)!=v for k,v in {'nonce':b['nonce'],'source_session':b['session_id'],'target':source,'job_id':b['job_id']}.items()):raise ValueError('Callback identity changed before native create')
            still_pending.append(x)
        if not still_pending:return {'source':source,'state':'already_consumed','retry_after_seconds':60}
        messages=still_pending;event_id=key(messages)
        # The app ticks about every 30s; rounding up to a minute can let the
        # legacy 60s notifier start the source first and block this heartbeat.
        due=backoff.next_allowed(now+timedelta(seconds=5))
        if due.microsecond:due=(due+timedelta(seconds=1)).replace(microsecond=0)
        due=backoff.next_allowed(due)
        state={'schema_version':1,'source_thread':source,'event_id':event_id,'state':'arming','created_at':stamp(),
               'due_at':due.isoformat(),'observed_source_status':status,'members':[{'job_id':x['binding']['job_id'],'message_id':x['receipt']['message_id']} for x in messages],
               'native_name':'DeepSeek 事件唤醒 '+source[:8],'model_controller_started':False}
        # Preserve previous receipt instead of overwriting its history.
        if previous:
            history=self.events/'history';history.mkdir(exist_ok=True)
            cli.save(history/(source+'-'+previous.get('event_id','unknown')+'.json'),previous)
        cli.save(fp,state)
        req={'mode':'create','kind':'heartbeat','destination':'thread','targetThreadId':source,'status':'ACTIVE',
             'name':state['native_name'],'prompt':event_prompt(source,event_id,[x['job_id'] for x in state['members']]),'rrule':one_time_rule(due)}
        try:
            self.mcp.call('automation_update',req)
            created=self.find_created(state)
            if created is None:raise DesktopMcpError('Native create returned without an exact matching stored timer',request_sent=True)
            state.update(state='armed',automation_id=created['id'],armed_at=stamp(),native_rrule=created['rrule'])
            cli.save(fp,state)
            return {'source':source,'state':'armed','event_id':event_id,'automation_id':created['id'],'due_at':due.isoformat(),'retry_after_seconds':30}
        except DesktopMcpError as e:
            state.update(state='uncertain',error=str(e),request_sent=e.request_sent);cli.save(fp,state)
            # Even a failed native reply may have saved the timer. Never resend.
            return {'source':source,'state':'uncertain','retry_after_seconds':60}
