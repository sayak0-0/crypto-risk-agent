# -*- coding: utf-8 -*-
"""对话式主界面：左边历史任务，右边一个问答窗口。

布局参考 Codex / DeepSeek：
    ┌──────────────┬──────────────────────────┐
    │ ＋ 新对话      │   对话内容 / 结果          │
    │ 今天          │                          │
    │  · BTC 方案   │                          │
    │ 工具          │   [输入框]                │
    └──────────────┴──────────────────────────┘
"""
import datetime as dt
import time

import pandas as pd
import streamlit as st

import chat
import plan_ui
import tasks
import tasks as _tasks

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AR = True
except ImportError:
    HAS_AR = False


# ---------------- 工具函数 ----------------

def _hist_items():
    """历史任务，按时间倒序。"""
    d = _tasks._read_all()
    items = []
    for tid, s in d.items():
        if s.get('任务名') == '测试任务' or '自测' in str(s.get('任务名', '')):
            continue                                  # 过滤自测残留
        items.append((tid, s))
    items.sort(key=lambda x: x[1].get('开始时间', ''), reverse=True)
    return items[:30]


def _short_summary(tid, s):
    """给历史列表用的一行摘要。"""
    state = s.get('状态')
    if state == '失败':
        return '❌ 失败'
    if state in ('排队中', '运行中'):
        return '⏳ 进行中'
    r = _tasks.load_result(tid)
    if not r:
        return '（无结果）'
    p = r.get('方案')
    if p:
        if p.get('双向'):
            return f"✅ {p.get('标的') or ''} 双向方案"
        if p.get('可执行'):
            return f"✅ {p.get('标的') or ''} {p.get('方向') or ''}"
        return '⚠️ 未出方案'
    return '✅ 完成'


def _hist_label(tid, s):
    """历史列表按钮的文字。"""
    name = s.get('任务名', '任务')
    t = str(s.get('开始时间', ''))[11:16]
    return f"{t}  {name}"


def _group_by_day(items):
    """按天分组。"""
    out = {}
    today = dt.date.today()
    for tid, s in items:
        raw = str(s.get('开始时间', ''))[:10]
        try:
            d = dt.date.fromisoformat(raw)
            label = ('今天' if d == today else
                     '昨天' if d == today - dt.timedelta(days=1) else raw)
        except Exception:
            label = '更早'
        out.setdefault(label, []).append((tid, s))
    return out


# ---------------- 渲染各种结果 ----------------

def _render_quote(res):
    snap, ind = res['快照'], res['指标']
    c = st.columns(4)
    c[0].metric('现价', f"{snap['标记价']:,.4f}")
    if snap.get('24h涨跌幅') is not None:
        c[1].metric('24h涨跌', f"{snap['24h涨跌幅']:+.2f}%")
    if snap.get('资金费率') is not None:
        c[2].metric('资金费率(8h)', f"{snap['资金费率'] * 100:+.4f}%",
                    '常规基准约 0.01%', delta_color='off')
    if snap.get('多空比') is not None:
        c[3].metric('账户多空比', f"{snap['多空比']:.2f}")
    if ind:
        d = st.columns(4)
        if ind.get('ATR14百分比'):
            d[0].metric('4h ATR', f"{ind['ATR14百分比']:.2f}%",
                        '止损要大于这个数', delta_color='off')
        if ind.get('距MA20百分比') is not None:
            d[1].metric('距 MA20', f"{ind['距MA20百分比']:+.2f}%")
        if ind.get('距近期高点百分比') is not None:
            d[2].metric('距30根高点', f"{ind['距近期高点百分比']:+.2f}%")
        if ind.get('距近期低点百分比') is not None:
            d[3].metric('距30根低点', f"+{ind['距近期低点百分比']:.2f}%")
    st.caption(f"数据源 {snap.get('交易所')}｜{snap.get('获取时间')}")


