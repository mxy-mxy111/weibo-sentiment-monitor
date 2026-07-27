# -*- coding: utf-8 -*-
"""采集豆瓣腾讯视频相关讨论帖（真实数据）

重要说明（2026-07 豆瓣改版后适配）：
- 豆瓣已改版：`group/search?cat=1019` 现在只返回“小组”而不再返回“讨论帖”，
  且访客态请求会被 sec.douban.com 安全系统拦截（重定向到人机验证页）。
- 本脚本策略：
  1) 绕过反爬：命中 sec.douban.com 时点击“点我继续浏览”放行；
  2) 通过关键词搜索发现腾讯视频/热播剧相关小组，并内置核心小组种子；
  3) 进入各小组抓取讨论帖列表（标题/时间/链接）；
  4) 用负面关键词过滤标题、排除会员交易广告帖；
  5) 进入候选帖详情页，取真实发帖时间与正文首段。
- 抓不到即如实为空，绝不编造。
"""
from playwright.sync_api import sync_playwright
import json, os, time, re
import urllib.parse as up

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# 登录态目录（由 douban_login.py 扫码生成）。存在则以登录态采集，显著降低反爬限流。
LOGIN_DATA_DIR = "douban_login_data"

# 关键词/剧集统一从 keywords_config 导入，与其他三平台同源，避免遗漏最新剧集。
# 扩展：导入正面 / 中性情感词，使豆瓣也覆盖情感三分类采集（pos / neu / neg）。
from keywords_config import (
    SERVICE_NEG, MEMBER_NEG, REG_NEG, CONTENT_NEG,
    AD_WORDS as _AD_WORDS_CFG, DRAMAS, VARIETY,
    POSITIVE_WORDS, NEUTRAL_WORDS,
)

# 用于“发现相关小组”的搜索词：腾讯视频通用 + 部分热播剧综（发现剧综粉丝组里的负面讨论）。
# 适度控制数量以降低触发 sec.douban.com 反爬的频率（剧综全覆盖主要靠下方 NEG_WORDS 标题过滤）。
SEARCH_TERMS = ["腾讯视频", "腾讯视频 会员"] + VARIETY[:4] + DRAMAS[:6]

# 内置核心小组种子：腾讯视频官方组 + 实测含腾讯视频相关讨论的活跃小组，
# 确保即使“搜索发现”被反爬限流，也有稳定来源可抓真实数据。
SEED_GROUPS = [
    "https://www.douban.com/group/612746/",   # 腾讯视频
    "https://www.douban.com/group/642171/",   # 实测含腾讯视频相关负面讨论
    "https://www.douban.com/group/751840/",
    "https://www.douban.com/group/751531/",
    "https://www.douban.com/group/762693/",
    "https://www.douban.com/group/754805/",
    "https://www.douban.com/group/740196/",
]

# 负面/风险关键词（标题命中才纳入候选）：统一四大板块负面词 + 豆瓣高发补充词，去重保序。
NEG_WORDS = list(dict.fromkeys(
    SERVICE_NEG + MEMBER_NEG + REG_NEG + CONTENT_NEG
    + ["崩", "掉线", "转圈", "bug", "BUG", "vip", "翻车", "骂", "垃圾",
       "坑", "吐槽", "骗", "难看", "没到账", "权益", "加载", "下架"]
))

# 会员交易/广告帖排除词（豆瓣腾讯视频组大量此类交易帖，非舆情）—— 统一自 keywords_config
AD_WORDS = _AD_WORDS_CFG


def unblock(pg, target_url=None):
    """命中 sec.douban.com 反爬拦截页时点击放行；失败则重载重试并逐步退避。

    返回 True 表示最终未被拦截，False 表示仍被拦（调用方可选择跳过）。
    """
    for attempt in range(4):
        if "sec.douban.com" not in pg.url:
            return True
        el = pg.query_selector("text=点我继续浏览")
        if not el:
            # 兜底：找页面里唯一可点的继续链接/按钮
            el = pg.query_selector("a.btn, button, a")
        if el:
            try:
                el.click()
                pg.wait_for_timeout(2500 + attempt * 1500)  # 逐步退避
            except Exception:
                pg.wait_for_timeout(2000)
        # 若点击后仍被拦，且给了目标 URL，则重载
        if "sec.douban.com" in pg.url and target_url:
            try:
                pg.wait_for_timeout(2000 + attempt * 1000)
                pg.goto(target_url, timeout=25000, wait_until="domcontentloaded")
                pg.wait_for_timeout(1500)
            except Exception:
                pass
    return "sec.douban.com" not in pg.url


def is_negative(title):
    t = title.lower()
    return any(w.lower() in t for w in NEG_WORDS)


def is_positive(title):
    t = title.lower()
    return any(w.lower() in t for w in POSITIVE_WORDS)


def is_neutral(title):
    t = title.lower()
    return any(w.lower() in t for w in NEUTRAL_WORDS)


