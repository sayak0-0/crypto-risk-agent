# -*- coding: utf-8 -*-
"""自测脚本：验证风控数学、统计口径、日志读写、行情解析。

运行：python selftest.py
不联网也能跑（行情部分用构造数据）。
"""
import json
import os
import sys
import traceback

import pandas as pd

import agents
import exchange_sync
import journal
import llm
import market
import metrics
import monitor
import news
import onchain
import plan
import symbol_picker
import tasks
import watchlist
import review
import risk

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f'  [通过] {name}')
    except Exception as e:
        FAIL.append((name, e))
        print(f'  [失败] {name} -> {type(e).__name__}: {e}')
        traceback.print_exc(limit=2)


def approx(a, b, tol=1e-6):
    assert abs(a - b) < tol, f'期望 {b}，实际 {a}'


def section(t):
    print(f'\n== {t} ==')


# ---------------- 风控数学 ----------------
section('风控数学')

def t_liq_long():
    # 100 开多，10 倍，维持保证金 0.5% -> 100*0.9/0.995
    approx(risk.liquidation_price(100, 10, 'long', 0.005), 90.45226130653266)

def t_liq_short():
    approx(risk.liquidation_price(100, 10, 'short', 0.005), 109.45273631840796)

def t_liq_no_mmr():
    approx(risk.liquidation_price(100, 10, 'long', 0.0), 90.0)

def t_breakeven():
    approx(risk.breakeven_price(100, 0.0005, 'long'), 100.10005, 1e-6)
    approx(risk.breakeven_price(100, 0.0005, 'short'), 99.90005, 1e-6)

def t_rr():
    approx(risk.rr_ratio(100, 95, 110), 2.0)

def t_position_basic():
    """10000 本金、风险 1%、开多 100 止损 95、无手续费 -> 数量 20，名义 2000，保证金 200。"""
    p = risk.calc_position(10000, 1, 100, 95, 10, 'long', fee_rate=0.0)
    approx(p['建议数量'], 20.0)
    approx(p['名义价值'], 2000.0)
    approx(p['占用保证金'], 200.0)
    approx(p['允许亏损额'], 100.0)
    approx(p['止损时实际亏损'], 100.0)

def t_position_with_fee():
    """带手续费时，总亏损（价格亏损+手续费）仍等于允许亏损额。"""
    p = risk.calc_position(10000, 1, 100, 95, 10, 'long', fee_rate=0.0005, mmr=0.005)
    approx(p['止损时实际亏损'], 100.0, 1e-6)
    assert p['建议数量'] < 20.0, '有手续费时数量应小于无手续费时'
    approx(p['预估手续费'], p['建议数量'] * 0.0005 * 195, 1e-6)

def t_position_short():
    p = risk.calc_position(1000, 2, 100, 105, 5, 'short', fee_rate=0.0)
    approx(p['建议数量'], 4.0)      # 20 / 5
    approx(p['名义价值'], 400.0)
    approx(p['占用保证金'], 80.0)

def t_position_errors():
    for args, msg in [
        (((10000, 1, 100, 100, 10)), '止损价不能等于开仓价'),
        (((10000, 1, 100, 105, 10)), '做多的止损价必须低于开仓价'),
        (((10000, 100, 100, 95, 10)), '风险百分比'),
        (((10000, 1, 100, 95, 0.5)), '杠杆'),
        (((0, 1, 100, 95, 10)), '本金'),
    ]:
        try:
            risk.calc_position(*args)
        except ValueError as e:
            assert msg in str(e), f'报错信息应包含「{msg}」，实际是「{e}」'
        else:
            raise AssertionError(f'应报错但没报错：{args}')

def t_position_bad_short():
    try:
        risk.calc_position(1000, 1, 100, 95, 10, 'short')
    except ValueError as e:
        assert '做空的止损价必须高于开仓价' in str(e)
    else:
        raise AssertionError('应报错')

for n, f in [('爆仓价-做多', t_liq_long), ('爆仓价-做空', t_liq_short),
             ('爆仓价-无维持保证金', t_liq_no_mmr), ('保本价', t_breakeven),
             ('盈亏比', t_rr), ('仓位计算-基础', t_position_basic),
             ('仓位计算-含手续费', t_position_with_fee),
             ('仓位计算-做空', t_position_short),
             ('仓位计算-参数校验', t_position_errors),
             ('仓位计算-做空方向校验', t_position_bad_short)]:
    check(n, f)


# ---------------- 开仓前检查 ----------------
section('开仓前纪律检查')

def t_check_pass():
    v, items = risk.pre_trade_check(10000, 1, 100, 95, 10, 'long', target=110)
    assert v == '通过', f'应该是通过，实际是 {v}：{items}'

def t_check_liq_first():
    """100 倍杠杆下爆仓价 99.5 比止损 95 更近，必须先于止损触发 -> 拒绝。"""
    v, items = risk.pre_trade_check(10000, 1, 100, 95, 100, 'long', target=110)
    assert v == '拒绝', f'应该拒绝，实际是 {v}'
    assert any(i['项目'] == '爆仓风险' and i['级别'] == '拒绝' for i in items)

def t_check_no_stop():
    v, items = risk.pre_trade_check(10000, 1, 100, 0, 10, 'long')
    assert v == '拒绝'
    assert any(i['项目'] == '止损' for i in items)

def t_check_big_risk():
    v, items = risk.pre_trade_check(10000, 5, 100, 90, 10, 'long', target=140)
    assert v == '拒绝', '5% 单笔风险应被拒绝'
    assert any(i['项目'] == '单笔风险' and i['级别'] == '拒绝' for i in items)

def t_check_bad_rr():
    v, items = risk.pre_trade_check(10000, 1, 100, 95, 10, 'long', target=102)
    assert v == '拒绝', '盈亏比 0.4 应被拒绝'

def t_check_streak():
    v, items = risk.pre_trade_check(10000, 1, 100, 95, 10, 'long', target=110,
                                    consecutive_losses=2, max_consecutive_losses=2)
    assert v == '拒绝', '连亏 2 笔应触发停手'
    assert any(i['项目'] == '连亏保护' for i in items)

def t_check_daily_limit():
    v, items = risk.pre_trade_check(10000, 1, 100, 95, 10, 'long', target=110,
                                    today_trades=3, max_daily_trades=3)
    assert v == '拒绝'

def t_check_short_safe():
    v, items = risk.pre_trade_check(10000, 1, 100, 105, 10, 'short', target=90)
    assert v == '通过', f'应通过，实际 {v}：{items}'

for n, f in [('好单子应该通过', t_check_pass),
             ('爆仓先于止损应拒绝', t_check_liq_first),
             ('没止损应拒绝', t_check_no_stop),
             ('单笔风险超 2% 应拒绝', t_check_big_risk),
             ('盈亏比过低应拒绝', t_check_bad_rr),
             ('连亏达到停手线应拒绝', t_check_streak),
             ('超过每日笔数应拒绝', t_check_daily_limit),
             ('做空安全单应通过', t_check_short_safe)]:
    check(n, f)


# ---------------- 日志读写 ----------------
section('交易日志')

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', '_自测_交易记录.csv')
if os.path.exists(TMP):
    os.remove(TMP)

def t_empty_load():
    d = journal.load(TMP)
    assert len(d) == 0
    assert list(d.columns)[:3] == ['编号', '开仓时间', '平仓时间']

def t_add_and_derive():
    journal.add_trade({
        '编号': 'T0001', '开仓时间': '2026-09-01 10:00', '平仓时间': '2026-09-01 12:00',
        '币种': 'BTCUSDT', '方向': '多', '杠杆': 10, '开仓价': 100, '平仓价': 110,
        '数量': 1, '止损价': 95, '手续费': 1, '情绪': '平静',
    }, TMP)
    journal.add_trade({
        '编号': 'T0002', '开仓时间': '2026-09-02 10:00', '平仓时间': '2026-09-02 11:00',
        '币种': 'ETHUSDT', '方向': '空', '杠杆': 10, '开仓价': 100, '平仓价': 105,
        '数量': 1, '止损价': 105, '手续费': 1, '情绪': '报复性交易',
    }, TMP)
    d = journal.load(TMP)
    assert len(d) == 2
    approx(float(d.loc[0, '净盈亏']), 9.0)     # (110-100)*1 - 1
    approx(float(d.loc[0, 'R倍数']), 1.8)      # 9 / (5*1)
    approx(float(d.loc[1, '净盈亏']), -6.0)    # (105-100)*1*(-1) - 1
    approx(float(d.loc[1, 'R倍数']), -1.2)
    approx(float(d.loc[0, '名义价值']), 100.0)
    assert journal.new_id(d) == 'T0003', journal.new_id(d)

def t_recent_state():
    st = journal.recent_state(journal.load(TMP))
    assert st['consecutive_losses'] == 1, st
    assert st['today_trades'] == 0, '这两笔是历史日期，今天应为 0'
    assert 'minutes_since_last_loss' in st

for n, f in [('空日志能正常读取', t_empty_load),
             ('新增交易并自动算盈亏', t_add_and_derive),
             ('近期状态统计', t_recent_state)]:
    check(n, f)


# ---------------- 统计指标 ----------------
section('绩效统计')

def t_summary():
    d = journal.load(TMP)
    s = metrics.summary(d, start_equity=1000)
    assert s['交易笔数'] == 2
    assert s['盈利笔数'] == 1 and s['亏损笔数'] == 1
    approx(s['胜率'], 50.0)
    approx(s['总净盈亏'], 3.0)
    approx(s['盈亏比'], 1.5)          # 9 / 6
    approx(s['盈利因子'], 1.5)        # 9 / 6
    approx(s['最大单笔亏损'], -6.0)
    approx(s['最大连亏'], 1)

def t_drawdown():
    d = journal.load(TMP)
    amt, pct = metrics.max_drawdown(d, start_equity=1000)
    approx(amt, 6.0)                  # 累计到 9 后回落到 3
    approx(pct, 6.0 / 1009 * 100, 1e-6)

def t_equity_curve():
    c = metrics.equity_curve(journal.load(TMP), start_equity=1000)
    assert len(c) == 2
    approx(float(c.iloc[0]['权益']), 1009.0)
    approx(float(c.iloc[1]['权益']), 1003.0)

def t_empty_summary():
    assert metrics.summary(journal.empty_df()) == {'交易笔数': 0}
    assert metrics.max_drawdown(journal.empty_df()) == (0.0, None)

def t_by_group():
    g = metrics.by_group(journal.load(TMP), '币种')
    assert len(g) == 2
    assert set(g['币种']) == {'BTCUSDT', 'ETHUSDT'}

for n, f in [('核心指标口径正确', t_summary), ('最大回撤正确', t_drawdown),
             ('权益曲线正确', t_equity_curve), ('空数据不报错', t_empty_summary),
             ('分组统计可用', t_by_group)]:
    check(n, f)


# ---------------- 行情解析 ----------------
section('行情解析')

def t_normalize():
    for raw in ['BTC', 'btc', 'BTCUSDT', 'BTC-USDT', 'BTC/USDT', 'btc_usdt',
                'BTCUSDT-SWAP', 'BTC-USD']:
        assert market.normalize_symbol(raw) == 'BTCUSDT', f'{raw} -> {market.normalize_symbol(raw)}'
    assert market.normalize_symbol('ETH') == 'ETHUSDT'

def t_okx_inst():
    assert market._okx_inst('BTC') == 'BTC-USDT-SWAP'
    assert market._okx_inst('ETHUSDT') == 'ETH-USDT-SWAP'

def t_indicators():
    kl = [[i, 100, 101, 99, 100, 10] for i in range(40)]
    ind = market.compute_indicators(kl, 100)
    approx(ind['MA20'], 100.0)
    approx(ind['MA60'] if 'MA60' in ind else ind['MA20'], 100.0)
    approx(ind['ATR14'], 2.0)
    approx(ind['ATR14百分比'], 2.0)
    approx(ind['近期高点'], 101.0)
    approx(ind['距近期高点百分比'], (100 - 101) / 101 * 100)
    assert '距MA20百分比' not in ind or abs(ind['距MA20百分比']) < 1e-9

def t_indicators_short_data():
    assert market.compute_indicators([], 100) == {}
    assert market.compute_indicators([[0, 1, 2, 1, 1.5, 1]], 100) == {}

def t_notes():
    snap = {'资金费率': 0.001, '多空比': 1.8}
    notes = market.market_notes(snap, {'ATR14百分比': 1.2})
    joined = ' '.join(notes)
    assert '多头比较拥挤' in joined, joined
    assert '散户做多' in joined, joined
    assert 'ATR' in joined, joined
    snap2 = {'资金费率': -0.001, '多空比': 0.5}
    joined2 = ' '.join(market.market_notes(snap2))
    assert '逼空' in joined2, joined2

def t_notes_empty():
    assert isinstance(market.market_notes({}, {}), list)

for n, f in [('币种代码归一化', t_normalize), ('OKX 合约代码', t_okx_inst),
             ('K线指标计算', t_indicators), ('K线数据不足不报错', t_indicators_short_data),
             ('行情文字解读', t_notes), ('空行情不报错', t_notes_empty)]:
    check(n, f)


# ---------------- 复盘 ----------------
section('复盘')

def t_rule_review():
    d = journal.load(TMP)
    findings, keys = review.rule_review(d, start_equity=1000)
    assert isinstance(findings, list) and len(findings) > 0
    assert '总净盈亏' in keys
    assert any('总账' in f for f in findings), findings

def t_rule_review_empty():
    findings, keys = review.rule_review(journal.empty_df())
    assert len(findings) == 1 and keys == {}

def t_revenge():
    d = pd.DataFrame([
        {'开仓时间': '2026-09-01 10:00', '平仓时间': '2026-09-01 11:00',
         '平仓价': 101.0, '净盈亏': -10.0, '方向': '多'},
        {'开仓时间': '2026-09-01 11:10', '平仓时间': '2026-09-01 12:00',
         '平仓价': 99.0, '净盈亏': -5.0, '方向': '多'},
        {'开仓时间': '2026-09-02 10:00', '平仓时间': '2026-09-02 11:00',
         '平仓价': 102.0, '净盈亏': 20.0, '方向': '多'},
    ])
    d['开仓时间'] = pd.to_datetime(d['开仓时间'])
    d['平仓时间'] = pd.to_datetime(d['平仓时间'])
    rev, ratio = review.revenge_trades(d)
    assert len(rev) == 1, f'应识别出 1 笔报复性交易，实际 {len(rev)}'
    approx(ratio, 1 / 3 * 100)

def t_prompt_build():
    d = journal.load(TMP)
    p = review.build_prompt(d, '我是不是交易太频繁？', start_equity=1000)
    assert p and '我是不是交易太频繁' in p
    assert '不要预测价格' in p
    assert 'BTCUSDT' in p

def t_prompt_empty():
    assert review.build_prompt(journal.empty_df()) is None

for n, f in [('规则复盘能产出结论', t_rule_review),
             ('空数据复盘不报错', t_rule_review_empty),
             ('识别报复性交易', t_revenge),
             ('提示词构造正确', t_prompt_build),
             ('空数据不构造提示词', t_prompt_empty)]:
    check(n, f)


# ---------------- 语法检查 ----------------
section('文件语法')

def t_compile_app():
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ['app.py', 'common.py', 'risk.py', 'journal.py',
                 'metrics.py', 'market.py', 'review.py']:
        path = os.path.join(base, name)
        with open(path, encoding='utf-8') as f:
            compile(f.read(), path, 'exec')

check('所有 py 文件可编译', t_compile_app)


# ---------------- 行情快照降级 ----------------
section('行情快照降级')

def _fake_fail(sym):
    raise RuntimeError('模拟接口挂了')

def t_snapshot_fallback():
    """币安、OKX 都挂了时，应该自动降级到 Bybit。"""
    orig = dict(market.FETCHERS)
    market.FETCHERS['binance'] = _fake_fail
    market.FETCHERS['okx'] = _fake_fail
    market.FETCHERS['bybit'] = lambda sym: {'标记价': 100.0, '交易所': 'Bybit'}
    try:
        snap = market.snapshot('BTC', '自动')
        assert snap['数据源'] == 'bybit', snap
        assert snap['标记价'] == 100.0
        assert len(snap['警告']) == 2, '应记录两条失败原因'
        import re as _re
        assert _re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$',
                         snap['获取时间']), \
            f'获取时间格式不对：{snap["获取时间"]}'
    finally:
        market.FETCHERS.clear()
        market.FETCHERS.update(orig)

def t_snapshot_all_fail():
    """全部失败时应该报错而不是静默返回空值。"""
    orig = dict(market.FETCHERS)
    for k in list(orig):
        market.FETCHERS[k] = _fake_fail
    try:
        try:
            market.snapshot('BTC')
        except RuntimeError as e:
            assert '所有数据源都失败了' in str(e), str(e)
        else:
            raise AssertionError('应该抛错')
    finally:
        market.FETCHERS.clear()
        market.FETCHERS.update(orig)

