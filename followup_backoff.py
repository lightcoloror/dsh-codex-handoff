"""Pure calendar/backoff policy; no model, native-tool, file or network operations."""
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
DELAYS = (2, 10, 30, 60, 120)
INSTRUCTION = '最新阶梯兜底（2026-11-03，覆盖历史每两分钟固定轮询说明）：每次完成收件、验收或无新结果检查后，若仍有开放工单，运行稳定CLI的 followup-next，把 update_request 原样交给 automation_update，再 followup-bind 记录真实状态；无新结果按2→10→30→60→120分钟退避，新回传或新任务重置。峰时不轮询，周末全天谷时。全部close后只暂停本项。仅原生工具应用调度，不直接写automation.toml；不要在模型内睡眠等下一次检查。'
# Encode the Beijing policy in UTC. The installed app's timezone-specific
# DTSTART path was observed adding eight hours to the intended first run.
# UTC 16-23 is the following Beijing morning on every day, including Sun/Mon.
BASE_RULE = ('RRULE:FREQ=MINUTELY;INTERVAL=120;BYHOUR=16,17,18,19,20,21,22,23;BYSECOND=0\n'
             'RRULE:FREQ=MINUTELY;INTERVAL=120;BYDAY=MO,TU,WE,TH,FR;BYHOUR=0,4,5,10,11,12,13,14,15;BYSECOND=0\n'
             'RRULE:FREQ=MINUTELY;INTERVAL=120;BYDAY=SA,SU;BYHOUR=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15;BYSECOND=0')

def local(at):
    if at.tzinfo is None: raise ValueError('Aware timestamp required')
    return at.astimezone(BJ)

def allowed(at):
    at=local(at)
    return at.weekday()>=5 or not (9<=at.hour<12 or 14<=at.hour<18)

def next_allowed(at):
    at=local(at)
    if allowed(at):return at
    return at.replace(hour=12 if at.hour<12 else 18,minute=0,second=0,microsecond=0)

def recurrence(due):
    due=local(due)
    if not allowed(due):raise ValueError('First check cannot be in peak time')
    # An explicit first occurrence, then only a two-hour safety net if the
    # source fails before rescheduling. A missed checkpoint never leaves a
    # perpetual two-minute timer behind.
    return ('DTSTART:'+due.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'\n'
            +BASE_RULE.replace('BYSECOND=0','BYSECOND='+str(due.second)))

def plan(previous, *, source, jobs, activity, at, reset=False):
    at=local(at)
    if not jobs:raise ValueError('No open jobs: pause the source followup instead')
    changed=(previous is None or previous.get('activity')!=activity or previous.get('jobs')!=sorted(jobs))
    if reset or changed:
        stage=0;checks=0;reason='new_activity' if changed else 'explicit_reset'
    elif not allowed(at):
        # A late queued turn in peak time must not consume a backoff step.
        if datetime.fromisoformat(previous['due_at'])<at:
            deferred=dict(previous);due=next_allowed(at).replace(microsecond=0)
            deferred.update(due_at=due.isoformat(),rrule=recurrence(due),reason='peak_deferred')
            return deferred
        return dict(previous)
    elif at<datetime.fromisoformat(previous['due_at']):
        return dict(previous)
    else:
        stage=min(int(previous['stage'])+1,len(DELAYS)-1)
        checks=int(previous.get('empty_checks',0))+1;reason='no_new_result'
    due=next_allowed(at+timedelta(minutes=DELAYS[stage]))
    # Round up, not down, so native-tool latency does not create a past timer.
    if due.microsecond:due=(due+timedelta(seconds=1)).replace(microsecond=0)
    return {'schema_version':1,'thread_id':source,'jobs':sorted(jobs),'activity':activity,
            'stage':stage,'delay_minutes':DELAYS[stage],'empty_checks':checks,
            'planned_at':at.isoformat(),'due_at':due.isoformat(),'reason':reason,
            'rrule':recurrence(due),'safety_net_minutes':120}
