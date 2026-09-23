# -*- coding: utf-8 -*-
"""多币种扫描界面：快速筛选 + 自选扫描。

「快速筛选」解决的是：700 多个币种，用户不知道该看哪个。
但它做的是【事实筛选】不是【推荐买入】—— 每条都标注了「为什么它在这里」，
用户能一眼看出这是客观数字还是别人的观点。
"""
import pandas as pd
import streamlit as st

import plan_ui
import tasks
import watchlist


def _table(rows, height=460):
    if not rows:
        st.info('这一分类里暂时没有币种。')
        return
    df = pd.DataFrame(rows)
    cols = [c for c in ['币种', '标记价', '24h涨跌%', '资金费率%',
                        '24h区间位置%', '成交额排名', '筛选理由'] if c in df.columns]
    show = df[cols].copy()
    if '资金费率%' in show.columns:
        show['资金费率%'] = show['资金费率%'].map(
            lambda x: f'{x:+.4f}%' if pd.notna(x) else '—')
    if '24h涨跌%' in show.columns:
        show['24h涨跌%'] = show['24h涨跌%'].map(
            lambda x: f'{x:+.2f}%' if pd.notna(x) else '—')
    if '24h区间位置%' in show.columns:
        show['24h区间位置%'] = show['24h区间位置%'].map(
            lambda x: f'{x:.0f}%' if pd.notna(x) else '—')
    if '标记价' in show.columns:
        show['标记价'] = show['标记价'].map(
            lambda x: f'{x:,.4f}' if pd.notna(x) else '—')
    show = show.rename(columns={'成交额排名': '成交额名次'})
    st.dataframe(show, height=min(height, 80 + 33 * len(show)))


def render_quick(cfg=None):
    st.markdown('### 🔍 不知道看哪个？按这几类快速筛')
    st.warning(
        '**这是「事实筛选」，不是「推荐买入」。**\n\n'
        '每个分类的依据都是客观数字（成交额、资金费率、涨跌幅、区间位置），'
        '而且每个币都会告诉你「**为什么它出现在这里**」。\n\n'
        '⚠️ 我用 3000 根历史 K 线做过条件统计 + 样本外验证：'
        '**「费率极端」「涨跌异动」这类条件在样本外不具备稳定预测力。**'
        '所以这里只帮你收敛注意力，不告诉你该买什么。'
    )

    if st.button('🔄 扫描全市场', type='primary', key='disc_run'):
        with st.spinner('正在扫全市场（约 3-5 秒）……'):
            st.session_state['disc'] = watchlist.discover(top_n=200)

    r = st.session_state.get('disc')
    if not r:
        st.info('点上面的按钮扫描。'
                '会从全市场 700+ 个合约里，按成交额取前 200 个来找异常。')
        return

    if r.get('错误'):
        st.error(r['错误'])
        return

    rows = r['数据']
    cats = watchlist.quick_categories(rows)
    st.caption(f"扫描时间 {r['扫描时间']}｜全市场 {r['全市场合约数']} 个合约，"
               f"已扫描成交额前 {r['已扫描']} 个")

    tabs = ['全部'] + list(cats.keys())
    pick = st.radio('分类', tabs, horizontal=True, key='disc_cat',
                    label_visibility='collapsed')

    if pick == '全部':
        hit = [x for x in rows if x.get('值得看')]
        hit.sort(key=lambda x: x.get('成交额排名') or 999)
        st.caption(f'全部有异动的：**{len(hit)} 个**（按成交额名次排序）')
        _table(hit, height=520)
    else:
        info = cats[pick]
        st.markdown(f"**{pick}**（{len(info['数据'])} 个）—— {info['说明']}")
        _table(info['数据'], height=460)

    # 一键把筛出来的币种设成自选
    if pick != '全部':
        if st.button(f"➕ 把这 {len(cats[pick]['数据'])} 个设为自选", key='disc_save'):
            syms = [x['币种'] for x in cats[pick]['数据']][:30]
            watchlist.save_watchlist(syms)
            st.success(f'已设为自选（{len(syms)} 个）。下面的「自选币种扫描」可以用它们。')

    # ---------- 看中了就直接生成方案（不用复制名字） ----------
    st.divider()
    st.markdown('#### 🎯 看中哪个？直接生成方案')
    cand = ([x['币种'] for x in cats[pick]['数据']] if pick != '全部'
            else [x['币种'] for x in rows if x.get('值得看')])
    if not cand:
        st.caption('这一分类里没有币种。')
        return

    cfg = cfg or {}
    cc1, cc2, cc3, cc4 = st.columns([2, 1, 1, 1])
    target = cc1.selectbox('币种', cand, key='disc_target',
                           format_func=lambda s: f'{s}')
    eq = cc2.number_input('本金', min_value=1.0,
                          value=float(cfg.get('本金', 1000.0)), step=100.0,
                          key='disc_eq', label_visibility='visible')
    rp = cc3.number_input('风险%', min_value=0.1, max_value=10.0,
                          value=float(cfg.get('单笔风险百分比', 1.0)), step=0.1,
                          key='disc_rp')
    lv = cc4.number_input('杠杆', min_value=1.0, max_value=125.0,
                          value=float(cfg.get('杠杆', 10.0)), step=1.0,
                          key='disc_lv')

    db = st.toggle('⚔️ 开启多空辩论（多 4 次模型调用）', value=True, key='disc_debate')
    if st.button(f'🚀 直接生成 {target} 的交易方案', type='primary', key='disc_go'):
        st.session_state['disc_task'] = plan_ui.start_task(
            target, cfg, eq, rp, lv, db)
        st.rerun()

    dtid = st.session_state.get('disc_task')
    if dtid:
        dres, dana = plan_ui.render_progress(dtid, key='disc_poll')
        if dres:
            st.divider()
            st.success(f'✅ {target} 的方案已生成（就地显示，不用切页签）')
            plan_ui.render_result(dres, dana)
        elif tasks.status(dtid).get('状态') in ('排队中', '运行中'):
            st.caption('👉 也可以切到「📋 交易方案」页签看，任务是一样的。')