for n, f in [('接口挂了能自动切换数据源', t_snapshot_fallback),
             ('全部接口挂了能报错', t_snapshot_all_fail)]:
    check(n, f)


# ---------------- 示例数据 ----------------
section('示例数据')

DEMO_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', '示例交易记录.csv')

def t_demo_load():
    d = journal.load(DEMO_CSV)
    assert len(d) == 18, f'示例数据应该是 18 笔，实际 {len(d)}'
    assert d['净盈亏'].notna().sum() == 18, '每笔都应该算出净盈亏'
    assert d['止损价'].isna().sum() == 1, '示例数据里应该有一笔没设止损'
    assert d.loc[d['止损价'].isna(), 'R倍数'].isna().all(), '没止损就算不出 R'

def t_demo_metrics():
    d = journal.load(DEMO_CSV)
    s = metrics.summary(d, start_equity=1000)
    assert s['交易笔数'] == 18
    assert s['盈利笔数'] == 7 and s['亏损笔数'] == 11
    assert s['总净盈亏'] < 0
    assert s['盈利因子'] < 1, '这份示例是一个亏损账户'
    assert s['最大连亏'] == 3
    assert s['最大回撤'] > 0

def t_demo_review():
    d = journal.load(DEMO_CSV)
    findings, keys = review.rule_review(d, start_equity=1000)
    joined = ' '.join(findings)
    assert '总账' in joined
    assert '手续费' in joined
    assert '形成死伤' not in joined
    assert '数学硬伤' in joined, '胜率 39% + 盈亏比 0.78 必须被点名'

for n, f in [('示例数据能读取', t_demo_load),
             ('示例数据指标合理', t_demo_metrics),
             ('示例数据能复盘', t_demo_review)]:
    check(n, f)


# ---------------- 界面冒烟 ----------------
section('界面冒烟测试')

def _app(path):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(path, default_timeout=180)
    at.run()
    return at

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')

def t_app_render():
    at = _app(APP)
    assert not at.exception, f'界面渲染报错：{[e.value for e in at.exception]}'
    assert len(at.tabs) == 9, f'应该是 9 个页签，实际 {len(at.tabs)}'
    labels = [m.label for m in at.metric]
    assert '今天已交易' in labels and '当前连亏' in labels

def t_app_precheck():
    at = _app(APP)
    for ni in at.number_input:
        if ni.label == '开仓价':
            ni.set_value(62000.0)
        elif ni.label == '止损价（必填）':
            ni.set_value(61000.0)
        elif ni.label.startswith('目标价'):
            ni.set_value(64500.0)
    [b for b in at.button if '检查这一单' in b.label][0].click()
    at.run()
    assert not at.exception, f'报错：{[e.value for e in at.exception]}'
    # ⚠️ 不能数 success 个数 —— 后台任务完成也会产生 success
    concl = [x.value for x in at.success if '结论' in str(x.value)]
    assert concl, f'应输出开仓结论，实际 success={[str(x.value)[:40] for x in at.success]}'
    assert '通过' in concl[0]
    vals = {m.label: m.value for m in at.metric}
    assert '建议开仓数量' in vals
    assert '止损时亏损' in vals
    # 1000 本金 × 1% 风险 = 止损应亏 10 USDT
    assert '10.00 USDT' in vals['止损时亏损'], vals['止损时亏损']

def t_app_reject_no_stop():
    """不填止损就点检查，应该直接报错。"""
    at = _app(APP)
    for ni in at.number_input:
        if ni.label == '开仓价':
            ni.set_value(62000.0)
    [b for b in at.button if '检查这一单' in b.label][0].click()
    at.run()
    assert not at.exception
    assert len(at.error) >= 1, '没填止损应该报错'

for n, f in [('界面能正常渲染', t_app_render),
             ('开仓前检查按钮可用', t_app_precheck),
             ('没填止损会报错', t_app_reject_no_stop)]:
    check(n, f)


# ---------------- 市场监控 ----------------
section('市场监控')

SNAP = {'标记价': 62000.0, '资金费率': 0.0012, '多空比': 2.1,
        '24h涨跌幅': -7.5, '24h最高': 65000.0, '24h最低': 61000.0}
IND = {'ATR14百分比': 4.2}

def _rule(t, th, lvl='警告'):
    return {'编号': 'R001', '币种': 'BTCUSDT', '类型': t, '阈值': th,
            '等级': lvl, '冷却分钟': 30, '启用': True}

def t_ev_price():
    ok, msg = monitor.evaluate(_rule('价格上破', 61000), SNAP)
    assert ok and '62200' not in msg and '上破' in msg, msg
    ok, _ = monitor.evaluate(_rule('价格上破', 63000), SNAP)
    assert not ok
    ok, msg = monitor.evaluate(_rule('价格下破', 63000), SNAP)
    assert ok and '下破' in msg

def t_ev_funding():
    ok, msg = monitor.evaluate(_rule('资金费率超过', 0.05), SNAP)
    assert ok and '资金费率' in msg, msg
    assert '0.1200' in msg, msg
    ok, _ = monitor.evaluate(_rule('资金费率超过', 0.5), SNAP)
    assert not ok, '阈值 0.5% 不该触发'
    # 负费率也应该触发（取绝对值）
    ok, msg = monitor.evaluate(_rule('资金费率超过', 0.05), {'标记价': 100, '资金费率': -0.002})
    assert ok and '空头拥挤' in msg, msg

def t_ev_ls():
    ok, msg = monitor.evaluate(_rule('多空比高于', 1.8), SNAP)
    assert ok and '散户做多' in msg, msg
    ok, _ = monitor.evaluate(_rule('多空比高于', 2.5), SNAP)
    assert not ok
    ok, msg = monitor.evaluate(_rule('多空比低于', 0.6), {'标记价': 1, '多空比': 0.5})
    assert ok and '散户做空' in msg, msg

def t_ev_vol_chg():
    ok, msg = monitor.evaluate(_rule('24h涨跌幅超过', 5), SNAP)
    assert ok and '-7.50' in msg, msg
    ok, _ = monitor.evaluate(_rule('24h涨跌幅超过', 10), SNAP)
    assert not ok

def t_ev_atr():
    ok, msg = monitor.evaluate(_rule('ATR超过', 3), SNAP, IND)
    assert ok and 'ATR' in msg, msg
    ok, _ = monitor.evaluate(_rule('ATR超过', 5), SNAP, IND)
    assert not ok

def t_ev_missing_data():
    """数据缺失时不能误报，也不能崩。"""
    for r in ['价格上破', '24h涨跌幅超过', '资金费率超过', '多空比高于']:
        ok, msg = monitor.evaluate(_rule(r, 0.0001), {})
        assert not ok and msg is None, f'{r} 缺数据不该触发'
    ok, _ = monitor.evaluate(_rule('ATR超过', 0.0001), SNAP, None)
    assert not ok, '没有指标数据不该触发'
    ok, _ = monitor.evaluate(_rule('距爆仓不足', 5), SNAP, IND, None)
    assert not ok, '没有持仓不该触发'

def t_position_health():
    longp = {'币种': 'BTCUSDT', '方向': '多', '开仓价': 100, '数量': 1,
             '杠杆': 10, '止损价': 96, '爆仓价': 90}
    h = monitor.position_health(longp, 95)
    approx(h['浮动盈亏'], -5.0)
    approx(h['距爆仓百分比'], (95 - 90) / 95 * 100, 1e-6)
    approx(h['距止损百分比'], (95 - 96) / 95 * 100, 1e-6)   # 负数=已穿过止损

    shortp = {'币种': 'ETHUSDT', '方向': '空', '开仓价': 100, '数量': 1,
              '杠杆': 10, '止损价': 104, '爆仓价': 110}
    h2 = monitor.position_health(shortp, 105)
    approx(h2['浮动盈亏'], -5.0)
    approx(h2['距爆仓百分比'], (110 - 105) / 105 * 100, 1e-6)
    approx(h2['距止损百分比'], (104 - 105) / 105 * 100, 1e-6)

def t_ev_liq_alert():
    pos = {'币种': 'BTCUSDT', '方向': '多', '开仓价': 100, '数量': 1,
           '杠杆': 10, '止损价': 96, '爆仓价': 90}
    h = monitor.position_health(pos, 92)
    h['_liq'] = 90
    ok, msg = monitor.evaluate(_rule('距爆仓不足', 5, '紧急'), SNAP, None, h)
    assert ok and '爆仓' in msg, msg
    ok, _ = monitor.evaluate(_rule('距爆仓不足', 1, '紧急'), SNAP, None, h)
    assert not ok, f'距爆仓 {h["距爆仓百分比"]:.1f}% 不该触发 1% 警戒线'
    # 已经跌穿爆仓价 -> 必须触发
    h2 = monitor.position_health(pos, 89)
    h2['_liq'] = 90
    ok, msg = monitor.evaluate(_rule('距爆仓不足', 1, '紧急'), SNAP, None, h2)
    assert ok and '已经触及' in msg, msg

def t_ev_stop_alert():
    pos = {'币种': 'BTCUSDT', '方向': '多', '开仓价': 100, '数量': 1,
           '杠杆': 10, '止损价': 96, '爆仓价': 90}
    h = monitor.position_health(pos, 96.5)
    h['_liq'] = 90
    ok, msg = monitor.evaluate(_rule('距止损不足', 1), SNAP, None, h)
    assert ok and '止损' in msg, msg
    h2 = monitor.position_health(pos, 95)
    h2['_liq'] = 90
    ok, msg = monitor.evaluate(_rule('距止损不足', 1), SNAP, None, h2)
    assert ok and '穿过' in msg, msg

def t_decide_cooldown():
    import datetime as _dt
    state = {}
    r = {'编号': 'R001', '冷却分钟': 30}
    t0 = _dt.datetime(2026, 9, 22, 10, 0, 0)
    assert monitor.decide(r, True, state, t0) is True
    assert monitor.decide(r, True, state, t0 + _dt.timedelta(minutes=5)) is False, '冷却期内不该重复报'
    assert monitor.decide(r, True, state, t0 + _dt.timedelta(minutes=31)) is True, '过了冷却该再提醒一次'
    # 条件恢复 -> 重新上膛
    assert monitor.decide(r, False, state, t0 + _dt.timedelta(minutes=32)) is False
    assert state['R001']['armed'] is True, '条件恢复后应重新上膛'
    assert monitor.decide(r, True, state, t0 + _dt.timedelta(minutes=33)) is True, '重新上膛后应立刻触发'

def t_decide_not_triggered():
    import datetime as _dt
    state = {}
    r = {'编号': 'R002', '冷却分钟': 30}
    assert monitor.decide(r, False, state, _dt.datetime(2026, 9, 22)) is False

def t_rules_crud():
    """规则的增删改查（用临时文件，不碰真实配置）。"""
    saved_rules, saved_state = monitor.RULES_PATH, monitor.STATE_PATH
    monitor.RULES_PATH = os.path.join(os.path.dirname(saved_rules), '_自测_规则.json')
    try:
        if os.path.exists(monitor.RULES_PATH):
            os.remove(monitor.RULES_PATH)
        assert monitor.load_rules() == []
        monitor.add_rule('btc', '价格上破', 70000, '警告', 30, '测试')
        monitor.add_rule('ETH', '资金费率超过', 0.1, '警告', 30)
        rs = monitor.load_rules()
        assert len(rs) == 2 and rs[0]['编号'] == 'R001' and rs[1]['编号'] == 'R002'
        assert rs[0]['币种'] == 'BTC', rs[0]
        monitor.toggle_rule('R001')
        assert monitor.load_rules()[0]['启用'] is False
        monitor.delete_rule('R001')
        rs = monitor.load_rules()
        assert len(rs) == 1 and rs[0]['编号'] == 'R001', '删完应该重新编号'
        try:
            monitor.add_rule('BTC', '不存在的类型', 1)
        except ValueError:
            pass
        else:
            raise AssertionError('非法规则类型应该报错')
    finally:
        if os.path.exists(monitor.RULES_PATH):
            os.remove(monitor.RULES_PATH)
        monitor.RULES_PATH = saved_rules
        monitor.STATE_PATH = saved_state

def t_collect_symbols():
    syms = monitor.collect_symbols(
        [{'币种': 'btc', '启用': True}, {'币种': 'ETH', '启用': False}],
        [{'币种': 'SOL'}])
    assert syms == ['SOL', 'btc'], syms

for n, f in [('价格类规则', t_ev_price), ('资金费率规则', t_ev_funding),
             ('多空比规则', t_ev_ls), ('涨跌幅规则', t_ev_vol_chg),
             ('ATR 规则', t_ev_atr), ('数据缺失不误报', t_ev_missing_data),
             ('持仓健康度计算', t_position_health),
             ('距爆仓警报', t_ev_liq_alert), ('距止损警报', t_ev_stop_alert),
             ('冷却与重新上膛', t_decide_cooldown),
             ('未触发时不上锁', t_decide_not_triggered),
             ('规则增删改查', t_rules_crud),
             ('待监控币种汇总', t_collect_symbols)]:
    check(n, f)


# ---------------- 交易所持仓同步 ----------------
section('交易所持仓同步')


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class _Capture:
    """假 requests.get，记录调用参数并返回预设数据。"""
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
        self.url, self.kwargs = None, None

    def __call__(self, url, **kw):
        self.url, self.kwargs = url, kw
        return _Resp(self.payload, self.status)


def _with_fake(payload, exchange_fn, *args, status=200):
    cap = _Capture(payload, status)
    orig = exchange_sync.requests.get
    exchange_sync.requests.get = cap
    try:
        return exchange_fn(*args), cap
    finally:
        exchange_sync.requests.get = orig


BN = [
    {'symbol': 'BTCUSDT', 'positionAmt': '0.010', 'entryPrice': '86000',
     'markPrice': '86200', 'unRealizedProfit': '2.0', 'liquidationPrice': '78000',
     'leverage': '10', 'isolatedMargin': '86'},
    {'symbol': 'ETHUSDT', 'positionAmt': '0', 'entryPrice': '0', 'markPrice': '2500',
     'unRealizedProfit': '0', 'liquidationPrice': '0', 'leverage': '10',
     'isolatedMargin': '0'},
    {'symbol': 'SOLUSDT', 'positionAmt': '-2.5', 'entryPrice': '150', 'markPrice': '148',
     'unRealizedProfit': '5', 'liquidationPrice': '165', 'leverage': '5',
     'isolatedMargin': '75'},
]

def t_mask():
    assert 'SECRET123456' not in exchange_sync.mask('SECRET123456')
    assert exchange_sync.mask('') == '（未配置）'
    assert exchange_sync.mask('abc') == '***'
    assert exchange_sync.mask('abcdefghijklm').startswith('abcd')

def t_has_credentials():
    """不能依赖真实 .env —— 用桩，否则本机配了 Key 这个测试就会挂。"""
    orig = exchange_sync.get_env
    try:
        # 什么都没配
        exchange_sync.get_env = lambda k, d=None: None
        ok, msg = exchange_sync.has_credentials('币安')
        assert ok is False and 'BINANCE_API_KEY' in msg, msg
        assert 'BINANCE_API_SECRET' in msg, msg
        assert exchange_sync.has_credentials('不存在的所')[0] is False

        # 只配了一半（币安需要两个）
        exchange_sync.get_env = lambda k, d=None: 'v' if k == 'BINANCE_API_KEY' else None
        ok, msg = exchange_sync.has_credentials('币安')
        assert ok is False and 'BINANCE_API_SECRET' in msg, msg

        # 全配好
        exchange_sync.get_env = lambda k, d=None: 'v'
        assert exchange_sync.has_credentials('币安')[0] is True

        # OKX 需要第三个字段 Passphrase
        exchange_sync.get_env = lambda k, d=None: None if k == 'OKX_PASSPHRASE' else 'v'
        ok, msg = exchange_sync.has_credentials('欧易OKX')
        assert ok is False and 'OKX_PASSPHRASE' in msg, msg
    finally:
        exchange_sync.get_env = orig

def t_binance_parse():
    pos, cap = _with_fake(BN, exchange_sync.binance_positions, 'k', 's')
    assert len(pos) == 2, f'应该过滤掉 0 持仓，实际 {len(pos)}'
    btc = pos[0]
    assert btc['币种'] == 'BTCUSDT' and btc['方向'] == '多'
    approx(btc['数量'], 0.01)
    approx(btc['开仓价'], 86000)
    approx(btc['爆仓价'], 78000)
    approx(btc['杠杆'], 10)
    assert pos[1]['方向'] == '空' and pos[1]['币种'] == 'SOLUSDT'
    # 签名与请求头
    assert 'signature=' in cap.url and 'timestamp=' in cap.url and 'recvWindow=' in cap.url
    assert cap.kwargs['headers']['X-MBX-APIKEY'] == 'k'

