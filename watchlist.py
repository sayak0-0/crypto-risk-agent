# -*- coding: utf-8 -*-
"""多币种扫描：一次看一片，而不是只盯 BTC/ETH。

效率设计：币安有批量接口，`/fapi/v1/ticker/24hr` 和 `/fapi/v1/premiumIndex`
一次返回**全部**合约的数据。所以不管自选多少币种，都只花 2 次请求。
"""
import json
from functools import lru_cache

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
        # 顺手算「价格在 24 小时区间的什么位置」
        try:
            hi, lo, px = out[sym]['24h最高'], out[sym]['24h最低'], out[sym]['标记价']
            if hi > lo:
                out[sym]['24h区间位置%'] = round((px - lo) / (hi - lo) * 100, 1)
        except Exception:
            pass
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

# ---------------- 快速筛选分类 ----------------
# 目的：400 多个币种，用户不知道看哪个。
# 但这里做的是【事实筛选】不是【推荐买入】—— 每个分类的依据都是客观可验证的数字，
# 而且会标注「为什么它出现在这里」，用户能一眼看出这是事实还是观点。
#
# ⚠️ 实测结论：用历史数据做过条件统计 + 样本外验证，
#    「费率极端」「涨跌异动」这类条件在样本外**不具备稳定预测力**。
#    所以这些分类的定位是「帮你筛选注意力」，不是「告诉你买什么」。


def _fmt_yi(v):
    if v is None:
        return '—'
    return f'{v / 1e8:,.1f}亿'


def annotate(rows, positions=None):
    """给每行加上筛选标签和理由。"""
    held = {str(p.get('币种', '')).upper() for p in (positions or [])}
    # 先按成交额算排名
    by_vol = sorted(rows, key=lambda x: -(x.get('24h成交额') or 0))
    rank = {r['币种']: i + 1 for i, r in enumerate(by_vol)}

    for r in rows:
        tags, why = [], []
        sym = r['币种']
        vol = r.get('24h成交额') or 0
        fr = r.get('资金费率%')
        chg = r.get('24h涨跌%')
        pos = r.get('24h区间位置%')

        r['成交额排名'] = rank.get(sym)

        if rank.get(sym, 999) <= 20:
            tags.append('流动性最好')
            why.append(f"成交额全市场第 {rank[sym]} 名（{_fmt_yi(vol)} USDT）")

        if fr is not None and abs(fr) >= 0.03:
            tags.append('费率异常')
            direction = '多头拥挤（做多要付钱）' if fr > 0 else '空头拥挤（做空要付钱）'
            why.append(f"8小时资金费率 {fr:+.4f}%，{direction}")

        if chg is not None and abs(chg) >= 5:
            tags.append('今日异动')
            why.append(f"24小时涨跌 {chg:+.1f}%")

        if pos is not None and (pos > 92 or pos < 8):
            tags.append('贴近区间边缘')
            where = '24小时区间顶部' if pos > 92 else '24小时区间底部'
            why.append(f"价格在{where}（区间位置 {pos:.0f}%）")

        if sym in held:
            tags.append('我的持仓')
            why.append('这是你当前实际持有的仓位')

        r['分类'] = tags
        r['筛选理由'] = '；'.join(why)
        r['值得看'] = bool(tags)
    return rows


@lru_cache(maxsize=1)
def crypto_contracts():
    """返回币安 USDT 永续里的真加密货币合约集合。

    过滤掉 TRADIFI_PERPETUAL（代币化股票、黄金、原油等）。
    元数据拿不到时返回 None，由调用方使用兜底名单，不阻断功能。
    """
    try:
        info = _get(BINANCE + '/fapi/v1/exchangeInfo')
        out = {
            x.get('symbol') for x in (info.get('symbols') or [])
            if x.get('status') == 'TRADING'
            and x.get('contractType') == 'PERPETUAL'
            and x.get('underlyingType') == 'COIN'
            and x.get('quoteAsset') == 'USDT'
        }
        return out or None
    except Exception:
        return None


def discover(top_n=200):
    """扫**全市场**（按成交额取前 top_n），找出值得看的币种。

    和 scan() 的区别：scan() 只扫你自选的，discover() 扫全市场 ——
    这样才能发现自选列表之外的异动。

    定位：帮你把注意力从 700 多个币种收敛到十几个，**不是推荐买入**。
    """
    import monitor
    try:
        market = market_overview()
    except Exception as e:
        return {'错误': f'{type(e).__name__}: {e}', '数据': []}
    rows = sorted(market.values(), key=lambda x: -(x.get('24h成交额') or 0))[:top_n]
    rows = annotate(rows, monitor.load_positions())
    return {'数据': rows, '扫描时间': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
            '全市场合约数': len(market), '已扫描': len(rows), '错误': None}


def quick_categories(rows):
    """返回 {分类名: [行...]}，以及每个分类的一句话说明。"""
    ORDER = ['我的持仓', '费率异常', '今日异动', '流动性最好', '贴近区间边缘']
    DESC = {
        '我的持仓':    '你实际持有的仓位 —— 最该看的',
        '费率异常':    '资金费率偏离常规，说明多空某一方拥挤',
        '今日异动':    '涨跌幅超过 5%，通常有原因',
        '流动性最好':  '成交额前 20 名，滑点最小',
        '贴近区间边缘': '价格贴着 24 小时高低点',
    }
    out = {}
    for c in ORDER:
        sub = [r for r in rows if c in (r.get('分类') or [])]
        if sub:
            out[c] = {'说明': DESC[c], '数据': sub}
    return out


def suggestions(limit=5):
    """给币种下拉框用的「值得看」提示。返回 [(币种, 理由), ...]。"""
    r = discover(top_n=200)
    if r.get('错误'):
        return []
    rows = r['数据']
    # 优先：持仓 > 费率异常 > 今日异动
    ORDER = ['我的持仓', '费率异常', '今日异动', '流动性最好']
    picked, seen = [], set()
    for cat in ORDER:
        for row in rows:
            if cat in (row.get('分类') or []) and row['币种'] not in seen:
                picked.append((row['币种'], row['筛选理由']))
                seen.add(row['币种'])
                if len(picked) >= limit:
                    return picked
    return picked
