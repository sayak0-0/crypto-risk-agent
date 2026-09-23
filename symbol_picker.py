# -*- coding: utf-8 -*-
"""币种选择器：一个下拉框搞定 —— 推荐币排最上面，其余按成交额排。

为什么这样设计：原来推荐币放在另一个区域，用户在下拉框里找不到它们，
得换个地方操作，很不一致。现在统一成一个下拉框：
    · 推荐的排最前面，带 ⭐ 和理由
    · 其余 400 个按成交额排序跟在后面
    · 打字可搜索，选哪个都是一样的操作
"""
import streamlit as st

import watchlist

FALLBACK = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT',
    'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'TRXUSDT', 'DOTUSDT', 'LTCUSDT',
    'ARBUSDT', 'OPUSDT', 'MATICUSDT', 'SUIUSDT', 'APTUSDT', 'NEARUSDT',
    'TONUSDT', 'ENAUSDT', 'ONDOUSDT', 'PEPEUSDT', 'WIFUSDT', 'TRUMPUSDT',
]


@st.cache_data(ttl=1800, show_spinner=False)
def options(limit=400):
    """返回 (有序币种列表, 行情信息, 来源说明, 推荐理由字典)。

    推荐币（持仓 / 费率异常 / 今日异动）排在最前面。
    """
    rec_map = {}
    try:
        sug = watchlist.suggestions(limit=10)
        rec_map = {sym: why for sym, why in sug}
    except Exception:
        rec_map = {}

    try:
        r = watchlist.market_overview()
        rows = sorted(r.values(), key=lambda x: -(x.get('24h成交额') or 0))
        rows = [x for x in rows if (x.get('24h成交额') or 0) > 1e6]
        syms = [x['币种'] for x in rows][:limit]
        info = {x['币种']: x for x in rows}
        # ⭐ 推荐币挪到最前面，其余保持成交额排序
        head = [s for s in rec_map if s in set(syms)]
        rest = [s for s in syms if s not in rec_map]
        ordered = head + rest
        if ordered:
            return ordered, info, f'交易所 {len(r)} 个合约，按成交额排序', rec_map
    except Exception as e:
        return FALLBACK, {}, f'拉取失败（{type(e).__name__}），用内置列表', rec_map
    return FALLBACK, {}, '用内置列表', rec_map


def _short_tag(why):
    """把长理由压成 3-6 个字的短标签（下拉列表里放不下长文本）。"""
    w = why or ''
    if '实际持有' in w:
        return '我的持仓'
    if '资金费率' in w and '多头拥挤' in w:
        return '多头拥挤'
    if '资金费率' in w and '空头拥挤' in w:
        return '空头拥挤'
    if '24小时涨跌' in w:
        return '今日异动'
    if '区间顶部' in w:
        return '贴区间顶'
    if '区间底部' in w:
        return '贴区间底'
    if '成交额全市场第' in w:
        return '成交额前20'
    return ''


def _label(sym, info, rec_map=None):
    """下拉列表里显示的文本。保持简短 —— 太长了下拉框会被挤变形。"""
    d = info.get(sym) or {}
    chg = d.get('24h涨跌%')
    arrow = ''
    if chg is not None:
        arrow = '📈' if chg > 0 else ('📉' if chg < 0 else '➖')
    base = f'{sym} {arrow}{chg:+.1f}%' if chg is not None else sym
    if rec_map and sym in rec_map:
        tag = _short_tag(rec_map[sym])
        # ⭐ 标记 + 短标签，完整理由放在下拉框下面的提示区
        return f'⭐ {base}' + (f'「{tag}」' if tag else '')
    # 非推荐的放在后面，加个前缀方便区分
    return f'　 {base}' if rec_map else base


def _suggest_hint():
    """提示：推荐币已经在下拉框顶部了。"""
    with st.expander('💡 下拉框最上面带 ⭐ 的是怎么回事？', expanded=False):
        st.caption(
            '那是按**客观异常**筛出来的（**不是推荐买入**）：'
            '你的持仓最优先，然后是资金费率偏离常规的、今天异动的。\n\n'
            '**它们和下面几百个币一样，直接在下拉框里选就行。**'
        )
        rec_map = options()[3]
        if not rec_map:
            st.caption('（当前拉不到全市场数据，检查网络或代理）')
            return
        for sym, why in list(rec_map.items())[:8]:
            a, b = st.columns([1, 3.5])
            a.markdown(f'⭐ **{sym}**')
            b.caption(why)
        st.caption('⚠️ 费率极端 / 涨跌异动在样本外**不具备稳定预测力**，只是帮你收敛注意力。')


def pick(label='币种', key='sym_pick', default='BTCUSDT', help_text=None,
         compact=False):
    """渲染币种选择框。推荐币在列表顶部，其余按成交额排序。"""
    syms, info, src, rec_map = options()
    pool = syms or FALLBACK
    # 默认优先选推荐里的第一个（没有就选 default）
    dft = default
    if rec_map:
        first_rec = next((s for s in pool if s in rec_map), None)
        if first_rec and default == 'BTCUSDT':
            dft = first_rec
    if dft not in pool:
        dft = pool[0] if pool else 'BTCUSDT'
    idx = pool.index(dft) if dft in pool else 0

    if compact:
        out = st.selectbox(label, pool, index=idx,
                           format_func=lambda s: _label(s, info, rec_map),
                           key=key, help=help_text)
    else:
        c1, c2 = st.columns([3, 1])
        out = c1.selectbox(label, pool, index=idx,
                           format_func=lambda s: _label(s, info, rec_map),
                           key=key, help=help_text)
        c2.caption(f'共 {len(pool)} 个')
        if rec_map:
            c2.caption(f'⭐ 推荐 {len(rec_map)} 个在最上')
        _suggest_hint()

    if out and not out.endswith('USDT') and not out.endswith('USDC'):
        out = out + 'USDT'
    return out


def pick_multi(label='自选币种', key='sym_multi', default=None):
    syms, info, src, rec_map = options()
    pool = syms or FALLBACK
    dft = default or ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']
    dft = [s for s in dft if s in pool] or pool[:4]
    picked = st.multiselect(label, pool, default=dft,
                            format_func=lambda s: _label(s, info, rec_map), key=key)
    st.caption(f'{src}｜已选 {len(picked)} 个')
    return picked