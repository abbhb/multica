# Cloud API to isolated self-host rehearsal

These operator tools export one accessible workspace through GET requests, prepare
a transactional database import, and reconcile a target API readback. They do not
change the Cloud service, redirect a daemon, change DNS, or perform a cutover.
Use Python 3.10+ and a dedicated self-host database. This is an API-visible copy,
not a native Cloud database backup.

For repeated incremental syncs while Cloud remains in use, install the
[workspace reconciliation console and server worker](console/README.md).

## Deploy alongside an existing HTTPS service

1. Record existing listeners, free memory/disk, TLS names and existing vhost
   responses. Back up the existing proxy configuration. Do not replace its
   default server. The overlay budgets about 1 GiB for three containers; leave
   headroom for the OS and existing services. Build images elsewhere on small hosts.
2. Copy the root `docker-compose.selfhost.yml`, this directory, and a private
   `.env` based on `.env.example` into a dedicated deployment directory. Generate
   independent random secrets. Pin and preload images for the host architecture;
   the overlay deliberately uses `pull_policy: never`. Inspect digests on the
   destination after `docker load`; the local image ID may differ on containerd.
3. Start with `docker compose -p multica-migration --env-file .env -f
   docker-compose.selfhost.yml -f deploy/migration/compose.yml up -d`. Run this
   command on one line. PostgreSQL has no published port; API/web ports bind only
   to loopback. Confirm backend `/health` and frontend `/login` before proxying.
4. Adapt `nginx.conf.example` with your domain, loopback ports and matching TLS
   certificate. Install as `zz-multica.conf` to avoid changing an existing implicit
   default. Run `nginx -t`, then reload. Recheck all pre-existing vhosts. Keep the
   private key outside Git and mode 0600. Arrange renewal separately.
5. Test with `curl --resolve DOMAIN:443:SERVER_IP https://DOMAIN/login`, then a
   local hosts entry. Keep public DNS and existing clients on Cloud during rehearsal.

The storage base URL is intentionally empty: attachment metadata uses durable API
download endpoints. Nginx also forwards `/uploads/` to the backend for local file
previews. Raw upload URLs are served without session authentication. Some API
download URLs use a high-entropy capability; they are not all strictly
session-authenticated. Configure mail delivery for normal email-code login; without
a mail provider the upstream self-host backend logs verification codes locally.

## Export and verify attachments

Keep every artifact below in an ignored, private directory (for example `data/`).
The CLI config contains a source token; never copy it to the server or commit it.
Only the account's accessible resources are exported. Use a fresh output directory
for each pass; `--resume` is only for recovering an interrupted pass, not freshness.

```sh
python3 scripts/cloud_sync.py export --config /private/source-config.json \
  --workspace WORKSPACE_UUID --output data/migration/source-01
python3 scripts/cloud_sync.py assets --config /private/source-config.json \
  --snapshot data/migration/source-01/snapshot.json --output data/migration/assets
```

Source config format is `{"server_url":"https://api.example.com","token":"..."}`.
Export exit 2 means the snapshot was saved but has blockers, such as an active run
or a truncated response. The exporter checks pagination and SHA-256 checkpoints.
Asset requests do not forward API authorization to CDN redirects; downloaded sizes
and hashes are verified. Later passes reuse immutable attachment IDs only when the
local size and hash still match. Markdown-only attachment references are resolved
through the metadata API even when absent from attachment lists. Failed or changed
pages must be resolved before cutover.

## Prepare and apply one snapshot

Capture the **running target** schema using `psql -X -At -f
deploy/migration/schema.sql > /private/schema.json` (one line, target connection).
Take a `pg_dump -Fc` backup and stop only this stack's backend before importing.

```sh
python3 scripts/cloud_sync_import.py \
  --snapshot data/migration/source-01/snapshot.json \
  --schema /private/schema.json --target https://multica.example.com \
  --assets data/migration/assets/assets.json --output data/migration/plan-01
```

Review `plan.json`: every table has mapped fields, keys, rows and SHA-256; omitted
API fields and intentional transformations are explicit. `--stage` permits a
partial rehearsal but never marks it complete. Nonterminal runs and their messages
are excluded to prevent task replay. Only terminal history is imported.

