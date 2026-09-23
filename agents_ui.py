# -*- coding: utf-8 -*-
"""「多智能体判断」页签的界面。"""
import pandas as pd
import streamlit as st

from common import fmt_usdt

import agents
import llm
import market
import plan


def _dir_badge(d):
    return {'偏多': '📈 偏多', '偏空': '📉 偏空',
            '中性': '➖ 中性', '无法判断': '❓ 无法判断'}.get(d, '❓ 未知')


def _conf_bar(c):
    try:
        c = int(c)
    except (TypeError, ValueError):
        return '—'
    return f'{c}/100'


def render(cfg=None):
    st.subheader('多智能体判断')
    st.warning(
        '**先说清楚**：多智能体 LLM 判断市场方向，实测准确率通常在抛硬币附近。'
        '这个功能的真实价值不是「预测未来」，而是'
        '**逼你同时从技术、资金、情绪、风控四个角度**'
        '看一遍，并且给出「什么情况下这个判断是错的」。\n\n'
        '**请务必用下面的「判断记录」里的真实准确率来决定要不要参考它**，'
        '而不是看它说得像不像那么回事。'
    )

    info = llm.describe()
    st.caption(f'当前模型服务：{info["服务地址"]}｜模型：{info["模型"]}｜Key：{info["Key"]}')
    if not info['已配置']:
        st.error('没有配置模型 API Key，这个功能用不了。'
                 '在 .env 里加 LLM_API_KEY=你的key（规则复盘和监控不需要它，照常可用）。')
        return

    # ---------- 控制区 ----------
    c1, c2, c3 = st.columns([2, 1, 2])
    symbol = c1.text_input('币种', value='BTC', key='ag_sym')
    exchange = c2.selectbox('数据源', ['自动', '币安', '欧易OKX', 'Bybit'], key='ag_ex')
    st.caption('每次分析 = 4 位分析师 + 1 位主持人 = 5 次模型调用，'
               '大约消耗 1 万 tokens。按 DeepSeek-V3 的价格算大约几分钱。')

    # ---------- 多模型协商配置 ----------
    with st.expander('🎭 多模型协商（四个分析师用四家不同厂商的模型）',
                     expanded=False):
        st.caption(
            '**为什么要用四个不同的模型**：如果四个分析师都用同一个模型，'
            '它们的「分歧」只是数据切片造成的，不是真观点不同。'
            '用四家不同厂商的模型，训练背景不同，分歧才是真分歧。'
        )
        cur = llm.analyst_models()
        role_names = ['技术面分析师', '资金面分析师', '情绪面分析师', '风控官']
        cols = st.columns(4)
        picks = []
        for i, (col, rn) in enumerate(zip(cols, role_names)):
            picks.append(col.text_input(rn, value=cur[i], key=f'am_{i}'))
        if st.button('💾 保存这四个模型', key='am_save'):
            try:
                update_env({'LLM_MODELS_ANALYSTS': ','.join(p.strip() for p in picks)})
                st.success('已保存。下次分析会生效。')
                st.rerun()
            except Exception as e:
                st.error(f'保存失败：{e}')
        st.caption('默认是四家不同厂商：Qwen（阿里）/ GLM（智谱）/ '
                   'DeepSeek / Kimi（月之暗面）。'
                   '你可以换成任意模型 —— 也可以故意配四个同厂商的做对比。')

    # ---------- 模型准确率排行 ----------
    _rows_all = agents.load_judgments()
    _settled = [r for r in _rows_all if r.get('已结算')]
    if _settled:
        with st.expander(f'📊 各模型准确率（已结算 {len(_settled)} 次判断）'):
            st.caption('这是「多模型协商」真正的价值验证 —— '
                       '跑一段时间后，用真实结果看哪家模型在这个任务上更准，'
                       '以及主持人综合后是变准还是被带偏。')
            ma = agents.model_accuracy(_rows_all)
            if ma:
                dfm = pd.DataFrame([{'模型': k, **v} for k, v in ma.items()])
                dfm = dfm.sort_values('准确率', ascending=False, na_position='last')
                st.dataframe(dfm, height=260)
                n_min = min(v['样本'] for v in ma.values())
                if n_min < 20:
                    st.warning(f'最小样本只有 {n_min} 次 —— **样本太少，'
                               '现在的排名说明不了问题**。至少每个模型 20 次以上再下结论。')
                else:
                    st.info('样本已够初步参考。注意：这是样本内表现，'
                            '不等于未来有效。')
            else:
                st.caption('还没有结算过判断。')

    # ---------- 模型服务商 ----------
    with st.expander('🌐 换服务商（DeepSeek官方 / OpenAI / 本地Ollama …）'):
        st.caption(
            '**不限硅基流动。** 只要对方提供 OpenAI 兼容接口就能接 —— '
            '换服务商只要改「接口地址 + Key」，不用改任何代码。'
        )
        names = list(llm.PROVIDER_PRESETS.keys())
        cur_prov = llm.provider_of()
        p1, p2 = st.columns([1, 2])
        prov = p1.selectbox('服务商', names, index=names.index(cur_prov)
                            if cur_prov in names else 0, key='prov_pick')
        preset = llm.PROVIDER_PRESETS[prov]
        p2.caption(f"接口地址：`{preset['base'] or '（自己填）'}`\n\n"
                   f"{preset['备注']}")

        with st.form('prov_form', clear_on_submit=True):
            new_base = st.text_input('接口地址（Base URL）',
                                     value=preset['base'],
                                     help='必须是 OpenAI 兼容的地址，一般以 /v1 结尾')
            new_key = st.text_input('API Key', type='password',
                                    help='换服务商要填它的 key。输入不显示')
            new_model = st.text_input('默认模型名（可以先留空，保存后点「拉取可用模型」）',
                                      value='')
            if st.form_submit_button('💾 保存服务商配置', type='primary'):
                upd = {'LLM_BASE_URL': new_base.strip()}
                if new_key.strip():
                    upd['LLM_API_KEY'] = new_key.strip()
                elif not preset['need_key']:
                    upd['LLM_API_KEY'] = 'ollama'
                if new_model.strip():
                    upd['LLM_MODEL'] = new_model.strip()
                try:
                    update_env(upd)
                    st.success(f'已切换到「{prov}」。'
                               + ('Key 已更新。' if new_key.strip() else
                                  '（Key 沿用原有的）'))
                    st.rerun()
                except Exception as e:
                    st.error(f'保存失败：{e}')

        st.caption('💡 保存后点下面的「📋 拉取可用模型」，能列出这家服务商支持的所有模型。'
                   '如果拉不到，说明地址不对或 Key 不对。')

    # ---------- 模型选择 ----------
    with st.expander('⚙️ 模型设置（分析师和主持人可以分开配）'):
        st.caption(
            '**为什么分开**：分析师干的活很简单（读几个数字、按格式输出），'
            '主持人干的活难（综合四方观点、抵抗从众）。'
            '好钢用在刀刃上 —— 分析师用便宜模型，主持人用最聪明的。'
        )
        m1, m2 = st.columns([1, 3])
        if m1.button('📋 拉取可用模型', key='ag_getmodels'):
            with st.spinner('正在获取模型列表……'):
                try:
                    st.session_state['ag_models'] = llm.list_models()
                except Exception as e:
                    st.session_state['ag_models_err'] = str(e)
        if st.session_state.get('ag_models_err'):
            st.warning(st.session_state.pop('ag_models_err'))

        models = st.session_state.get('ag_models') or []
        cur_a = llm.model_name('analyst')
        cur_c = llm.model_name('chair')

        if models:
            import re as _re
            # 默认隐藏向量/重排/语音/图像类，它们不是对话模型
            CHAT_ONLY = st.checkbox('只看对话模型（隐藏向量/语音/图像类）',
                                    value=True, key='ag_chatonly')
            pool = models
            if CHAT_ONLY:
                pool = [m for m in models
                        if not _re.search(r'embed|rerank|asr|ocr|image|vl-|video|'
                                          r'speech|tts|audio|z-image|guard|bge|'
                                          r'kolors|wan2|captioner|i2v|t2v', m.lower())]
            kw = st.text_input('搜索模型（比如 deepseek / qwen / glm）', key='ag_kw')
            if kw.strip():
                k = kw.strip().lower()
                pool = [m for m in pool if k in m.lower()]
            st.caption(f'共 {len(pool)} 个可选（你账号总共 {len(models)} 个）')

            def _idx(cur, pool):
                return pool.index(cur) if cur in pool else 0

            c1, c2 = st.columns(2)
            a_pick = c1.selectbox('分析师模型', pool or models, index=_idx(cur_a, pool or models),
                                  key='ag_ma')
            c_pick = c2.selectbox('主持人模型', pool or models, index=_idx(cur_c, pool or models),
                                  key='ag_mc')
        else:
            st.caption('点上面的「拉取可用模型」可以从列表里选，也可以直接手填模型名。'
                       '换别的服务商（DeepSeek官方 / OpenAI / 本地 Ollama）就手填。')
            c1, c2 = st.columns(2)
            a_pick = c1.text_input('分析师模型', value=cur_a, key='ag_ma_t')
            c_pick = c2.text_input('主持人模型', value=cur_c, key='ag_mc_t')

        st.caption('💡 **不限这几个**：任何 OpenAI 兼容接口的模型都能用。'
                   '想换服务商，在 `.env` 里改 `LLM_BASE_URL` 即可'
                   '（DeepSeek 官方 / OpenAI / 本地 Ollama 都行）。')

        b1, b2 = st.columns(2)
        if b1.button('🧪 体检这两个模型', key='ag_checkup',
                     help='跑 3 道有标准答案的客观题，看它算术和陷阱识别靠不靠谱'):
            st.session_state['ag_checkup'] = {}
            for label, mname in [('分析师', a_pick), ('主持人', c_pick)]:
                with st.spinner(f'正在体检 {mname} ……'):
                    try:
                        st.session_state['ag_checkup'][label] = llm.checkup(mname)
                    except Exception as e:
                        st.session_state['ag_checkup'][label] = {'错误': str(e)[:160]}

        ck = st.session_state.get('ag_checkup')
        if ck:
            for label, r in ck.items():
                if r.get('错误'):
                    st.error(f'{label}：{r["错误"]}')
                    continue
                if r['是否通过']:
                    st.success(f"{label} `{r['模型']}` → **{r['得分']}** ✅ 全部正确")
                else:
                    st.warning(f"{label} `{r['模型']}` → **{r['得分']}** （{r['正确率']}%）")
                for d in r['明细']:
                    icon = {'正确': '✅', '错误': '❌'}.get(d['结果'], '⚠️')
                    st.caption(f"　{icon} {d['题目']}：作答 {d.get('作答', '-')}"
                               f"（标准答案 {d.get('标准答案', '-')}）"
                               + (f"　{d.get('说明','')}" if d.get('说明') else ''))

        if st.button('💾 保存到 .env（立即生效）', key='ag_savemodels'):
            try:
                update_env({'LLM_MODEL_ANALYST': a_pick, 'LLM_MODEL_CHAIR': c_pick})
                st.success(f'已保存。分析师：{a_pick}｜主持人：{c_pick}')
                st.rerun()
            except Exception as e:
                st.error(f'保存失败：{e}')

    ct1, ct2 = st.columns([1, 2])
    if ct1.button('🧪 先测一下这个模型行不行', key='ag_probe'):
        with st.spinner('正在给模型做一次小测验……'):
            try:
                pr = llm.probe()
                st.session_state['ag_probe'] = pr
            except Exception as e:
                st.session_state['ag_probe'] = {'通过': False, '原因': str(e)}

    pr = st.session_state.get('ag_probe')
    if pr:
        if pr.get('通过'):
            st.success(f"✅ 通过了。模型 `{pr['模型']}` 能在 {pr['耗时秒']} 秒内"
                       f"按要求输出结构化 JSON，格式没问题。")
        else:
            st.error('❌ 没通过。这个模型守不住输出格式，'
                     '小模型常见的毛病就是这个。')
        st.caption(f"纯 JSON 无包裹：{pr.get('纯JSON无包裹')}｜"
                   f"方向字段：{pr.get('方向字段合规')}｜"
                   f"信心字段：{pr.get('信心字段合规')}｜"
                   f"耗时：{pr.get('耗时秒')} 秒")
        with st.expander('看模型的原始输出'):
            st.code(pr.get('原始输出') or pr.get('原因') or '（空）')
        st.caption('这一步只花 1 次调用、约 200 tokens。正式分析要 5 次。')

    st.divider()
    db1, db2 = st.columns([1, 3])
    use_debate = db1.toggle('⚔️ 开启多空辩论', value=False, key='ag_debate',
                            help='让一个看多研究员和一个看空研究员互相辩论两轮')
    if use_debate:
        db2.caption('**多 4 次模型调用**（看多/看空各两轮）。'
                    '借鉴 TradingAgents（10.8万星）的设计 —— '
                    '四个分析师看的是同一批数据，可能全都偏多，'
                    '那是「共识」还是「同质化」分不清。'
                    '强制安排专职看多和专职看空，从制度上保证有对立观点。')

    if st.button('🚀 启动多智能体分析', type='primary', key='ag_run'):
        steps = []
        ph = st.empty()

        def on_progress(msg):
            steps.append(msg)
            ph.info('进度：\n\n' + '\n\n'.join(f'- {s}' for s in steps))

        try:
            with st.spinner('分析中，大约需要 30-60 秒……'):
                result = agents.analyze_symbol(symbol, exchange, on_progress=on_progress,
                                                enable_debate=use_debate)
            st.session_state['ag_result'] = result
            ph.success('分析完成。')
        except Exception as e:
            ph.empty()
            st.error(f'分析失败：{e}')

    result = st.session_state.get('ag_result')
    if not result:
        return

    # ---------- 结论 ----------
    chair = result.get('主持人') or {}
    st.divider()
    st.markdown(f'### 主持人结论：{_dir_badge(chair.get("方向"))}')
    m1, m2, m3 = st.columns(3)
    m1.metric('综合信心度', _conf_bar(chair.get('信心')))
    m2.metric('分析时价格', f'{result.get("当时价格", 0):,.4f}')
    m3.metric('分析时间', (result.get('时间') or '')[-8:])

    compl = result.get('格式合规') or {}
    if compl:
        rate = compl.get('格式合规率', 0)
        txt = (f"分析师：{compl.get('分析师模型')}｜主持人：{compl.get('主持人模型')}｜"
               f"四份结论格式合规：{compl.get('分析师格式合规')}｜"
               f"本轮模型调用：{compl.get('总模型调用次数')} 次")
        if rate == 100:
            st.caption('✅ ' + txt)
        elif rate >= 50:
            st.warning('⚠️ ' + txt + '　（有分析师没按格式输出，模型可能偏小）')
        else:
            st.error('❌ ' + txt + '　（大部分输出不合格式，'
                     '这个模型可能撑不住，建议换大一点的模型或改用完整提示词模式）')

    conf = chair.get('信心')
    if isinstance(conf, (int, float)) and conf < agents.DEFAULT_CONFIDENCE_GATE:
        st.info(f'信心度 {conf} 低于 {agents.DEFAULT_CONFIDENCE_GATE}，'
                '按你自己定的门槛，这次判断不该被当成信号。')

    if chair.get('数据核对'):
        st.info(f"🔍 **数据核对**：{chair['数据核对']}")

    for label, key, icon in [('综合判断', '综合判断', '🎯'), ('共识', '共识', '🤝'),
                             ('分歧', '分歧', '⚡'), ('最重要的反面证据', '最重要的反面证据', '🛡️'),
                             ('什么情况下我错了', '什么情况下我错了', '❌'),
                             ('给交易者的提醒', '给交易者的提醒', '💬')]:
        v = chair.get(key)
        if v:
            if key == '什么情况下我错了':
                st.error(f'{icon} **{label}**：{v}')
            elif key == '分歧':
                st.warning(f'{icon} **{label}**：{v}')
            else:
                st.markdown(f'{icon} **{label}**：{v}')

    # ---------- 辩论内容 ----------
    if result.get('辩论'):
        st.divider()
        st.markdown('### ⚔️ 多空辩论实录')
        st.caption('看这个的时候重点不是"谁赢了"，而是**双方在辩论后有没有调整立场**。'
                   '如果有一方说"我认输了"，那是信息量最大的一句话。')
        d = result['辩论']
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('#### 📈 看多研究员')
            b1 = d.get('看多第一轮') or {}
            st.markdown('**第一轮论证**')
            st.markdown(f"- 核心论证：{b1.get('核心论证', '—')}")
            st.markdown(f"- 最有力证据：{b1.get('最有力的证据', '—')}")
            st.markdown(f"- **自曝弱点**：{b1.get('我的弱点', '—')}")
            b2 = d.get('看多反驳') or {}
            st.markdown('**第二轮反驳**')
            st.markdown(f"- 对方破绽：{b2.get('对方的破绽', '—')}")
            st.markdown(f"- 我的回应：{b2.get('我的回应', '—')}")
            ch = b2.get('我改变了吗', '—')
            st.markdown(f"- **{ch}**" + (f" → {b2.get('调整后的立场')}"
                                        f"（信心 {b2.get('调整后的信心')}）"
                                        if b2.get('调整后的立场') else ''))
        with c2:
            st.markdown('#### 📉 看空研究员')
            r1 = d.get('看空第一轮') or {}
            st.markdown('**第一轮论证**')
            st.markdown(f"- 核心论证：{r1.get('核心论证', '—')}")
            st.markdown(f"- 最有力证据：{r1.get('最有力的证据', '—')}")
            st.markdown(f"- **自曝弱点**：{r1.get('我的弱点', '—')}")
            r2 = d.get('看空反驳') or {}
            st.markdown('**第二轮反驳**')
            st.markdown(f"- 对方破绽：{r2.get('对方的破绽', '—')}")
            st.markdown(f"- 我的回应：{r2.get('我的回应', '—')}")
            ch2 = r2.get('我改变了吗', '—')
            st.markdown(f"- **{ch2}**" + (f" → {r2.get('调整后的立场')}"
                                         f"（信心 {r2.get('调整后的信心')}）"
                                         if r2.get('调整后的立场') else ''))

    # ---------- 生成交易方案 ----------
    st.divider()
    st.markdown('### 📋 生成交易方案')
    st.caption(
        '**分工**：代码负责算（结构位、止损候选、盈亏比、仓位、爆仓价），'
        '模型只负责从代码算好的候选里选一个。'
        '**模型不允许凭空给价格** —— 如果它编了一个候选里没有的位置，会被直接拒绝。'
    )

    pc1, pc2, pc3 = st.columns(3)
    _eq = pc1.number_input('本金（USDT）', min_value=1.0,
                           value=float((cfg or {}).get('本金', 1000.0)),
                           step=100.0, key='plan_eq')
    _rp = pc2.number_input('单笔风险（%）', min_value=0.1, max_value=10.0,
                           value=float((cfg or {}).get('单笔风险百分比', 1.0)),
                           step=0.1, key='plan_rp')
    _lv = pc3.number_input('杠杆', min_value=1.0, max_value=125.0,
                           value=float((cfg or {}).get('杠杆', 10.0)),
                           step=1.0, key='plan_lv')

    if st.button('📋 生成完整方案', type='primary', key='plan_run'):
        with st.spinner('正在算结构位并让方案官选择……'):
            try:
                res = plan.build_plan(
                    symbol, result, equity=_eq, risk_pct=_rp, leverage=_lv,
                    fee_rate=float((cfg or {}).get('手续费率', 0.0005)),
                    mmr=float((cfg or {}).get('维持保证金率', 0.005)),
                    min_rr=float((cfg or {}).get('最低盈亏比', 1.5)))
                st.session_state['plan_result'] = res
            except Exception as e:
                st.session_state['plan_err'] = f'{type(e).__name__}: {e}'

    if st.session_state.get('plan_err'):
        st.error(st.session_state.pop('plan_err'))

    pres = st.session_state.get('plan_result')
    if pres:
        if not pres.get('可执行'):
            st.warning('**没有生成可执行方案**')
            st.markdown(f"原因：{pres.get('原因')}")
            st.caption('⚠️ 拒绝给方案是正常结果。信号不清时不下手，比硬编一个方案专业得多。')
        else:
            st.code(plan.format_plan(pres), language=None)
            with st.expander('📐 程序算出的候选止损位（模型只能从这里选）'):
                st.dataframe(pd.DataFrame(pres['候选止损']), height=320)
                st.caption('每个候选都是代码按公式算的，不是模型编的。')

            c1, c2, c3 = st.columns(3)
            c1.metric('止损时亏损', f'{pres["仓位"]["止损时实际亏损"]:,.2f} USDT',
                      f'本金 {pres["仓位"]["止损时实际亏损比例"]:.2f}%')
            c2.metric('达到止盈赚', f'{pres["仓位"]["达到目标的盈利"]:,.2f} USDT',
                      f'盈亏比 {pres["止盈依据"]["盈亏比"]:g}:1')
            c3.metric('估算爆仓价', f'{pres["仓位"]["爆仓价"]:,.4f}',
                      '✅ 在止损之外' if not pres['仓位']['爆仓先于止损'] else '🚨 危险')

            st.error(f"⚠️ **这个方案在什么情况下失效**：{pres.get('可执行条件')}")
            st.warning(f"**主要风险**：{pres.get('主要风险')}")
            st.caption(f"方案官模型：{pres.get('方案官模型')}｜"
                       '所有数字均由程序计算，模型只做了选择性判断')

    # ---------- 四位分析师 ----------
    st.markdown('### 四位分析师的独立观点')
    st.caption('注意看他们的信心度和「什么情况下我错了」——'
               '分歧越大，说明这件事越没有定论。')
    cols = st.columns(4)
    for col, a in zip(cols, result.get('分析师') or []):
        with col:
            flag = '' if a.get('_格式合规', True) else ' ⚠️'
            if a.get('_调用次数', 1) > 1:
                flag += f'（重试 {a["_调用次数"]} 次）'
            st.markdown(f'**{a.get("_名称")}**{flag}')
            st.markdown(f'### {_dir_badge(a.get("方向"))}')
            st.caption(f'信心 {_conf_bar(a.get("信心"))}')
            st.markdown(f'**核心理由**  \n{a.get("核心理由") or "—"}')
            st.markdown(f'**主要风险**  \n{a.get("主要风险") or "—"}')
            st.markdown(f'**什么情况下我错了**  \n{a.get("什么情况下我错了") or "—"}')
            st.markdown(f'**看不到什么**  \n{a.get("我看不到什么") or "—"}')
            with st.expander('他看到的原始数据'):
                st.json(a.get('_数据') or {})

    # ---------- 记录这次判断 ----------
    st.divider()
    st.markdown('### 记录这次判断（最重要的一步）')
    st.caption('不记录，你就永远不知道它准不准。记录后到期会自动结算对错。')
    r1, r2, r3 = st.columns([1, 1, 2])
    horizon = r1.selectbox('结算周期', [4, 12, 24, 72, 168], index=2,
                           format_func=lambda h: f'{h} 小时', key='ag_h')
    note = r3.text_input('备注（选填）', key='ag_note',
                         placeholder='例：大非农数据公布前，市场偏谨慎')
    if r2.button('📌 记录这次判断', key='ag_save'):
        try:
            row = agents.record_judgment(result, horizon_hours=horizon, note=note)
            st.success(f'已记录。将在 {row["结算时间"]} 自动结算，'
                       f'当时价格 {row["当时价格"]:,.4f}。')
        except Exception as e:
            st.error(f'记录失败：{e}')

    # ---------- 准确率 ----------
    st.divider()
    st.markdown('### 它到底准不准')
    rows = agents.load_judgments()
    if not rows:
        st.info('还没有任何判断记录。多跑几次并记录，过一段时间这里会告诉你它的真实准确率。')
        return

    st_ = agents.accuracy_stats(rows)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric('总判断数', st_['总判断数'])
    k2.metric('已结算 / 待结算', f'{st_["已结算"]} / {st_["待结算"]}')
    k3.metric('准确率', f'{st_["准确率"]}%' if st_['准确率'] is not None else '—',
              f'样本 {st_["可统计"]} 次', delta_color='off')
    k4.metric('高信心准确率', f'{st_["高信心准确率"]}%'
              if st_['高信心准确率'] is not None else '—',
              f'信心≥{agents.DEFAULT_CONFIDENCE_GATE} 的 {st_.get("高信心样本数", 0)} 次',
              delta_color='off')

    honest = agents.honest_verdict(st_)
    if '比抛硬币还差' in honest or '和抛硬币没区别' in honest:
        st.error(honest)
    elif '还没有结算' in honest:
        st.info(honest)
    else:
        st.success(honest)

    if st.button('🔄 结算到期判断（拉当前价核对）', key='ag_settle'):
        with st.spinner('正在核对到期判断……'):
            try:
                n, errors = agents.settle_due()
                if n:
                    st.success(f'本次结算了 {n} 条判断。')
                else:
                    st.info('还没有到期的判断。')
                for e in errors:
                    st.warning(e)
                if n:
                    st.rerun()
            except Exception as e:
                st.error(f'结算失败：{e}')

    show = pd.DataFrame(rows)[::-1]
    cols = [c for c in ['记录时间', '币种', '方向', '信心', '当时价格',
                        '结算时间', '到期价格', '涨跌幅%', '对不对',
                        '已结算', '备注'] if c in show.columns]
    st.dataframe(show[cols], height=340)
    st.download_button('⬇️ 导出判断记录',
                       show[cols].to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
                       file_name='判断记录.csv', mime='text/csv')