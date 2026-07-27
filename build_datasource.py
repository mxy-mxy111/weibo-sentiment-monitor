# -*- coding: utf-8 -*-
"""
构建对外公网 JSON 数据源：整合本轮真实采集的原始帖(raw_posts)、
归并分级事件(sentiment_events)与过往风险历史(risk_history)。
供外部(with 后端)定时拉取。所有内容基于真实采集文件，不编造。

采集规范（每轮必须遵守）：
  每次刷新必须"一并采集"四个平台并整合进本数据源：
    1. 微博   weibo_parsed_in_window.json / weibo_content_parsed_in_window.json
    2. 豆瓣   douban_parsed_in_window.json
    3. 小红书 xhs_verified_results.json
    4. 黑猫投诉 heimao_parsed_in_window.json
  四平台数据统一归入 raw_posts，并做跨平台去重（重复内容删除）。
  若某平台本轮确实无数据，如实标注，不编造。
"""
import json, os, re, html

BASE = os.path.dirname(os.path.abspath(__file__))

def load(fn):
    p = os.path.join(BASE, fn)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

from datetime import datetime, timedelta

# 剧综名清单（用于豆瓣相关性判定：剧名帖正文常不含"腾讯"二字，需按剧综名识别）
try:
    from keywords_config import TITLE_WORDS as _TITLE_WORDS
except Exception:
    _TITLE_WORDS = []

# ============================================================
# 1. 载入四平台原始采集数据
# ============================================================
weibo_general = load("weibo_parsed_in_window.json")
weibo_content = load("weibo_content_parsed_in_window.json")
douban_raw    = load("douban_parsed_in_window.json")
# 小红书：优先用本轮 24h 窗口过滤结果(parse_xhs.py 产出)；若不存在则回退到全量核实文件
xhs_raw       = load("xhs_in_window.json") or load("xhs_verified_results.json")
heimao_raw    = load("heimao_parsed_in_window.json")
risk_history  = load("risk_history.json")

# ---- 时间窗口（终点=本次运行当前时间）----
# 微博/黑猫等实时型平台用 24h；小红书为慢发酵平台，与 parse_xhs 一致放宽到 7 天(168h)，
# 可用 XHS_WINDOW_HOURS 覆盖。豆瓣沿用其解析脚本 douban_parsed_in_window.json 的窗口，不在此二次过滤。
_NOW = datetime.fromisoformat(os.environ["PIPELINE_NOW"]) if os.environ.get("PIPELINE_NOW") else datetime.now()
_PERIOD_START = _NOW - timedelta(hours=24)
_XHS_WINDOW_HOURS = int(os.environ.get("XHS_WINDOW_HOURS", "168"))
_XHS_PERIOD_START = _NOW - timedelta(hours=_XHS_WINDOW_HOURS)

def parse_xhs_date(text, now):
    """把小红书 date_text 解析为发布时间(尽量)。无法解析返回 None。
    支持: 刚刚 / N分钟前 / N小时前 / 昨天 / N天前 / MM-DD / YYYY-MM-DD，
    兼容前缀'编辑于'与尾部地域(如' 北京')。"""
    if not text:
        return None
    t = str(text).strip()
    t = t.replace("编辑于", "").strip()
    # 去掉尾部地域(第一个日期/相对时间 token 之后的中文地名)
    t = t.split(" ")[0].strip() if (" " in t and ("前" in t.split(" ")[0] or "-" in t.split(" ")[0] or "昨天" in t.split(" ")[0] or "刚刚" in t.split(" ")[0])) else t
    try:
        if "刚刚" in t:
            return now
        m = re.match(r"(\d+)\s*分钟前", t)
        if m:
            return now - timedelta(minutes=int(m.group(1)))
        m = re.match(r"(\d+)\s*小时前", t)
        if m:
            return now - timedelta(hours=int(m.group(1)))
        if t.startswith("昨天"):
            return now - timedelta(days=1)
        m = re.match(r"(\d+)\s*天前", t)
        if m:
            return now - timedelta(days=int(m.group(1)))
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{1,2})-(\d{1,2})", t)
        if m:
            return datetime(now.year, int(m.group(1)), int(m.group(2)))
    except Exception:
        return None
    return None

# ---- 微博：按 mid 去重 ----
by_mid = {}
for e in weibo_general + weibo_content:
    mid = str(e.get("mid") or "")
    if not mid:
        continue
    if mid not in by_mid:
        by_mid[mid] = e
weibo_posts = list(by_mid.values())

