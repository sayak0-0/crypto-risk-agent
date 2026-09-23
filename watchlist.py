# -*- coding: utf-8 -*-
"""多币种扫描：一次看一片，而不是只盯 BTC/ETH。

效率设计：币安有批量接口，`/fapi/v1/ticker/24hr` 和 `/fapi/v1/premiumIndex`
一次返回**全部**合约的数据。所以不管自选多少币种，都只花 2 次请求。
"""
import json

import requests

from common import get_proxy, DATA_DIR
import os

WATCHLIST_PATH = os.path.join(DATA_DIR, '我的自选币种.json')
UA = {'User-Agent': 'Mozilla/5.0'}
BINANCE = 'https://fapi.binance.com'

# 默认自选：主流 + 有一定流动性的山寨
DEFAULT_WATCHLIST = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
    'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'ARBUSDT', 'ENAUSDT',
    'ONDOUSDT', 'TRUMPUSDT',
]

# 过滤掉这些噪音合约（杠杆代币、交割合约等）
# ⚠️ 币安的「下季度」交割合约是 `_` 开头（如 _BTCUSDT），不是结尾 —— 两头都要判
EXCLUDE_SUFFIX = ('DOWNUSDT', 'UPUSDT', 'BULLUSDT', 'BEARUSDT')
EXCLUDE_PREFIX = ('_',)


def load_watchlist():
    if not os.path.exists(WATCHLIST_PATH):
        return list(DEFAULT_WATCHLIST)
    try:
        with open(WATCHLIST_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return [str(x).upper().strip() for x in d if str(x).strip()] or list(DEFAULT_WATCHLIST)
    except Exception:
        return list(DEFAULT_WATCHLIST)


def save_watchlist(symbols):
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
        json.dump(list(symbols), f, ensure_ascii=False, indent=2)
    return WATCHLIST_PATH


def _get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=20, proxies=get_proxy())
    r.raise_for_status()
    return r.json()


def market_overview(exchange='币安'):
    """拿全市场的价格 + 涨跌 + 资金费率（2 次请求）。"""
    tickers = _get(f'{BINANCE}/fapi/v1/ticker/24hr')
    premium = _get(f'{BINANCE}/fapi/v1/premiumIndex')

    out = {}
    for t in tickers:
        sym = t.get('symbol', '')
        if not sym or any(sym.endswith(x) for x in EXCLUDE_SUFFIX):
            continue
        try:
            out[sym] = {
                '币种': sym,
                '标记价': float(t['lastPrice']),
                '24h涨跌%': float(t['priceChangePercent']),
                '24h成交额': float(t['quoteVolume']),
                '24h最高': float(t['highPrice']),
                '24h最低': float(t['lowPrice']),
                '资金费率%': None,
                '持仓量': None,
            }
        except (TypeError, ValueError, KeyError):
            continue
    for p in premium:
        sym = p.get('symbol')
        if sym in out:
            try:
                out[sym]['资金费率%'] = float(p['lastFundingRate']) * 100
                out[sym]['标记价'] = float(p['markPrice'])
            except (TypeError, ValueError, KeyError):
                pass
    return out


def scan(symbols=None, min_volume_usd=5e7, top_by='成交额'):
    """扫描自选币种，返回带信号的列表。

    min_volume_usd: 成交额低于这个数的不看（流动性太差，滑点会吃掉一切）
    """
    syms = symbols or load_watchlist()
    try:
        market = market_overview()
    except Exception as e:
        return {'错误': f'{type(e).__name__}: {e}', '数据': []}

    rows = []
    for s in syms:
        # ⚠️ 必须先转大写 —— 否则用户写 'btc' 匹配不到 'BTCUSDT'
        s = str(s).upper().strip()
        sym = s if s.endswith('USDT') else s + 'USDT'
        d = market.get(sym)
        if not d:
            continue
        notes = []
        fr = d.get('资金费率%')
        chg = d.get('24h涨跌%')
        vol = d.get('24h成交额', 0)
        # 流动性检查
        if vol < min_volume_usd:
            notes.append('⚠️ 流动性偏低')
        # 资金费率极端值（币圈的实用信号之一）
        if fr is not None:
            if fr > 0.05:
                notes.append(f'🔴 费率偏高 {fr:.4f}%（多头拥挤，做多要付钱）')
            elif fr > 0.02:
                notes.append(f'🟠 费率偏高 {fr:.4f}%')
            elif fr < -0.05:
                notes.append(f'🔵 费率偏负 {fr:.4f}%（空头拥挤，可能逼空）')
            elif fr < -0.02:
                notes.append(f'🔵 费率偏负 {fr:.4f}%')
        # 涨跌幅异动
        if chg is not None:
            if chg > 8:
                notes.append(f'📈 24h涨 {chg:.1f}%')
            elif chg < -8:
                notes.append(f'📉 24h跌 {chg:.1f}%')
        # 区间位置
        hi, lo, p = d.get('24h最高'), d.get('24h最低'), d.get('标记价')
        pos = None
        if hi and lo and p and hi > lo:
            pos = (p - lo) / (hi - lo) * 100
            d['24h区间位置%'] = round(pos, 1)
            if pos > 92:
                notes.append('⬆️ 贴近24h高点')
            elif pos < 8:
                notes.append('⬇️ 贴近24h低点')
        d['关注点'] = '　'.join(notes) if notes else ''
        d['值得看'] = bool(notes)
        rows.append(d)

    if top_by == '成交额':
        rows.sort(key=lambda x: -(x.get('24h成交额') or 0))
    elif top_by == '涨跌幅':
        rows.sort(key=lambda x: -(x.get('24h涨跌%') or 0))
    elif top_by == '资金费率':
        rows.sort(key=lambda x: -(abs(x.get('资金费率%') or 0)))
    return {'数据': rows, '扫描时间': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
            '市场总数': len(market), '错误': None}