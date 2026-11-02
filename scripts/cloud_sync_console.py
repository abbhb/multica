#!/usr/bin/env python3
"""Browser-only operator console. The web process has no Cloud token or Docker access."""

import argparse
import hashlib
import hmac
import json
import re
import secrets
import uuid
from functools import partial
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, build_opener

from cloud_sync import NoRedirect, SyncError
from cloud_sync_jobs import Jobs

STATES = {
    "queued": "排队中",
    "running": "运行中",
    "success": "一致",
    "partial": "待处理",
    "failed": "失败",
}
MODES = {"baseline": "首次迁移", "compare": "仅对账", "sync": "增量同步"}
PHASES = {
    "queued": "等待执行",
    "source": "读取官方 API",
    "assets": "校验源文件",
    "plan": "比对上次基线",
    "apply": "写入增量",
    "readback": "读回自部署数据",
    "verify": "核对记录和文件",
    "done": "已完成",
}
TABLES = dict(
    line.split(" ", 1)
    for line in """user 用户
workspace 工作区
member 工作区成员
agent 智能体
agent_runtime 运行时
agent_invocation_target 调用范围
agent_skill 智能体 skill
agent_task_queue 智能体运行
task_message 运行消息
task_usage 运行用量
issue 任务
comment 评论
activity_log 任务动态
issue_status 任务状态
issue_workflow 工作流
issue_label 标签
issue_to_label 任务标签
issue_subscriber 任务订阅
issue_view 任务视图
issue_view_preference 视图偏好
project 项目
project_resource 项目资源
skill skills
skill_file skill 文件
attachment 附件与头像
inbox_item 收件箱
notification_preference 通知偏好
chat_session 会话
chat_message 聊天消息""".splitlines()
)


def h(value):
    return escape(str(value), quote=True)


def authorize(cookie, backend, workspace):
    """Delegate session validation to Multica, then match membership by user AND workspace."""
    jar = SimpleCookie()
    try:
        jar.load(cookie)
    except CookieError:
        return 401, None
    if "multica_auth" not in jar:
        return 401, None
    auth = jar["multica_auth"].value
    opener = build_opener(NoRedirect())

    def read(path):
        req = Request(backend + path, headers={"Cookie": "multica_auth=" + auth})
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    try:
        me = read("/api/me")
        members = read(f"/api/workspaces/{workspace}/members")
        allowed = any(
            m["workspace_id"] == workspace
            and m["user_id"] == me["id"]
            and m["role"] in ("owner", "admin")
            for m in members
        )
        return (200, (me["id"], auth)) if allowed else (403, None)
    except HTTPError as exc:
        exc.close()
        return (exc.code if exc.code in (401, 403) else 503), None
    except (OSError, URLError, ValueError, KeyError, TypeError, SyncError):
        return 503, None


def date_tag(value):
    return f'<time datetime="{h(value)}">{h(value)}</time>' if value else "—"


