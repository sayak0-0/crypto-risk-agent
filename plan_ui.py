# -*- coding: utf-8 -*-
"""「交易方案」页签：一键从零到完整方案。

一个按钮走完：四家不同厂商的模型分析 → 多空辩论 → 程序算结构位 → 出完整方案。
"""
import time

import pandas as pd
import streamlit as st

import agents
import llm
import plan as plan_mod
import tasks
import decision_log

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AR = True
except ImportError:
    HAS_AR = False

DIRECTION_OPTIONS = ['由 AI 判断（推荐）', '只看多', '只看空']


# ---------------- 后台执行（不能碰 st.*） ----------------

def _run_plan(symbol, exchange, equity, risk_pct, leverage, use_debate,
              force_dir, cfg, progress, news_context=None):
    """在后台线程里跑完整流程。

    ⚠️ 这个函数**绝对不能调用任何 st 的方法** —— 它跑在独立线程里，
    所有进度都通过 progress() 回调上报。
    """
    progress('正在拉取行情数据……')
    analysis = agents.analyze_symbol(symbol, exchange, on_progress=progress,
                                     enable_debate=use_debate,
                                     extra_context=news_context)
    if force_dir:
        a = dict(analysis)
        a['主持人'] = dict(a.get('主持人') or {}, 方向=force_dir)
        analysis = a
    progress(f'{llm.model_name("planner").split("/")[-1]}｜方案官｜正在选择止损、止盈并计算仓位')
    res = plan_mod.build_plan(
        symbol, analysis, equity=equity, risk_pct=risk_pct, leverage=leverage,
        fee_rate=float(cfg.get('手续费率', 0.0005)),
        mmr=float(cfg.get('维持保证金率', 0.005)),
        min_rr=float(cfg.get('最低盈亏比', 1.5)),
        user_forced=bool(force_dir))
    decision_log.record(symbol, analysis, res, analysis.get('市场状态'))
    progress('完成')
    return {'方案': res, '分析': analysis}


# ---------------- 方案渲染（可被其他页签复用） ----------------