def t_binance_sign_correct():
    """用独立算法复算签名，确认拼接方式没错。"""
    import hashlib, hmac, re
    _, cap = _with_fake(BN, exchange_sync.binance_positions, 'mykey', 'mysecret')
    qs = cap.url.split('?', 1)[1]
    unsigned = re.sub(r'&signature=[0-9a-f]+$', '', qs)
    expect = hmac.new(b'mysecret', unsigned.encode(), hashlib.sha256).hexdigest()
    assert f'signature={expect}' in cap.url, '币安签名拼接不对'
    assert unsigned.startswith('timestamp=') and 'recvWindow=5000' in unsigned

def t_okx_parse():
    payload = {'code': '0', 'data': [
        {'instId': 'BTC-USDT-SWAP', 'pos': '10', 'posSide': 'long', 'avgPx': '86000',
         'markPx': '86200', 'upl': '2', 'liqPx': '78000', 'lever': '10',
         'margin': '86', 'notionalUsd': '862'},
        {'instId': 'ETH-USDT-SWAP', 'pos': '0', 'posSide': 'long', 'avgPx': '0',
         'markPx': '2500', 'upl': '0', 'liqPx': '0', 'lever': '10',
         'margin': '0', 'notionalUsd': '0'},
    ]}
    pos, cap = _with_fake(payload, exchange_sync.okx_positions, 'k', 's', 'p')
    assert len(pos) == 1, f'应过滤 0 持仓，实际 {len(pos)}'
    btc = pos[0]
    assert btc['币种'] == 'BTCUSDT', btc
    assert btc['方向'] == '多'
    approx(btc['数量'], 862 / 86200, 1e-9)     # 张数 -> 币数
    h = cap.kwargs['headers']
    assert h['OK-ACCESS-KEY'] == 'k' and h['OK-ACCESS-PASSPHRASE'] == 'p'
    assert len(h['OK-ACCESS-SIGN']) > 20 and 'T' in h['OK-ACCESS-TIMESTAMP']
    assert '/api/v5/account/positions' in cap.url

def t_bybit_parse():
    payload = {'retCode': 0, 'result': {'list': [
        {'symbol': 'BTCUSDT', 'side': 'Buy', 'size': '0.02', 'avgPrice': '86000',
         'markPrice': '86200', 'unrealisedPnl': '4', 'liqPrice': '78000',
         'leverage': '10', 'positionIM': '172'},
        {'symbol': 'ETHUSDT', 'side': 'Sell', 'size': '0', 'avgPrice': '0',
         'markPrice': '2500', 'unrealisedPnl': '0', 'liqPrice': '0',
         'leverage': '10', 'positionIM': '0'},
    ]}}
    pos, cap = _with_fake(payload, exchange_sync.bybit_positions, 'k', 's')
    assert len(pos) == 1
    assert pos[0]['方向'] == '多' and pos[0]['币种'] == 'BTCUSDT'
    approx(pos[0]['数量'], 0.02)
    h = cap.kwargs['headers']
    assert h['X-BAPI-API-KEY'] == 'k' and h['X-BAPI-RECV-WINDOW'] == '5000'
    # Bybit 签名 = hmac(时间戳 + key + recvWindow + queryString)
    import hashlib, hmac
    qs = cap.url.split('?', 1)[1]
    expect = hmac.new(b's', (h['X-BAPI-TIMESTAMP'] + 'k' + '5000' + qs).encode(),
                      hashlib.sha256).hexdigest()
    assert h['X-BAPI-SIGN'] == expect, 'Bybit 签名拼接不对'

def t_error_no_leak():
    """报错信息里绝对不能出现密钥。"""
    secret = 'SUPER_SECRET_VALUE_12345'
    for payload, status in [({'code': -2015, 'msg': 'Invalid API-key'}, 401),
                            ({'msg': 'boom'}, 500)]:
        try:
            _with_fake(payload, exchange_sync.binance_positions, 'mykey', secret, status=status)
        except Exception as e:
            assert secret not in str(e), f'报错泄露了密钥：{e}'
            assert 'mykey' not in str(e), f'报错泄露了 Key：{e}'
        else:
            raise AssertionError(f'HTTP {status} 应该报错')

def t_error_401_message():
    try:
        _with_fake({}, exchange_sync.binance_positions, 'k', 's', status=401)
    except PermissionError as e:
        assert '读取' in str(e) and '权限' in str(e), str(e)
    else:
        raise AssertionError('401 应该抛 PermissionError')

def t_fetch_unsupported():
    try:
        exchange_sync.fetch_positions('火币')
    except ValueError as e:
        assert '暂不支持' in str(e)
    else:
        raise AssertionError('不支持的交易所应该报错')

def t_diff_open_add():
    old = [{'币种': 'BTCUSDT', '方向': '多', '数量': 0.01}]
    new = [{'币种': 'BTCUSDT', '方向': '多', '数量': 0.03},
           {'币种': 'ETHUSDT', '方向': '空', '数量': 1.0}]
    ev = {e['类型']: e for e in exchange_sync.diff_positions(old, new)}
    assert '加仓' in ev and '开仓' in ev, ev
    assert ev['开仓']['说明'].startswith('ETHUSDT')

def t_diff_close_reduce():
    old = [{'币种': 'BTCUSDT', '方向': '多', '数量': 0.03},
           {'币种': 'SOLUSDT', '方向': '多', '数量': 2}]
    new = [{'币种': 'BTCUSDT', '方向': '多', '数量': 0.01}]
    ev = {e['类型']: e for e in exchange_sync.diff_positions(old, new)}
    assert '减仓' in ev and '平仓' in ev, ev
    assert 'SOLUSDT' in ev['平仓']['说明']

def t_diff_flip():
    old = [{'币种': 'BTCUSDT', '方向': '多', '数量': 0.01}]
    new = [{'币种': 'BTCUSDT', '方向': '空', '数量': 0.01}]
    ev = exchange_sync.diff_positions(old, new)
    assert len(ev) == 1 and ev[0]['类型'] == '反手', ev

def t_diff_no_change():
    old = [{'币种': 'BTCUSDT', '方向': '多', '数量': 0.01}]
    assert exchange_sync.diff_positions(old, list(old)) == []

def t_sync_to_local_keeps_stop():
    """同步时应该保留你手动填的止损价（交易所接口不返回它）。"""
    saved = monitor.POSITIONS_PATH
    monitor.POSITIONS_PATH = os.path.join(os.path.dirname(saved), '_自测_持仓.json')
    try:
        if os.path.exists(monitor.POSITIONS_PATH):
            os.remove(monitor.POSITIONS_PATH)
        monitor.save_positions([{'币种': 'BTCUSDT', '方向': '多', '开仓价': 85000,
                                 '数量': 0.01, '杠杆': 10, '止损价': 83000,
                                 '爆仓价': 77000}])
        got = exchange_sync.sync_to_local([{
            '交易所': '币安', '币种': 'BTCUSDT', '方向': '多', '数量': 0.02,
            '开仓价': 86000, '标记价': 86200, '杠杆': 10,
            '未实现盈亏': 4.0, '爆仓价': 78000}])
        assert len(got) == 1
        approx(got[0]['止损价'], 83000, 1e-9)      # 止损保留
        approx(got[0]['开仓价'], 86000, 1e-9)      # 其他用交易所的
        approx(got[0]['数量'], 0.02, 1e-9)
        assert got[0]['同步时间']
        assert monitor.load_positions()[0]['止损价'] == 83000
    finally:
        if os.path.exists(monitor.POSITIONS_PATH):
            os.remove(monitor.POSITIONS_PATH)
        monitor.POSITIONS_PATH = saved

def _report(d, status=200):
    r, _ = _with_fake(d, exchange_sync.binance_key_report, 'k', 's', status=status)
    return r

def t_whitelist_error_shows_ip():
    """IP 不在白名单时，报错要直接告诉我们币安看到的 IP 是多少。"""
    payload = {'code': -2015,
               'msg': 'Invalid API-key, IP, or permissions for action, '
                      'request ip: 203.0.113.77'}
    try:
        _with_fake(payload, exchange_sync.binance_positions, 'k', 's', status=401)
    except PermissionError as e:
        msg = str(e)
        assert '203.0.113.77' in msg, f'报错里应该带上币安看到的 IP：{msg}'
        assert 'IP 白名单' in msg
        assert 'k' != msg and 'mykey' not in msg
    else:
        raise AssertionError('401 应该报错')

def t_no_ip_in_error_still_works():
    """报错里没有 IP 信息时，也不能崩，给通用提示即可。"""
    try:
        _with_fake({'code': -2015, 'msg': 'Invalid API-key'},
                   exchange_sync.binance_positions, 'k', 's', status=401)
    except PermissionError as e:
        assert '拒绝了请求' in str(e)
        assert '白名单' in str(e), '即使没有具体 IP，也要提到白名单这个可能'
    else:
        raise AssertionError('应该报错')

def t_extract_ip_variants():
    """各种错误格式都要能抠出 IP。"""
    class R:
        def __init__(self, d): self._d = d
        def json(self): return self._d
    cases = [
        ({'msg': 'Invalid API-key, IP, or permissions for action, request ip: 1.2.3.4'},
         '1.2.3.4'),
        ({'msg': 'request ip: 8.8.8.8'}, '8.8.8.8'),
        ({'msg': '请求IP: 9.9.9.9'}, None),
        ({'msg': 'no ip here'}, None),
        ('not a dict', None),
    ]
    for payload, want in cases:
        got = exchange_sync._request_ip_hint(R(payload))
        assert got == want, f'{payload} -> 期望 {want}，实际 {got}'

def t_key_report_dangerous():
    """开了提现权限 = 最高危险等级，必须明确警告。"""
    rep = _report({'enableReading': True, 'enableFutures': True,
                   'enableWithdrawals': True, 'ipRestrict': True,
                   'createTime': 1700000000000})
    assert rep['风险等级'] == '危险', rep
    assert any('立刻去币安关掉' in t for t in rep['建议']), rep['建议']

def t_key_report_no_ip_whitelist():
    """有合约权限但没绑 IP 白名单 = 风险偏高，要给出补救建议。"""
    rep = _report({'enableReading': True, 'enableFutures': True,
                   'enableWithdrawals': False, 'ipRestrict': False})
    assert rep['风险等级'] == '偏高', rep
    assert any('IP 白名单' in t for t in rep['建议']), rep['建议']
    assert any('补上这个洞' in t for t in rep['建议'])

def t_key_report_acceptable():
    """合约权限 + IP 白名单 = 本场景能做到的最稳配置。"""
    rep = _report({'enableReading': True, 'enableFutures': True,
                   'enableWithdrawals': False, 'ipRestrict': True,
                   'enableSpotAndMarginTrading': False})
    assert rep['风险等级'] == '可接受', rep
    assert any('最稳配置' in t for t in rep['建议']), rep['建议']

def t_key_report_safe_no_futures():
    """没开合约权限最安全，但要提醒同步功能用不了。"""
    rep = _report({'enableReading': True, 'enableFutures': False,
                   'enableWithdrawals': False, 'ipRestrict': False})
    assert rep['风险等级'] == '安全', rep
    assert any('读不到 U 本位合约持仓' in t for t in rep['建议']), rep['建议']

def t_key_report_extra_perms():
    """现货交易、划转这些用不上的权限要被点出来。"""
    rep = _report({'enableReading': True, 'enableFutures': True,
                   'enableWithdrawals': False, 'ipRestrict': True,
                   'enableSpotAndMarginTrading': True,
                   'permitsUniversalTransfer': True})
    j = ' '.join(rep['建议'])
    assert '现货交易权限' in j and '划转权限' in j, rep['建议']

def t_key_report_unavailable():
    """接口不可用时返回 None，而不是抛异常或假装正常。"""
    assert _report({}, status=403) is None
    assert _report({}, status=500) is None
    orig = exchange_sync.requests.get
    exchange_sync.requests.get = lambda *a, **k: (_ for _ in ()).throw(
        exchange_sync.requests.ConnectionError('no net'))
    try:
        assert exchange_sync.binance_key_report('k', 's') is None
    finally:
        exchange_sync.requests.get = orig

def t_key_report_no_creds():
    assert exchange_sync.binance_key_report('', '') is None

for n, f in [('白名单报错会显示真实IP', t_whitelist_error_shows_ip),
             ('报错没有IP也不崩', t_no_ip_in_error_still_works),
             ('各种格式都能抠出IP', t_extract_ip_variants),
             ('权限体检-提现最危险', t_key_report_dangerous),
             ('权限体检-没绑IP白名单', t_key_report_no_ip_whitelist),
             ('权限体检-合约+IP白名单', t_key_report_acceptable),
             ('权限体检-没开合约权限', t_key_report_safe_no_futures),
             ('权限体检-点出多余权限', t_key_report_extra_perms),
             ('权限体检-接口挂了返回None', t_key_report_unavailable),
             ('权限体检-没配Key返回None', t_key_report_no_creds),
             ('密钥打码', t_mask), ('检测 Key 是否配置', t_has_credentials),
             ('币安持仓解析', t_binance_parse), ('币安签名拼接', t_binance_sign_correct),
             ('OKX 持仓解析与折算', t_okx_parse), ('Bybit 持仓解析', t_bybit_parse),
             ('报错不泄露密钥', t_error_no_leak), ('401 给出人话提示', t_error_401_message),
             ('不支持的交易所报错', t_fetch_unsupported),
             ('识别开仓与加仓', t_diff_open_add), ('识别减仓与平仓', t_diff_close_reduce),
             ('识别反手', t_diff_flip), ('持仓没变不产生事件', t_diff_no_change),
             ('同步保留止损价', t_sync_to_local_keeps_stop)]:
    check(n, f)


# ---------------- 多智能体判断 ----------------
section('多智能体判断')

SNAP_MA = {'标记价': 86000.0, '资金费率': 0.0012, '多空比': 2.1,
           '多头账户占比': 0.677, '持仓量': 123456.0, '24h成交额': 9.9e9,
           '24h涨跌幅': -3.2, '24h最高': 88500.0, '24h最低': 84200.0,
           '交易所': '币安', 'K线': []}
IND_MA = {'MA20': 85000.0, 'MA60': 83000.0, '距MA20百分比': 1.18,
          '距MA60百分比': 3.61, 'ATR14百分比': 2.4, '近期高点': 89000.0,
          '近期低点': 84000.0, '距近期高点百分比': -3.37,
          '距近期低点百分比': 2.38, '近24小时涨跌幅': -3.0}

def _brief_of(name):
    for a in agents.ANALYSTS:
        if a['名称'] == name:
            return a['brief'](SNAP_MA, IND_MA)
    raise AssertionError(name)

def t_analysts_see_different_data():
    """核心设计：每个分析师只看自己的数据切片，否则多 agent 就是摆设。"""
    tech = _brief_of('技术面分析师')
    fund = _brief_of('资金面分析师')
    sent = _brief_of('情绪面分析师')
    risk = _brief_of('风控官')

    def ks(b):
        return ' | '.join(b.keys())
    assert 'MA20' in ks(tech) and '资金费率' not in ks(tech) \
        and '多空比' not in ks(tech), '技术面不该看到费率和多空比：' + ks(tech)
    assert '资金费率' in ks(fund) and 'MA20' not in ks(fund) \
        and '多空比' not in ks(fund), '资金面不该看到均线和多空比：' + ks(fund)
    assert '多空比' in ks(sent) and 'MA20' not in ks(sent) \
        and '资金费率' not in ks(sent), '情绪面不该看到均线和费率：' + ks(sent)
    assert 'MA20' not in ks(risk), '风控官不该看均线（那会诱导他判断方向）'
    assert 'ATR占价格百分比' in risk, '风控官必须看到波动率'
    # 四个人的字段集合不应该完全一样
    sets = [set(x.keys()) for x in (tech, fund, sent, risk)]
    assert len({frozenset(x) for x in sets}) == 4, '四份数据切片必须各不相同'

def t_range_pos():
    assert agents._range_pos({'24h最高': 200, '24h最低': 100,
                              '标记价': 125}) == '25.0%'
    assert agents._range_pos({'24h最高': 200, '24h最低': 100,
                              '标记价': 200}) == '100.0%'
    assert agents._range_pos({'24h最高': 100, '24h最低': 100,
                              '标记价': 100}) == '无数据'
    assert agents._range_pos({}) == '无数据'

