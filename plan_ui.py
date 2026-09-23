# -*- coding: utf-8 -*-
"""「交易方案」独立页签：一键从零到完整方案。

为什么单独出来：原来「生成交易方案」藏在多智能体页签底部，
而且必须先跑完一次分析才出现 —— 用户根本找不到，也不知道要往下滚。
这个页签把「分析 → 辩论 → 算数字 → 出方案」串成一条线，一个按钮走完。
"""
import time

import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AR = True
except ImportError:
    HAS_AR = False

import agents
import llm
import risk
import tasks
from common import get_env

DIRECTION_OPTIONS = ['由 AI 判断（推荐）', '只看多', '只看空']


def _run_plan(symbol, exchange, equity, risk_pct, leverage, use_debate,
              force_dir, cfg, progress):
    """在后台线程里跑完整流程。

    ⚠️ 这个函数**绝对不能调用任何 st 的方法** —— 它跑在独立线程里，
    Streamlit 的上下文在主线程。所有进度都通过 progress() 回调上报。
    """
    import plan as plan_mod
    progress('正在拉取行情数据……')
    analysis = agents.analyze_symbol(symbol, exchange, on_progress=progress,
                                     enable_debate=use_debate)
    if force_dir:
        a = dict(analysis)
        a['主持人'] = dict(a.get('主持人') or {}, 方向=force_dir)
        analysis = a
    progress('正在计算结构位，并让方案官选止损……')
    res = plan_mod.build_plan(
        symbol, analysis, equity=equity, risk_pct=risk_pct, leverage=leverage,
        fee_rate=float(cfg.get('手续费率', 0.0005)),
        mmr=float(cfg.get('维持保证金率', 0.005)),
        min_rr=float(cfg.get('最低盈亏比', 1.5)))
    progress('完成')
    return {'方案': res, '分析': analysis}


