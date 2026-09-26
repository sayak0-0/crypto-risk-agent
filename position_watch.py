# -*- coding: utf-8 -*-
"""开仓观察：用户确认已开仓后，每24小时更新一次市场盈亏。"""
import argparse
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import market
import decision_log
import exchange_sync
from common import DATA_DIR

WATCH_PATH = Path(DATA_DIR) / '开仓观察.json'
PUBLIC_PATH = Path(__file__).resolve().parent / 'public' / 'positions.json'
DUE_HOURS = 24


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _load():
    if not WATCH_PATH.exists():
        return []
    try:
        data = json.loads(WATCH_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows):
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(WATCH_PATH)
    export_public(rows)


def _f(v, default=0.0):
    try: return float(v)
    except Exception: return default


def reconcile_plan(plan):
    """用币安实际持仓和挂单覆盖方案假设值。"""
    symbol = market.normalize_symbol(plan.get('标的'))
    want = '多' if plan.get('方向') == '做多' else '空'
    positions = exchange_sync.binance_positions()
    pos = next((x for x in positions if x.get('币种') == symbol), None)
    if not pos:
        return {'对账成功': False, '原因': f'币安未检测到 {symbol} 的实际持仓'}
    if pos.get('方向') != want:
        return {'对账成功': False, '原因': f'币安实际方向是{pos.get("方向")}，方案方向是{want}'}
    orders = exchange_sync.binance_open_orders(symbol)
    stop = target = None
    for o in orders:
        typ = str(o.get('类型') or '')
        px = _f(o.get('触发价') or o.get('价格'))
        if px <= 0: continue
        if typ.startswith('STOP') or typ == 'TRAILING_STOP_MARKET': stop = px
        if typ.startswith('TAKE_PROFIT'): target = px
    return {
        '对账成功': True, '币种': symbol, '方向中文': '做多' if want == '多' else '做空',
        '方向': 'long' if want == '多' else 'short',
        '入场价': _f(pos.get('开仓价')), '数量': abs(_f(pos.get('数量'))),
        '杠杆': _f(pos.get('杠杆'), 1), '保证金': _f(pos.get('保证金')),
        '止损价': stop or _f(plan.get('止损价')), '止盈价': target or _f(plan.get('止盈价')),
        '交易所止损单': bool(stop), '交易所止盈单': bool(target),
        '挂单警告': ('缺少交易所止损单' if not stop else '') + (('；缺少交易所止盈单' if not target else '')),
        '对账时间': _now(),
    }


def add_plan(plan):
    if not plan or not plan.get('可执行') or plan.get('双向'):
        raise ValueError('只能观察已经生成的可执行单方向方案')
    actual = reconcile_plan(plan)
    if not actual.get('对账成功'):
        raise ValueError(actual.get('原因') or '无法与币安实际持仓对账')
    symbol = actual['币种']
    direction = actual['方向']
    record = {
        'id': uuid.uuid4().hex[:12],
        '币种': symbol,
        '方向': direction,
        '方向中文': actual['方向中文'],
        '入场价': actual['入场价'],
        '止损价': actual['止损价'],
        '止盈价': actual['止盈价'],
        '杠杆': actual['杠杆'],
        '数量': actual['数量'],
        '保证金': actual['保证金'],
        '持仓校验': '已与币安对账',
        '交易所止损单': actual['交易所止损单'],
        '交易所止盈单': actual['交易所止盈单'],
        '挂单警告': actual['挂单警告'],
        '最后对账': actual['对账时间'],
        '开仓时间': _now(),
        '最后检查': '',
        '最后日检': '',
        '下次检查': _now(),
        '当前价': None,
        '浮动盈亏': None,
        '保证金收益率': None,
        '状态': '等待首检',
        '行情状态': '等待',
        '已平仓': False,
    }
    rows = _load()
    # 同一标的同方向已有活动记录时不重复添加。
    for x in rows:
        if (not x.get('已平仓') and x.get('币种') == symbol
                and x.get('方向') == direction):
            return x
    rows.append(record)
    observe(record, force_daily=True)
    _save(rows)
    return record


def _daily_due(record, due_hours=DUE_HOURS):
    if not record.get('最后日检'):
        return True
    try:
        last = datetime.strptime(record['最后日检'], '%Y-%m-%d %H:%M:%S')
        return datetime.now() - last >= timedelta(hours=due_hours)
    except Exception:
        return True


