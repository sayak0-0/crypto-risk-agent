# -*- coding: utf-8 -*-
"""合约风控计算：仓位、杠杆、爆仓价、盈亏比、开仓前纪律检查。

全部是纯函数，方便自测。
约定：价格用 USDT，数量用币，名义价值 = 数量 x 价格。

重要提醒：爆仓价是「孤仓 + 简化模型」的估算，只考虑合约自身保证金，
没有考虑全仓模式、未实现盈亏、资金费率累积和交易所分层维持保证金。
实际爆仓价请以交易所 App 显示的为准，本模块只用来提前排雷。
"""


def stop_distance(entry, stop):
    """止损距离（绝对值）。"""
    return abs(float(entry) - float(stop))


def liquidation_price(entry, leverage, direction='long', mmr=0.005):
    """估算爆仓价（孤仓，简化模型）。

    做多：entry * (1 - 1/L) / (1 - mmr)
    做空：entry * (1 + 1/L) / (1 + mmr)
    """
    entry = float(entry)
    leverage = float(leverage)
    mmr = float(mmr)
    if entry <= 0:
        raise ValueError('开仓价必须大于 0')
    if leverage < 1:
        raise ValueError('杠杆必须 >= 1')
    if not 0 <= mmr < 1:
        raise ValueError('维持保证金率应在 0 到 1 之间')
    if direction == 'long':
        return entry * (1 - 1 / leverage) / (1 - mmr)
    elif direction == 'short':
        return entry * (1 + 1 / leverage) / (1 + mmr)
    raise ValueError("方向只能是 'long' 或 'short'")


def breakeven_price(entry, fee_rate=0.0005, direction='long'):
    """算上开平仓手续费后，不亏不赚的价格。"""
    entry = float(entry)
    fee_rate = float(fee_rate)
    if direction == 'long':
        return entry * (1 + fee_rate) / (1 - fee_rate)
    elif direction == 'short':
        return entry * (1 - fee_rate) / (1 + fee_rate)
    raise ValueError("方向只能是 'long' 或 'short'")


def rr_ratio(entry, stop, target):
    """盈亏比 = 盈利空间 / 亏损空间（不含手续费）。"""
    loss = stop_distance(entry, stop)
    win = abs(float(target) - float(entry))
    if loss <= 0:
        raise ValueError('止损价不能等于开仓价')
    return win / loss


def calc_position(equity, risk_pct, entry, stop, leverage,
                  direction='long', fee_rate=0.0005, mmr=0.005,
                  target=None):
    """按「单笔固定风险比例」算仓位。

    思路：先定死这笔最多亏多少 USDT，再倒推能开多少币。
    这样无论止损设得多远，亏损金额都是可控的。
    """
    equity = float(equity)
    risk_pct = float(risk_pct)
    entry = float(entry)
    stop = float(stop)
    leverage = float(leverage)
    fee_rate = float(fee_rate)

    if equity <= 0:
        raise ValueError('本金必须大于 0')
    if risk_pct <= 0 or risk_pct >= 100:
        raise ValueError('单笔风险百分比应在 0 到 100 之间')
    if entry <= 0 or stop <= 0:
        raise ValueError('价格必须大于 0')
    if leverage < 1:
        raise ValueError('杠杆必须 >= 1')

    dist = stop_distance(entry, stop)
    if dist <= 0:
        raise ValueError('止损价不能等于开仓价')

    if direction == 'long' and stop >= entry:
        raise ValueError('做多的止损价必须低于开仓价')
    if direction == 'short' and stop <= entry:
        raise ValueError('做空的止损价必须高于开仓价')
    if direction not in ('long', 'short'):
        raise ValueError("方向只能是 'long' 或 'short'")

    risk_amount = equity * risk_pct / 100.0

    # 让「价格亏损 + 开平仓手续费」正好等于允许亏损额
    # qty * dist + qty * fee_rate * (entry + stop) = risk_amount
    unit_cost = dist + fee_rate * (entry + stop)
    qty = risk_amount / unit_cost

    notional = qty * entry
    margin = notional / leverage
    fee_est = qty * fee_rate * (entry + stop)
    price_loss = qty * dist
    loss_at_stop = price_loss + fee_est

    liq = liquidation_price(entry, leverage, direction, mmr)
    if direction == 'long':
        liq_before_stop = liq >= stop      # 爆仓价在止损之上 = 先爆仓，危险
    else:
        liq_before_stop = liq <= stop

    out = {
        '方向': direction,
        '开仓价': entry,
        '止损价': stop,
        '止损距离': dist,
        '止损距离百分比': dist / entry * 100,
        '允许亏损额': risk_amount,
        '建议数量': qty,
        '名义价值': notional,
        '占用保证金': margin,
        '保证金占本金比例': margin / equity * 100,
        '预估手续费': fee_est,
        '止损时实际亏损': loss_at_stop,
        '止损时实际亏损比例': loss_at_stop / equity * 100,
        '爆仓价': liq,
        '爆仓价距开仓百分比': abs(liq - entry) / entry * 100,
        '爆仓先于止损': liq_before_stop,
        '保本价': breakeven_price(entry, fee_rate, direction),
        '杠杆': leverage,
    }
    if target:
        out['目标价'] = float(target)
        out['盈亏比'] = rr_ratio(entry, stop, target)
        out['达到目标的盈利'] = qty * abs(float(target) - entry)
    return out