def permalink(e):
    mid = e.get("mid")
    return "https://m.weibo.cn/detail/{}".format(mid) if mid else None

# ============================================================
# 2. 噪音过滤规则
# ============================================================
NOISE_PAT = [
    r"收\s*(ipad|平板|vip|svip|腾讯)", r"走.?鱼", r"会员共享", r"任意端\d+r", r"到.*过期",
    r"活动来了", r"购买戳", r"连续包年", r"抓紧入", r"太给力", r"年卡.{0,6}158",
    r"来打分", r"守护.*打分", r"摇人", r"低赞一星", r"新号\d.?老号", r"聚宝",
    r"心有多宽", r"人走茶凉", r"打碎心中", r"多努力", r"抛弃这世界",  # 鸡汤+会员罗列刷屏
    r"茶业博览|会展服务|展会",
    r"剪映",  # 剪映/爱奇艺套娃，非腾讯视频主体
]
# 会员买卖/代开/拼车/云包场帮抢/运营商卡等推广类广告（小红书高发）
SALE_PAT = [
    r"免费领", r"附属卡", r"先登后付", r"独享", r"家庭卡", r"日\s*\d\s*周\s*\d",
    r"出\s*腾讯", r"出\s*svip", r"出腾讯视频vip", r"接.?云?包场", r"元宝里.*攒",
    r"福利中心", r"兑换商城", r"低价", r"秒到", r"秒发", r"拼车", r"来拼", r"拼\s*\d",
    r"车位", r"上车", r"组队", r"缺\s*\d\s*人", r"人齐", r"拉群",
    r"有人要", r"想看的剧尽管看", r"人工在线", r"云包场", r"帮抢", r"走过路过",
    r"\d+\s*/\s*年", r"\d+\s*r\b", r"批发", r"代开", r"接单", r"低至",
    r"联通.{0,6}卡", r"电信.{0,6}卡", r"移动.{0,6}卡", r"校园卡", r"月租",
    r"全国通用流量", r"话费", r"限时返场", r"权益版", r"预存", r"看\s*\d+\s*分钟",
    r"月\s*\d+\b", r"周\s*\d+\b", r"💺", r"套餐", r"返场", r"性价比",
    r"年卡", r"\d+\s*块", r"租借", r"一起来看", r"没人要", r"安利.{0,6}(svip|年卡|会员)",
]
OFFICIAL_NAME = ["官方微博", "流媒体网"]

# 投诉/维权/吐槽的强信号：出现这些说明是真实用户负面，即便正文捎带"云包场/包场/年卡"等软推广词，也应救回，不当广告
COMPLAINT_PAT = [
    r"投诉", r"维权", r"举报", r"退款", r"退钱", r"不给退", r"乱扣", r"乱收费",
    r"自动续费", r"扣款", r"扣费", r"垃圾", r"卡顿", r"卡的", r"卡成", r"卡的不是",
    r"难看", r"吃相", r"坑", r"霸王", r"看不了", r"离谱", r"不要脸", r"凭什么",
    r"恶心", r"气死", r"差评", r"太贵", r"故障", r"崩", r"闪退", r"加载不出",
    r"缓冲", r"刷新不出", r"客服", r"善待", r"割韭菜", r"套路", r"退费", r"维权教程",
]
# 硬交易特征：出现基本可确定是卖家在买卖/拉人，即便含投诉词也不予救回
HARD_SALE_PAT = [
    r"出\s*腾讯", r"出\s*svip", r"出腾讯视频vip", r"低价", r"秒到", r"秒发",
    r"批发", r"代开", r"接单", r"低至", r"免费领", r"附属卡", r"家庭卡",
    r"\d+\s*/\s*年", r"\d+\s*r\b", r"加\s*微", r"私\s*信", r"扣\s*1", r"有偿",
    r"车位", r"上车", r"缺\s*\d\s*人", r"人齐", r"拉群", r"拼车", r"来拼",
    r"人工在线", r"走过路过", r"福利中心", r"兑换商城", r"独享", r"先登后付",
    r"接.?云?包场", r"帮抢",
]

def is_noise_weibo(e):
    t = e.get("text") or ""
    name = e.get("screen_name") or ""
    if any(o in name for o in OFFICIAL_NAME):
        return True, "官方/媒体号"
    if t.count("会员") >= 2 and ("qq音乐" in t.lower() or "芒果tv会员" in t.lower() or "网易云" in t) and "投诉" not in t and "退" not in t:
        return True, "会员买卖/罗列刷屏广告"
    for p in NOISE_PAT:
        if re.search(p, t, re.I):
            return True, "促销/买卖/控评/无关噪音"
    return False, ""

