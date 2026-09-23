# -*- coding: utf-8 -*-
"""多币种扫描界面。"""
import pandas as pd
import streamlit as st

import watchlist
import symbol_picker


def render():
    st.markdown('### 🔭 多币种扫描')
    st.caption(
        '**别只盯 BTC/ETH。** 用币安的批量接口，一次扫描全部自选币种 —— '
        '不管加多少个，都只花 2 次请求。'
    )

    syms = watchlist.load_watchlist()
    picked = symbol_picker.pick_multi('自选币种（可搜索、可多选）',
                                      key='wl_multi', default=syms)

    c1, c2 = st.columns(2)
    min_vol = c1.number_input('最低成交额（亿USDT）', min_value=0.0, max_value=1000.0,
                              value=0.5, step=0.5, key='wl_vol')
    top_by = c2.selectbox('排序方式', ['成交额', '涨跌幅', '资金费率'], key='wl_sort')

    b1, b2 = st.columns([1, 3])
    if b1.button('🔍 扫描', type='primary', key='wl_run'):
        if picked:
            watchlist.save_watchlist(picked)
        with st.spinner('扫描中……'):
            st.session_state['wl'] = watchlist.scan(
                picked or syms, min_volume_usd=min_vol * 1e8, top_by=top_by)
    b2.caption('关注点里会自动标出：**资金费率极端值**（币圈公认的拥挤度信号）、'
               '24h 涨跌异动、贴近区间高低点、流动性不足。')

    r = st.session_state.get('wl')
    if not r:
        st.info('点上面的「扫描」按钮。')
        return
    if r.get('错误'):
        st.error(r['错误'])
        return

    rows = r['数据']
    if not rows:
        st.warning('没有扫到数据。检查币种代码是否正确（比如 BTCUSDT 或直接写 BTC）。')
        return

    st.caption(f"扫描时间 {r['扫描时间']}｜自选 {len(rows)} 个｜"
               f"全市场 {r['市场总数']} 个合约")
    interesting = [x for x in rows if x.get('值得看')]
    if interesting:
        st.success(f'其中 {len(interesting)} 个有值得关注的点，已在表里标出。')

    df = pd.DataFrame(rows)
    cols = [c for c in ['币种', '标记价', '24h涨跌%', '资金费率%', '24h区间位置%',
                        '24h成交额', '关注点'] if c in df.columns]
    show = df[cols].copy()
    if '24h成交额' in show.columns:
        show['24h成交额'] = (show['24h成交额'] / 1e8).round(2)
        show = show.rename(columns={'24h成交额': '24h成交额(亿U)'})
    st.dataframe(show, height=min(560, 80 + 35 * len(show)))

    st.markdown('#### 挑一个去生成方案')
    pick = st.selectbox('币种', ['（不选）'] + [x['币种'] for x in rows], key='wl_pick')
    if pick != '（不选）':
        st.caption(f'去 **📋 交易方案** 页签，把币种填成 `{pick}` 就能生成完整方案。')
        if st.button(f'📋 复制这个币种名（{pick}）', key='wl_copy'):
            st.code(pick.lstrip('USDT') and pick.replace('USDT', ''), language=None)

    st.warning(
        '**⚠️ 关于「扫描出机会」这件事的诚实说明**\n\n'
        '这个扫描只做**事实呈现**（费率多少、涨跌多少、在区间什么位置），'
        '**不判断哪个会涨**。\n\n'
        '我前面用 3000 根 K 线做过条件统计 + 分段验证，'
        '技术指标和资金费率的条件在样本外都不稳定。'
        '**所以上面「值得看」的标记，意思是「这个数字异常」，不是「值得买」。**'
    )
