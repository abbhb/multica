#!/usr/bin/env python3
"""Read-only Cloud API snapshot and self-host migration tooling (Python stdlib)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class SyncError(Exception):
    pass


class HTTPReadError(SyncError):
    def __init__(self, path, status):
        super().__init__(f"GET {path}: HTTP {status}")
        self.status = status


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value) + b"\n")
    temporary.chmod(0o600)
    temporary.replace(path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("API redirect refused; credentials were not forwarded")


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origins):
        self.origins = origins

    def check(self, url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc) not in self.origins or parsed.username:
            raise SyncError("attachment URL is outside the API/CDN origins")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.check(newurl)
        return urllib.request.Request(newurl, headers={"Accept-Encoding": "identity"})


class API:
    def __init__(self, base, token, workspace):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise SyncError("invalid API URL")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise SyncError("non-loopback API requires HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SyncError("API URL must not contain credentials, query or fragment")
        self.base = base.rstrip("/")
        self.workspace = str(uuid.UUID(workspace))
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path):
        if not path.startswith("/api/") or "#" in path:
            raise SyncError("only relative /api/ reads are supported")
        req = urllib.request.Request(self.base + path, headers={
            "Authorization": "Bearer " + self.token,
            "X-Workspace-ID": self.workspace,
            "Accept": "application/json",
        })
        for attempt in range(4):
            try:
                with self.opener.open(req, timeout=90) as response:
                    return json.load(response), dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 502, 503, 504) or attempt == 3:
                    raise HTTPReadError(path, exc.code) from None
                time.sleep(min(10, 2 ** attempt))
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.IncompleteRead) as exc:
                if attempt == 3:
                    raise SyncError(f"GET {path}: transport failure ({type(exc).__name__}); resume the checkpoint") from None
                time.sleep(min(10, 2 ** attempt))
        raise SyncError("unreachable retry state")


def header(headers, name):
    return next((v for k, v in headers.items() if k.lower() == name.lower()), "")


def rows_of(value, key=None):
    rows = value.get(key) if key and isinstance(value, dict) else value
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SyncError(f"expected a list of objects ({key or 'array'})")
    return rows


def unique(rows, key="id"):
    seen = set()
    for row in rows:
        identifier = row.get(key)
        if identifier is None or identifier in seen:
            raise SyncError(f"missing or duplicate {key} in API collection")
        seen.add(identifier)
    return rows


def download_assets(api, snapshot_path, output):
    snapshot = json.loads(Path(snapshot_path).read_text())
    if snapshot["source"] != api.base or snapshot["workspace_id"] != api.workspace:
        raise SyncError("asset source does not match snapshot")
    attachments = snapshot["objects"]["attachments"]
    if snapshot["object_hashes"]["attachments"] != digest(attachments):
        raise SyncError("attachment snapshot checksum mismatch")
    config, _ = api.get("/api/config")
    base = urllib.parse.urlsplit(api.base)
    origins = {(base.scheme, base.netloc)}
    if config.get("cdn_domain"):
        origins.add(("https", config["cdn_domain"]))
    redirect = AssetRedirect(origins)
    opener = urllib.request.build_opener(redirect)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = json.loads((output / "assets.json").read_text()) if (output / "assets.json").exists() else {}
    manifest = {}

    def download(attachment):
        attachment_id = str(uuid.UUID(attachment["id"]))
        cached = existing.get(attachment_id)
        file = output / attachment_id
        if (cached and cached.get("file") == attachment_id and file.exists()
                and file.stat().st_size == cached["size"] == attachment["size_bytes"]
                and hashlib.sha256(file.read_bytes()).hexdigest() == cached["sha256"]):
            return attachment_id, {**cached, "filename": attachment["filename"], "content_type": attachment["content_type"]}
        data, _ = api.get(f"/api/attachments/{attachment_id}")
        url = urllib.parse.urljoin(api.base + "/", data["download_url"])
        redirect.check(url)
        temporary = output / (attachment_id + ".tmp")
        sha, size = hashlib.sha256(), 0
        try:
            # The metadata endpoint mints a download capability. No bearer token
            # ever reaches the CDN or a redirect destination.
            request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
            with opener.open(request, timeout=90) as response, temporary.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > attachment["size_bytes"]:
                        raise SyncError(f"attachment exceeds declared size: {attachment_id}")
                    sha.update(chunk)
                    stream.write(chunk)
            if size != attachment["size_bytes"]:
                raise SyncError(f"attachment is incomplete: {attachment_id}")
            temporary.chmod(0o600)
            temporary.replace(file)
        except urllib.error.URLError:
            raise SyncError(f"attachment download failed: {attachment_id}") from None
        return attachment_id, {"file": attachment_id, "size": size, "sha256": sha.hexdigest(), "filename": attachment["filename"], "content_type": attachment["content_type"]}

    with ThreadPoolExecutor(max_workers=3) as pool:
        for attachment_id, metadata in pool.map(download, attachments):
            manifest[attachment_id] = metadata
            save(output / "assets.json", manifest)
            print(f"attachment {len(manifest)}/{len(attachments)}", flush=True)
    save(output / "assets.json", manifest)
    return manifest


class Exporter:
    def __init__(self, api, output, resume=False):
        self.api = api
        self.output = Path(output)
        if (self.output / "snapshot.json").exists():
            raise SyncError("snapshot already exists; use a new output directory")
        context = {"source": api.base, "workspace_id": api.workspace}
        context_file = self.output / "source.json"
        if context_file.exists() and json.loads(context_file.read_text()) != context:
            raise SyncError("checkpoint belongs to another source or workspace")
        save(context_file, context)
        self.objects = {}
        self.requests = {}
        self.blockers = []
        self.unavailable = []
        self.resume = resume

    def get(self, path):
        key = hashlib.sha256(path.encode()).hexdigest()
        checkpoint = self.output / "responses" / (key + ".checkpoint.json")
        if self.resume and checkpoint.exists():
            cached = json.loads(checkpoint.read_text())
            data = json.loads((self.output / "responses" / (key + ".json")).read_text())
            if cached["path"] != path or cached["sha256"] != digest(data):
                raise SyncError("checkpoint checksum mismatch")
            headers = cached["headers"]
        else:
            data, headers = self.api.get(path)
            save(self.output / "responses" / (key + ".json"), data)
            save(checkpoint, {"path": path, "sha256": digest(data), "headers": {
                k: v for k, v in headers.items() if k.lower().startswith(("x-multica-", "x-agent-"))
            }})
        self.requests[path] = {"file": f"responses/{key}.json", "sha256": digest(data)}
        for name, value in headers.items():
            if "truncated" in name.lower() and value.lower() not in ("", "false", "0"):
                self.blockers.append(f"truncated response: {path} ({name})")
        return data, headers

    def collection(self, name, path, key=None):
        value, _ = self.get(path)
        rows = unique(rows_of(value, key))
        if isinstance(value, dict) and "total" in value and value["total"] != len(rows):
            raise SyncError(f"incomplete collection: {path}")
        self.objects[name] = rows
        print(f"{name}: {len(rows)}", flush=True)
        return rows

    def prefetch(self, paths):
        # Each response has its own checkpoint. Keep Cloud load bounded.
        with ThreadPoolExecutor(max_workers=3) as pool:
            for _ in pool.map(self.get, paths):
                pass
        self.resume = True

    def issues(self):
        rows, total = [], None
        while total is None or len(rows) < total:
            value, _ = self.get(f"/api/issues?limit=100&offset={len(rows)}&sort=created_at&direction=asc")
            page = rows_of(value, "issues")
            if total is not None and total != value.get("total"):
                raise SyncError("issue count changed during pagination; export again")
            total = value.get("total")
            if not isinstance(total, int) or (not page and len(rows) != total):
                raise SyncError("invalid issue pagination")
            rows.extend(page)
            unique(rows)
        if len(rows) != total:
            raise SyncError("issue count mismatch")
        return rows

    def export(self):
        me, _ = self.get("/api/me")
        workspace, _ = self.get(f"/api/workspaces/{self.api.workspace}")
        self.objects["user"] = [me]
        self.objects["workspace"] = [workspace]
        specifications = [
            ("members", f"/api/workspaces/{self.api.workspace}/members", None),
            ("projects", "/api/projects", "projects"),
            ("agents", "/api/agents?include_archived=true", None),
            ("skills", "/api/skills", None),
            ("labels", "/api/labels", "labels"),
            ("properties", "/api/properties", "properties"),
            ("statuses", "/api/issue-statuses?include_archived=true", "statuses"),
            ("workflows", "/api/issue-workflows", "workflows"),
            ("squads", "/api/squads", None),
            ("autopilots", "/api/autopilots", "autopilots"),
            ("chats", "/api/chat/sessions?status=all", None),
            ("runtimes", "/api/runtimes", None),
            ("quick_actions", "/api/quick-actions", "quick_actions"),
            ("pins", "/api/pins?include=view", None),
            ("inbox", "/api/inbox", None),
        ]
        self.prefetch(path for _, path, _ in specifications)
        for name, path, key in specifications:
            self.collection(name, path, key)
        self.objects["chat_pins"] = rows_of(self.get("/api/chat/pinned-agents")[0])
        self.objects["notification_preferences"] = [self.get("/api/notification-preferences")[0]]
        cursor, seen_cursors = None, set()
        while True:
            path = "/api/inbox/archived/page?limit=100"
            if cursor:
                path += "&cursor=" + urllib.parse.quote(cursor)
            page, _ = self.get(path)
            self.objects["inbox"].extend(rows_of(page, "items"))
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                raise SyncError("archived inbox cursor did not advance")
            seen_cursors.add(cursor)
        unique(self.objects["inbox"])
        views = {}
        scopes = ["scope_type=workspace", "scope_type=my"]
        scopes.extend("scope_type=project&scope_id=" + project["id"] for project in self.objects["projects"])
        self.objects["view_preferences"] = []
        for query in scopes:
            preference, _ = self.get("/api/issue-view-preferences?" + query)
            if preference.get("updated_at"):
                self.objects["view_preferences"].append(preference)
            value, _ = self.get("/api/issue-views?" + query)
            for view in rows_of(value):
                views[view["id"]] = view
        self.objects["views"] = list(views.values())
        for scope in ("sidebar", "sidebar_projects"):
            path = "/api/issue-view-preferences?scope_type=" + scope
            try:
                preference, _ = self.get(path)
            except HTTPReadError as exc:
                if exc.status not in (400, 404):
                    raise
                self.unavailable.append(path)
                continue
            if preference.get("updated_at"):
                self.objects["view_preferences"].append(preference)
        self.objects["issues"] = self.issues()
        print(f"issues: {len(self.objects['issues'])}", flush=True)
        for name in ("project_resources", "skill_files", "agent_skills", "comments", "attachments", "tasks", "chat_messages", "task_messages", "issue_labels", "agent_labels", "skill_labels", "subscribers"):
            self.objects[name] = []

        for project in self.objects["projects"]:
            value, _ = self.get(f"/api/projects/{project['id']}/resources")
            self.objects["project_resources"].extend(rows_of(value, "resources"))
        self.prefetch(f"/api/skills/{skill['id']}?include=content" for skill in self.objects["skills"])
        for index, skill in enumerate(self.objects["skills"]):
            detail, _ = self.get(f"/api/skills/{skill['id']}?include=content")
            self.objects["skill_files"].extend(detail.pop("files", []))
            self.objects["skills"][index] = detail
            for label in skill.get("labels", []):
                self.objects["skill_labels"].append({"skill_id": skill["id"], "label_id": label["id"]})

        tasks = {}
        self.prefetch(f"/api/agents/{agent['id']}/{suffix}" for agent in self.objects["agents"]
                      for suffix in ("skills", "labels", "tasks?limit=200&include_usage=true"))
        for agent in self.objects["agents"]:
            agent_id = agent["id"]
            if agent.get("has_custom_env") or agent.get("mcp_config_redacted"):
                self.blockers.append(f"agent secrets require explicit transfer: {agent_id}")
            value, _ = self.get(f"/api/agents/{agent_id}/skills")
            for skill in rows_of(value):
                self.objects["agent_skills"].append({"agent_id": agent_id, **skill})
            value, _ = self.get(f"/api/agents/{agent_id}/labels")
            for label in rows_of(value, "labels"):
                self.objects["agent_labels"].append({"agent_id": agent_id, "label_id": label["id"]})
            before, seen = "", set()
            while True:
                value, headers = self.get(f"/api/agents/{agent_id}/tasks?limit=200&include_usage=true" + ("&before=" + urllib.parse.quote(before) if before else ""))
                for task in rows_of(value):
                    if task["id"] in seen:
                        raise SyncError("agent task pagination repeated a task")
                    seen.add(task["id"])
                    tasks[task["id"]] = task
                next_before = header(headers, "X-Agent-Tasks-Next-Cursor")
                if not next_before:
                    break
                if next_before == before:
                    raise SyncError("agent task cursor did not advance")
                before = next_before

        self.prefetch(
            f"/api/issues/{issue['id']}{suffix}"
            for issue in self.objects["issues"]
            for suffix in ("", "/comments", "/attachments", "/task-runs", "/labels", "/wakeups", "/pull-requests", "/timeline", "/subscribers")
        )
        for index, issue in enumerate(self.objects["issues"]):
            issue_id = issue["id"]
            detail, _ = self.get(f"/api/issues/{issue_id}")
            self.objects["issues"][index] = detail
            self.objects["subscribers"].extend(rows_of(self.get(f"/api/issues/{issue_id}/subscribers")[0]))
            value, _ = self.get(f"/api/issues/{issue_id}/comments")
            self.objects["comments"].extend(rows_of(value))
            for comment in rows_of(value):
                self.objects["attachments"].extend(comment.get("attachments") or [])
            value, _ = self.get(f"/api/issues/{issue_id}/attachments")
            self.objects["attachments"].extend(rows_of(value))
            value, _ = self.get(f"/api/issues/{issue_id}/task-runs")
            for task in rows_of(value):
                tasks[task["id"]] = task
            value, _ = self.get(f"/api/issues/{issue_id}/labels")
            for label in rows_of(value, "labels"):
                self.objects["issue_labels"].append({"issue_id": issue_id, "label_id": label["id"]})
            # These reads establish whether omitted operational records block cutover.
            for child in ("wakeups", "pull-requests", "timeline"):
                self.get(f"/api/issues/{issue_id}/{child}")
            print(f"issue {index + 1}/{len(self.objects['issues'])}", flush=True)

        for chat in self.objects["chats"]:
            cursor, seen = {}, set()
            while True:
                query = urllib.parse.urlencode({"limit": 100, **cursor})
                value, _ = self.get(f"/api/chat/sessions/{chat['id']}/messages/page?{query}")
                for message in rows_of(value, "messages"):
                    if message["id"] in seen:
                        raise SyncError("chat pagination repeated a message")
                    seen.add(message["id"])
                    self.objects["chat_messages"].append(message)
                    self.objects["attachments"].extend(message.get("attachments") or [])
                if not value.get("has_more"):
                    break
                next_cursor = value.get("next_cursor")
                if not next_cursor or not value["messages"]:
                    raise SyncError("chat cursor missing")
                cursor = {"before_created_at": next_cursor["created_at"], "before_id": next_cursor["id"]}
        self.objects["tasks"] = list(tasks.values())
        self.prefetch(f"/api/tasks/{task['id']}/messages" for task in self.objects["tasks"])
        for index, task in enumerate(self.objects["tasks"]):
            if task["status"] not in ("completed", "failed", "cancelled"):
                self.blockers.append(f"nonterminal task: {task['id']} ({task['status']})")
            value, _ = self.get(f"/api/tasks/{task['id']}/messages")
            self.objects["task_messages"].extend(rows_of(value))
            print(f"run {index + 1}/{len(tasks)}", flush=True)
        self.objects["attachments"] = list({row["id"]: row for row in self.objects["attachments"]}.values())
        # Uploads embedded directly in markdown may not be linked to the issue's
        # attachment list. Resolve these through the authenticated metadata API.
        config, _ = self.get("/api/config")
        origins = {urllib.parse.urlsplit(self.api.base).netloc}
        if config.get("attachment_public_url"):
            origins.add(urllib.parse.urlsplit(config["attachment_public_url"]).netloc)
        known_ids = {row["id"] for row in self.objects["attachments"]}
        referenced = set()
        avatars = {}
        for collection in ("user", "workspace", "agents", "projects", "skills"):
            for row in self.objects[collection]:
                reference = avatar_reference(row.get("avatar_url"), origins)
                if reference:
                    avatars[reference["url"]] = reference
                    referenced.add(reference["attachment_id"])
        self.objects["avatars"] = list(avatars.values())
        for collection in ("issues", "comments", "chat_messages", "skill_files", "skills", "projects"):
            for row in self.objects[collection]:
                for field in ("description", "content"):
                    referenced.update(attachment_references(row.get(field), origins))
        for attachment_id in sorted(referenced - known_ids):
            attachment, _ = self.get(f"/api/attachments/{attachment_id}")
            self.objects["attachments"].append(attachment)
        by_id = {a["id"]: a for a in self.objects["attachments"]}
        for avatar in self.objects["avatars"]:
            attachment = by_id[avatar["attachment_id"]]
            if not attachment["content_type"].startswith("image/") or any(attachment.get(k) for k in
                    ("issue_id", "comment_id", "chat_session_id", "chat_message_id", "task_id")):
                raise SyncError("avatar must reference a standalone image upload")
        snapshot = {
            "format": 1, "source": self.api.base, "workspace_id": self.api.workspace,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "objects": self.objects, "requests": self.requests, "blockers": sorted(set(self.blockers)),
            "unavailable_optional_surfaces": self.unavailable,
            "object_hashes": {name: digest(rows) for name, rows in self.objects.items()},
        }
        save(self.output / "snapshot.json", snapshot)
        print(json.dumps({"counts": {k: len(v) for k, v in self.objects.items()}, "blockers": self.blockers}), flush=True)
        return snapshot


def attachment_references(value, origins):
    if not isinstance(value, str):
        return set()
    result = set()
    pattern = r"(?:https?://([^/\s]+))?/api/attachments/([0-9a-fA-F-]{36})/(?:download|content)\b"
    for match in re.finditer(pattern, value):
        if match.group(1) and match.group(1) not in origins:
            continue
        try:
            result.add(str(uuid.UUID(match.group(2))))
        except ValueError:
            continue
    return result


def avatar_reference(value, origins):
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.netloc not in origins or not parsed.path.startswith("/api/avatars/"):
        return None
    match = re.fullmatch(r"/api/avatars/[^/]+/((?:workspaces|users)/([0-9a-f-]{36})/([0-9a-f-]{36})\.(?:png|jpg|jpeg|gif|webp|avif|bmp|ico))", parsed.path)
    if not match:
        raise SyncError("unsupported owned avatar storage key")
    uuid.UUID(match.group(2))
    return {"url": value, "storage_key": match.group(1), "attachment_id": str(uuid.UUID(match.group(3)))}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="GET-only snapshot of one accessible Cloud workspace")
    export.add_argument("--config", type=Path, required=True, help="existing CLI config; never modified")
    export.add_argument("--workspace", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--resume", action="store_true", help="reuse verified reads from an interrupted export; re-export fresh before cutover")
    assets = sub.add_parser("assets", help="download attachment bytes with SHA-256 verification")
    assets.add_argument("--config", required=True, type=Path)
    assets.add_argument("--snapshot", required=True, type=Path)
    assets.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        if args.command == "assets":
            snapshot = json.loads(args.snapshot.read_text())
            download_assets(API(config["server_url"], config["token"], snapshot["workspace_id"]), args.snapshot, args.output)
            return 0
        snapshot = Exporter(API(config["server_url"], config["token"], args.workspace), args.output, args.resume).export()
        return 2 if snapshot["blockers"] else 0
    except (SyncError, OSError, ValueError, KeyError) as exc:
        print(f"cloud-sync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
