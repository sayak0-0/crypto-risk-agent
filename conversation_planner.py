# -*- coding: utf-8 -*-
"""LLM 对话规划器：理解多轮上下文，把自然语言翻译成规范动作。"""
import json
import re

import llm

MODEL = 'Qwen/Qwen3.5-35B-A3B'
ACTIONS = {
    'plan', 'recommend_plan', 'scan', 'quote', 'positions',
    'account', 'orders', 'news', 'review', 'dashboard',
    'chat', 'clarify', 'cancel',
}


def _json(text):
    t = re.sub(r'^```(?:json)?\s*', '', str(text or '').strip())
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:
        i, j = t.find('{'), t.rfind('}')
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                return None
    return None


def _valid_symbol(sym):
    s = str(sym or '').upper().strip()
    return s if re.fullmatch(r'[A-Z0-9]{2,15}USDT', s) else None


def _canonical(action, symbol, text):
    sym = _valid_symbol(symbol)
    if action == 'recommend_plan':
        return f'给我 {sym} 的开仓方案' if sym else '推荐一个币并生成方案'
    if action == 'plan':
        return f'给我 {sym} 的开仓方案' if sym else '生成交易方案'
    if action == 'quote':
        return f'{sym} 现价多少' if sym else 'BTC 现价多少'
    if action == 'positions':
        return '我的持仓怎么样'
    if action == 'account':
        return '我的币安余额还剩多少'
    if action == 'orders':
        return '我的开单情况和止盈止损在哪里'
    if action == 'scan':
        return '扫描一下全市场有什么异动'
    if action == 'news':
        return '看看现在有哪些高影响新闻'
    if action == 'review':
        return '帮我复盘一下'
    if action == 'dashboard':
        return '看看我的绩效'
    return text


def should_use(text, intent='chat', symbol=None):
    """明确命令免调用；模糊追问、纠正和普通问答交给规划器。"""
    t = str(text or '')
    if intent == 'chat':
        return True
    return bool(re.search(
        r'换一个|换个|不要|排除|除了|重新|不是|稳一点|稳点|风险小|适合我|更|别选|想法|这单|刚才|那个',
        t))


def plan(text, state=None):
    """返回结构化规划；失败时返回 None，调用方回退规则。"""
    state = state or {}
    history = state.get('history') or []
    candidates = state.get('last_candidates') or []
    last_symbol = state.get('last_symbol') or ''
    system = (
        '你是加密货币交易助手的对话规划器。只理解用户意图和上下文，'
        '不分析行情、不预测价格。输出严格 JSON，不要 markdown。'
    )
    user = f'''最近对话：
{json.dumps(history[-8:], ensure_ascii=False)}

状态：
- 上一轮币种：{last_symbol or '无'}
- 上一轮候选：{json.dumps(candidates[:20], ensure_ascii=False)}

当前用户输入：{text}

可选 action：
plan（指定币生成方案）、recommend_plan（推荐并生成方案）、scan、quote、
positions、account、orders、news、review、dashboard、chat、clarify、cancel。

请输出：
{{
  "action": "上述之一",
  "symbol": "币种代码或 null",
  "exclude_symbols": [],
  "constraints": {{"prefer": "", "avoid": ""}},
  "reason": "一句话说明你怎么理解",
  "confidence": 0.0到1.0,
  "clarify": "需要追问时的问题，否则空字符串"
}}

规则：
- “换一个/不要这个/再来一个”时，从上一轮候选中排除上一轮币种，选择下一个。
- 没有指定币但要求方案，用 recommend_plan。
- “余额/还剩多少钱”用 account；“挂单/止盈/止损”用 orders。
- 只有确实无法理解时才用 clarify。'''
    try:
        raw, _usage, _model = llm.chat(
            system, user, model=MODEL, timeout=35,
            temperature=0.0, max_tokens=320)
        obj = _json(raw) or {}
        action = str(obj.get('action') or 'chat').strip()
        if action not in ACTIONS:
            action = 'chat'
        sym = _valid_symbol(obj.get('symbol'))
        excludes = [_valid_symbol(x) for x in (obj.get('exclude_symbols') or [])]
        excludes = [x for x in excludes if x]
        if action in ('recommend_plan', 'plan') and not sym:
            options = [x for x in candidates if x and x not in excludes
                       and x != last_symbol]
            if options:
                sym = options[0]
            elif last_symbol and action == 'plan':
                sym = last_symbol
        return {
            'action': action, 'symbol': sym,
            'exclude_symbols': excludes,
            'constraints': obj.get('constraints') or {},
            'reason': str(obj.get('reason') or '')[:200],
            'confidence': float(obj.get('confidence') or 0),
            'clarify': str(obj.get('clarify') or '')[:300],
            'normalized_text': _canonical(action, sym, text),
            '_model': MODEL,
        }
    except Exception:
        return None