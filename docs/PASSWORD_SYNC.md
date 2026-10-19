# OTS-linked password integration

## Required configuration

Configure site and Celery with the same environment:

```dotenv
OTS_PASSWORD_API_URL=https://ots-internal.example.com
OTS_PASSWORD_SERVICE_TOKEN=<OTS OJ_PASSWORD_SERVICE_TOKEN>
OTS_CREDENTIAL_READ_TOKEN=<OTS OJ_CREDENTIAL_READ_TOKEN>
OTS_CALLBACK_TOKEN=<OTS OTS_TO_OJ_CALLBACK_TOKEN>
OTS_PASSWORD_TIMEOUT_SECONDS=5
OTS_PASSWORD_CHANGE_ENABLED=0
OTS_SYNC_AUTO_ENABLED=1
```

All three password capability tokens must be distinct from each other and the
export token. Use at least 32 random characters. Never put them in browser
configuration. An empty API URL falls back to `OTS_SYNC_API_URL`. URL, transport,
TLS, network and timeout validation reuse the existing sync client's rules;
production validation requires HTTPS. Redirects and environment proxies are
disabled; responses are bounded to 16 KiB. Current/new passwords are sent only
over synchronous protected HTTP, never Celery or callbacks. Mark these routes
and forms as sensitive in proxies/APM; disable request/response body capture.

The parent `ITMC-OJ-docker` repository's production env example, validator,
Compose env-file wiring and secret initializer are updated separately from this
OJ submodule. `prod-init` generates distinct missing secrets; configure matching
secrets on OTS before deploying the OJ code. Do not use a development `.env` in
production. No production deployment is performed by the implementation.

## Authentication and sessions

Linked password login uses `OTSModelBackend` and verifies the current password
against the OTS UUID, ACTIVE status and credential version. It then reads current
credentials, requires the same version, synchronizes under row locks, and checks
the applied version again. It creates no OTS session. Remote errors deny the
linked login and terminate Django's backend chain; cached credentials are never
a login fallback. Existing 2FA, CSRF, bans and Django login controls remain.
`local_disabled` is checked before and after synchronization. IP-based login is
not available for linked accounts. Independent accounts use Django normally.

The legacy ModelBackend entry remains after the OTS backend to preserve existing
independent users' session backend paths. The OTS backend raises PermissionDenied
for every rejected linked attempt so that legacy entry cannot verify a stale
linked cache. Do not reorder or remove that gate.

The middleware checks the OTS status/version endpoint before each authenticated
linked HTTP request. It returns 503 when OTS is unavailable; an outdated version
or disabled account logs out the user. Django's password session hash also
invalidates sessions when the imported cache changes. This deliberately favors
security over linked-account availability and adds an OTS call to each linked
request. OTS checks its own PostgreSQL credential version for Redis sessions.
There is no claim of absolute instantaneous revocation for an in-flight request,
network partition, queued job or already-running task.

The shared production Django Redis cache limits linked login/change attempts
to 10 per OJ user per minute; cache failure denies the attempt. OTS independently
limits verify/change/receipt calls to 10 per UUID per minute. Event callbacks use
a separate per-user callback throttle. Do not replace production cache with a
per-process cache in a multi-worker deployment.

## Change and recovery

The linked form retains the existing Django layout and uses OTS's password
policy without applying Django's independent complexity validators. It submits
a fresh operation UUID with the current and new passwords. Success is displayed
only after a committed OTS receipt; the current OJ session is logged out.
Wrong current password, invalid policy, temporary service failures and unknown
outcomes have separate messages. A lost response triggers a receipt query,
never a blind mutation retry. If the query is inconclusive, try the new password
on login before submitting another change. Cache refresh failure after a
confirmed commit still means the password changed; outbox/reconciliation repairs
the cache. No plaintext password or request fingerprint is persisted locally.

Reset-mail excludes linked users; reset-confirm/Admin/management-password paths
reject them. Social account setup also cannot write a linked password. OTS
recovery remains responsible for forgotten passwords. No recovery protocol is
invented if OTS has no safe recovery facility.
Both UserAdmin's target-user password route and AdminSite's own password-change
route are protected; they are separate Django writers.

## Synchronization

Migration `0216_ots_credential_version` adds the non-editable applied version,
defaulting to zero without changing links or existing user history. Versioned
events and full sync use the same `OTSAccountSyncService.sync_account` with a
locked link and user. Older snapshots cannot overwrite newer credentials; a
conflicting hash at an equal nonzero version is rejected. Unversioned exports
are supported only before the link has received a versioned record.
The migration explicitly blocks reversal so application rollback retains the
credential history.

`POST /internal/ots/password-changed` authenticates its dedicated callback token
with constant-time comparison and reads at most 2049 bytes. It accepts a UUID
event ID/user ID, positive version and timezone-aware occurrence timestamp,
rejecting timestamps more than five minutes in the future. The applied version
is the durable replay barrier. Duplicate/older events return 204 without IO to
OTS; newer events fetch current credentials, possibly newer than the event.
Only a successful OJ database commit acknowledges delivery. Database/network
failure returns a retryable error to the OTS outbox. No asynchronous broker
publication window exists for this callback, so Celery restart cannot lose it.
Unknown links are left to full reconciliation; events never auto-link identities.

`OTS_SYNC_AUTO_ENABLED` remains the periodic reconciliation path. Conflicts
retain their existing error history and never overwrite an independent account.
Unchanged source hash fingerprints preserve Django-upgraded hashes and sessions.
Profile changes do not rotate the password cache. No submissions, contest,
judge or historical records are edited.

## Rollout and rollback

Deploy OTS migration 47/API first with callback disabled. Apply OJ migration
0216, configure service capabilities and deploy OJ with change UI disabled.
Authoritative linked login/session checking is active immediately, so OTS must
be reachable before this deployment. Run full sync to backfill versions. Enable
the OTS callback URL, inspect delivery and then enable the OJ change UI.
Verify A → B on OJ and B → C on OTS with both backends running on disposable
databases; check old passwords and old sessions after each step.

Rollback by disabling the change UI while retaining the authoritative login/
session gate, migrations, hashers and links. Rolling OJ back to stale-cache
authentication is unsafe. If a previous release cannot retain the gate, deny
linked authentication during maintenance until fixed. Do not reverse credential
versions, unlink accounts or restore older password data for application rollback.

For the detailed OTS API, outbox lease/backoff/exhaustion monitoring and re-arm
runbook, see `docs/PASSWORD_SYNC.md` in `ITMC-Projects/OTS`.

## Verification limits

Run `python manage.py check` and the entire
`python manage.py test judge.test_ots_sync --noinput` on disposable MariaDB.
The existing concurrency test requires real row locking and multiple connections.
SQLite can exercise logic using a temporary model-created schema, but cannot
validate the historical MySQL migrations or production concurrency. Tests cover
authority failure without fallback, independent users, old session path
compatibility, version ordering/replays, current credentials, local disabling,
timeout receipts, policy delegation, cache failure, callback authentication and
session fail-closed behavior. Run both services plus Redis in Docker for final
E2E and failure-injection acceptance; do not treat mocked IO tests as E2E.
