from datetime import datetime,timedelta
import unittest
import followup_backoff as b

class BackoffTests(unittest.TestCase):
    def dt(self,value):return datetime.fromisoformat(value)
    def test_empty_checks_progress_to_two_hours_and_cap(self):
        at=self.dt('2026-11-07T00:00:00+08:00');state=None;delays=[]
        for _ in range(7):
            state=b.plan(state,source='source',jobs=['a'],activity='same',at=at)
            delays.append(state['delay_minutes']);at=self.dt(state['due_at'])
        self.assertEqual(delays,[2,10,30,60,120,120,120])
        self.assertIn('FREQ=HOURLY;INTERVAL=2;',state['rrule'])
        self.assertNotIn('FREQ=MINUTELY',state['rrule'])

    def test_new_result_and_new_job_reset_but_early_checks_do_not_advance(self):
        at=self.dt('2026-11-07T00:00:00+08:00')
        state=b.plan(None,source='s',jobs=['a'],activity='old',at=at)
        self.assertEqual(b.plan(state,source='s',jobs=['a'],activity='old',at=at),state)
        state=b.plan(state,source='s',jobs=['a'],activity='old',at=self.dt(state['due_at']))
        self.assertEqual(state['delay_minutes'],10)
        changed=b.plan(state,source='s',jobs=['a'],activity='new',at=self.dt(state['due_at']))
        self.assertEqual(changed['delay_minutes'],2)
        added=b.plan(state,source='s',jobs=['a','b'],activity='old',at=self.dt(state['due_at']))
        self.assertEqual(added['delay_minutes'],2)

    def test_peak_defers_due_to_noon_and_evening_without_consuming_step(self):
        for now,due in [('2026-11-03T08:59:00+08:00','2026-11-03T12:00:00+08:00'),
                        ('2026-11-03T13:59:00+08:00','2026-11-03T18:00:00+08:00')]:
            state=b.plan(None,source='s',jobs=['a'],activity='old',at=self.dt(now))
            self.assertEqual(state['due_at'],due)
            late=self.dt(now)+timedelta(minutes=5)
            self.assertEqual(b.plan(state,source='s',jobs=['a'],activity='old',at=late),state)

    def test_no_job_requires_pause_and_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):b.plan(None,source='s',jobs=[],activity='x',at=self.dt('2026-11-03T12:00:00+08:00'))
        with self.assertRaises(ValueError):b.allowed(datetime(2026,11,3,12))

    def test_late_peak_turn_rolls_forward_without_an_empty_check(self):
        state=b.plan(None,source='s',jobs=['a'],activity='x',at=self.dt('2026-11-03T08:57:00+08:00'))
        deferred=b.plan(state,source='s',jobs=['a'],activity='x',at=self.dt('2026-11-03T09:03:00+08:00'))
        self.assertEqual(deferred['due_at'],'2026-11-03T12:00:00+08:00')
        self.assertEqual(deferred['stage'],0);self.assertEqual(deferred['empty_checks'],0)

    def test_utc_anchor_and_windows_preserve_beijing_weekday_weekend_boundaries(self):
        from datetime import timezone
        self.assertTrue(b.recurrence(self.dt('2026-11-03T13:37:07+08:00')).startswith('DTSTART:20261103T053707Z\n'))
        rules=[dict(p.split('=',1) for p in line.removeprefix('RRULE:').split(';')) for line in b.BASE_RULE.splitlines()]
        names=['MO','TU','WE','TH','FR','SA','SU']
        # Exhaust a full local week, including both midnight day shifts.
        start=self.dt('2026-11-02T00:00:00+08:00')
        for minute in range(7*24*60):
            at=start+timedelta(minutes=minute);utc=at.astimezone(timezone.utc)
            encoded=any(('BYDAY' not in r or names[utc.weekday()] in r['BYDAY'].split(',')) and str(utc.hour) in r['BYHOUR'].split(',') for r in rules)
            expected=at.weekday()>=5 or at.hour<9 or 12<=at.hour<14 or at.hour>=18
            self.assertEqual(encoded,expected,str(at))

    def test_hourly_safety_net_preserves_first_due_minute_and_second(self):
        from datetime import timezone
        for value in ['2026-11-14T17:41:12+08:00', '2026-11-15T01:42:11+08:00',
                      '2026-11-15T09:27:18+08:00', '2026-11-16T12:00:07+08:00']:
            due=self.dt(value);utc=due.astimezone(timezone.utc)
            lines=b.recurrence(due).splitlines()
            self.assertEqual(lines[0],'DTSTART:'+utc.strftime('%Y%m%dT%H%M%SZ'))
            for line in lines[1:]:
                rule=dict(part.split('=',1) for part in line.removeprefix('RRULE:').split(';'))
                self.assertEqual((rule['FREQ'],rule['INTERVAL']),('HOURLY','2'))
                self.assertEqual(int(rule['BYMINUTE']),utc.minute)
                self.assertEqual(int(rule['BYSECOND']),utc.second)

    def test_cached_legacy_rule_is_refreshed_without_advancing_or_mutating(self):
        at=self.dt('2026-11-14T17:39:11+08:00')
        state=b.plan(None,source='synthetic-source',jobs=['synthetic-job'],activity='same',at=at)
        legacy=dict(state,rrule=state['rrule'].replace('FREQ=HOURLY;INTERVAL=2','FREQ=MINUTELY;INTERVAL=120'))
        original=dict(legacy)
        actual=b.plan(legacy,source='synthetic-source',jobs=['synthetic-job'],activity='same',at=at)
        self.assertEqual(actual,state)
        self.assertEqual(legacy,original)
        self.assertNotIn('FREQ=MINUTELY',actual['rrule'])

    def test_early_peak_cache_refresh_preserves_due_and_backoff(self):
        at=self.dt('2026-11-16T08:59:00+08:00')
        state=b.plan(None,source='synthetic-source',jobs=['synthetic-job'],activity='same',at=at)
        legacy=dict(state,rrule='RRULE:FREQ=MINUTELY;INTERVAL=120')
        actual=b.plan(legacy,source='synthetic-source',jobs=['synthetic-job'],activity='same',at=self.dt('2026-11-16T09:03:00+08:00'))
        self.assertEqual(actual,state)

    def test_json_policy_uses_same_safe_base_rule(self):
        import json
        from pathlib import Path
        policy=json.loads((Path(b.__file__).parent/'followup-schedule.json').read_text(encoding='utf-8'))
        self.assertEqual(policy['rrule'],b.BASE_RULE)
        self.assertEqual(policy['interval_minutes'],120)
        self.assertEqual(policy['backoff_minutes'],list(b.DELAYS))

if __name__=='__main__':unittest.main()
