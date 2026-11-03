#!/usr/bin/env python3
"""Prepare a reviewable, transactional import into a dedicated self-host database.

This tool never connects to Cloud or a database. It emits a plan and SQL for an
operator to run with psql against the stopped, dedicated staging stack.
"""

import argparse
import base64
from collections import defaultdict
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
import uuid
import urllib.parse

from cloud_sync import SyncError, avatar_reference, canonical, digest, save


TABLES = {
    "user": "user", "workspace": "workspace", "members": "member",
    "statuses": "issue_status", "workflows": "issue_workflow",
    "projects": "project", "project_resources": "project_resource",
    "skills": "skill", "skill_files": "skill_file", "labels": "issue_label",
    "runtimes": "agent_runtime", "agents": "agent", "agent_skills": "agent_skill",
    "issues": "issue", "issue_labels": "issue_to_label", "agent_labels": "agent_to_label",
    "skill_labels": "skill_to_label", "chats": "chat_session", "tasks": "agent_task_queue",
    "comments": "comment", "chat_messages": "chat_message", "task_messages": "task_message",
    "views": "issue_view", "quick_actions": "quick_action", "attachments": "attachment",
    "pins": "pinned_item", "chat_pins": "chat_pinned_agent", "inbox": "inbox_item",
    "notification_preferences": "notification_preference", "view_preferences": "issue_view_preference",
    "subscribers": "issue_subscriber",
}
KEYS = {
    "agent_skill": ["agent_id", "skill_id"], "issue_to_label": ["issue_id", "label_id"],
    "agent_to_label": ["agent_id", "label_id"], "skill_to_label": ["skill_id", "label_id"],
    "issue_vcs_pull_request": ["issue_id", "pull_request_id"],
    "issue_pull_request": ["issue_id", "pull_request_id"],
    "issue_subscriber": ["issue_id", "user_type", "user_id"],
    "issue_view_preference": ["workspace_id", "user_id", "scope_type", "scope_id"],
}
NAMESPACE = uuid.UUID("df2bd8ba-c64a-4d32-965b-07b3bb5c675a")


def stored_status_category(category):
    # The API deliberately serializes lifecycle categories as legacy wire keys.
    # Match server/internal/issuestatus.ParseCategory at the database boundary.
    categories = {"backlog": "unstarted", "todo": "unstarted", "in_progress": "started",
                  "in_review": "started", "blocked": "started", "cancelled": "closed",
                  "unstarted": "unstarted", "started": "started", "done": "done", "closed": "closed"}
    if category not in categories:
        raise SyncError("unknown status category")
    return categories[category]


def identifier(value):
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", value):
        raise SyncError("invalid SQL identifier")
    return '"' + value + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def stable_id(*parts):
    return str(uuid.uuid5(NAMESPACE, canonical(parts).decode()))


def load_snapshot(path):
    snapshot = json.loads(Path(path).read_text())
    if snapshot.get("format") != 1:
        raise SyncError("unsupported snapshot format")
    for name, rows in snapshot["objects"].items():
        if snapshot["object_hashes"].get(name) != digest(rows):
            raise SyncError(f"snapshot checksum mismatch: {name}")
    return snapshot


def response(snapshot, root, path):
    metadata = snapshot["requests"][path]
    file = (root / metadata["file"]).resolve()
    if not file.is_relative_to(root.resolve()):
        raise SyncError("response path escapes snapshot directory")
    value = json.loads(file.read_text())
    if digest(value) != metadata["sha256"]:
        raise SyncError(f"response checksum mismatch: {path}")
    return value


def default_value(column, snapshot_time):
    if column["is_nullable"] == "YES":
        return None
    default = column["column_default"]
    if default in ("true", "false"):
        return default == "true"
    if default and re.fullmatch(r"-?\d+(\.\d+)?", default):
        return json.loads(default)
    if default in ("now()", "CURRENT_TIMESTAMP"):
        return snapshot_time
    match = re.fullmatch(r"'(.*)'::[a-z0-9_\[\] ]+", default or "")
    if match:
        value = match.group(1).replace("''", "'")
        if column["udt_name"] == "jsonb":
            return json.loads(value)
        if column["udt_name"].startswith("_") and value == "{}":
            return []
        return value
    raise SyncError(f"no value for required {column['table_name']}.{column['column_name']}")