def pre_trade_check(equity, risk_pct, entry, stop, leverage, direction='long',
                    target=None, fee_rate=0.0005, mmr=0.005,
                    min_rr=1.5, today_trades=0, max_daily_trades=3,
                    consecutive_losses=0, max_consecutive_losses=2,
                    minutes_since_last_loss=None, extra_notes=''):
    """开仓前纪律检查。返回 (结论, 检查明细列表)。

    结论取值：'通过' / '警告' / '拒绝'
    这是给「想下单的手」踩刹车的，不是给你壮胆的。
    """
    items = []

    def add(level, name, msg):
        items.append({'级别': level, '项目': name, '说明': msg})

    # 1. 止损必须存在且在正确一侧
    if not stop or float(stop) <= 0:
        add('拒绝', '止损', '没有止损价。不允许开仓 —— 这是在赌，不是在交易。')
        return '拒绝', items
    entry = float(entry)
    stop = float(stop)
    if stop == entry:
        add('拒绝', '止损', '止损价等于开仓价，等于没有止损。')
        return '拒绝', items

    if direction == 'long' and stop > entry:
        add('拒绝', '止损方向', '做多的止损价高于开仓价，方向搞反了。')
        return '拒绝', items
    if direction == 'short' and stop < entry:
        add('拒绝', '止损方向', '做空的止损价低于开仓价，方向搞反了。')
        return '拒绝', items

    try:
        pos = calc_position(equity, risk_pct, entry, stop, leverage,
                            direction, fee_rate, mmr, target)
    except ValueError as e:
        add('拒绝', '参数', str(e))
        return '拒绝', items

    add('通过', '止损', f'止损距离 {pos["止损距离百分比"]:.2f}%，'
                        f'止损时预计亏 {pos["止损时实际亏损"]:.2f} USDT'
                        f'（本金的 {pos["止损时实际亏损比例"]:.2f}%）。')

    # 2. 单笔风险上限
    _actual = pos['止损时实际亏损比例']
    if _actual > 2.0 + 1e-9:
        add('拒绝', '单笔风险', f'这笔止损要亏本金的 {pos["止损时实际亏损比例"]:.2f}%，'
                                '超过 2% 的红线。要么减小仓位，要么把止损拉近。')
    elif _actual > 1.0 + 1e-9:
        add('警告', '单笔风险', f'止损亏损占本金 {pos["止损时实际亏损比例"]:.2f}%，'
                                '偏激进，建议控制在 1% 以内。')
    else:
        add('通过', '单笔风险', f'止损亏损占本金 {pos["止损时实际亏损比例"]:.2f}%，风险可控。')

    # 3. 爆仓价是否比止损更近
    if pos['爆仓先于止损']:
        add('拒绝', '爆仓风险', f'按 {leverage:g}x 杠杆，估算爆仓价 {pos["爆仓价"]:.4f} 比止损价还近。'
                                '这意味着你可能先被强平，止损单根本来不及成交。请降杠杆或改止损。')
    else:
        add('通过', '爆仓风险', f'估算爆仓价 {pos["爆仓价"]:.4f}，'
                                f'在止损价之外（距开仓 {pos["爆仓价距开仓百分比"]:.2f}%），'
                                '正常情况下止损会先触发。')

    # 4. 保证金是否够
    if pos['保证金占本金比例'] > 100:
        add('拒绝', '保证金', f'需要保证金 {pos["占用保证金"]:.2f} USDT，超过本金。'
                              '杠杆不够或本金不足。')
    elif pos['保证金占本金比例'] > 50:
        add('警告', '保证金', f'占用本金 {pos["保证金占本金比例"]:.2f}%，'
                              '仓位偏重，浮亏时容易心态崩。建议不超过 30%。')
    else:
        add('通过', '保证金', f'占用本金 {pos["保证金占本金比例"]:.2f}%。')

    # 5. 盈亏比
    if target:
        rr = pos['盈亏比']
        if rr < 1.0:
            add('拒绝', '盈亏比', f'盈亏比只有 {rr:.2f}：赚的空间比亏的还小。这种交易长期必亏。')
        elif rr < min_rr:
            add('警告', '盈亏比', f'盈亏比 {rr:.2f}，低于你设的 {min_rr:.2f}。'
                                  f'意味着你需要胜率超过 {100 / (1 + rr):.0f}% 才能不亏。')
        else:
            add('通过', '盈亏比', f'盈亏比 {rr:.2f}，合格。')
    else:
        add('提示', '盈亏比', '没有填目标价，算不出盈亏比。建议开仓前就想好准备在哪里止盈。')

    # 6. 止损太近容易被扫
    if pos['止损距离百分比'] < 0.3:
        add('警告', '止损距离', f'止损只有 {pos["止损距离百分比"]:.2f}%，'
                                '币圈正常波动就能扫掉，容易被「扫损后反向」。')

    # 7. 杠杆本身
    if leverage >= 50:
        add('警告', '杠杆', f'{leverage:g}x 杠杆下，价格反向 {100/leverage:.2f}% 就爆仓。'
                            '高杠杆不会让你多赚，只会让你先出场 —— 仓位大小已经由风险决定。')

    # 8. 交易频率
    if today_trades >= max_daily_trades:
        add('拒绝', '交易频率', f'今天已经交易 {today_trades} 笔，达到上限 {max_daily_trades} 笔。'
                                '停下来，多数人的亏损来自手痒而不是机会少。')
    elif today_trades >= max_daily_trades - 1:
        add('警告', '交易频率', f'今天已经交易 {today_trades} 笔，接近上限。')
    else:
        add('通过', '交易频率', f'今天已交易 {today_trades} 笔，还有额度。')

    # 9. 连亏停手
    if consecutive_losses >= max_consecutive_losses:
        add('拒绝', '连亏保护', f'已经连亏 {consecutive_losses} 笔，触发了你自己设的停手线。'
                                '现在开仓大概率是在报复市场。强制休息。')
    elif consecutive_losses > 0:
        add('警告', '连亏保护', f'当前连亏 {consecutive_losses} 笔，注意是不是在情绪化交易。')

    # 10. 刚亏完马上又开
    if minutes_since_last_loss is not None and minutes_since_last_loss < 30:
        add('警告', '情绪', f'上一笔亏损才过了 {minutes_since_last_loss:.0f} 分钟。'
                            '先离开屏幕 10 分钟再决定。')

    if extra_notes.strip():
        add('记录', '入场理由', extra_notes.strip())

    levels = {i['级别'] for i in items}
    if '拒绝' in levels:
        verdict = '拒绝'
    elif '警告' in levels:
        verdict = '警告'
    else:
        verdict = '通过'
    return verdict, items