def _render_positions(res):
    if res.get('提示'):
        st.info(res['提示'])
        return
    rows = res['持仓']
    for p in rows:
        if p.get('错误'):
            st.warning(f"{p.get('币种')}：拉不到行情（{p['错误']}）")
            continue
        with st.container(border=True):
            c = st.columns(5)
            c[0].markdown(f"**{p['币种']}**  \n{p.get('方向')} {p.get('杠杆')}x")
            c[1].metric('现价', f"{p['当前价']:,.4f}")
            c[2].metric('浮盈亏', f"{p['浮动盈亏']:+,.2f} U",
                        delta_color='normal')
            if p.get('距爆仓百分比') is not None:
                c[3].metric('距爆仓', f"{p['距爆仓百分比']:.2f}%",
                            '⚠️ 很近' if p['距爆仓百分比'] < 10 else '安全',
                            delta_color='off')
            if p.get('距止损百分比') is not None:
                c[4].metric('距止损', f"{p['距止损百分比']:.2f}%")
            if p.get('距爆仓百分比') is not None and p['距爆仓百分比'] < 10:
                st.error(f"🚨 {p['币种']} 距离估算爆仓只剩 {p['距爆仓百分比']:.2f}%，马上处理")


def _render_scan(res):
    if res.get('错误'):
        st.error(res['错误'])
        return
    st.caption(f"扫描时间 {res['扫描时间']}｜全市场 {res['全市场合约数']} 个合约")
    cats = res['分类']
    if not cats:
        st.info('这次没扫到明显的异动。')
        return
    tabs = st.tabs(list(cats.keys()))
    for tab, (name, v) in zip(tabs, cats.items()):
        with tab:
            st.caption(v['说明'])
            df = pd.DataFrame(v['数据'])
            cols = [c for c in ['币种', '标记价', '24h涨跌%', '资金费率%',
                                '成交额排名', '筛选理由'] if c in df.columns]
            show = df[cols].copy()
            for c in ['24h涨跌%', '资金费率%']:
                if c in show.columns:
                    show[c] = show[c].map(lambda x: f'{x:+.2f}%' if pd.notna(x) else '—')
            if '标记价' in show.columns:
                show['标记价'] = show['标记价'].map(lambda x: f'{x:,.6f}' if pd.notna(x) else '—')
            st.dataframe(show, height=min(400, 80 + 33 * len(show)))
    st.caption('⚠️ 这是**事实筛选**不是推荐买入 —— 费率极端/涨跌异动在样本外'
               '不具备稳定预测力，只是帮你收敛注意力。')


def _render_news(res):
    """展示高影响新闻，只谈事实与波动风险，不给利好利空。"""
    scan = res.get('扫描') or {}
    note = res.get('风险') or {}
    text = note.get('提示') or '暂时没有可用的新闻风险提示。'
    level = note.get('级别')
    if level == '警告':
        st.warning(text)
    elif level == '提示':
        st.info(text)
    else:
        st.success(text)

    c = st.columns(3)
    c[0].metric('抓取新闻', f"{scan.get('总条数', 0)} 条")
    c[1].metric('高影响', f"{scan.get('高影响条数', 0)} 条")
    c[2].metric('抓取时间', str(scan.get('抓取时间', '—'))[11:16] or '—')

    cats = scan.get('按类别') or {}
    for name, info in list(cats.items())[:4]:
        with st.expander(f"**{name}** · {info.get('条数', 0)} 条", expanded=True):
            if info.get('建议'):
                st.caption(info['建议'])
            for title in (info.get('示例') or []):
                st.markdown(f'- {title}')

    events = scan.get('高影响事件') or []
    if events:
        st.markdown('**高影响新闻**')
        for item in events[:12]:
            title = item.get('标题') or '（无标题）'
            link = item.get('链接')
            if link:
                st.markdown(f"- [{title}]({link})")
            else:
                st.markdown(f'- {title}')
            cats_txt = ' / '.join(item.get('影响类别') or [])
            meta = ' · '.join(x for x in [item.get('来源'), cats_txt, str(item.get('时间') or '')[:25]] if x)
            if meta:
                st.caption(meta)

    errors = scan.get('错误') or []
    if errors:
        with st.expander(f'部分数据源暂时不可用（{len(errors)} 个）'):
            for err in errors:
                st.caption(str(err))
    st.caption('新闻只用于判断波动风险，不代表利好或利空，也不给出涨跌方向。')


def _render_review(res):
    if not res.get('有数据'):
        st.info('还没有已平仓的交易记录。先记几笔再来复盘。')
        return
    for f in res['结论']:
        st.markdown(f)
        st.divider()


