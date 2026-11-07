"""Opt-in bounded current-chat supervision over the existing authenticated bridge.

No scheduler/service/model configuration changes. Durable callbacks, owner checks,
saved request identity and explicit reviewer ACK/close use the existing CLI.
"""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import os
import time
import json
from pathlib import Path
import stage_chain

@contextmanager
def exclusive(path):
    with path.open('a+b') as handle:
        if path.stat().st_size==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def run(cli,job_id,thread=None,timeout_seconds=900,watch_only=False,emit=None,authorization_ref=None,after_turn=0):
    b=cli.owner(job_id,thread);directory=cli.job_dir(job_id);spec=cli.load(directory/'task-spec.json');auth=spec.get('foreground_supervision') or {}
    if b['status']!='active':raise ValueError('Only active jobs may be supervised')
    if type(after_turn) is not int or after_turn<0 or (after_turn and not watch_only):raise ValueError('after_turn requires watch-only and a nonnegative completed turn number')
    # Existing native user authorization may be referenced without revising the
    # frozen business inputs or spending the task's one prepared-revision slot.
    # The reference records evidence; it does not grant new authority itself.
    if authorization_ref is not None:
        auth={'authorized':True,'source_thread':b['thread_id'],
              'authorization_ref':str(Path(authorization_ref).resolve()),'max_seconds':timeout_seconds}
    if auth.get('authorized') is not True or auth.get('source_thread')!=b['thread_id'] or not auth.get('authorization_ref'):raise ValueError('Explicit current-source foreground authorization is required')
    allowed=auth.get('max_seconds',0)
    if type(allowed) is not int or not 30<=timeout_seconds<=min(1800,allowed):raise ValueError('Foreground window must be 30..1800 seconds within its authorization')
    if not Path(auth['authorization_ref']).is_file():raise ValueError('Foreground authorization reference is missing')
    # A spec field is a reference to actual caller authorization, never authority
    # for the model to change settings or create follow-up work.
    if emit is None:emit=lambda value:print(json.dumps(value,ensure_ascii=False),flush=True)
    with exclusive(directory/'foreground.lock'):
        chain=stage_chain.read(cli,b) if 'stages' in spec or (directory/stage_chain.FILE).exists() else None
        expected=None
        if chain:
            if chain['pending']:raise ValueError('Reconcile the uncertain stage submission before supervision')
            stage=stage_chain.view(chain);expected=stage['callback_id']
            if after_turn and after_turn!=stage['after_turn']:raise ValueError('after_turn differs from the recorded stage boundary')
            after_turn=stage['after_turn']
            if after_turn and not watch_only:raise ValueError('Later stages require --watch-only supervision')
        start=time.monotonic();deadline=start+timeout_seconds;record={'job_id':job_id,'source_thread':b['thread_id'],'session_id':b['session_id'],'pid':os.getpid(),'started_at':cli.now(),'deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=timeout_seconds)).isoformat(),'mode':'foreground_current_chat','authorization_ref':auth['authorization_ref'],'authorization_sha256':cli.digest(auth['authorization_ref']),'state':'running','automatic_ack':False,'automatic_close':False,'cold_chat_wakeup_claimed':False}
        receipt_path=directory/'foreground-supervision.json';cli.save(receipt_path,record)
        def finish(state,**extra):
            record.update(state=state,ended_at=cli.now(),**extra);cli.save(receipt_path,record);return dict(record)
        try:
            current=cli.status(job_id,thread)
            if not watch_only and not (directory/'submit-response.json').exists():
                if (directory/'foreground-submit-intent.json').exists():raise ValueError('Prior submit outcome uncertain; inspect actual state and use --watch-only, never blindly resend')
                if current['status']!='idle' or current['last_turn'] is not None:raise ValueError('Remote execution already exists without a local submission receipt; reconcile before submitting')
                cli.save(directory/'foreground-submit-intent.json',{'at':cli.now(),'source_thread':b['thread_id'],'session_id':b['session_id'],'pid':os.getpid(),'deadline_at':record['deadline_at'],'request_sha256':cli.digest(directory/'submit-request.json')})
                accepted=cli._append_prepared(job_id);record['submission_response']=accepted;cli.save(receipt_path,record);emit({'event':'submitted','job_id':job_id,'session_id':b['session_id'],'response':accepted})
            last=None;completed_without_callback=None
            while time.monotonic()<deadline:
                current=cli.status(job_id,thread)
                messages=cli.peek_job_callbacks(job_id,thread,expected=expected) if expected else cli.peek_job_callbacks(job_id,thread)
                changed=(current['status'],json.dumps(current.get('last_turn'),sort_keys=True),tuple(messages))
                if changed!=last:
                    emit({'event':'observed','job_id':job_id,'status':current['status'],'last_turn':current.get('last_turn'),'cursor':current['cursor'],'observed':current.get('observed'),'callback_ids':messages});last=changed
                last_turn=current.get('last_turn') or {};reason=last_turn.get('reason')
                newer_turn=after_turn==0 or last_turn.get('turn',0)>after_turn
                if newer_turn and current['status']=='idle' and reason=='completed':
                    if messages:return finish('ready_for_independent_review',producer=current,callback_ids=messages)
                    completed_without_callback=completed_without_callback or time.monotonic()
                    if time.monotonic()-completed_without_callback>=10:return finish('producer_completed_callback_missing',producer=current)
                if newer_turn and reason in ('failed','cancelled','error'):return finish('producer_failed',producer=current)
                if current['status']=='attention':
                    return finish('input_required',producer=current,note='Inspect native attention evidence; no approval was granted.')
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                cli.save(receipt_path,{**record,'last_observation_at':cli.now(),'producer':current,'callback_ids':messages})
                wait=cli.request('POST','/tasks/wait',{'taskId':job_id,'waitMs':max(1,min(5000,int(remaining*1000))),'condition':{'sinceSeq':current['cursor'],'on':['turn-end','input-request']}})
                if any(e.get('kind')=='permission' for e in wait.get('events',[])):
                    return finish('input_required',producer=current,note='Use the existing native approval path; no approval was granted.')
                if wait.get('timedOut') is False and not wait.get('events'):
                    time.sleep(0.25)  # An immediately satisfied native wait must not spin.
            return finish('window_ended_pending',producer=current,note='Same job and durable inbox retained; caller must review/resume in this chat. No automatic cold-chat scheduling.')
        except Exception as error:
            finish('supervisor_error',error=str(error));raise
