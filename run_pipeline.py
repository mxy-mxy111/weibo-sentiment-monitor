# -*- coding: utf-8 -*-
"""
四平台舆情一键流水线：采集 -> 解析(24h窗口=本次运行当前时间) -> 构建 datasource.json -> 推送 GitHub。

设计原则（对应用户要求）：
  1. 每次运行都以"当前时间"为24小时窗口终点，保证"只要抓取就看到最新一次询问时间的数据"。
  2. 单个平台失败不阻断整体：失败会记录日志并继续，build_datasource.py 会如实标注该平台无数据（绝不编造）。
  3. 构建完成后自动 commit + push，线上动态看板（读取 GitHub raw 的 datasource.json）随即显示最新数据。

用法：
    python3 run_pipeline.py            # 正常：以当前时间为窗口终点
    python3 run_pipeline.py --no-push  # 只构建不推送
    PIPELINE_NOW=2026-07-24T10:00:00 python3 run_pipeline.py   # 复现指定时间窗

说明：微博、黑猫为纯脚本采集，可全自动完成；豆瓣正文核实与小红书候选核实
(douban_parsed_in_window.json / xhs_candidates.json / xhs_verified_results.json)
依赖登录态与人工/智能体核实，若对应文件已存在则会被 build_datasource.py 一并纳入，
否则该平台按"本轮无数据"如实处理。
"""
import os
import sys
import subprocess
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python3"

# 统一本次运行时间，写入环境变量，供所有解析脚本共享同一窗口终点
NOW = os.environ.get("PIPELINE_NOW") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
os.environ["PIPELINE_NOW"] = NOW

# 采集/解析步骤（顺序执行，单步失败不阻断）
STEPS = [
    ("采集-微博通用", "collect_weibo.py"),
    ("采集-微博剧综", "collect_weibo_content.py"),
    ("解析-微博通用", "parse_weibo.py"),
    ("解析-微博剧综", "parse_weibo_content.py"),
    # 豆瓣：绕过反爬采集小组讨论帖 -> 24h窗口过滤（豆瓣改版后适配，访客态可抓真实数据）
    ("采集-豆瓣", "collect_douban.py"),
    ("解析-豆瓣", "parse_douban.py"),
    ("采集-黑猫", "collect_heimao.py"),
    ("解析-黑猫", "parse_heimao.py"),
    # 小红书：采集 -> 负面候选初筛 -> 登录态核实正文/时间 -> 24h窗口过滤
    # 依赖 xhs_login_data 登录态；若登录态失效，核实取不到正文，窗口过滤后按"本轮无数据"处理，不编造
    ("采集-小红书", "collect_xhs.py"),
    ("筛选-小红书候选", "filter_xhs_candidates.py"),
    ("核实-小红书正文", "verify_xhs.py"),
    ("解析-小红书窗口", "parse_xhs.py"),
]


def run(step_name, script):
    path = os.path.join(BASE, script)
    if not os.path.exists(path):
        print(f"[跳过] {step_name}: {script} 不存在")
        return False
    print(f"\n===== [{step_name}] {script} =====")
    try:
        r = subprocess.run([PY, path], cwd=BASE, env=os.environ.copy(),
                            capture_output=True, text=True, timeout=1200)
        sys.stdout.write(r.stdout[-4000:])
        if r.stderr:
            sys.stderr.write(r.stderr[-2000:])
        ok = r.returncode == 0
        print(f"[{'OK' if ok else '失败'}] {step_name} (returncode={r.returncode})")
        return ok
    except Exception as e:
        print(f"[异常] {step_name}: {e}")
        return False


def build():
    print("\n===== [构建] build_datasource.py =====")
    r = subprocess.run([PY, os.path.join(BASE, "build_datasource.py")],
                       cwd=BASE, env=os.environ.copy(), capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode == 0


def git_push():
    print("\n===== [推送] git commit & push =====")
    msg = f"update datasource.json: 四平台整合版 {NOW}"
    cmds = [
        ["git", "add", "datasource.json", "腾讯视频负面舆情监测看板.html"],
        ["git", "-c", "user.name=weibo-monitor",
         "-c", "user.email=weibo-monitor@users.noreply.github.com",
         "commit", "-m", msg],
        ["git", "push", "origin", "main"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=BASE, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.stderr:
            sys.stderr.write(r.stderr)
        # commit 在"无改动"时会非0退出，属正常，继续尝试 push
        if c[0] == "git" and "commit" in c and r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
            print("[提示] datasource.json 无变化，跳过提交")
            return True
        if r.returncode != 0 and "commit" not in c:
            print(f"[失败] {' '.join(c)} (returncode={r.returncode})")
            return False
    return True


def main():
    no_push = "--no-push" in sys.argv
    print(f"流水线开始，窗口终点 PIPELINE_NOW = {NOW}，窗口起点 = "
          f"{(datetime.fromisoformat(NOW) - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S')}")

    results = {name: run(name, script) for name, script in STEPS}

    if not build():
        print("\n[终止] build_datasource.py 失败")
        sys.exit(1)

    print("\n----- 各步骤结果 -----")
    for name, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {name}")

    if no_push:
        print("\n[--no-push] 已构建 datasource.json，未推送。")
        return

    if git_push():
        print("\n[完成] datasource.json 已推送到 GitHub main。线上看板刷新即显示最新数据。")
    else:
        print("\n[警告] 推送失败，请检查 git 凭据/网络。")
        sys.exit(2)


if __name__ == "__main__":
    main()