def render_page(cfg, jobs, run_id, token):
    history = jobs.history()
    selected = jobs.get(run_id or history[0]["id"]) if history else None
    report = (selected or {}).get("report") or {}
    tables = report.get("tables", {})
    active = next((j for j in history if j["state"] in ("queued", "running")), None)
    slug = cfg["slug"]
    path = f"/{slug}/reconciliation"
    state = selected["state"] if selected else "empty"
    metrics = [
        ("核对类型", len(tables)),
        ("官方记录", sum(t["expected"] for t in tables.values())),
        ("自部署记录", sum(t["actual"] for t in tables.values())),
        ("记录差异", report.get("difference_count", "—")),
    ]
    metrics_html = "".join(
        f"<div><dt>{name}</dt><dd>{value:,}</dd></div>"
        if isinstance(value, int) and tables
        else f"<div><dt>{name}</dt><dd>—</dd></div>"
        for name, value in metrics
    )
    rows = []
    for key, v in sorted(
        tables.items(),
        key=lambda item: (
            -sum(len(item[1][k]) for k in ("missing", "extra", "changed")),
            TABLES.get(item[0], item[0]),
        ),
    ):
        differences = []
        for kind, title in (
            ("missing", "目标缺少"),
            ("extra", "目标多出"),
            ("changed", "字段不同"),
        ):
            for item in v[kind][:40]:
                text = (
                    item
                    if isinstance(item, str)
                    else item["key"] + " · " + ", ".join(item["fields"])
                )
                differences.append(f"<li>{title} <code>{h(text)}</code></li>")
        detail = (
            f"<details><summary>{sum(len(v[k]) for k in ('missing', 'extra', 'changed'))} 项差异</summary><ul>{''.join(differences)}</ul></details>"
            if differences
            else '<span class="ok">一致</span>'
        )
        rows.append(
            f'<tr><th scope="row">{h(TABLES.get(key, key))}<small>{h(key)}</small></th><td>{v["expected"]:,}</td><td>{v["actual"]:,}</td><td>{detail}</td></tr>'
        )
    alerts = []
    if selected and selected["error"]:
        alerts.append(h(selected["error"]))
    for message in report.get("snapshot_blockers", []):
        alerts.append(h(message))
    files = report.get("files")
    if files:
        for ident in files["mismatches"]:
            alerts.append("文件差异 " + h(ident))
    history_html = "".join(
        f'<li><a href="?run={h(j["id"])}"><span>{MODES[j["mode"]]}</span>{date_tag(j["created"])}<b>{STATES[j["state"]]}</b></a></li>'
        for j in history
    )
    active_html = (
        f'<p role="status">当前进度 · {PHASES[active["phase"]]}</p>' if active else ""
    )
    delta = report.get("delta")
    delta_html = (
        f"<p>源端新增 {delta['added']} · 更新 {delta['updated']} · 移除 {delta['removed']} · 本批写入 {report['written']}</p>"
        if delta
        else ""
    )
    binary_html = (
        f"<p>文件校验 {files['matched']} / {files['expected']} · SHA-256</p>"
        if files
        else ""
    )
    download = (
        f'<a href="{path}/report?run={h(selected["id"])}" download="reconciliation.json">下载完整明细</a>'
        if report
        else ""
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据对账 · Multica</title><link rel="stylesheet" href="{path}/style.css"><script defer src="{path}/page.js"></script></head>
<body data-active="{int(bool(active and selected and active["id"] == selected["id"]))}"><main data-reconciliation><header><a href="/{slug}/issues" target="_top">← 工作区</a><div class="eyebrow">MULTICA / SELF-HOST</div><h1>数据对账</h1>
<p>官方服务 → 自部署工作区</p></header>
<section class="actions"><form action="{path}/runs" method="post"><input type="hidden" name="csrf" value="{h(token)}">
<button class="primary" name="mode" value="sync" {"disabled" if active else ""}>运行增量同步</button>
<button name="mode" value="compare" {"disabled" if active else ""}>仅对账</button></form>{active_html}</section>
<section class="card"><div class="heading"><h2>{MODES.get((selected or {}).get("mode"), "最近一次对账")}</h2><b class="badge {h(state)}">{STATES.get(state, "尚无记录")}</b></div>
<p>{date_tag((selected or {}).get("finished") or (selected or {}).get("created"))}</p><dl>{metrics_html}</dl>{delta_html}{binary_html}{download}
{('<ul class="alerts">' + "".join("<li>" + a + "</li>" for a in alerts) + "</ul>") if alerts else ""}</section>
<section><h2>分类明细</h2><div class="grid"><table><thead><tr><th>数据类型</th><th>官方</th><th>自部署</th><th>结果</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>
<section><h2>运行历史</h2><ul class="history">{history_html}</ul></section></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, config, jobs, signing_key, assets, **kwargs):
        self.cfg, self.jobs, self.signing_key, self.assets = (
            config,
            jobs,
            signing_key,
            assets,
        )
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        pass  # Never log cookies, form fields or signed upstream URLs.

    def reply(self, status, body=b"", mime="text/plain; charset=utf-8", location=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, post=False):
        base = f"/{self.cfg['slug']}/reconciliation"
        url = urlsplit(self.path)
        if url.path not in (
            base,
            base + "/runs",
            base + "/style.css",
            base + "/page.js",
            base + "/report",
        ):
            return self.reply(404)
        code, actor = authorize(
            self.headers.get("Cookie", ""),
            self.cfg["backend"],
            self.cfg["workspace_id"],
        )
        if code != 200:
            if code == 401 and not post and url.path == base:
                return self.reply(303, location="/login?next=" + quote(base, safe=""))
            return self.reply(
                code,
                {
                    401: "请先登录",
                    403: "需要工作区所有者或管理员权限",
                    503: "暂时无法验证登录，请刷新重试",
                }[code],
            )
        token = hmac.new(
            self.signing_key, actor[1].encode(), hashlib.sha256
        ).hexdigest()
        if post:
            if url.path != base + "/runs":
                return self.reply(405)
            if self.headers.get("Origin") != self.cfg["origin"]:
                return self.reply(403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < length <= 1024
                    or self.headers.get_content_type()
                    != "application/x-www-form-urlencoded"
                ):
                    return self.reply(400)
                form = parse_qs(
                    self.rfile.read(length).decode("ascii"),
                    strict_parsing=True,
                    max_num_fields=2,
                )
                if not hmac.compare_digest(form["csrf"][0], token):
                    return self.reply(403)
                run = self.jobs.enqueue(form["mode"][0], actor[0])
            except (ValueError, KeyError):
                return self.reply(400)
            return self.reply(303, location=base + "?run=" + run)
        if url.path == base + "/report":
            item = self.jobs.get(parse_qs(url.query).get("run", [""])[0])
            if not item or not item["report"]:
                return self.reply(404)
            return self.reply(
                200, json.dumps(item["report"]), "application/json; charset=utf-8"
            )
        if url.path == base:
            run = parse_qs(url.query).get("run", [None])[0]
            if run and (len(run) > 40 or not self.jobs.get(run)):
                return self.reply(404)
            return self.reply(
                200,
                render_page(self.cfg, self.jobs, run, token),
                "text/html; charset=utf-8",
            )
        for filename, mime in (
            ("style.css", "text/css"),
            ("page.js", "application/javascript"),
        ):
            if url.path == base + "/" + filename:
                return self.reply(200, (self.assets / filename).read_bytes(), mime)
        self.reply(405)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(post=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("config", type=Path)
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    uuid.UUID(config["workspace_id"])
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", config["slug"]):
        raise ValueError("invalid workspace slug")
    handler = partial(
        Handler,
        config=config,
        jobs=Jobs(config["jobs"]),
        signing_key=secrets.token_bytes(32),
        assets=Path(config["static_dir"]),
    )
    ThreadingHTTPServer(("127.0.0.1", config["port"]), handler).serve_forever()


if __name__ == "__main__":
    main()
