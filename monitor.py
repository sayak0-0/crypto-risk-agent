# -*- coding: utf-8 -*-
"""市场监控：规则引擎 + 冷却去重。

设计要点：
1. 规则写在 JSON 里，随时加删，不用改代码。
2. 触发过的警报会「上锁」，等条件恢复正常后才重新上膛（避免同一个警报刷屏）。
3. 即使条件一直不恢复，超过冷却时间也会再提醒一次（持续异常需要持续关注）。
"""
import json
import os
import time
from datetime import datetime, timedelta

from common import DATA_DIR, fmt_usdt

RULES_PATH = os.path.join(DATA_DIR, '我的监控规则.json')
POSITIONS_PATH = os.path.join(DATA_DIR, '我的持仓.json')
STATE_PATH = os.path.join(DATA_DIR, '监控状态.json')
ALERT_LOG = os.path.join(DATA_DIR, '警报历史.jsonl')

# 规则类型：(名称, 阈值单位提示, 默认阈值)
RULE_TYPES = {
    '价格上破': ('价格涨到该值以上时提醒', 0.0),
    '价格下破': ('价格跌到该值以下时提醒', 0.0),
    '24h涨跌幅超过': ('24 小时涨跌幅绝对值超过该百分比（%）', 5.0),
    '资金费率超过': ('资金费率绝对值超过该百分比（%，按 8 小时一次）', 0.05),
    '多空比高于': ('账户多空比高于该值时提醒（散户一边倒）', 1.8),
    '多空比低于': ('账户多空比低于该值时提醒', 0.6),
    'ATR超过': ('4 小时 ATR 占价格百分比超过该值（%）', 3.0),
    '距爆仓不足': ('价格距离爆仓价不足该百分比（%）时紧急提醒', 5.0),
    '距止损不足': ('价格距离止损价不足该百分比（%）时提醒', 1.0),
}

LEVELS = ['提示', '警告', '紧急']
LEVEL_ICON = {'提示': 'ℹ️', '警告': '⚠️', '紧急': '🚨'}

行情类 = ['价格上破', '价格下破', '24h涨跌幅超过', '资金费率超过',
         '多空比高于', '多空比低于', 'ATR超过']
持仓类 = ['距爆仓不足', '距止损不足']


# ---------------- 规则读写 ----------------

def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_rules():
    rules = _read_json(RULES_PATH, [])
    return rules if isinstance(rules, list) else []


def save_rules(rules):
    return _write_json(RULES_PATH, rules)


def add_rule(symbol, rule_type, threshold, level='警告', cooldown=30, note=''):
    """加一条监控规则。"""
    if rule_type not in RULE_TYPES:
        raise ValueError(f'不支持的规则类型：{rule_type}')
    if level not in LEVELS:
        raise ValueError(f'等级只能是 {LEVELS}')
    rules = load_rules()
    rules.append({
        '编号': f'R{len(rules) + 1:03d}',
        '币种': str(symbol).upper().strip(),
        '类型': rule_type,
        '阈值': float(threshold),
        '等级': level,
        '冷却分钟': int(cooldown),
        '备注': note,
        '启用': True,
    })
    # 重新编号，避免删过之后重号
    for i, r in enumerate(rules, 1):
        r['编号'] = f'R{i:03d}'
    save_rules(rules)
    return rules


def delete_rule(rule_id):
    rules = [r for r in load_rules() if r.get('编号') != rule_id]
    for i, r in enumerate(rules, 1):
        r['编号'] = f'R{i:03d}'
    save_rules(rules)
    return rules


def toggle_rule(rule_id):
    rules = load_rules()
    for r in rules:
        if r.get('编号') == rule_id:
            r['启用'] = not r.get('启用', True)
    save_rules(rules)
    return rules


# ---------------- 持仓 ----------------

def load_positions():
    pos = _read_json(POSITIONS_PATH, [])
    return pos if isinstance(pos, list) else []


def save_positions(positions):
    return _write_json(POSITIONS_PATH, positions)


def upsert_position(symbol, direction, entry, qty, leverage, stop=0.0,
                    liq=0.0, mmr=0.005):
    """记录一笔持仓，供「距爆仓 / 距止损」两类规则使用。"""
    import risk as risk_mod
    symbol = str(symbol).upper().strip()
    direction_en = 'long' if direction in ('多', 'long') else 'short'
    if liq and liq > 0:
        liq_price = float(liq)
    else:
        liq_price = risk_mod.liquidation_price(entry, leverage, direction_en, mmr)

    positions = [p for p in load_positions() if p.get('币种') != symbol]
    positions.append({
        '币种': symbol,
        '方向': '多' if direction_en == 'long' else '空',
        '开仓价': float(entry),
        '数量': float(qty),
        '杠杆': float(leverage),
        '止损价': float(stop or 0),
        '爆仓价': float(liq_price),
    })
    save_positions(positions)
    return positions


