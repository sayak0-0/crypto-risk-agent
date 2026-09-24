# -*- coding: utf-8 -*-
"""交易方案生成：方向 + 入场 + 止损 + 止盈 + 仓位，每个数字都可追溯。

设计原则（和前面的多智能体不同，这一版有硬性的分工）：
    代码负责「算」——结构位、止损候选、盈亏比、仓位、爆仓价，全是确定性的公式
    模型负责「选」——从代码算好的候选里挑一个，并说明理由

模型不允许凭空给价格。它只能在给定候选里选，所以每个数字都能追溯。

⚠️ 诚实提示：这不会让方向判断变准。回测显示模型判断方向约 54-61%，
和抛硬币没有统计差异。本模块的价值在于「把数字算到精确、把风险算到明处」，
而不是提高胜率。
"""
import json
import math
import re

import market
import risk
from common import get_env
from llm import analyst_models, chat, model_name


# ---------------- 结构位计算（纯代码） ----------------

def swing_points(klines, lookback=20, tail=None):
    """从 K 线里找近期摆动高/低点。"""
    if not klines or len(klines) < lookback + 1:
        return {}
    highs = [k[2] for k in klines]
    lows = [k[3] for k in klines]
    closes = [k[4] for k in klines]
    return {
        '近期最高': max(highs[-lookback:]),
        '近期最低': min(lows[-lookback:]),
        '前高': max(highs[-lookback * 2:-lookback]) if len(highs) >= lookback * 2 else None,
        '前低': min(lows[-lookback * 2:-lookback]) if len(lows) >= lookback * 2 else None,
        '最新收盘': closes[-1],
    }


def stop_candidates(price, atr, ind, sp, direction):
    """算出一组候选止损位。每个都带计算依据。

    direction: 'long' 或 'short'
    返回 [{'名称','价格','距离百分比','说明'}, ...]
    """
    out = []

    def add(name, px, why):
        if px is None or px <= 0:
            return
        dist = abs(price - px) / price * 100
        if direction == 'long' and px >= price:
            return
        if direction == 'short' and px <= price:
            return
        out.append({'名称': name, '价格': round(px, 6),
                    '距离百分比': round(dist, 3), '说明': why})

    if direction == 'long':
        if sp.get('近期最低'):
            add('近期摆动低点外', sp['近期最低'] - atr * 0.2,
                f"近 20 根 K 线最低点 {sp['近期最低']:.4f} 再留 0.2×ATR 缓冲，"
                '避开刚好扫到低点的假突破')
        if sp.get('前低'):
            add('前低外', sp['前低'] - atr * 0.2,
                f"更早一轮的低点 {sp['前低']:.4f}，是更强的支撑")
        for mult in (1.0, 1.5, 2.0, 2.5):
            add(f'{mult:g}倍ATR', price - atr * mult,
                f'纯波动率止损：{mult:g}×ATR({atr:.4f})。'
                f'倍数越大越不容易被扫，但止损金额也越大')
        if ind.get('MA20'):
            add('MA20下方', ind['MA20'] - atr * 0.3,
                f"4小时 MA20 是 {ind['MA20']:.4f}，跌破它意味着短线结构走坏")
        if ind.get('MA60'):
            add('MA60下方', ind['MA60'] - atr * 0.3,
                f"4小时 MA60 是 {ind['MA60']:.4f}，中期趋势线")
    else:
        if sp.get('近期最高'):
            add('近期摆动高点外', sp['近期最高'] + atr * 0.2,
                f"近 20 根 K 线最高点 {sp['近期最高']:.4f} 再留 0.2×ATR 缓冲")
        if sp.get('前高'):
            add('前高外', sp['前高'] + atr * 0.2,
                f"更早一轮的高点 {sp['前高']:.4f}，是更强的阻力")
        for mult in (1.0, 1.5, 2.0, 2.5):
            add(f'{mult:g}倍ATR', price + atr * mult,
                f'纯波动率止损：{mult:g}×ATR({atr:.4f})')
        if ind.get('MA20'):
            add('MA20上方', ind['MA20'] + atr * 0.3,
                f"4小时 MA20 是 {ind['MA20']:.4f}，站上它意味着短线结构转强")
        if ind.get('MA60'):
            add('MA60上方', ind['MA60'] + atr * 0.3,
                f"4小时 MA60 是 {ind['MA60']:.4f}")

    # 距离过滤：上下限都要跟着波动率走
    # ⚠️ 原来上限写死 15%，高波动币（ATR>8%）会把 1.5/2/2.5 倍 ATR 全砍掉，
    #    只剩 1 倍 ATR 一个候选，方案官无从选择只能说「不做」。
    #    实测踩过：ATR 10% 的币只剩 1 个候选。
    atr_pct = atr / price * 100 if price else 2.0
    # 下限：至少 0.5 倍 ATR —— 止损比半个 ATR 还近，等于送钱给正常波动
    lo = max(0.05, atr_pct * 0.5)
    # 上限：至少 3.5 倍 ATR —— 否则高波动币会被固定上限砍光候选
    hi = max(15.0, atr_pct * 3.5)
    out = [x for x in out if lo <= x['距离百分比'] <= hi]
    # 按距离排序
    out.sort(key=lambda x: x['距离百分比'])
    return out


