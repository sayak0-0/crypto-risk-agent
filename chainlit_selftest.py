# -*- coding: utf-8 -*-
"""Chainlit 原型自测：先验证纯展示逻辑，不启动浏览器。"""
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import chainlit_app as app


def test_actions():
    labels = [a.label for a in app._actions()]
    for want in ('生成方案', '扫描市场', '查行情', '新闻风险', 'BTC', 'DOGE'):
        assert want in labels, (want, labels)
    assert len(labels) == 16


def test_pick_intent_and_render():
    intent, symbol = app.chat.classify('适合开仓的币种')
    assert intent == 'pick', (intent, symbol)
    text = app._pick_md({
        '说明': '只是观察名单', '筛选条件': '流动性好、波动不过大',
        '候选': [{'币种': 'BTCUSDT', '标记价': 100000.0,
                  '24h涨跌%': -2.0, '资金费率%': 0.005,
                  '成交额排名': 1, '筛选理由': '成交额第一'}],
    })
    assert 'BTCUSDT' in text and '成交额第 1 名' in text


def test_self_info():
    assert app.is_self_question('和你对话用的什么模型')
    assert app.is_self_question('你的 RAG 里有什么')
    assert not app.is_self_question('BTC 现价多少')
    text = app._self_info_md()
    assert app.llm.model_name('analyst') in text
    assert 'RAG' in text and '公开新闻' in text
    assert '行情' in text


def test_account_render():
    text = app._account_md({
        '账户': {'钱包余额': 1000.0, '保证金余额': 1010.0, '可用余额': 800.0},
        '持仓': [{'币种': 'BTCUSDT', '方向': '多', '杠杆': 10,
                  '开仓价': 80000.0, '标记价': 81000.0,
                  '未实现盈亏': 10.0, '爆仓价': 72000.0}],
        '挂单': [{'币种': 'BTCUSDT', '类型': 'STOP_MARKET',
                  '买卖': 'SELL', '触发价': 78000.0, '数量': 0.1,
                  '已成交': 0.0, '全平仓单': True}],
    })
    assert '币安账户' in text and '1,010.00' in text
    assert '止损市价' in text and '78,000.000000' in text


def test_context_reference():
    cands = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','NEARUSDT']
    text, sym = app._resolve_reference_values('第六个币 给我个开仓方案', cands, 'BTCUSDT')
    assert 'NEARUSDT' in text and sym == 'NEARUSDT'
    text, sym = app._resolve_reference_values('我说的是NEARUSDT 给这个的方案', cands, 'BTCUSDT')
    assert sym == 'NEARUSDT' and 'NEARUSDT' in text


def test_quote():
    text = app._quote_md({
        '币种': 'BTCUSDT',
        '快照': {'标记价': 100000.0, '24h涨跌幅': 2.5,
                 '资金费率': 0.0001, '多空比': 1.2,
                 '获取时间': '2026-09-24 12:00:00'},
        '指标': {'ATR14百分比': 1.8, '距MA20百分比': -0.5},
    })
    assert '100,000.0000' in text
    assert '+2.50%' in text
    assert '1.80%' in text


def test_single_plan():
    plan = {
        '可执行': True, '标的': 'BTCUSDT', '方向': '做多',
        '入场价': 100.0, '止损价': 95.0,
        '止损依据': {'距离百分比': 5.0}, '止盈价': 110.0,
        '仓位': {'建议数量': 2.0, '名义价值': 200.0,
                 '止损时实际亏损': 10.0, '止损时实际亏损比例': 1.0,
                 '爆仓价': 50.0},
        '纪律检查': {'结论': '通过'},
    }
    text = app._plan_md(plan)
    assert 'BTCUSDT 做多' in text
    assert '止损价' in text and '95.0000' in text
    assert '通过' in text


def test_both_plan():
    plan = {
        '标的': 'ETHUSDT', '双向': True,
        'AI判断': {'方向': '无法判断', '信心': 20, '说明': '信号冲突'},
        '做多': {
            '入场价': 100.0, '止损价': 95.0, '止盈价': 110.0,
            '止损依据': {'距离百分比': 5.0},
            '仓位': {'建议数量': 1, '名义价值': 100,
                     '止损时实际亏损': 5, '止损时实际亏损比例': 1,
                     '爆仓价': 50},
            '纪律检查': {'结论': '通过'},
        },
        '做空': {
            '入场价': 100.0, '止损价': 105.0, '止盈价': 90.0,
            '止损依据': {'距离百分比': 5.0},
            '仓位': {'建议数量': 1, '名义价值': 100,
                     '止损时实际亏损': 5, '止损时实际亏损比例': 1,
                     '爆仓价': 150},
            '纪律检查': {'结论': '通过'},
        },
    }
    text = app._plan_md(plan)
    assert '双向方案' in text
    assert '## 做多' in text and '## 做空' in text
    assert '95.0000' in text and '105.0000' in text


def test_result_dispatch():
    quote = {'类型': '行情', '币种': 'BTCUSDT',
             '快照': {'标记价': 100.0}, '指标': {}}
    assert 'BTCUSDT' in app._result_md('quote', quote)
    chat = {'类型': '对话', '内容': '只做事实说明'}
    assert app._result_md('chat', chat) == '只做事实说明'


if __name__ == '__main__':
    tests = [test_actions, test_pick_intent_and_render, test_context_reference, test_self_info, test_account_render, test_quote, test_single_plan,
             test_both_plan, test_result_dispatch]
    for fn in tests:
        fn()
        print(f'[通过] {fn.__name__}')
    print(f'\nChainlit 原型自测通过 {len(tests)} 项')