def t_extract_json():
    good = '{"方向": "偏多", "信心": 70}'
    assert agents._extract_json(good)['方向'] == '偏多'
    fenced = '```json\n{"方向": "偏空", "信心": 55}\n```'
    assert agents._extract_json(fenced)['信心'] == 55
    noisy = '好的，我的分析如下：\n{"方向": "中性", "信心": 40}\n以上。'
    assert agents._extract_json(noisy)['方向'] == '中性'
    assert agents._extract_json('完全不是 JSON') is None
    assert agents._extract_json('') is None

def t_judge():
    assert agents._judge('偏多', 1.5) is True
    assert agents._judge('偏多', -1.5) is False
    assert agents._judge('偏空', -2.0) is True
    assert agents._judge('偏空', 2.0) is False
    assert agents._judge('中性', 5.0) is None, '中性不参与准确率统计'
    assert agents._judge('无法判断', 5.0) is None

def t_analyze_end_to_end():
    """打桩跑一遍完整流程：应调用 5 次模型（4 分析师 + 1 主持人）。"""
    import market as mk
    saved = (mk.snapshot, mk.compute_indicators, agents.call_llm, agents.market.snapshot,
             agents.market.compute_indicators)
    calls = []

    def fake_call(system, user, model=None, api_key=None, **kw):
        calls.append(system[:20])
        if '主持人' in system:
            return json.dumps({'方向': '偏空', '信心': 62, '共识': '都偏谨慎',
                               '分歧': '技术面偏多，资金面偏空',
                               '综合判断': '整体偏空但分歧明显',
                               '最重要的反面证据': '价格仍在MA20上方',
                               '什么情况下我错了': '价格站上89000',
                               '给交易者的提醒': '轻仓'}, ensure_ascii=False), {'total_tokens': 500}, 'm'
        for key, val in [('技术面', '偏多'), ('资金面', '偏空'),
                         ('情绪面', '偏空'), ('风控官', '中性')]:
            if key in system:
                return json.dumps({'方向': val, '信心': 65, '核心理由': '理由',
                                   '主要风险': '风险', '什么情况下我错了': '条件',
                                   '我看不到什么': '看不到'}, ensure_ascii=False), \
                    {'total_tokens': 300}, 'm'
        raise AssertionError('未识别的 system：' + system[:40])

    agents.call_llm = fake_call
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    try:
        r = agents.analyze_symbol('BTC')
    finally:
        mk.snapshot, mk.compute_indicators = saved[0], saved[1]
        agents.call_llm = saved[2]
        agents.market.snapshot = saved[3]
        agents.market.compute_indicators = saved[4]

    assert len(calls) == 5, f'应该有 5 次模型调用（4+1），实际 {len(calls)}'
    assert len(r['分析师']) == 4
    names = [a['_名称'] for a in r['分析师']]
    assert set(names) == {'技术面分析师', '资金面分析师', '情绪面分析师', '风控官'}, names
    assert r['主持人']['方向'] == '偏空'
    assert r['币种'] == 'BTCUSDT'
    approx(r['当时价格'], 86000.0)
    assert r['用量']['total_tokens'] == 300 * 4 + 500, r['用量']
    # 每个分析师都带了数据快照和自己的结论
    for a in r['分析师']:
        assert a['_数据'] and a['方向'] in agents.DIRECTIONS

def t_record_and_load():
    saved = agents.JUDGMENTS_PATH
    agents.JUDGMENTS_PATH = os.path.join(os.path.dirname(saved), '_自测_判断.jsonl')
    try:
        if os.path.exists(agents.JUDGMENTS_PATH):
            os.remove(agents.JUDGMENTS_PATH)
        result = {'币种': 'BTCUSDT', '当时价格': 86000,
                  '主持人': {'方向': '偏空', '信心': 62, '综合判断': '偏空',
                            '什么情况下我错了': '站上89000'},
                  '分析师': [{'_名称': '技术面分析师', '方向': '偏多', '信心': 65}],
                  '用量': {'total_tokens': 1700}, '模型': 'm'}
        row = agents.record_judgment(result, horizon_hours=24)
        assert row['已结算'] is False and row['方向'] == '偏空'
        assert row['结算时间'] > row['记录时间']
        rows = agents.load_judgments()
        assert len(rows) == 1 and rows[0]['币种'] == 'BTCUSDT'
        assert rows[0]['分析师方向'][0]['名称'] == '技术面分析师'
    finally:
        if os.path.exists(agents.JUDGMENTS_PATH):
            os.remove(agents.JUDGMENTS_PATH)
        agents.JUDGMENTS_PATH = saved