def is_noise_generic(text, extra_sale=True):
    t = text or ""
    for p in NOISE_PAT:
        if re.search(p, t, re.I):
            return True, "促销/买卖/控评/无关噪音"
    if extra_sale:
        for p in SALE_PAT:
            if re.search(p, t, re.I):
                # 边界救回：正文含明确投诉/维权强信号，且不含硬交易特征(出腾讯/低价/秒到/加微/xxr/车位/拼车/帮抢等)，
                # 说明是"捎带云包场等软词但实为投诉"的真实负面，予以保留，不当广告过滤。
                if (any(re.search(cp, t, re.I) for cp in COMPLAINT_PAT)
                        and not any(re.search(hp, t, re.I) for hp in HARD_SALE_PAT)):
                    return False, ""
                return True, "会员买卖/代开/推广广告"
    return False, ""

# ============================================================
# 3. 四平台归一化为 raw_posts（统一结构）
# ============================================================
raw_posts = []

# ---- 微博 ----
for e in weibo_posts:
    noise, reason = is_noise_weibo(e)
    raw_posts.append({
        "id": str(e.get("mid") or ""),
        "platform": "weibo",
        "author": e.get("screen_name"),
        "followers": e.get("followers_count"),
        "verified": e.get("verified"),
        "published_at": e.get("dt"),
        "text": html.unescape(e.get("text") or ""),
        "reposts": e.get("reposts_count"),
        "comments": e.get("comments_count"),
        "likes": e.get("attitudes_count"),
        "keywords": e.get("keywords"),
        "permalink": permalink(e),
        "filtered_as_noise": noise,
        "noise_reason": reason or None,
    })

# ---- 豆瓣 ----
def douban_id(href):
    m = re.search(r"/topic/(\d+)", href or "")
    return "douban_" + m.group(1) if m else "douban_" + str(abs(hash(href)) % (10 ** 10))

for e in douban_raw:
    title = e.get("title") or ""
    snippet = e.get("content_snippet") or ""
    text = (title + " " + snippet).strip()
    mentions = e.get("mentions_tengxun")
    noise, reason = is_noise_generic(text, extra_sale=False)
    # 相关性：正文提及"腾讯"、被标注 mentions，或命中腾讯视频剧综名(如"桃花坞")均视为相关。
    # 剧名讨论帖正文常不出现"腾讯"二字，故必须按剧综名识别，避免腾讯综艺/剧集负面被误剔。
    hit_title = any(w and w in text for w in _TITLE_WORDS)
    if not noise and not mentions and "腾讯" not in text and not hit_title:
        noise, reason = True, "未提及腾讯视频/无关组帖"
    raw_posts.append({
        "id": douban_id(e.get("href")),
        "platform": "douban",
        "author": e.get("group"),
        "followers": None,
        "published_at": e.get("dt_parsed"),
        "text": text,
        "reposts": None,
        "comments": e.get("replies"),
        "likes": None,
        "keywords": e.get("kw"),
        "permalink": e.get("href"),
        "filtered_as_noise": noise,
        "noise_reason": reason or None,
    })

# ---- 小红书（应用统一 24h 窗口：仅纳入窗口内笔记，窗口外/无法解析时间的如实剔除）----
xhs_collected = 0      # 本轮已采集并核实成功(status=OK)的腾讯视频相关笔记数
xhs_in_window = 0      # 其中发布时间落在 24h 窗口内的数量
for e in xhs_raw:
    if e.get("status") and e.get("status") != "OK":
        continue
    xhs_collected += 1
    pub_dt = parse_xhs_date(e.get("date_text"), _NOW)
    # 小红书窗口与 parse_xhs 对齐(默认7天)：只有能解析且落在[xhs_period_start, now]内的才纳入
    if not (pub_dt and _XHS_PERIOD_START <= pub_dt <= _NOW):
        continue
    xhs_in_window += 1
    title = e.get("real_title") or e.get("title") or ""
    desc = e.get("desc") or ""
    text = (title + " " + desc).strip()
    noise, reason = is_noise_generic(text, extra_sale=True)
    # 主体相关性校验：正文须提及腾讯(视频)，否则判为竞品/无关噪音，避免优酷/百度网盘等被误计
    if not noise and ("腾讯" not in text and "鹅厂" not in text and "tx视频" not in text.lower()):
        noise, reason = True, "非腾讯视频主体(竞品/无关)"
    raw_posts.append({
        "id": str(e.get("note_id") or ""),
        "platform": "xiaohongshu",
        "author": None,
        "followers": None,
        "published_at": pub_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "date_text": e.get("date_text"),
        "text": text,
        "reposts": None,
        "comments": None,
        "likes": None,
        "keywords": e.get("kw"),
        "permalink": e.get("final_url") or e.get("href"),
        "filtered_as_noise": noise,
        "noise_reason": reason or None,
    })

