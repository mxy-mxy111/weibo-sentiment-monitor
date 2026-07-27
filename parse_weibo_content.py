import json
import os
import re
from datetime import datetime, timedelta

with open("weibo_content_raw_results.json", "r", encoding="utf-8") as f:
    raw = json.load(f)

# 抓取时间窗口以"本次运行的当前时间"为终点，覆盖过去24小时。
# 可用环境变量 PIPELINE_NOW=YYYY-MM-DDTHH:MM:SS 覆盖（用于复现历史某轮）。
now = datetime.fromisoformat(os.environ["PIPELINE_NOW"]) if os.environ.get("PIPELINE_NOW") else datetime.now()
cutoff = now - timedelta(hours=24)

def parse_weibo_time(t):
    try:
        return datetime.strptime(t, "%a %b %d %H:%M:%S %z %Y").replace(tzinfo=None)
    except Exception:
        pass
    if "刚刚" in t:
        return now
    m = re.match(r"(\d+)分钟前", t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.match(r"(\d+)小时前", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.match(r"今天\s*(\d+):(\d+)", t)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
    m = re.match(r"(\d+)-(\d+)$", t)
    if m:
        return datetime(now.year, int(m.group(1)), int(m.group(2)))
    m = re.match(r"(\d{4})-(\d+)-(\d+)", t)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None

all_posts = {}

for kw, data in raw.items():
    text = data.get("text", "")
    try:
        j = json.loads(text)
    except Exception:
        continue
    cards = j.get("data", {}).get("cards", [])
    for card in cards:
        mblog = card.get("mblog")
        if not mblog:
            for cg in card.get("card_group", []) or []:
                mb = cg.get("mblog")
                if mb:
                    mblog = mb
                    break
        if not mblog:
            continue
        mid = mblog.get("mid") or mblog.get("id")
        if not mid:
            continue
        created_at = mblog.get("created_at", "")
        dt = parse_weibo_time(created_at)
        raw_text = mblog.get("text", "")
        clean_text = re.sub(r"<[^>]+>", "", raw_text)
        user = mblog.get("user", {}) or {}
        entry = all_posts.get(mid)
        if entry is None:
            entry = {
                "mid": mid,
                "bid": mblog.get("bid"),
                "created_at": created_at,
                "dt": dt.isoformat() if dt else None,
                "text": clean_text,
                "user_id": user.get("id"),
                "screen_name": user.get("screen_name"),
                "followers_count": user.get("followers_count"),
                "verified": user.get("verified"),
                "reposts_count": mblog.get("reposts_count"),
                "comments_count": mblog.get("comments_count"),
                "attitudes_count": mblog.get("attitudes_count"),
                "keywords": set(),
            }
            all_posts[mid] = entry
        entry["keywords"].add(kw)

in_window = []
out_window = []
for mid, e in all_posts.items():
    e["keywords"] = sorted(e["keywords"])
    if e["dt"]:
        dt = datetime.fromisoformat(e["dt"])
        if dt >= cutoff:
            in_window.append(e)
        else:
            out_window.append(e)
    else:
        out_window.append(e)

print(f"Total unique posts: {len(all_posts)}")
print(f"In 24h window: {len(in_window)}")

in_window.sort(key=lambda x: x["dt"] or "", reverse=True)

with open("weibo_content_parsed_in_window.json", "w", encoding="utf-8") as f:
    json.dump(in_window, f, ensure_ascii=False, indent=2)

# 额外输出全量解析（去重后、未做时间窗口过滤），用于"过滤前总抓取量"统计
all_list = sorted(all_posts.values(), key=lambda x: x["dt"] or "", reverse=True)
with open("weibo_content_parsed_all.json", "w", encoding="utf-8") as f:
    json.dump(all_list, f, ensure_ascii=False, indent=2)
print(f"Total all posts saved: {len(all_list)}")

print("saved. printing all in-window posts:")
for e in in_window:
    print(e["dt"], "|", e["screen_name"], "|", e["text"][:80].replace("\n"," "))