If the snapshot includes owned avatars, also pass `--avatar-signing-key-file
/private/target-jwt-secret` (on one line). This private mode-0600 file contains the
target's existing `JWT_SECRET`, not the Cloud key. It is used only to derive the
avatar HMAC in the same way as the backend; the secret is never written into plans
or SQL. The target readback planner does not need the secret. Emoji and external
profile URLs are preserved.

Copy verified asset files to the backend upload volume as `cloud-sync/ATTACHMENT_UUID`.
For entries in `snapshot.objects.avatars`, use their validated `storage_key`
instead of the `cloud-sync/` path, preserving the image extension and original ID.
Preserve each file's manifest hash and optionally add a `.meta.json` sidecar
with `filename` and `content_type`. Use validated UUID filenames only. Ensure the
backend process can read them; do not publish the upload directory as static files.

Run `psql -X -v ON_ERROR_STOP=1 -f dry-run.sql` against the dedicated target.
This performs the import and typed field comparisons, then **rolls back**. Only
after it passes run the corresponding `import.sql`, which commits one transaction.
Keep SQL and error logs private: PostgreSQL error details can include user content.

The database role must be able to set `session_replication_role=replica`: historical
imports suppress application triggers, so comments do not generate new notifications
and historical tasks do not execute. SQL still checks constraints, locks imported
tables and verifies all projected rows. It refuses another workspace in the target.
It does not delete target rows. Re-run the backend image's
`/app/backfill_task_usage_hourly --months-back 0` with its normal database environment
to rebuild usage aggregates, then restart the backend. Keep Cloud daemons detached.

For a later pass, provide `--previous-plan data/migration/plan-01/plan.json`. A row
may change only when the current target matches the last applied plan or already
matches the incoming version. Unexpected target edits abort the whole transaction.
Save each successfully applied plan. Extra/deleted source records are reported by
reconciliation and require an explicit resolution; this tool never silently deletes.

## Reconcile and test switching

Create a short-lived **target** PAT for the imported user, storing it in a separate
private config. Export the target through the same `cloud_sync.py export` command.
Generate its plan with `--readback`, the same target schema, target HTTPS origin
and assets manifest. This mode emits only the comparison plan, with no import SQL.

```sh
python3 scripts/cloud_sync_reconcile.py \
  --expected-plan data/migration/plan-01/plan.json \
  --actual-plan data/migration/readback-plan/plan.json \
  --output data/migration/reconciliation.json
python3 -m unittest discover -s scripts -p 'cloud_sync*test.py'
```

The report compares keys, counts and every stored API field, normalizing timestamp
representations and the explicitly synthesized usage update timestamp. It never
prints business content. Missing, extra and changed records fail reconciliation.
Also compare every target attachment byte hash with the downloaded manifest; a
matching database URL does not prove the file exists. Check login, board/detail,
comments, skills, historical run logs, attachments and WebSocket connection in a
browser. Verify existing HTTPS services again and inspect container resource use.

For switching tests, stop producing new Cloud work, wait for in-flight tasks to
finish, take a fresh export, apply its delta and repeat reconciliation. A matching
snapshot is only a point-in-time result. Keep target writes out of the next import
or resolve its conflicts explicitly. Change daemon/client endpoints only after the
user chooses to test the switch. Retain Cloud and the pre-import backup for rollback.

## API scope and limits

Included: identity/workspace membership, owned avatar images, projects/resources, skills and file contents,
agents/skills/labels/invocation targets, issues/statuses/workflows/labels, comments,
attachments, activity timeline, terminal runs/messages/usage, chat sessions/messages,
views, pins, active subscriptions and current-user notification/view preferences.
Runtimes and agents are copied offline; daemon IDs and sessions are detached.
Message/usage row IDs absent from the API are deterministic. Inbox bodies are API
previews, with full comment content available in comments. Archived inbox endpoints
expose group representatives, not the complete internal archive. Hidden/opted-out
subscriptions and other members' personal settings cannot be reconstructed.

Nonempty squads, autopilots, custom properties, wakeups, PR links or redacted agent
secrets block a complete import and need a dedicated mapping. Authentication tokens,
email codes, external integration credentials, billing, inaccessible/archived/deleted
internal records and Cloud-only infrastructure are not copied. Review the source
account's scope before declaring the user-visible migration complete. Store this
report separately from any claim of complete database equivalence.

Rollback uses the dedicated database backup and upload volume snapshot. Remove only
this stack's vhost/containers if abandoning rehearsal; do not use a global Docker
prune or alter other virtual hosts. These tools do not merge code or change DNS.