# ---- 黑猫投诉（真实用户投诉，均视为真实负面，不做噪音过滤）----
for e in heimao_raw:
    m = e.get("main") or {}
    a = e.get("author") or {}
    title = m.get("title") or ""
    summary = m.get("summary") or ""
    text = (title + " " + summary).strip()
    kw = "|".join([x for x in [m.get("appeal"), m.get("issue")] if x])
    raw_posts.append({
        "id": "heimao_" + str(m.get("sn") or ""),
        "platform": "heimao",
        "author": a.get("title"),
        "followers": None,
        "published_at": m.get("created_dt_str") or m.get("timestamp"),
        "text": text,
        "reposts": None,
        "comments": m.get("comment_amount"),
        "likes": m.get("upvote_amount"),
        "keywords": kw or None,
        "permalink": m.get("url"),
        "filtered_as_noise": False,
        "noise_reason": None,
    })

# ============================================================
# 4. 跨平台去重（重复内容删除）
#    先按 id 去重，再按正文归一化指纹去重（保留信息更全者）
# ============================================================
def norm_text(t):
    t = (t or "").lower()
    t = re.sub(r"[\s\W_]+", "", t)
    return t[:60]

seen_id = set()
seen_fp = {}
deduped = []
dup_removed = 0
for p in raw_posts:
    pid = p.get("id") or ""
    if pid and pid in seen_id:
        dup_removed += 1
        continue
    fp = norm_text(p.get("text"))
    if fp and len(fp) >= 12:
        if fp in seen_fp:
            dup_removed += 1
            continue
        seen_fp[fp] = True
    if pid:
        seen_id.add(pid)
    deduped.append(p)
raw_posts = deduped

# 各平台计数
def plat_stats(platform):
    items = [r for r in raw_posts if r["platform"] == platform]
    real = [r for r in items if not r["filtered_as_noise"]]
    return len(items), len(real)

wb_total, wb_real = plat_stats("weibo")
db_total, db_real = plat_stats("douban")
xhs_total, xhs_real = plat_stats("xiaohongshu")
hm_total, hm_real = plat_stats("heimao")

# ============================================================
# 5. 归并分级事件（基于微博本轮真实帖，人工规则归并）
# ============================================================
def find(*subs):
    """按文本子串定位真实微博帖，返回永久链接与作者，用于事件引用。"""
    out = []
    for e in weibo_posts:
        t = e.get("text") or ""
        if all(s in t for s in subs):
            out.append({"author": e.get("screen_name"), "followers": e.get("followers_count"),
                        "permalink": permalink(e), "published_at": e.get("dt"),
                        "likes": e.get("attitudes_count"), "reposts": e.get("reposts_count")})
    return out

def find_heimao(*subs):
    """按文本子串定位黑猫投诉真实条目，用于会员收费类事件引用。"""
    out = []
    for e in heimao_raw:
        m = e.get("main") or {}
        t = (m.get("title") or "") + (m.get("summary") or "")
        if all(s in t for s in subs):
            out.append({"author": (e.get("author") or {}).get("title"),
                        "permalink": m.get("url"), "published_at": m.get("created_dt_str"),
                        "likes": None, "reposts": None})
    return out

