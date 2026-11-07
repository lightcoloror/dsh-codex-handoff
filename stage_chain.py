"""Source-owned, independently reviewed stages within one existing DSH job."""
import json
import os
from pathlib import Path
import re

FILE = 'stage-chain.json'


def plan(spec):
    if 'stages' not in spec:
        return None
    stages = spec['stages']
    if not isinstance(stages, list) or not 2 <= len(stages) <= 16:
        raise ValueError('A chain needs 2..16 planned stages')
    seen = set()
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {'id', 'goal', 'acceptance'}:
            raise ValueError('Each stage needs exactly id, goal and acceptance')
        sid, goal, checks = stage['id'], stage['goal'], stage['acceptance']
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,35}', sid) or sid in seen:
            raise ValueError('Stage IDs must be short and unique')
        seen.add(sid)
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError('A stage needs a bounded goal')
        if not isinstance(checks, list) or not 1 <= len(checks) <= 12 or any(not isinstance(c, str) or not c.strip() or len(c) > 500 for c in checks):
            raise ValueError('A stage needs bounded independent acceptance checks')
    return stages


def stage_prompt(stage, message_id, corrections=None):
    text = ('Execute only this planned stage in the existing authorized task and workspace. '
            'Keep earlier artifacts as evidence; do not proceed to another stage without a new owner instruction.\n'
            f"Stage: {stage['id']}\nGoal: {stage['goal']}\nAcceptance:\n" +
            '\n'.join('- ' + check for check in stage['acceptance']))
    if corrections:
        text += '\nAddress these reviewed corrections within the same stage and scope:\n' + '\n'.join('- ' + x for x in corrections)
    return text + (f'\nPublish this stage result with codex_handoff_publish, using message_id={message_id} '
                   'and the original job ID and nonce from handoff.json. Summarize artifacts, checks and blockers; keep full logs in the task directory. Then finish this turn.')


def init(cli, binding, stages):
    job = binding['job_id']
    cli.save(cli.job_dir(job)/FILE, {'schema': 1, 'job_id': job, 'source_thread': binding['thread_id'],
        'workspace_id': binding['workspace_id'], 'plan_sha256': cli.spec_digest(stages), 'stages': stages,
        'rounds': [{'index': 0, 'attempt': 1, 'kind': 'initial', 'after_turn': 0,
                    'callback_id': 'result-1', 'request_id': 'submit-'+job}], 'pending': None})


def read(cli, binding):
    directory = cli.job_dir(binding['job_id']); p = directory/FILE
    spec = cli.load(directory/'task-spec.json')
    stages = plan(spec)
    if stages is None and not p.exists():
        return None
    value = cli.load(p)
    if (stages is None or value['plan_sha256'] != cli.spec_digest(stages) or value['stages'] != stages
            or value['job_id'] != binding['job_id'] or value['source_thread'] != binding['thread_id']
            or value['workspace_id'] != binding['workspace_id']):
        raise ValueError('Stage plan or source/workspace binding changed')
    return value


def remote(cli, b):
    workspace=Path(b['workspace_path']);task_root=Path(b['task_root'])
    if workspace.resolve()!=workspace or task_root.resolve()!=task_root or not task_root.is_relative_to(workspace):
        raise ValueError('Stage workspace or task root is no longer the registered physical path')
    t = cli.request('GET', '/tasks/get', query={'taskId': b['task_id']})['task']
    if (t.get('taskId') != b['task_id'] or t.get('sessionId') != b['session_id']
            or t.get('workspaceId') != b['workspace_id']
            or os.path.normcase(os.path.normpath(t.get('workspacePath') or '')) != os.path.normcase(os.path.normpath(b['workspace_path']))
            or (t.get('external') or {}).get('conversationId') != b['thread_id']):
        raise ValueError('Remote stage task/session/workspace/source binding mismatch')
    observed = t.get('observedConfig') or {}
    if observed and any(observed.get(k) != v for k, v in b['selection'].items()):
        raise ValueError('Stage model/effort differs from the original selection')
    return t


