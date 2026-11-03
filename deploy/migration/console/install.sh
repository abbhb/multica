#!/bin/sh
# Install only the operator services. Private configuration is provisioned separately.
set -eu
test "$(id -u)" = 0
test -f /etc/multica-reconcile/web.json
test -f /etc/multica-reconcile/worker.json
if systemctl is-active --quiet multica-reconcile-worker.service; then
    python3 - <<'PYGUARD'
import json, sqlite3
from pathlib import Path
db = Path(json.loads(Path("/etc/multica-reconcile/web.json").read_text())["jobs"])
if db.exists():
    with sqlite3.connect(db) as conn:
        if conn.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            raise SystemExit("Wait for the active migration before upgrading the operator services")
PYGUARD
fi
src=$(CDPATH= cd -- "$(dirname "$0")/../../.." && pwd)
lib=/usr/local/lib/multica-reconcile
id multica-reconcile >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/multica-reconcile --shell /usr/sbin/nologin multica-reconcile
install -d -m 755 "$lib" "$lib/static"
install -d -m 700 -o multica-reconcile -g multica-reconcile /var/lib/multica-reconcile
for name in cloud_sync cloud_sync_import cloud_sync_reconcile cloud_sync_jobs cloud_sync_worker cloud_sync_console; do
    install -m 644 "$src/scripts/$name.py" "$lib/"
done
install -m 644 "$src"/deploy/migration/console/style.css "$src"/deploy/migration/console/page.js "$lib/static/"
install -m 644 "$src"/deploy/migration/console/*.service /etc/systemd/system/
# Create the queue as its web-service owner before the privileged worker opens it.
runuser -u multica-reconcile -- env PYTHONPATH="$lib" python3 -c 'import json; from cloud_sync_jobs import Jobs; Jobs(json.load(open("/etc/multica-reconcile/web.json"))["jobs"])'
/usr/bin/python3 "$lib/cloud_sync_worker.py" /etc/multica-reconcile/worker.json --initialize
systemctl daemon-reload
systemctl enable multica-reconcile-web.service multica-reconcile-worker.service
systemctl restart multica-reconcile-web.service multica-reconcile-worker.service