sentiment_events = [
    {
        "id": "EV-20260723-01",
        "level": "P1",
        "section": "会员收费",
        "title": "《半熟恋人5》柳周CP剪辑争议叠加SVIP超点退款诉求持续发酵",
        "summary": "多名自称腾讯视频SVIP付费用户就《半熟恋人5》维权：指节目长期用周佑凌&柳柳CP话题引流吸引充值，正片却大量删减二人镜头、碎片化剪辑制造对立；伴随'坚决不买超点''退钱，买SVIP不是来看边角料'等付费不满与退款诉求，24小时内同类发帖十余条。",
        "post_count": len(find("半熟恋人")),
        "evidence": (find("半熟恋人", "投诉") + find("半熟恋人5柳周镜头少") + find("买svip") + find("退钱"))[:8],
    },
    {
        "id": "EV-20260723-02",
        "level": "P1",
        "section": "其他/监管竞品",
        "title": "《五十公里桃花坞第六季》'用已故周涛拍真人秀'实名举报言论",
        "summary": "用户以'实名举报'措辞发帖，称腾讯视频《五十公里桃花坞第六季》'用死人周涛拍真人秀节目、欺骗观众'，要求公开道歉；措辞激烈、带举报/道德争议属性，存在被放大传播风险(同一用户24h内重复发布3条)。",
        "post_count": len(find("桃花坞", "举报")),
        "evidence": find("桃花坞", "举报")[:3],
    },
    {
        "id": "EV-20260723-03",
        "level": "P2",
        "section": "内容运营",
        "title": "《十日终焉》选角争议(擦边博主进组加戏)延续",
        "summary": "书粉持续发帖反对'擦边博主'进组出演'余念安/白月光'，称影响青少年价值观、要求剧组出具官方声明或换人，态度延续此前组织化维权基调。",
        "post_count": len(find("十日终焉")) + len(find("擦边博主进组")),
        "evidence": (find("十日终焉") + find("擦边博主进组"))[:4],
    },
    {
        "id": "EV-20260723-04",
        "level": "P2",
        "section": "技术功能",
        "title": "广告体验与试看限制吐槽(《这一秒过火》等)",
        "summary": "用户吐槽同为S+剧《这一秒过火》广告过多、观感割裂('广告钢钢的')；另有用户激烈吐槽'看1分钟广告只给试看3分钟'，反映广告密度与试看策略的体验不满。",
        "post_count": len(find("广告")) ,
        "evidence": (find("这一秒过火", "广告") + find("试看3分钟"))[:4],
    },
    {
        "id": "EV-20260723-05",
        "level": "P0",
        "section": "其他/监管竞品",
        "title": "含自伤/抑郁倾向的极端归因言论(需即时人工研判)",
        "summary": "一名用户发帖称'被腾讯逼到走投无路、有严重抑郁、接下来想不开、绝对是腾讯逼的'，将极端情绪归因于账号被永久封停。虽粉丝量极低、传播面小，但含自伤倾向表述，按风险监测规则上报为P0，建议人工即时核实并做安抚/申诉引导，防止舆情与安全事件叠加。",
        "post_count": len(find("抑郁", "腾讯")),
        "evidence": find("抑郁", "腾讯")[:2],
    },
]

# ---- 黑猫投诉：会员收费系统性投诉事件（有数据才纳入）----
heimao_refund = find_heimao("退款") + find_heimao("扣费") + find_heimao("自动续费")
# 黑猫按 sn 内部去重
_hseen = set(); _he = []
for v in heimao_refund:
    k = v.get("permalink")
    if k and k not in _hseen:
        _hseen.add(k); _he.append(v)
if hm_real >= 5:
    sentiment_events.append({
        "id": "EV-20260723-06",
        "level": "P1",
        "section": "会员收费",
        "title": "黑猫投诉集中反映自动续费未告知/退款遭拒(会员收费系统性风险)",
        "summary": "黑猫投诉平台'腾讯视频小助手'受理账号窗口内新增 {} 条真实用户投诉，高度集中在'自动续费未明确告知''充值后版权缺失要求退款''退款遭拒/客服踢皮球'等，反映会员收费与退款处理已具备系统性特征，建议结合工单核实处理时效。".format(hm_real),
        "post_count": hm_real,
        "evidence": _he[:6],
    })

# 清理空事件(evidence 为空说明本轮无对应真实帖，直接剔除，避免编造)
sentiment_events = [ev for ev in sentiment_events if ev["evidence"]]

# ============================================================
# 6. KPI
# ============================================================
def _build_conclusion(p0, p1, p2, events, hm_real):
    """对 P0/P1/P2 逐级做简要概括：事件数 + 板块 + 凝练问题。
    结论聚焦、简洁，去掉举例/剧名，与下方分点(含完整描述与证据)形成区分。"""
    import re as _re

    def _short(t):
        # 去掉举例括号与剧名，仅保留问题主干，使结论比下方分点更凝练
        t = _re.sub(r"[（(][^）)]*[)）]", "", t or "")
        t = _re.sub(r"《[^》]*》", "", t)
        return t.strip("、，。/ ") or "相关负面反馈"

    by = {"P0": [], "P1": [], "P2": []}
    for e in events:
        lv = (e.get("level") or "").upper()
        if lv in by:
            by[lv].append(e)

    cnt_map = {"P0": p0, "P1": p1, "P2": p2}
    seg = []
    for lv in ("P0", "P1", "P2"):
        head = "{} {}起".format(lv, cnt_map[lv])
        es = by[lv]
        if es:
            items = []
            for e in es[:2]:
                sec = e.get("section") or ""
                c = e.get("post_count")
                s = (sec + "—" if sec else "") + _short(e.get("title"))
                if c:
                    s += "（{}条）".format(c)
                items.append(s)
            head += "：" + "；".join(items)
        seg.append(head)
    return "本轮 " + "｜".join(seg) + "。"