def remove_position(symbol):
    positions = [p for p in load_positions() if p.get('币种') != symbol]
    save_positions(positions)
    return positions


def position_health(pos, price):
    """算一笔持仓的健康度：距爆仓、距止损、浮动盈亏。"""
    entry = float(pos['开仓价'])
    qty = float(pos.get('数量') or 0)
    is_long = pos.get('方向', '多') == '多'
    sign = 1 if is_long else -1
    out = {'币种': pos['币种'], '方向': pos.get('方向'), '当前价': price,
           '浮动盈亏': (price - entry) * qty * sign}

    liq = float(pos.get('爆仓价') or 0)
    if liq > 0:
        # 距离爆仓还有多少百分比（正数=还有空间，负数=已经爆了）
        out['距爆仓百分比'] = ((price - liq) / price * 100) if is_long \
            else ((liq - price) / price * 100)

    stop = float(pos.get('止损价') or 0)
    if stop > 0:
        out['距止损百分比'] = ((price - stop) / price * 100) if is_long \
            else ((stop - price) / price * 100)
    return out


# ---------------- 仓位体检 ----------------
# 专门抓那些「看起来没事、其实一直在漏钱」的仓位，比如长期留着的零碎仓位。

DUST_NOTIONAL = 100.0      # 名义价值低于这个数，算零碎仓位（USDT）


def position_audit(pos, price, funding_rate=None):
    """给一笔持仓做体检，返回发现列表。

    每项是 {'级别', '项目', '说明'}。级别：提示 / 警告 / 紧急。
    """
    out = []
    entry = float(pos.get('开仓价') or 0)
    qty = float(pos.get('数量') or 0)
    lev = float(pos.get('杠杆') or 1)
    is_long = pos.get('方向', '多') == '多'
    sign = 1 if is_long else -1

    if entry <= 0 or qty <= 0 or not price:
        return out

    notional = qty * price
    margin = notional / lev if lev else 0
    upl = (price - entry) * qty * sign
    roi = (upl / margin * 100) if margin else 0
    price_move = (price - entry) / entry * 100 * sign

    # 1. 零碎仓位（尘埃仓）
    if notional < DUST_NOTIONAL:
        out.append({
            '级别': '提示', '项目': '仓位规模',
            '说明': (f'名义价值只有 {notional:,.2f} USDT，属于零碎仓位。'
                     f'保证金 {margin:,.2f} USDT。这类仓位通常赚不到有意义的钱，'
                     '但会一直占用你的注意力。'),
        })

    # 2. 百分比好看但钱不多 —— 杠杆最容易骗人的地方
    if roi > 100 and abs(upl) < DUST_NOTIONAL:
        out.append({
            '级别': '提示', '项目': '收益率虚高',
            '说明': (f'收益率 {roi:+.0f}% 看着很爽，但绝对金额只有 {upl:+,.2f} USDT。'
                     f'价格实际动了 {price_move:+.2f}%。'
                     f'记住：决定你赚多少的是仓位大小，不是杠杆倍数 —— '
                     f'杠杆只决定你多快爆仓。'),
        })

    # 3. 资金费拖累
    if funding_rate is not None and abs(funding_rate) > 0:
        cost_per_day = notional * funding_rate * 3
        cost_per_year = cost_per_day * 365
        pay = (is_long and funding_rate > 0) or ((not is_long) and funding_rate < 0)
        if pay and abs(cost_per_year) > 0:
            if upl > 0 and cost_per_year > abs(upl) * 0.1:
                out.append({
                    '级别': '警告', '项目': '资金费',
                    '说明': (f'当前费率 {funding_rate * 100:+.4f}%/8h，'
                             f'这个方向的仓位要付费。按现在仓位算：'
                             f'每天 {cost_per_day:,.4f}、一年 {cost_per_year:,.4f} USDT。'
                             f'一年资金费相当于你当前浮盈的 '
                             f'{cost_per_year / abs(upl) * 100:.0f}%。'),
                })
            elif cost_per_year > margin:
                out.append({
                    '级别': '警告', '项目': '资金费',
                    '说明': (f'一年资金费约 {cost_per_year:,.4f} USDT，'
                             f'已经超过你的保证金 {margin:,.2f} USDT 了。'
                             '长期拿着的话，光资金费就能把你磨掉。'),
                })
        elif not pay:
            out.append({
                '级别': '提示', '项目': '资金费',
                '说明': (f'当前费率对你这个方向是有利的，'
                         f'每天约收 {abs(cost_per_day):,.4f} USDT。'),
            })

    # 4. 爆仓距离
    liq = float(pos.get('爆仓价') or 0)
    if liq > 0:
        dist = ((price - liq) / price * 100) if is_long else ((liq - price) / price * 100)
        if dist <= 0:
            out.append({'级别': '紧急', '项目': '爆仓', '说明': '已经触及估算爆仓价。'})
        elif dist < 10:
            out.append({
                '级别': '警告', '项目': '爆仓距离',
                '说明': (f'距估算爆仓只剩 {dist:.2f}%。'
                         f'{lev:.0f}x 杠杆下，价格反向 {100 / lev:.2f}% 就到临界点。'),
            })
        else:
            out.append({
                '级别': '提示', '项目': '爆仓距离',
                '说明': f'距估算爆仓 {dist:.2f}%，短期没有强平风险。',
            })
    return out


