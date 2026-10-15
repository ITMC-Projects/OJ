# System monitoring

Open `/admin/monitor/`. Access requires an active staff account with both
`judge.view_judge` and `judge.view_all_submission`. Superusers qualify.
All routes are GET-only, private and never cached by browsers.
This dashboard does not add administrative mutations; existing Admin actions
retain their permissions and CSRF protection.

## Deployment

Deploy the Django and Bridge changes together, restart Bridge using the existing
service procedure, and run `collectstatic` as for other Admin assets. A Bridge
restart affects connected judges: use the existing maintenance/draining procedure.
No migration or new dependency is required. Deploy the optional judge-server
`dmoj/sysinfo.py` change and reconnect judges to obtain host counters. Old judges
continue working and report unknown counters. Old Bridge instances return an
unavailable state until upgraded. Keep the Django-facing Bridge listener private:
it is the existing trusted command transport, without per-command authentication.

## API

- `GET /admin/monitor/api/overview/?offset=0`: health, registered judges, live
  assignments, first 100 queued submissions in scheduler order, diagnostics,
  execution statistics and recent arrival-cohort history. Offset is bounded.
- `GET /admin/monitor/api/judges/<name>/?after=0`: current judge details and at
  most 100 testcase results after the supplied testcase position; `has_more`
  indicates another page. Source, program output, feedback, compile messages,
  authentication keys and IP addresses are excluded.
- `/admin/monitor/judges/<name>/`: directly addressable judge detail screen.

Bridge snapshots copy allowlisted primitive state under `JudgeList.lock`.
They never query the database or write sockets under that lock. Serialization
and network I/O run afterwards. Monitoring does not dispatch, reserve or abort
submissions. Queue snapshots are cached for 3 seconds; history for 30 seconds.
Polling is sequential every 5 seconds, slowing to 30 seconds in hidden tabs.
Socket connect/read time is bounded to a 2-second overall budget; both compressed
and uncompressed responses are capped at 4 MiB. Avoid many simultaneous viewers
when the configured cache is process-local.

## Interpretation and incident response

Queue order comes from Bridge, including priorities, explicit target and contest
bans. Diagnostics cover the displayed queue page and each judge's current busy,
disabled, tier, runtime, problem and reservation constraints. An eligible entry
means eligibility at capture time, not a promise of immediate dispatch. Queue
pages are independent live snapshots and may shift under traffic.

Assignments come from connected handlers, not historical `judged_on`. Database
details can lag behind Bridge events. Offline means absent from a successful
Bridge snapshot; Bridge failure means unknown. DB `online` flags are not used
as proof of a live connection. Disabled busy judges retain the current assignment.
Up to 20 database submissions in processing/grading without a live assignment
are also listed. These can be transient during finish/reassignment; investigate
persisting entries as potential orphaned judging operations.

Testcase totals remain unknown: generators, batching, pretests and short circuit
make `current_testcase` unsuitable as a completion denominator. Details show only
received persisted testcase results. Database and Bridge snapshots are not atomic.
Rejudging resets detail pagination when a new Bridge dispatch timestamp is observed.

Execution statistics describe finish events received by this Bridge process,
including compile errors and aborts, over at most 5 minutes since startup.
The 10,000-event buffer exposes truncation. Dispatch-to-finish duration excludes
queue latency and is not testcase CPU time. End-to-end latency is unavailable.
Disconnects and malformed protocol failures are not included in finish rates;
inspect existing Bridge logs alongside this dashboard. Historical data is grouped
by submission arrival during the last hour, not completion time; rejudges update
existing rows and are not a separate historical event series.

Investigate a heartbeat older than 30 seconds or no grading activity for 120
seconds. These are warnings, not automatic proof of a stuck operation; long tests
may legitimately be quiet. Check judge logs and problem limits before intervention.

Linux CPU/RAM/network metrics describe the host view exposed through `/proc`;
containers may expose host totals rather than cgroup limits. Disk describes `/`
only; network counters exclude loopback and are cumulative bytes, not bytes/sec.
Unsupported metrics are omitted, never set to zero. Web/database/cache/event/Celery
machines have no installed agent in this change; their CPU/RAM/disk/network
remain unavailable. Database health means monitoring queries succeeded; Bridge
health means the live state request succeeded. Broker/Celery/event delivery health
must still be checked using their existing operational tools.

Rollback the web and Bridge changes together and redeploy static files. The
judge-server extension can remain: unknown ping fields are backward compatible.

## Verification

Run `python manage.py test judge.test_monitoring` in the project's configured
Django environment, then `python manage.py check` and the normal project suite.
Run `python -m unittest discover -s tests -p test_monitor_sysinfo.py` from
judge-server for the dependency-free host collector tests.
