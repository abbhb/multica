"""Shared write policy and read-only schema coverage for reconciliation."""

from cloud_sync import SyncError


READ_ONLY_NOTICE = "当前为只读对账，本地新增和修改会保留；不会从官方导入、覆盖或删除业务数据。"


def sync_enabled(config):
    return config.get("allow_sync") is True


class BlockedRun(SyncError):
    def __init__(self, message, schema=None):
        super().__init__(message)
        self.report = {"written": 0, "comparison_complete": False}
        if schema is not None:
            self.report["schema"] = schema


def schema_difference(reference, current):
    if not reference:
        raise SyncError("缺少迁移结构基线，暂不能核对；请先核验基线文件")
    before = {(r["table_name"], r["column_name"]): r for r in reference}
    after = {(r["table_name"], r["column_name"]): r for r in current}
    changed = []
    for key in sorted(before.keys() & after.keys()):
        attributes = sorted(k for k, value in before[key].items() if after[key].get(k) != value)
        if attributes:
            changed.append({"column": ".".join(key), "attributes": attributes})
    added = [".".join(key) for key in sorted(after.keys() - before.keys())]
    removed = [".".join(key) for key in sorted(before.keys() - after.keys())]
    return {
        "matches": not (added or removed or changed),
        "projection_compatible": not (removed or changed),
        "added": added, "removed": removed, "changed": changed,
    }
