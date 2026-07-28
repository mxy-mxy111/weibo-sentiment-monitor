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

# 情感三分类词库（供 classify_sentiment 对每条原始帖做 pos / neu / neg 判定）
try:
    from keywords_config import (
        STRONG_NEG_SENTIMENT_WORDS as _STRONG_NEG,
        SOFT_NEG_SENTIMENT_WORDS as _SOFT_NEG,
        DUBBING_NEG_PHRASES as _DUBBING_NEG,
        POSITIVE_WORDS as _POS_W,
        NEUTRAL_WORDS as _NEU_W,
    )
except Exception:
    _STRONG_NEG, _SOFT_NEG, _DUBBING_NEG, _POS_W, _NEU_W = [], [], [], [], []

# ---- 情感三分类判定（pos / neu / neg）----
# 规则（重构，解决"夸剧/中性被误判为 neg"问题）：
#   1) 命中【强负面词】(投诉/故障/欺诈/收费/封禁等) -> 直接 neg（优先级最高，
#      即便同时出现夸赞也保留为负面，如"好看但退款太难"仍是投诉）。
#   2) 仅命中【弱负面词】(弃剧/离谱/尴尬等轻吐槽) 且【无正面词】 -> neg；
#      若同时出现正面词（如"演活了女主+弃剧""封神+离谱"），正面语境压过弱负面 -> pos/neu。
#   3) 仅命中正面词 -> pos；皆无 -> neu。
#   4) 命中【配音/原声争议短语】(拒配音/要求原声/配音不符等) -> 直接 neg（强信号，
#      优先级等同强负面，压过帖中"原声好评/夸赞"等正面词，避免真实投诉被误判为正面）。
#   黑猫投诉正文必含强负面词，自然归 neg。
def classify_sentiment(text):
    t = text or ""
    strong = sum(1 for w in _STRONG_NEG if w in t)
    soft = sum(1 for w in _SOFT_NEG if w in t)
    dub = sum(1 for w in _DUBBING_NEG if w in t)
    pos = sum(1 for w in _POS_W if w in t)
    if strong or dub:
        return "neg"
    if pos and not soft:
        return "pos"
    if pos and soft:
        # 正面语境压过弱负面：赞多于/等于轻吐槽 -> pos，否则归中性
        return "pos" if pos >= soft else "neu"
    if soft:
        return "neg"
    return "neu"

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
# 推广/控评类（腾讯视频相关，非真实用户情感）→ 归为「中性」，保留在监控量中，不计入负面风险
PROMO_PAT = [
    r"收\s*(ipad|平板|vip|svip|腾讯)", r"走.?鱼", r"会员共享", r"任意端\d+r", r"到.*过期",
    r"活动来了", r"购买戳", r"连续包年", r"抓紧入", r"太给力", r"年卡.{0,6}158",
    r"来打分", r"守护.*打分", r"摇人", r"低赞一星", r"新号\d.?老号", r"聚宝",
    r"心有多宽", r"人走茶凉", r"打碎心中", r"多努力", r"抛弃这世界",  # 鸡汤+会员罗列刷屏(控评)
]
# 竞品/无关内容（非腾讯视频主体，如剪映/爱奇艺套娃、会展等）→ 仍按硬噪音剔除
IRRELEVANT_PAT = [
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
    """返回 (类别, 原因)。类别: 'hard'=竞品/无关/其他产品(剔除); 'soft'=腾讯视频相关推广/控评/官方(归中性,保留监控量); None=非噪音。"""
    t = e.get("text") or ""
    name = e.get("screen_name") or ""
    if any(o in name for o in OFFICIAL_NAME):
        return "soft", "官方/媒体号(计中性)"
    if t.count("会员") >= 2 and ("qq音乐" in t.lower() or "芒果tv会员" in t.lower() or "网易云" in t) and "投诉" not in t and "退" not in t:
        return "hard", "会员买卖/罗列刷屏广告(竞品)"
    for p in IRRELEVANT_PAT:
        if re.search(p, t, re.I):
            return "hard", "竞品/无关内容"
    for p in PROMO_PAT:
        if re.search(p, t, re.I):
            return "soft", "控评/腾讯视频推广(计中性)"
    return None, ""

def is_noise_generic(text, extra_sale=True):
    """同 is_noise_weibo，用于豆瓣/小红书（extra_sale 仅小红书）。"""
    t = text or ""
    for p in IRRELEVANT_PAT:
        if re.search(p, t, re.I):
            return "hard", "竞品/无关内容"
    for p in PROMO_PAT:
        if re.search(p, t, re.I):
            return "soft", "控评/腾讯视频推广(计中性)"
    if extra_sale:
        for p in SALE_PAT:
            if re.search(p, t, re.I):
                # 边界救回：正文含明确投诉/维权强信号，且不含硬交易特征，说明是"捎带推广软词但实为投诉"的真实负面，保留(可能判neg)。
                if (any(re.search(cp, t, re.I) for cp in COMPLAINT_PAT)
                        and not any(re.search(hp, t, re.I) for hp in HARD_SALE_PAT)):
                    return None, ""
                # 其余 腾讯视频会员买卖/代开/云包场/拼车等推广 → 计中性(按用户要求：广告不减少监控量，归入中性)
                # 注：小红书此分支已前置要求正文含"腾讯/鹅厂"，故匹配的均为腾讯视频相关推广。
                return "soft", "会员买卖/代开/推广广告(计中性)"
    return None, ""

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
        "filtered_as_noise": noise == "hard",
        "noise_category": noise or "",
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
        noise, reason = "hard", "未提及腾讯视频/无关组帖"
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
        "filtered_as_noise": noise == "hard",
        "noise_category": noise or "",
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
        "filtered_as_noise": noise == "hard",
        "noise_category": noise or "",
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

# ---- 为每条原始帖打"情感标签"：pos / neu / neg / unclassified（未分类） ----
#   规则（用户要求 2026-07-28）：
#     - 竞品/无关等硬噪音(filtered_as_noise) → 未分类（本就不在监控主体）
#     - 广告/控评/官方号等腾讯视频相关推广(soft) → 未分类（保留监控量，但不计入负面风险，也不计入中性）
#     - 其余真实帖 → classify_sentiment 判 pos / neu / neg
#   未分类段在看板保留展示，并对其含义做说明。
for _p in raw_posts:
    if _p.get("filtered_as_noise") or _p.get("noise_category") == "soft":
        _p["sentiment"] = "unclassified"
    else:
        _p["sentiment"] = classify_sentiment(_p.get("text"))

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

# 各平台计数（含情感三分类细分：pos / neu / neg；real_negative = 真实负面 = neg）
def plat_stats(platform):
    items = [r for r in raw_posts if r["platform"] == platform]
    real = [r for r in items if not r["filtered_as_noise"]]
    pos = sum(1 for r in real if r.get("sentiment") == "pos")
    neu = sum(1 for r in real if r.get("sentiment") == "neu")
    neg = sum(1 for r in real if r.get("sentiment") == "neg")
    return len(items), len(real), pos, neu, neg

wb_total, wb_real, wb_pos, wb_neu, wb_neg = plat_stats("weibo")
db_total, db_real, db_pos, db_neu, db_neg = plat_stats("douban")
xhs_total, xhs_real, xhs_pos, xhs_neu, xhs_neg = plat_stats("xiaohongshu")
hm_total, hm_real, hm_pos, hm_neu, hm_neg = plat_stats("heimao")

# ============================================================
# 5. 动态风险聚类（取代写死的事件模板）
#    对每条"去噪后的真实负面(neg)帖"按主题关键词聚类；
#    同一主题命中数 >= 阈值 即视为一个风险事件，并按严重度自动定级 P0/P1/P2，
#    不再依赖特定日期的事件模板，任何真实负面主题都不会漏判。
# ============================================================

# 主题定义：(key, 板块section, 基础级别, [板块命中子串], 最小簇大小, SUBTHEMES)
# SUBTHEMES: (sub_key, 子主题标题[具体事件原因], [命中子串], 最小簇大小)
# 两级聚类：先按板块(第1级)归并，再在板块内按 SUBTHEMES(第2级,具体事件原因)细分，
# 同一板块下不同原因(如内容运营下的"遇见王沥川下架""完美世界造黄谣""弃剧")分别成独立事件。
# 顺序即优先级：先匹配"会员/账号/极端"等高风险主题，再落到内容/技术/广告。
THEME_DEFS = [
    ("member", "会员收费", "P1",
     ["自动续费", "退款", "退钱", "退费", "乱扣费", "乱收费", "重复扣费", "扣费",
      "套娃收费", "超前点播", "会员权益", "权益缩水", "权益不保", "svip", "vip",
      "会员涨价", "未到账", "割韭菜", "霸王条款"], 1,
     [
        ("auto_renew", "自动续费/扣费未告知致退款诉求",
         ["自动续费", "扣费", "乱扣费", "未到账", "退款", "退钱", "退费"], 1),
        ("svip_quality", "SVIP/会员画质与权益不符预期",
         ["svip", "vip", "画质", "清晰度", "臻彩", "1080p", "会员比免", "升级到svip", "升svip", "吃相难看"], 1),
        ("minor", "未成年人/误充退款遭拒",
         ["未成年", "小孩", "孩子", "误充", "不小心"], 1),
        ("reward", "云包场/积分商城虚假宣传",
         ["云包场", "积分商城", "虚假宣传"], 1),
     ]),
    ("account", "账号安全", "P1",
     ["封号", "封禁", "盗号", "风控", "涉嫌诈骗", "账号限制", "社交功能", "永久封停"], 1,
     [
        ("wechat_ban", "微信账号批量封禁/风控误伤",
         ["封号", "封禁", "永久封停", "社交功能", "账号限制", "风控", "涉嫌诈骗"], 1),
     ]),
    ("extreme", "其他/监管竞品", "P0",
     ["抑郁", "想不开", "走投无路", "自伤", "自杀", "轻生", "活不下去", "逼到走投无路"], 1,
     [
        ("self_harm", "含极端自伤倾向的归因言论",
         ["抑郁", "想不开", "走投无路", "自伤", "自杀", "轻生", "活不下去", "逼到走投无路"], 1),
     ]),
    ("content", "内容运营", "P2",
     ["下架", "停更", "断更", "选角争议", "剪辑", "删减", "塌房", "价值观", "魔改",
      "抄袭", "烂尾", "悬浮", "抠图", "抵制", "辱女", "造黄谣", "黄谣", "弃剧", "弃坑", "难看",
      "配音", "原声"], 2,
     [
        ("wanglichuan", "《遇见王沥川》下架争议",
         ["遇见王沥川", "王沥川"], 1),
        ("perfect_world", "《完美世界》造黄谣/下架风波",
         ["完美世界"], 1),
        ("quit", "剧集弃剧/观感差",
         ["弃剧", "弃坑"], 1),
        ("produce", "抠图/滤镜/制作粗糙争议",
         ["抠图", "滤镜", "悬浮", "烂尾", "魔改", "抄袭", "塌房", "价值观", "造黄谣", "黄谣", "辱女"], 1),
        ("dubbing", "配音/原声争议（要求原声/拒配音）",
         ["配音", "原声", "拒配音", "改原声", "改为原声", "必须用原声", "希望用原声"], 1),
     ]),
    ("tech",    "技术功能", "P2",
     ["卡顿", "闪退", "崩溃", "黑屏", "白屏", "花屏", "无法播放", "看不了", "加载",
      "缓冲", "投屏", "故障", "报错", "画质差", "音画不同步", "卡死", "卡住",
      "登录不上", "无法登录"], 2,
     [
        ("playback", "播放卡顿/闪退/崩溃等故障",
         ["卡顿", "闪退", "崩溃", "黑屏", "白屏", "花屏", "无法播放", "看不了", "加载",
          "缓冲", "投屏", "故障", "报错", "画质差", "音画不同步", "卡死", "卡住",
          "登录不上", "无法登录"], 1),
     ]),
    ("ad",      "技术功能", "P2",
     ["广告多", "广告太多", "弹窗广告", "前情提要广告", "试看", "广告钢钢的"], 2,
     [
        ("ad_exp", "广告过多/试看限制体验差",
         ["广告多", "广告太多", "弹窗广告", "前情提要广告", "试看", "广告钢钢的"], 1),
     ]),
]
# 板块 -> SUBTHEMES 索引；子主题(section, sub_key) -> 命中词，便于按板块/子主题查找
_SECTION_SUBS = {t[0]: t[5] for t in THEME_DEFS}
_SUB_KW = {(sec, sk): skws for sec, _s, _l, _k, _m, subs in THEME_DEFS for sk, _t, skws, _sm in subs}

def _theme_of(text):
    t = text or ""
    for key, _sec, _lv, kws, _min, _subs in THEME_DEFS:
        if any(k in t for k in kws):
            return key
    return None

def _subtheme_of(section_key, text):
    t = text or ""
    for sk, _title, skws, _smin in _SECTION_SUBS.get(section_key, []):
        if any(k in t for k in skws):
            return sk
    return None

# 两级聚类：第1级按板块；第2级在板块内按具体事件原因(SUBTHEMES)细分
_clusters = {t[0]: [] for t in THEME_DEFS}
_other = []          # 未命中任何板块的零散真实负面
for _p in raw_posts:
    if _p["filtered_as_noise"] or _p.get("sentiment") != "neg":
        continue
    _k = _theme_of(_p.get("text"))
    if _k:
        _clusters[_k].append(_p)
    else:
        _other.append(_p)

_sub_clusters = {}   # (section_key, sub_key) -> [posts]：同板块下按具体原因细分
_section_other = {t[0]: [] for t in THEME_DEFS}   # 命中板块但未命中任何子主题的零散帖
for _sec, _posts in _clusters.items():
    for _p in _posts:
        _sk = _subtheme_of(_sec, _p.get("text"))
        if _sk:
            _sub_clusters.setdefault((_sec, _sk), []).append(_p)
        else:
            _section_other[_sec].append(_p)

def _engagement(p):
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return (_int(p.get("likes")) + _int(p.get("reposts")) * 3
            + _int(p.get("followers")) // 1000)

# 平台名中文化（报告/看板展示用汉字，不用拼音）
PLATFORM_CN = {"weibo": "微博", "xiaohongshu": "小红书", "heimao": "黑猫投诉", "douban": "豆瓣"}

def _to_evidence(p):
    return {
        "platform": PLATFORM_CN.get(p.get("platform"), p.get("platform")),
        "author": p.get("author") or "匿名用户",
        "followers": p.get("followers"),
        "likes": p.get("likes"),
        "reposts": p.get("reposts"),
        "published_at": p.get("published_at"),
        "permalink": p.get("permalink"),
        "text": (p.get("text") or "")[:120],
    }

def _build_event(section_key, sub_key, section, sub_title, base_level, posts):
    count = len(posts)
    # 严重度：基础级别；P2 主题若簇规模 >=10 升级为 P1（系统性），P0/P1 不升不降
    level = base_level
    if base_level == "P2" and count >= 10:
        level = "P1"
    # 平台分布（中文化：用汉字而非拼音）
    plats = {}
    for p in posts:
        cn = PLATFORM_CN.get(p["platform"], p["platform"])
        plats[cn] = plats.get(cn, 0) + 1
    plat_desc = "、".join("{} {}条".format(k, v) for k, v in plats.items())
    # 子主题(具体原因)内最高频命中词（用于摘要，帮助快速定位原因）
    kws = _SUB_KW.get((section_key, sub_key), [])
    kw_counter = {}
    for p in posts:
        for k in kws:
            if k in (p.get("text") or ""):
                kw_counter[k] = kw_counter.get(k, 0) + 1
    top_kw = sorted(kw_counter.items(), key=lambda x: -x[1])
    kw_desc = "、".join(k for k, _ in top_kw[:3]) if top_kw else sub_title
    # 代表性原文（按互动量取前 6）
    top = sorted(posts, key=_engagement, reverse=True)[:6]
    evidence = [_to_evidence(p) for p in top]
    sample = (posts[0].get("text") or "").replace("\n", " ")
    title = sub_title
    # 单句串联版（P2 列表 / 预警内联使用），平台已中文化
    summary = ("本轮在{}共聚类出 {} 条与「{}」相关的真实负面反馈（动态聚类，按事件原因自动归并，不依赖固定模板）。"
               "高频原因词：{}。典型表述如：「{}…」。建议结合工单/客服渠道核实处理时效。").format(
        plat_desc, count, sub_title, kw_desc, sample[:30])
    # 分点版（P0/P1 详情逐条展示，加标记；平台中文化、不再堆叠成一段）
    summary_points = [
        "本轮在{}，共聚类出 {} 条与「{}」相关的真实负面反馈（动态聚类，按事件原因自动归并，不依赖固定模板）。".format(
            plat_desc, count, sub_title),
        "高频原因词：{}。".format(kw_desc),
        "典型表述如：「{}…」。".format(sample[:30]),
        "建议结合工单/客服渠道核实处理时效。",
    ]
    return {
        "id": "EV-DYN-{}-{}-{}".format(section_key.upper(), sub_key.upper(), _NOW.strftime("%Y%m%d")),
        "level": level,
        "section": section,
        "title": title,
        "summary": summary,
        "summary_points": summary_points,
        "post_count": count,
        "evidence": evidence,
    }

sentiment_events = []
for key, section, base_level, _kws, min_count, subs in THEME_DEFS:
    for sk, stitle, _skws, smin in subs:
        posts = _sub_clusters.get((key, sk), [])
        if len(posts) >= smin:
            sentiment_events.append(_build_event(key, sk, section, stitle, base_level, posts))
    # 板块内未命中任何子主题的零散负面，>=3 条也单列，避免漏判
    so = _section_other.get(key, [])
    if len(so) >= 3:
        sentiment_events.append(_build_event(key, "other", section, "{}—其他分散负面".format(section), base_level, so))
# 兜底：未命中任何板块的零散真实负面，>=3 条也单列，避免漏判
if len(_other) >= 3:
    sentiment_events.append(_build_event("other", "other", "其他/监管竞品", "其他/监管竞品—其他分散负面", "P2", _other))

# 按级别(高危在前)、规模(大在前)二次排序，便于阅读与看板呈现
_lv_order = {"P0": 0, "P1": 1, "P2": 2}
sentiment_events.sort(key=lambda e: (_lv_order.get(e["level"], 9), -(e.get("post_count") or 0)))

# ============================================================
# 6. KPI
# ============================================================
def _build_conclusion(p0, p1, p2, events, hm_real):
    """核心水位结论：一句话，仅给出各级事件数 + 主要涉及的问题，不展开分点。"""
    by = {"P0": [], "P1": [], "P2": []}
    for e in events:
        lv = (e.get("level") or "").upper()
        if lv in by:
            by[lv].append(e)
    parts = []
    for lv, n in (("P0", p0), ("P1", p1), ("P2", p2)):
        issues = "、".join(e.get("title") or "" for e in by[lv]) if by[lv] else "无"
        parts.append("{} {} 起（主要：{}）".format(lv, n, issues))
    return "本轮风险分级：" + "；".join(parts) + "。"

p0 = sum(1 for e in sentiment_events if e["level"] == "P0")
p1 = sum(1 for e in sentiment_events if e["level"] == "P1")
p2 = sum(1 for e in sentiment_events if e["level"] == "P2")
# 情感三分类汇总（仅统计去噪后的真实帖）
pos_count = sum(1 for r in raw_posts if not r["filtered_as_noise"] and r.get("sentiment") == "pos")
neu_count = sum(1 for r in raw_posts if not r["filtered_as_noise"] and r.get("sentiment") == "neu")
neg_count = sum(1 for r in raw_posts if not r["filtered_as_noise"] and r.get("sentiment") == "neg")
# 真实负面 = 去噪后判定为 neg 的帖（情感三分类口径下，"真实负面"即 neg 维度）
real_negative = neg_count
# 未分类构成（软/硬/去重，均不依赖 total_collected，可先算）；unclassified 总数在 total_collected 定义后再计算
soft_count = sum(1 for r in raw_posts if r.get("noise_category") == "soft")          # 广告/控评/官方号
hard_count = sum(1 for r in raw_posts if r["filtered_as_noise"])                     # 竞品/无关硬噪音
dedup_count = dup_removed                                                          # 跨平台去重剔除
conclusion = _build_conclusion(p0, p1, p2, sentiment_events, hm_real)

# ---- 过滤前总抓取量（各平台采集/解析出的原始条数：时间窗口过滤、噪音过滤、跨平台去重之前）----
def _count(fn):
    d = load(fn)
    return len(d) if isinstance(d, list) else 0

# ---- 采集量统计 ----
#   in_window_total  : 窗口内实际纳入分析的采集量（与 raw_posts 同源，= 各平台 *_in_window 文件之和）
#   total_collected  : 本次共采集毛量（= 各平台本轮原始抓取并经解析的全部结果，
#                      含时间窗口外内容、跨平台重复、竞品/无关、广告控评等）。
#                      这是"本轮到底抓了多少条"的真实数字（一千余条），用于看板"本次共采集 xx 条"。
_collected = {
    "weibo": _count("weibo_parsed_in_window.json") + _count("weibo_content_parsed_in_window.json"),
    "douban": _count("douban_parsed_in_window.json"),
    "xiaohongshu": xhs_in_window,   # 窗口内已核实笔记数(见上文代码计算)
    "heimao": _count("heimao_parsed_in_window.json"),
}
in_window_total = sum(_collected.values())

# 各平台「本次共采集」毛量（原始抓取全部结果，未经过滤）
_crawled_files = {
    "weibo": ("weibo_parsed_all.json", "weibo_content_parsed_all.json"),
    "douban": ("douban_raw_results.json",),
    "xiaohongshu": ("xhs_raw_results.json",),
    "heimao": ("heimao_raw_results.json",),
}
_collected_by_crawled = {k: sum(_count(f) for f in fs) for k, fs in _crawled_files.items()}
total_collected = sum(_collected_by_crawled.values())   # 本次共采集毛量（一千余条）

# 符合情感色彩的帖子数（正面+中性+负面，即参与三分类的帖子）
sentiment_total = pos_count + neu_count + neg_count
# 非情感色彩：本次共采集但不符合情感色彩、未纳入三分类的内容
non_sentiment = max(total_collected - sentiment_total, 0)
# 非情感色彩构成拆解：时间窗口外（非本轮分析区间）+ 窗口内已识别剔除（重复 / 竞品无关 / 广告控评）
out_of_window = max(total_collected - in_window_total, 0)
duplicate = dup_removed                       # 跨平台/同平台重复
competitor_irrelevant = hard_count            # 竞品/无关内容
promo = soft_count                            # 广告/控评/官方号
non_sentiment_breakdown = {
    "out_of_window": out_of_window,
    "duplicate": duplicate,
    "competitor_irrelevant": competitor_irrelevant,
    "promo": promo,
}

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
    xhs_status = "本轮去噪有效相关 {} 条".format(xhs_real)

# ============================================================
# 6.5 每日自动整合进「过往风险回顾」(risk_history.json)
#   规则(用户要求)：以后每天早上采集完毕后，把当轮结果整合进历史，长期沉淀。
#   为杜绝此前"采集了却没写进历史"的断更问题，此步在每轮 build 时自动执行。
#   幂等策略：
#     - 按"日期"去重：当天已存在记录则不重复追加(保留既有/人工撰写的 highlight)，
#       避免同一天多档采集刷屏；如需强制多档留痕可设 ALLOW_MULTI_DAILY=1。
#     - 采集完全失败(raw_posts 为空)时跳过，绝不写入 0/0/0 空记录污染趋势。
#     - 设 SKIP_HISTORY_APPEND=1 可临时关闭(用于本地调试/复现历史时间窗)。
# ============================================================
_today = now.strftime("%Y-%m-%d")
_skip_append = os.environ.get("SKIP_HISTORY_APPEND") == "1"
_allow_multi = os.environ.get("ALLOW_MULTI_DAILY") == "1"
_has_today = any((isinstance(h, dict) and h.get("date") == _today) for h in risk_history)
if _skip_append:
    print("[历史] SKIP_HISTORY_APPEND=1，本轮不追加过往风险回顾")
elif len(raw_posts) == 0:
    print("[历史] 本轮 raw_posts 为空(疑似采集失败)，跳过追加，避免断更式空记录")
elif _has_today and not _allow_multi:
    print("[历史] {} 当日已有记录，按幂等策略不重复追加".format(_today))
else:
    _new_entry = {
        "date": _today,
        "slot": now.strftime("%H:%M") + "（每日自动整合）",
        "p0": p0, "p1": p1, "p2": p2,
        "pos": pos_count, "neu": neu_count, "neg": neg_count,
        "sentiment_total": sentiment_total,
        "non_sentiment": non_sentiment,
        "total": real_negative,
        "highlight": conclusion,
    }
    risk_history.append(_new_entry)
    with open(os.path.join(BASE, "risk_history.json"), "w", encoding="utf-8") as _hf:
        json.dump(risk_history, _hf, ensure_ascii=False, indent=2)
    print("[历史] 已自动整合当日记录 -> {} {} | P0/P1/P2={}/{}/{} 情感 pos/neu/neg={}/{}/{} total(neg)={} (历史累计 {} 条)".format(
        _today, _new_entry["slot"], p0, p1, p2, pos_count, neu_count, neg_count, real_negative, len(risk_history)))

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
            "weibo": {"total": wb_total, "real_negative": wb_neg, "pos": wb_pos, "neu": wb_neu},
            "douban": {"total": db_total, "real_negative": db_neg, "pos": db_pos, "neu": db_neu},
            "xiaohongshu": {"total": xhs_total, "real_negative": xhs_neg, "pos": xhs_pos, "neu": xhs_neu},
            "heimao": {"total": hm_total, "real_negative": hm_neg, "pos": hm_pos, "neu": hm_neu},
        },
        "schema_version": "1.1",
        "note": "本数据源每轮一并采集微博+豆瓣+小红书+黑猫投诉四平台并跨平台去重(重复内容删除)。raw_posts 为去重后的原始采集帖(含 platform 平台标记与 filtered_as_noise 噪音标记)；sentiment_events 为按看板过滤规则归并分级后的真实负面/风险事件；risk_history 为逐轮风险回顾全量历史。所有内容基于真实采集，不含编造数据。",
    },
    "kpi": {
        "total_collected": total_collected,                  # 本次共采集毛量（一千余条）
        "collected_by_platform": _collected_by_crawled,      # 各平台本次共采集量
        "in_window_total": in_window_total,                  # 窗口内纳入分析的采集量
        "total_raw": len(raw_posts),
        "real_negative": real_negative,
        "pos": pos_count, "neu": neu_count, "neg": neg_count,
        "sentiment_total": sentiment_total,                  # 符合情感色彩的帖子数
        "non_sentiment": non_sentiment,                      # 非情感色彩（已剔除）量
        "non_sentiment_breakdown": non_sentiment_breakdown,
        "p0": p0, "p1": p1, "p2": p2,
        "conclusion": conclusion,
    },
    "sentiment_events": sentiment_events,
    "raw_posts": raw_posts,
    "risk_history": risk_history,
}

out = os.path.join(BASE, "datasource.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(datasource, f, ensure_ascii=False, indent=2)

print("本次共采集:", total_collected, "| 符合情感色彩:", sentiment_total,
      "(neg", neg_count, "/pos", pos_count, "/neu", neu_count, ")",
      "| 非情感色彩剔除:", non_sentiment,
      "(窗口外", out_of_window, "/重复", duplicate, "/竞品无关", competitor_irrelevant, "/广告控评", promo, ")")
print("平台明细  微博:{}/({}neg {}pos {}neu)  豆瓣:{}/({}neg {}pos {}neu)  小红书:{}/({}neg {}pos {}neu)  黑猫:{}/({}neg {}pos {}neu) (采集/情感细分)".format(
    wb_total, wb_neg, wb_pos, wb_neu, db_total, db_neg, db_pos, db_neu,
    xhs_total, xhs_neg, xhs_pos, xhs_neu, hm_total, hm_neg, hm_pos, hm_neu))
print("events P0/P1/P2:", p0, p1, p2, "| risk_history:", len(risk_history))
print("written:", out)