# ---------------- 规则判定 ----------------

def evaluate(rule, snap=None, ind=None, pos_health=None):
    """判断一条规则是否触发。

    返回 (是否触发, 说明文字)；不触发或数据缺失返回 (False, None)。
    """
    t = rule.get('类型')
    th = float(rule.get('阈值', 0))
    sym = rule.get('币种', '')

    if t in 持仓类:
        if not pos_health:
            return False, None
        h = pos_health
        if t == '距爆仓不足':
            d = h.get('距爆仓百分比')
            if d is None:
                return False, None
            liq = h.get('_liq') or 0
            if d <= 0:
                return True, (f'🚨 {sym} 已经触及估算爆仓价！现价 {h["当前价"]:.4f}，'
                              f'估算爆仓价 {liq:.4f}。马上处理，不要等。')
            if d <= th:
                return True, (f'{sym} 距离估算爆仓只剩 {d:.2f}%（你设的警戒线 {th}%）。'
                              f'现价 {h["当前价"]:.4f}，爆仓价 {liq:.4f}，'
                              f'浮盈亏 {fmt_usdt(h["浮动盈亏"])} USDT。')
            return False, None
        if t == '距止损不足':
            d = h.get('距止损百分比')
            if d is None:
                return False, None
            if d <= 0:
                return True, f'{sym} 已经穿过你的止损价了，该执行计划了。'
            if d <= th:
                return True, (f'{sym} 距离止损价只剩 {d:.2f}%（阈值 {th}%）。'
                              f'浮盈亏 {fmt_usdt(h["浮动盈亏"])} USDT。')
            return False, None
        return False, None

    if not snap:
        return False, None
    price = snap.get('标记价')
    if not price:
        return False, None

    if t == '价格上破':
        if price >= th:
            return True, f'{sym} 现价 {price:,.4f}，已上破 {th:,.4f}。'
    elif t == '价格下破':
        if price <= th:
            return True, f'{sym} 现价 {price:,.4f}，已下破 {th:,.4f}。'
    elif t == '24h涨跌幅超过':
        chg = snap.get('24h涨跌幅')
        if chg is not None and abs(chg) >= th:
            return True, f'{sym} 24 小时涨跌 {chg:+.2f}%，超过你设的 {th}%。'
    elif t == '资金费率超过':
        fr = snap.get('资金费率')
        if fr is not None and abs(fr * 100) >= th:
            side = '多头拥挤（做多要付钱）' if fr > 0 else '空头拥挤（做空要付钱）'
            return True, (f'{sym} 资金费率 {fr * 100:+.4f}%（8 小时），{side}。'
                          '极端费率往往出现在阶段性的多空失衡处。')
    elif t == '多空比高于':
        lr = snap.get('多空比')
        if lr is not None and lr >= th:
            return True, (f'{sym} 账户多空比 {lr:.2f}，散户做多明显一边倒（阈值 {th}）。'
                          '散户一致性高的时候，往往是被反向收割的一方。')
    elif t == '多空比低于':
        lr = snap.get('多空比')
        if lr is not None and lr <= th:
            return True, (f'{sym} 账户多空比 {lr:.2f}，散户做空明显一边倒（阈值 {th}）。')
    elif t == 'ATR超过':
        if ind and ind.get('ATR14百分比') is not None and ind['ATR14百分比'] >= th:
            return True, (f'{sym} 4 小时 ATR 已到 {ind["ATR14百分比"]:.2f}%（阈值 {th}%），'
                          '波动在放大。这种时候原来的止损距离很可能不够用了。')
    return False, None


