# Native followup recurrence safety

2026-11-08, Codex (exact runtime model not independently verified).

The optional legacy native followup uses an explicit UTC first due time and a
two-hour safety net. The normal foreground supervision path needs no automation.
The 2/10/30/60/120-minute backoff and Beijing weekday peak exclusions are unchanged.

Encode the safety net as `FREQ=HOURLY;INTERVAL=2`, with explicit first-due minute
and second fields. Do not use `FREQ=MINUTELY;INTERVAL=120` with the weekday/hour
filters. Codex Windows 26.1103.4239.0's bundled recurrence implementation can
cycle indefinitely in its filtered-day hourly advance for that combination.
For example, a synthetic Saturday UTC 09:41 anchor can advance through hours
1,17,9 repeatedly, none allowed by the weekday-only rule. A union of rules still
blocks while computing that component, even when another component has a valid
next occurrence. Synchronous recurrence evaluation can stall the desktop.

The emitted UTC hour/day filters retain the Beijing policy: weekday peaks
09:00-12:00 and 14:00-18:00 are excluded; weekends are entirely eligible. Minute
and second precision is preserved. Cached pre-upgrade plans are re-encoded when
returned, without advancing backoff, moving their due time, mutating the caller's
state or changing source/job bindings. This refresh does not apply a native
automation mutation or claim it succeeded.

Offline checks: `python -B -m unittest test_followup_backoff`. They check calendar
coverage, first-due precision and cached-plan migration. The release host also
ran the installed recurrence library in an isolated memory-only VM with timeouts:
five problematic inputs timed out before the correction; their hourly equivalents
returned in milliseconds with next times matching independent calendar enumeration.
No application entrypoint, automation mutation, real task or model was invoked
by that isolated check. The application's own bundled library is not distributed.

This is a generator-side workaround, not a patch to the Codex application or a
general guarantee about all recurrence expressions or desktop versions. Existing
client copies require an explicit reviewed update; a GitHub push does not upgrade
an already installed client. Preserve historical task state and never restart or
replay work to apply a scheduling correction. Existing native automations are
changed only through their authorized app interface; never edit automation.toml.