def view(value):
    round_ = value['rounds'][-1]
    return {'job_id': value['job_id'], 'stage': value['stages'][round_['index']]['id'],
            'stage_number': round_['index']+1, 'stage_count': len(value['stages']),
            'attempt': round_['attempt'], 'after_turn': round_['after_turn'], 'callback_id': round_['callback_id'],
            'review': round_.get('review'), 'submission_uncertain': value['pending'] is not None,
            'rework_rounds': sum(r['kind']=='rework' for r in value['rounds'])}


def _verified_events(events, round_, turn):
    starts = [e['sessionSeq'] for e in events if e.get('phase')=='turn-start' and e.get('turn')==turn]
    ends = [e['sessionSeq'] for e in events if e.get('phase')=='turn-completed' and e.get('turn')==turn]
    if len(starts)!=1 or len(ends)!=1 or starts[0]>=ends[0]:
        raise ValueError('Stage needs complete native start and completion evidence')
    selected = [e for e in events if starts[0]<=e['sessionSeq']<=ends[0]]
    if not any(e.get('role')=='user' and e.get('requestId')==round_['request_id'] for e in selected):
        raise ValueError('Native turn does not contain the stage request identity')
    calls = {e['callId'] for e in selected if e.get('phase')=='tool-call' and e.get('tool')=='codex_handoff_publish' and e.get('callId')}
    for e in selected:
        if e.get('phase')!='tool-result' or e.get('callId') not in calls or e.get('failed') is not False:
            continue
        try:
            result = json.loads(e.get('outputPreview',''))
        except (ValueError, TypeError):
            continue
        if isinstance(result,dict) and result.get('message_id')==round_['callback_id'] and result.get('status') in ('inbox','queued'):
            return e['callId']
    raise ValueError('No successful native publisher result for this stage callback')


def review(cli, job, review_file, thread=None):
    from foreground_run import exclusive
    b=cli.owner(job,thread); d=cli.job_dir(job)
    with exclusive(d/'foreground.lock'):
        value=read(cli,b)
        if not value or b['status']!='active' or value['pending'] or (d/'close-intent.json').exists():
            raise ValueError('Stage review requires an active reconciled chain')
        round_=value['rounds'][-1]; review_digest=cli.digest(review_file)
        if round_.get('review'):
            if round_['review']['sha256']!=review_digest:
                raise ValueError('Stage review already recorded; preserve the original evidence')
            return view(value)
        r=cli.load(review_file); findings=r.get('findings'); paths=r.get('artifacts',[])
        if r.get('quality') not in ('accepted','rework') or not isinstance(findings,list) or not 1<=len(findings)<=12 or any(not isinstance(x,str) or not x.strip() or len(x)>500 for x in findings):
            raise ValueError('Stage review needs accepted/rework and bounded independent findings')
        if not isinstance(paths,list) or any(not isinstance(x,str) for x in paths):
            raise ValueError('Stage artifacts must be task-relative paths')
        t=remote(cli,b); terminal=t.get('lastTurn') or {}; turn=terminal.get('turn',0)
        if t['status']!='idle' or terminal.get('reason')!='completed' or type(turn) is not int or turn<=round_['after_turn']:
            raise ValueError('Wait for this stage producer turn to complete')
        if (d/'callback-pending.json').exists():
            raise ValueError('Read and ACK the actual result before stage review')
        p=d/'callbacks'/(round_['callback_id']+'.json')
        receipt=cli.load(p)
        if any(receipt.get(k)!=v for k,v in {'job_id':job,'message_id':round_['callback_id'],'target':b['thread_id'],
                'source_session':b['session_id'],'nonce':b['nonce'],'status':'acknowledged'}.items()):
            raise ValueError('Stage callback receipt does not match the acknowledged result')
        cli.transcript(job,thread)
        trace=cli.load(d/'transcript.json')
        if trace.get('taskId')!=b['task_id'] or trace.get('sessionId')!=b['session_id']:
            raise ValueError('Stage transcript identity mismatch')
        publisher=_verified_events(trace['items'],round_,turn)
        artifacts=[]
        for relative in paths:
            p=cli.inside(Path(b['task_root']),relative)
            if not p.is_file():raise ValueError('Missing reviewed stage artifact')
            artifacts.append({'relative_path':relative,'sha256':cli.digest(p)})
        round_['review']={'quality':r['quality'],'findings':findings,'artifacts':artifacts,'turn':turn,
                          'publisher_call_id':publisher,'sha256':review_digest,'reviewed_at':cli.now()}
        cli.save(d/FILE,value)
        return view(value)