def make_plan(snapshot_path, schema_path, target, stage=False, assets_path=None, readback=False, avatar_secret_path=None):
    snapshot = load_snapshot(snapshot_path)
    if target.rstrip("/") == snapshot["source"] and not readback:
        raise SyncError("target must differ from the Cloud source")
    root = Path(snapshot_path).parent
    schema = json.loads(Path(schema_path).read_text())
    columns = defaultdict(dict)
    for column in schema:
        columns[column["table_name"]][column["column_name"]] = column
    source = snapshot["objects"]
    ws = snapshot["workspace_id"]
    blocked = list(snapshot["blockers"])
    tables = defaultdict(list)
    omitted = {}
    assets = json.loads(Path(assets_path).read_text()) if assets_path else {}
    if assets_path:
        for attachment_id, asset in assets.items():
            if asset["file"] != str(uuid.UUID(attachment_id)):
                raise SyncError("invalid attachment file path")
            file = Path(assets_path).parent / asset["file"]
            if file.stat().st_size != asset["size"] or hashlib.sha256(file.read_bytes()).hexdigest() != asset["sha256"]:
                raise SyncError(f"attachment checksum mismatch: {attachment_id}")
    replacements = {}
    avatar_rows = {a["attachment_id"]: a for a in source.get("avatars", [])}
    avatar_urls = {}
    if avatar_rows:
        for avatar in source["avatars"]:
            if avatar_reference(avatar["url"], {urllib.parse.urlsplit(avatar["url"]).netloc}) != avatar:
                raise SyncError("invalid avatar storage mapping")
        if not readback and not avatar_secret_path:
            raise SyncError("owned avatars require --avatar-signing-key-file with the target JWT_SECRET")
        secret = Path(avatar_secret_path).read_bytes().strip() if avatar_secret_path else None
        for avatar in source["avatars"]:
            avatar_urls[avatar["url"]] = avatar["url"] if readback else signed_avatar_url(target, avatar["storage_key"], secret)
    source_origins = {urllib.parse.urlsplit(snapshot["source"]).netloc}
    for attachment in source["attachments"]:
        if attachment["id"] not in assets:
            blocked.append(f"attachment bytes not downloaded: {attachment['id']}")
        for field in ("url", "markdown_url"):
            if attachment.get(field):
                source_origins.add(urllib.parse.urlsplit(attachment[field]).netloc)
                replacements[attachment[field]] = target.rstrip("/") + f"/api/attachments/{attachment['id']}/download"

    def rewrite(value):
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
            return rewrite_attachment_links(value, {a["id"] for a in source["attachments"]}, source_origins, target)
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        return value

    for collection, table in TABLES.items():
        if not columns[table]:
            raise SyncError(f"missing target table: {table}")
        discarded = set()
        terminal_ids = {r["id"] for r in tables["agent_task_queue"]} if table == "task_message" else set()
        for original in source.get(collection, []):
            row = dict(original)
            if "workspace_id" in row and row["workspace_id"] != ws:
                raise SyncError(f"cross-workspace row: {collection}")
            if table == "workspace":
                row["issue_counter"] = max((i["number"] for i in source["issues"]), default=0)
            elif table == "issue_status":
                row["category"] = stored_status_category(row["category"])
            elif table == "issue_workflow":
                row["initial_status_key"] = "todo"
                if row["steps"] and not any(s["status_key"] == "todo" for s in row["steps"]):
                    row["initial_status_key"] = row["steps"][0]["status_key"]
            elif table == "agent_runtime":
                row.update(status="offline", daemon_id=None, last_seen_at=None, profile_id=None)
            elif table == "agent":
                row["status"] = "offline"
                for item in row.get("invocation_targets", []):
                    item = dict(item)
                    item.update(agent_id=row["id"], created_by=row["owner_id"])
                    item.setdefault("id", stable_id("invocation", row["id"], item.get("target_type"), item.get("target_id")))
                    tables["agent_invocation_target"].append(item)
            elif table == "agent_skill":
                row = {"agent_id": row["agent_id"], "skill_id": row["id"], "enabled": row.get("enabled", True)}
            elif table == "issue":
                row["duplicate_of_issue_id"] = row.get("duplicate_of")
            elif table == "comment":
                row["workspace_id"] = ws
                for item in row.get("reactions", []):
                    tables["comment_reaction"].append({**item, "workspace_id": ws})
            elif table == "chat_session":
                row["pinned_at"] = row["updated_at"] if row.get("pinned") else None
            elif table in ("chat_pinned_agent", "notification_preference", "issue_view_preference"):
                row.update(workspace_id=ws, user_id=source["user"][0]["id"])
                if table != "issue_view_preference":
                    row["id"] = stable_id(table, ws, row["user_id"], row.get("agent_id"))
            elif table == "agent_task_queue":
                if row["status"] not in ("completed", "failed", "cancelled"):
                    continue
                if row.get("issue_id") == "":
                    row["issue_id"] = None
                if row.get("runtime_id") == "":
                    row["runtime_id"] = None
                attribution = row.get("attribution") or {}
                row.update(
                    accountable_user_id=(attribution.get("initiator") or {}).get("id"),
                    originator_user_id=(attribution.get("originator") or {}).get("id"),
                    originator_source=attribution.get("source"),
                    trigger_evidence_kind=(attribution.get("evidence") or {}).get("kind"),
                    trigger_evidence_ref_id=(attribution.get("evidence") or {}).get("ref_id"),
                )
                for field in ("delegated_from_task_id", "retry_of_task_id", "rerun_of_task_id", "accountable_user_id", "rule_version_id"):
                    if field in attribution:
                        row[field] = attribution[field]
                cancelled = row.get("cancelled_by") or {}
                for key in ("type", "id", "name"):
                    row["cancelled_by_" + key] = cancelled.get(key)
                for usage in row.get("usage") or []:
                    tables["task_usage"].append({
                        **usage, "id": stable_id("usage", row["id"], usage["provider"], usage["model"]),
                        "task_id": row["id"], "created_at": row["created_at"],
                        "updated_at": snapshot["exported_at"],
                    })
            elif table == "task_message":
                if row["task_id"] not in terminal_ids:
                    continue
                row["id"] = stable_id("message", row["task_id"], row["seq"])
            elif table == "attachment":
                if row["id"] not in assets:
                    continue
                asset = assets[row["id"]]
                if asset["size"] != row["size_bytes"]:
                    raise SyncError(f"attachment byte count mismatch: {row['id']}")
                key = avatar_rows[row["id"]]["storage_key"] if row["id"] in avatar_rows else f"cloud-sync/{row['id']}"
                row["url"] = "/uploads/" + key
            if row.get("avatar_url") in avatar_urls:
                row["avatar_url"] = avatar_urls[row["avatar_url"]]
            for field in ("description", "content"):
                if field in row:
                    row[field] = rewrite(row[field])
            discarded.update(set(row) - set(columns[table]))
            tables[table].append({k: v for k, v in row.items() if k in columns[table]})
        omitted[collection] = sorted(discarded)

    # Activity rows preserve their original IDs, actors and event times. Comments
    # are already imported through the complete comment surface above.
    for issue in source["issues"]:
        timeline = response(snapshot, root, f"/api/issues/{issue['id']}/timeline")
        if isinstance(timeline, dict):
            timeline = timeline["entries"]
        for entry in timeline:
            if entry["type"] == "activity":
                tables["activity_log"].append({
                    **{k: entry.get(k) for k in ("id", "actor_type", "actor_id", "action", "details", "created_at")},
                    "workspace_id": ws, "issue_id": issue["id"],
                })
        wakeups = response(snapshot, root, f"/api/issues/{issue['id']}/wakeups")
        if wakeups:
            blocked.append(f"wakeup configuration needs separate migration: {issue['id']}")
        prs = response(snapshot, root, f"/api/issues/{issue['id']}/pull-requests")
        if prs.get("pull_requests"):
            blocked.append(f"PR links need separate migration: {issue['id']}")
    for collection in ("squads", "autopilots", "properties"):
        if source.get(collection):
            blocked.append(f"unsupported nonempty collection: {collection}")

    projected = {}
    for table, rows in tables.items():
        if not rows:
            continue
        allowed = columns[table]
        fields = sorted(set().union(*(row.keys() for row in rows)))
        if any(field not in allowed for field in fields):
            raise SyncError(f"unmapped fields in {table}")
        keys = KEYS.get(table, ["id"])
        seen = set()
        for row in rows:
            for field in fields:
                if field not in row:
                    row[field] = default_value(allowed[field], snapshot["exported_at"])
                if row[field] == "" and allowed[field]["udt_name"] == "uuid":
                    row[field] = None
                if row[field] is None and allowed[field]["is_nullable"] == "NO":
                    raise SyncError(f"null required field: {table}.{field}")
            key = tuple(row[k] for k in keys)
            if key in seen:
                raise SyncError(f"duplicate import key in {table}")
            seen.add(key)
        for field, column in allowed.items():
            if field not in fields and column["is_nullable"] == "NO" and column["column_default"] is None:
                raise SyncError(f"missing required field: {table}.{field}")
        rows.sort(key=lambda row: tuple(str(row[k]) for k in keys))
        projected[table] = {"keys": keys, "fields": fields, "rows": rows, "sha256": digest(rows)}
    if blocked and not stage:
        raise SyncError("cutover blocked; use --stage for a partial rehearsal: " + "; ".join(blocked))
    return {
        "format": 1, "source": snapshot["source"], "workspace_id": ws, "target": target,
        "snapshot_sha256": digest(snapshot), "schema_sha256": digest(schema),
        "snapshot_import_complete": not blocked, "blockers": blocked, "tables": projected,
        "api_fields_not_stored_directly": omitted,
        "intentional_changes": [
            "Runtimes are offline and detached from Cloud daemon identities; agents are offline.",
            "Only terminal runs and their messages are staged; active runs require a fresh export.",
            "Attachment references use the target authenticated download endpoint.",
            "Task message and usage IDs are deterministic because the API omits storage row IDs.",
            "Authentication tokens, external integration credentials and daemon sessions are not copied.",
            "Inbox bodies retain the API preview; full comment bodies are stored separately.",
            "Only API-visible active subscriptions and current-user preferences are available.",
            "Owned avatar bytes are copied and their capability URLs are signed with the target key.",
        ],
    }


