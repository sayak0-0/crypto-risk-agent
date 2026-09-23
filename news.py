# -*- coding: utf-8 -*-
"""新闻事件监控：抓实时新闻 → 识别高影响事件 → 风控提醒。

⚠️ 边界声明（很重要）：
  本模块**不预测新闻会让价格涨还是跌**。理由：
    · 市场经常反着走（利好出尽、买预期卖事实）
    · 反应发生在几秒内，等读到新闻行情已经走完
    · 同一条新闻在不同市场状态下效果相反

  本模块做的是三件事（都是风控，不需要预测方向）：
    ① 告诉你「现在有什么高影响事件正在发生」
    ② 告诉你「这类事件历史上通常伴随波动放大」→ 提醒你减小仓位
    ③ 事后归因：价格异动时，把同期新闻摆出来
    
  换句话说：它不告诉你「该做多还是做空」，
  它告诉你「现在这个时点适不适合开仓」。

数据源：各家 RSS（免费、无 Key）。新闻标题属于第三方内容，
只作为事实线索，不构成交易建议。
"""
import re
import time
from datetime import datetime, timedelta, timezone

import requests

UA = {'User-Agent': 'Mozilla/5.0 (compatible; trading-journal-agent)'}

FEEDS = [
    ('Cointelegraph', 'https://cointelegraph.com/rss', 'crypto'),
    ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'crypto'),
    ('美联储', 'https://www.federalreserve.gov/feeds/press_all.xml', 'macro'),
    ('SEC', 'https://www.sec.gov/news/pressreleases.rss', 'regulatory'),
    ('白宫', 'https://www.whitehouse.gov/presidential-actions/feed/', 'policy'),
    ('Reuters 商业', 'https://feeds.reuters.com/reuters/businessNews', 'macro'),
    ('CFTC', 'https://www.cftc.gov/RSS/RSSGP/rssgp.xml', 'regulatory'),
]

# 高影响事件识别：只用**标题**匹配，而且区分强弱。
# 教训：一开始匹配摘要 + 关键词太宽（比如只要有 "etf" 就算），
# 结果 79 条里 56% 都被判成高影响 —— 那样提醒就没有任何意义了。
#
# 强信号：标题命中就直接算高影响事件
STRONG_PATTERNS = {
    '货币政策': [r'\bfomc\b', r'rate (cut|hike|decision)', r'federal (reserve|funds) rate',
                 r'powell (says|signals|warns)', r'降息', r'加息', r'利率决议'],
    '通胀数据': [r'\bcpi\b', r'\bpce\b', r'inflation (data|report|cools|rises|jumps|surges)',
                 r'通胀数据', r'消费者物价'],
    '就业数据': [r'nonfarm', r'non-farm', r'jobs report', r'payrolls? (rise|fall|beat|miss)',
                 r'非农', r'失业率'],
    '关税贸易': [r'tariff', r'trade war', r'export (ban|control)', r'sanctions? on',
                 r'关税', r'贸易战', r'出口管制'],
    # 注意：'sec charges 某骗子' 这类普通执法案对大盘没影响，
    # 所以只保留「针对大盘资产/交易所」和「ETF 审批」这两种真正影响市场的
    '加密监管': [r'sec (approves|rejects) (the )?etf',
                 r'etf (approval|approved|launch|reject|denied)',
                 r'sec (sues|charges) (binance|coinbase|ripple|kraken|okx|tether|circle)',
                 r'regulat(ion|ory) (bill|law|framework|crackdown) (passes|signed|proposed)',
                 r'监管法案', r'etf 获批', r'etf 被拒'],
    '交易所风险': [r'exchange (hack|exploit|halt|suspend)', r'suspend(s|ed)? withdrawals',
                   r'insolven|bankrupt', r'被盗', r'暂停提现', r'破产'],
    '地缘政治': [r'\bwar\b', r'invasion', r'military (strike|action)', r'geopolit',
                 r'战争', r'军事行动', r'袭击'],
    '重大政策': [r'trump (says|announces|signs|threatens|imposes)',
                 r'executive order', r'strategic (bitcoin|reserve)',
                 r'特朗普(说|宣布|签署|威胁)', r'行政命令'],
}

