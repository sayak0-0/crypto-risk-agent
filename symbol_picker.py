# -*- coding: utf-8 -*-
"""币种选择器：下拉列表 + 手填兜底。

为什么做这个：原来所有地方都靠手打币种（BTC / ETH…），
打错了不会报错，只是拿不到数据，体验很差。

现在改成从交易所拉全市场币种，按成交额排序放进下拉框。
Streamlit 的 selectbox 支持打字搜索，所以选起来很快。
拉不到（离线 / 网络问题）就用内置的常用列表兜底。
"""
import streamlit as st

import watchlist

# 兜底列表：按流动性大致排序
FALLBACK = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT',
    'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'TRXUSDT', 'DOTUSDT', 'LTCUSDT',
    'ARBUSDT', 'OPUSDT', 'MATICUSDT', 'SUIUSDT', 'APTUSDT', 'NEARUSDT',
    'TONUSDT', 'ENAUSDT', 'ONDOUSDT', 'PEPEUSDT', 'WIFUSDT', 'TRUMPUSDT',
]


@st.cache_data(ttl=1800, show_spinner=False)
def options(limit=400):
    """拿可选币种。返回 (列表, {币种: 行情信息}, 数据来源说明)。

    缓存 30 分钟 —— 币种列表不会频繁变动，不用每次刷新都请求。
    """
    try:
        r = watchlist.market_overview()
        rows = sorted(r.values(), key=lambda x: -(x.get('24h成交额') or 0))
        rows = [x for x in rows if (x.get('24h成交额') or 0) > 1e6]   # 过滤极小额
        syms = [x['币种'] for x in rows][:limit]
        info = {x['币种']: x for x in rows}
        if syms:
            return syms, info, f'来自交易所，共 {len(r)} 个合约（已按成交额排序）'
    except Exception as e:
        return FALLBACK, {}, f'拉取失败（{type(e).__name__}），用内置常用列表'
    return FALLBACK, {}, '用内置常用列表'


def _label(sym, info):
    d = info.get(sym) or {}
    chg = d.get('24h涨跌%')
    if chg is None:
        return sym
    arrow = '📈' if chg > 0 else ('📉' if chg < 0 else '➖')
    return f'{sym}　{arrow} {chg:+.1f}%'


def pick(label='币种', key='sym_pick', default='BTCUSDT', help_text=None,
         compact=False):
    """渲染一个币种选择控件，返回选中的币种代码（如 BTCUSDT）。

    带「手动输入」选项 —— 列表里没有的币种（或离线时）可以自己填。
    compact=True 时只放一个下拉框（用于本来就窄的表单，避免嵌套两层列）
    """
    syms, info, src = options()
    pool = syms or FALLBACK
    dft = default if default in pool else (pool[0] if pool else 'BTCUSDT')
    idx = pool.index(dft) if dft in pool else 0

    if compact:
        out = st.selectbox(label, pool, index=idx,
                           format_func=lambda s: _label(s, info), key=key,
                           help=help_text)
    else:
        manual_key = f'{key}__manual'
        mode_key = f'{key}__mode'
        c1, c2 = st.columns([3, 1])
        manual = c2.toggle('✏️ 手填', key=mode_key,
                           help='列表里没有的币种，或者离线时用手填')
        if manual:
            v = c1.text_input(label, value=dft, key=manual_key,
                              help=help_text or '直接输入币种代码，如 BTCUSDT 或 BTC')
            out = (v or '').strip().upper()
        else:
            out = c1.selectbox(label, pool, index=idx,
                               format_func=lambda s: _label(s, info), key=key,
                               help=help_text)
        c2.caption(f'共 {len(syms)} 个')

    if out and not out.endswith('USDT') and not out.endswith('USDC'):
        out = out + 'USDT'
    return out


def pick_multi(label='自选币种', key='sym_multi', default=None):
    """多选版本，给「多币种扫描」用。"""
    syms, info, src = options()
    dft = default or ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']
    dft = [s for s in dft if s in syms] or (syms[:4] if syms else FALLBACK[:4])
    picked = st.multiselect(label, syms or FALLBACK, default=dft,
                            format_func=lambda s: _label(s, info), key=key)
    st.caption(f'{src}｜已选 {len(picked)} 个')
    return picked