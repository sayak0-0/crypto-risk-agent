# -*- coding: utf-8 -*-
"""加密货币合约交易助手（Streamlit 界面）。

四个页签：开仓前检查 / 交易日志 / 绩效看板 / AI 复盘 / 行情参考
运行：streamlit run app.py
"""
import datetime as dt
import os

import pandas as pd
import streamlit as st

import agents_ui
import chat_ui
import context_ui
import journal
import llm
import market
import metrics
import monitor_ui
import plan_ui
import review
import risk
import watchlist_ui
from common import get_env, load_config, save_config, fmt_usdt
import symbol_picker

st.set_page_config(page_title='合约交易助手', layout='wide')

TRADES_PATH = journal.TRADES_CSV


# ============ 侧边栏：风控设置 ============
cfg = load_config()

# ============ 界面模式：默认对话式，可切回经典多页签 ============
if 'ui_mode' not in st.session_state:
    st.session_state['ui_mode'] = 'chat'

if st.session_state['ui_mode'] == 'chat':
    chat_ui.render(cfg)
    st.stop()

# ================= 以下是经典多页签界面 =================

if st.sidebar.button('← 回到对话界面', use_container_width=True,
                     type='primary', key='back_to_chat'):
    st.session_state['ui_mode'] = 'chat'
    st.rerun()

st.sidebar.title('我的风控设置')
with st.sidebar.form('cfg'):
    equity = st.number_input('账户本金（USDT）', min_value=1.0,
                             value=float(cfg['本金']), step=100.0)
    risk_pct = st.number_input('单笔最大风险（本金%）', min_value=0.1, max_value=20.0,
                               value=float(cfg['单笔风险百分比']), step=0.1)
    leverage = st.number_input('杠杆', min_value=1.0, max_value=125.0,
                               value=float(cfg['杠杆']), step=1.0)
    fee_rate = st.number_input('手续费率（单边，0.0005 = 0.05%）', min_value=0.0,
                               max_value=0.01, value=float(cfg['手续费率']),
                               step=0.0001, format='%.4f')
    mmr = st.number_input('维持保证金率（0.005 = 0.5%）', min_value=0.0, max_value=0.5,
                          value=float(cfg['维持保证金率']), step=0.001, format='%.3f')
    min_rr = st.number_input('最低可接受盈亏比', min_value=0.5, max_value=10.0,
                             value=float(cfg['最低盈亏比']), step=0.1)
    max_daily = st.number_input('每日最多交易笔数', min_value=1, max_value=50,
                                value=int(cfg['每日最多交易笔数']), step=1)
    max_streak = st.number_input('连亏几笔后强制停手', min_value=1, max_value=10,
                                 value=int(cfg['连亏几笔后停手']), step=1)
    if st.form_submit_button('保存设置'):
        save_config({
            '本金': equity, '单笔风险百分比': risk_pct, '杠杆': leverage,
            '手续费率': fee_rate, '维持保证金率': mmr, '最低盈亏比': min_rr,
            '每日最多交易笔数': max_daily, '连亏几笔后停手': max_streak,
        })
        st.success('已保存')

st.sidebar.divider()
st.sidebar.caption(
    '这是一个帮你算账和踩刹车的工具，不是赚钱工具。\n\n'
    '它不会预测价格，也不会帮你下单。它的价值在于：'
    '让你每单亏多少事先就知道，让你亏钱的规律被记录下来。'
)

df = journal.load(TRADES_PATH)
state = journal.recent_state(df)

# 把侧边栏当前的值（不一定已保存）打包给监控页用
live_cfg = dict(cfg, 本金=equity, 单笔风险百分比=risk_pct, 杠杆=leverage,
                手续费率=fee_rate, 维持保证金率=mmr, 最低盈亏比=min_rr,
                每日最多交易笔数=max_daily, 连亏几笔后停手=max_streak)