def render(cfg=None):
    cfg = cfg or {}
    st.subheader('📋 生成交易方案')
    st.caption(
        '一个按钮走完全流程：**四家不同厂商的模型分析 → 多空辩论 → 程序算结构位 → 出完整方案**。'
    )

    st.info(
        '**分工说清楚**：代码负责算（结构位、止损候选、盈亏比、仓位、爆仓价），'
        '模型只负责从代码算好的候选里**选**一个。'
        '**模型不允许凭空给价格** —— 编造的位置会被直接拒绝。'
    )

    # ---------- 参数 ----------
    c1, c2, c3, c4 = st.columns(4)
    symbol = c1.text_input('币种', value='BTC', key='pl_sym')
    equity = c2.number_input('本金（USDT）', min_value=1.0,
                             value=float(cfg.get('本金', 1000.0)), step=100.0, key='pl_eq')
    risk_pct = c3.number_input('单笔风险（%）', min_value=0.1, max_value=10.0,
                               value=float(cfg.get('单笔风险百分比', 1.0)),
                               step=0.1, key='pl_rp')
    leverage = c4.number_input('杠杆', min_value=1.0, max_value=125.0,
                               value=float(cfg.get('杠杆', 10.0)), step=1.0, key='pl_lv')

    c5, c6, c7 = st.columns(3)
    use_debate = c5.toggle('⚔️ 开启多空辩论', value=True, key='pl_debate',
                           help='多 4 次模型调用，但能暴露对立观点')
    direction_pref = c6.selectbox('方向偏好', DIRECTION_OPTIONS, key='pl_dir')
    exchange = c7.selectbox('数据源', ['自动', '币安', '欧易OKX', 'Bybit'], key='pl_ex')

    est = 5 + (4 if use_debate else 0) + 1
    st.caption(f'预计 {est} 次模型调用｜'
               f'分析师：{" / ".join(m.split("/")[-1] for m in llm.analyst_models())}｜'
               f'主持人：{llm.model_name("chair")}')

    # ---------- 一键生成（后台跑，不阻塞界面）----------
    st.caption(
        '💡 **这个任务在后台跑** —— 跑的时候你可以随便点别的页签、抓新闻、看行情，'
        '**不会中断**。刷新页面或关掉浏览器再打开，结果仍然在。'
    )

    if st.button('🚀 生成完整交易方案', type='primary', key='pl_run'):
        force = None
        if direction_pref != DIRECTION_OPTIONS[0]:
            force = '偏多' if '多' in direction_pref else '偏空'
        tid = tasks.start('交易方案', _run_plan,
                          symbol, exchange, float(equity), float(risk_pct),
                          float(leverage), bool(use_debate), force, dict(cfg))
        st.session_state['pl_task'] = tid
        st.rerun()

    # 找当前任务：优先用本次会话的，否则找最近一个
    tid = st.session_state.get('pl_task')
    if not tid:
        tid, _ = tasks.latest('交易方案')
        if tid:
            st.session_state['pl_task'] = tid

    result = None
    analysis = None
    if tid:
        stt = tasks.status(tid)
        state = stt.get('状态')
        if state in ('排队中', '运行中'):
            st.info(f"⏳ **正在后台运行**（任务 {tid.split('-')[-1]}）\n\n"
                    f"当前进度：{stt.get('进度') or '启动中'}\n\n"
                    f"开始时间：{stt.get('开始时间')}")
            st.caption('你可以随便切页签、抓新闻。想看最新进度就回到这一页。')
            if HAS_AR:
                st_autorefresh(interval=5000, key='pl_poll')
            else:
                if st.button('🔄 刷新进度', key='pl_refresh'):
                    st.rerun()
        elif state == '失败':
            st.error(f"❌ 后台任务失败：{stt.get('错误')}")
            if stt.get('堆栈'):
                with st.expander('技术细节'):
                    st.code(stt['堆栈'])
        elif state == '完成':
            loaded = tasks.load_result(tid)
            if loaded:
                result = loaded.get('方案')
                analysis = loaded.get('分析')
                st.success(f"✅ 已完成（{stt.get('开始时间')}）")
            else:
                st.warning('任务完成了但结果读不出来。')
        st.caption(f'任务 ID：{tid}')

    if not result:
        st.divider()
        st.markdown('#### 还没有生成方案')
        st.caption('填好上面的参数，点 **🚀 生成完整交易方案**。')
        st.markdown(
            '**输出会包含**：\n'
            '- 方向（四家不同厂商的模型 + 多空辩论的结论）\n'
            '- 入场价 / 止损价（附为什么选这个位置）/ 止盈价\n'
            '- 精确仓位 / 止损时亏损金额 / 爆仓价\n'
            '- 纪律检查 + 这个方案在什么情况下失效'
        )
        return

    if not result.get('可执行'):
        st.warning('**没有生成可执行方案**')
        st.markdown(f"原因：{result.get('原因')}")
        st.caption('⚠️ 拒绝给方案是正常结果。信号不清时不下手，比硬编一个方案专业得多。'
                   '如果你想强制要一个方向，把上面的「方向偏好」改成只看多或只看空。')
        if result.get('候选止损'):
            with st.expander('程序算出的候选止损位'):
                st.dataframe(pd.DataFrame(result['候选止损']), height=300)
        return

    # ---------- 方案 ----------
    st.divider()
    import plan as plan_mod
    st.markdown(f"### {result['标的']}　{result['方向']}")
    c1, c2, c3 = st.columns(3)
    c1.metric('入场价', f"{result['入场价']:,.4f}")
    c2.metric('止损价', f"{result['止损价']:,.4f}",
              f"距入场 {result['止损依据']['距离百分比']:.2f}%")
    c3.metric('止盈价', f"{result['止盈价']:,.4f}",
              f"盈亏比 {result['止盈依据']['盈亏比']:g}:1")

    p = result['仓位']
    c4, c5, c6, c7 = st.columns(4)
    c4.metric('建议数量', f"{p['建议数量']:.6f}")
    c5.metric('名义价值', f"{p['名义价值']:,.2f} U")
    c6.metric('止损时亏损', f"{p['止损时实际亏损']:,.2f} U",
              f"本金 {p['止损时实际亏损比例']:.2f}%", delta_color='inverse')
    c7.metric('达到止盈赚', f"{p['达到目标的盈利']:,.2f} U")

    c8, c9, c10 = st.columns(3)
    c8.metric('占用保证金', f"{p['占用保证金']:,.2f} U",
              f"本金 {p['保证金占本金比例']:.1f}%")
    c9.metric('估算爆仓价', f"{p['爆仓价']:,.4f}",
              '✅ 在止损之外' if not p['爆仓先于止损'] else '🚨 危险')
    c10.metric('预估手续费', f"{p['预估手续费']:,.2f} U")

    st.error(f"⚠️ **这个方案在什么情况下失效**：{result.get('可执行条件')}")
    st.warning(f"**主要风险**：{result.get('主要风险')}")

    # 最小下单量检查（本金小的时候很关键）
    mn = result.get('最小下单量')
    if mn:
        if mn['通过']:
            st.success(f"✅ **最小下单量检查**：{mn['说明']}")
        else:
            st.error(f"❌ **最小下单量检查**：{mn['说明']}")

    # 分批建仓建议
    batches = result.get('分批建仓') or []
    if len(batches) > 1:
        st.markdown('#### 🪜 分批建仓建议')
        st.caption(
            '**为什么分批**：一次性满仓没有容错空间。分批能摊薄成本、留子弹应对不利走势。'
            '**关键约束：总风险仍然是本金 1%** —— 分批不是为了放大风险，'
            '是把同样的风险分散到不同价位。'
        )
        for b in batches[:-1]:
            c1, c2, c3, c4 = st.columns([1, 1.2, 1, 2])
            c1.markdown(f"**{b['批次']}**")
            c2.markdown(f"{b['价位']:,.4f}")
            c3.markdown(f"{b['数量']:.6f}")
            c4.caption(f"{b['占比']}｜{b['说明']}")
        last = batches[-1]
        st.info(f"**加权平均建仓成本：{last['价位']:,.4f}**"
                f"（总数量 {last['数量']:.6f}，名义价值 {last['名义价值']:,.2f} USDT）")
        st.caption('⚠️ 如果价格直接朝有利方向走了，第2、3批可能不会成交 —— '
                   '那说明你只用 30% 仓位吃到了行情。这是分批的必然代价。')

    st.markdown('#### 纪律检查')
    for c in result['纪律检查']['明细']:
        icon = {'通过': '✅', '警告': '⚠️', '拒绝': '❌', '提示': '💡'}.get(c['级别'], '•')
        st.markdown(f'{icon} **{c["项目"]}** —— {c["说明"]}')

    st.markdown('#### 为什么选这个止损位')
    st.markdown(f"**{result['止损依据']['名称']}**　{result['止损依据']['价格']:,.4f}")
    st.caption(result['止损依据']['说明'])

    with st.expander('📐 程序算出的全部候选止损位（模型只能从这里选）'):
        st.dataframe(pd.DataFrame(result['候选止损']), height=340)
        st.caption('每个候选都是代码按公式算的，不是模型编的。')

    if analysis:
        with st.expander('🧠 这个方案背后的分析过程'):
            st.markdown('**四位分析师（四家不同厂商）**')
            for a in analysis.get('分析师') or []:
                st.markdown(f"- **{a['_名称']}**（{a.get('_模型')}）："
                            f"{a.get('方向')} 信心{a.get('信心')}")
                st.caption(f"　{a.get('核心理由')}")
            if analysis.get('辩论'):
                st.markdown('**⚔️ 多空辩论**')
                d = analysis['辩论']
                for side, k1, k2 in [('📈 看多', '看多第一轮', '看多反驳'),
                                      ('📉 看空', '看空第一轮', '看空反驳')]:
                    x1, x2 = d.get(k1) or {}, d.get(k2) or {}
                    st.markdown(f"**{side}**")
                    st.caption(f"论证：{str(x1.get('核心论证'))[:200]}")
                    st.caption(f"自曝弱点：{x1.get('我的弱点')}")
                    st.caption(f"反驳后：{x2.get('我改变了吗')}")
            chair = analysis.get('主持人') or {}
            if chair.get('数据核对'):
                st.info(f"🔍 数据核对：{chair['数据核对']}")

    st.caption('所有数字由程序计算，模型只做选择性判断')