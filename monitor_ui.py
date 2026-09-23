# -*- coding: utf-8 -*-
"""「市场监控」页签的界面。单独成文件，避免 app.py 太臃肿。"""
import datetime as dt

import pandas as pd
import streamlit as st

import exchange_sync
import llm
import market
import monitor
from common import fmt_usdt, get_env, update_env
import symbol_picker

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


def _flash(text, level='警告'):
    icon = monitor.LEVEL_ICON.get(level, '•')
    if level == '紧急':
        st.error(f'{icon} {text}')
    elif level == '警告':
        st.warning(f'{icon} {text}')
    else:
        st.info(f'{icon} {text}')


def render(cfg):
    _ex_default = list(exchange_sync.ENV_KEYS.keys())[0]
    conf_ok_pre, _ = exchange_sync.has_credentials(_ex_default)

    st.subheader('市场监控')
    st.caption(
        '按规则自动盯盘，触发就提醒。'
        '所有规则都要你自己定 —— 工具不会替你决定什么是「值得关注」。'
    )

    # ---------- 自动刷新 ----------
    c1, c2, c3 = st.columns([1, 1, 2])
    auto = c1.toggle('自动刷新', value=False, key='mon_auto',
                     help='开着页面就能一直盯。关掉页面就停。')
    interval = c2.selectbox('刷新间隔', [30, 60, 120, 300], index=1,
                            format_func=lambda x: f'{x} 秒', key='mon_int')
    exchange = c3.selectbox('数据源', ['自动', '币安', '欧易OKX', 'Bybit'],
                            key='mon_ex')

    if auto and HAS_AUTOREFRESH:
        st_autorefresh(interval=interval * 1000, key='mon_tick')
        st.caption(f'⏱ 自动刷新已开启（每 {interval} 秒）。'
                   f'上次刷新：{dt.datetime.now():%H:%M:%S}')
    elif auto and not HAS_AUTOREFRESH:
        st.warning('没装 streamlit-autorefresh，自动刷新用不了。'
                   '手动点下面的「立即检查」也一样。')

    # ---------- 填写 API Key（避免把密钥发到聊天里） ----------
    with st.expander('🔑 填写交易所 API Key（新用户先点这里）',
                     expanded=(not conf_ok_pre)):
        st.caption(
            '**在这里填，比发到任何聊天里都安全** —— 它只会写进你本机的 `.env` 文件，'
            '不上传、不打印、不会进 git。填完建议手动清空浏览器输入框。'
        )
        st.warning('填写前请确认已在交易所关掉「提现」和「现货交易」权限。'
                   '币安读合约持仓需要开「允许合约」，这是它的设计限制。')

        ex_pick = st.selectbox('要配置哪个交易所', list(exchange_sync.ENV_KEYS.keys()),
                               key='key_ex')
        env_names = exchange_sync.ENV_KEYS[ex_pick]
        with st.form('key_form', clear_on_submit=True):
            vals = {}
            for idx, name in enumerate(env_names):
                if not name:
                    continue
                label = {0: 'API Key', 1: 'API Secret', 2: 'Passphrase（OKX 需要）'}[idx]
                vals[name] = st.text_input(label, type='password',
                                           key=f'ki_{ex_pick}_{idx}',
                                           help='粘贴即可，内容不会显示出来')
            if st.form_submit_button('💾 保存到 .env（立即生效）', type='primary'):
                filled = {k: v.strip() for k, v in vals.items() if v and v.strip()}
                if len(filled) < len([n for n in env_names if n]):
                    st.error('有字段没填，请把该交易所需要的都填完。')
                else:
                    try:
                        update_env(filled)
                        st.success(f'已保存到 .env。输入框已清空。')
                        st.rerun()
                    except Exception as e:
                        st.error(f'保存失败：{e}')

    # ---------- ① 同步交易所持仓 ----------
    st.markdown('#### ① 从交易所同步持仓')
    st.caption('在手机上开仓、加仓、平仓之后，点一下同步 —— 这里会自动更新，不用手动填。'
               '只读你的持仓，不会动你的钱。')
    sy1, sy2 = st.columns([1, 3])
    src_ex = sy1.selectbox('交易所', list(exchange_sync.ENV_KEYS.keys()), key='sync_ex')
    conf_ok, conf_msg = exchange_sync.has_credentials(src_ex)
    auto_sync = sy2.toggle('每次检查时自动同步', value=False, key='sync_auto',
                           help='和自动刷新一起用：开着页面就会自动把持仓同步过来')
    if conf_ok:
        env_key = exchange_sync.ENV_KEYS[src_ex][0]
        st.caption(f'Key 状态：✅ {conf_msg}｜当前 Key：'
                   f'{exchange_sync.mask(get_env(env_key))}')
    else:
        st.info(f'{src_ex} 还没配置 API Key（{conf_msg}）。'
                '到「复盘」页签下面看配置方法，或翻 README 的「同步交易所持仓」一节。')

    if src_ex == '币安' and conf_ok:
        if st.button('🛡️ 检查这个 Key 的权限安不安全', key='sync_audit'):
            with st.spinner('正在读取 Key 权限……'):
                try:
                    rep = exchange_sync.binance_key_report()
                    st.session_state['sync_audit'] = rep
                except Exception as e:
                    st.session_state['sync_audit_err'] = str(e)
        if st.session_state.get('sync_audit_err'):
            st.warning(st.session_state.pop('sync_audit_err'))
        rep = st.session_state.get('sync_audit')
        if rep:
            lvl = rep.get('风险等级')
            line = (f"读取 {'✅' if rep['读取权限'] else '❌'}　"
                    f"合约 {'✅' if rep['合约权限'] else '❌'}　"
                    f"提现 {'❌ 已关（好）' if not rep['提现权限'] else '❗开着（危险）'}　"
                    f"IP白名单 {'✅ 已绑' if rep['IP白名单'] else '❌ 未绑'}　"
                    f"现货交易 {'开' if rep['现货交易'] else '关'}　"
                    f"划转 {'开' if rep['划转权限'] else '关'}")
            if lvl == '危险':
                st.error(f'风险等级：{lvl}\n\n{line}')
            elif lvl == '偏高':
                st.warning(f'风险等级：{lvl}\n\n{line}')
            else:
                st.success(f'风险等级：{lvl}\n\n{line}')
            for t in rep['建议']:
                st.markdown(f'- {t}')
            if rep.get('创建时间'):
                st.caption(f'这个 Key 创建于 {rep["创建时间"]}')

    sync_clicked = st.button('🔄 从交易所同步持仓', type='primary',
                             disabled=not conf_ok)
    if (auto_sync or sync_clicked) and conf_ok:
        with st.spinner(f'正在从{src_ex}读取持仓……'):
            try:
                fetched = exchange_sync.fetch_positions(src_ex)
                events = exchange_sync.diff_positions(monitor.load_positions(), fetched)
                merged = exchange_sync.sync_to_local(fetched)
                st.session_state['sync_ok'] = f'同步成功，当前持有 {len(merged)} 笔。'
                st.session_state['sync_events'] = events
            except PermissionError as e:
                st.session_state['sync_err'] = str(e)
            except Exception as e:
                st.session_state['sync_err'] = f'同步失败：{e}'

    if st.session_state.get('sync_ok'):
        st.success(st.session_state.pop('sync_ok'))
    if st.session_state.get('sync_err'):
        st.error(st.session_state.pop('sync_err'))

    events = st.session_state.get('sync_events') or []
    if events:
        st.markdown('**检测到这些变化：**')
        for ev in events:
            icon = {'开仓': '🟢', '加仓': '➕', '减仓': '➖',
                    '平仓': '🔴', '反手': '🔄'}.get(ev['类型'], '•')
            st.markdown(f'{icon} {ev["说明"]}')
        st.caption('这些变化可以手动补记到「交易日志」，也可以等以后加自动记账。')

    st.divider()

    # ---------- 我的持仓 ----------
    st.markdown('#### ② 我的持仓（用来算「离爆仓还有多远」）')
    positions = monitor.load_positions()
    with st.expander('添加 / 更新一笔持仓', expanded=(len(positions) == 0)):
        with st.form('pos_form', clear_on_submit=True):
            p1 = st.columns(4)
            with p1[0]:
                p_sym = symbol_picker.pick('币种', key='pos_sym', default='BTCUSDT', compact=True)
            p_dir = p1[1].selectbox('方向', ['多', '空'])
            p_entry = p1[2].number_input('开仓价', min_value=0.0, value=0.0,
                                         format='%.6f', step=0.0)
            p_qty = p1[3].number_input('数量（币）', min_value=0.0, value=0.0,
                                       format='%.6f', step=0.0)
            p2 = st.columns(3)
            p_lev = p2[0].number_input('杠杆', min_value=1.0, max_value=125.0,
                                       value=float(cfg['杠杆']), step=1.0)
            p_stop = p2[1].number_input('止损价', min_value=0.0, value=0.0,
                                        format='%.6f', step=0.0)
            p_liq = p2[2].number_input('爆仓价（不填就按杠杆估算）', min_value=0.0,
                                       value=0.0, format='%.6f', step=0.0)
            if st.form_submit_button('保存持仓', type='primary'):
                if p_entry <= 0 or p_qty <= 0:
                    st.error('开仓价和数量都要大于 0')
                else:
                    monitor.upsert_position(p_sym, p_dir, p_entry, p_qty, p_lev,
                                            p_stop, p_liq, cfg['维持保证金率'])
                    st.success(f'已记录 {p_sym.upper()} 持仓')
                    st.rerun()

    if positions:
        st.dataframe(pd.DataFrame(positions), height=min(180, 60 + 35 * len(positions)))
        rm = st.selectbox('删除某笔持仓', ['（不删）'] + [p['币种'] for p in positions],
                          key='mon_rmpos')
        if rm != '（不删）' and st.button(f'确认删除 {rm} 持仓'):
            monitor.remove_position(rm)
            st.rerun()
    else:
        st.caption('还没记录持仓。不记录也能用行情类规则，但没法监控爆仓距离。')

    st.divider()

    # ---------- 监控规则 ----------
    st.markdown('#### ③ 监控规则')
    rules = monitor.load_rules()

    with st.expander('添加规则', expanded=(len(rules) == 0)):
        types = list(monitor.RULE_TYPES.keys())
        with st.form('rule_form', clear_on_submit=True):
            r1 = st.columns(3)
            with r1[0]:
                r_sym = symbol_picker.pick('币种', key='rule_sym', default='BTCUSDT', compact=True)
            r_type = r1[1].selectbox('规则类型', types, key='r_type')
            r_th = r1[2].number_input(
                '阈值', value=float(monitor.RULE_TYPES[r_type][1]),
                format='%.4f', step=0.0, key='r_th')
            st.caption(f'说明：{monitor.RULE_TYPES[r_type][0]}')
            r2 = st.columns(3)
            r_lv = r2[0].selectbox('等级', monitor.LEVELS, index=1)
            r_cd = r2[1].number_input('冷却时间（分钟）', min_value=1, max_value=1440,
                                      value=30, step=5)
            r_note = r2[2].text_input('备注（选填）')
            if st.form_submit_button('添加规则', type='primary'):
                try:
                    monitor.add_rule(r_sym, r_type, r_th, r_lv, r_cd, r_note)
                    st.success('已添加')
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

        st.caption('懒得想阈值？一键加一套常用的：')
        qc1, qc2 = st.columns([1, 3])
        with qc1:
            quick_sym = symbol_picker.pick('币种', key='quick_sym', default='BTCUSDT', compact=True)
        if qc2.button('一键添加推荐规则组合'):
            try:
                snap = market.snapshot(quick_sym, exchange)
                price = snap['标记价']
                sym = market.normalize_symbol(quick_sym)
                up = price * 1.05
                down = price * 0.95
                for args in [
                    (sym, '价格上破', up, '提示', 60, '涨 5% 提醒'),
                    (sym, '价格下破', down, '警告', 60, '跌 5% 提醒'),
                    (sym, '24h涨跌幅超过', 5.0, '警告', 60, '日内波动放大'),
                    (sym, '资金费率超过', 0.05, '警告', 120, '多空失衡'),
                    (sym, '多空比高于', 1.8, '提示', 120, '散户一边倒'),
                    (sym, '多空比低于', 0.6, '提示', 120, '散户一边倒'),
                    (sym, 'ATR超过', 3.0, '警告', 240, '波动率升高'),
                ]:
                    monitor.add_rule(*args)
                st.success(f'已按当前价 {price:,.4f} 添加 7 条规则。价格线可自行调整。')
                st.rerun()
            except Exception as e:
                st.error(f'加规则失败（可能是行情拉不到）：{e}')

    if rules:
        show = pd.DataFrame(rules)
        st.dataframe(show, height=min(320, 60 + 35 * len(rules)))
        d1, d2 = st.columns(2)
        pick = d1.selectbox('选择规则', ['（不选）'] + [f'{r["编号"]} {r["币种"]} {r["类型"]}'
                                                    for r in rules],
                            key='mon_pick')
        if pick != '（不选）' and d2.button('删除这条规则'):
            monitor.delete_rule(pick.split(' ')[0])
            st.rerun()
    else:
        st.caption('还没有规则。')

    st.divider()

    # ---------- 立即检查 ----------
    st.markdown('#### ④ 立即检查')
    cc1, cc2 = st.columns([1, 3])
    if cc1.button('🔍 立即检查', type='primary'):
        st.session_state['mon_run'] = True
    if cc2.button('清空冷却状态（让所有规则可以立刻重报）'):
        monitor.clear_state()
        st.info('冷却状态已清空。')

    do_run = auto or st.session_state.get('mon_run')
    if do_run:
        with st.spinner('正在拉行情并检查规则……'):
            try:
                alerts, errors, snaps = monitor.run_once(exchange=exchange)
                st.session_state['mon_snaps'] = snaps
                st.session_state['mon_errors'] = errors
                st.session_state['mon_last'] = alerts
                st.session_state['mon_run'] = False
            except Exception as e:
                st.error(f'检查失败：{e}')

    alerts = st.session_state.get('mon_last') or []
    errors = st.session_state.get('mon_errors') or []
    snaps = st.session_state.get('mon_snaps') or {}

    if alerts:
        st.markdown(f'**本轮触发 {len(alerts)} 条警报**')
        for a in alerts:
            _flash(f'[{a["币种"]} · {a["规则"]}] {a["说明"]}', a['等级'])
    elif do_run or snaps:
        st.success('本轮没有触发任何警报。')

    for e in errors:
        st.warning(e)

    # ---------- 持仓健康度 ----------
    positions = monitor.load_positions()
    if positions and snaps:
        st.markdown('#### ⑤ 持仓健康度')
        rows = []
        for p in positions:
            snap = snaps.get(p['币种'])
            if not snap or not snap.get('标记价'):
                continue
            h = monitor.position_health(p, snap['标记价'])
            rows.append({
                '币种': p['币种'], '方向': p['方向'], '当前价': h['当前价'],
                '开仓价': p['开仓价'], '浮盈亏': h['浮动盈亏'],
                '距爆仓': h.get('距爆仓百分比'), '距止损': h.get('距止损百分比'),
            })
        if rows:
            dfh = pd.DataFrame(rows)
            st.dataframe(dfh, height=min(220, 60 + 35 * len(dfh)))
            for r in rows:
                d = r.get('距爆仓')
                if d is not None and d < 10:
                    st.error(f'🚨 {r["币种"]} 距离爆仓只剩 {d:.2f}%，浮盈亏 '
                             f'{fmt_usdt(r["浮盈亏"])} USDT')
                ds = r.get('距止损')
                if ds is not None and ds < 1:
                    st.warning(f'⚠️ {r["币种"]} 距离止损只剩 {ds:.2f}%')

        st.markdown('#### 仓位体检')
        st.caption('专门抓那些「看起来没事、其实一直在漏钱」的仓位。')
        for p in positions:
            snap = snaps.get(p['币种'])
            if not snap or not snap.get('标记价'):
                continue
            findings = monitor.position_audit(p, snap['标记价'], snap.get('资金费率'))
            if not findings:
                continue
            st.markdown(f'**{p["币种"]}**')
            for fnd in findings:
                icon = {'提示': 'ℹ️', '警告': '⚠️', '紧急': '🚨'}.get(fnd['级别'], '•')
                line = f'{icon} **{fnd["项目"]}** —— {fnd["说明"]}'
                if fnd['级别'] in ('警告', '紧急'):
                    st.warning(line)
                else:
                    st.info(line)

    # ---------- 当前行情速览 ----------
    if snaps:
        st.markdown('#### ⑥ 当前行情速览')
        rows = []
        for sym, s in snaps.items():
            rows.append({
                '币种': sym, '标记价': s.get('标记价'),
                '24h涨跌%': s.get('24h涨跌幅'),
                '资金费率%': (s.get('资金费率') or 0) * 100 if s.get('资金费率') is not None else None,
                '多空比': s.get('多空比'),
                '数据源': s.get('交易所'),
            })
        st.dataframe(pd.DataFrame(rows), height=min(240, 60 + 35 * len(rows)))

    # ---------- 警报历史 ----------
    st.divider()
    st.markdown('#### ⑦ 警报历史')
    hist = monitor.load_alerts(200)
    if not hist:
        st.caption('还没有触发过警报。')
    else:
        hdf = pd.DataFrame(hist)[['时间', '等级', '币种', '规则', '说明']]
        st.dataframe(hdf, height=300)
        st.download_button('⬇️ 导出警报历史',
                           hdf.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
                           file_name='警报历史.csv', mime='text/csv')
