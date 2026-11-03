#!/usr/bin/env python3
"""Run operator-requested, one-way incremental imports on a dedicated rehearsal host."""

import argparse
import fcntl
import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from urllib.request import Request

from cloud_sync import API, SyncError, digest, download_assets, save
from cloud_sync_import import make_plan, sql_for_plan
from cloud_sync_jobs import Jobs, now
from cloud_sync_reconcile import reconcile

INITIAL = str(uuid.UUID(int=0))
SCRIPTS = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text())


def delta_plan(current, previous):
    """Find changed primary keys, retaining the original synthetic usage timestamp on no-ops."""
    if any(
        current[k] != previous[k]
        for k in ("source", "workspace_id", "schema_sha256", "target")
    ):
        raise SyncError("基线与当前源、目标、工作区或数据库结构不匹配")
    delta = {**current, "tables": {}}
    stats = {"added": 0, "updated": 0, "removed": 0}
    for table in set(previous["tables"]) | set(current["tables"]):
        old = previous["tables"].get(table)
        new = current["tables"].get(table)
        if old and digest(old["rows"]) != old["sha256"]:
            raise SyncError("已应用的基线校验失败")
        if not new:
            stats["removed"] += len(old["rows"])
            continue
        keys = new["keys"]
        by_key = {tuple(r[k] for k in keys): r for r in old["rows"]} if old else {}
        changes = []
        for row in new["rows"]:
            before = by_key.pop(tuple(row[k] for k in keys), None)
            if (
                before is not None
                and table == "task_usage"
                and all(
                    row.get(k) == before.get(k)
                    for k in set(row) | set(before)
                    if k != "updated_at"
                )
            ):
                row["updated_at"] = before["updated_at"]
            if row != before:
                stats["added" if before is None else "updated"] += 1
                changes.append(row)
        stats["removed"] += len(by_key)
        new["sha256"] = digest(new["rows"])
        if changes:
            delta["tables"][table] = {**new, "rows": changes, "sha256": digest(changes)}
    return delta, stats