def rr_targets(entry, stop, direction, ratios=(1.5, 2.0, 2.5, 3.0)):
    """按盈亏比反推止盈价。"""
    dist = abs(entry - stop)
    out = []
    for r in ratios:
        px = entry + dist * r if direction == 'long' else entry - dist * r
        out.append({'盈亏比': r, '止盈价': round(px, 6),
                    '盈利空间百分比': round(dist * r / entry * 100, 3)})
    return out


# ---------------- 最小下单量检查 ----------------
# 本金小的时候，按风险算出来的仓位可能低于交易所的最小下单量，
# 根本开不了仓。必须提前发现并说清楚。
DEFAULT_MIN_NOTIONAL = 100.0      # 保守默认（币安 BTCUSDT 是 100 USDT）

COMMON_MIN_NOTIONAL = {
    'BTCUSDT': 100.0, 'ETHUSDT': 20.0, 'BNBUSDT': 20.0, 'SOLUSDT': 5.0,
    'XRPUSDT': 5.0, 'DOGEUSDT': 5.0, 'ADAUSDT': 5.0, 'LINKUSDT': 5.0,
    'ARBUSDT': 5.0, 'ENAUSDT': 5.0, 'TRUMPUSDT': 5.0, 'ONDOUSDT': 5.0,
}


def fetch_min_notional(symbol):
    """从币安拿这个交易对的最小名义价值。拿不到就用保守默认值。"""
    sym = market.normalize_symbol(symbol)
    try:
        info = market._get('https://fapi.binance.com/fapi/v1/exchangeInfo')
        for s in info.get('symbols', []):
            if s.get('symbol') != sym:
                continue
            for f in s.get('filters', []):
                if f.get('filterType') == 'MIN_NOTIONAL':
                    return float(f.get('notional') or DEFAULT_MIN_NOTIONAL)
    except Exception:
        pass
    return COMMON_MIN_NOTIONAL.get(sym, DEFAULT_MIN_NOTIONAL)


def check_min_notional(symbol, notional, min_notional=None):
    """检查仓位够不够最小下单量。"""
    mn = min_notional if min_notional is not None else fetch_min_notional(symbol)
    ok = notional >= mn
    return {
        '通过': ok, '最小名义价值': mn, '实际名义价值': round(notional, 2),
        '说明': (f'仓位名义价值 {notional:,.2f} USDT，交易所最小 {mn:,.0f} USDT，可以开。'
                 if ok else
                 f'⚠️ 仓位名义价值只有 {notional:,.2f} USDT，'
                 f'低于这个交易对的最小下单量 {mn:,.0f} USDT —— '
                 f'这种仓位开不出来。要么加大本金，要么把止损放近一点。'),
    }


# ---------------- 分批建仓 ----------------
def split_entries(price, stop, direction, total_qty, batches=3):
    """把总仓位拆成几批，给出每批的价位和数量。

    为什么这么做：从实战笔记里学到的 —— 一次性满仓没有容错空间，
    分批建仓能摊薄成本、留子弹应对不利走势。

    ⚠️ 关键约束：**总风险仍然是原来那么多**，不是每批都算 1%。
       分批的目的不是放大风险，是把同样的风险分散到不同价位。
    """
    if batches <= 1 or total_qty <= 0:
        return []
    dist = abs(price - stop)
    if dist <= 0:
        return []

    if direction == 'long':
        # 第1批现价，第2批回踩到止损的一半，第3批更深
        levels = [price, price - dist * 0.35, price - dist * 0.7]
    else:
        levels = [price, price + dist * 0.35, price + dist * 0.7]
    levels = levels[:batches]

    # 权重：第一批轻，后面逐步加重（避免一开始就下重注）
    weights = [0.30, 0.35, 0.35][:batches]
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    out = []
    for i, (px, w) in enumerate(zip(levels, weights), 1):
        out.append({
            '批次': f'第{i}批',
            '价位': round(px, 6),
            '数量': round(total_qty * w, 8),
            '名义价值': round(px * total_qty * w, 2),
            '占比': f'{w * 100:.0f}%',
            '说明': ('现价直接进' if i == 1 else
                     f'等价格回踩到 {px:,.4f} 再进（距现价 {abs(px - price) / price * 100:.2f}%）'),
        })
    # 算加权平均成本
    total_q = sum(x['数量'] for x in out)
    if total_q:
        avg = sum(x['价位'] * x['数量'] for x in out) / total_q
        out.append({'批次': '合计', '价位': round(avg, 6), '数量': round(total_q, 8),
                    '名义价值': round(sum(x['名义价值'] for x in out), 2),
                    '占比': '100%', '说明': '加权平均建仓成本'})
    return out


