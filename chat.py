# -*- coding: utf-8 -*-
"""对话引擎：把一句自然语言路由到对应功能。

设计原则：
    · 规则优先 —— 关键词能判准的就不调模型（快、免费、可控）
    · 不预测涨跌 —— 只用手里的事实数据回答
"""
import re

import market
import monitor
import news
import watchlist

# 币种别名
ALIASES = {
    '比特币': 'BTCUSDT', '大饼': 'BTCUSDT', 'btc': 'BTCUSDT',
    '以太坊': 'ETHUSDT', '以太': 'ETHUSDT', 'eth': 'ETHUSDT',
    '索拉纳': 'SOLUSDT', 'solana': 'SOLUSDT', 'sol': 'SOLUSDT',
    '币安币': 'BNBUSDT', 'bnb': 'BNBUSDT',
    '狗狗币': 'DOGEUSDT', 'doge': 'DOGEUSDT',
    '瑞波': 'XRPUSDT', 'xrp': 'XRPUSDT',
    '艾达': 'ADAUSDT', 'ada': 'ADAUSDT',
    'link': 'LINKUSDT', 'arb': 'ARBUSDT',
    'trump': 'TRUMPUSDT', '特朗普币': 'TRUMPUSDT',
    'zec': 'ZECUSDT', 'uni': 'UNIUSDT', 'sui': 'SUIUSDT',
    'pepe': 'PEPEUSDT', 'wif': 'WIFUSDT',
}

# 意图规则（越具体的放前面）
RULES = [
    ('news',      r'(新闻|快讯|消息面|宏观|美联储|特朗普|讲话|关税|非农|CPI|PCE|加息|降息|议息|政策|地缘|战争|大选|SEC|监管)'),
    ('plan',      r'(方案|分析一下|怎么看|该做多|该做空|能不能做|有机会|值不值得|要不要做|能买|能做)'),
    ('pick',      r'(适合.*开仓|适合.*交易|可以开.*哪些|开仓.*哪些|哪些币|哪些币种|什么币|买什么|做什么币|推荐.*币种|推荐.*币)'),
    ('scan',      r'(扫描|扫一遍|推荐|哪些币|异动|全市场|热门|有啥机会|有什么机会)'),
    ('positions', r'(我的持仓|我的仓位|持仓怎么样|仓位健康|拿着什么|爆仓距离|我买的)'),
    ('review',    r'(复盘|总结|我亏在哪|我的问题|亏钱的原因|分析我自己)'),
    ('dashboard', r'(绩效|看板|胜率|盈亏比|回撤|统计)'),
    ('quote',     r'(现价|多少钱|价格|行情|费率|资金费)'),
    ('sync',      r'(同步|绑定|拉取).*(持仓|仓位)'),
]

LABELS = {
    'plan': '生成交易方案', 'scan': '扫描全市场', 'pick': '筛选可观察币种',
    'news': '新闻风险',
    'positions': '查看持仓', 'review': '复盘', 'dashboard': '绩效看板',
    'quote': '查行情',
    'sync': '同步持仓', 'chat': '对话',
}