class Target:
    def __init__(self, cfg):
        self.project = cfg["project"]
        self.pg = self.project + "-postgres-1"
        self.backend = self.project + "-backend-1"
        self.pg_args = [
            "docker",
            "exec",
            "-i",
            self.pg,
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            cfg["db_user"],
            "-d",
            cfg["db_name"],
        ]
        self.cfg = cfg

    def check_identity(self):
        for container in (self.pg, self.backend):
            label = subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "com.docker.compose.project"}}',
                    container,
                ],
                text=True,
            ).strip()
            if label != self.project:
                raise SyncError("容器不属于指定部署")

    def sql(self, sql, log=None):
        result = subprocess.run(
            self.pg_args, input=sql, text=True, capture_output=True, check=False
        )
        if result.returncode:
            if log:
                log.write(result.stderr)
                log.flush()
            if "division by zero" in result.stderr:
                raise SyncError(
                    "写入保护已触发。请核对目标端修改、运行时连接和已应用基线"
                )
            raise SyncError("目标数据库操作失败，详情见执行器私有日志")
        return result.stdout

    def schema(self, reference):
        query = """SELECT json_agg(to_jsonb(c) ORDER BY table_name,ordinal_position) FROM (
            SELECT table_name,column_name,data_type,udt_name,is_nullable,column_default,ordinal_position
            FROM information_schema.columns WHERE table_schema='public') c;"""
        live = json.loads(self.sql(query))
        # Older schema captures selected the same columns but omitted ordinal_position.
        keys = reference[0].keys()
        if [{k: r[k] for k in keys} for r in live] != reference:
            raise SyncError("目标数据库结构发生变化，需重新核验迁移基线")

    def baseline(self):
        ws = str(uuid.UUID(self.cfg["workspace_id"]))
        return str(
            uuid.UUID(
                self.sql(
                    f"SELECT run_id FROM migration_control.baseline WHERE workspace_id='{ws}';"
                ).strip()
            )
        )

    def initialize(self):
        ws = str(uuid.UUID(self.cfg["workspace_id"]))
        self.sql(f"""BEGIN; CREATE SCHEMA IF NOT EXISTS migration_control;
            CREATE TABLE IF NOT EXISTS migration_control.baseline(workspace_id uuid PRIMARY KEY, run_id uuid NOT NULL);
            ALTER TABLE migration_control.baseline ADD COLUMN IF NOT EXISTS rollup_pending boolean NOT NULL DEFAULT false;
            INSERT INTO migration_control.baseline(workspace_id,run_id) VALUES ('{ws}','{INITIAL}') ON CONFLICT DO NOTHING; COMMIT;""")

    def uploads(self):
        value = subprocess.check_output(
            [
                "docker",
                "volume",
                "inspect",
                self.project + "_backend_uploads",
                "--format",
                "{{.Mountpoint}}",
            ],
            text=True,
        ).strip()
        return Path(value).resolve(strict=True)

    def backup(self, run, root):
        path = root / "backups"
        path.mkdir(exist_ok=True, mode=0o700)
        temp = path / (run + ".partial")
        with temp.open("wb") as f:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.pg,
                    "pg_dump",
                    "-Fc",
                    "-U",
                    self.cfg["db_user"],
                    "-d",
                    self.cfg["db_name"],
                ],
                stdout=f,
                check=True,
            )
        temp.replace(path / (run + ".dump"))

    def apply(self, run, old_run, delta, previous, root, log):
        ws = str(uuid.UUID(delta["workspace_id"]))
        run, old_run = str(uuid.UUID(run)), str(uuid.UUID(old_run))
        sql = sql_for_plan(delta, previous).removesuffix("COMMIT;\n")
        # Keep connected daemons and live tasks out of rehearsal imports. The
        # baseline pointer commits with the data so retries survive a worker crash.
        guard = f"""SET LOCAL lock_timeout='5s';
            LOCK TABLE workspace,agent_runtime,agent_task_queue IN SHARE ROW EXCLUSIVE MODE;
            SELECT 1 / (EXISTS (SELECT 1 FROM migration_control.baseline
                WHERE workspace_id='{ws}' AND run_id='{old_run}' FOR UPDATE))::int;
            SELECT 1 / (NOT EXISTS (SELECT 1 FROM agent_runtime WHERE daemon_id IS NOT NULL OR status <> 'offline'))::int;
            SELECT 1 / (NOT EXISTS (SELECT 1 FROM agent_task_queue WHERE status NOT IN ('completed','failed','cancelled')))::int;
        """
        sql = sql.replace("BEGIN;\n", "BEGIN;\n" + guard, 1)
        needs_rollup = bool(
            {"task_usage", "agent_task_queue", "issue", "agent"}
            & delta["tables"].keys()
        )
        rollup_flag = "true" if needs_rollup else "rollup_pending"
        sql += f"UPDATE migration_control.baseline SET rollup_pending={rollup_flag},run_id='{run}' WHERE workspace_id='{ws}';\n"
        self.sql(sql + "ROLLBACK;", log)
        self.backup(run, root)
        result = self.sql(sql + "COMMIT;", log)
        return sum(
            json.loads(line).get("written", 0)
            for line in result.splitlines()
            if line.startswith("{")
        )

    def finish_rollup(self, log):
        ws = str(uuid.UUID(self.cfg["workspace_id"]))
        if (
            self.sql(
                f"SELECT rollup_pending FROM migration_control.baseline WHERE workspace_id='{ws}';"
            ).strip()
            != "t"
        ):
            return
        result = subprocess.run(
            [
                "docker",
                "exec",
                self.backend,
                "/app/backfill_task_usage_hourly",
                "--months-back",
                "0",
            ],
            stdout=log,
            stderr=log,
            check=False,
        )
        if result.returncode:
            raise SyncError("增量已写入，用量汇总待重试")
        self.sql(
            f"UPDATE migration_control.baseline SET rollup_pending=false WHERE workspace_id='{ws}';"
        )