def advance(cli, job, thread=None):
    from foreground_run import exclusive
    b=cli.owner(job,thread); d=cli.job_dir(job)
    with exclusive(d/'foreground.lock'):
        value=read(cli,b)
        if not value or b['status']!='active' or (d/'close-intent.json').exists():
            raise ValueError('Advance requires an open stage chain')
        t=remote(cli,b)
        pending=value['pending']
        if pending:
            # Read-only reconciliation: an uncertain POST is never sent again.
            rounds=[r for r in t.get('rounds',[]) if r.get('requestId')==pending['round']['request_id'] and r.get('accepted') is True]
            if len(rounds)!=1:
                raise ValueError('Stage submission uncertain; keep intent and inspect native evidence without resending')
            value['rounds'].append(pending['round']);value['pending']=None;cli.save(d/FILE,value)
            return {**view(value),'reconciled':True,'session_id':b['session_id']}
        current=value['rounds'][-1]; review_=current.get('review')
        if not review_:
            raise ValueError('Independently review the current stage before advancing')
        if t['status']!='idle' or (t.get('lastTurn') or {}).get('turn')!=review_['turn'] or (t.get('lastTurn') or {}).get('reason')!='completed' or (d/'callback-pending.json').exists():
            raise ValueError('Native task changed since the stage review')
        for artifact in review_['artifacts']:
            p=cli.inside(Path(b['task_root']),artifact['relative_path'])
            if not p.is_file() or cli.digest(p)!=artifact['sha256']:
                raise ValueError('Reviewed stage artifact changed before continuation')
        rework=review_['quality']=='rework'; index=current['index']+(not rework)
        if index==len(value['stages']):
            return {**view(value),'ready_to_close':True,'session_id':b['session_id']}
        attempt=current['attempt']+1 if rework else 1
        message_id=f'stage-{index+1}-attempt-{attempt}'
        if (d/'callbacks'/(message_id+'.json')).exists():
            raise ValueError('Next stage callback identity already exists; reconcile without reuse')
        request_id='chain-'+job+'-'+message_id
        round_={'index':index,'attempt':attempt,'kind':'rework' if rework else 'advance',
                'after_turn':review_['turn'],'request_id':request_id,'callback_id':message_id}
        body={'taskId':job,'requestId':request_id,'kind':'feedback' if rework else 'instruction','mode':'queue',
              'message':stage_prompt(value['stages'][index],message_id,review_['findings'] if rework else None)}
        value['pending']={'round':round_,'body':body,'created_at':cli.now()};cli.save(d/FILE,value)
        response=cli.request('POST','/tasks/append',body)
        if response.get('accepted') is not True or response.get('taskId')!=job or response.get('sessionId')!=b['session_id']:
            raise ValueError('Stage response unconfirmed; reconcile the saved intent without resending')
        value['rounds'].append(round_);value['pending']=None;cli.save(d/FILE,value)
        return {**view(value),'submitted':True,'session_id':b['session_id']}


def close_fields(value, quality, terminal):
    if not value:
        return {}
    if value['pending']:
        raise ValueError('Resolve uncertain stage submission before close')
    reviews=[r['review'] for r in value['rounds'] if r.get('review')]
    accepted={r['index'] for r in value['rounds'] if r.get('review',{}).get('quality')=='accepted'}
    current=value['rounds'][-1].get('review',{})
    if quality=='accepted' and (len(accepted)!=len(value['stages']) or current.get('turn')!=(terminal or {}).get('turn')):
        raise ValueError('All planned stages need independent acceptance in their actual producer turns')
    count=sum(r['kind']=='rework' for r in value['rounds'])
    return {'stage_chain':{'planned':len(value['stages']),'accepted':len(accepted),'reviews':reviews},
            'rework_rounds':count,'first_pass':quality=='accepted' and not count}