def render_result(result, analysis=None):
    """渲染完整交易方案。抽出来是为了让「多币种扫描」也能就地显示。"""
    if not result:
        return

    # 双向方案（AI 方向不明确时）
    if result.get('双向'):
        ai = result.get('AI判断') or {}
        st.warning(
            f"**AI 没能确定方向**（判断「{ai.get('方向')}」，信心 {ai.get('信心')}）\n\n"
            f"{ai.get('说明')}"
        )
        st.caption('⚠️ 下面**两个方向**的参数都是程序按公式算准的（止损/止盈/仓位/爆仓价），'
                   '但「这一单该不该做」取决于你自己的判断 —— 工具不替你决定这个。')

        left, right = st.columns(2)
        for col, key, title in ((left, '做多', '📈 做多方案'),
                                 (right, '做空', '📉 做空方案')):
            side = result.get(key)
            with col:
                st.markdown(f'#### {title}')
                if not side:
                    st.info('这个方向算不出合理的止损位。')
                    continue
                st.markdown(f"**入场价**　{side['入场价']:,.8f}")
                st.markdown(f"**止损价**　{side['止损价']:,.8f}"
                            f"　（{side['止损依据']['名称']}，"
                            f"距入场 {side['止损依据']['距离百分比']:.2f}%）")
                st.markdown(f"**止盈价**　{side['止盈价']:,.8f}"
                            f"　（盈亏比 {side['止盈依据']['盈亏比']:g}:1）")
                pos = side['仓位']
                st.markdown(f"**建议数量**　{pos['建议数量']:,.2f}")
                st.markdown(f"**名义价值**　{pos['名义价值']:,.2f} U")
                st.markdown(f"**止损亏损**　{pos['止损时实际亏损']:,.2f} U"
                            f"（本金 {pos['止损时实际亏损比例']:.2f}%）")
                st.markdown(f"**估算爆仓**　{pos['爆仓价']:,.8f}"
                            + ('　✅' if not pos['爆仓先于止损'] else '　🚨 危险'))
                st.markdown(f"**纪律检查**　{side['纪律检查']['结论']}")
                mn = side.get('最小下单量')
                if mn and not mn['通过']:
                    st.error(mn['说明'])
                with st.expander('候选止损位 / 分批建仓'):
                    st.dataframe(pd.DataFrame(side['候选止损']), height=220)
                    for b in (side.get('分批建仓') or [])[:-1]:
                        st.caption(f"{b['批次']}　{b['价位']:,.8f}　{b['数量']:,.2f}　{b['占比']}")

        with st.expander('🧠 为什么 AI 判断不了方向'):
            st.markdown('**四位分析师的意见**')
            for a in (analysis or {}).get('分析师') or []:
                st.markdown(f"- **{a['_名称']}**（{a.get('_模型')}）："
                            f"{a.get('方向')} 信心 {a.get('信心')}")
                st.caption(f"　{a.get('核心理由')}")
            ch = (analysis or {}).get('主持人') or {}
            for k in ['共识', '分歧', '综合判断']:
                if ch.get(k):
                    st.markdown(f"**{k}**：{ch[k]}")
        return

    # 不可执行
    if not result.get('可执行'):
        st.warning('**没有生成可执行方案**')
        st.markdown(f"原因：{result.get('原因')}")
        st.caption('⚠️ 拒绝给方案是正常结果。信号不清时不下手，比硬编一个方案专业得多。')
        if result.get('候选止损'):
            with st.expander('程序算出的候选止损位'):
                st.dataframe(pd.DataFrame(result['候选止损']), height=300)
        return

    st.markdown(f"### {result['标的']}　{result['方向']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('杠杆', f"{result.get('杠杆', '—')}x")
    c2.metric('入场价', f"{result['入场价']:,.4f}")
    c3.metric('止损价', f"{result['止损价']:,.4f}",
              f"距入场 {result['止损依据']['距离百分比']:.2f}%")
    c4.metric('止盈价', f"{result['止盈价']:,.4f}",
              f"盈亏比 {result['止盈依据']['盈亏比']:g}:1")

    p = result['仓位']
    st.caption(f"方向倾向：{result.get('倾向') or result.get('方向')}（{result.get('倾向强度') or '中'}）")
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

    mn = result.get('最小下单量')
    if mn:
        (st.success if mn['通过'] else st.error)(f"**最小下单量**：{mn['说明']}")

    batches = result.get('分批建仓') or []
    if len(batches) > 1:
        st.markdown('#### 🪜 分批建仓建议')
        st.caption('**为什么分批**：一次性满仓没有容错空间。分批能摊薄成本、留子弹。'
                   '**关键约束：总风险仍然是本金 1%** —— 分批是为了分散，不是放大。')
        for b in batches[:-1]:
            x1, x2, x3, x4 = st.columns([1, 1.2, 1, 2])
            x1.markdown(f"**{b['批次']}**")
            x2.markdown(f"{b['价位']:,.4f}")
            x3.markdown(f"{b['数量']:.6f}")
            x4.caption(f"{b['占比']}｜{b['说明']}")
        last = batches[-1]
        st.info(f"**加权平均建仓成本：{last['价位']:,.4f}**"
                f"（总数量 {last['数量']:.6f}，名义价值 {last['名义价值']:,.2f} USDT）")

    st.markdown('#### 纪律检查')
    for chk in result['纪律检查']['明细']:
        icon = {'通过': '✅', '警告': '⚠️', '拒绝': '❌', '提示': '💡'}.get(chk['级别'], '•')
        st.markdown(f'{icon} **{chk["项目"]}** —— {chk["说明"]}')

    if result.get('兜底提示'):
        st.warning(result['兜底提示'])
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
            d = analysis.get('辩论')
            if d:
                st.markdown('**⚔️ 多空辩论**')
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


def start_task(symbol, cfg, equity=None, risk_pct=None, leverage=None,
               use_debate=True, force_dir=None, exchange='自动', news_context=None):
    """从任何页签启动一次方案生成（后台跑）。返回任务 ID。"""
    tid = tasks.start('交易方案', _run_plan,
                      symbol, exchange,
                      float(equity if equity is not None else cfg.get('本金', 1000.0)),
                      float(risk_pct if risk_pct is not None
                            else cfg.get('单笔风险百分比', 1.0)),
                      float(leverage if leverage is not None else cfg.get('杠杆', 10.0)),
                      bool(use_debate), force_dir, dict(cfg),
                      news_context=news_context)
    return tid


def render_progress(tid, key='pl_poll'):
    """显示后台任务进度。返回 (结果, 分析) 或 (None, None)。"""
    if not tid:
        return None, None
    stt = tasks.status(tid)
    state = stt.get('状态')
    if state in ('排队中', '运行中'):
        st.info(f"⏳ **正在后台运行**（{tid.split('-')[-1]}）\n\n"
                f"进度：{stt.get('进度') or '启动中'}\n\n"
                f"开始于：{stt.get('开始时间')}\n\n"
                f"**你可以随便切页签、抓新闻、看行情，不会中断。**")
        if HAS_AR:
            st_autorefresh(interval=5000, key=key)
        return None, None
    if state == '失败':
        err = str(stt.get('错误') or '')
        st.error('**这个任务失败了**')
        # 分情况给能照着做的建议
        if '连不上交易所' in err or 'ConnectionError' in err or 'SSL' in err:
            st.warning('👉 **是网络问题，不是工具坏了。** 上面写了解决办法。')
        elif '余额不足' in err or '402' in err:
            st.warning('👉 **是模型账号余额不足**，去硅基流动充值即可。'
                       '（不充值也能用：开仓前检查、交易日志、绩效看板、市场监控都不需要模型）')
        elif '超时' in err:
            st.warning('👉 **模型响应太慢超时了**，可以换个更快的模型，或稍后重试。')
        st.markdown(err)
        if stt.get('堆栈'):
            with st.expander('技术细节'):
                st.code(stt['堆栈'])
        return None, None
    if state == '完成':
        loaded = tasks.load_result(tid)
        if loaded:
            return loaded.get('方案'), loaded.get('分析')
        st.warning('任务完成了但结果读不出来。')
    return None, None


# ---------------- 主界面 ----------------

def render(cfg=None):
    cfg = cfg or {}
    st.subheader('📋 生成交易方案')
    st.caption('一个按钮走完全流程：**四家不同厂商的模型分析 → 多空辩论 → '
               '程序算结构位 → 出完整方案**。')
    st.info('**分工说清楚**：代码负责算（结构位、止损候选、盈亏比、仓位、爆仓价），'
            '模型只负责从代码算好的候选里**选**一个。'
            '**模型不允许凭空给价格** —— 编造的位置会被直接拒绝。')

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        symbol = __import__('symbol_picker').pick('币种', key='pl_sym',
                                                  default='BTCUSDT')
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
    st.caption('💡 **这个任务在后台跑** —— 跑的时候你可以随便点别的页签、抓新闻、'
               '看行情，**不会中断**。刷新页面或关掉再打开，结果仍然在。')

    if st.button('🚀 生成完整交易方案', type='primary', key='pl_run'):
        force = None
        if direction_pref != DIRECTION_OPTIONS[0]:
            force = '偏多' if '多' in direction_pref else '偏空'
        st.session_state['pl_task'] = start_task(
            symbol, cfg, equity, risk_pct, leverage, use_debate, force, exchange)
        st.rerun()

    tid = st.session_state.get('pl_task')
    if not tid:
        tid, _ = tasks.latest('交易方案')
        if tid:
            st.session_state['pl_task'] = tid

    result, analysis = render_progress(tid)
    if result:
        st.divider()
        st.success('✅ 方案已生成')
        render_result(result, analysis)
        st.caption(f'任务 ID：{tid}')
    elif not tid:
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