def signed_avatar_url(target, key, secret):
    if not secret:
        raise SyncError("empty target avatar signing secret")
    signing_key = hashlib.sha256(b"avatar-url:" + secret).digest()
    signature = base64.urlsafe_b64encode(hmac.new(signing_key, key.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return target.rstrip("/") + "/api/avatars/" + signature + "/" + key


def rewrite_attachment_links(value, attachment_ids, source_origins, target):
    pattern = r"(?:https?://([^/\s]+))?/api/attachments/([0-9a-fA-F-]{36})/(?:download|content)\b"
    def replace(match):
        if match.group(1) and match.group(1) not in source_origins:
            return match.group(0)
        if match.group(2) not in attachment_ids:
            return match.group(0)
        return target.rstrip("/") + f"/api/attachments/{match.group(2)}/download"
    return re.sub(pattern, replace, value)


def chunks(rows, max_bytes=2_000_000):
    batch, size = [], 0
    for row in rows:
        row_size = len(canonical(row))
        if batch and size + row_size > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += row_size
    if batch:
        yield batch


def sql_for_plan(plan, previous=None, dry_run=False):
    if previous and any(previous.get(k) != plan.get(k) for k in ("source", "workspace_id", "target", "schema_sha256")):
        raise SyncError("previous plan belongs to a different source, target or schema")
    sql = ["\\set ON_ERROR_STOP on", "SET standard_conforming_strings = on;", "BEGIN;",
           "SET LOCAL session_replication_role = replica;",
           "SELECT 1 / (NOT EXISTS (SELECT 1 FROM workspace WHERE id <> " + literal(plan["workspace_id"]) + "::uuid))::int AS dedicated_workspace_guard;"]
    for table, spec in plan["tables"].items():
        if spec["sha256"] != digest(spec["rows"]):
            raise SyncError(f"plan checksum mismatch: {table}")
        target = "public." + identifier(table)
        incoming, prior = identifier("_sync_" + table), identifier("_prior_" + table)
        fields, keys = spec["fields"], spec["keys"]
        cols = ", ".join(map(identifier, fields))
        sql.extend([f"LOCK TABLE {target} IN SHARE ROW EXCLUSIVE MODE;",
                    f"CREATE TEMP TABLE {incoming} (LIKE {target} INCLUDING DEFAULTS) ON COMMIT DROP;",
                    f"CREATE TEMP TABLE {prior} (LIKE {target} INCLUDING DEFAULTS) ON COMMIT DROP;"])
        previous_spec = previous["tables"].get(table) if previous else None
        if previous_spec and previous_spec["sha256"] != digest(previous_spec["rows"]):
            raise SyncError(f"previous plan checksum mismatch: {table}")
        if previous_spec:
            incoming_by_key = {tuple(row[k] for k in keys): row for row in spec["rows"]}
            previous_spec = {**previous_spec, "rows": [row for row in previous_spec["rows"]
                if tuple(row[k] for k in keys) in incoming_by_key
                and incoming_by_key[tuple(row[k] for k in keys)] != row]}
        for temp, dataset in ((incoming, spec), (prior, previous_spec)):
            if not dataset:
                continue
            dataset_cols = ", ".join(map(identifier, dataset["fields"]))
            for batch in chunks(dataset["rows"]):
                sql.append(f"INSERT INTO {temp} ({dataset_cols}) SELECT {dataset_cols} FROM jsonb_populate_recordset(NULL::{target}, {literal(canonical(batch).decode())}::jsonb);")
        if not previous:
            sql.append(f"INSERT INTO {prior} SELECT * FROM {incoming};")
        def joined(left, right):
            return " AND ".join(f"{left}.{identifier(k)} = {right}.{identifier(k)}" for k in keys)
        def record(alias):
            return "ROW(" + ", ".join(f"{alias}.{identifier(k)}" for k in fields) + ")"
        sql.append(f"SELECT 1 / (NOT EXISTS (SELECT 1 FROM {target} t JOIN {incoming} s ON {joined('t','s')} LEFT JOIN {prior} p ON {joined('s','p')} WHERE {record('t')} IS DISTINCT FROM {record('s')} AND (p.{identifier(keys[0])} IS NULL OR {record('t')} IS DISTINCT FROM {record('p')})))::int AS {identifier('no_conflicts_' + table)};")
        if previous_spec:
            sql.append(f"SELECT 1 / (NOT EXISTS (SELECT 1 FROM {prior} p LEFT JOIN {target} t ON {joined('p','t')} WHERE t.{identifier(keys[0])} IS NULL))::int AS {identifier('no_missing_' + table)};")
        assignments = ", ".join(f"{identifier(k)} = EXCLUDED.{identifier(k)}" for k in fields if k not in keys)
        action = (f"DO UPDATE SET {assignments} WHERE {record('t')} IS DISTINCT FROM {record('EXCLUDED')}"
                  if assignments else "DO NOTHING")
        sql.append(f"WITH written AS (INSERT INTO {target} AS t ({cols}) SELECT {cols} FROM {incoming} ON CONFLICT ({', '.join(map(identifier, keys))}) {action} RETURNING 1) SELECT json_build_object('written', count(*)) FROM written;")
        sql.append(f"SELECT 1 / (NOT EXISTS (SELECT 1 FROM {incoming} s LEFT JOIN {target} t ON {joined('t','s')} WHERE {record('t')} IS DISTINCT FROM {record('s')}))::int AS {identifier('verified_' + table)};")
        sql.append(f"SELECT json_build_object('table', {literal(table)}, 'verified_rows', count(*)) FROM {incoming};")
    sql.append("ROLLBACK;" if dry_run else "COMMIT;")
    return "\n".join(sql) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--readback", action="store_true", help="prepare a comparison plan only; emit no import SQL")
    parser.add_argument("--avatar-signing-key-file", type=Path, help="private file containing target JWT_SECRET; never stored in plans")
    args = parser.parse_args()
    try:
        if (args.output / "plan.json").exists():
            raise SyncError("plan already exists; use a new output directory to preserve the applied baseline")
        plan = make_plan(args.snapshot, args.schema, args.target, args.stage, args.assets, args.readback, args.avatar_signing_key_file)
        previous = json.loads(args.previous_plan.read_text()) if args.previous_plan else None
        save(args.output / "plan.json", plan)
        for name, dry in (() if args.readback else (("import.sql", False), ("dry-run.sql", True))):
            file = args.output / name
            file.write_text(sql_for_plan(plan, previous, dry))
            file.chmod(0o600)
        print(json.dumps({"rows": {k: len(v["rows"]) for k, v in plan["tables"].items()}, "snapshot_import_complete": plan["snapshot_import_complete"], "blockers": plan["blockers"]}))
    except (SyncError, OSError, ValueError, KeyError) as exc:
        print(f"cloud-sync-import: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
