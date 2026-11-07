"""Bounded, source-owned usage observations; no model, billing or quota API calls."""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import uuid

CODEX_SESSIONS = Path.home()/'.codex/sessions'
TAIL_BYTES = 8*1024*1024
FILE = 'usage-ledger.json'
UNKNOWN = 'unknown'
DS_KEYS = ('inputTokens','cacheReadTokens','cacheWriteTokens','outputTokens','totalTokens')
CX_KEYS = ('input_tokens','cached_input_tokens','output_tokens','total_tokens')
CX_EXTRA = ('cache_write_input_tokens','reasoning_output_tokens')


def _counts(raw, required, optional=()):
    if not isinstance(raw,dict) or any(k not in raw for k in required):
        return None
    values={k:raw[k] for k in (*required,*optional) if k in raw}
    return values if all(type(v) is int and v>=0 for v in values.values()) else None


def _sums(rows):
    if not rows:return UNKNOWN
    keys=set.union(*(set(r) for r in rows))
    return {k:sum(r[k] for r in rows) if all(k in r for r in rows) else UNKNOWN for k in sorted(keys)}


def deepseek(trace, b):
    if not trace:return {'coverage':UNKNOWN,'observed_tokens':UNKNOWN,'metered_messages':UNKNOWN}
    if trace.get('taskId')!=b['task_id'] or trace.get('sessionId')!=b['session_id']:
        raise ValueError('DeepSeek usage transcript identity mismatch')
    items={};conflict=False
    for item in trace.get('items',[]):
        seq=item.get('sessionSeq')
        if type(seq) is not int or seq<0:continue
        if seq in items and items[seq]!=item:conflict=True
        items[seq]=item
    seqs=sorted(items); rows=[]; unmetered=0
    for item in items.values():
        if item.get('kind')!='message' or item.get('role')!='assistant':continue
        u=_counts(item.get('usage'),DS_KEYS)
        if u is None or u['totalTokens']!=sum(u[k] for k in DS_KEYS[:-1]):
            unmetered+=1;continue
        rows.append(u)
    incomplete=(conflict or trace.get('truncatedBefore') is not None or not seqs or seqs[0]>1
                or any(b_!=a+1 for a,b_ in zip(seqs,seqs[1:])) or seqs[-1]<trace.get('cursor',-1))
    return {'coverage':('partial' if incomplete or unmetered else 'recorded_events') if rows else UNKNOWN,
            'observed_tokens':_sums(rows),'metered_messages':len(rows),'unmetered_messages':unmetered,
            'last_observed_seq':seqs[-1] if seqs else None,'source':'bridge_assistant_message_usage',
            'request_count_is_not_inferred':True}


def _rollout_path(source, supplied=None):
    if supplied is not None:
        path=Path(supplied).resolve()
        if (not path.is_relative_to(CODEX_SESSIONS.resolve()) or not path.name.startswith('rollout-')
                or not path.name.endswith('-'+source+'.jsonl') or not path.is_file()):
            raise ValueError('Use the exact source rollout inside the local Codex sessions directory')
        return path
    ident=uuid.UUID(source)
    if ident.version!=7:return None
    day=datetime.fromtimestamp((ident.int>>80)/1000,timezone.utc).date()
    candidates=[]
    for offset in (-1,0,1):
        d=day+timedelta(days=offset)
        for path in (CODEX_SESSIONS/f'{d:%Y/%m/%d}').glob('rollout-*-'+source+'.jsonl'):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(CODEX_SESSIONS.resolve()):
                candidates.append(path)
    if len(candidates)>1:raise ValueError('Multiple source rollouts; specify the exact file')
    return candidates[0] if candidates else None


def _codex_counts(value):
    r=_counts(value,CX_KEYS,CX_EXTRA)
    if (r is None or r['total_tokens']!=r['input_tokens']+r['output_tokens']
            or r['cached_input_tokens']+r.get('cache_write_input_tokens',0)>r['input_tokens']
            or r.get('reasoning_output_tokens',0)>r['output_tokens']):return None
    return r


def codex_rollout(path, source):
    """Read metadata and a bounded tail; attribute only deltas within native turns."""
    with Path(path).open('rb') as handle:
        meta=json.loads(handle.readline(2*1024*1024))
        p=meta.get('payload') or {}
        if meta.get('type')!='session_meta' or p.get('id',p.get('session_id'))!=source:
            raise ValueError('Codex rollout does not belong to the source chat')
        end=handle.seek(0,2);offset=max(0,end-TAIL_BYTES);handle.seek(offset)
        if offset:handle.readline(TAIL_BYTES)
        data=handle.read(TAIL_BYTES)
    rows={}; active=None; previous=None; pending={}; bad=set()
    for line in data.splitlines():
        try: x=json.loads(line)
        except (ValueError,UnicodeError):
            if active:bad.add(active)
            previous=None;continue
        if not isinstance(x,dict) or x.get('type')!='event_msg':continue
        p=x.get('payload') or {};kind=p.get('type');turn=p.get('turn_id')
        if kind=='task_started' and isinstance(turn,str):
            active=turn;pending.setdefault(turn,[])
        elif kind in ('task_complete','turn_aborted') and turn==active and active is not None:
            samples=pending.pop(active,[])
            rows[active]={'coverage':'partial' if active in bad else 'recorded_events' if samples else UNKNOWN,
                          'observed_tokens':_sums(samples),'usage_increments':len(samples),'settled':True}
            active=None
        elif kind=='token_count' and p.get('info') is not None:
            info=p['info']; total=_codex_counts(info.get('total_token_usage')) if isinstance(info,dict) else None
            last=_codex_counts(info.get('last_token_usage')) if isinstance(info,dict) else None
            delta=None
            if total is not None:
                if previous is None:
                    if total==last and not offset:delta=total
                elif total.keys()==previous.keys():
                    delta=_codex_counts({k:total[k]-previous[k] for k in total})
            previous=total
            if active:
                if delta is None:bad.add(active)
                elif delta['total_tokens']>0:pending[active].append(delta)
    if active:
        samples=pending.get(active,[])
        rows[active]={'coverage':'pending','observed_tokens':_sums(samples),'usage_increments':len(samples),'settled':False}
    return rows,active


