# -*- coding: utf-8 -*-
"""复盘：先用规则把你亏钱的规律挖出来，需要时可以再让大模型总结。

规则部分不需要联网、不需要花钱，是最该先看的。
大模型部分走硅基流动（SiliconFlow）接口，用 .env 里的 SILICONFLOW_API_KEY。
"""
import json
import numbers

import pandas as pd

from common import get_env
from journal import closed_trades
from metrics import summary, by_group, hourly_by

import llm

DEFAULT_MODEL = llm.DEFAULT_MODEL


# ---------------- 规则复盘 ----------------

def _f(x, d=0.0):
    try:
        v = float(x)
        return d if pd.isna(v) else v
    except (TypeError, ValueError):
        return d


def revenge_trades(df, minutes=30):
    """找出「刚亏完没多久又开仓」的交易，这是情绪化交易的典型特征。"""
    closed = closed_trades(df)
    if len(closed) < 2:
        return pd.DataFrame(), 0.0
    hits = []
    prev = None
    for _, row in closed.iterrows():
        if prev is not None:
            t0, t1 = prev.get('平仓时间'), row.get('开仓时间')
            prev_pnl = _f(prev.get('净盈亏'), 0)
            if pd.notna(t0) and pd.notna(t1) and prev_pnl < 0:
                gap = (t1 - t0).total_seconds() / 60
                if 0 <= gap <= minutes:
                    hits.append(row)
        prev = row
    frame = pd.DataFrame(hits) if hits else pd.DataFrame()
    ratio = len(hits) / len(closed) * 100 if len(closed) else 0.0
    return frame, ratio