# ---------------- 方案官（模型只负责选） ----------------

PLANNER_SYSTEM = ('你是交易方案官。你只能从给定的候选里做选择，'
                  '不允许自己发明价格。你的任务是选出最合理的一组，并说明理由。')

PLANNER_PROMPT = '''标的：{symbol}
当前价格：{price}
方向判断：{direction}（来自四位分析师的独立分析，信心 {confidence}）

四位分析师的观点摘要：
{views}

【候选止损位】—— 由程序根据 K 线结构算出，你只能从这些里选一个：
{stops}

【可选盈亏比】{ratios}

请输出 JSON（只输出 JSON，不要 markdown 代码块、不要其他文字）：
{{
  "止损位名称": "必须与上面候选里的名称完全一致",
  "盈亏比": 从上面可选值里选一个数字,
  "选择理由": "为什么选这个止损位。要说明它对应的是什么结构",
  "主要风险": "这个方案最可能怎么失败",
  "什么情况下我错了": "一个可验证的条件",
  "要不要做": "做 或 不做"
}}

硬性要求：
- 只能选上面列出的止损位名称，不能自己编。
- 如果方向是「无法判断」或「中性」，就选 "不做"。
- 不要因为想给方案就硬给。不做也是一个专业结论。'''

PLANNER_SYSTEM_COMPACT = PLANNER_SYSTEM


def _extract_json(t):
    t = (t or '').strip()
    t2 = re.sub(r'^```(?:json)?\s*', '', t)
    t2 = re.sub(r'\s*```$', '', t2)
    for c in (t2, t[t.find('{'):t.rfind('}') + 1] if '{' in t else ''):
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


def call_with_hard_timeout(fn, timeout_sec, *args, **kwargs):
    """硬超时：到点就放弃，不等它。

    为什么需要：requests 的 timeout 只管「两次收到数据之间的间隔」，
    不是总时长。模型慢慢流式返回时，超时永远不会触发 ——
    实测踩过：方案官卡了 10 分钟以上，timeout=300 形同虚设。

    用守护线程 + 队列实现真正的硬超时。超时返回 None，调用方走兜底。
    """
    import queue as _q
    import threading as _th

    box = _q.Queue()

    def worker():
        try:
            box.put(('ok', fn(*args, **kwargs)))
        except Exception as e:                      # noqa: BLE001
            box.put(('err', e))

    _th.Thread(target=worker, daemon=True).start()
    try:
        kind, val = box.get(timeout=timeout_sec)
    except _q.Empty:
        return None
    if kind == 'err':
        raise val
    return val