def _costs(value):
    if not isinstance(value,dict) or set(value)-{'codex','deepseek'}:
        raise ValueError('Cost evidence must be keyed by codex and/or deepseek')
    for entry in value.values():
        if not isinstance(entry,dict) or set(entry)!={'amount','currency','source','evidence'}:
            raise ValueError('A measured cost needs amount, currency, source and evidence')
        amount=entry['amount']; currency=entry['currency']; evidence=entry['evidence']
        if (type(amount) not in (int,float) or not math.isfinite(amount) or amount<0 or not isinstance(currency,str)
                or not re.fullmatch(r'[A-Z]{3}',currency) or entry['source'] not in ('actual_invoice','actual_account_delta')
                or not isinstance(evidence,str) or not evidence.strip()):
            raise ValueError('Do not record estimated charges as actual cost')
    return value


def sync(cli,b,*,rollout=None,turns=(),link_active=False,cost_file=None,strict=False,legacy_rework=None):
    from foreground_run import exclusive
    d=cli.job_dir(b['job_id']);path=d/FILE
    identity={k:b[k] for k in ('job_id','thread_id','session_id')}
    with exclusive(d/'usage.lock'):
        ledger=cli.load(path) if path.exists() else {**identity,'schema':1,'codex_turns':{},'account_cost':{'codex':UNKNOWN,'deepseek':UNKNOWN}}
        if any(ledger.get(k)!=v for k,v in identity.items()):raise ValueError('Usage ledger binding mismatch')
        if cost_file:ledger['account_cost'].update(_costs(cli.load(cost_file)))
        if any(str(uuid.UUID(t))!=t for t in turns):raise ValueError('Supply full native Codex turn IDs')
        selected=set(ledger['codex_turns'])|set(turns);rows={};active=None
        if selected or link_active or rollout:
            try:
                log=_rollout_path(b['thread_id'],rollout)
                if log:
                    rows,active=codex_rollout(log,b['thread_id'])
                ledger['codex_source_status']='observed' if log else UNKNOWN
            except (OSError,ValueError,TypeError,KeyError):
                if strict:raise
                ledger['codex_source_status']=UNKNOWN
        if link_active and active:selected.add(active)
        for t in sorted(selected):
            previous=ledger['codex_turns'].get(t)
            sample=rows.get(t)
            if sample and (not previous or not previous.get('settled') or sample['coverage']=='recorded_events'):
                ledger['codex_turns'][t]=sample
            elif previous is None:
                ledger['codex_turns'][t]={'coverage':UNKNOWN,'observed_tokens':UNKNOWN,'settled':False}
        records=list(ledger['codex_turns'].values())
        measured=[r['observed_tokens'] for r in records if isinstance(r['observed_tokens'],dict)]
        ledger['codex']={'coverage':('recorded_events' if all(r['coverage']=='recorded_events' for r in records) else 'partial') if measured else UNKNOWN,
                         'observed_tokens':_sums(measured),
                         'linked_turn_count':len(records),'pending_turn_count':sum(r['coverage']=='pending' for r in records),
                         'attribution':'whole_source_turn_inclusive; may include independent work; deduplicate by source and turn across jobs',
                         'quota_attributed_to_job':UNKNOWN}
        trace=d/'transcript.json'
        ledger['deepseek']=deepseek(cli.load(trace) if trace.exists() else None,b)
        chain=d/'stage-chain.json'; phases=cli.load(chain)['rounds'] if chain.exists() else []
        final=d/'final-receipt.json'; review=cli.load(final) if final.exists() else {}
        legacy_count=legacy_rework if type(legacy_rework) is int and legacy_rework>=0 else review.get('rework_rounds')
        ledger['workflow']={'recorded_legacy_feedback_requests':len(list(d.glob('feedback-*.json'))),
                            'normal_stage_advances':sum(r['kind']=='advance' for r in phases),
                            'rework_rounds':sum(r['kind']=='rework' for r in phases) if phases else legacy_count if type(legacy_count) is int and legacy_count>=0 else UNKNOWN}
        ledger['updated_at']=cli.now();cli.save(path,ledger)
        return ledger