st.sidebar.divider()
st.sidebar.metric('今天已交易', f'{state["today_trades"]} 笔')
st.sidebar.metric('当前连亏', f'{state["consecutive_losses"]} 笔')

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9 = st.tabs(
    ['交易方案', '开仓前检查', '交易日志', '绩效看板', '复盘',
     '行情参考', '市场监控', '市场情报', '多智能体'])


# ============ 页签2：开仓前检查 ============
with tab2:
    st.subheader('开仓前检查')
    st.caption('准备下单前，先把数字填进来。算完再决定要不要点确认。')

    c1, c2, c3 = st.columns(3)
    symbol = c1.text_input('币种', value='BTC', key='ck_symbol')
    direction_cn = c2.radio('方向', ['多', '空'], horizontal=True, key='ck_dir')
    lev_use = c3.number_input('这一单用几倍杠杆', min_value=1.0, max_value=125.0,
                              value=float(leverage), step=1.0, key='ck_lev')

    c4, c5, c6 = st.columns(3)
    entry = c4.number_input('开仓价', min_value=0.0, value=0.0, step=1.0,
                            format='%.6f', key='ck_entry')
    stop = c5.number_input('止损价（必填）', min_value=0.0, value=0.0, step=1.0,
                           format='%.6f', key='ck_stop')
    target = c6.number_input('目标价（选填，填了才能算盈亏比）', min_value=0.0,
                             value=0.0, step=1.0, format='%.6f', key='ck_target')

    reason = st.text_input('入场理由（一句话说清为什么开这一单）',
                           placeholder='例：4小时回踩MA20不破，缩量，止损放在前低下方',
                           key='ck_reason')

    if st.button('检查这一单', type='primary'):
        direction = 'long' if direction_cn == '多' else 'short'
        if entry <= 0:
            st.error('请填写开仓价')
        elif stop <= 0:
            st.error('请填写止损价。没有止损价就不能开仓 —— 这是硬规则。')
        else:
            try:
                verdict, items = risk.pre_trade_check(
                    equity=equity, risk_pct=risk_pct, entry=entry, stop=stop,
                    leverage=lev_use, direction=direction,
                    target=target if target > 0 else None,
                    fee_rate=fee_rate, mmr=mmr, min_rr=min_rr,
                    today_trades=state['today_trades'], max_daily_trades=max_daily,
                    consecutive_losses=state['consecutive_losses'],
                    max_consecutive_losses=max_streak,
                    minutes_since_last_loss=state['minutes_since_last_loss'],
                    extra_notes=reason,
                )
                pos = risk.calc_position(equity, risk_pct, entry, stop, lev_use,
                                         direction, fee_rate, mmr,
                                         target if target > 0 else None)

                if verdict == '拒绝':
                    st.error('## 结论：拒绝开仓')
                elif verdict == '警告':
                    st.warning('## 结论：可以开，但有几处要注意')
                else:
                    st.success('## 结论：通过')

                m1, m2, m3, m4 = st.columns(4)
                m1.metric('建议开仓数量', f'{pos["建议数量"]:.6f} {market.normalize_symbol(symbol)}')
                m2.metric('名义价值', f'{pos["名义价值"]:,.2f} USDT')
                m3.metric('占用保证金', f'{pos["占用保证金"]:,.2f} USDT',
                          f'{pos["保证金占本金比例"]:.1f}% 本金')
                m4.metric('止损时亏损', f'{pos["止损时实际亏损"]:,.2f} USDT',
                          f'{pos["止损时实际亏损比例"]:.2f}%', delta_color='inverse')

                m5, m6, m7, m8 = st.columns(4)
                m5.metric('估算爆仓价', f'{pos["爆仓价"]:,.4f}')
                m6.metric('保本价', f'{pos["保本价"]:,.4f}')
                m7.metric('止损距离', f'{pos["止损距离百分比"]:.2f}%')
                m8.metric('预估手续费', f'{pos["预估手续费"]:,.2f} USDT')
                if '盈亏比' in pos:
                    st.metric('盈亏比', f'{pos["盈亏比"]:.2f}',
                              f'达到目标赚 {pos["达到目标的盈利"]:,.2f} USDT')

                st.markdown('#### 逐条检查')
                for it in items:
                    icon = {'通过': '通过', '警告': '注意', '拒绝': '拒绝',
                            '提示': '💡', '记录': '📝'}.get(it['级别'], '•')
                    st.markdown(f'{icon} **{it["项目"]}** —— {it["说明"]}')

                if st.button('把这单存成待开仓记录'):
                    st.session_state['pending'] = {
                        '币种': market.normalize_symbol(symbol),
                        '方向': direction_cn,
                        '杠杆': lev_use,
                        '开仓价': entry,
                        '止损价': stop,
                        '目标价': target if target > 0 else '',
                        '入场理由': reason,
                    }
                    st.info('已存入。切到「交易日志」页签去完善并保存。')
            except ValueError as e:
                st.error(f'参数有问题：{e}')

    st.divider()
    st.markdown('#### 一句话记住的事')
    st.markdown(
        '- 仓位不是靠感觉来的，是**先定亏多少，再倒推开多少**。\n'
        '- 杠杆高低不影响你这单亏多少钱（因为你按风险算仓位），'
        '它只决定你离爆仓有多近。\n'
        '- 止损价必须在爆仓价**之内**，否则你会先被强平，止损单白挂。'
    )


