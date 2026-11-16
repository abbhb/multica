import copy
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from cloud_sync import digest, save
from cloud_sync_policy import BlockedRun, schema_difference, sync_enabled
from cloud_sync_worker import Target, run_job


REFERENCE = [{
    "table_name": "issue", "column_name": "id", "data_type": "uuid",
    "udt_name": "uuid", "is_nullable": "NO", "column_default": None,
    "ordinal_position": 1,
}]


def spec(rows):
    return {"keys": ["id"], "fields": sorted(rows[0]), "rows": rows, "sha256": digest(rows)}


class PolicyTests(unittest.TestCase):
    def test_import_requires_an_explicit_boolean_opt_in(self):
        for config in ({}, {"allow_sync": False}, {"allow_sync": "true"}, {"allow_sync": 1}):
            self.assertFalse(sync_enabled(config))
        self.assertTrue(sync_enabled({"allow_sync": True}))

    def test_additive_schema_is_comparable_but_not_importable(self):
        live = copy.deepcopy(REFERENCE)
        live.append({**live[0], "column_name": "new_field", "ordinal_position": 2})
        target = Target({"project": "test", "db_user": "test", "db_name": "test"})
        target.sql = MagicMock(return_value=json.dumps(live))
        coverage = target.schema(REFERENCE, read_only=True)
        self.assertEqual(coverage["added"], ["issue.new_field"])
        self.assertTrue(coverage["projection_compatible"])
        with self.assertRaises(BlockedRun) as blocked:
            target.schema(REFERENCE)
        self.assertEqual(blocked.exception.report["written"], 0)

    def test_removed_or_retyped_columns_block_comparison_with_named_coverage(self):
        for live, field in (([], "removed"), ([{**REFERENCE[0], "data_type": "text"}], "changed")):
            target = Target({"project": "test", "db_user": "test", "db_name": "test"})
            target.sql = MagicMock(return_value=json.dumps(live))
            with self.assertRaises(BlockedRun) as blocked:
                target.schema(REFERENCE, read_only=True)
            self.assertTrue(blocked.exception.report["schema"][field])
            self.assertFalse(blocked.exception.report["schema"]["projection_compatible"])

    def test_schema_diagnostics_contain_column_names_without_definition_values(self):
        changed = [{**REFERENCE[0], "column_default": "private-default-value"}]
        report = schema_difference(REFERENCE, changed)
        self.assertEqual(report["changed"], [{"column": "issue.id", "attributes": ["column_default"]}])
        self.assertNotIn("private-default-value", json.dumps(report))

    def test_read_only_worker_rejects_a_queued_import_before_any_target_or_source_access(self):
        with patch("cloud_sync_worker.Target") as target, patch("cloud_sync_worker.capture") as capture:
            with self.assertRaises(BlockedRun) as blocked:
                run_job({}, MagicMock(), {"id": "run", "mode": "sync"}, Path("unused"), io.StringIO())
            target.assert_not_called()
            capture.assert_not_called()
            self.assertEqual(blocked.exception.report["written"], 0)

    def test_compare_preserves_local_additions_baseline_and_recovery_points(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            tmp = Path(directory) / "run"
            (root / "baselines").mkdir(parents=True)
            (root / "backups").mkdir()
            (tmp / "source").mkdir(parents=True)
            expected = {
                "source": "https://source.example", "target": "https://target.example",
                "workspace_id": "00000000-0000-0000-0000-000000000001", "schema_sha256": "schema", "blockers": [],
                "tables": {"user": spec([{"id": "user"}]), "issue": spec([{"id": "shared", "title": "same"}])},
            }
            actual = copy.deepcopy(expected)
            actual["tables"]["issue"] = spec([
                {"id": "shared", "title": "same"}, {"id": "local-only", "title": "Keep my work"},
            ])
            save(root / "schema.json", REFERENCE)
            for name in ("source", "target"):
                save(root / (name + ".json"), {"server_url": f"https://{name}.example", "token": "fixture"})
            save(root / "baselines/previous.json", expected)
            save(tmp / "source/read-stats.json", {})
            for i in range(5):
                (root / "backups" / f"recovery-{i}.dump").write_bytes(b"recovery point")
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            target = MagicMock()
            target.baseline.return_value = "previous"
            target.schema.return_value = {"matches": False, "projection_compatible": True, "added": ["issue.new_field"], "removed": [], "changed": []}
            jobs = MagicMock()
            with patch("cloud_sync_worker.Target", return_value=target), \
                    patch("cloud_sync_worker.capture", return_value=tmp / "snapshot.json"), \
                    patch("cloud_sync_worker.download_assets", return_value={}), \
                    patch("cloud_sync_worker.readback_assets", return_value={}), \
                    patch("cloud_sync_worker.make_plan", side_effect=[expected, actual]), \
                    patch("cloud_sync_worker.copy_assets") as copy_assets:
                run_job({"private_dir": str(root), "workspace_id": expected["workspace_id"], "origin": expected["target"]},
                        jobs, {"id": "run", "mode": "compare"}, tmp, io.StringIO())
            target.apply.assert_not_called()
            target.backup.assert_not_called()
            target.finish_rollup.assert_not_called()
            copy_assets.assert_not_called()
            report = jobs.finish.call_args.args[2]
            self.assertEqual(jobs.finish.call_args.args[1], "partial")
            self.assertEqual(report["written"], 0)
            self.assertEqual(len(report["tables"]["issue"]["extra"]), 1)
            self.assertEqual(report["schema"]["added"], ["issue.new_field"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})
