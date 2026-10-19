# OTS account synchronization

Automatic synchronization is an authorized extension: enable
`OTS_SYNC_AUTO_ENABLED=1` to check OTS every 30 seconds through Celery Beat.
Use exactly one `beat` service per deployment. Run `make migrate`, then
`make up` to start/recreate worker and scheduler with the root `.env`.
Set the flag to `0` and recreate both services to disable automatic sync.
The manual superuser button remains available.

Scheduled ticks expire after 30 seconds. If a queued/running sync owns the
database reservation, the tick is skipped rather than queued behind it.
System runs have `triggered_by=NULL` and appear in the same history. Each
successful cycle still exports all source accounts; allow for API load and
roughly 2,880 history rows per day. A full scan taking longer than 30 seconds
causes later ticks to skip until it finishes. This is not a real-time guarantee.

Migration `0215_ots_password_digest` records a SHA-256 fingerprint of the
source bcrypt hash. A stable source preserves Django's upgraded password and
active sessions. A changed source password imports the new bcrypt hash and
invalidates old sessions as usual. Existing links without a fingerprint are
reconciled once on their first sync after this migration.

Superusers can open `/admin/auth/user/`, select **Sync Up OTS**, confirm,
and follow the queued/running result page. The page refreshes every five
seconds and lists counts and paginated, sanitized account errors. Previous
runs remain available under **Lịch sử đồng bộ OTS**.

## Configuration and rollout

1. Generate a dedicated random service token, for example with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   Set `OJ_SYNC_SERVICE_TOKEN` in the OTS backend. Do not reuse the
   IT Rounds token. OTS rejects nonempty tokens shorter than 32 characters,
   whitespace, and reuse of the configured IT Rounds token. With no token,
   the export endpoint rejects every request.
2. Configure both OJ `site` and `celery` through `environment/site.env`:

   ```dotenv
   OTS_SYNC_API_URL=https://ots-internal.example.com
   OTS_SYNC_SERVICE_TOKEN=<same-dedicated-secret>
   OTS_SYNC_TIMEOUT_SECONDS=10
   OTS_SYNC_PAGE_SIZE=100
   OTS_SYNC_ALLOW_INTERNAL_HTTP=0
   ```

   The URL is the origin, without an endpoint path. HTTPS is required for
   public hosts. Private/loopback IPs support HTTP; an internal hostname
   requires `OTS_SYNC_ALLOW_INTERNAL_HTTP=1` and a protected network.
   Protect the endpoint with network ACLs. Never expose port 5432 to OJ.
3. Rebuild the OJ base/site/celery images to install `bcrypt==4.3.0`.
   Stop old site/worker processes before using the new task.
4. Apply `python manage.py migrate` before starting new site and Celery
   processes. Migration `0213_ots_account_sync` creates four new tables and
   seeds the singleton sync lock; existing users and submissions are unchanged.
5. Restart site and worker with matching configuration. Ensure Celery's broker
   is reachable, tasks are consumed, and outbound HTTPS to OTS works from the
   worker. Sync work runs in Celery, not a long-lived HTTP request.
6. Perform a manual run as a superuser. Confirm counts, login with a test OTS
   user, change its password on OTS, sync again, and confirm that the old
   password fails and the new one succeeds. Do not export credentials to files.

OJ retains existing Django password hashers. Imported bcrypt uses
`ots_bcrypt$<source-hash>`; supported prefixes are `$2a$`, `$2b$`, `$2y$`
with costs 4–16. Higher costs are rejected to bound login CPU. Django may
upgrade the imported hash after login. The next sync restores the current
source password when its fingerprint changes. Passwords over 72 UTF-8
bytes cannot authenticate through the imported Go bcrypt verifier.

## Identity and local controls

Only `USER` accounts are exported, regardless of application/team/status.
UUID is the sole source identity; student IDs become uppercase usernames.
Email/username collisions never merge users or overwrite an unlinked account.
Every account commits independently; a profile failure rolls back that account.
No sync grants staff/superuser flags or removes missing users/submissions.

After investigating `USERNAME_CONFLICT` or `EMAIL_CONFLICT`, a superuser may
create an **OTS account link** in Django Admin. Verify the user's actual OTS
UUID, OJ identity, source student ID/status/update timestamp, and check the
identity verification acknowledgement. Linking does not import credentials;
the next sync does so. The UUID and OJ user become read-only after linking.
Edit **local_disabled** on that link to lock a user locally. It takes effect
immediately and survives later OTS ACTIVE exports. A BLOCKED OTS user stays
inactive. An ordinary `is_active` edit is not a persistent local override.

Linked users may change passwords on OJ when `OTS_PASSWORD_CHANGE_ENABLED=1`.
That form calls OTS with the current password; only OTS writes the authoritative
password. Django reset-mail, reset-confirm, Admin and the `changepassword`
management command cannot independently reset linked credentials. Recovery
remains with OTS; no service-token-only recovery API is provided. Local OJ users
retain Django authentication. See [PASSWORD_SYNC.md](PASSWORD_SYNC.md).

## Failure and recovery

Network/429/5xx failures retry at most three attempts per page with bounded
backoff. Authentication failures stop immediately. Redirects are rejected,
TLS is verified, proxy environment settings are ignored, and response bodies
are bounded to 1 MiB. An invalid page stops before processing that page.
Invalid accounts are recorded individually and the run continues. Re-running
is safe and reconciles previously committed accounts.

A database singleton reserves both QUEUED and RUNNING jobs. Duplicate task
delivery does not claim the same job twice. A worker crash deliberately leaves
the reservation held: automatic expiry could permit two workers to write
concurrently. Monitor queued/running age and worker health.

If a run is stuck, stop the worker that could execute it and verify it is no
longer running. Then execute:

```sh
python manage.py recover_ots_sync <run-id> --worker-stopped
```

Restart the worker and trigger a fresh sync. Late deliveries of the failed
run are ignored. Do not release the reservation while its worker is running.
If a crash happened after an account commit but before progress persistence,
the old run's counts can underreport; the next complete run reconciles state.

All UI/errors use fixed codes and contain no source hashes, tokens, or SQL.
Disable request/response body capture in proxies/APM for the export route;
preserve its `Cache-Control: no-store`. Never log credential payloads.

## Verification

```sh
python manage.py test judge.test_ots_sync --noinput
python manage.py check
```

The test database user must be allowed to create/use `test_<database>`.
Tests run against a separate database, including a real concurrent queue test.
OTS verification: `go test ./...`, `go vet ./...`, `go build ./cmd/api`.
For the PostgreSQL export repository test, set `OJ_EXPORT_TEST_DATABASE_URL`
to a disposable PostgreSQL database. The test creates an isolated schema.

Rollback application code only after stopping site and worker. Retain the
new tables and configured custom password hasher while imported accounts
exist, otherwise those accounts cannot log in. Do not reverse the migration
or remove identity links as a rollback shortcut.