# ============ 页签3：交易日志 ============
with tab3:
    st.subheader('交易日志')
    st.caption(f'记录文件：{TRADES_PATH}')

    pending = st.session_state.get('pending', {})

    with st.form('add_trade', clear_on_submit=True):
        r1 = st.columns(4)
        f_open = r1[0].date_input('开仓日期', value=dt.date.today())
        f_open_t = r1[1].time_input('开仓时间', value=dt.datetime.now().time().replace(microsecond=0))
        f_close = r1[2].date_input('平仓日期', value=dt.date.today())
        f_close_t = r1[3].time_input('平仓时间', value=dt.datetime.now().time().replace(microsecond=0))

        r2 = st.columns(4)
        f_symbol = r2[0].text_input('币种', value=pending.get('币种', 'BTCUSDT'))
        f_dir = r2[1].selectbox('方向', journal.方向选项,
                                index=journal.方向选项.index(pending.get('方向', '多')))
        f_lev = r2[2].number_input('杠杆', min_value=1.0, max_value=125.0,
                                   value=float(pending.get('杠杆', leverage)), step=1.0)
        f_exchange = r2[3].selectbox('交易所', journal.交易所选项)

        r3 = st.columns(5)
        f_entry = r3[0].number_input('开仓价', min_value=0.0,
                                     value=float(pending.get('开仓价', 0.0)),
                                     format='%.6f', step=0.0)
        f_exit = r3[1].number_input('平仓价', min_value=0.0, value=0.0,
                                    format='%.6f', step=0.0)
        f_qty = r3[2].number_input('数量（币）', min_value=0.0, value=0.0,
                                   format='%.6f', step=0.0)
        f_stop = r3[3].number_input('止损价', min_value=0.0,
                                    value=float(pending.get('止损价', 0.0)),
                                    format='%.6f', step=0.0)
        f_tp = r3[4].number_input('止盈价', min_value=0.0,
                                  value=float(pending.get('目标价') or 0.0),
                                  format='%.6f', step=0.0)

        r4 = st.columns(3)
        f_fee = r4[0].number_input('手续费（USDT，不填自动估算）', min_value=0.0,
                                   value=0.0, step=0.01)
        f_emo = r4[1].selectbox('开仓时的状态', journal.情绪选项)
        f_note = r4[2].text_input('复盘笔记')

        f_reason = st.text_input('入场理由', value=pending.get('入场理由', ''))

        if st.form_submit_button('➕ 保存这笔交易', type='primary'):
            if f_entry <= 0:
                st.error('开仓价必须大于 0')
            elif f_exit <= 0:
                st.error('平仓价必须大于 0（还没平仓就先别记）')
            elif f_qty <= 0:
                st.error('数量必须大于 0')
            else:
                fee = f_fee
                if fee <= 0:
                    fee = f_qty * f_entry * fee_rate + f_qty * f_exit * fee_rate
                row = {
                    '编号': journal.new_id(df),
                    '开仓时间': dt.datetime.combine(f_open, f_open_t).strftime('%Y-%m-%d %H:%M'),
                    '平仓时间': dt.datetime.combine(f_close, f_close_t).strftime('%Y-%m-%d %H:%M'),
                    '交易所': f_exchange,
                    '币种': f_symbol.strip().upper(),
                    '方向': f_dir,
                    '杠杆': f_lev,
                    '开仓价': f_entry,
                    '平仓价': f_exit,
                    '数量': f_qty,
                    '止损价': f_stop,
                    '止盈价': f_tp,
                    '手续费': round(fee, 4),
                    '净盈亏': '',
                    'R倍数': '',
                    '情绪': f_emo,
                    '入场理由': f_reason,
                    '复盘笔记': f_note,
                }
                df = journal.add_trade(row, TRADES_PATH)
                st.session_state.pop('pending', None)
                st.success(f'已保存 {row["编号"]}。刷新一下就能在下面看到。')
                st.rerun()

    st.divider()

    if len(df) == 0:
        st.info('还没有交易记录。从上面一笔一笔开始记，记满 10 笔就会发现规律。')
    else:
        show = df.copy()
        for c in ['开仓时间', '平仓时间']:
            if c in show.columns:
                show[c] = show[c].dt.strftime('%Y-%m-%d %H:%M')
        if '持仓时长' in show.columns:
            show['持仓时长'] = show['持仓时长'].map(
                lambda x: f'{x.total_seconds()/3600:.1f}h'
                if pd.notna(x) and hasattr(x, 'total_seconds') else '')
        cols = [c for c in ['编号', '开仓时间', '平仓时间', '币种', '方向', '杠杆',
                            '开仓价', '平仓价', '数量', '止损价', '手续费', '净盈亏',
                            'R倍数', '情绪', '入场理由', '复盘笔记'] if c in show.columns]
        st.dataframe(show[cols], height=420)

        c1, c2 = st.columns([1, 3])
        c1.download_button('⬇️ 导出 CSV',
                           df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
                           file_name='我的交易记录.csv', mime='text/csv')
        with c2.expander('删除操作'):
            st.caption('删除是不可撤销的，建议先导出备份。')
            if st.button('删除最后一条记录'):
                if len(df):
                    journal.save(df.iloc[:-1], TRADES_PATH)
                    st.warning('已删除最后一条。')
                    st.rerun()
            uploaded = st.file_uploader('或上传 CSV 覆盖全部记录', type=['csv'])
            if uploaded is not None:
                try:
                    newdf = pd.read_csv(uploaded, encoding='utf-8-sig', dtype=str)
                    journal.save(newdf, TRADES_PATH)
                    st.success('已覆盖，请刷新页面查看。')
                except Exception as e:
                    st.error(f'读取失败：{e}')