def ask_planner(symbol, price, analysis, stops, ratios, model=None, api_key=None,
                user_forced=False):
    """让方案官从候选里选一组。"""
    chair = analysis.get('主持人') or {}
    views = []
    for a in analysis.get('分析师') or []:
        views.append(f"- {a.get('_名称')}：{a.get('方向')}"
                     f"（信心 {a.get('信心')}）{str(a.get('核心理由'))[:80]}")
    extra = ''
    if user_forced:
        extra = ('\n\n【重要】用户已经明确指定了方向，不接受「不做」。'
                 '请务必从候选里选一个止损位，给出可执行的参数；'
                 '你对风险的顾虑写在「主要风险」里，但不要用「不做」来回避。')
    prompt = PLANNER_PROMPT.format(
        symbol=symbol, price=f'{price:,.4f}',
        direction=chair.get('方向'), confidence=chair.get('信心'),
        views='\n'.join(views),
        stops=json.dumps([{'名称': s['名称'], '价格': s['价格'],
                           '距离百分比': s['距离百分比'], '说明': s['说明']}
                          for s in stops], ensure_ascii=False, indent=1),
        ratios=', '.join(str(r) for r in ratios)) + extra
    # 两级超时：先主方案官，超时后换更快模型，最后才走程序兜底。
    def run_once(use_model, hard_sec, inner_sec, max_tokens):
        try:
            return call_with_hard_timeout(
                chat, hard_sec, PLANNER_SYSTEM, prompt,
                model=use_model, api_key_override=api_key,
                timeout=inner_sec, temperature=0.2, max_tokens=max_tokens)
        except Exception:
            return None

    out = run_once(model or model_name('planner'), 240, 180, 1000)
    if out is None:
        faster = analyst_models()[0] if analyst_models() else model_name('analyst')
        out = run_once(faster, 150, 120, 700)
    if out is None:
        return None, {}, '（方案官两次超时，已使用程序默认止损位）'
    text, usage, used = out
    return _extract_json(text), usage, used


# ---------------- 组装完整方案 ----------------

