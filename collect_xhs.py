# -*- coding: utf-8 -*-
"""使用已登录的小红书会话，采集"腾讯视频"相关笔记（真实数据，保留完整href含xsec_token）"""
from playwright.sync_api import sync_playwright
import json, time

# 关键词统一从 keywords_config 导入，覆盖 服务(技术功能) + 会员收费 + 维权监管 +
# 全部热播剧综内容运营 四大板块，与微博/豆瓣保持一致，避免遗漏最新剧集。
# 扩展：补充"正面 / 中性"搜索词，使采集覆盖情感三分类（pos / neu / neg）。
from keywords_config import SERVICE_NEG, MEMBER_NEG, REG_NEG, DRAMAS, VARIETY, POSITIVE_SEARCH, NEUTRAL_SEARCH
KEYWORDS = (
    ["腾讯视频"]
    + ["腾讯视频 " + w for w in SERVICE_NEG]
    + ["腾讯视频 " + w for w in MEMBER_NEG]
    + ["腾讯视频 " + w for w in REG_NEG]
    # 每部热播/待播剧综 × 内容运营负面词（差评/弃剧），确保最新剧集全覆盖
    + [f"{t} 差评" for t in (DRAMAS + VARIETY)]
    + [f"{t} 弃剧" for t in DRAMAS]
    # 每部热播/待播剧综 × 内容运营正面词（好看/推荐），捕获剧综口碑类正面笔记
    + [f"{t} 好看" for t in (DRAMAS + VARIETY)]
    + [f"{t} 推荐" for t in (DRAMAS + VARIETY)]
    # 每部热播剧综 × 中性词（更新/定档），捕获剧综进度/资讯类中性笔记
    + [f"{t} 更新" for t in DRAMAS]
    + [f"{t} 定档" for t in VARIETY]
    # 平台级正面 / 中性搜索词
    + POSITIVE_SEARCH + NEUTRAL_SEARCH
)

results = {}

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        "xhs_login_data",
        headless=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()

    for kw in KEYWORDS:
        try:
            url = f"https://www.xiaohongshu.com/search_result?keyword={kw}"
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            body_text = page.inner_text("body")
            if "登录后查看搜索结果" in body_text:
                print(kw, "-> 需要登录，跳过")
                continue
            # 账号风控检测：300011 安全限制页会重定向到 website-login/error，
            # 此时所有搜索都为空，继续跑没有意义，立即报错终止避免静默空跑
            if "website-login/error" in page.url or "账号存在异常" in body_text or "安全限制" in body_text:
                print(kw, "-> ACCOUNT_BLOCKED 账号被风控(300011)，终止采集")
                raise SystemExit(2)
            for _ in range(3):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1000)

            anchors = page.query_selector_all("a[href*='/search_result/']")
            print(kw, "-> anchors:", len(anchors))

            for a in anchors:
                try:
                    href = a.get_attribute("href")
                    if not href:
                        continue
                    note_id = href.split("/search_result/")[-1].split("?")[0]
                    title = a.inner_text().strip()
                    full_href = "https://www.xiaohongshu.com" + href if href.startswith("/") else href
                    if note_id not in results or (not results[note_id]["title"] and title):
                        results[note_id] = {
                            "note_id": note_id,
                            "href": full_href,
                            "title": title,
                            "kw": kw,
                        }
                except Exception:
                    continue
        except Exception as e:
            print(kw, "ERROR", e)
        time.sleep(1.5)

    ctx.close()

print("total unique notes:", len(results))
with open("xhs_raw_results.json", "w", encoding="utf-8") as f:
    json.dump(list(results.values()), f, ensure_ascii=False, indent=2)