# ============ 页签4：绩效看板 ============
with tab4:
    st.subheader('绩效看板')
    s = metrics.summary(df, start_equity=equity)

    if not s or s.get('交易笔数', 0) == 0:
        st.info('还没有已平仓的交易，先去「交易日志」记几笔。')
    else:
        k1, k2, k3, k4 = st.columns(4)
        k1.metric('总净盈亏', f'{fmt_usdt(s["总净盈亏"])} USDT',
                  f'{s["总净盈亏"] / equity * 100:.2f}% 本金')
        k2.metric('胜率', f'{s["胜率"]:.1f}%', f'{s["盈利笔数"]}胜 / {s["亏损笔数"]}负')
        k3.metric('盈亏比', f'{s["盈亏比"]:.2f}' if s['盈亏比'] else '-',
                  f'平均赚 {s["平均盈利"]:.1f} / 亏 {s["平均亏损"]:.1f}')
        k4.metric('盈利因子', f'{s["盈利因子"]:.2f}' if s['盈利因子'] else '-',
                  '>1.5 才算健康', delta_color='off')

        k5, k6, k7, k8 = st.columns(4)
        k5.metric('期望值（每笔）', f'{fmt_usdt(s["期望值"])} USDT')
        k6.metric('平均 R', f'{s["平均R"]:.2f}R' if s['平均R'] is not None else '-',
                  f'累计 {s["总R"]:.1f}R' if s['总R'] is not None else '')
        k7.metric('最大回撤', f'{s["最大回撤"]:,.2f} USDT',
                  f'{s["最大回撤百分比"]:.2f}%' if s['最大回撤百分比'] else '')
        k8.metric('总手续费', f'{s["总手续费"]:,.2f} USDT',
                  f'占成交额 {s["总手续费"] / s["总名义成交额"] * 100:.3f}%'
                  if s['总名义成交额'] else '')

        st.caption(f'最大连胜 {s["最大连胜"]} 笔 / 最大连亏 {s["最大连亏"]} 笔'
                   + (f' / 平均持仓 {s["平均持仓小时"]:.1f} 小时'
                      if s.get('平均持仓小时') else ''))

        st.markdown('#### 权益曲线')
        curve = metrics.equity_curve(df, start_equity=equity)
        if len(curve):
            st.line_chart(curve.set_index('序号')['权益'], height=260)

        st.markdown('#### 归因分析：钱到底亏在哪')
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('**按币种**')
            g = metrics.by_group(df, '币种')
            st.dataframe(g, height=220) if len(g) else st.caption('无数据')
        with c2:
            st.markdown('**按情绪状态**')
            g = metrics.by_group(df, '情绪')
            st.dataframe(g, height=220) if len(g) else st.caption('无数据')

        c3, c4 = st.columns(2)
        with c3:
            st.markdown('**按方向**')
            g = metrics.by_group(df, '方向')
            st.dataframe(g, height=180) if len(g) else st.caption('无数据')
        with c4:
            st.markdown('**按时段（小时）**')
            g = metrics.hourly_by(df)
            st.dataframe(g, height=180) if len(g) else st.caption('无数据')