# 弱信号：标题必须同时命中两个才算（避免 "ETF 流入创新高" 这种日常新闻被误判）
WEAK_PAIRS = [
    (r'\betf\b', r'(approval|approved|launch|reject|denied)'),
    (r'trump', r'(crypto|bitcoin|tariff|china|fed)'),
    (r'(binance|coinbase|okx|kraken)', r'(probe|lawsuit|charges|settle|sec|suspend)'),
    (r'sec (charges|sues)', r'(binance|coinbase|ripple|kraken|okx|tether|circle)'),
    (r'(china|russia|iran)', r'(ban|sanction|restrict|tariff)'),
]


def _compile_patterns():
    out = {}
    for cat, pats in STRONG_PATTERNS.items():
        out[cat] = [re.compile(p, re.I) for p in pats]
    weak = [(re.compile(a, re.I), re.compile(b, re.I)) for a, b in WEAK_PAIRS]
    return out, weak


_STRONG, _WEAK = _compile_patterns()



# 命中某类事件时的风控建议（保守规则，不是预测）
IMPACT_ADVICE = {
    '货币政策': '利率决议前后波动通常放大。建议决议前把仓位降到平时一半以内，或空仓等结果。',
    '通胀数据': 'CPI/PCE 公布瞬间常有剧烈波动。建议公布前 30 分钟到后 30 分钟不要开新仓。',
    '就业数据': '非农公布时点波动集中。同样建议避开公布前后半小时。',
    '关税贸易': '关税类消息多为突发，方向难料但波动确定放大。持仓要检查止损距离。',
    '加密监管': '监管消息对加密影响直接且剧烈，可能单边大幅波动。',
    '交易所风险': '交易所风险会引发挤兑踩踏，流动性可能瞬间枯竭，止损单可能失效。',
    '地缘政治': '地缘冲突通常引发风险资产抛售，波动放大。',
    '重大政策': '重大政策声明前后波动放大，且方向难料。',
    '需要关注': '这条新闻同时命中两个关键词，可能值得留意，但不确定是否构成事件冲击。',
}


def _clean(text):
    text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', text or '', flags=re.S)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_feed(xml, source, kind):
    """从 RSS/Atom 里抠出条目。"""
    items = []
    # RSS: <item>...</item>
    for block in re.findall(r'<item[\s>].*?</item>', xml, re.S) or \
                 re.findall(r'<entry[\s>].*?</entry>', xml, re.S):
        t = re.search(r'<title[^>]*>(.*?)</title>', block, re.S)
        title = _clean(t.group(1)) if t else ''
        if not title:
            continue
        d = re.search(r'<pubDate[^>]*>(.*?)</pubDate>', block, re.S) or \
            re.search(r'<updated[^>]*>(.*?)</updated>', block, re.S) or \
            re.search(r'<published[^>]*>(.*?)</published>', block, re.S)
        date = _clean(d.group(1)) if d else ''
        l = re.search(r'<link[^>]*href="([^"]+)"', block) or \
            re.search(r'<link[^>]*>(.*?)</link>', block, re.S)
        link = (l.group(1) if l else '').strip()
        desc = re.search(r'<description[^>]*>(.*?)</description>', block, re.S)
        items.append({
            '来源': source, '类型': kind, '标题': title,
            '时间': date, '链接': link,
            '摘要': _clean(desc.group(1))[:200] if desc else '',
        })
    return items


def fetch_news(limit=60, timeout=20):
    """抓取所有 RSS 源。返回去重后的新闻列表。"""
    out, errors = [], []
    for name, url, kind in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code != 200:
                errors.append(f'{name}: HTTP {r.status_code}')
                continue
            got = parse_feed(r.text, name, kind)
            out.extend(got[:20])
        except Exception as e:
            errors.append(f'{name}: {type(e).__name__}')
    # 按标题去重
    seen, uniq = set(), []
    for it in out:
        key = it['标题'][:40]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq[:limit], errors


