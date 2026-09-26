# -*- coding: utf-8 -*-
"""HyperData Terminal 适配器：订单流、爆仓、盘口和危险仓位。"""
import os, requests

BASE = os.getenv('HYPERDATA_URL', 'http://127.0.0.1:8420')

def _get(path, timeout=8):
    r = requests.get(BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()

def context(symbol):
    coin = str(symbol or 'BTC').upper().replace('USDT','')
    out = {}
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
    except Exception as e: out['订单流错误'] = type(e).__name__
    try:
        l = _get('/v1/liquidations/stats')
        out.update({'爆仓总量U': l.get('total_volume_usd'), '爆仓笔数': l.get('total_count'),
                    '多头爆仓U': l.get('long_volume_usd'), '空头爆仓U': l.get('short_volume_usd')})
    except Exception as e: out['爆仓错误'] = type(e).__name__
    try:
        b = _get(f'/v1/orderbook/{coin}')
        out.update({'盘口失衡': b.get('imbalance'), '买卖价差': b.get('spread')})
    except Exception as e: out['盘口错误'] = type(e).__name__
    return out