# ============ 页签5：复盘 ============
with tab5:
    st.subheader('复盘')

    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown('#### ① 规则复盘（不花钱，先看这个）')
        if st.button('生成规则复盘', type='primary'):
            findings, keys = review.rule_review(df, start_equity=equity)
            for f in findings:
                st.markdown(f)
                st.divider()
    with c2:
        st.markdown('#### ② AI 复盘（调用硅基流动，花钱）')
        _llm = llm.describe()
        key_ok = _llm['已配置']
        st.caption((f"模型服务：{_llm['服务地址']}｜模型：{_llm['模型']}｜Key：{_llm['Key']}"
                    if key_ok else
                    '未配置模型 API Key，AI 复盘不可用（规则复盘不受影响）。'
                    '在 .env 里加 LLM_API_KEY=你的key 即可'))
        question = st.text_area('想问的问题（选填）',
                                placeholder='例：我是不是太频繁交易了？我的止损设置合理吗？',
                                height=90)
        model = st.text_input('模型名', value=llm.model_name())
        if st.button('生成 AI 复盘', disabled=not key_ok):
            with st.spinner('模型正在读你的交易记录……'):
                try:
                    text, usage = review.llm_review(df, question, model=model,
                                                    start_equity=equity)
                    st.markdown(text)
                    st.caption(f'模型：{usage["模型"]}｜用量：{usage["用量"]}')
                except Exception as e:
                    st.error(str(e))
        with st.expander('怎么知道该用哪个模型？'):
            st.caption('点下面的按钮会列出你这个硅基流动账号可用的模型，'
                       '挑一个填到上面的输入框里，或写进 .env 的 SILICONFLOW_MODEL。')
            if st.button('列出可用模型'):
                try:
                    ms = review.list_models()
                    st.write(f'共 {len(ms)} 个，前 30 个：')
                    st.code('\n'.join(ms[:30]))
                except Exception as e:
                    st.error(str(e))


