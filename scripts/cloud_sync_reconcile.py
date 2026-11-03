#!/usr/bin/env python3
"""Compare the stored projections of a Cloud snapshot and a target API readback.

Generate both plans with cloud_sync_import.py against the same schema and target
origin. Differences contain keys and field names, never user content. This is
an API-visible reconciliation, not evidence that inaccessible Cloud tables match.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from cloud_sync import SyncError, canonical, digest, save


# The API omits this timestamp; the planner synthesizes it at export time.
SYNTHETIC_FIELDS = {"task_usage": {"updated_at"}}


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return value


def reconcile(expected, actual):
    for field in ("workspace_id", "target", "schema_sha256"):
        if expected[field] != actual[field]:
            raise SyncError(f"readback plan mismatch: {field}")
    results = {}
    for table in sorted(set(expected["tables"]) | set(actual["tables"])):
        left, right = expected["tables"].get(table), actual["tables"].get(table)
        keys = (left or right)["keys"]
        if left and right and left["keys"] != right["keys"]:
            raise SyncError(f"readback key mismatch: {table}")
        def indexed(spec):
            if not spec:
                return {}
            if spec["sha256"] != digest(spec["rows"]):
                raise SyncError(f"plan checksum mismatch: {table}")
            return {canonical([row[k] for k in keys]).decode(): normalize({
                k: v for k, v in row.items() if k not in SYNTHETIC_FIELDS.get(table, set())
            }) for row in spec["rows"]}
        a, b = indexed(left), indexed(right)
        changes = []
        for key in sorted(a.keys() & b.keys()):
            fields = sorted(k for k in set(a[key]) | set(b[key]) if a[key].get(k) != b[key].get(k))
            if fields:
                changes.append({"key": key, "fields": fields})
        results[table] = {"expected": len(a), "actual": len(b),
                          "missing": sorted(a.keys() - b.keys()), "extra": sorted(b.keys() - a.keys()),
                          "changed": changes, "expected_sha256": digest(a), "actual_sha256": digest(b)}
    differences = sum(len(v[k]) for v in results.values() for k in ("missing", "extra", "changed"))
    blockers = expected["blockers"] + actual["blockers"]
    return {"api_projection_matches": differences == 0, "difference_count": differences,
            "snapshot_blockers": blockers, "tables": results,
            "ignored_synthetic_fields": {k: sorted(v) for k, v in SYNTHETIC_FIELDS.items()},
            "cutover_requires": "Fresh source snapshot, no blockers, matching readback and attachment hashes, then user cutover testing."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-plan", required=True, type=Path)
    parser.add_argument("--actual-plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = reconcile(json.loads(args.expected_plan.read_text()), json.loads(args.actual_plan.read_text()))
        save(args.output, report)
        print(json.dumps({k: v for k, v in report.items() if k != "tables"}))
        return 0 if report["api_projection_matches"] and not report["snapshot_blockers"] else 2
    except (SyncError, OSError, ValueError, KeyError) as exc:
        print(f"cloud-sync-reconcile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
