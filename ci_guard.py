"""备援跑批的前置判断：近 N 小时内是否已有成功的定时跑批。

退出码：0 = 已有成功记录（应跳过补跑）；1 = 没有（应执行补跑）。

设计要点：
- 不用 `gh run list`：该步骤在 checkout 之前执行，工作目录没有 .git，
  gh 会报 "not a git repository" 并被兜底成 0，导致每天重复补跑。
- 不用 jq：解析失败容易被兜底成 0，同样会重复补跑。改用 Python 显式判断。
- 只统计 event == schedule 的成功记录，手动触发（补刷）不算作当天主任务已完成。
"""
import datetime as dt
import json
import os
import sys
import urllib.request

WORKFLOW_FILE = "wxread.yml"


def fetch_runs(repo, token):
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{WORKFLOW_FILE}/runs?status=success&per_page=40")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "wxread-guard",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def count_recent(runs, hours):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).timestamp()
    n = 0
    for run in runs:
        if run.get("event") != "schedule":
            continue
        try:
            ts = dt.datetime.strptime(
                run["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc).timestamp()
        except (KeyError, ValueError):
            continue
        if ts > since:
            n += 1
    return n


def main():
    hours = int(os.getenv("GUARD_HOURS", "14"))
    local_json = os.getenv("GUARD_JSON_FILE")

    if local_json:
        with open(local_json, encoding="utf-8") as f:
            data = json.load(f)
    else:
        repo = os.environ["REPO"]
        token = os.environ["GH_TOKEN"]
        try:
            data = fetch_runs(repo, token)
        except Exception as exc:  # noqa: BLE001
            # 查询失败时按「今天还没跑」处理：宁可多跑一次，也不要漏掉当天时长
            print(f"查询 GitHub 记录失败：{exc}")
            print("保守处理：视为当天未跑，执行补跑")
            return 1

    n = count_recent(data.get("workflow_runs", []), hours)
    print(f"近 {hours} 小时成功的定时跑批次数：{n}")
    if n > 0:
        print("当天主任务已成功，跳过补跑")
        return 0
    print("当天没有成功的定时跑批，执行补跑")
    return 1


if __name__ == "__main__":
    sys.exit(main())