def classify(item):
    """判断标题命中了哪些高影响类别。

    只用标题匹配（摘要里太多噪声）。
    强信号单独命中即可，弱信号要成对出现。
    返回 (类别列表, 强度)；强度 '高' 或 '中'。
    """
    title = item.get('标题', '') or ''
    hits = []
    for cat, pats in _STRONG.items():
        if any(p.search(title) for p in pats):
            hits.append(cat)
    if hits:
        return hits, '高'
    for a, b in _WEAK:
        if a.search(title) and b.search(title):
            return ['需要关注'], '中'
    return [], ''

def scan(limit=60):
    """抓新闻 + 分类，返回高影响事件清单。"""
    news, errors = fetch_news(limit)
    for it in news:
        cats, strength = classify(it)
        it['影响类别'] = cats
        it['强度'] = strength
        it['高影响'] = strength == '高'
    high = [it for it in news if it['高影响']]
    cats = {}
    for it in high:
        for c in it['影响类别']:
            cats.setdefault(c, []).append(it)
    return {
        '抓取时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '总条数': len(news),
        '高影响条数': len(high),
        '按类别': {c: {'条数': len(v), '建议': IMPACT_ADVICE.get(c, ''),
                       '示例': [x['标题'][:70] for x in v[:3]]}
                   for c, v in sorted(cats.items(), key=lambda x: -len(x[1]))},
        '高影响事件': high,
        '全部新闻': news,
        '错误': errors,
    }


def risk_note(scan_result, has_position=False):
    """根据当前新闻，给一句风控提示。"""
    cats = scan_result.get('按类别') or {}
    if not cats:
        return {'级别': '正常', '提示': '当前没有检测到高影响事件。'}
    top = list(cats.items())[0]
    n = scan_result['高影响条数']
    level = '警告' if n >= 5 else '提示'
    lines = [f"检测到 {n} 条可能引发波动的新闻，主要集中在「{top[0]}」。"]
    if top[1].get('建议'):
        lines.append(top[1]['建议'])
    if has_position:
        lines.append('⚠️ 你当前有持仓 —— 建议检查止损距离是否够扛住放大的波动。')
    return {'级别': level, '提示': ' '.join(lines), '主要类别': top[0]}


def llm_interpret(items, model=None, api_key=None):
    """让模型解读新闻——但严格限定为「事实梳理 + 风险提示」，禁止预测方向。"""
    import llm
    if not items:
        return None
    titles = '\n'.join(f"- [{x['来源']}] {x['标题']}" for x in items[:12])
    prompt = f'''下面是刚刚抓到的几条新闻标题：

{titles}

请输出 JSON（只输出 JSON，不要其他文字）：
{{
  "事实梳理": "这几条新闻在说什么。只描述事实，不要加解读",
  "可能受影响的方向": "说明哪些资产/板块可能受影响",
  "波动预期": "放大 或 中性 或 收窄",
  "风险提示": "给持仓者的具体提醒",
  "我不确定的地方": "哪些信息你无法从标题判断"
}}

硬性要求：
- 禁止预测价格涨跌方向。不要写「利好」「利空」「会涨」「会跌」。
- 只允许说「波动可能放大/收窄」这类关于波动幅度的判断。
- 如果标题信息不足，就直接说信息不足。
- 中文回答。'''
    try:
        text, usage, used = llm.chat(
            '你是金融风险监控助手。你只做事实梳理和风险提示，不做价格预测。',
            prompt, model=model, api_key_override=api_key,
            temperature=0.2, max_tokens=800)
        import json as _json
        t = re.sub(r'^```(?:json)?\s*', '', (text or '').strip())
        t = re.sub(r'\s*```$', '', t)
        try:
            obj = _json.loads(t)
        except Exception:
            i, j = t.find('{'), t.rfind('}')
            obj = _json.loads(t[i:j + 1]) if i >= 0 and j > i else {'原文': text}
        obj['_模型'] = used
        obj['_用量'] = usage
        return obj
    except Exception as e:
        return {'错误': str(e)[:200]}