def find_symbol(text):
    """从一句话里找出币种代码。"""
    t = (text or '').lower()
    m = re.search(r'\b([a-z0-9]{2,15})\s*[-/]?\s*usdt\b', t)
    if m:
        return m.group(1).upper() + 'USDT'
    for alias, sym in sorted(ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in t:
            return sym
    m = re.search(r'\b([A-Z]{2,6})\b', text or '')
    if m and m.group(1) not in ('USDT', 'AI', 'OK', 'API'):
        return m.group(1) + 'USDT'
    return None


def classify(text):
    """判断这句话想干什么。返回 (意图, 币种)。"""
    t = (text or '').strip()
    if not t:
        return 'chat', None
    sym = find_symbol(t)
    # 解释型问题优先当问答，避免“资金费率”里的“费率”把它误判成查行情。
    if re.search(r'(什么是|是什么意思|解释一下|解释下|怎么理解|有什么区别|为什么会|为什么是)', t, re.I):
        return 'chat', sym
    for name, pattern in RULES:
        if re.search(pattern, t, re.I):
            return name, sym
    return 'chat', sym


# ---------------- 各意图执行 ----------------

def run_quote(symbol):
    sym = symbol or 'BTCUSDT'
    snap = market.snapshot(sym, '自动')
    ind = market.compute_indicators(snap.get('K线'), snap.get('标记价'))
    return {'类型': '行情', '币种': market.normalize_symbol(sym),
            '快照': snap, '指标': ind}


def run_positions():
    pos = monitor.load_positions()
    if not pos:
        return {'类型': '持仓', '持仓': [],
                '提示': '现在没有持仓记录。可以在左边「工具 → 持仓同步」把交易所的实际仓位拉过来。'}
    rows = []
    for p in pos:
        try:
            price = market.snapshot(p['币种'], '自动')['标记价']
            h = monitor.position_health(p, price)
            h['开仓价'] = p.get('开仓价')
            h['杠杆'] = p.get('杠杆')
            h['止损价'] = p.get('止损价')
            rows.append(h)
        except Exception as e:
            rows.append({'币种': p.get('币种'), '错误': f'{type(e).__name__}'})
    return {'类型': '持仓', '持仓': rows}


def run_scan(top_n=200):
    r = watchlist.discover(top_n=top_n)
    if r.get('错误'):
        return {'类型': '扫描', '错误': r['错误']}
    return {'类型': '扫描', '分类': watchlist.quick_categories(r['数据']),
            '全部': r['数据'], '扫描时间': r['扫描时间'],
            '全市场合约数': r['全市场合约数']}


def run_news():
    """抓取高影响新闻，只做事实梳理和波动风险提示，不判断利好利空。"""
    result = news.scan(limit=60)
    note = news.risk_note(result)
    return {'类型': '新闻', '扫描': result, '风险': note}


def run_pick(limit=6):
    """筛选“值得先看”的币种，不把它们包装成买入推荐。"""
    r = watchlist.discover(top_n=300)
    if r.get('错误'):
        return {'类型': '候选', '错误': r['错误']}
    rows = []
    excluded = {
        'USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USD1USDT',
        'XAUUSDT', 'XAGUSDT', 'CLUSDT', 'NGUSDT', 'EURUSDT', 'GBPUSDT',
        'SOXLUSDT', 'SPYUSDT', 'QQQUSDT', 'TSLAUSDT', 'AAPLUSDT',
        'NVDAUSDT', 'MSFTUSDT', 'AMZNUSDT', 'GOOGLUSDT', 'METAUSDT',
        'COINUSDT', 'MSTRUSDT',
    }
    crypto_only = watchlist.crypto_contracts()
    for row in r.get('数据') or []:
        sym = str(row.get('币种') or '')
        if not sym.endswith('USDT') or sym in excluded:
            continue
        if crypto_only and sym not in crypto_only:
            continue
        change = abs(float(row.get('24h涨跌%') or 0))
        fr = abs(float(row.get('资金费率%') or 0))
        rank = int(row.get('成交额排名') or 9999)
        # 先排除极端波动和极端费率，再按流动性、波动和费率排。
        if change > 8 or fr > 0.08:
            continue
        score = rank + change * 3 + fr * 120
        item = dict(row)
        item['_score'] = score
        rows.append(item)
    rows.sort(key=lambda x: x['_score'])
    picked = rows[:limit]
    return {
        '类型': '候选',
        '候选': picked,
        '筛选条件': '成交额靠前；24小时涨跌绝对值不超过 8%；资金费率绝对值不超过 0.08%。',
        '说明': '没有“绝对适合开仓”的币种。下面只是先帮你收敛到流动性好、极端程度低的标的；具体能不能开，还要看你的入场结构和止损。',
        '扫描时间': r.get('扫描时间'),
        '全市场合约数': r.get('全市场合约数'),
    }


def run_review():
    import journal
    import metrics
    import review
    d = journal.load()
    findings, keys = review.rule_review(d)
    return {'类型': '复盘', '结论': findings, '关键数字': keys,
            '指标': metrics.summary(d),
            '有数据': metrics.summary(d).get('交易笔数', 0) > 0}


def run_dashboard():
    import journal
    import metrics
    d = journal.load()
    return {'类型': '绩效', '指标': metrics.summary(d),
            '按币种': metrics.by_group(d, '币种'),
            '按情绪': metrics.by_group(d, '情绪')}


def run_plan(symbol, cfg=None, use_debate=True, force_dir=None):
    import plan_ui
    return plan_ui.start_task(symbol or 'BTCUSDT', cfg or {},
                              use_debate=use_debate, force_dir=force_dir)


def run_chat(text, symbol=None, allow_llm=False):
    """开放问题：用手里的事实回答，不预测涨跌。"""
    lines = []
    sym = symbol or 'BTCUSDT'
    try:
        q = run_quote(sym)
        snap, ind = q['快照'], q['指标']
        lines.append(f"**{q['币种']}** 现价 **{snap['标记价']:,.4f}**")
        lines.append('')
        if snap.get('24h涨跌幅') is not None:
            lines.append(f"- 24小时涨跌 **{snap['24h涨跌幅']:+.2f}%**")
        if snap.get('资金费率') is not None:
            fr = snap['资金费率'] * 100
            tone = ('偏高 → 多头拥挤，做多要付钱' if fr > 0.03
                    else '偏负 → 空头拥挤，做空要付钱' if fr < -0.03 else '正常区间')
            lines.append(f"- 8小时资金费率 **{fr:+.4f}%**（{tone}；常规基准约 0.01%）")
        if snap.get('多空比') is not None:
            lines.append(f"- 账户多空比 **{snap['多空比']:.2f}**"
                         f"（{'多头账户更多' if snap['多空比'] > 1 else '空头账户更多'}）")
        if ind.get('ATR14百分比'):
            lines.append(f"- 4小时 ATR **{ind['ATR14百分比']:.2f}%**"
                         '（你的止损距离应该大于这个数，否则容易被扫）')
        if ind.get('距近期高点百分比') is not None:
            lines.append(f"- 距最近 30 根 K 线高点 **{ind['距近期高点百分比']:+.2f}%**")
    except Exception as e:
        lines.append(f'注意：拿不到行情（{type(e).__name__}）')

    if allow_llm:
        try:
            import llm
            facts = '\n'.join(lines)
            answer, _usage, _model = llm.chat(
                '你是加密货币合约的风险助手。只根据用户提供的事实回答，'
                '不预测涨跌，不承诺收益。回答要短、直接、可执行；'
                '如果信息不足，就明确说信息不足。',
                f'用户问题：{text}\n\n可用事实：\n{facts}',
                model=llm.model_name('analyst'),
                temperature=0.2, max_tokens=700)
            return {'类型': '对话', '内容': answer}
        except Exception as e:
            lines += ['', f'> 模型回答暂时不可用：{type(e).__name__}']

    lines += ['', '---', '', '**我能帮你做这些**（直接打字，或点下面的按钮）：',
              '', '| 你说 | 我做 |', '|---|---|',
              '| 帮我分析一下 BTC | 跑多模型分析 → 出完整方案 |',
              '| 扫描有什么异动 | 扫全市场，按事实分类 |',
              '| 我的持仓怎么样 | 算距爆仓 / 距止损 / 浮盈亏 |',
              '| 帮我复盘 | 从你的交易记录里找亏损规律 |',
              '| BTC 现价多少 | 拉实时行情 + 资金费率 |',
              '', '> **我不预测涨跌。** 我用 42 个模型、600+ 次调用验证过：'
              '方向判断准确率 54-61%，和抛硬币没有统计差异。'
              '所以我只做三件事 —— **把数字算准、把风险摆明、把纪律拦住**。']
    return {'类型': '对话', '内容': '\n'.join(lines)}


def dispatch(text, cfg=None, allow_llm=False):
    """把一句话分发到对应功能。返回 (意图, 结果)。"""
    intent, sym = classify(text)
    if intent == 'plan':
        return intent, {'类型': '方案任务', 'task_id': run_plan(sym, cfg),
                        '币种': sym or 'BTCUSDT'}
    if intent == 'scan':
        return intent, run_scan()
    if intent == 'pick':
        return intent, run_pick()
    if intent == 'news':
        return intent, run_news()
    if intent == 'positions':
        return intent, run_positions()
    if intent == 'review':
        return intent, run_review()
    if intent == 'dashboard':
        return intent, run_dashboard()
    if intent == 'quote':
        return intent, run_quote(sym)
    return 'chat', run_chat(text, sym, allow_llm=allow_llm)


def quick_actions():
    """快捷按钮。"""
    return [
        ('生成方案', '帮我分析一下 BTC'),
        ('适合开仓', '适合开仓的币种'),
        ('扫描市场', '扫描一下全市场有什么异动'),
        ('查看持仓', '我的持仓怎么样'),
        ('复盘', '帮我复盘一下我亏在哪'),
        ('绩效', '看看我的绩效'),
        ('查行情', 'BTC 现价多少'),
        ('新闻风险', '看看现在有哪些高影响新闻'),
    ]