def simple_plan(symbol, snap, ind, direction, equity, risk_pct, leverage,
                fee_rate=0.0005, mmr=0.005, stop_name='2倍ATR'):
    """不用模型选 —— 直接用程序默认止损位出一个方案。

    为什么需要：方向不明确时，用户还是想知道「如果做，参数是多少」。
    这时候没必要再多调一次模型（慢且贵），程序自己选最常用的 2 倍 ATR 就行。
    """
    price = snap['标记价']
    atr = ind.get('ATR14') or price * 0.02
    sp = swing_points(snap.get('K线'))
    stops = stop_candidates(price, atr, ind, sp, direction)
    if not stops:
        return None
    names = {x['名称']: x for x in stops}
    chosen = names.get(stop_name) or list(names.values())[len(names) // 2]
    stop = chosen['价格']
    rr = 2.0
    tp = rr_targets(price, stop, direction, (rr,))[0]['止盈价']
    pos = risk.calc_position(equity, risk_pct, price, stop, leverage,
                             direction, fee_rate, mmr, tp)
    verdict, checks = risk.pre_trade_check(
        equity, risk_pct, price, stop, leverage, direction, tp, fee_rate, mmr, 1.5)
    min_check = check_min_notional(symbol, pos['名义价值'])
    batches = []
    if min_check['通过'] and pos['建议数量'] > 0:
        batches = split_entries(price, stop, direction, pos['建议数量'], 3)
    return {
        '方向': '做多' if direction == 'long' else '做空',
        '杠杆': leverage,
        '入场价': price, '止损价': stop, '止盈价': tp,
        '止损依据': chosen, '止盈依据': {'盈亏比': rr},
        '仓位': pos, '纪律检查': {'结论': verdict, '明细': checks},
        '最小下单量': min_check, '分批建仓': batches,
        '候选止损': stops,
        '_程序默认': True,
    }


def directional_lean(analysis):
    """只在证据足够时给弱倾向；不足时明确返回 None，不硬猜。"""
    score = 0.0
    directional_conf = 0.0
    directional_count = 0
    for a in (analysis or {}).get('分析师') or []:
        if a.get('_名称') == '风控官':
            continue
        try:
            conf = max(0.0, min(100.0, float(a.get('信心') or 0)))
        except Exception:
            conf = 0.0
        direction = a.get('方向')
        if direction not in ('偏多', '偏空'):
            continue
        directional_count += 1
        directional_conf += conf
        score += conf if direction == '偏多' else -conf
    ratio = abs(score) / directional_conf if directional_conf else 0.0
    # 至少两位分析师明确投票、总信心足够、分差有意义，才给倾向。
    if directional_count < 2 or directional_conf < 60 or ratio < 0.15:
        return None, None, round(score, 1)
    lean = '偏多' if score > 0 else '偏空'
    strength = '强' if ratio >= 0.5 else '中' if ratio >= 0.25 else '弱'
    return lean, strength, round(score, 1)


def build_plan(symbol, analysis, equity, risk_pct, leverage,
               fee_rate=0.0005, mmr=0.005, min_rr=1.5, model=None, api_key=None,
               user_forced=False):
    """把分析结果 + 结构位 + 仓位计算，组装成一个完整可执行的方案。"""
    snap = market.snapshot(symbol, '自动')
    ind = market.compute_indicators(snap.get('K线'), snap.get('标记价'))
    price = snap['标记价']
    atr = ind.get('ATR14') or price * 0.02

    chair = analysis.get('主持人') or {}
    direction_cn = chair.get('方向')
    if direction_cn == '偏多':
        direction = 'long'
    elif direction_cn == '偏空':
        direction = 'short'
    else:
        # ⚠️ 方向不明确时不能拒绝出方案 ——
        #    用户找这个工具就是为了不自己判断方向，
        #    如果反过来要求他先指定方向，就是循环依赖，违背初衷。
        #    正确做法：把两个方向的参数都算给他，让「要不要做」由他自己决定。
        long_p = simple_plan(symbol, snap, ind, 'long', equity, risk_pct,
                             leverage, fee_rate, mmr)
        short_p = simple_plan(symbol, snap, ind, 'short', equity, risk_pct,
                              leverage, fee_rate, mmr)
        if not long_p and not short_p:
            return {'可执行': False, '标的': market.normalize_symbol(symbol),
                    '杠杆': leverage,
                    '原因': '算不出合理的止损位（波动太小或数据不足）。',
                    '主持人': chair, '快照': snap, '指标': ind}
        return {
            '可执行': True, '双向': True,
            '标的': market.normalize_symbol(symbol), '杠杆': leverage,
            'AI判断': {
                '方向': direction_cn,
                '信心': chair.get('信心'),
                '倾向': chair.get('倾向') or directional_lean(analysis)[0],
                '倾向强度': chair.get('倾向强度') or directional_lean(analysis)[1],
                '说明': (f'四个分析师没能得出一致方向（最终判断「{direction_cn}」，'
                         f'信心 {chair.get("信心")}）。所以下面把**两个方向**的参数都算给你 ——'
                         '**该不该做、做哪个方向，由你决定。**'),
            },
            '做多': long_p, '做空': short_p,
            '主持人': chair, '快照': snap, '指标': ind,
        }

    sp = swing_points(snap.get('K线'))
    stops = stop_candidates(price, atr, ind, sp, direction)
    if not stops:
        return {'可执行': False, '标的': market.normalize_symbol(symbol),
                '杠杆': leverage,
                '原因': '算不出合理的止损位（波动太小或数据不足）。',
                '主持人': chair, '快照': snap, '指标': ind}

    ratios = [1.5, 2.0, 2.5, 3.0]
    picked, usage, used_model = ask_planner(symbol, price, analysis, stops, ratios,
                                            model, api_key,
                                            user_forced=user_forced)
    planner_timeout = False
    if not picked:
        # 方案官超时/解析失败 → 不放弃，用程序默认止损位继续
        planner_timeout = True
        picked = {}

    # 校验：模型选的名称必须真实存在于候选里（防止它编造）
    names = {s['名称']: s for s in stops}
    raw_pick = picked.get('止损位名称')
    chosen = names.get(str(raw_pick or '').strip())

    fallback_note = None
    if chosen is None:
        # ⚠️ 这里原来是直接返回失败，实测发现太脆：
        # 模型可能因为超时/截断/字段名写错而选不出来，但方案本身不该因此作废。
        # 改成用确定性默认值兜底：优先 2 倍 ATR（波动率止损里最常用的），
        # 并明确标注这是程序给的、不是模型选的。
        default = names.get('2倍ATR') or list(names.values())[len(names) // 2]
        chosen = default
        if planner_timeout:
            why = '方案官超时或输出无法解析'
        elif raw_pick in (None, '', 'None'):
            why = '模型没有返回有效的止损位名称'
        else:
            why = f'模型选的「{raw_pick}」不在候选里'
        fallback_note = (f'⚠️ {why}，已回退到程序默认的「{chosen["名称"]}」。'
                         '这一条不是模型的选择，请自行核对是否合适。')
        if not isinstance(picked, dict):
            picked = {}
        picked = dict(picked)
        picked['选择理由'] = (picked.get('选择理由')
                          or '（模型未给出理由，此为程序默认值）')

    if str(picked.get('要不要做', '')).strip() == '不做':
        return {'可执行': False, '标的': market.normalize_symbol(symbol),
                '杠杆': leverage,
                '原因': f"方案官结论是「不做」。理由：{picked.get('选择理由', '')}",
                '主持人': chair, '快照': snap, '指标': ind,
                '候选止损': stops, '方案官': picked}

    # 盈亏比也必须在候选里
    try:
        rr = float(picked.get('盈亏比'))
    except (TypeError, ValueError):
        rr = None
    if rr not in ratios:
        rr = min(ratios, key=lambda x: abs(x - (rr or 2.0)))

    stop = chosen['价格']
    targets = rr_targets(price, stop, direction, (rr,))
    tp = targets[0]['止盈价']

    # 用现成的风控代码算仓位（这一段是纯公式，不经过模型）
    pos = risk.calc_position(equity, risk_pct, price, stop, leverage,
                             direction, fee_rate, mmr, tp)
    verdict, checks = risk.pre_trade_check(
        equity, risk_pct, price, stop, leverage, direction, tp,
        fee_rate, mmr, min_rr)

    # 最小下单量检查（本金小时很关键）
    min_check = check_min_notional(symbol, pos['名义价值'])

    # 分批建仓建议（从实战笔记学到的：不要一次满仓）
    batches = []
    if min_check['通过'] and pos['建议数量'] > 0:
        batches = split_entries(price, stop, direction, pos['建议数量'], batches=3)

    return {
        '可执行': True,
        '标的': market.normalize_symbol(symbol),
        '最小下单量': min_check,
        '分批建仓': batches,
        '方向': '做多' if direction == 'long' else '做空',
        '杠杆': leverage,
        '倾向': chair.get('倾向') or directional_lean(analysis)[0],
        '倾向强度': chair.get('倾向强度') or directional_lean(analysis)[1],
        '方向依据': chair,
        '分析师观点': analysis.get('分析师'),
        '入场价': price,
        '止损价': stop,
        '止损依据': chosen,
        '止盈价': tp,
        '止盈依据': targets[0],
        '仓位': pos,
        '纪律检查': {'结论': verdict, '明细': checks},
        '方案官': picked,
        '方案官模型': used_model,
        '兜底提示': fallback_note,
        '候选止损': stops,
        '快照': snap,
        '指标': ind,
        '可执行条件': picked.get('什么情况下我错了'),
        '主要风险': picked.get('主要风险'),
    }


def format_plan(plan):
    """把方案格式化成给人看的中文文本。"""
    if not plan.get('可执行'):
        return f"❌ 没有生成可执行方案。\n\n原因：{plan.get('原因')}"
    p = plan['仓位']
    L = []
    L.append(f"【{plan['标的']}　{plan['方向']}】")
    L.append('')
    L.append(f"  入场价      {plan['入场价']:,.4f}")
    L.append(f"  止损价      {plan['止损价']:,.4f}"
             f"   （{plan['止损依据']['名称']}，距入场 {plan['止损依据']['距离百分比']:.2f}%）")
    L.append(f"  止盈价      {plan['止盈价']:,.4f}"
             f"   （盈亏比 {plan['止盈依据']['盈亏比']:g}:1）")
    L.append('')
    L.append(f"  建议数量    {p['建议数量']:.6f}")
    L.append(f"  名义价值    {p['名义价值']:,.2f} USDT")
    L.append(f"  占用保证金  {p['占用保证金']:,.2f} USDT"
             f"（本金 {p['保证金占本金比例']:.1f}%）")
    L.append(f"  止损时亏损  {p['止损时实际亏损']:,.2f} USDT"
             f"（本金 {p['止损时实际亏损比例']:.2f}%）")
    L.append(f"  达到止盈赚  {p['达到目标的盈利']:,.2f} USDT")
    L.append(f"  预估手续费  {p['预估手续费']:,.2f} USDT")
    L.append('')
    L.append(f"  估算爆仓价  {p['爆仓价']:,.4f}"
             f"　{'✅ 在止损之外' if not p['爆仓先于止损'] else '🚨 比止损更近，危险'}")
    L.append(f"  保本价      {p['保本价']:,.4f}")
    L.append('')
    L.append(f"  纪律检查：{plan['纪律检查']['结论']}")
    for c in plan['纪律检查']['明细']:
        icon = {'通过': '✅', '警告': '⚠️', '拒绝': '❌', '提示': '💡'}.get(c['级别'], '•')
        L.append(f"    {icon} {c['项目']}：{c['说明']}")
    L.append('')
    if plan.get('兜底提示'):
        L.append('')
        L.append(f"  {plan['兜底提示']}")
    L.append('')
    L.append(f"  止损位为什么选它：{plan['止损依据']['说明']}")
    L.append(f"  方案官理由：{plan['方案官'].get('选择理由')}")
    L.append(f"  主要风险：{plan.get('主要风险')}")
    L.append(f"  ⚠️ 什么情况下这个方案失效：{plan.get('可执行条件')}")
    return '\n'.join(L)