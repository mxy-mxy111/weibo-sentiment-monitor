import json
import time
import random
from playwright.sync_api import sync_playwright

# 关键词库（"腾讯视频" + 任一词）。统一从 keywords_config 导入四大板块负面词，
# 避免与其他平台脚本各自维护导致遗漏。（板块2 内容运营剧综词见 collect_weibo_content.py）
# 扩展：补充"正面 / 中性"搜索词，使采集覆盖情感三分类（pos / neu / neg），
# 供 build_datasource 三分类判定与"情感声量趋势"使用。
from keywords_config import SERVICE_NEG, MEMBER_NEG, REG_NEG, POSITIVE_SEARCH, NEUTRAL_SEARCH
KEYWORDS = (["腾讯视频 " + w for w in (SERVICE_NEG + MEMBER_NEG + REG_NEG)]
            + POSITIVE_SEARCH + NEUTRAL_SEARCH)

def main():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )
        page = context.new_page()
        # 建立访客会话
        try:
            page.goto("https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D%E8%85%BE%E8%AE%AF%E8%A7%86%E9%A2%91", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except Exception as e:
            print("init session error:", e)

        cookies = context.cookies()
        print("cookies count:", len(cookies))
        for c in cookies:
            print(c['name'])

        for kw in KEYWORDS:
            api_url = f"https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D61%26q%3D{kw}%26t%3D0&page_type=searchall"
            try:
                resp = page.evaluate(f"""
                    async () => {{
                        const r = await fetch({json.dumps(api_url)}, {{credentials: 'include'}});
                        const t = await r.text();
                        return {{status: r.status, text: t}};
                    }}
                """)
                status = resp.get("status")
                text = resp.get("text", "")
                print(kw, "status=", status, "len=", len(text))
                results[kw] = {"status": status, "text": text}
            except Exception as e:
                print(kw, "ERROR", e)
                results[kw] = {"status": None, "text": "", "error": str(e)}
            time.sleep(random.uniform(1.0, 2.0))

        browser.close()

    with open("weibo_raw_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print("done")

if __name__ == "__main__":
    main()