def is_ad(title):
    return any(w in title for w in AD_WORDS)


def extract_post_time(pg):
    """从详情页正文提取真实发帖时间字符串（返回 ISO 或空）"""
    try:
        txt = pg.inner_text("body")
    except Exception:
        return ""
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?", txt)
    if m:
        y, mo, d, h, mi = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        s = m.group(6) or "00"
        return f"{y}-{mo}-{d}T{h}:{mi}:{s}"
    return ""


def extract_content(pg):
    for sel in [".topic-content", "#link-report", ".topic-richtext",
                ".rich-content", ".article .topic-doc"]:
        el = pg.query_selector(sel)
        if el:
            return re.sub(r"\s+", " ", el.inner_text().strip())[:300]
    return ""


def discover_groups(pg):
    """通过关键词搜索发现相关小组链接"""
    groups = set(SEED_GROUPS)
    for kw in SEARCH_TERMS:
        try:
            surl = "https://www.douban.com/group/search?cat=1019&q=" + up.quote(kw)
            pg.goto(surl, timeout=25000, wait_until="domcontentloaded")
            pg.wait_for_timeout(1800)
            unblock(pg, surl)
            for a in pg.query_selector_all(".result a, .content a"):
                href = a.get_attribute("href") or ""
                m = re.search(r"douban\.com/group/(\d+)/?", href)
                # 搜索结果的小组链接可能是 link2 跳转，需解开
                if not m:
                    mm = re.search(r"url=([^&]+)", href)
                    if mm:
                        real = up.unquote(mm.group(1))
                        m = re.search(r"douban\.com/group/(\d+)/?", real)
                if m:
                    groups.add(f"https://www.douban.com/group/{m.group(1)}/")
        except Exception as e:
            print("discover ERR", kw, e)
        time.sleep(2.5)
    return list(groups)


results = {}

with sync_playwright() as p:
    use_login = os.path.isdir(LOGIN_DATA_DIR) and os.listdir(LOGIN_DATA_DIR)
    if use_login:
        # 登录态采集：复用扫码保存的持久化上下文（大幅降低 sec.douban.com 反爬）
        ctx = p.chromium.launch_persistent_context(
            LOGIN_DATA_DIR,
            headless=True,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        browser = None
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("[登录态] 使用 douban_login_data 登录态采集")
    else:
        # 访客态回退：无登录态时仍尝试抓取（易被限流）
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        print("[访客态] 未找到 douban_login_data，建议先运行 douban_login.py 扫码登录")

    groups = discover_groups(page)
    print("发现相关小组:", len(groups))
    for g in groups:
        print("  ", g)

    # 遍历小组抓讨论帖列表 -> 负面候选
    candidates = []
    for g in groups[:12]:  # 控制规模
        try:
            page.goto(g, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(1800)
            if not unblock(page, g):
                print(f"[{g}] 反爬拦截，跳过")
                time.sleep(2.5)
                continue
            rows = page.query_selector_all("table.olt tr")
            hit = 0
            for r in rows[1:]:
                a = r.query_selector("td.title a") or r.query_selector("a")
                if not a:
                    continue
                title = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if not title or "/group/topic/" not in href:
                    continue
                tm = r.query_selector("td.time")
                list_time = tm.inner_text().strip() if tm else ""
                if (is_negative(title) or is_positive(title) or is_neutral(title)) and not is_ad(title):
                    topic_id = re.search(r"/topic/(\d+)", href)
                    tid = topic_id.group(1) if topic_id else href
                    if tid not in results:
                        candidates.append({
                            "tid": tid, "title": title,
                            "url": href.split("?")[0], "list_time": list_time,
                            "group": g,
                        })
                        results[tid] = None
                        hit += 1
            print(f"[{g}] 讨论帖 {max(0,len(rows)-1)} 条 -> 负面候选 {hit}")
        except Exception as e:
            print("group ERR", g, e)
        time.sleep(2.5)

    # 进入候选详情页取真实发帖时间与正文
    final = []
    for c in candidates[:40]:  # 控制核实规模
        try:
            page.goto(c["url"], timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            unblock(page, c["url"])
            t_iso = extract_post_time(page)
            content = extract_content(page)
            final.append({
                "title": c["title"],
                "content": content,
                "url": c["url"],
                "time_iso": t_iso,
                "list_time": c["list_time"],
                "group": c["group"],
                "source": "douban",
            })
            print(f"核实 {c['title'][:24]} | time={t_iso or c['list_time']}")
        except Exception as e:
            print("detail ERR", c["url"], e)
        time.sleep(1.2)

    try:
        ctx.close()
    except Exception:
        pass
    if browser is not None:
        browser.close()

print("total douban candidates:", len(final))
with open("douban_raw_results.json", "w", encoding="utf-8") as f:
    json.dump(final, f, ensure_ascii=False, indent=2)