p0 = sum(1 for e in sentiment_events if e["level"] == "P0")
p1 = sum(1 for e in sentiment_events if e["level"] == "P1")
p2 = sum(1 for e in sentiment_events if e["level"] == "P2")
real_negative = sum(1 for r in raw_posts if not r["filtered_as_noise"])

# ---- 过滤前总抓取量（各平台采集/解析出的原始条数：时间窗口过滤、噪音过滤、跨平台去重之前）----
def _count(fn):
    d = load(fn)
    return len(d) if isinstance(d, list) else 0

_collected = {
    "weibo": _count("weibo_parsed_all.json") + _count("weibo_content_parsed_all.json"),
    "douban": _count("douban_raw_results.json"),
    "xiaohongshu": _count("xhs_raw_results.json"),
    "heimao": _count("heimao_raw_results.json"),
}
total_collected = sum(_collected.values())

now = _NOW
period_start = _PERIOD_START

# 本轮已登录采集并核实成功(status=OK)的腾讯视频相关笔记总数(取全量核实文件，避免仅统计窗口过滤后的子集)
_xhs_verified_all = load("xhs_verified_results.json")
xhs_verified_ok = sum(1 for x in _xhs_verified_all if (not x.get("status")) or x.get("status") == "OK") or xhs_collected
xhs_out_window = max(xhs_verified_ok - xhs_in_window, 0)

# 小红书状态：区分"登录失效未采集" / "采集核实成功但窗口内无新增" / "窗口内有真实负面"
if xhs_verified_ok == 0:
    xhs_status = "本轮未纳入(登录态失效/风控)"
else:
    xhs_status = "本轮真实负面 {} 条".format(xhs_real)

datasource = {
    "meta": {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "period_start": period_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "period_end": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": [
            "微博 m.weibo.cn 公开搜索(通用关键词 + 热播剧综专项)",
            "豆瓣小组 公开讨论搜索",
            "小红书 公开笔记搜索(登录态核实)",
            "黑猫投诉 tousu.sina.com.cn 公开投诉",
        ],
        "xiaohongshu_status": xhs_status,
        "platform_stats": {
            "weibo": {"total": wb_total, "real_negative": wb_real},
            "douban": {"total": db_total, "real_negative": db_real},
            "xiaohongshu": {"total": xhs_total, "real_negative": xhs_real},
            "heimao": {"total": hm_total, "real_negative": hm_real},
        },
        "schema_version": "1.1",
        "note": "本数据源每轮一并采集微博+豆瓣+小红书+黑猫投诉四平台并跨平台去重(重复内容删除)。raw_posts 为去重后的原始采集帖(含 platform 平台标记与 filtered_as_noise 噪音标记)；sentiment_events 为按看板过滤规则归并分级后的真实负面/风险事件；risk_history 为逐轮风险回顾全量历史。所有内容基于真实采集，不含编造数据。",
    },
    "kpi": {
        "total_collected": total_collected,
        "collected_by_platform": _collected,
        "total_raw": len(raw_posts),
        "real_negative": real_negative,
        "p0": p0, "p1": p1, "p2": p2,
        "conclusion": _build_conclusion(p0, p1, p2, sentiment_events, hm_real),
    },
    "sentiment_events": sentiment_events,
    "raw_posts": raw_posts,
    "risk_history": risk_history,
}

out = os.path.join(BASE, "datasource.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(datasource, f, ensure_ascii=False, indent=2)

print("raw_posts:", len(raw_posts), "| real_negative:", real_negative,
      "| 去重删除:", dup_removed)
print("平台明细  微博:{}/{}  豆瓣:{}/{}  小红书:{}/{}  黑猫:{}/{} (采集/真实负面)".format(
    wb_total, wb_real, db_total, db_real, xhs_total, xhs_real, hm_total, hm_real))
print("events P0/P1/P2:", p0, p1, p2, "| risk_history:", len(risk_history))
print("written:", out)