def t_settle_due():
    import datetime as _dt
    saved = agents.JUDGMENTS_PATH
    agents.JUDGMENTS_PATH = os.path.join(os.path.dirname(saved), '_自测_判断2.jsonl')
    try:
        if os.path.exists(agents.JUDGMENTS_PATH):
            os.remove(agents.JUDGMENTS_PATH)
        past = (_dt.datetime.now() - _dt.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        future = (_dt.datetime.now() + _dt.timedelta(hours=22)).strftime('%Y-%m-%d %H:%M:%S')
        agents.save_judgments([
            {'币种': 'BTCUSDT', '方向': '偏空', '信心': 70, '当时价格': 100.0,
             '结算时间': past, '已结算': False},
            {'币种': 'ETHUSDT', '方向': '偏多', '信心': 70, '当时价格': 100.0,
             '结算时间': past, '已结算': False},
            {'币种': 'SOLUSDT', '方向': '偏多', '信心': 70, '当时价格': 100.0,
             '结算时间': future, '已结算': False},
        ])
        prices = {'BTCUSDT': 95.0, 'ETHUSDT': 103.0, 'SOLUSDT': 999.0}
        n, errors = agents.settle_due(price_getter=lambda s: prices[s])
        assert n == 2, f'应该结算 2 条，实际 {n}'
        assert not errors
        rows = {r['币种']: r for r in agents.load_judgments()}
        assert rows['BTCUSDT']['对不对'] is True, '看空且跌了，应判对'
        approx(rows['BTCUSDT']['涨跌幅%'], -5.0)
        assert rows['ETHUSDT']['对不对'] is True, '看多且涨了，应判对'
        assert rows['SOLUSDT']['已结算'] is False, '没到期不该结算'
        assert not rows['SOLUSDT'].get('到期价格')
    finally:
        if os.path.exists(agents.JUDGMENTS_PATH):
            os.remove(agents.JUDGMENTS_PATH)
        agents.JUDGMENTS_PATH = saved

def t_accuracy_stats():
    rows = [
        {'方向': '偏多', '信心': 80, '已结算': True, '对不对': True},
        {'方向': '偏多', '信心': 70, '已结算': True, '对不对': False},
        {'方向': '偏空', '信心': 75, '已结算': True, '对不对': True},
        {'方向': '偏空', '信心': 40, '已结算': True, '对不对': False},
        {'方向': '中性', '信心': 50, '已结算': True, '对不对': None},
        {'方向': '偏多', '信心': 90, '已结算': False},
    ]
    st = agents.accuracy_stats(rows)
    assert st['总判断数'] == 6
    assert st['已结算'] == 5 and st['待结算'] == 1
    assert st['可统计'] == 4, f'中性不该计入，实际 {st["可统计"]}'
    approx(st['准确率'], 50.0)
    # 信心 >= 60 的有 3 条（80✓ 70✗ 75✓）-> 66.7%
    approx(st['高信心准确率'], 200 / 3, 0.05)
    assert st['方向分布']['偏多'] == 3

def t_honest_verdict():
    assert '还没有结算' in agents.honest_verdict({'可统计': 0})
    v = agents.honest_verdict({'可统计': 5, '准确率': 60.0})
    assert '样本太小' in v, v
    v = agents.honest_verdict({'可统计': 30, '准确率': 40.0})
    assert '比抛硬币还差' in v and '不要按它的方向做单' in v, v
    v = agents.honest_verdict({'可统计': 40, '准确率': 51.0})
    assert '没区别' in v and '检查清单' in v, v
    v = agents.honest_verdict({'可统计': 60, '准确率': 58.0})
    assert '高于抛硬币' in v, v

def t_analyst_prompts_require_falsifiable():
    """提示词里必须强制要求「可证伪条件」和禁止预测点位。"""
    from agents import ANALYST_PROMPT, CHAIR_PROMPT
    for pr in (ANALYST_PROMPT, CHAIR_PROMPT):
        assert '什么情况下我错了' in pr, '必须强制要求给出可证伪条件'
        assert '不要预测具体价格点位' in pr, '必须禁止预测点位'
    assert '多数一致不等于正确' in CHAIR_PROMPT, '主持人必须被提醒不要盲从多数'
    assert '无法判断' in ANALYST_PROMPT, '必须允许回答「无法判断」'

for n, f in [('分析师看到不同数据（核心）', t_analysts_see_different_data),
             ('24小时区间位置计算', t_range_pos),
             ('JSON 提取容错', t_extract_json), ('对错判定', t_judge),
             ('完整流程打桩跑通', t_analyze_end_to_end),
             ('判断记录读写', t_record_and_load), ('到期自动结算', t_settle_due),
             ('准确率统计', t_accuracy_stats), ('诚实评价文案', t_honest_verdict),
             ('提示词强制可证伪', t_analyst_prompts_require_falsifiable)]:
    check(n, f)


# ---------------- 模型服务配置 ----------------
section('模型服务配置')


def _fake_env(d):
    """把 llm 的配置读取换成一个确定的字典，避免受真实 .env 文件影响。"""
    orig = llm.get_env

    def fake(key, default=None):
        v = d.get(key)
        return v if v else default
    llm.get_env = fake
    return lambda: setattr(llm, 'get_env', orig)


def t_llm_base_url():
    r = _fake_env({'LLM_BASE_URL': 'https://api.deepseek.com/v1/'})
    try:
        assert llm.base_url() == 'https://api.deepseek.com/v1', llm.base_url()
        assert llm.chat_url() == 'https://api.deepseek.com/v1/chat/completions'
        assert llm.models_url() == 'https://api.deepseek.com/v1/models'
    finally:
        r()
    r = _fake_env({})
    try:
        assert llm.base_url() == llm.DEFAULT_BASE_URL
    finally:
        r()
    r = _fake_env({'SILICONFLOW_BASE_URL': 'http://localhost:11434/v1'})
    try:
        assert llm.base_url() == 'http://localhost:11434/v1', '旧的变量名要兼容'
    finally:
        r()

def t_llm_key_priority():
    r = _fake_env({'LLM_API_KEY': 'new-key', 'SILICONFLOW_API_KEY': 'old-key'})
    try:
        assert llm.api_key() == 'new-key', 'LLM_API_KEY 应该优先'
    finally:
        r()
    r = _fake_env({'SILICONFLOW_API_KEY': 'old-key'})
    try:
        assert llm.api_key() == 'old-key', '应该回退到旧的 SILICONFLOW_API_KEY'
    finally:
        r()
    r = _fake_env({})
    try:
        assert llm.api_key() is None
    finally:
        r()

def t_llm_model():
    r = _fake_env({'LLM_MODEL': 'deepseek-chat', 'SILICONFLOW_MODEL': 'sf'})
    try:
        assert llm.model_name() == 'deepseek-chat'
    finally:
        r()
    r = _fake_env({'SILICONFLOW_MODEL': 'sf-model'})
    try:
        assert llm.model_name() == 'sf-model'
    finally:
        r()
    r = _fake_env({})
    try:
        assert llm.model_name() == llm.DEFAULT_MODEL
        assert llm.model_name(default='override') == 'override'
    finally:
        r()

def t_llm_describe_masks_key():
    r = _fake_env({'LLM_API_KEY': 'sk-abcdefghijklmnop',
                   'LLM_BASE_URL': 'http://localhost:11434/v1'})
    try:
        d = llm.describe()
        assert d['已配置'] is True
        assert 'abcdefghijklmnop' not in d['Key'], f'describe 泄露了完整 Key：{d["Key"]}'
        assert d['Key'].startswith('sk-a') and '…' in d['Key']
        assert d['服务地址'] == 'http://localhost:11434/v1'
    finally:
        r()
    r = _fake_env({})
    try:
        assert llm.describe()['已配置'] is False
    finally:
        r()

def t_llm_no_key_raises():
    r = _fake_env({})
    try:
        try:
            llm.chat('s', 'u')
        except RuntimeError as e:
            assert 'LLM_API_KEY' in str(e), str(e)
            assert 'Ollama' in str(e), '应该提示还有本地模型这个选项'
        else:
            raise AssertionError('没配 Key 应该报错')
    finally:
        r()

def t_llm_402_message():
    """余额不足要给可操作的提示，而不是扔一串英文。"""
    class R:
        status_code = 402
        text = '{"message":"insufficient balance"}'
    orig_post = llm.requests.post
    llm.requests.post = lambda *a, **k: R()
    r = _fake_env({'LLM_API_KEY': 'k'})
    try:
        try:
            llm.chat('s', 'u')
        except RuntimeError as e:
            msg = str(e)
            assert '余额不足' in msg and 'LLM_MODEL' in msg, msg
            assert 'insufficient balance' not in msg, '不该只扔英文原文'
        else:
            raise AssertionError('402 应该报错')
    finally:
        llm.requests.post = orig_post
        r()

def t_llm_401_and_network():
    for code, kw in [(401, 'Key'), (429, '限流')]:
        class R:
            status_code = code
            text = '{}'
        orig = llm.requests.post
        llm.requests.post = lambda *a, **k: R()
        r = _fake_env({'LLM_API_KEY': 'k'})
        try:
            try:
                llm.chat('s', 'u')
            except RuntimeError as e:
                assert kw in str(e), f'HTTP {code} 的提示里应该有「{kw}」：{e}'
            else:
                raise AssertionError(f'HTTP {code} 应该报错')
        finally:
            llm.requests.post = orig
            r()

for n, f in [('模型服务地址可切换', t_llm_base_url),
             ('API Key 读取优先级', t_llm_key_priority),
             ('模型名读取优先级', t_llm_model),
             ('配置描述不泄露 Key', t_llm_describe_masks_key),
             ('没配 Key 给明确指引', t_llm_no_key_raises),
             ('余额不足给可操作提示', t_llm_402_message),
             ('401 和限流提示', t_llm_401_and_network)]:
    check(n, f)


# ---------------- 小模型适配 ----------------
section('小模型适配')

def t_prompt_mode_auto():
    """小参数量模型应该自动切到精简提示词。"""
    for m, want in [('qwen2.5:7b', 'compact'), ('qwen3:8b', 'compact'),
                    ('llama3.2:3b', 'compact'), ('Qwen/Qwen3-14B', 'compact'),
                    ('deepseek-ai/DeepSeek-V3', 'full'),
                    ('deepseek-ai/DeepSeek-V3.2', 'full'),
                    ('gpt-4o', 'full')]:
        r = _fake_env({'LLM_MODEL': m})
        try:
            got = llm.prompt_mode()
            assert got == want, f'{m} 应该是 {want}，实际 {got}'
        finally:
            r()

def t_prompt_mode_manual():
    r = _fake_env({'LLM_MODEL': 'qwen2.5:7b', 'LLM_PROMPT_MODE': 'full'})
    try:
        assert llm.prompt_mode() == 'full', '手动指定应该覆盖自动判断'
    finally:
        r()
    r = _fake_env({'LLM_MODEL': 'deepseek-ai/DeepSeek-V3', 'LLM_PROMPT_MODE': 'compact'})
    try:
        assert llm.prompt_mode() == 'compact'
    finally:
        r()

def t_compact_prompts_have_examples():
    """小模型靠例子学格式，所以精简版必须带示例。"""
    assert '举例：' in agents.ANALYST_COMPACT, '分析师精简版必须给例子'
    assert '举例：' in agents.CHAIR_COMPACT, '主持人精简版必须给例子'
    # 精简版必须比完整版短，否则就失去意义了
    assert len(agents.ANALYST_COMPACT) < len(agents.ANALYST_PROMPT), '精简版应该更短'
    assert len(agents.CHAIR_COMPACT) < len(agents.CHAIR_PROMPT), '精简版应该更短'
    # 精简版也不能丢掉核心要求
    assert '无法判断' in agents.ANALYST_COMPACT
    assert '具体数字' in agents.ANALYST_COMPACT, '要防止小模型说空话'
    assert '不要因为多数人看多' in agents.CHAIR_COMPACT, '主持人必须防同质化'

def t_retry_on_bad_json():
    """第一次输出不是 JSON 时，应该自动重试一次，成功后标记合规。"""
    calls = []
    def fake(system, user, model=None, api_key=None, **kw):
        calls.append(user)
        if len(calls) == 1:
            return '我觉得这个市场比较复杂，可能需要再看看。', {}, 'm'
        return ('{"方向":"偏空","信心":55,"核心理由":"费率偏高",'
                '"主要风险":"x","什么情况下我错了":"价格站上89000",'
                '"我看不到什么":"y"}'), {'total_tokens': 100}, 'm'
    saved = (agents.call_llm, agents.market.snapshot, agents.market.compute_indicators)
    agents.call_llm = fake
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    try:
        r = agents.analyze_symbol('BTC', prompt_mode='compact')
    finally:
        agents.call_llm, agents.market.snapshot, agents.market.compute_indicators = saved

    assert len(calls) >= 2
    assert '不是合法 JSON' in calls[1], '重试时应该明确指出上次格式不对'
    bad = [a for a in r['分析师'] if a['_调用次数'] == 2]
    assert len(bad) == 1, '应该正好有一位分析师重试了一次'
    assert bad[0]['_格式合规'] is True, '重试成功后应标记为合规'
    assert bad[0]['方向'] == '偏空'
    assert r['格式合规']['分析师格式合规'] == '4/4', r['格式合规']
    assert r['格式合规']['格式合规率'] == 100.0

def t_compliance_reported_when_all_fail():
    """全部解析失败时，必须如实报告合规率，不能假装成功。"""
    def fake(system, user, model=None, api_key=None, **kw):
        return '这不是 JSON', {}, 'm'
    saved = (agents.call_llm, agents.market.snapshot, agents.market.compute_indicators)
    agents.call_llm = fake
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    try:
        r = agents.analyze_symbol('BTC', prompt_mode='compact')
    finally:
        agents.call_llm, agents.market.snapshot, agents.market.compute_indicators = saved

    assert r['格式合规']['格式合规率'] == 0.0, r['格式合规']
    assert r['格式合规']['分析师格式合规'] == '0/4'
    for a in r['分析师']:
        assert a['方向'] == '无法判断', '解析不出来时必须老实说无法判断'
        assert a['信心'] == 0
        assert a['_调用次数'] == 2, '应该重试过'
        assert '解析失败' in a['核心理由']

def t_mode_passed_through():
    """compact 模式下应该真的用精简版模板。"""
    used = []
    def fake(system, user, model=None, api_key=None, **kw):
        used.append(user)
        return ('{"方向":"中性","信心":30,"核心理由":"x","主要风险":"y",'
                '"什么情况下我错了":"z","我看不到什么":"w"}'), {}, 'm'
    saved = (agents.call_llm, agents.market.snapshot, agents.market.compute_indicators)
    agents.call_llm = fake
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    try:
        r = agents.analyze_symbol('BTC', prompt_mode='compact')
        assert 'compact' in r['格式合规']['提示词模式'], r['格式合规']
        assert any('举例：' in u for u in used), 'compact 模式应该用带例子的模板'
        assert not any('硬性要求' in u for u in used), 'compact 模式不该用完整版模板'
    finally:
        agents.call_llm, agents.market.snapshot, agents.market.compute_indicators = saved

def t_role_models():
    """分析师和主持人可以配不同的模型。"""
    r = _fake_env({'LLM_MODEL': 'base-model',
                   'LLM_MODEL_ANALYST': 'cheap-model',
                   'LLM_MODEL_CHAIR': 'smart-model'})
    try:
        assert llm.model_name() == 'base-model'
        assert llm.model_name('analyst') == 'cheap-model'
        assert llm.model_name('chair') == 'smart-model'
    finally:
        r()
    # 没单独配就回退到统一的 LLM_MODEL
    r = _fake_env({'LLM_MODEL': 'base-model'})
    try:
        assert llm.model_name('analyst') == 'base-model'
        assert llm.model_name('chair') == 'base-model'
    finally:
        r()

def t_role_prompt_mode():
    """提示词模式按角色各自的模型判断。"""
    r = _fake_env({'LLM_MODEL_ANALYST': 'qwen2.5:7b',
                   'LLM_MODEL_CHAIR': 'deepseek-ai/DeepSeek-V4-Pro'})
    try:
        assert llm.prompt_mode('analyst') == 'compact', '小模型分析师要精简版'
        assert llm.prompt_mode('chair') == 'full', '强模型主持人用完整版'
    finally:
        r()

def t_analyst_models_config():
    """四个分析师各自用不同模型 —— 这才是真正的多模型协商。"""
    r = _fake_env({'LLM_MODELS_ANALYSTS':
                   'm1,m2,m3,m4'})
    try:
        assert llm.analyst_models() == ['m1', 'm2', 'm3', 'm4']
        assert [llm.analyst_model(i) for i in range(4)] == ['m1', 'm2', 'm3', 'm4']
    finally:
        r()
    # 没配置 -> 用四个不同厂商的默认值
    r = _fake_env({})
    try:
        d = llm.analyst_models()
        assert len(d) == 4
        assert len(set(d)) == 4, f'默认应该是四个不同的模型：{d}'
        vendors = {m.split('/')[0] for m in d}
        assert len(vendors) >= 3, f'默认应该来自不同厂商：{vendors}'
    finally:
        r()
    # 只配了部分 -> 剩下的用默认补齐
    r = _fake_env({'LLM_MODELS_ANALYSTS': 'only-one'})
    try:
        d = llm.analyst_models()
        assert len(d) == 4 and d[0] == 'only-one', d
    finally:
        r()

def t_role_models_reach_llm_layer():
    """验证四个分析师真的用了四个不同的模型，主持人用另一个。"""
    seen = []

    def fake_chat(system, user, model=None, api_key_override=None, **kw):
        seen.append(model)
        if '主持人' in system:
            return ('{"方向":"中性","信心":30,"共识":"a","分歧":"b",'
                    '"综合判断":"c","最重要的反面证据":"d",'
                    '"什么情况下我错了":"e","给交易者的提醒":"f"}'), {}, model
        return ('{"方向":"中性","信心":30,"核心理由":"x","主要风险":"y",'
                '"什么情况下我错了":"z","我看不到什么":"w"}'), {}, model

    saved = (llm.chat, agents.market.snapshot, agents.market.compute_indicators)
    llm.chat = fake_chat
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    r = _fake_env({'LLM_MODELS_ANALYSTS': 'qa,qb,qc,qd',
                   'LLM_MODEL_CHAIR': 'the-chair'})
    try:
        res = agents.analyze_symbol('BTC')
    finally:
        r()
        llm.chat, agents.market.snapshot, agents.market.compute_indicators = saved

    assert sorted(seen[:4]) == ['qa', 'qb', 'qc', 'qd'], f'四位分析师应各用一个模型：{seen}'
    assert seen[4] == 'the-chair', f'主持人应该用自己的模型：{seen}'
    assert len(set(seen[:4])) == 4, '四个分析师的模型必须互不相同'
    # 每个分析师的结论里要带上自己用的模型（便于事后统计谁准）
    for a in res['分析师']:
        assert a.get('_模型'), a
    assert res['格式合规']['主持人模型'] == 'the-chair'

for n, f in [('小模型自动切精简提示词', t_prompt_mode_auto),
             ('分角色模型配置', t_role_models),
             ('分角色提示词模式', t_role_prompt_mode),
             ('四个分析师各配一个模型', t_analyst_models_config),
             ('多模型真的传下去了', t_role_models_reach_llm_layer),
             ('提示词模式可手动指定', t_prompt_mode_manual),
             ('精简版带示例且更短', t_compact_prompts_have_examples),
             ('格式错误自动重试', t_retry_on_bad_json),
             ('全部失败时如实报告', t_compliance_reported_when_all_fail),
             ('模式确实透传到提示词', t_mode_passed_through)]:
    check(n, f)


# ---------------- .env 安全更新 ----------------
section('.env 安全更新')

def t_update_env_preserves_others():
    """更新 .env 时绝不能弄丢其他配置行和注释。"""
    import common as cm
    tmp = os.path.join(os.path.dirname(cm.ENV_PATH), '_自测_env')
    try:
        io_ = None
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('# 我的注释\n'
                    'SILICONFLOW_API_KEY=keep-me\n'
                    'LLM_MODEL=old-model\n'
                    '\n'
                    '# 交易所\n'
                    'BINANCE_API_KEY=also-keep\n')

        cm.update_env({'LLM_MODEL': 'new-model',
                       'LLM_MODEL_ANALYST': 'cheap'}, path=tmp)
        txt = open(tmp, encoding='utf-8').read()
        lines = txt.splitlines()

        assert 'SILICONFLOW_API_KEY=keep-me' in txt, '别的 Key 被弄丢了'
        assert 'BINANCE_API_KEY=also-keep' in txt, '交易所 Key 被弄丢了'
        assert '# 我的注释' in txt and '# 交易所' in txt, '注释被弄丢了'
        assert 'LLM_MODEL=new-model' in txt, '已有键应该被更新'
        assert 'LLM_MODEL=old-model' not in txt, '旧值应该被替换掉'
        assert 'LLM_MODEL_ANALYST=cheap' in txt, '新键应该被追加'
        assert len(lines) == len([x for x in lines if x is not None])
        # 重复调用不应该产生重复键
        cm.update_env({'LLM_MODEL': 'again'}, path=tmp)
        txt2 = open(tmp, encoding='utf-8').read()
        assert txt2.count('LLM_MODEL=') == 1, '不该出现重复的键'
        assert 'LLM_MODEL=again' in txt2
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def t_update_env_creates_file():
    import common as cm
    tmp = os.path.join(os.path.dirname(cm.ENV_PATH), '_自测_env2')
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        cm.update_env({'NEW_KEY': 'v1'}, path=tmp)
        assert os.path.exists(tmp) and 'NEW_KEY=v1' in open(tmp, encoding='utf-8').read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

for n, f in [('更新 .env 不弄丢其他配置', t_update_env_preserves_others),
             ('.env 不存在时能创建', t_update_env_creates_file)]:
    check(n, f)


# ---------------- 仓位体检 ----------------
section('仓位体检')

ETH_DUST = {'币种': 'ETHUSDT', '方向': '多', '开仓价': 1767.62, '数量': 0.016,
            '杠杆': 20, '止损价': 0, '爆仓价': 1702.49}

def _find(findings, item):
    for f in findings:
        if f['项目'] == item:
            return f
    return None

def t_audit_dust():
    """零碎仓位要被抓出来。"""
    f = monitor.position_audit(ETH_DUST, 2749.33, 0.000087)
    dust = _find(f, '仓位规模')
    assert dust is not None, f
    assert dust['级别'] == '提示'
    assert '零碎仓位' in dust['说明']
    assert '占用你的注意力' in dust['说明']

def t_audit_roi_illusion():
    """收益率虚高但金额很小 —— 这条是小仓位高杠杆最典型的错觉。"""
    f = monitor.position_audit(ETH_DUST, 2749.33, 0.000087)
    roi = _find(f, '收益率虚高')
    assert roi is not None, [x['项目'] for x in f]
    assert '714' in roi['说明'] or '71' in roi['说明'], roi['说明']
    assert '决定你赚多少的是仓位大小，不是杠杆倍数' in roi['说明']
    assert '只决定你多快爆仓' in roi['说明']

def t_audit_funding_drag():
    """资金费侵蚀浮盈要被算出来。"""
    f = monitor.position_audit(ETH_DUST, 2749.33, 0.000087)
    fr = _find(f, '资金费')
    assert fr is not None, [x['项目'] for x in f]
    assert fr['级别'] == '警告'
    assert '一年' in fr['说明'] and '相当于你当前浮盈的' in fr['说明']

def t_audit_funding_beneficial():
    """负费率做多 = 在收钱，应该提示而不是警告。"""
    pos = dict(ETH_DUST, 方向='多')
    f = monitor.position_audit(pos, 2749.33, -0.0001)
    fr = _find(f, '资金费')
    assert fr is not None and fr['级别'] == '提示', fr
    assert '收' in fr['说明']

def t_audit_funding_direction():
    """正费率做空 = 收钱；不能被算成付费。"""
    pos = dict(ETH_DUST, 方向='空', 开仓价=2749.33, 爆仓价=3000)
    f = monitor.position_audit(pos, 2700, 0.000087)
    fr = _find(f, '资金费')
    assert fr is not None and fr['级别'] == '提示', fr
    assert '收' in fr['说明'], fr['说明']

def t_audit_liq_distance():
    """距爆仓很近要警告。"""
    pos = {'币种': 'BTCUSDT', '方向': '多', '开仓价': 86000, '数量': 0.01,
           '杠杆': 100, '爆仓价': 85400}
    f = monitor.position_audit(pos, 85700, None)
    liq = _find(f, '爆仓距离')
    assert liq is not None and liq['级别'] == '警告', liq
    assert '100x' in liq['说明']
    # 很远就只是提示
    pos2 = dict(pos, 爆仓价=70000)
    f2 = monitor.position_audit(pos2, 85700, None)
    assert _find(f2, '爆仓距离')['级别'] == '提示'

def t_audit_big_position_not_flagged():
    """大仓位不该被误判成零碎仓位。"""
    pos = {'币种': 'BTCUSDT', '方向': '多', '开仓价': 80000, '数量': 0.5,
           '杠杆': 5, '爆仓价': 65000}
    f = monitor.position_audit(pos, 86000, 0.0001)
    assert _find(f, '仓位规模') is None, '大仓位不该报零碎'
    assert _find(f, '收益率虚高') is None, '收益率不高就不该报'

def t_audit_bad_input():
    """数据不全会返回空列表，不能崩。"""
    assert monitor.position_audit({}, 100) == []
    assert monitor.position_audit(ETH_DUST, 0) == []
    assert monitor.position_audit({'开仓价': 100, '数量': 0}, 100) == []

for n, f in [('零碎仓位被抓出', t_audit_dust),
             ('收益率虚高错觉', t_audit_roi_illusion),
             ('资金费拖累', t_audit_funding_drag),
             ('负费率做多在收钱', t_audit_funding_beneficial),
             ('正费率做空在收钱', t_audit_funding_direction),
             ('爆仓距离判定', t_audit_liq_distance),
             ('大仓位不误报', t_audit_big_position_not_flagged),
             ('脏数据不崩', t_audit_bad_input)]:
    check(n, f)


# ---------------- 交易方案生成 ----------------
section('交易方案生成')

KL = [[i, 100, 104, 96, 100, 10] for i in range(60)]
KL[-1] = [59, 100, 102, 98, 100, 10]

def t_swing_points():
    sp = plan.swing_points(KL, lookback=10)
    assert sp['近期最高'] == 104 and sp['近期最低'] == 96, sp
    assert sp['最新收盘'] == 100

def t_stop_candidates_long():
    """做多的候选止损必须全部在现价下方。"""
    sp = {'近期最低': 96, '前低': 93, '近期最高': 104, '前高': 107}
    ind = {'MA20': 98, 'MA60': 95}
    cs = plan.stop_candidates(100, 2.0, ind, sp, 'long')
    assert cs, '应该算出候选'
    for c in cs:
        assert c['价格'] < 100, f'做多止损必须在下方：{c}'
        assert 0.3 <= c['距离百分比'] <= 15, c
        assert c['说明'], '每个候选必须带计算依据'
    names = [c['名称'] for c in cs]
    assert '近期摆动低点外' in names and '1.5倍ATR' in names, names
    # 应该按距离从近到远排序
    ds = [c['距离百分比'] for c in cs]
    assert ds == sorted(ds), ds

def t_stop_candidates_short():
    sp = {'近期最低': 96, '前低': 93, '近期最高': 104, '前高': 107}
    ind = {'MA20': 102, 'MA60': 105}
    cs = plan.stop_candidates(100, 2.0, ind, sp, 'short')
    assert cs
    for c in cs:
        assert c['价格'] > 100, f'做空止损必须在上方：{c}'

def t_stop_filters_extremes():
    """太近（<0.3%）和太远（>15%）的候选要被过滤掉。"""
    sp = {'近期最低': 99.95, '前低': 50}
    cs = plan.stop_candidates(100, 0.1, {'MA20': 99.99}, sp, 'long')
    for c in cs:
        assert 0.3 <= c['距离百分比'] <= 15, c

def t_rr_targets():
    t = plan.rr_targets(100, 95, 'long', (2.0,))
    assert t[0]['止盈价'] == 110, t
    t = plan.rr_targets(100, 105, 'short', (2.0,))
    assert t[0]['止盈价'] == 90, t

def _fake_market(snap, ind):
    saved = (plan.market.snapshot, plan.market.compute_indicators)
    plan.market.snapshot = lambda s, e: snap
    plan.market.compute_indicators = lambda k, p: ind
    return saved

def _analysis(direction='偏多', conf=65):
    return {'主持人': {'方向': direction, '信心': conf, '综合判断': 'x',
                      '什么情况下我错了': '跌破关键位'},
            '分析师': [{'_名称': '技术面分析师', '方向': direction, '信心': conf,
                       '核心理由': '理由'}]}

def t_build_plan_ok():
    """完整组装：模型只选，代码算所有数字。"""
    snap = {'标记价': 100.0, 'K线': KL, '资金费率': 0.0001, '交易所': '币安'}
    ind = {'ATR14': 2.0, 'ATR14百分比': 2.0, 'MA20': 98, 'MA60': 95}
    saved = _fake_market(snap, ind)
    saved_chat = plan.chat
    plan.chat = lambda system, user, **kw: (
        json.dumps({'止损位名称': '1.5倍ATR', '盈亏比': 2.0,
                    '选择理由': '放在 1.5 倍 ATR 外，避开正常波动',
                    '主要风险': '震荡加剧', '什么情况下我错了': '跌破 95',
                    '要不要做': '做'}, ensure_ascii=False), {}, 'm')
    try:
        r = plan.build_plan('BTC', _analysis(), equity=1000, risk_pct=1,
                            leverage=10, fee_rate=0.0)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat

    assert r['可执行'] is True, r.get('原因')
    assert r['方向'] == '做多'
    assert r['入场价'] == 100.0
    approx(r['止损价'], 97.0)          # 100 - 2.0*1.5
    approx(r['止盈价'], 106.0)          # 100 + 3*2
    # 仓位必须由 risk.calc_position 算出，不能由模型给
    # 本金 1000 × 风险 1% = 10 USDT；止损距离 3 → 数量 = 10/3
    approx(r['仓位']['建议数量'], 10 / 3.0, 1e-9)
    approx(r['仓位']['止损时实际亏损'], 10.0, 1e-6)   # 本金 1000 的 1%
    assert r['纪律检查']['结论'] in ('通过', '警告')
    assert r['止损依据']['名称'] == '1.5倍ATR'
    assert '方案官' in r

def t_build_plan_rejects_fake_stop():
    """核心安全属性：绝不使用模型编造的价格。

    原设计是「模型选了候选外的名字就直接失败」。实测发现太脆：
    模型可能因超时/截断/字段名写错而选不出来，整个方案作废、白烧前面 9 次调用。
    改成兜底，但安全属性不变 —— 兜底用的是【程序算出的候选价】，
    绝不是模型编的价，而且会明确标注这是程序给的、不是模型选的。
    """
    snap = {'标记价': 100.0, 'K线': KL}
    ind = {'ATR14': 2.0, 'MA20': 98, 'MA60': 95}
    saved = _fake_market(snap, ind)
    saved_chat = plan.chat
    plan.chat = lambda system, user, **kw: (
        json.dumps({'止损位名称': '我自己算的 91.7', '盈亏比': 2.0,
                    '选择理由': 'x', '主要风险': 'y', '什么情况下我错了': 'z',
                    '要不要做': '做'}, ensure_ascii=False), {}, 'm')
    try:
        r = plan.build_plan('BTC', _analysis(), equity=1000, risk_pct=1, leverage=10)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat

    assert r['可执行'] is True, '应该用兜底继续出方案'
    # ① 必须明确标注这不是模型的选择
    assert r.get('兜底提示'), '兜底必须标注出来'
    assert '不在候选里' in r['兜底提示'], r['兜底提示']
    assert '不是模型的选择' in r['兜底提示'], r['兜底提示']
    # ② 最关键的：止损价必须来自程序的候选，绝不是模型编的 91.7
    cand_prices = [c['价格'] for c in r['候选止损']]
    assert r['止损价'] in cand_prices, \
        f'止损价 {r["止损价"]} 不在候选里 —— 用了模型编的价格！'
    assert abs(r['止损价'] - 91.7) > 1e-6, '绝不能用模型编的 91.7'

def t_build_plan_fallback_on_garbage():
    """模型完全没返回有效字段时，也要兜底而不是失败。"""
    snap = {'标记价': 100.0, 'K线': KL}
    saved = _fake_market(snap, {'ATR14': 2.0, 'MA20': 98, 'MA60': 95})
    saved_chat = plan.chat
    plan.chat = lambda system, user, **kw: ('{"完全不对的字段": 1}', {}, 'm')
    try:
        r = plan.build_plan('BTC', _analysis(), equity=1000, risk_pct=1, leverage=10)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat
    assert r['可执行'] is True, r.get('原因')
    assert r.get('兜底提示'), '要标注兜底'
    assert r['止损价'] in [c['价格'] for c in r['候选止损']]
    # 兜底优先用 2 倍 ATR（波动率止损里最常用的）
    assert r['止损依据']['名称'] == '2倍ATR', r['止损依据']['名称']

def t_format_plan_shows_fallback():
    """兜底提示要在方案文本里显示出来，不能藏着。"""
    txt = plan.format_plan({
        '可执行': True, '标的': 'BTC', '方向': '做多',
        '入场价': 100, '止损价': 98,
        '止损依据': {'名称': '2倍ATR', '价格': 98, '距离百分比': 2.0, '说明': 'x'},
        '止盈价': 104, '止盈依据': {'盈亏比': 2.0},
        '仓位': {'建议数量': 1, '名义价值': 100, '占用保证金': 10,
                 '保证金占本金比例': 1, '止损时实际亏损': 10,
                 '止损时实际亏损比例': 1, '达到目标的盈利': 20,
                 '预估手续费': 0.1, '爆仓价': 90, '爆仓先于止损': False,
                 '保本价': 100.1},
        '纪律检查': {'结论': '通过', '明细': []},
        '方案官': {'选择理由': 'x'}, '兜底提示': '⚠️ 模型没选，已回退',
        '可执行条件': 'a', '主要风险': 'b'})
    assert '已回退' in txt, '兜底提示必须显示在方案文本里'

def t_build_plan_unclear_direction():
    """方向无法判断时，不该硬给方案。"""
    snap = {'标记价': 100.0, 'K线': KL}
    saved = _fake_market(snap, {'ATR14': 2.0, 'MA20': 98, 'MA60': 95})
    saved_chat = plan.chat
    called = []
    plan.chat = lambda *a, **k: (called.append(1), ('{}', {}, 'm'))[1]
    try:
        r = plan.build_plan('BTC', _analysis('无法判断', 20),
                            equity=1000, risk_pct=1, leverage=10)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat
    assert r['可执行'] is False
    assert '不构成开仓依据' in r['原因'], r['原因']
    assert not called, '方向不明时不该再调用模型（省一次钱）'

def t_build_plan_respects_no_trade():
    """方案官说「不做」，就要真的不做。"""
    snap = {'标记价': 100.0, 'K线': KL}
    saved = _fake_market(snap, {'ATR14': 2.0, 'MA20': 98, 'MA60': 95})
    saved_chat = plan.chat
    plan.chat = lambda system, user, **kw: (
        json.dumps({'止损位名称': '1.5倍ATR', '盈亏比': 2.0,
                    '选择理由': '数据矛盾太大', '主要风险': 'x',
                    '什么情况下我错了': 'y', '要不要做': '不做'},
                   ensure_ascii=False), {}, 'm')
    try:
        r = plan.build_plan('BTC', _analysis(), equity=1000, risk_pct=1, leverage=10)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat
    assert r['可执行'] is False
    assert '不做' in r['原因']

def t_build_plan_rr_snapped():
    """模型给的盈亏比不在候选里时，要吸附到最近的合法值，不能直接采信。"""
    snap = {'标记价': 100.0, 'K线': KL}
    saved = _fake_market(snap, {'ATR14': 2.0, 'MA20': 98, 'MA60': 95})
    saved_chat = plan.chat
    plan.chat = lambda system, user, **kw: (
        json.dumps({'止损位名称': '1.5倍ATR', '盈亏比': 2.17,
                    '选择理由': 'x', '主要风险': 'y', '什么情况下我错了': 'z',
                    '要不要做': '做'}, ensure_ascii=False), {}, 'm')
    try:
        r = plan.build_plan('BTC', _analysis(), equity=1000, risk_pct=1, leverage=10)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat
    assert r['可执行']
    assert r['止盈依据']['盈亏比'] in (1.5, 2.0, 2.5, 3.0), r['止盈依据']

def t_format_plan():
    snap = {'标记价': 100.0, 'K线': KL}
    saved = _fake_market(snap, {'ATR14': 2.0, 'ATR14百分比': 2.0, 'MA20': 98, 'MA60': 95})
    saved_chat = plan.chat
    plan.chat = lambda system, user, **kw: (
        json.dumps({'止损位名称': '1.5倍ATR', '盈亏比': 2.0, '选择理由': '避开波动',
                    '主要风险': '震荡', '什么情况下我错了': '跌破 95',
                    '要不要做': '做'}, ensure_ascii=False), {}, 'm')
    try:
        r = plan.build_plan('BTC', _analysis(), equity=1000, risk_pct=1,
                            leverage=10, fee_rate=0.0)
        txt = plan.format_plan(r)
    finally:
        plan.market.snapshot, plan.market.compute_indicators = saved
        plan.chat = saved_chat
    assert '做多' in txt and '入场价' in txt and '止损价' in txt
    assert '止盈价' in txt and '估算爆仓价' in txt
    assert '什么情况下这个方案失效' in txt

def t_format_plan_not_executable():
    txt = plan.format_plan({'可执行': False, '原因': '测试原因'})
    assert '没有生成可执行方案' in txt and '测试原因' in txt

for n, f in [('摆动高低点计算', t_swing_points),
             ('做多候选止损都在下方', t_stop_candidates_long),
             ('做空候选止损都在上方', t_stop_candidates_short),
             ('过滤极端候选', t_stop_filters_extremes),
             ('盈亏比反推止盈价', t_rr_targets),
             ('完整组装方案', t_build_plan_ok),
             ('绝不用模型编的价格（核心）', t_build_plan_rejects_fake_stop),
             ('模型返回垃圾时兜底', t_build_plan_fallback_on_garbage),
             ('兜底提示要显示出来', t_format_plan_shows_fallback),
             ('方向不明不硬给方案', t_build_plan_unclear_direction),
             ('尊重「不做」的结论', t_build_plan_respects_no_trade),
             ('盈亏比吸附到合法值', t_build_plan_rr_snapped),
             ('方案文本格式化', t_format_plan),
             ('不可执行时的提示', t_format_plan_not_executable)]:
    check(n, f)


# ---------------- 服务商切换 ----------------
section('服务商切换')

def t_provider_presets_valid():
    """预设里的地址格式要对，不然切过去就连不上。"""
    assert len(llm.PROVIDER_PRESETS) >= 8
    for name, cfg in llm.PROVIDER_PRESETS.items():
        assert 'base' in cfg and 'need_key' in cfg and '备注' in cfg, name
        b = cfg['base']
        if b:   # 「其他（自己填）」允许为空
            assert b.startswith('http'), f'{name} 的地址不像 URL：{b}'

def t_provider_of_identifies():
    """能根据 base_url 反查出当前用的是哪家。"""
    cases = [
        ('https://api.siliconflow.cn/v1', '硅基流动（默认）'),
        ('https://api.deepseek.com', 'DeepSeek 官方'),
        ('https://api.deepseek.com/', 'DeepSeek 官方'),          # 容忍结尾斜杠
        ('https://api.openai.com/v1', 'OpenAI'),
        ('http://localhost:11434/v1', '本地 Ollama（免费）'),
        ('https://dashscope.aliyuncs.com/compatible-mode/v1', '阿里通义千问'),
    ]
    for url, want in cases:
        got = llm.provider_of(url)
        assert got == want, f'{url} -> 期望 {want}，实际 {got}'

def t_provider_of_unknown():
    """没见过的地址要归到「其他」，不能乱认。"""
    assert llm.provider_of('https://my-company-internal-llm.local/v1') == '其他（自己填）'
    assert llm.provider_of('http://192.168.1.50:8000/v1') == '其他（自己填）'

def t_provider_of_follows_config():
    """不传参数时应该读当前 .env 配置。"""
    r = _fake_env({'LLM_BASE_URL': 'https://api.moonshot.cn/v1'})
    try:
        assert llm.provider_of() == '月之暗面 Kimi'
    finally:
        r()

for n, f in [('服务商预设格式正确', t_provider_presets_valid),
             ('能识别当前服务商', t_provider_of_identifies),
             ('陌生地址归到「其他」', t_provider_of_unknown),
             ('识别跟随当前配置', t_provider_of_follows_config)]:
    check(n, f)


# ---------------- 新闻事件分类 ----------------
section('新闻事件分类')

def t_news_rules_regression():
    """分类规则回归：这些标题的分类结果不能变。"""
    cases = [
        ('Federal Reserve issues FOMC statement', '高'),
        ('SEC Charges Founder of $16M Crypto Scheme', ''),
        ('SEC sues Binance over compliance failures', '高'),
        ('BlackRock Bitcoin ETF approval expected', '高'),
        ('Bitcoin ETFs flirt with $1B as inflows hit 2026 high', ''),
        ('Trump announces new tariffs on China', '高'),
        ("Here's what happened in crypto today", ''),
        ('Coldcard hackers move 52 bitcoin', ''),
    ]
    for title, want in cases:
        cats, strength = news.classify({'标题': title})
        assert strength == want, f'{title!r} -> 强度 {strength!r}，期望 {want!r}'

def t_news_noise_rate():
    """误报率不能太高，否则提醒就没意义了。"""
    normal = [
        'Bitcoin price analysis: what traders expect next',
        'Ethereum upgrade scheduled for next month',
        'Crypto exchange adds new trading pairs',
        'Weekly market recap: altcoins outperform',
        'Interview with a DeFi founder',
        'NFT sales volume rises 12% this week',
        'Here is how to stake your tokens',
        'Bitcoin mining difficulty adjusts upward',
    ]
    bad = [t for t in normal if news.classify({'标题': t})[1] == '高']
    assert len(bad) == 0, f'这些普通新闻被误判成高影响：{bad}'

def t_news_advice_covers_categories():
    """每个可分类别都要有对应的风控建议。"""
    for cat in set(news.STRONG_PATTERNS):
        assert cat in news.IMPACT_ADVICE, f'{cat} 缺少风控建议'

def t_news_parse_feed():
    xml = (
        '<?xml version="1.0"?><rss><channel>'
        '<item><title>Test Title &amp; More</title>'
        '<link>https://example.com/a</link>'
        '<pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>'
        '<description><![CDATA[<p>Some <b>desc</b></p>]]></description>'
        '</item></channel></rss>'
    )
    items = news.parse_feed(xml, '测试源', 'crypto')
    assert len(items) == 1, items
    assert items[0]['标题'] == 'Test Title & More', items[0]
    assert items[0]['链接'] == 'https://example.com/a'
    assert 'Some desc' in items[0]['摘要'], items[0]

def t_news_risk_note():
    empty = news.risk_note({'按类别': {}, '高影响条数': 0})
    assert empty['级别'] == '正常'
    some = news.risk_note({'按类别': {'货币政策': {'条数': 2, '建议': '建议减仓'}},
                           '高影响条数': 2}, has_position=True)
    assert some['级别'] == '提示' and '减仓' in some['提示']
    assert '持仓' in some['提示']
    many = news.risk_note({'按类别': {'关税贸易': {'条数': 9, '建议': 'x'}},
                           '高影响条数': 9})
    assert many['级别'] == '警告'

def t_onchain_enrich():
    """链上衍生指标计算。"""
    flows = [{'date': f'2026-01-{i+1:02d}', 'ts': 0, '流入': 200.0, '流出': 100.0,
              '净流入': 100.0, '活跃地址': 1000.0 + i, '流通量': 1000.0,
              '价格': 100.0} for i in range(10)]
    stable = [{'date': f'2026-01-{i+1:02d}', 'ts': 0, '稳定币总量': 1000.0 + i * 10}
              for i in range(10)]
    out = onchain.enrich(flows, stable)
    assert len(out) == 10
    approx(out[0]['净流入占市值万分之'], 10.0, 1e-6)
    approx(out[-1]['净流入7日均'], 100.0, 1e-6)

for n, f in [('新闻分类规则回归', t_news_rules_regression),
             ('普通新闻不误报', t_news_noise_rate),
             ('每类都有风控建议', t_news_advice_covers_categories),
             ('RSS 解析', t_news_parse_feed),
             ('风控提示分级', t_news_risk_note),
             ('链上衍生指标计算', t_onchain_enrich)]:
    check(n, f)

# ---------------- 各模型准确率统计 ----------------
section('各模型准确率统计')

def t_model_accuracy():
    """按模型分别统计对错，并对比主持人综合后是变准还是变差。"""
    rows = [
        {'已结算': True, '涨跌幅%': 2.0, '方向': '偏多',
         '分析师方向': [
             {'名称': '技术面', '模型': 'A', '方向': '偏多'},    # 对
             {'名称': '资金面', '模型': 'B', '方向': '偏空'},    # 错
             {'名称': '情绪面', '模型': 'C', '方向': '中性'},    # 不计
         ]},
        {'已结算': True, '涨跌幅%': -3.0, '方向': '偏空',
         '分析师方向': [
             {'名称': '技术面', '模型': 'A', '方向': '偏空'},    # 对
             {'名称': '资金面', '模型': 'B', '方向': '偏多'},    # 错
             {'名称': '情绪面', '模型': 'C', '方向': '偏空'},    # 对
         ]},
        {'已结算': False, '涨跌幅%': None, '方向': '偏多',
         '分析师方向': [{'名称': '技术面', '模型': 'A', '方向': '偏多'}]},
    ]
    ma = agents.model_accuracy(rows)
    assert ma['A']['样本'] == 2 and ma['A']['准确率'] == 100.0, ma
    assert ma['B']['样本'] == 2 and ma['B']['准确率'] == 0.0, ma
    assert ma['C']['样本'] == 1 and ma['C']['准确率'] == 100.0, ma
    # 主持人：两次都对
    assert ma['【主持人综合】']['准确率'] == 100.0, ma
    # 未结算的不该计入
    assert ma['A']['样本'] == 2

def t_model_accuracy_empty():
    assert agents.model_accuracy([]) == {}

def t_model_accuracy_skips_abstain():
    """弃权（中性/无法判断）不参与准确率统计。"""
    rows = [{'已结算': True, '涨跌幅%': 1.0, '方向': '中性',
             '分析师方向': [{'模型': 'X', '方向': '无法判断'}]}]
    ma = agents.model_accuracy(rows)
    assert 'X' not in ma, '全部弃权不该出现在统计里'
    assert '【主持人综合】' not in ma

for n, f in [('各模型准确率统计', t_model_accuracy),
             ('空记录不报错', t_model_accuracy_empty),
             ('弃权不计入准确率', t_model_accuracy_skips_abstain)]:
    check(n, f)


# ---------------- 多空辩论 ----------------
section('多空辩论')

def t_debate_runs_four_more_calls():
    """开启辩论后应该多出 4 次调用（两轮 × 看多看空各一次）。"""
    seen = []

    def fake_chat(system, user, model=None, api_key_override=None, **kw):
        tag = 'other'
        if '看多研究员' in system:
            tag = 'bull'
        elif '看空研究员' in system:
            tag = 'bear'
        elif '主持人' in system:
            tag = 'chair'
        seen.append(tag)
        if tag == 'bull':
            return ('{"立场":"看多","核心论证":"x","最有力的证据":"y",'
                    '"我的弱点":"z","什么情况下我认输":"w"}'), {}, model
        if tag == 'bear':
            return ('{"立场":"看空","核心论证":"x","最有力的证据":"y",'
                    '"我的弱点":"z","什么情况下我认输":"w"}'), {}, model
        if tag == 'chair':
            return ('{"方向":"中性","信心":30,"共识":"a","分歧":"b",'
                    '"综合判断":"c","最重要的反面证据":"d",'
                    '"什么情况下我错了":"e","给交易者的提醒":"f"}'), {}, model
        return ('{"方向":"中性","信心":30,"核心理由":"x","主要风险":"y",'
                '"什么情况下我错了":"z","我看不到什么":"w"}'), {}, model

    saved = (llm.chat, agents.market.snapshot, agents.market.compute_indicators)
    llm.chat = fake_chat
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    r = _fake_env({'LLM_MODELS_ANALYSTS': 'm1,m2,m3,m4',
                   'LLM_MODEL_CHAIR': 'chair-m'})
    try:
        res = agents.analyze_symbol('BTC', enable_debate=True)
    finally:
        r()
        llm.chat, agents.market.snapshot, agents.market.compute_indicators = saved

    assert seen.count('bull') == 2, f'看多研究员应该被调用两次（陈述+反驳）：{seen}'
    assert seen.count('bear') == 2, f'看空研究员应该被调用两次：{seen}'
    assert seen.count('chair') == 1
    assert res['辩论'] is not None, '应该返回辩论内容'
    for k in ['看多第一轮', '看空第一轮', '看多反驳', '看空反驳']:
        assert k in res['辩论'], res['辩论'].keys()

def t_debate_off_by_default():
    """默认不开辩论，省调用次数。"""
    seen = []

    def fake_chat(system, user, model=None, api_key_override=None, **kw):
        seen.append('bull' if '看多研究员' in system else
                    'bear' if '看空研究员' in system else 'other')
        return ('{"方向":"中性","信心":30,"共识":"a","分歧":"b",'
                '"综合判断":"c","最重要的反面证据":"d",'
                '"什么情况下我错了":"e","给交易者的提醒":"f"}'), {}, model

    saved = (llm.chat, agents.market.snapshot, agents.market.compute_indicators)
    llm.chat = fake_chat
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    try:
        res = agents.analyze_symbol('BTC')
    finally:
        llm.chat, agents.market.snapshot, agents.market.compute_indicators = saved
    assert 'bull' not in seen and 'bear' not in seen, '默认不该启动辩论'
    assert res['辩论'] is None

def t_debate_prompts_demand_weakness():
    """辩论提示词必须强制「说出自己的弱点」，否则就成了互相吹捧。"""
    from agents import DEBATE_ROUND1, DEBATE_ROUND2
    assert '我的弱点' in DEBATE_ROUND1, '第一轮必须要求自曝弱点'
    assert '说不出弱点的论证不值得信' in DEBATE_ROUND1
    assert '对方的破绽' in DEBATE_ROUND2, '第二轮必须针对对方具体论点'
    assert '如果你觉得对方说得对，就承认' in DEBATE_ROUND2, \
        '必须允许认输，否则辩论变成嘴硬'
    assert '我认输了' in DEBATE_ROUND2

for n, f in [('辩论会多调用4次', t_debate_runs_four_more_calls),
             ('默认不开辩论', t_debate_off_by_default),
             ('辩论提示词强制自曝弱点', t_debate_prompts_demand_weakness)]:
    check(n, f)

def t_debate_chair_must_check_data():
    """主持人必须被要求核对辩论中的数字 —— 这是实测发现漏洞后加的。"""
    from agents import DEBATE_CHAIR_ADDON
    assert '数据核对' in DEBATE_CHAIR_ADDON, '必须要求主持人核对数据'
    assert '不能跳过' in DEBATE_CHAIR_ADDON, '要标成强制步骤'
    assert '降低对其整段论证的权重' in DEBATE_CHAIR_ADDON, \
        '引错数据的一方要被降权，否则错误会污染结论'
    assert '指出错误的那一方是更可信的' in DEBATE_CHAIR_ADDON, \
        '要明确告诉主持人：抓错的一方更可信'

def t_chair_schema_has_data_check():
    """主持人的输出格式里要有「数据核对」字段。"""
    from agents import CHAIR_PROMPT
    assert '数据核对' in CHAIR_PROMPT, CHAIR_PROMPT[:200]

def t_debate_norm_list_fields():
    """模型把「三条理由」输出成列表时，要拍成文本，不能在界面显示 Python 列表。"""
    seen = []
    def fake_chat(system, user, model=None, api_key_override=None, **kw):
        if '看多研究员' in system:
            return ('{"立场":"看多","核心论证":["理由一","理由二","理由三"],'
                    '"最有力的证据":"x","我的弱点":"y","什么情况下我认输":"z"}'), {}, model
        if '看空研究员' in system:
            return ('{"立场":"看空","核心论证":["a","b","c"],'
                    '"最有力的证据":"x","我的弱点":"y","什么情况下我认输":"z"}'), {}, model
        return ('{"方向":"中性","信心":30,"数据核对":"无冲突","共识":"a","分歧":"b",'
                '"综合判断":"c","最重要的反面证据":"d",'
                '"什么情况下我错了":"e","给交易者的提醒":"f"}'), {}, model
    saved = (llm.chat, agents.market.snapshot, agents.market.compute_indicators)
    llm.chat = fake_chat
    agents.market.snapshot = lambda s, e: dict(SNAP_MA)
    agents.market.compute_indicators = lambda k, p: dict(IND_MA)
    try:
        res = agents.analyze_symbol('BTC', enable_debate=True)
    finally:
        llm.chat, agents.market.snapshot, agents.market.compute_indicators = saved
    v = res['辩论']['看多第一轮']['核心论证']
    assert isinstance(v, str), f'列表应该被拍成字符串，实际 {type(v)}'
    assert '1. 理由一' in v and '3. 理由三' in v, v

for n, f in [('主持人必须核对数据', t_debate_chair_must_check_data),
             ('主持人输出含数据核对字段', t_chair_schema_has_data_check),
             ('列表字段规范化为文本', t_debate_norm_list_fields)]:
    check(n, f)

# ---------------- 数据必须带单位（踩过坑） ----------------
section('数据带单位')

SNAP_U = {'标记价': 86380.0, '资金费率': 0.00002746, '多空比': 0.934,
          '多头账户占比': 0.483, '持仓量': 109549.0, '24h成交额': 1.635e10,
          '24h涨跌幅': 0.61, '24h最高': 87000.0, '24h最低': 85080.0}
IND_U = {'MA20': 82998.13, 'MA60': 79449.0, '距MA20百分比': 4.06,
         '距MA60百分比': 8.72, 'ATR14百分比': 1.3, '近期高点': 87385.1,
         '近期低点': 76000.0, '距近期高点百分比': -1.17,
         '距近期低点百分比': 13.66, '近24小时涨跌幅': 0.6}

def t_all_numbers_have_units():
    """所有喂给模型的数字都必须带单位。

    事故背景：资金费率原本传裸数字 0.002746，模型读成 0.2746%（放大100倍），
    据此判断"费率极端高位"并把方向结论整个反转。根因是没标单位。
    """
    import re
    for a in agents.ANALYSTS:
        brief = a['brief'](SNAP_U, IND_U)
        for k, v in brief.items():
            if v == '无数据':
                continue
            s = str(v)
            # 每个字段名本身要说明单位/含义
            has_unit_word = bool(re.search(
                r'单位%|百分比|占比|比（|USDT|币|相对位置|均线|费率', k))
            # 值本身也要带单位后缀
            has_unit_suffix = bool(re.search(r'%|USDT|币|%$', s))
            assert has_unit_word or has_unit_suffix, \
                f'{a["名称"]} 的字段「{k}」= {v} 看不出单位'

def t_funding_rate_has_scale_hint():
    """资金费率必须带上"常规基准"提示，否则模型不知道 0.0027% 算高还是低。"""
    brief = agents.ANALYSTS[1]['brief'](SNAP_U, IND_U)
    fr_key = [k for k in brief if '资金费率' in k]
    assert fr_key, brief.keys()
    v = str(brief[fr_key[0]])
    assert '0.0027%' in v, f'应该明确是 0.0027% 而不是 0.002746：{v}'
    assert '常规基准' in v, f'要给出参照系，否则模型无法判断高低：{v}'
    assert '正数=多头' in v or '多头付钱' in v, f'要说清正负号含义：{v}'

def t_context_has_units():
    """主持人用来核对数字的 context 也必须带单位。"""
    import inspect
    src = inspect.getsource(agents.analyze_symbol)
    i = src.index("context = json.dumps")
    seg = src[i:i + 900]
    assert 'USDT' in seg, 'context 里现价要带 USDT'
    assert '%' in seg, 'context 里百分比要有 %'
    assert '常规基准' in seg, 'context 里资金费率要给参照系'

for n, f in [('所有数字都带单位', t_all_numbers_have_units),
             ('资金费率有量级提示', t_funding_rate_has_scale_hint),
             ('主持人核对基准也带单位', t_context_has_units)]:
    check(n, f)

# ---------------- 最小下单量 & 分批建仓 ----------------
section('最小下单量与分批建仓')

def t_min_notional():
    r = plan.check_min_notional('BTCUSDT', 500, min_notional=100)
    assert r['通过'] is True, r
    assert '100' in r['说明']
    r2 = plan.check_min_notional('BTCUSDT', 30, min_notional=100)
    assert r2['通过'] is False, r2
    assert '开不出来' in r2['说明'], r2['说明']
    # 常见币种要有保守默认值
    assert plan.COMMON_MIN_NOTIONAL['BTCUSDT'] == 100.0
    assert plan.COMMON_MIN_NOTIONAL['ETHUSDT'] == 20.0

def t_split_entries_long():
    """做多：分批价位必须都在当前价下方，总数量等于原仓位。"""
    b = plan.split_entries(100.0, 90.0, 'long', 1.0, batches=3)
    assert len(b) == 4, f'3 批 + 合计 = 4 行，实际 {len(b)}'
    for x in b[:-1]:
        assert x['价位'] <= 100.0, f'做多的加仓价应在现价下方：{x}'
        assert x['数量'] > 0
    total = sum(x['数量'] for x in b[:-1])
    approx(total, 1.0, 1e-9)
    # 合计行的均价应该在现价和止损之间
    assert 90.0 < b[-1]['价位'] <= 100.0, b[-1]

def t_split_entries_short():
    b = plan.split_entries(100.0, 110.0, 'short', 2.0, batches=3)
    for x in b[:-1]:
        assert x['价位'] >= 100.0, f'做空的加仓价应在现价上方：{x}'
    approx(sum(x['数量'] for x in b[:-1]), 2.0, 1e-9)

def t_split_entries_edge():
    assert plan.split_entries(100, 100, 'long', 1) == []
    assert plan.split_entries(100, 90, 'long', 0) == []
    assert plan.split_entries(100, 90, 'long', 1, batches=1) == []

def t_split_first_batch_lighter():
    """第一批应该最轻（不要一上来就下重注）。"""
    b = plan.split_entries(100.0, 90.0, 'long', 1.0, batches=3)
    q = [x['数量'] for x in b[:-1]]
    assert q[0] < q[1] and q[0] < q[2], f'第一批应该最轻：{q}'

for n, f in [('最小下单量检查', t_min_notional),
             ('做多分批建仓', t_split_entries_long),
             ('做空分批建仓', t_split_entries_short),
             ('分批建仓边界情况', t_split_entries_edge),
             ('第一批最轻', t_split_first_batch_lighter)]:
    check(n, f)


# ---------------- 多币种扫描 ----------------
section('多币种扫描')

def t_watchlist_defaults():
    assert len(watchlist.DEFAULT_WATCHLIST) >= 8
    assert 'BTCUSDT' in watchlist.DEFAULT_WATCHLIST
    assert all(s.endswith('USDT') for s in watchlist.DEFAULT_WATCHLIST)

def t_watchlist_filter():
    """杠杆代币这类噪音合约要被过滤掉。"""
    for bad in ['BTCUPUSDT', 'ETHDOWNUSDT', 'BTCBULLUSDT']:
        assert any(bad.endswith(x) for x in watchlist.EXCLUDE_SUFFIX), bad
    # 交割合约是 `_` 开头
    for bad in ['_BTCUSDT', '_ETHUSDT']:
        assert any(bad.startswith(x) for x in watchlist.EXCLUDE_PREFIX), bad

def t_market_overview_parses():
    """批量接口解析 + 资金费率合并。"""
    tickers = [
        {'symbol': 'BTCUSDT', 'lastPrice': '86000', 'priceChangePercent': '1.5',
         'quoteVolume': '9e9', 'highPrice': '87000', 'lowPrice': '84000'},
        {'symbol': 'ETHUSDT', 'lastPrice': '2700', 'priceChangePercent': '-2.0',
         'quoteVolume': '5e9', 'highPrice': '2800', 'lowPrice': '2600'},
        {'symbol': 'BTCUPUSDT', 'lastPrice': '10', 'priceChangePercent': '5',
         'quoteVolume': '1', 'highPrice': '11', 'lowPrice': '9'},
    ]
    premium = [{'symbol': 'BTCUSDT', 'markPrice': '86100', 'lastFundingRate': '0.0012'},
               {'symbol': 'ETHUSDT', 'markPrice': '2699', 'lastFundingRate': '-0.0008'}]
    calls = []
    def fake_get(url, params=None):
        calls.append(url)
        return tickers if 'ticker' in url else premium
    orig = watchlist._get
    watchlist._get = fake_get
    try:
        d = watchlist.market_overview()
    finally:
        watchlist._get = orig
    assert 'BTCUSDT' in d and 'ETHUSDT' in d, d.keys()
    assert 'BTCUPUSDT' not in d, '杠杆代币应该被过滤'
    approx(d['BTCUSDT']['资金费率%'], 0.12, 1e-9)     # 0.0012 * 100
    assert d['BTCUSDT']['标记价'] == 86100.0
    assert len(calls) == 2, f'应该只用 2 次请求（批量接口），实际 {len(calls)}'

def t_scan_flags():
    """扫描要能标出费率极端值等关注点。"""
    fake = {
        # 标记价贴近 24h 高点（86000 接近 87000），才会触发「贴近24h高点」
        'BTCUSDT': {'币种': 'BTCUSDT', '标记价': 86800, '24h涨跌%': 9.5,
                    '24h成交额': 9e9, '24h最高': 87000, '24h最低': 84000,
                    '资金费率%': 0.12},
        'ETHUSDT': {'币种': 'ETHUSDT', '标记价': 2700, '24h涨跌%': -1.0,
                    '24h成交额': 5e9, '24h最高': 2800, '24h最低': 2600,
                    '资金费率%': 0.002},
        'XRPUSDT': {'币种': 'XRPUSDT', '标记价': 2.5, '24h涨跌%': 0.5,
                    '24h成交额': 1e7, '24h最高': 2.6, '24h最低': 2.4,
                    '资金费率%': 0.001},
    }
    orig = watchlist.market_overview
    watchlist.market_overview = lambda exchange='币安': dict(fake)
    try:
        r = watchlist.scan(['BTC', 'ETH', 'XRP'], min_volume_usd=5e7)
    finally:
        watchlist.market_overview = orig
    rows = {x['币种']: x for x in r['数据']}
    assert '费率偏高' in rows['BTCUSDT']['关注点'], rows['BTCUSDT']['关注点']
    assert '24h涨' in rows['BTCUSDT']['关注点']
    assert '贴近24h高点' in rows['BTCUSDT']['关注点']
    assert '流动性偏低' in rows['XRPUSDT']['关注点'], rows['XRPUSDT']['关注点']
    assert rows['BTCUSDT']['值得看'] is True
    assert rows['ETHUSDT']['值得看'] is False, rows['ETHUSDT']['关注点']

def t_scan_handles_symbol_forms():
    """BTC / btc / BTCUSDT 都要能扫到。"""
    fake = {'BTCUSDT': {'币种': 'BTCUSDT', '标记价': 86000, '24h涨跌%': 1.0,
                        '24h成交额': 9e9, '24h最高': 87000, '24h最低': 84000,
                        '资金费率%': 0.002}}
    orig = watchlist.market_overview
    watchlist.market_overview = lambda exchange='币安': dict(fake)
    try:
        r = watchlist.scan(['BTC', 'btc', 'BTCUSDT'], min_volume_usd=1e6)
    finally:
        watchlist.market_overview = orig
    assert len(r['数据']) == 3, '三种写法都该扫到'

for n, f in [('自选默认值', t_watchlist_defaults),
             ('过滤杠杆代币', t_watchlist_filter),
             ('批量接口解析', t_market_overview_parses),
             ('扫描标记关注点', t_scan_flags),
             ('币种写法容错', t_scan_handles_symbol_forms)]:
    check(n, f)

# ---------------- 后台任务（修「点别的就中断」） ----------------
section('后台任务')

def t_task_start_is_instant():
    """start() 必须立刻返回 —— 这是不被界面交互打断的前提。"""
    import time as _t
    def slow(progress, secs=0.6):
        progress('干活中')
        _t.sleep(secs)
        return {'ok': True}
    t0 = _t.time()
    # ⚠️ progress 是关键字参数，其他参数必须用关键字传（不能用位置）
    tid = tasks.start('自测任务', slow, secs=0.5)
    elapsed = _t.time() - t0
    assert elapsed < 0.2, f'start() 应该立刻返回，实际用了 {elapsed:.2f} 秒'
    assert tid and '自测任务' in tid
    # 等它跑完
    for _ in range(30):
        if tasks.status(tid).get('状态') in ('完成', '失败'):
            break
        _t.sleep(0.1)
    st = tasks.status(tid)
    assert st.get('状态') == '完成', st
    r = tasks.load_result(tid)
    assert r == {'ok': True}, r

def t_task_progress_reported():
    """进度要能被外部读到 —— 界面靠它显示。"""
    import time as _t
    def job(progress):
        for i in range(3):
            progress(f'步骤{i+1}')
            _t.sleep(0.25)
        return {'n': 3}
    tid = tasks.start('进度任务', job)
    seen = set()
    for _ in range(40):
        seen.add(tasks.status(tid).get('进度'))
        if tasks.status(tid).get('状态') in ('完成', '失败'):
            break
        _t.sleep(0.1)
    assert any('步骤1' in str(x) for x in seen), seen
    assert tasks.status(tid).get('状态') == '完成'

def t_task_failure_captured():
    """任务抛异常时要记录失败原因，不能让界面永远转圈。"""
    import time as _t
    def boom(progress):
        progress('要炸了')
        raise ValueError('故意失败的测试')
    tid = tasks.start('失败任务', boom)
    for _ in range(40):
        if tasks.status(tid).get('状态') in ('完成', '失败'):
            break
        _t.sleep(0.1)
    st = tasks.status(tid)
    assert st.get('状态') == '失败', st
    assert 'ValueError' in st.get('错误', ''), st
    assert tasks.load_result(tid) is None

def t_task_survives_other_work():
    """核心场景：任务在跑的时候，主线程干别的事，任务不能受影响。"""
    import time as _t
    def long_job(progress):
        total = 0
        for i in range(6):
            progress(f'第{i+1}步')
            total += i
            _t.sleep(0.2)
        return {'total': total}
    tid = tasks.start('长任务', long_job)
    # 模拟用户在界面上点别的（主线程做别的工作）
    other_work_done = 0
    for _ in range(50):
        other_work_done += 1                     # 相当于点了别的按钮
        _t.sleep(0.05)
        if tasks.status(tid).get('状态') in ('完成', '失败'):
            break
    st = tasks.status(tid)
    assert st.get('状态') == '完成', f'主线程干别的事不该影响后台任务：{st}'
    r = tasks.load_result(tid)
    assert r and r.get('total') == 15, r
    assert other_work_done > 5, '主线程应该一直在干活'
    assert tasks.running_tasks() == {} or tid not in tasks.running_tasks()

def t_task_latest_lookup():
    tid = tasks.start('查找任务', lambda progress: {'x': 1})
    import time as _t
    for _ in range(30):
        if tasks.status(tid).get('状态') == '完成':
            break
        _t.sleep(0.1)
    got_id, got = tasks.latest('查找任务')
    assert got_id == tid, (got_id, tid)
    assert got.get('状态') == '完成'

for n, f in [('start立即返回', t_task_start_is_instant),
             ('进度可被读取', t_task_progress_reported),
             ('失败原因被记录', t_task_failure_captured),
             ('干别的事不影响后台任务（核心）', t_task_survives_other_work),
             ('能查最近任务', t_task_latest_lookup)]:
    check(n, f)


# ---------------- 币种选择器 ----------------
section('币种选择器')

def t_symbol_fallback_list():
    """兜底列表必须够用，且格式统一。"""
    assert len(symbol_picker.FALLBACK) >= 20
    assert all(s.endswith('USDT') for s in symbol_picker.FALLBACK)
    assert 'BTCUSDT' in symbol_picker.FALLBACK
    assert 'ETHUSDT' in symbol_picker.FALLBACK
    # 不应该有重复
    assert len(symbol_picker.FALLBACK) == len(set(symbol_picker.FALLBACK))

def t_symbol_options_offline():
    """拉不到行情时必须回落到内置列表，不能报错。"""
    orig = symbol_picker.watchlist.market_overview
    symbol_picker.watchlist.market_overview = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('模拟断网'))
    try:
        symbol_picker.options.clear()          # 清缓存
        syms, info, src, rec = symbol_picker.options()
        assert syms == symbol_picker.FALLBACK, '断网时应该用兜底列表'
        assert info == {}
        assert '失败' in src, src
    finally:
        symbol_picker.watchlist.market_overview = orig
        symbol_picker.options.clear()