def _render_dashboard(res):
    s = res['指标']
    if not s or s.get('交易笔数', 0) == 0:
        st.info('还没有已平仓的交易记录。')
        return
    c = st.columns(4)
    c[0].metric('总净盈亏', f"{s['总净盈亏']:+,.2f} U")
    c[1].metric('胜率', f"{s['胜率']:.1f}%", f"{s['盈利笔数']}胜/{s['亏损笔数']}负")
    c[2].metric('盈亏比', f"{s['盈亏比']:.2f}" if s.get('盈亏比') else '—')
    c[3].metric('盈利因子', f"{s['盈利因子']:.2f}" if s.get('盈利因子') else '—',
                '>1.5 才健康', delta_color='off')
    d = st.columns(4)
    d[0].metric('期望值/笔', f"{s['期望值']:+,.2f} U")
    d[1].metric('平均R', f"{s['平均R']:.2f}R" if s.get('平均R') is not None else '—')
    d[2].metric('最大回撤', f"{s['最大回撤']:,.2f} U")
    d[3].metric('总手续费', f"{s['总手续费']:,.2f} U")
    for col, key in (('按币种', '按币种'), ('按情绪', '按情绪')):
        g = res.get(key)
        if g is not None and len(g):
            st.markdown(f'**{col}**')
            st.dataframe(g, height=min(240, 60 + 33 * len(g)))


def _render_result(intent, res):
    """按意图渲染结果。"""
    if intent == 'plan':
        tid = res['task_id']
        st.session_state['chat_task'] = tid
        st.session_state['chat_task_sym'] = res.get('币种')
        return
    t = res.get('类型')
    if t == '行情':
        _render_quote(res)
    elif t == '持仓':
        _render_positions(res)
    elif t == '扫描':
        _render_scan(res)
    elif t == '新闻':
        _render_news(res)
    elif t == '复盘':
        _render_review(res)
    elif t == '绩效':
        _render_dashboard(res)
    elif t == '对话':
        st.markdown(res['内容'])
    else:
        st.write(res)


# ---------------- 左侧栏 ----------------

def _sidebar(cfg):
    with st.sidebar:
        st.markdown('### 📉 合约交易助手')

        if st.button('＋  新对话', use_container_width=True, type='primary',
                     key='new_chat'):
            st.session_state['chat'] = []
            st.session_state.pop('chat_task', None)
            st.session_state.pop('view_task', None)
            st.rerun()

        st.divider()

        # 正在跑的任务（置顶）
        running = tasks.running_tasks()
        if running:
            st.caption('⚡ 进行中')
            for tid, s in running.items():
                st.markdown(f"**{_hist_label(tid, s)}**")
                st.caption(f"　{s.get('进度', '')}")

        # 历史任务
        items = _hist_items()
        if items:
            st.caption('历史任务')
            for day, group in _group_by_day(items).items():
                st.caption(f'　{day}')
                for tid, s in group[:6]:
                    label = f"{_hist_label(tid, s)}  ·  {_short_summary(tid, s)}"
                    if st.button(label, key=f'h_{tid}', use_container_width=True):
                        st.session_state['view_task'] = tid
                        st.rerun()
        else:
            st.caption('还没有历史任务')

        st.divider()
        st.caption('工具')
        if st.button('📒 交易日志 / 绩效 / 监控', use_container_width=True,
                     key='to_classic'):
            st.session_state['ui_mode'] = 'classic'
            st.rerun()
        if st.button('⚙️ 风控设置', use_container_width=True, key='to_cfg'):
            st.session_state['show_cfg'] = not st.session_state.get('show_cfg', False)
            st.rerun()

        if st.session_state.get('show_cfg'):
            _cfg_form(cfg)

        st.divider()
        with st.expander('ℹ️ 这个工具不做什么'):
            st.caption(
                '**不预测涨跌。** 我用 42 个模型、600+ 次调用验证过：'
                '方向判断准确率 54-61%，和抛硬币没有统计差异。\n\n'
                '**不接交易所下单。** 只能只读你的持仓。\n\n'
                '它只做三件事：**把数字算准、把风险摆明、把纪律拦住。**'
            )