def _reconcile_due(record, minutes=15):
    if not record.get('最后对账'): return True
    try:
        return datetime.now() - datetime.strptime(record['最后对账'], '%Y-%m-%d %H:%M:%S') >= timedelta(minutes=minutes)
    except Exception: return True


def reconcile_record(record):
    if not _reconcile_due(record): return True
    try:
        positions = exchange_sync.binance_positions()
    except Exception as e:
        record['对账状态'] = f'查询失败:{type(e).__name__}'
        return True
    pos = next((x for x in positions if x.get('币种') == record.get('币种')), None)
    if not pos:
        record.update({'已平仓': True, '状态': '币安已无持仓', '行情状态': '暂停'})
        return False
    record.update({'数量': abs(_f(pos.get('数量'))), '入场价': _f(pos.get('开仓价')),
                   '杠杆': _f(pos.get('杠杆'), 1), '保证金': _f(pos.get('保证金')),
                   '最后对账': _now(), '对账状态': '已对账'})
    return True


def observe(record, force_daily=False):
    if record.get('已平仓'):
        return record
    if not reconcile_record(record):
        return record
    try:
        snap = market.snapshot(record['币种'], '自动')
        price = float(snap['标记价'])
    except Exception as e:
        # 单次网络失败不能覆盖上次成功的盈亏，只标记行情暂时不可用。
        record['行情状态'] = '暂不可用'
        record['行情错误'] = type(e).__name__
        record['最后检查'] = _now()
        if record.get('当前价') is None:
            record['状态'] = '等待行情'
        return record
    sign = 1 if record.get('方向') == 'long' else -1
    qty = float(record.get('数量') or 0)
    entry = float(record.get('入场价') or 0)
    margin = float(record.get('保证金') or 0)
    pnl = (price - entry) * qty * sign
    roi = pnl / margin * 100 if margin else 0.0
    stop, target = float(record.get('止损价') or 0), float(record.get('止盈价') or 0)
    if sign > 0 and price <= stop:
        state = '已触发止损'
    elif sign < 0 and price >= stop:
        state = '已触发止损'
    elif sign > 0 and price >= target:
        state = '已触发止盈'
    elif sign < 0 and price <= target:
        state = '已触发止盈'
    elif pnl > 0:
        state = '盈利'
    elif pnl < 0:
        state = '亏损'
    else:
        state = '持平'
    record.update({
        '当前价': price,
        '浮动盈亏': round(pnl, 6),
        '保证金收益率': round(roi, 4),
        '状态': state,
        '最后检查': _now(),
        '行情状态': '正常',
    })
    if force_daily or _daily_due(record):
        record['最后日检'] = _now()
        record['下次检查'] = (datetime.now() + timedelta(hours=DUE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
    return record


def update_all(force=False, due_hours=DUE_HOURS):
    rows = _load()
    for row in rows:
        force_daily = force or _daily_due(row, due_hours)
        observe(row, force_daily=force_daily)
    _save(rows)
    try:
        decision_log.update_outcomes()
    except Exception:
        pass
    return rows


def close_watch(watch_id):
    rows = _load()
    for row in rows:
        if row.get('id') == watch_id:
            row['已平仓'] = True
            row['状态'] = '已结束观察'
            break
    _save(rows)
    return rows


def export_public(rows=None):
    rows = _load() if rows is None else rows
    active = [x for x in rows if not x.get('已平仓')]
    active.sort(key=lambda x: x.get('开仓时间', ''), reverse=True)
    PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_PATH.write_text(json.dumps(active[:20], ensure_ascii=False, indent=2),
                           encoding='utf-8')
    return active[:20]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--interval', type=int, default=300, help='行情刷新频率，默认5分钟')
    ap.add_argument('--due-hours', type=int, default=24)
    args = ap.parse_args()
    if args.once:
        rows = update_all(force=args.force, due_hours=args.due_hours)
        print(f'更新 {len(rows)} 条，活动 {len(export_public(rows))} 条')
        return
    while True:
        try:
            rows = update_all(force=args.force, due_hours=args.due_hours)
            print(f'[{_now()}] 活动观察 {len(export_public(rows))} 条', flush=True)
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f'[{_now()}] 更新失败 {type(e).__name__}: {e}', flush=True)
        time.sleep(max(60, args.interval))


if __name__ == '__main__':
    main()