def capture(api_config, workspace, folder, log, message_cache=None):
    cmd = [
        sys.executable,
        str(SCRIPTS / "cloud_sync.py"),
        "export",
        "--config",
        str(api_config),
        "--workspace",
        workspace,
        "--output",
        str(folder),
    ]
    if message_cache:
        cmd.extend(["--message-cache", str(message_cache)])
    result = subprocess.run(cmd, stdout=log, stderr=log, check=False)
    if result.returncode not in (0, 2):
        raise SyncError("API 快照读取失败，查看执行器日志后可重新运行")
    return folder / "snapshot.json"


def copy_assets(plan, manifest, cache, uploads):
    for row in plan["tables"].get("attachment", {}).get("rows", []):
        meta = manifest[row["id"]]
        key = row["url"].removeprefix("/uploads/")
        dest = (uploads / key).resolve()
        if not dest.is_relative_to(uploads) or dest == uploads:
            raise SyncError("无效的存储路径")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if dest.exists():
            with dest.open("rb") as existing:
                existing_hash = hashlib.file_digest(existing, "sha256").hexdigest()
            if existing_hash != meta["sha256"]:
                raise SyncError("目标文件与源文件哈希不同")
            continue
        temp = dest.with_suffix(dest.suffix + ".pending")
        shutil.copyfile(cache / meta["file"], temp)
        temp.replace(dest)
        save(
            Path(str(dest) + ".meta.json"),
            {k: meta[k] for k in ("filename", "content_type")},
        )