def _cfg_form(cfg):
    from common import save_config
    st.caption('风控参数')
    eq = st.number_input('本金 USDT', min_value=1.0, value=float(cfg.get('本金', 1000.0)),
                         step=100.0, key='c_eq')
    rp = st.number_input('单笔风险 %', min_value=0.1, max_value=20.0,
                         value=float(cfg.get('单笔风险百分比', 1.0)), step=0.1, key='c_rp')
    lv = st.number_input('杠杆', min_value=1.0, max_value=125.0,
                         value=float(cfg.get('杠杆', 10.0)), step=1.0, key='c_lv')
    if st.button('保存', key='c_save'):
        save_config(dict(cfg, 本金=eq, 单笔风险百分比=rp, 杠杆=lv))
        st.success('已保存')
        st.rerun()


# ---------------- 主区域 ----------------

def _inject_style():
    st.markdown("""
    <style>
      .block-container {padding-top: 1.15rem; padding-bottom: 6rem; max-width: 1120px;}
      [data-testid="stHeader"] {background: transparent;}
      [data-testid="stToolbar"], .stDeployButton {display: none;}
      [data-testid="stSidebar"] {border-right: 1px solid rgba(128,128,128,.18);}
      [data-testid="stChatMessage"] {padding: .25rem .1rem;}
      div[data-testid="stChatInput"] textarea {min-height: 52px;}
    </style>
    """, unsafe_allow_html=True)


def render(cfg):
    _inject_style()
    _sidebar(cfg)

    st.caption('💬 直接说你想要什么 —— 比如「帮我看看 BTC 有没有机会」')

    # 历史任务详情
    view = st.session_state.get('view_task')
    if view:
        stt = tasks.status(view)
        st.markdown(f"### 历史任务：{stt.get('任务名', '')}　{stt.get('开始时间', '')}")
        if st.button('← 返回对话', key='back_chat'):
            st.session_state.pop('view_task', None)
            st.rerun()
        st.divider()
        if stt.get('状态') == '失败':
            st.error(stt.get('错误'))
        else:
            r = tasks.load_result(view)
            if r and r.get('方案'):
                plan_ui.render_result(r['方案'], r.get('分析'))
            else:
                st.info('这个任务没有可显示的结果。')
        return

    # 对话历史
    msgs = st.session_state.get('chat') or []
    for m in msgs:
        with st.chat_message('user' if m['role'] == 'user' else 'assistant'):
            if m['role'] == 'user':
                st.markdown(m['content'])
            else:
                _render_result(m.get('intent', 'chat'), m['content'])

    # 正在跑的方案任务
    tid = st.session_state.get('chat_task')
    if tid:
        with st.chat_message('assistant'):
            sym = st.session_state.get('chat_task_sym') or ''
            res, ana = plan_ui.render_progress(tid, key='chat_poll')
            if res:
                st.success(f'✅ {sym} 的方案已生成')
                plan_ui.render_result(res, ana)

    # 空状态：快捷按钮
    if not msgs and not tid:
        st.markdown('#### 想做什么？')
        acts = chat.quick_actions()
        cols = st.columns(4)
        for i, (label, prompt) in enumerate(acts):
            if cols[i % 4].button(label, use_container_width=True, key=f'qa_{i}'):
                st.session_state['_pending_prompt'] = prompt
                st.rerun()

        st.markdown('##### 常用币种 · 点一下直接分析')
        symbols = [
            ('BTC 比特币', 'BTCUSDT'), ('ETH 以太坊', 'ETHUSDT'),
            ('SOL', 'SOLUSDT'), ('BNB', 'BNBUSDT'),
            ('XRP', 'XRPUSDT'), ('DOGE', 'DOGEUSDT'),
        ]
        sym_cols = st.columns(6)
        for i, (label, symbol) in enumerate(symbols):
            if sym_cols[i].button(label, use_container_width=True, key=f'sym_{symbol}'):
                st.session_state['_pending_prompt'] = f'帮我分析一下 {symbol}'
                st.rerun()

    # 输入框
    pending = st.session_state.pop('_pending_prompt', None)
    text = st.chat_input('说点什么…（例：帮我分析一下 BTC / 扫描有什么异动 / 我的持仓怎么样）')
    if not text and pending:
        text = pending
    if text:
        msgs.append({'role': 'user', 'content': text})
        with st.spinner('处理中…'):
            try:
                intent, res = chat.dispatch(text, cfg)
            except Exception as e:
                intent, res = 'chat', {'类型': '对话',
                                       '内容': f'⚠️ 出错了：`{type(e).__name__}: {e}`'}
        msgs.append({'role': 'assistant', 'content': res, 'intent': intent})
        st.session_state['chat'] = msgs
        st.rerun()