import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from cloud_sync import API, AssetRedirect, Exporter, NoRedirect, SyncError, agent_secrets_redacted, attachment_references, avatar_reference, digest, save
from cloud_sync_import import identifier, literal, load_snapshot, rewrite_attachment_links, sql_for_plan, stable_id, stored_status_category
from cloud_sync_reconcile import reconcile


class FakeAPI:
    base = "https://source.example"
    workspace = "00000000-0000-0000-0000-000000000001"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return next(self.responses)


class ExportTests(unittest.TestCase):
    def test_all_known_agent_secret_surfaces_block_complete_copy(self):
        for agent in ({"has_custom_env": True}, {"mcp_config_redacted": True},
                      {"runtime_config": {"gateway": {"token": "***"}}}):
            self.assertTrue(agent_secrets_redacted(agent))
        self.assertFalse(agent_secrets_redacted({"runtime_config": {"model": "example"}}))

    def test_avatar_mapping_rejects_traversal_and_ignores_external_profiles(self):
        self.assertIsNone(avatar_reference("https://profile.example/avatar.png", {"source.example"}))
        with self.assertRaises(SyncError):
            avatar_reference("https://source.example/api/avatars/signature/workspaces/../../secret.webp", {"source.example"})
        id = "00000000-0000-0000-0000-000000000001"
        value = f"https://source.example/api/avatars/signature/workspaces/{id}/{id}.webp"
        self.assertEqual(avatar_reference(value, {"source.example"})["storage_key"], f"workspaces/{id}/{id}.webp")

    def test_transient_transport_failure_retries_the_same_get(self):
        api = API("https://source.example", "secret", FakeAPI.workspace)
        response = io.StringIO("[]")
        response.headers = {}
        api.opener = MagicMock()
        api.opener.open.side_effect = [urllib.error.URLError("connection closed"), response]
        with patch("cloud_sync.time.sleep"):
            self.assertEqual(api.get("/api/projects"), ([], {}))
        self.assertEqual(api.opener.open.call_count, 2)
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in api.opener.open.call_args_list))

    def test_inline_attachments_include_only_source_or_relative_links(self):
        id = "00000000-0000-0000-0000-000000000001"
        self.assertEqual(attachment_references(f"![image](https://source.example/api/attachments/{id}/download)", {"source.example"}), {id})
        self.assertEqual(attachment_references(f"/api/attachments/{id}/content", set()), {id})
        self.assertEqual(attachment_references(f"https://other.example/api/attachments/{id}/download", {"source.example"}), set())

    def test_pagination_walks_all_pages(self):
        api = FakeAPI([({"issues": [{"id": "a"}], "total": 2}, {}),
                       ({"issues": [{"id": "b"}], "total": 2}, {})])
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual([r["id"] for r in Exporter(api, root).issues()], ["a", "b"])
        self.assertIn("offset=1", api.calls[1])

    def test_duplicate_or_changing_page_is_not_a_complete_export(self):
        for page in ({"issues": [{"id": "a"}], "total": 2},
                     {"issues": [{"id": "b"}], "total": 3},
                     {"issues": [], "total": 2}):
            with self.subTest(page=page), tempfile.TemporaryDirectory() as root:
                api = FakeAPI([({"issues": [{"id": "a"}], "total": 2}, {}), (page, {})])
                with self.assertRaises(SyncError):
                    Exporter(api, root).issues()

    def test_resume_keeps_non_boolean_truncation_signal(self):
        with tempfile.TemporaryDirectory() as root:
            first = Exporter(FakeAPI([([], {"X-Multica-Timeline-Truncated": "activity"})]), root)
            first.get("/api/issues/a/timeline")
            resumed = Exporter(FakeAPI([]), root, resume=True)
            resumed.get("/api/issues/a/timeline")
            self.assertTrue(resumed.blockers)
            self.assertEqual(resumed.api.calls, [])

    def test_resume_rejects_other_workspace_and_corrupt_response(self):
        with tempfile.TemporaryDirectory() as root:
            api = FakeAPI([([{"id": "a"}], {})])
            Exporter(api, root).get("/api/projects")
            other = FakeAPI([])
            other.workspace = "00000000-0000-0000-0000-000000000002"
            with self.assertRaises(SyncError):
                Exporter(other, root, resume=True)
            response = next(p for p in (Path(root) / "responses").glob("*.json") if not p.name.endswith(".checkpoint.json"))
            response.write_text("[]")
            with self.assertRaises(SyncError):
                Exporter(FakeAPI([]), root, resume=True).get("/api/projects")

    def test_remote_plain_http_and_api_redirect_are_rejected(self):
        with self.assertRaises(SyncError):
            API("http://source.example", "secret", FakeAPI.workspace)
        with self.assertRaises(SyncError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_attachment_redirect_does_not_forward_credentials(self):
        redirect = AssetRedirect({("https", "cdn.example")})
        with self.assertRaises(SyncError):
            redirect.check("http://127.0.0.1/private")
        request = redirect.redirect_request(None, None, 302, "", {}, "https://cdn.example/file")
        self.assertNotIn("Authorization", request.headers)


class ImportTests(unittest.TestCase):
    def test_attachment_rewrite_preserves_external_urls_and_target_origin(self):
        id = "00000000-0000-0000-0000-000000000001"
        path = f"/api/attachments/{id}/download"
        target = "https://target.example"
        text = f"https://source.example{path} {path} https://other.example{path} {target}{path}"
        expected = f"{target}{path} {target}{path} https://other.example{path} {target}{path}"
        self.assertEqual(rewrite_attachment_links(text, {id}, {"source.example"}, target), expected)

    def test_wire_status_categories_map_to_storage_lifecycle(self):
        self.assertEqual(stored_status_category("todo"), "unstarted")
        self.assertEqual(stored_status_category("in_review"), "started")
        self.assertEqual(stored_status_category("cancelled"), "closed")
        self.assertEqual(stored_status_category("done"), "done")
        with self.assertRaises(SyncError):
            stored_status_category("surprise")

    def plan(self):
        rows = [{"id": "00000000-0000-0000-0000-000000000003", "content": "a'); DROP TABLE issue; --"}]
        return {"source": "https://source.example", "target": "https://target.example",
                "workspace_id": FakeAPI.workspace, "schema_sha256": "schema",
                "tables": {"comment": {"keys": ["id"], "fields": ["id", "content"], "rows": rows, "sha256": digest(rows)}}}

    def test_sql_is_transactional_and_conflict_checked(self):
        sql = sql_for_plan(self.plan(), dry_run=True)
        self.assertIn("SET LOCAL session_replication_role = replica", sql)
        self.assertIn("no_conflicts_comment", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertIn("ON CONFLICT", sql)
        self.assertTrue(sql.endswith("ROLLBACK;\n"))
        self.assertIn("a''); DROP TABLE issue; --", sql)
        self.assertNotIn("DELETE FROM", sql)

    def test_import_rejects_modified_plan_or_wrong_previous_target(self):
        plan = self.plan()
        previous = self.plan()
        previous["target"] = "https://different.example"
        with self.assertRaises(SyncError):
            sql_for_plan(plan, previous)
        plan["tables"]["comment"]["rows"][0]["content"] = "tampered"
        with self.assertRaises(SyncError):
            sql_for_plan(plan)
        previous = self.plan()
        previous["tables"]["comment"]["rows"][0]["content"] = "tampered"
        with self.assertRaises(SyncError):
            sql_for_plan(self.plan(), previous)

    def test_reconciliation_detects_business_changes_and_extra_rows(self):
        expected, actual = self.plan(), self.plan()
        expected["blockers"] = actual["blockers"] = []
        self.assertTrue(reconcile(expected, actual)["api_projection_matches"])
        spec = actual["tables"]["comment"]
        spec["rows"][0]["content"] = "changed"
        spec["rows"].append({"id": "extra", "content": "extra"})
        spec["sha256"] = digest(spec["rows"])
        report = reconcile(expected, actual)
        self.assertEqual(report["difference_count"], 2)
        self.assertEqual(report["tables"]["comment"]["changed"][0]["fields"], ["content"])

    def test_snapshot_digest_is_required(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "snapshot.json"
            save(file, {"format": 1, "objects": {"issues": []}, "object_hashes": {"issues": "wrong"}})
            with self.assertRaises(SyncError):
                load_snapshot(file)

    def test_identifiers_and_repeatable_message_ids(self):
        with self.assertRaises(SyncError):
            identifier('comment; DROP TABLE issue')
        self.assertEqual(literal("a'b"), "'a''b'")
        self.assertEqual(stable_id("task", 1), stable_id("task", 1))
        self.assertNotEqual(stable_id("task", 1), stable_id("task", 2))


if __name__ == "__main__":
    unittest.main()