def readback_assets(api, snapshot, directory):
    directory.mkdir(mode=0o700)

    def fetch(row):
        ident = str(uuid.UUID(row["id"]))
        dest = directory / ident
        req = Request(
            api.base + f"/api/attachments/{ident}/download",
            headers={
                "Authorization": "Bearer " + api.token,
                "X-Workspace-ID": api.workspace,
            },
        )
        sha = hashlib.sha256()
        with api.opener.open(req, timeout=60) as stream, dest.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                out.write(chunk)
                sha.update(chunk)
        size = dest.stat().st_size
        if size != row["size_bytes"]:
            raise SyncError("目标附件长度与 API 元数据不一致")
        return ident, {
            "file": ident,
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size": size,
            "sha256": sha.hexdigest(),
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = dict(pool.map(fetch, read(snapshot)["objects"]["attachments"]))
    save(directory / "assets.json", result)
    return result


def run_job(cfg, jobs, job, tmp, log):
    run = job["id"]
    root, ws = Path(cfg["private_dir"]), cfg["workspace_id"]
    target = Target(cfg)
    target.check_identity()
    target.schema(read(root / "schema.json"))
    old_run = target.baseline()
    src, dst = read(root / "source.json"), read(root / "target.json")
    cloud = API(src["server_url"], src["token"], ws)
    local = API(dst["server_url"], dst["token"], ws)
    snapshot = capture(
        root / "source.json",
        ws,
        tmp / "source",
        log,
        root / "messages" if job["mode"] == "sync" else None,
    )
    jobs.phase(run, "assets")
    assets = download_assets(cloud, snapshot, root / "assets")
    jobs.phase(run, "plan")
    expected = make_plan(
        snapshot,
        root / "schema.json",
        cfg["origin"],
        True,
        root / "assets/assets.json",
        avatar_secret_path=root / "avatar.key",
    )
    previous = read(root / "baselines" / (old_run + ".json"))
    if (
        expected["tables"]["user"]["rows"][0]["id"]
        != previous["tables"]["user"]["rows"][0]["id"]
    ):
        raise SyncError("官方凭据对应的用户与迁移基线不同")
    delta, stats = delta_plan(expected, previous)
    written, failure = 0, None
    unsupported = [
        b for b in expected["blockers"] if not b.startswith("nonterminal task:")
    ]
    if job["mode"] == "sync" and not unsupported and delta["tables"]:
        jobs.phase(run, "apply")
        copy_assets(expected, assets, root / "assets", target.uploads())
        save(root / "baselines" / (run + ".json"), expected)
        try:
            written = target.apply(run, old_run, delta, previous, root, log)
        except SyncError as exc:
            failure = str(exc)
    if job["mode"] == "sync" and failure is None:
        try:
            target.finish_rollup(log)
        except SyncError as exc:
            failure = str(exc)
    expected_file = root / "baselines" / (run + ".json")
    if not expected_file.is_file():
        expected_file = tmp / "expected.json"
        save(expected_file, expected)
    del previous, delta, expected
    gc.collect()
    jobs.phase(run, "readback")
    actual_snapshot = capture(root / "target.json", ws, tmp / "target", log)
    actual_assets = readback_assets(local, actual_snapshot, tmp / "readback-assets")
    actual = make_plan(
        actual_snapshot,
        root / "schema.json",
        cfg["origin"],
        True,
        tmp / "readback-assets/assets.json",
        readback=True,
    )
    jobs.phase(run, "verify")
    expected = read(expected_file)
    report = reconcile(expected, actual)
    mismatches = sorted(
        key
        for key in assets.keys() | actual_assets.keys()
        if assets.get(key, {}).get("sha256") != actual_assets.get(key, {}).get("sha256")
    )
    report.update(
        source_read=read(tmp / "source" / "read-stats.json"),
        delta=stats,
        written=written,
        files={
            "matched": sum(k not in mismatches for k in assets),
            "expected": len(assets),
            "actual": len(actual_assets),
            "mismatches": mismatches,
        },
    )
    clean = not (
        failure
        or report["difference_count"]
        or report["snapshot_blockers"]
        or mismatches
    )
    jobs.finish(
        run,
        "failed" if failure else ("success" if clean else "partial"),
        report,
        failure,
    )
    # Retain the active baseline and recent recovery points; snapshots and raw
    # target readbacks are temporary. The queue retains the reconciliation history.
    keep = target.baseline() + ".json"
    for directory, glob in (
        (root / "baselines", "*.json"),
        (root / "backups", "*.dump"),
    ):
        files = sorted(
            directory.glob(glob), key=lambda f: f.stat().st_mtime, reverse=True
        )
        for file in files[3:]:
            if file.name not in (keep, INITIAL + ".json"):
                file.unlink()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("config", type=Path)
    p.add_argument("--initialize", action="store_true")
    args = p.parse_args()
    cfg = read(args.config)
    root = Path(cfg["private_dir"])
    os.umask(0o077)
    jobs = Jobs(cfg["jobs"])
    if args.initialize:
        if not (root / "baselines" / (INITIAL + ".json")).is_file():
            raise SyncError("缺少初始已应用基线")
        target = Target(cfg)
        target.check_identity()
        target.schema(read(root / "schema.json"))
        target.initialize()
        if (root / "initial-report.json").is_file():
            seed = read(root / "initial-report.json")
            if seed["workspace_id"] != cfg["workspace_id"]:
                raise SyncError("初始报告与工作区不匹配")
            with jobs.connection() as db:
                db.execute(
                    """INSERT OR IGNORE INTO jobs(id,mode,actor,created,finished,state,phase,report)
                    VALUES(?,'baseline','operator',?,?,?,'done',?)""",
                    (
                        INITIAL,
                        seed["created"],
                        seed["created"],
                        seed["state"],
                        json.dumps(seed["report"]),
                    ),
                )
        return
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs.interrupted()
        while True:
            job = jobs.claim()
            if not job:
                time.sleep(2)
                continue
            log_path = root / "worker.log"
            if log_path.exists() and log_path.stat().st_size > 8 * 1024 * 1024:
                log_path.replace(root / "worker.previous.log")
            with log_path.open("a", buffering=1) as log, redirect_stdout(log):
                print(now(), job["id"], job["mode"])
                try:
                    with tempfile.TemporaryDirectory(dir=root) as directory:
                        run_job(cfg, jobs, job, Path(directory), log)
                except (
                    SyncError,
                    OSError,
                    ValueError,
                    KeyError,
                    subprocess.SubprocessError,
                ) as exc:
                    import traceback

                    traceback.print_exc(file=log)
                    jobs.finish(
                        job["id"],
                        "failed",
                        error=str(exc)
                        if isinstance(exc, SyncError)
                        else "执行失败，请查看执行器私有日志后重试",
                    )


if __name__ == "__main__":
    main()