def render_watchlist():
    st.markdown('### 📋 自选币种扫描')
    syms = watchlist.load_watchlist()

    c1, c2 = st.columns(2)
    with c1:
        picked = st.multiselect('自选币种（可搜索、可多选）', syms + [
            s for s in watchlist.DEFAULT_WATCHLIST if s not in syms],
            default=syms, key='wl_multi')
    min_vol = c2.number_input('最低成交额（亿USDT）', min_value=0.0,
                              max_value=1000.0, value=0.5, step=0.5, key='wl_vol')

    c3, c4 = st.columns([1, 3])
    top_by = c3.selectbox('排序方式', ['成交额', '涨跌幅', '资金费率'], key='wl_sort')
    if c4.button('🔍 扫描自选', type='primary', key='wl_run'):
        if picked:
            watchlist.save_watchlist(picked)
        with st.spinner('扫描中……'):
            st.session_state['wl'] = watchlist.scan(
                picked or syms, min_volume_usd=min_vol * 1e8, top_by=top_by)

    r = st.session_state.get('wl')
    if not r:
        st.info('选好币种后点「扫描自选」。')
        return
    if r.get('错误'):
        st.error(r['错误'])
        return

    rows = r['数据']
    if not rows:
        st.warning('没扫到数据。检查币种代码（如 BTCUSDT）或网络。')
        return
    st.caption(f"扫描时间 {r['扫描时间']}｜自选 {len(rows)} 个")
    _table(rows, height=420)


def render(cfg=None):
    render_quick(cfg)
    st.divider()
    render_watchlist()

    st.divider()
    st.info(
        '**⚠️ 关于「扫描出机会」这件事的诚实说明**\n\n'
        '这个扫描只做**事实呈现**——费率多少、涨跌多少、在区间什么位置，'
        '**不判断哪个会涨**。\n\n'
        '我前面用 3000 根 K 线做过条件统计 + 分段验证，'
        '技术指标和资金费率的条件在样本外都不稳定。'
        '**所以「值得看」的意思是「这个数字异常」，不是「值得买」。**'
    )