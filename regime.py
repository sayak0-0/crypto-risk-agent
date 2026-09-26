# -*- coding: utf-8 -*-
"""规则版市场状态识别：趋势、震荡、高波动、事件驱动。"""

def classify(snap, ind, trends=None, news_text=''):
    price = float(snap.get('标记价') or 0)
    ma20 = float(ind.get('MA20') or 0)
    ma60 = float(ind.get('MA60') or 0)
    atr_pct = abs(float(ind.get('ATR14百分比') or 0))
    chg24 = abs(float(snap.get('24h涨跌幅') or 0))
    gap = ((ma20 - ma60) / price * 100) if price and ma20 and ma60 else 0.0
    news_text = str(news_text or '')
    event_words = ('美联储', '非农', 'CPI', '特朗普', '关税', 'SEC', 'ETF', '加息', '降息', '战争', '监管')
    event_hit = any(x.lower() in news_text.lower() for x in event_words)
    reasons = []
    if atr_pct >= 8 or chg24 >= 10:
        regime, conf = '高波动', min(95, 60 + atr_pct * 2)
        reasons.append(f'ATR {atr_pct:.2f}%，24h涨跌 {chg24:.2f}%')
    elif event_hit and atr_pct >= 3:
        regime, conf = '事件驱动', 70
        reasons.append('检索资料命中高影响事件，且波动率抬升')
    elif gap >= 1.2 and ma20 and price >= ma20:
        regime, conf = '趋势上涨', min(90, 55 + gap * 8)
        reasons.append(f'MA20高于MA60 {gap:.2f}%，价格在MA20上方')
    elif gap <= -1.2 and ma20 and price <= ma20:
        regime, conf = '趋势下跌', min(90, 55 + abs(gap) * 8)
        reasons.append(f'MA20低于MA60 {abs(gap):.2f}%，价格在MA20下方')
    else:
        regime, conf = '震荡', max(45, 75 - abs(gap) * 10)
        reasons.append(f'均线差距 {gap:.2f}%，方向性不强')
    return {'状态': regime, '信心': round(conf, 1), '依据': reasons,
            'ATR百分比': round(atr_pct, 3), 'MA20_MA60差': round(gap, 3)}


def weight_hint(regime):
    return {
        '趋势上涨': {'技术面分析师': 1.3, '资金面分析师': 1.15, '情绪面分析师': .9, '风控官': 1},
        '趋势下跌': {'技术面分析师': 1.3, '资金面分析师': 1.2, '情绪面分析师': .9, '风控官': 1.1},
        '震荡': {'技术面分析师': .9, '资金面分析师': .9, '情绪面分析师': 1.2, '风控官': 1.2},
        '高波动': {'技术面分析师': .8, '资金面分析师': 1, '情绪面分析师': .9, '风控官': 1.6},
        '事件驱动': {'技术面分析师': .7, '资金面分析师': .9, '情绪面分析师': 1, '风控官': 1.7},
    }.get(regime, {})