# ---------------- 状态与冷却 ----------------

def load_state():
    s = _read_json(STATE_PATH, {})
    return s if isinstance(s, dict) else {}


def save_state(state):
    return _write_json(STATE_PATH, state)


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def decide(rule, triggered, state, now):
    """结合「上锁」和「冷却」决定要不要真的报警。

    逻辑：
    - 条件从「假」变「真」→ 立刻报，然后上锁
    - 条件还是「真」但已过冷却时间 → 再报一次（持续异常要持续关注）
    - 条件变回「假」→ 解锁上膛，下次再触发就能马上报
    """
    key = rule.get('编号') or f"{rule.get('币种')}-{rule.get('类型')}-{rule.get('阈值')}"
    st = state.get(key) or {}
    armed = st.get('armed', True)
    last = _parse(st.get('last_fired', ''))

    if not triggered:
        if not armed or last:
            state[key] = {'armed': True, 'last_fired': ''}
        return False

    cooldown = max(1, int(rule.get('冷却分钟', 30)))
    if armed:
        state[key] = {'armed': False, 'last_fired': now.isoformat(timespec='seconds')}
        return True
    if last and now - last >= timedelta(minutes=cooldown):
        state[key] = {'armed': False, 'last_fired': now.isoformat(timespec='seconds')}
        return True
    return False


# ---------------- 主流程 ----------------

def collect_symbols(rules=None, positions=None):
    rules = rules if rules is not None else load_rules()
    positions = positions if positions is not None else load_positions()
    syms = {r.get('币种') for r in rules if r.get('启用', True) and r.get('币种')}
    syms |= {p.get('币种') for p in positions if p.get('币种')}
    return sorted(syms)


def run_once(exchange='自动', symbols=None, persist=True, now=None):
    """跑一轮监控。返回 (警报列表, 错误列表, 快照字典)。"""
    import market as market_mod

    now = now or datetime.now()
    rules = [r for r in load_rules() if r.get('启用', True)]
    positions = load_positions()
    symbols = symbols or collect_symbols(rules, positions)

    snapshots, indicators, errors = {}, {}, []
    for sym in symbols:
        try:
            snap = market_mod.snapshot(sym, exchange)
            snapshots[sym] = snap
            indicators[sym] = market_mod.compute_indicators(
                snap.get('K线'), snap.get('标记价'))
        except Exception as e:
            errors.append(f'{sym} 行情获取失败：{e}')
            continue

    health = {}
    for p in positions:
        sym = p.get('币种')
        price = (snapshots.get(sym) or {}).get('标记价')
        if price and p.get('数量'):
            h = position_health(p, price)
            h['_liq'] = p.get('爆仓价') or 0
            health[sym] = h

    state = load_state()
    alerts = []
    for rule in rules:
        sym = rule.get('币种')
        pos_h = health.get(sym) if rule.get('类型') in 持仓类 else None
        triggered, text = evaluate(rule, snapshots.get(sym), indicators.get(sym), pos_h)
        if decide(rule, triggered, state, now):
            alerts.append({
                '时间': now.strftime('%Y-%m-%d %H:%M:%S'),
                '等级': rule.get('等级', '警告'),
                '币种': sym,
                '规则': rule.get('类型'),
                '说明': text,
                '备注': rule.get('备注', ''),
            })

    if persist:
        save_state(state)
        if alerts:
            append_alerts(alerts)
    return alerts, errors, snapshots


def append_alerts(alerts):
    """把警报追加到历史文件（一行一条 JSON）。"""
    os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
    with open(ALERT_LOG, 'a', encoding='utf-8') as f:
        for a in alerts:
            f.write(json.dumps(a, ensure_ascii=False) + '\n')
    return ALERT_LOG


def load_alerts(limit=100):
    """读最近的警报历史（新的在前）。"""
    if not os.path.exists(ALERT_LOG):
        return []
    out = []
    with open(ALERT_LOG, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-limit:][::-1]


def clear_state():
    """清空冷却状态（想立刻重新测试规则时用）。"""
    return save_state({})