def rule_review(df, start_equity=None):
    """规则复盘：返回 (结论列表, 关键数字字典)。"""
    closed = closed_trades(df)
    if len(closed) == 0:
        return ['还没有已平仓的交易记录，先记几笔再来复盘。'], {}

    s = summary(closed, start_equity)
    findings = []
    keys = {}

    n = s['交易笔数']
    total = s['总净盈亏']
    fee = _f(s.get('总手续费'))
    notional = _f(s.get('总名义成交额'))
    gross_win = _f(closed.loc[closed['净盈亏'] > 0, '净盈亏'].sum())
    losses = closed[closed['净盈亏'] < 0]

    keys['总净盈亏'] = total
    keys['手续费'] = fee
    keys['盈亏比'] = s.get('盈亏比')
    keys['胜率'] = s.get('胜率')
    keys['平均R'] = s.get('平均R')

    # 1. 总账
    if total < 0:
        findings.append(f'【总账】{n} 笔交易，合计净亏 {abs(total):,.2f} USDT。'
                        '先别急着换策略，下面几条才是钱漏掉的地方。')
    else:
        findings.append(f'【总账】{n} 笔交易，合计净赚 {total:,.2f} USDT。')

    # 2. 手续费
    if fee > 0:
        fee_ratio = fee / abs(total) * 100 if total else None
        keys['手续费占总盈亏比'] = fee_ratio
        if gross_win > 0 and fee / gross_win > 0.3:
            findings.append(f'【手续费】累计手续费 {fee:,.2f} USDT，'
                            f'吃掉了你所有盈利单利润的 {fee / gross_win * 100:.0f}%。'
                            '这说明你交易太频繁了 —— 每一次进出都在给交易所交租。'
                            '减少交易笔数通常是提盈亏最省力的办法。')
        elif notional > 0:
            eff = fee / notional * 100
            msg = (f'【手续费】累计 {fee:,.2f} USDT，'
                   f'相当于名义成交额的 {eff:.3f}%。')
            if start_equity:
                msg += f'你的总名义成交额 {notional:,.0f} USDT，' \
                       f'是本金的 {notional / float(start_equity):.1f} 倍。'
            findings.append(msg)

    # 3. 胜率和盈亏比的组合
    wr = _f(s.get('胜率'))
    rr = s.get('盈亏比')
    if rr is not None:
        rr = _f(rr, 0)
        need_wr = 100 / (1 + rr) if rr > 0 else 100
        keys['不亏所需胜率'] = need_wr
        if wr < need_wr:
            findings.append(f'【数学硬伤】胜率 {wr:.0f}%，盈亏比 {rr:.2f}。'
                            f'这个组合要胜率超过 {need_wr:.0f}% 才不亏，'
                            f'你现在低了 {need_wr - wr:.0f} 个百分点。'
                            '两条路选一条：要么等更好的位置进场把盈亏比提上去，'
                            '要么放弃胜率低的那类交易。')
        else:
            findings.append(f'【数学】胜率 {wr:.0f}% + 盈亏比 {rr:.2f}，'
                            f'这套组合的盈亏平衡胜率是 {need_wr:.0f}%，你目前是达标的。')

    # 4. 平均 R
    avg_r = s.get('平均R')
    if avg_r is not None:
        if avg_r < 0:
            findings.append(f'【每笔风险】平均每笔亏 {abs(avg_r):.2f} 个 R（1 个 R = 你止损时亏的钱）。'
                            '这个数是负数，说明你在用「大亏小赚」的方式交易，'
                            '这是账户缩水最快的一种结构。')
        elif avg_r > 0.2:
            findings.append(f'【每笔风险】平均每笔赚 {avg_r:.2f} 个 R，结构是健康的。')

    # 5. 情绪归因
    emo = by_group(closed, '情绪')
    if len(emo):
        worst = emo.iloc[0]
        if _f(worst['净盈亏']) < 0 and worst['笔数'] >= 2:
            keys['最亏情绪'] = worst['情绪']
            findings.append(f'【情绪】「{worst["情绪"]}」状态下交易 {worst["笔数"]:.0f} 笔，'
                            f'合计亏 {abs(_f(worst["净盈亏"])):,.2f} USDT，'
                            f'是你最大的亏损来源档。下次出现这种状态，直接停手。')

    # 6. 币种归因
    sym = by_group(closed, '币种')
    if len(sym) and len(sym) > 1:
        worst = sym.iloc[0]
        if _f(worst['净盈亏']) < 0 and worst['笔数'] >= 2:
            findings.append(f'【币种】你在 {worst["币种"]} 上亏得最多'
                            f'（{worst["笔数"]:.0f} 笔，{_f(worst["净盈亏"]):,.2f} USDT）。'
                            '如果这个币的波动你驾驭不了，先把它从自选里删掉。')

    # 7. 报复性交易
    rev, rev_ratio = revenge_trades(closed)
    if rev_ratio > 0:
        rev_pnl = _f(rev['净盈亏'].sum()) if len(rev) else 0
        keys['亏损后30分钟内再开仓占比'] = rev_ratio
        if rev_ratio >= 20:
            findings.append(f'【情绪化】有 {rev_ratio:.0f}% 的交易发生在上一笔亏损后 30 分钟内，'
                            f'这些单子合计 {rev_pnl:,.2f} USDT。'
                            '这是典型的报复性交易。规则很简单：亏完就关软件，至少 30 分钟。')

    # 8. 最大单笔亏损
    worst_one = _f(s.get('最大单笔亏损'))
    avg_loss = _f(s.get('平均亏损'))
    if avg_loss > 0 and abs(worst_one) > avg_loss * 3:
        findings.append(f'【单笔失控】最大一笔亏 {abs(worst_one):,.2f} USDT，'
                        f'是平均亏损的 {abs(worst_one) / avg_loss:.1f} 倍。'
                        '说明有些单子你根本没按计划止损（或者压根没设）。'
                        '这一条通常是账户回撤的主因。')

    # 9. 时段归因
    hb = hourly_by(closed)
    if len(hb) >= 3:
        bad = hb.iloc[0]
        if _f(bad['净盈亏']) < 0 and bad['笔数'] >= 2:
            findings.append(f'【时段】{int(bad["小时"]):02d}:00 前后开的仓最亏'
                            f'（{bad["笔数"]:.0f} 笔，{_f(bad["净盈亏"]):,.2f} USDT）。'
                            '如果是半夜盯盘，睡眠不足本身就是亏损因子。')

    # 10. 下一步
    todo = []
    if s['最大连亏'] >= 3:
        todo.append(f'连亏最多到过 {s["最大连亏"]} 笔，把「连亏 2 笔强制停手一天」写死成规则。')
    if fee > abs(total) and total < 0:
        todo.append('本周只允许开 5 单，先把手续费降下来。')
    if avg_loss > 0 and abs(worst_one) > avg_loss * 3:
        todo.append('每单必须先在交易所挂上止损单再进场，止损单没挂上就不算开仓。')
    if rev_ratio >= 20:
        todo.append('亏损后设 30 分钟冷却期，手机放远一点。')
    if rr is not None and _f(rr) < 1.5:
        todo.append('把盈亏比从 1.5 作为硬门槛，达不到就不做这一单。')
    if todo:
        findings.append('【接下来做什么】\n- ' + '\n- '.join(todo))

    return findings, keys


