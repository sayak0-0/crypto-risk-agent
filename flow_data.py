# -*- coding: utf-8 -*-
"""HyperData Terminal 适配器：订单流、爆仓、盘口和危险仓位。"""
import os, requests, json, time
from pathlib import Path
from common import DATA_DIR

BASE = os.getenv('HYPERDATA_URL', 'http://127.0.0.1:8420')

def _get(path, timeout=8):
    r = requests.get(BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()

def context(symbol):
    coin = str(symbol or 'BTC').upper().replace('USDT','')
    out = {}
    coverage = {}
    liq_confirmed = liq_heuristic = 0
    try:
        m = _get(f'/v1/market/{coin}')
        out.update({'标记价': m.get('mark_price'), '资金费率': m.get('funding_rate'),
                    '持仓量': m.get('open_interest'), '24h涨跌%': (m.get('price_change_24h_pct') or 0)*100,
                    '基差%': m.get('premium_pct')})
    except Exception as e: out['行情错误'] = type(e).__name__
    try:
        o = _get(f'/v1/orderflow/{coin}')
        out.update({'订单流信号': o.get('aggregate_signal'), 'CVD': o.get('cumulative_cvd'),
                    '主动买卖覆盖': o.get('venues_contributing')})
        coverage = o.get('venue_coverage') or {}
    except Exception as e: out['订单流错误'] = type(e).__name__
    try:
        l = _get('/v1/liquidations/stats')
        liq_confirmed = int(l.get('confirmed_count') or 0)
        liq_heuristic = int(l.get('heuristic_count') or 0)
        out.update({'爆仓总量U': l.get('total_volume_usd'), '爆仓笔数': l.get('total_count'),
                    '确认爆仓数': liq_confirmed, '推测爆仓数': liq_heuristic,
                    '多头爆仓U': l.get('long_volume_usd'), '空头爆仓U': l.get('short_volume_usd')})
    except Exception as e: out['爆仓错误'] = type(e).__name__
    try:
        b = _get(f'/v1/orderbook/{coin}')
        out.update({'盘口失衡': b.get('imbalance'), '买卖价差': b.get('spread')})
    except Exception as e: out['盘口错误'] = type(e).__name__
    # 合并本地 Bybit 成交流。
    try:
        bf = json.loads((Path(DATA_DIR) / 'bybit_orderflow.json').read_text(encoding='utf-8'))
        age = time.time() - (Path(DATA_DIR) / 'bybit_orderflow.json').stat().st_mtime
        rows = bf.get('数据') or {}
        row = rows.get(coin) or rows.get(coin + 'USDT')
        if row and age <= 300 and bf.get('状态') == 'ok':
            coverage['bybit'] = 'ok'
            out['Bybit订单流'] = row
    except Exception:
        pass
    orderflow_venues = sum(1 for v in coverage.values() if v == 'ok')
    can_direction = orderflow_venues >= 2
    level = '可用' if can_direction else ('仅参考' if (coverage or liq_heuristic) else '不可用')
    reasons = []
    if orderflow_venues < 2: reasons.append(f'订单流仅 {orderflow_venues} 个交易所有效')
    if liq_confirmed == 0: reasons.append('没有确认爆仓数据；爆仓只能提示风险，不能投方向票')
    out['数据质量'] = {'等级': level, '可用于方向判断': can_direction,
                       '爆仓可用于方向判断': liq_confirmed > 0,
                       '有效订单流交易所数': orderflow_venues, '原因': reasons}
    return out