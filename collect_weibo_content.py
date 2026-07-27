import json
import time
import random
from playwright.sync_api import sync_playwright

# 板块2 内容运营：聚焦当前腾讯视频热播剧/综的负面舆情。
# 结构 = 当前热播剧综名 × 负面后缀。剧综名与负面维度统一从 keywords_config 导入，
# 每轮只需维护 keywords_config.py 的 DRAMAS / VARIETY，四平台自动同步，避免遗漏。
from keywords_config import DRAMAS, VARIETY, CONTENT_NEG, POSITIVE_TITLE_PAIRS, NEUTRAL_TITLE_PAIRS
_TITLES = DRAMAS + VARIETY   # 全部热播/待播剧综（电视剧+短剧+综艺）
_NEG = CONTENT_NEG           # 内容运营负面维度后缀
# 生成"剧名 后缀"组合（择要，避免组合爆炸拖慢采集）
_PAIRS = []
for _t in _TITLES:
    for _n in _NEG[:4]:  # 每部剧取前4个高频负面维度（剧目已扩容，控制总量）
        _PAIRS.append(f"{_t} {_n}")
# 补充平台级内容运营通用负面词
_GENERIC = [
    "腾讯视频 剧 塌房", "腾讯视频 剧 数据造假", "腾讯视频 剧 抠图",
    "腾讯视频 综艺 尴尬", "腾讯视频 短剧 骂", "腾讯视频 断更",
    "腾讯视频 独播 差评", "腾讯视频 剧 下架",
]
# 内容运营维度的"正面 / 中性"补充：取头部热播剧综 × 正/中后缀（控制总量，避免组合爆炸）。
# 与负面组合同源，确保剧综口碑/进度类内容也能被采到，实现情感三分类。
_TOP_TITLES = DRAMAS[:6] + VARIETY[:4]
_POS_PAIRS = [f"{_t} {_s}" for _t in _TOP_TITLES for _s in ("好看", "推荐", "值得看", "封神", "好评")]
_NEU_PAIRS = [f"{_t} {_s}" for _t in _TOP_TITLES for _s in ("更新", "定档", "开播", "预告", "阵容")]
KEYWORDS = _PAIRS + _GENERIC + _POS_PAIRS + _NEU_PAIRS

def main():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )
        page = context.new_page()
        try:
            page.goto("https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D%E8%85%BE%E8%AE%AF%E8%A7%86%E9%A2%91", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except Exception as e:
            print("init session error:", e)

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

    with open("weibo_content_raw_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print("done")

if __name__ == "__main__":
    main()
