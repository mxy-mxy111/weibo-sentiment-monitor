# -*- coding: utf-8 -*-
"""采集黑猫投诉平台 - 腾讯视频公司主页真实投诉数据（滚动加载翻页）"""
from playwright.sync_api import sync_playwright
import json, urllib.parse as up

COUID = "7850526767"  # 腾讯视频 - 当前活跃黑猫投诉商家账号
BASE = f"https://tousu.sina.com.cn/company/view/?couid={COUID}"

all_complaints = {}

def on_resp(resp):
    if "received_complaints" in resp.url:
        try:
            j = resp.json()
            q = dict(up.parse_qsl(up.urlparse(resp.url).query))
            if q.get("type") == "1":
                data = j["result"]["data"]
                for c in data.get("complaints", []):
                    sn = c["main"]["sn"]
                    all_complaints[sn] = c
        except Exception:
            pass

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
    page = ctx.new_page()
    page.on("response", on_resp)
    page.goto(BASE, timeout=25000, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    # 通过滚动触发懒加载翻页，抓取足够多页覆盖24小时窗口(及更早作对比)
    for i in range(20):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)

    browser.close()

print("total collected:", len(all_complaints))
with open("heimao_raw_results.json", "w", encoding="utf-8") as f:
    json.dump(list(all_complaints.values()), f, ensure_ascii=False, indent=2)

items = sorted(all_complaints.values(), key=lambda c: c["main"]["timestamp"], reverse=True)
for c in items[:40]:
    m = c["main"]
    print(m["timestamp"], "|", m["title"][:45], "|", m["url"])