def t_symbol_options_sort_and_filter():
    """有数据时要按成交额排序，并过滤掉极小额合约。"""
    fake = {
        'AAAUSDT': {'币种': 'AAAUSDT', '24h成交额': 9e9, '24h涨跌%': 3.0},
        'BBBUSDT': {'币种': 'BBBUSDT', '24h成交额': 5e9, '24h涨跌%': -1.0},
        'TINYUSDT': {'币种': 'TINYUSDT', '24h成交额': 1e4, '24h涨跌%': 0.0},
    }
    orig = symbol_picker.watchlist.market_overview
    symbol_picker.watchlist.market_overview = lambda *a, **k: fake
    try:
        symbol_picker.options.clear()
        syms, info, src, rec = symbol_picker.options()
        assert syms[0] == 'AAAUSDT', f'应按成交额排序：{syms}'
        assert syms[1] == 'BBBUSDT'
        assert 'TINYUSDT' not in syms, '成交额过小的应被过滤'
        assert '交易所' in src and '按成交额排序' in src, src
    finally:
        symbol_picker.watchlist.market_overview = orig
        symbol_picker.options.clear()

def t_symbol_label_format():
    """标签要能看出涨跌。"""
    info = {'BTCUSDT': {'24h涨跌%': 2.5},
            'ETHUSDT': {'24h涨跌%': -1.8},
            'XXXUSDT': {}}
    assert '📈' in symbol_picker._label('BTCUSDT', info)
    assert '📉' in symbol_picker._label('ETHUSDT', info)
    assert '+2.5%' in symbol_picker._label('BTCUSDT', info)
    assert symbol_picker._label('XXXUSDT', info) == 'XXXUSDT', '没数据时只显示代码'
    # 推荐的要有 ⭐ 和短标签（完整理由放在下面的提示区，不塞进下拉框）
    lab = symbol_picker._label('BTCUSDT', info,
                               {'BTCUSDT': '8小时资金费率 +0.067%，多头拥挤（做多要付钱）'})
    assert lab.startswith('⭐'), lab
    assert '多头拥挤' in lab, lab
    assert len(lab) < 40, f'下拉标签不能太长，否则选框会被挤变形：{lab}'
    # 理由里的关键信息要被压缩成短标签
    assert symbol_picker._short_tag('24小时涨跌 -27.1%') == '今日异动'
    assert symbol_picker._short_tag('价格在24小时区间底部') == '贴区间底'
    assert symbol_picker._short_tag('这是你当前实际持有的仓位') == '我的持仓'