# ---------------- 大模型复盘 ----------------

def list_models(api_key=None):
    """列出当前模型服务可用的模型，用来挑一个写进 .env。"""
    return llm.list_models(api_key)


def build_prompt(df, question='', start_equity=None, recent=25):
    """构造给模型的提示词。只喂数据，不给判断。"""
    s = summary(df, start_equity)
    if not s or s.get('交易笔数', 0) == 0:
        return None

    def clean(obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                cv = clean(v)
                if cv is None and isinstance(v, numbers.Real) and not isinstance(v, bool) \
                        and pd.isna(v):
                    continue
                out[k] = cv
            return out
        if isinstance(obj, (list, tuple)):
            return [clean(x) for x in obj]
        if isinstance(obj, numbers.Real) and not isinstance(obj, bool):
            if pd.isna(obj):
                return None
            return round(float(obj), 4)
        return obj

    closed = closed_trades(df)
    tail = closed.tail(recent)
    cols = ['开仓时间', '平仓时间', '币种', '方向', '杠杆', '开仓价', '平仓价',
            '止损价', '净盈亏', 'R倍数', '情绪', '入场理由', '复盘笔记']
    cols = [c for c in cols if c in tail.columns]
    tail = tail[cols].copy()
    for c in cols:
        if pd.api.types.is_datetime64_any_dtype(tail[c]):
            tail[c] = tail[c].dt.strftime('%Y-%m-%d %H:%M')

    payload = {
        '绩效指标': clean(s),
        '按情绪分组': clean(by_group(closed, '情绪').to_dict('records')),
        '按币种分组': clean(by_group(closed, '币种').to_dict('records')),
        '按方向分组': clean(by_group(closed, '方向').to_dict('records')),
        '最近交易': tail.to_dict('records'),
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)

    prompt = (
        '你是一个严格的加密货币合约交易教练。下面是一位散户的真实交易数据和记录。\n\n'
        + text +
        '\n\n请完成以下事情：\n'
        '1. 指出他亏损（或盈利少）的三个最主要的行为原因，每条都要引用上面的具体数字。\n'
        '2. 指出他做得对的地方（如果有），也要用数字说明。\n'
        '3. 给出三条最具体、可执行的改进规则，要能写进交易纪律里，'
        '不要写「控制风险」「保持耐心」这种废话。\n'
        '4. 如果数据里能看出明显的情绪化交易模式，直接点出来。\n\n'
        '硬性要求：不要预测价格，不要推荐任何币种或买卖方向，'
        '不要承诺收益。你只分析他的行为模式、风险结构和记录规律。'
        '用中文，大白话，直接一点，不要客套。'
    )
    if question.strip():
        prompt += f'\n\n另外回答他的问题：{question.strip()}'
    return prompt


def llm_review(df, question='', api_key=None, model=None, start_equity=None,
               recent=25, timeout=120):
    """调用大模型做复盘。返回 (文本, 用量说明)。"""
    if not (api_key or llm.api_key()):
        raise RuntimeError('没有配置模型 API Key，无法使用 AI 复盘。'
                           '在 .env 里加 LLM_API_KEY=你的key 就能用。'
                           '（规则复盘不需要联网、不花钱，照常可用）')
    prompt = build_prompt(df, question, start_equity, recent)
    if prompt is None:
        raise RuntimeError('还没有可复盘的交易记录。')

    text, usage, used_model = llm.chat(
        '你是严谨的交易行为分析师，只做行为与风险分析，不做价格预测。',
        prompt, model=model, api_key_override=api_key, timeout=timeout,
        temperature=0.4, max_tokens=2000)
    return text, {'模型': used_model, '用量': usage}