# ============ 页签6：行情参考 ============
with tab6:
    st.subheader('行情参考')
    st.caption('公开数据，只做客观描述。这里没有买卖信号，也不预测价格。')
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        m_symbol = symbol_picker.pick('币种', key='mkt_symbol', default='BTCUSDT')
    m_exchange = c2.selectbox('数据源', ['自动', '币安', '欧易OKX', 'Bybit'])
    c3.write('')
    if c3.button('🔄 拉取最新数据'):
        with st.spinner('正在拉取公开行情……'):
            try:
                snap = market.snapshot(m_symbol, m_exchange)
                st.session_state['snap'] = snap
                ind = market.compute_indicators(snap.get('K线'), snap.get('标记价'))
                st.session_state['ind'] = ind
            except Exception as e:
                st.error(f'拉取失败：{e}')

    snap = st.session_state.get('snap')
    ind = st.session_state.get('ind') or {}
    if snap:
        st.caption(f'数据源：{snap.get("交易所")}｜获取时间：{snap.get("获取时间")}')
        m1, m2, m3, m4 = st.columns(4)
        m1.metric('标记价', f'{snap.get("标记价", 0):,.4f}')
        if snap.get('24h涨跌幅') is not None:
            m2.metric('24h涨跌', f'{snap["24h涨跌幅"]:.2f}%')
        if snap.get('资金费率') is not None:
            m3.metric('资金费率（8h）', f'{snap["资金费率"] * 100:.4f}%')
        if snap.get('多空比') is not None:
            m4.metric('账户多空比', f'{snap["多空比"]:.2f}')

        if ind:
            st.markdown('#### 波动与位置')
            n1, n2, n3, n4 = st.columns(4)
            if ind.get('ATR14百分比'):
                n1.metric('4h ATR', f'{ind["ATR14百分比"]:.2f}%', '你的止损要大于这个数')
            if ind.get('距近期高点百分比') is not None:
                n2.metric('距 30 根K线高点', f'{ind["距近期高点百分比"]:.2f}%')
            if ind.get('距近期低点百分比') is not None:
                n3.metric('距 30 根K线低点', f'+{ind["距近期低点百分比"]:.2f}%')
            if ind.get('距MA20百分比') is not None:
                n4.metric('距 MA20', f'{ind["距MA20百分比"]:.2f}%')

        st.markdown('#### 客观观察')
        for note in market.market_notes(snap, ind):
            st.markdown(f'- {note}')

        st.markdown('#### 快速试算：按当前价，止损放哪')
        st.caption('用当前 ATR 给你一个参考止损距离。这只是波动尺度的参考，不是建议。')
        if ind.get('ATR14'):
            atr = ind['ATR14']
            price = snap.get('标记价')
            st.write(f'价格 {price:,.4f}｜1 倍 ATR = ±{atr:,.4f}｜'
                     f'1.5 倍 ATR = ±{atr * 1.5:,.4f}｜2 倍 ATR = ±{atr * 2:,.4f}')
            st.caption('如果你的止损距离小于 1 倍 ATR，大概率会被日常波动扫掉。')
    else:
        st.info('点上面的「拉取最新数据」按钮获取行情。')

    st.divider()
    watchlist_ui.render(live_cfg)


# ============ 页签7：市场监控 ============
with tab7:
    monitor_ui.render(live_cfg)


# ============ 页签8：市场情报（新闻 + 链上）============
with tab8:
    st.subheader('市场情报')
    st.caption(
        '**实时新闻事件 + 链上数据。** 这两块都是「背景信息」，不是交易信号。\n\n'
        '新闻模块只做三件事：告诉你现在有什么高影响事件、这类事件历史上通常伴随波动放大、'
        '事后把异动和新闻对上。**它不预测新闻会让价格涨还是跌。**'
    )
    context_ui.render_news()
    st.divider()
    context_ui.render_onchain()


# ============ 页签9：多智能体判断 ============
with tab9:
    agents_ui.render(live_cfg)


# ============ 页签0：交易方案（放最前面，一眼就能看到）============
with tab1:
    plan_ui.render(live_cfg)