for n, f in [('兜底币种列表', t_symbol_fallback_list),
             ('断网时回落到内置列表', t_symbol_options_offline),
             ('按成交额排序并过滤', t_symbol_options_sort_and_filter),
             ('标签显示涨跌', t_symbol_label_format)]:
    check(n, f)


# ---------------- 快速筛选分类 ----------------
section('快速筛选分类')

FAKE_MKT = [
    {'币种': 'BTCUSDT', '标记价': 86000, '24h涨跌%': 2.0, '24h成交额': 1.3e10,
     '24h最高': 87000, '24h最低': 84000, '资金费率%': 0.004, '24h区间位置%': 67.0},
    {'币种': 'ZECUSDT', '标记价': 500, '24h涨跌%': 10.0, '24h成交额': 2.8e9,
     '24h最高': 510, '24h最低': 440, '资金费率%': 0.010, '24h区间位置%': 86.0},
    {'币种': 'ONEUSDT', '标记价': 0.01, '24h涨跌%': -26.0, '24h成交额': 3e8,
     '24h最高': 0.014, '24h最低': 0.0099, '资金费率%': -0.374, '24h区间位置%': 1.0},
    {'币种': 'QUIETUSDT', '标记价': 10, '24h涨跌%': 0.3, '24h成交额': 2e8,
     '24h最高': 10.2, '24h最低': 9.8, '资金费率%': 0.001, '24h区间位置%': 50.0},
]

