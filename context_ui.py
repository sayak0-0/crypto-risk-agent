# -*- coding: utf-8 -*-
"""「事件与链上」界面：新闻事件监控 + 链上数据。"""
import datetime as dt

import pandas as pd
import streamlit as st

import llm
import news
import onchain
from common import get_env


def render_onchain():
    st.markdown('### ⛓️ 链上数据')
    st.caption('免费数据源：CoinMetrics（交易所资金流）+ DefiLlama（稳定币供应）。'
               '每日更新，不是实时。')
    if st.button('🔄 拉取链上数据', key='oc_load'):
        with st.spinner('正在拉取……'):
            try:
                st.session_state['oc'] = onchain.snapshot()
            except Exception as e:
                st.session_state['oc_err'] = f'{type(e).__name__}: {e}'
    if st.session_state.get('oc_err'):
        st.error(st.session_state.pop('oc_err'))
    s = st.session_state.get('oc')
    if not s:
        st.info('点上面的按钮拉取链上数据。')
        return
    if s.get('错误'):
        st.error(s['错误'])
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('当日净流入', f"{s['当日净流入_亿']:+.2f} 亿",
              '流入为正=潜在抛压' if s['当日净流入_亿'] > 0 else '净流出=偏囤币')
    c2.metric('7日均净流入', f"{s['7日均净流入_亿']:+.2f} 亿")
    c3.metric('活跃地址', f"{s['活跃地址']:,}")
    if s.get('稳定币7日变化%') is not None:
        c4.metric('稳定币7日', f"{s['稳定币7日变化%']:+.2f}%")
    for line in s.get('解读', []):
        st.markdown(f'- {line}')

    with st.expander('近 30 天明细'):
        df = pd.DataFrame(s['历史'])
        cols = [c for c in ['date', '流入', '流出', '净流入', '活跃地址',
                            '稳定币7日变化%'] if c in df.columns]
        st.dataframe(df[cols], height=300)

    st.warning(
        '**⚠️ 链上数据的实测结论**：我做过 2 年数据的条件统计 + 4 段验证，'
        '**没有任何一个组合能达到「4 段全部为正」的可靠标准**。\n\n'
        '相对最好的是「活跃地址高于 7 日均 10%」（3/4 段为正，7-14天）和'
        '「稳定币 7 日减少」（2/2 段方向一致）。\n\n'
        '**把上面的数字当参考信息，不要当交易信号。**'
        '详细检验过程见 `链上数据回测.py`。'
    )


def render_news():
    st.markdown('### 📰 事件监控')
    st.caption('抓取公开 RSS（美联储 / SEC / CoinDesk / Cointelegraph 等），'
               '识别可能引发波动放大的事件。')

    st.info(
        '**它能做什么**：告诉你现在有什么高影响事件、这类事件历史上通常伴随波动放大，'
        '提醒你避开或减小仓位。\n\n'
        '**它不做什么**：**不预测新闻会让价格涨还是跌**。'
        '市场经常反着走（利好出尽），反应发生在几秒内，'
        '而且同一条新闻在不同市场状态下效果相反。'
    )

    c1, c2 = st.columns([1, 3])
    if c1.button('🔄 抓取最新新闻', key='nw_load'):
        with st.spinner('正在抓取……'):
            try:
                st.session_state['news'] = news.scan(limit=80)
            except Exception as e:
                st.session_state['news_err'] = f'{type(e).__name__}: {e}'
    live = c2.toggle('自动刷新（每 5 分钟）', value=False, key='nw_auto')

    if st.session_state.get('news_err'):
        st.error(st.session_state.pop('news_err'))
    s = st.session_state.get('news')
    if not s:
        st.info('点上面的按钮抓取新闻。')
        return

    st.caption(f"抓取时间 {s['抓取时间']}｜共 {s['总条数']} 条，"
               f"其中高影响事件 {s['高影响条数']} 条")
    if s.get('错误'):
        st.caption('部分源不可用：' + '；'.join(s['错误']))

    note = news.risk_note(s, has_position=bool(_has_position()))
    if note['级别'] == '正常':
        st.success(note['提示'])
    elif note['级别'] == '警告':
        st.error('🚨 ' + note['提示'])
    else:
        st.warning('⚠️ ' + note['提示'])

    if s['高影响事件']:
        st.markdown('#### 高影响事件')
        for it in s['高影响事件'][:15]:
            with st.container(border=True):
                st.markdown(f"**[{'/'.join(it['影响类别'])}] {it['标题']}**")
                meta = f"{it['来源']}"
                if it.get('时间'):
                    meta += f" · {it['时间']}"
                st.caption(meta)
                for cat in it['影响类别']:
                    adv = news.IMPACT_ADVICE.get(cat)
                    if adv:
                        st.caption(f"→ {adv}")
                if it.get('链接'):
                    st.markdown(f"[原文]({it['链接']})")
    else:
        st.success('当前没有检测到高影响事件。')

    with st.expander(f"全部新闻（{s['总条数']} 条）"):
        df = pd.DataFrame([{ '时间': x.get('时间', ''), '来源': x['来源'],
                             '标题': x['标题'], '强度': x.get('强度', ''),
                             '类别': '/'.join(x.get('影响类别') or [])}
                           for x in s['全部新闻']])
        st.dataframe(df, height=400)

    st.divider()
    st.markdown('#### 让模型梳理这些新闻（可选）')
    key_ok = bool(get_env('LLM_API_KEY') or get_env('SILICONFLOW_API_KEY'))
    st.caption('模型只做「事实梳理 + 风险提示」，提示词里明确禁止它预测涨跌方向。'
               if key_ok else '未配置模型 Key，此功能不可用。')
    if st.button('🤖 梳理这几条新闻', disabled=not key_ok, key='nw_llm'):
        with st.spinner('模型正在梳理……'):
            r = news.llm_interpret(s['高影响事件'] or s['全部新闻'][:8])
        if not r:
            st.warning('没有可梳理的新闻。')
        elif r.get('错误'):
            st.error(r['错误'])
        else:
            for k in ['事实梳理', '可能受影响的方向', '波动预期',
                      '风险提示', '我不确定的地方']:
                if r.get(k):
                    st.markdown(f'**{k}**：{r[k]}')
            st.caption(f"模型：{r.get('_模型')}｜用量：{r.get('_用量')}")


def _has_position():
    try:
        import monitor
        return bool(monitor.load_positions())
    except Exception:
        return False