import copy
import hashlib
import http.client
import io
import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlencode

from cloud_sync import (
    SyncError,
    canonical,
    digest,
    read_task_messages,
    stream_canonical,
)
from cloud_sync_console import Handler
from cloud_sync_jobs import Jobs
from cloud_sync_worker import delta_plan


class MembershipAPI(BaseHTTPRequestHandler):
    def do_GET(self):
        user = self.headers.get("Cookie", "").removeprefix("multica_auth=")
        if user not in {"owner", "admin", "member", "elsewhere"}:
            self.send_error(401)
            return
        value = (
            {"id": user}
            if self.path == "/api/me"
            else [
                {
                    "workspace_id": "other" if user == "elsewhere" else "ws",
                    "user_id": user,
                    "role": "admin" if user == "elsewhere" else user,
                },
                {"workspace_id": "ws", "user_id": "someone-else", "role": "owner"},
            ]
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def log_message(self, *args):
        pass


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        api = self.serve(MembershipAPI)
        self.jobs = Jobs(Path(self.root.name) / "jobs.sqlite3")
        cfg = {
            "backend": f"http://127.0.0.1:{api.server_port}",
            "workspace_id": "ws",
            "slug": "demo",
            "origin": "https://target.example",
        }
        self.web = self.serve(
            partial(
                Handler,
                config=cfg,
                jobs=self.jobs,
                signing_key=b"fixture",
                assets=Path(self.root.name),
            )
        )
        self.path = "/demo/reconciliation"

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, method, path, user=None, data=None, **headers):
        if user:
            headers["Cookie"] = "multica_auth=" + user
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.web.server_port)
        try:
            conn.request(
                method, path, body=urlencode(data) if data else None, headers=headers
            )
            response = conn.getresponse()
            return (
                response.status,
                dict(response.getheaders()),
                response.read().decode(),
            )
        finally:
            conn.close()

    def test_all_surfaces_require_the_workspace_admin(self):
        status, headers, _ = self.request("GET", self.path)
        self.assertEqual(status, 303)
        self.assertIn("/login?next=", headers["Location"])
        for path in (self.path, self.path + "/report?run=1", self.path + "/runs"):
            method = "POST" if path.endswith("/runs") else "GET"
            for user in ("member", "elsewhere"):
                self.assertEqual(
                    self.request(method, path, user, **{"X-User-Role": "owner"})[0], 403
                )
        self.assertEqual(self.request("POST", self.path + "/runs", "fake")[0], 401)
        for user in ("owner", "admin"):
            self.assertEqual(self.request("GET", self.path, user)[0], 200)

    def test_submit_validates_origin_and_session_bound_csrf_and_deduplicates(self):
        _, _, html = self.request("GET", self.path, "owner")
        token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        data = {"mode": "sync", "csrf": token}
        self.assertEqual(
            self.request(
                "POST",
                self.path + "/runs",
                "owner",
                data,
                Origin="https://other.example",
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "POST",
                self.path + "/runs",
                "admin",
                data,
                Origin="https://target.example",
            )[0],
            403,
        )
        self.assertEqual(self.jobs.history(), [])
        a = self.request(
            "POST", self.path + "/runs", "owner", data, Origin="https://target.example"
        )
        b = self.request(
            "POST", self.path + "/runs", "owner", data, Origin="https://target.example"
        )
        self.assertEqual(a[0], 303)
        self.assertEqual(a[1]["Location"], b[1]["Location"])
        self.assertEqual(self.jobs.claim()["mode"], "sync")
        self.assertIsNone(self.jobs.claim())

    def test_concurrent_clicks_create_one_run_and_restart_marks_interruption(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(
                pool.map(lambda _: self.jobs.enqueue("compare", "owner"), range(12))
            )
        self.assertEqual(len(set(ids)), 1)
        self.jobs.claim()
        self.jobs.interrupted()
        self.assertEqual(self.jobs.get(ids[0])["state"], "failed")
        self.assertNotEqual(self.jobs.enqueue("sync", "owner"), ids[0])


def spec(rows):
    return {
        "keys": ["id"],
        "fields": sorted(rows[0]),
        "rows": rows,
        "sha256": digest(rows),
    }


class DeltaTests(unittest.TestCase):
    def test_only_changed_keys_are_written_and_removals_are_reported(self):
        previous = {
            "source": "cloud",
            "target": "target",
            "workspace_id": "ws",
            "schema_sha256": "schema",
            "tables": {
                "issue": spec(
                    [
                        {"id": "old", "title": "before"},
                        {"id": "stable", "title": "same"},
                        {"id": "deleted", "title": "gone"},
                    ]
                ),
                "task_usage": spec([{"id": "usage", "tokens": 9, "updated_at": "old"}]),
            },
        }
        current = copy.deepcopy(previous)
        current["tables"]["issue"] = spec(
            [
                {"id": "old", "title": "after"},
                {"id": "stable", "title": "same"},
                {"id": "new", "title": "new"},
            ]
        )
        current["tables"]["task_usage"] = spec(
            [{"id": "usage", "tokens": 9, "updated_at": "new"}]
        )
        delta, counts = delta_plan(current, previous)
        self.assertEqual(counts, {"added": 1, "updated": 1, "removed": 1})
        self.assertEqual(set(delta["tables"]), {"issue"})
        self.assertEqual(
            [r["id"] for r in delta["tables"]["issue"]["rows"]], ["old", "new"]
        )
        self.assertEqual(
            current["tables"]["task_usage"], previous["tables"]["task_usage"]
        )
        self.assertEqual(
            delta_plan(current, copy.deepcopy(current))[1],
            {"added": 0, "updated": 0, "removed": 0},
        )

    def test_streamed_snapshots_keep_existing_checksums(self):
        value = [{"z": "鲸眼🦄\n" * 15000, "a": [None, True, 2.5]}]
        buffer = io.BytesIO()
        stream_canonical(value, buffer.write)
        self.assertEqual(buffer.getvalue(), canonical(value))
        self.assertEqual(digest(value), hashlib.sha256(canonical(value)).hexdigest())

    def test_tampered_baseline_fails_before_planning(self):
        baseline = {
            "source": "cloud",
            "target": "target",
            "workspace_id": "ws",
            "schema_sha256": "schema",
            "tables": {"issue": spec([{"id": "x"}])},
        }
        current = copy.deepcopy(baseline)
        baseline["tables"]["issue"]["rows"][0]["id"] = "wrong"
        with self.assertRaises(SyncError):
            delta_plan(current, baseline)


class MessageCursorTests(unittest.TestCase):
    def test_verified_anchor_appends_or_refetches_if_the_log_was_reset(self):
        tid = "00000000-0000-0000-0000-000000000001"
        task = {"id": tid, "status": "completed", "completed_at": "stamp"}
        items = [{"task_id": tid, "seq": i, "content": str(i)} for i in range(1, 4)]
        path = f"/api/tasks/{tid}/messages"
        with TemporaryDirectory() as root:
            calls = []

            def initial(url):
                calls.append(url)
                return items[:2], {}

            read_task_messages(initial, task, Path(root))
            self.assertEqual(calls, [path])
            calls.clear()

            def append(url):
                calls.append(url)
                return items[1:], {}

            rows, reused = read_task_messages(append, task, Path(root))
            self.assertEqual((rows, reused), (items, 1))
            self.assertEqual(calls, [path + "?since=1"])
            calls.clear()

            def reset(url):
                calls.append(url)
                return ([], {}) if "?" in url else (items[:1], {})

            self.assertEqual(
                read_task_messages(reset, task, Path(root)), (items[:1], 0)
            )
            self.assertEqual(calls, [path + "?since=2", path])
