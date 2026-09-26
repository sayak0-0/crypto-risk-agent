# -*- coding: utf-8 -*-
"""决策记录与 4h/24h/72h 结果验证。"""
import json, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
import market
from common import DATA_DIR

PATH = Path(DATA_DIR) / '决策记录.json'
HORIZONS = (4, 24, 72)


def _now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def _load():
    try: return json.loads(PATH.read_text(encoding='utf-8'))
    except Exception: return []
def _save(rows):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(PATH)


def record(symbol, analysis, plan=None, regime=None):
    chair = (analysis or {}).get('主持人') or {}
    direction = (plan or {}).get('方向') or chair.get('方向')
    price = (plan or {}).get('入场价') or (analysis or {}).get('当时价格')
    if not price:
        try: price = market.snapshot(symbol, '自动')['标记价']
        except Exception: price = None
    row = {
        'id': uuid.uuid4().hex[:12], '记录时间': _now(), '币种': market.normalize_symbol(symbol),
        '方向': direction, '信心': chair.get('信心'), '基准价': price,
        '市场状态': (regime or {}).get('状态'), '状态信心': (regime or {}).get('信心'),
        '结果': {str(h): None for h in HORIZONS},
        '是否正确': {str(h): None for h in HORIZONS},
    }
    rows = _load(); rows.append(row); _save(rows); return row


def _due(row, hours):
    dt = datetime.strptime(row['记录时间'], '%Y-%m-%d %H:%M:%S')
    return datetime.now() >= dt + timedelta(hours=hours)


def update_outcomes():
    rows = _load(); updated = 0
    for row in rows:
        base = float(row.get('基准价') or 0)
        direction = row.get('方向')
        if not base or direction not in ('偏多', '偏空', '做多', '做空'):
            continue
        for h in HORIZONS:
            key = str(h)
            if row.get('结果', {}).get(key) is not None or not _due(row, h):
                continue
            try:
                price = float(market.snapshot(row['币种'], '自动')['标记价'])
            except Exception:
                continue
            ret = (price / base - 1) * 100
            ok = ret > 0 if direction in ('偏多', '做多') else ret < 0
            row.setdefault('结果', {})[key] = round(ret, 4)
            row.setdefault('是否正确', {})[key] = ok
            updated += 1
    if updated: _save(rows)
    return rows, updated


def accuracy_by_regime(rows=None, horizon=24):
    rows = _load() if rows is None else rows
    groups = {}
    for r in rows:
        state = r.get('市场状态') or '未知'
        ok = (r.get('是否正确') or {}).get(str(horizon))
        if ok is None:
            continue
        g = groups.setdefault(state, {'样本': 0, '正确': 0})
        g['样本'] += 1
        g['正确'] += 1 if ok else 0
    for g in groups.values():
        g['可评价'] = g['样本'] >= 30
        g['准确率'] = round(g['正确'] / g['样本'] * 100, 1) if g['样本'] >= 30 else None
        if g['样本'] < 30: g['说明'] = '该状态样本不足，暂不评价'
    return groups


def summary(rows=None):
    rows = _load() if rows is None else rows
    out = {}
    for h in HORIZONS:
        vals = [r.get('是否正确', {}).get(str(h)) for r in rows]
        vals = [x for x in vals if x is not None]
        n = len(vals)
        out[str(h)] = {'样本': n, '正确': sum(vals), '可评价': n >= 100,
                       '准确率': round(sum(vals)/n*100, 1) if n >= 100 else None,
                       '说明': '样本不足，暂不评价' if n < 100 else '已满足最低样本门槛'}
    return out