def t_annotate_tags():
    """每个分类标签都要有客观依据，并写明理由。"""
    rows = watchlist.annotate([dict(x) for x in FAKE_MKT])
    d = {r['币种']: r for r in rows}
    # BTC 成交额第 1 → 流动性最好
    assert '流动性最好' in d['BTCUSDT']['分类'], d['BTCUSDT']
    assert '第 1 名' in d['BTCUSDT']['筛选理由']
    # ZEC 涨 10% → 今日异动
    assert '今日异动' in d['ZECUSDT']['分类']
    assert '+10.0%' in d['ZECUSDT']['筛选理由']
    # ONE 费率 -0.374% → 费率异常 + 区间底部
    assert '费率异常' in d['ONEUSDT']['分类']
    assert '空头拥挤' in d['ONEUSDT']['筛选理由']
    assert '贴近区间边缘' in d['ONEUSDT']['分类']
    assert '底部' in d['ONEUSDT']['筛选理由']
    # QUIET 波动/费率都正常 —— 不该有「费率异常」「今日异动」「贴近区间边缘」
    # （注：测试集只有 4 个币，所以它仍会命中「流动性最好」，生产环境是 200 个币不会）
    for tag in ['费率异常', '今日异动', '贴近区间边缘']:
        assert tag not in d['QUIETUSDT']['分类'], f'{tag} 不该出现：{d["QUIETUSDT"]}'
    assert '成交额全市场第 4 名' in d['QUIETUSDT']['筛选理由']

def t_liquidity_rank_threshold():
    """成交额排名必须真的按成交额算，且只有前 20 名进「流动性最好」。"""
    rows = []
    for i in range(25):
        rows.append({'币种': f'S{i}USDT', '标记价': 1.0,
                     '24h成交额': (100 - i) * 1e6,   # 递减
                     '24h涨跌%': 0.1, '资金费率%': 0.001,
                     '24h最高': 1.01, '24h最低': 0.99, '24h区间位置%': 50.0})
    out = watchlist.annotate(rows)
    d = {r['币种']: r for r in out}
    assert d['S0USDT']['成交额排名'] == 1
    assert d['S24USDT']['成交额排名'] == 25
    assert '流动性最好' in d['S0USDT']['分类']
    assert '流动性最好' not in d['S24USDT']['分类'], '第 25 名不该进前 20'
    # 分界点：第 20 名进，第 21 名不进
    assert '流动性最好' in d['S19USDT']['分类']
    assert '流动性最好' not in d['S20USDT']['分类']

def t_annotate_position_priority():
    """持仓的币种必须被标记出来 —— 这是最该看的。"""
    rows = watchlist.annotate([dict(x) for x in FAKE_MKT],
                              positions=[{'币种': 'QUIETUSDT'}])
    d = {r['币种']: r for r in rows}
    assert '我的持仓' in d['QUIETUSDT']['分类']
    assert '实际持有' in d['QUIETUSDT']['筛选理由']
    # 持仓分类要排在最前面
    cats = watchlist.quick_categories(rows)
    assert list(cats.keys())[0] == '我的持仓', list(cats.keys())

def t_quick_categories():
    rows = watchlist.annotate([dict(x) for x in FAKE_MKT])
    cats = watchlist.quick_categories(rows)
    assert '费率异常' in cats and '今日异动' in cats and '流动性最好' in cats
    for name, v in cats.items():
        assert v['说明'], f'{name} 缺少说明'
        assert len(v['数据']) >= 1
        for r in v['数据']:
            assert name in r['分类'], (name, r['币种'])
    # 没有持仓时不该出现「我的持仓」分类
    assert '我的持仓' not in cats

def t_annotate_handles_missing_fields():
    """字段缺失/None 时不能崩。"""
    rows = watchlist.annotate([
        {'币种': 'AUSDT', '24h成交额': None, '资金费率%': None,
         '24h涨跌%': None, '24h区间位置%': None},
        {'币种': 'BUSDT'},   # 几乎什么都没有
    ])
    assert len(rows) == 2
    for r in rows:
        assert '分类' in r and '筛选理由' in r

def t_discover_limited():
    """discover 只取成交额前 N 个，不能把 700 个全塞进来。"""
    orig = watchlist.market_overview
    watchlist.market_overview = lambda *a, **k: {x['币种']: dict(x) for x in FAKE_MKT}
    try:
        r = watchlist.discover(top_n=2)
        assert len(r['数据']) == 2, r
        assert r['全市场合约数'] == 4
        assert r['已扫描'] == 2
        # 应该是成交额最大的两个
        assert {x['币种'] for x in r['数据']} == {'BTCUSDT', 'ZECUSDT'}
    finally:
        watchlist.market_overview = orig

def t_discover_handles_error():
    orig = watchlist.market_overview
    watchlist.market_overview = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('模拟断网'))
    try:
        r = watchlist.discover()
        assert r['错误'] and '断网' in r['错误']
        assert r['数据'] == []
    finally:
        watchlist.market_overview = orig

for n, f in [('分类标签有客观依据', t_annotate_tags),
             ('持仓优先标记', t_annotate_position_priority),
             ('分类聚合正确', t_quick_categories),
             ('字段缺失不崩', t_annotate_handles_missing_fields),
             ('全市场扫描限量', t_discover_limited),
             ('扫描失败不崩', t_discover_handles_error)]:
    check(n, f)


# ---------------- 收尾 ----------------
if os.path.exists(TMP):
    os.remove(TMP)

print('\n' + '=' * 50)
print(f'通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
if FAIL:
    print('失败清单：')
    for n, e in FAIL:
        print(f'  - {n}: {e}')
    sys.exit(1)
print('全部通过 ✅')