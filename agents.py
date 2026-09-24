# -*- coding: utf-8 -*-
"""多智能体市场判断：一个主持人 + 四个分工分析师。

设计原则（很重要，别改）：
1. 每个分析师只看「自己的那一份数据」，故意不共享全部输入。
   否则它们只是在互相复制观点，多 agent 变成摆设。
2. 每个分析师必须回答「什么情况下我错了」。
   说不出可证伪条件的判断，等于没说。
3. 主持人必须列出分歧点，不允许把不同意见抹平。
4. 每一次判断都记下来（含当时价格），到期自动结算对错。
   这是唯一能让你知道「它到底准不准」的办法。

⚠️ 诚实声明：多智能体 LLM 判断市场方向，实测准确率通常在抛硬币附近。
这不是预言机，是一个帮你多角度看问题的检查清单。请用「判断记录」里的
真实准确率来决定要不要参考它，而不是凭感觉。
"""
import json
import numbers
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

import market
from common import DATA_DIR, get_env
import llm

DEFAULT_MODEL = llm.DEFAULT_MODEL

JUDGMENTS_PATH = os.path.join(DATA_DIR, '判断记录.jsonl')
DEFAULT_HORIZON_HOURS = 24
DEFAULT_CONFIDENCE_GATE = 60       # 低于这个信心度的判断，不当成信号

DIRECTIONS = ['偏多', '偏空', '中性', '无法判断']


# ---------------- 四个分析师怎么分工 ----------------

def _model_short(name):
    return str(name or '模型').split('/')[-1]


def _t(v, digits=2, unit=''):
    """数值格式化。**必须带单位** —— 这是踩过坑的。

    实测事故：资金费率原本传裸数字 0.002746，模型把它读成 0.2746%（放大100倍），
    于是得出"费率极端高位、多头拥挤"的结论，方向判断整个反过来。
    根因是我没标单位，模型只能猜。现在所有数字一律带单位。
    """
    if v is None:
        return '无数据'
    try:
        txt = f'{float(v):,.{digits}f}'
    except (TypeError, ValueError):
        return '无数据'
    return f'{txt}{unit}' if unit else txt


ANALYSTS = [
    {
        '名称': '技术面分析师',
        'system': ('你是加密货币期货的技术面分析师。你只被允许使用价格结构类数据。'
                   '你不看资金费率、不看多空比。'),
        'brief': lambda s, i: {
            '现价': _t(s.get('标记价'), 4, ' USDT'),
            '4小时均线MA20': _t(i.get('MA20'), 4, ' USDT'),
            '4小时均线MA60': _t(i.get('MA60'), 4, ' USDT'),
            '距MA20百分比': _t(i.get('距MA20百分比'), 2, '%（正数=价格在均线上方）'),
            '距MA60百分比': _t(i.get('距MA60百分比'), 2, '%（正数=价格在均线上方）'),
            '近30根K线高点': _t(i.get('近期高点'), 4, ' USDT'),
            '近30根K线低点': _t(i.get('近期低点'), 4, ' USDT'),
            '距近期高点百分比': _t(i.get('距近期高点百分比'), 2, '%（负=在高点下方）'),
            '距近期低点百分比': _t(i.get('距近期低点百分比'), 2, '%（正=在低点上方）'),
            'ATR占价格百分比': _t(i.get('ATR14百分比'), 2, '%（4小时平均波动幅度）'),
            '近24小时涨跌百分比': _t(i.get('近24小时涨跌幅'), 2, '%'),
        },
    },
    {
        '名称': '资金面分析师',
        'system': ('你是加密货币期货的资金面分析师。你只被允许使用资金费率和持仓类数据。'
                   '你不看K线形态、不看多空比。'),
        'brief': lambda s, i: {
            '现价': _t(s.get('标记价'), 4, ' USDT'),
            '资金费率（每8小时，单位%）': _t((s.get('资金费率') or 0) * 100, 4,
                                        '%（正数=多头付钱给空头；常规基准约0.01%）'),
            '持仓量（币）': _t(s.get('持仓量'), 0, ' 币'),
            '持仓量1小时变化': _t((s.get('衍生品趋势') or {}).get('持仓量_1小时变化'), 2, '%'),
            '持仓量4小时变化': _t((s.get('衍生品趋势') or {}).get('持仓量_4小时变化'), 2, '%'),
            '资金费率近3期均值': _t((s.get('衍生品趋势') or {}).get('资金费率_最近3期均值'), 4, '%'),
            '资金费率趋势变化': _t((s.get('衍生品趋势') or {}).get('资金费率_趋势变化'), 4, '%'),
            '标记价相对现货指数基差': _t((s.get('衍生品趋势') or {}).get('基差百分比'), 4, '%'),
            '24小时成交额': _t(s.get('24h成交额'), 0, ' USDT'),
        },
    },
    {
        '名称': '情绪面分析师',
        'system': ('你是加密货币市场的情绪面分析师。你只被允许使用多空账户比例和涨跌类数据。'
                   '你不看均线、不看资金费率。'),
        'brief': lambda s, i: {
            '现价': _t(s.get('标记价'), 4, ' USDT'),
            '账户多空比（>1=多头账户更多）': _t(s.get('多空比'), 3),
            '多头账户占比（0-1之间的小数）': _t(s.get('多头账户占比'), 3),
            '24小时涨跌幅': _t(s.get('24h涨跌幅'), 2, '%'),
            '24小时最高': _t(s.get('24h最高'), 4, ' USDT'),
            '24小时最低': _t(s.get('24h最低'), 4, ' USDT'),
            '当前价在24小时区间的相对位置': _range_pos(s),
            '多空比4小时变化': _t((s.get('衍生品趋势') or {}).get('多空比_4小时变化'), 3),
            '主动买卖比（>1=主动买更强）': _t((s.get('衍生品趋势') or {}).get('主动买卖比_当前'), 3),
            '主动买卖比4小时变化': _t((s.get('衍生品趋势') or {}).get('主动买卖比_4小时变化'), 3),
        },
    },
    {
        '名称': '风控官',
        'system': ('你是交易风控官。你不判断方向，只评估「现在这个位置开仓，风险在哪里」。'
                   '你的职责是泼冷水，不是给信号。'),
        'brief': lambda s, i: {
            '现价': _t(s.get('标记价'), 4, ' USDT'),
            'ATR占价格百分比': _t(i.get('ATR14百分比'), 2, '%（4小时平均波动幅度）'),
            '距近期高点百分比': _t(i.get('距近期高点百分比'), 2, '%（负=在高点下方）'),
            '距近期低点百分比': _t(i.get('距近期低点百分比'), 2, '%（正=在低点上方）'),
            '资金费率（每8小时，单位%）': _t((s.get('资金费率') or 0) * 100, 4,
                                        '%（正数=多头付钱；常规基准约0.01%）'),
            '账户多空比（>1=多头账户更多）': _t(s.get('多空比'), 3),
        },
    },
]


NEWS_ANALYST = {
    '名称': '事件/新闻分析师',
    'system': ('你是加密货币事件与新闻分析师。你只能使用提供的公开新闻证据，'
               '必须区分事实、媒体观点和推断。禁止编造新闻。'),
    'brief': lambda s, i: {
        '币种': market.normalize_symbol(s.get('币种') or ''),
        '公开新闻证据': s.get('_新闻证据') or '本次没有检索到相关公开新闻',
        '要求': '判断新闻对波动和风险的影响，不预测具体价格。',
    },
}


def _range_pos(snap):
    """当前价在 24 小时区间的相对位置（0%=最低，100%=最高）。"""
    hi, lo, p = snap.get('24h最高'), snap.get('24h最低'), snap.get('标记价')
    if hi is None or lo is None or p is None or hi <= lo:
        return '无数据'
    return f'{(p - lo) / (hi - lo) * 100:.1f}%'


ANALYST_PROMPT = '''这里是你在本次分析中能看到的全部数据：

{data}

请严格按下面的 JSON 格式输出，不要加任何解释文字、不要加 markdown 代码块：

{{
  "方向": "偏多 或 偏空 或 中性 或 无法判断",
  "信心": 0到100的整数,
  "核心理由": "一到两句话，必须引用上面数据里的具体数字",
  "主要风险": "你看到的、与你结论相反的证据",
  "什么情况下我错了": "一个具体的、可验证的条件，比如『价格跌破X』『费率转为负』",
  "我看不到什么": "你这块数据无法覆盖的信息"
}}

硬性要求：
- 不要预测具体价格点位。
- 信心度要诚实。数据不足或信号矛盾时，就给低信心（30 以下），不要为了显得专业而虚高。
- 如果你这份数据根本判断不了方向，就直接选「无法判断」，这是允许的、也是正确的回答。
- 中文回答。'''

CHAIR_PROMPT = '''你是这次分析的主持人。下面是四位分析师独立给出的结论（他们各自只看到自己那块数据）：

{analyst_results}

四位分析师的原始数据背景：{context}

请按下面的 JSON 格式输出，不要加任何解释文字、不要加 markdown 代码块：

{{
  "方向": "偏多 或 偏空 或 中性 或 无法判断",
  "信心": 0到100的整数,
  "倾向": "偏多 或 偏空（即使无法判断方向，也必须给一个弱倾向）",
  "倾向强度": "弱 或 中 或 强",
  "数据核对": "逐个核对辩论中引用的关键数字。如果有引用错误，明确指出是谁错了，以及这对结论的影响",
  "共识": "四位分析师在哪些点上是一致的",
  "分歧": "他们在哪些点上不一致。如果确实没分歧，请明确说『没有分歧，但这可能只是因为他们看的是同一批公开信息，不构成强信号』",
  "综合判断": "两三句话。必须说明你的结论主要建立在谁的判断上、为什么",
  "最重要的反面证据": "如果有人要反驳你，最有力的一条是什么",
  "什么情况下我错了": "一个可验证的具体条件",
  "给交易者的提醒": "一句话"
}}

硬性要求：
- 不要预测具体价格点位。
- 不要因为「多数分析师看多」就下偏多的结论。多数一致不等于正确，可能是同质化。
- 风控官不判断方向，如果他的结论和其他人冲突，请说明那是视角不同，不是矛盾。
- 如果四位里有两位以上是「无法判断」或信心低于 30，你的最终信心也不应该高，但仍必须给「倾向」。
- 「方向」可以是无法判断，但「倾向」不能留空。
- 中文回答。'''


# ---------------- 辩论环节（借鉴 TradingAgents 的设计） ----------------
# 为什么要有这个：四个分析师看的是同一批公开数据，很可能全都偏多 ——
# 那是「共识」还是「同质化」分不清。
# 强制安排一个专职看多、一个专职看空，从制度上保证有对立观点。

BULL_SYSTEM = ('你是一位坚定的看多研究员。你的职责是找出所有支持上涨的证据，'
               '构建最强的做多论证。这不是让你说违心话 —— 而是让看多的那一面'
               '被充分表达出来，避免被看空的声量盖过。')

BEAR_SYSTEM = ('你是一位坚定的看空研究员。你的职责是找出所有支持下跌的证据，'
               '构建最强的做空论证。这不是让你说违心话 —— 而是让看空的那一面'
               '被充分表达出来，避免被看多的声量盖过。')

DEBATE_ROUND1 = '''某标的当前价格：{price}

四位分析师（各自只看到局部数据）的结论：
{views}

你现在的任务是：{side}。

请输出 JSON（只输出 JSON，不要其他文字）：
{{
  "立场": "{side}",
  "核心论证": "三条理由，每条都要引用上面数据里的具体数字",
  "最有力的证据": "你认为最不能反驳的那一条",
  "我的弱点": "你的立场最大的漏洞是什么，诚实说",
  "什么情况下我认输": "一个可验证的条件"
}}

要求：
- 必须引用具体数字，不要说空话
- 「我的弱点」必须诚实。说不出弱点的论证不值得信
- 中文回答'''

DEBATE_ROUND2 = '''某标的当前价格：{price}

你原本的立场：{side}
你第一轮的论证：
{my_first}

对立方的论证：
{opponent}

现在请**直接反驳对方**。

输出 JSON（只输出 JSON，不要其他文字）：
{{
  "对方的破绽": "对方论证里最站不住脚的一点，要具体指出是哪条",
  "我的回应": "针对对方的核心论点，你的反驳",
  "我改变了吗": "完全没变 或 部分调整 或 我认输了",
  "调整后的立场": "偏多/偏空/中性/无法判断",
  "调整后的信心": 0到100的整数,
  "最后仍然确定的一点": "不管怎么辩论，你最有把握的那一条"
}}

要求：
- 必须针对对方的具体论点反驳，不能泛泛而谈
- 如果你觉得对方说得对，就承认。辩论不是为了赢，是为了找出真相
- 中文回答'''

DEBATE_CHAIR_ADDON = '''

另外，这里有一场看多研究员和看空研究员的结构化辩论：

{debate}

【⚠️ 强制步骤：数据核对 —— 这一步不能跳过】

实测发现辩论双方**会引用错误的数字**。曾出现过这种情况：
看多方引用「24小时涨跌+0.44%」，实际数据是 -0.10%，
看空方正确指出了这个错误，但主持人却判定「看多方数据更符合原始数据」——
把唯一正确的信息丢了。

所以你必须：
1. **逐个核对**辩论中出现的每个关键数字，是否与上面的「原始数据背景」一致。
2. 如果有任何一方引用了**与原始数据不符的数字**，**明确指出是谁错了**，
   并且**降低对其整段论证的权重** —— 一个引错数据的人，其他推理也不可信。
3. **特别注意**：如果一方指出了对方的数字错误，而你的核对确认这个指正是对的，
   那么在这一轮里，**指出错误的那一方是更可信的**。
4. 你的最终结论**不能建立在错误的数字上**。

核对完之后再考虑：
- 谁的论证更扎实？谁回避了对方的关键问题？
- 双方在辩论后是否调整了立场？调整为"我认输"的信息量很大。
- 把你的最终方向判断和这场辩论的结论对照：一致还是冲突？
'''

# ---------------- 小模型友好版提示词 ----------------
# 小模型的毛病不是「不够聪明」，而是「不守格式、爱说空话」。
# 对策：给现成例子（示例对 7B 模型效果极好）+ 减少必填字段 + 句子改短。

ANALYST_COMPACT = '''下面是你负责的数据：

{data}

请照下面这个例子的格式，输出你自己的结论（只输出 JSON，不要其他文字）：

举例：
{{
  "方向": "偏空",
  "信心": 65,
  "核心理由": "资金费率 0.1200% 偏高，多头拥挤；多空比 2.1 散户一边倒做多",
  "什么情况下我错了": "资金费率转负、或价格站上 89000"
}}

要求：
- "方向" 只能填：偏多 / 偏空 / 中性 / 无法判断
- "信心" 填 0 到 100 的整数
- "核心理由" 必须引用上面数据里的具体数字，不要写空话
- 数据看不出方向就填 "无法判断"，这是允许的
- 只输出 JSON'''

CHAIR_COMPACT = '''四位分析师的结论：

{analyst_results}

数据背景：{context}

请照下面例子的格式输出（只输出 JSON，不要其他文字）：

举例：
{{
  "方向": "偏空",
  "信心": 60,
  "倾向": "偏空",
  "倾向强度": "中",
  "分歧": "技术面偏多，资金面偏空，两人看法相反",
  "综合判断": "偏空。主要依据资金面：费率 0.1200% 偏高，多头拥挤",
  "什么情况下我错了": "价格站上 89000 且费率回落"
}}

要求：
- "方向" 只能填：偏多 / 偏空 / 中性 / 无法判断
- 不要因为多数人看多就填偏多，多数的看法可能只是重复
- 有两个人以上填 "无法判断"，你的信心就别超过 50
- 方向可以无法判断，但必须给倾向：偏多 或 偏空
- 只输出 JSON'''

# ---------------- LLM 调用 ----------------

def _extract_json(text):
    """从模型回复里抠出 JSON，兼容各种包裹方式。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find('{')
    end = t.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            pass
    return None


def _clean(obj):
    """把 NaN 之类清掉，方便写 JSON。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            cv = _clean(v)
            if cv is None and isinstance(v, numbers.Real) and not isinstance(v, bool) \
                    and pd.isna(v):
                continue
            out[k] = cv
        return out
    if isinstance(obj, (list, tuple)):
        return [_clean(x) for x in obj]
    if isinstance(obj, numbers.Real) and not isinstance(obj, bool):
        if pd.isna(obj):
            return None
        return round(float(obj), 6)
    return obj


def call_llm(system, user, model=None, api_key=None, timeout=150,
             temperature=0.5, max_tokens=900, role=None):
    """调用模型（走统一的 llm 层，支持任何 OpenAI 兼容服务）。

    role='analyst' / 'chair' 时会自动选用对应的模型配置。
    """
    return llm.chat(system, user, model=model or llm.model_name(role),
                    api_key_override=api_key, timeout=timeout,
                    temperature=temperature, max_tokens=max_tokens)


def analyze_symbol(symbol, exchange='自动', model=None, api_key=None,
                   on_progress=None, prompt_mode='auto', enable_debate=False,
                   extra_context=None):
    """跑一次完整的多智能体分析。

    on_progress(消息文本) 会在每个阶段被调用，方便界面显示进度。
    返回 dict：{快照, 指标, 分析师, 主持人, 用量, 模型}
    """
    if prompt_mode == 'auto':
        analyst_mode = llm.prompt_mode('analyst')
        chair_mode = llm.prompt_mode('chair')
    else:
        analyst_mode = chair_mode = prompt_mode
    prompt_mode = analyst_mode

    def prog(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    prog(f'正在拉取 {symbol} 的行情数据……')
    snap = market.snapshot(symbol, exchange)
    ind = market.compute_indicators(snap.get('K线'), snap.get('标记价'))
    if not ind:
        raise RuntimeError('K线数据不足，算不出技术指标。')
    prog('正在读取衍生品时间序列和公开新闻证据……')
    snap['衍生品趋势'] = market.derivatives_trend(symbol)
    snap['_新闻证据'] = extra_context or ''
    active_analysts = list(ANALYSTS) + ([NEWS_ANALYST] if extra_context else [])

    def run_one(pair):
        idx, a = pair
        brief = a['brief'](snap, ind)
        # 每个分析师用各自配置的模型（多模型协商）
        my_model = model or llm.analyst_model(idx)
        prog(f'{_model_short(my_model)}｜{a["名称"]}｜开始分析')
        use_compact = (prompt_mode == 'compact')
        tmpl = ANALYST_COMPACT if use_compact else ANALYST_PROMPT
        user = tmpl.format(data=json.dumps(_clean(brief), ensure_ascii=False, indent=1))

        parsed, usage_total, attempts, raw = None, {}, 0, ''
        last = ''
        fail_reason = None
        for attempt in range(2):        # 解析失败自动重试一次
            attempts += 1
            prompt = user
            if attempt == 1:
                prompt = (user + '\n\n【重要】你上次的输出不是合法 JSON。'
                                 '这次请只输出一个 JSON 对象，'
                                 '不要 markdown 代码块、不要任何解释文字。')
            try:
                text, usage, used_m = call_llm(
                    a['system'], prompt, my_model, api_key, role='analyst',
                    timeout=240,
                    temperature=0.2 if attempt else 0.4,
                    max_tokens=500 if use_compact else 700)
            except Exception as e:
                # 单个模型超时/报错，不能拖垮整轮分析 ——
                # 用四个不同厂商的模型时，遇到慢模型的概率会明显变高。
                fail_reason = f'{type(e).__name__}: {str(e)[:80]}'
                usage = {}
                text = ''
                break
            last = text or ''
            for k, v in (usage or {}).items():
                if isinstance(v, (int, float)):
                    usage_total[k] = usage_total.get(k, 0) + v
            parsed = _extract_json(text)
            if parsed:
                break

        ok = bool(parsed)
        if not ok:
            if fail_reason:
                parsed = {'方向': '无法判断', '信心': 0,
                          '核心理由': f'调用失败（{fail_reason}）',
                          '主要风险': '', '什么情况下我错了': '',
                          '我看不到什么': ''}
                parsed['_失败'] = fail_reason
            else:
                parsed = {'方向': '无法判断', '信心': 0,
                          '核心理由': '模型输出解析失败（重试后仍失败）',
                          '主要风险': '', '什么情况下我错了': '',
                          '我看不到什么': ''}
                raw = last[:500]
        parsed['_名称'] = a['名称']
        parsed['_数据'] = brief
        parsed['_格式合规'] = ok
        parsed['_调用次数'] = attempts
        parsed['_模型'] = my_model
        prog(f'{_model_short(my_model)}｜{a["名称"]}｜完成（{parsed.get("方向", "未知")}，信心 {parsed.get("信心", 0)}）')
        if raw:
            parsed['_原始输出'] = raw
        return parsed, usage_total
    prog(f'{len(active_analysts)} 位分析师正在并行分析……'
         + ('（小模型友好模式）' if prompt_mode == 'compact' else ''))
    results, total_usage = [], {}
    with ThreadPoolExecutor(max_workers=len(active_analysts)) as pool:
        for parsed, usage in pool.map(run_one, list(enumerate(active_analysts))):
            results.append(parsed)
            for k, v in (usage or {}).items():
                if isinstance(v, (int, float)):
                    total_usage[k] = total_usage.get(k, 0) + v

    prog('主持人正在汇总……')
    condensed = [{'分析师': r['_名称'], '方向': r.get('方向'), '信心': r.get('信心'),
                  '核心理由': r.get('核心理由'), '主要风险': r.get('主要风险'),
                  '什么情况下我错了': r.get('什么情况下我错了')} for r in results]
    # ⚠️ 这些是主持人用来「核对辩论里数字」的基准，必须带单位，
    # 否则它会像之前那样把 0.002746 读成 0.2746%
    context = json.dumps(_clean({
        '币种': market.normalize_symbol(symbol),
        '现价': _t(snap.get('标记价'), 4, ' USDT'),
        '4小时ATR百分比': _t(ind.get('ATR14百分比'), 2, '%'),
        '24小时涨跌幅': _t(snap.get('24h涨跌幅'), 2, '%'),
        '资金费率（每8小时，单位%）': _t((snap.get('资金费率') or 0) * 100, 4,
                                    '%（正=多头付钱；常规基准约0.01%）'),
        '账户多空比（>1=多头账户更多）': _t(snap.get('多空比'), 3),
        '持仓量1小时变化': _t((snap.get('衍生品趋势') or {}).get('持仓量_1小时变化'), 2, '%'),
        '持仓量4小时变化': _t((snap.get('衍生品趋势') or {}).get('持仓量_4小时变化'), 2, '%'),
        '基差百分比': _t((snap.get('衍生品趋势') or {}).get('基差百分比'), 4, '%'),
        '主动买卖比': _t((snap.get('衍生品趋势') or {}).get('主动买卖比_当前'), 3),
        '公开新闻证据': (extra_context or '无')[:3000],
    }), ensure_ascii=False, indent=1)

    debate_result = None
    debate_text = ''
    if enable_debate:
        def run_side(side, system, opponent=None, my_first=None):
            if opponent is None:
                user = DEBATE_ROUND1.format(
                    price=f'{snap["标记价"]:,.4f}',
                    views=json.dumps(_clean(condensed), ensure_ascii=False, indent=1),
                    side=side)
            else:
                user = DEBATE_ROUND2.format(
                    price=f'{snap["标记价"]:,.4f}', side=side,
                    my_first=json.dumps(my_first, ensure_ascii=False, indent=1),
                    opponent=json.dumps(opponent, ensure_ascii=False, indent=1))
            try:
                txt, us, md = call_llm(system, user, model, api_key, role='chair',
                                       timeout=240,
                                       temperature=0.5, max_tokens=900)
                return _extract_json(txt) or {'_解析失败': (txt or '')[:200]}, us
            except Exception as e:
                return {'_失败': f'{type(e).__name__}: {str(e)[:80]}'}, {}

        debate_model = model or llm.model_name('chair')
        prog(f'{_model_short(debate_model)}｜看多研究员｜第一轮论证')
        prog(f'{_model_short(debate_model)}｜看空研究员｜第一轮论证')
        with ThreadPoolExecutor(max_workers=2) as pool:
            fb = pool.submit(run_side, '看多', BULL_SYSTEM)
            fr = pool.submit(run_side, '看空', BEAR_SYSTEM)
            bull1, u1 = fb.result()
            bear1, u2 = fr.result()
        for u in (u1, u2):
            for k, v in (u or {}).items():
                if isinstance(v, (int, float)):
                    total_usage[k] = total_usage.get(k, 0) + v

        prog('第二轮：互相反驳……')
        with ThreadPoolExecutor(max_workers=2) as pool:
            fb2 = pool.submit(run_side, '看多', BULL_SYSTEM, bear1, bull1)
            fr2 = pool.submit(run_side, '看空', BEAR_SYSTEM, bull1, bear1)
            bull2, u3 = fb2.result()
            bear2, u4 = fr2.result()
        for u in (u3, u4):
            for k, v in (u or {}).items():
                if isinstance(v, (int, float)):
                    total_usage[k] = total_usage.get(k, 0) + v

        def _norm(x):
            """模型有时把「三条理由」输出成列表，界面上会显示成 Python 列表，
            这里统一拍成文本。"""
            out = dict(x or {})
            for k, v in list(out.items()):
                if isinstance(v, list):
                    out[k] = '\n'.join(f'{i+1}. {s}' for i, s in enumerate(v))
            return out

        prog(f'{_model_short(debate_model)}｜看多研究员｜反驳空方')
        prog(f'{_model_short(debate_model)}｜看空研究员｜反驳多方')
        debate_result = {'看多第一轮': _norm(bull1), '看空第一轮': _norm(bear1),
                         '看多反驳': _norm(bull2), '看空反驳': _norm(bear2)}
        debate_text = json.dumps(_clean(debate_result), ensure_ascii=False, indent=1)

    chair_tmpl = CHAIR_COMPACT if chair_mode == 'compact' else CHAIR_PROMPT
    chair_user = chair_tmpl.format(
            analyst_results=json.dumps(_clean(condensed), ensure_ascii=False, indent=1),
            context=context)
    if debate_text:
        chair_user += DEBATE_CHAIR_ADDON.format(debate=debate_text)
    chair_system = '你是严谨的金融市场分析主持人，负责汇总多位分析师和一场多空辩论。'
    chair_model = model or llm.model_name('chair')
    prog(f'{_model_short(chair_model)}｜主持人｜正在整合 5 位分析师和辩论结论')
    try:
        chair_text, chair_usage, used_model = call_llm(
            chair_system, chair_user, model, api_key, role='chair',
            timeout=300,
            temperature=0.3 if chair_mode == 'compact' else 0.4,
            max_tokens=700 if chair_mode == 'compact' else 1100)
    except Exception as first_error:
        # 主持人超时不能拖垮整张方案：自动换更快的分析模型重试一次。
        prog(f'{_model_short(llm.model_name("analyst"))}｜主持人（备用）｜正在重新整合')
        try:
            chair_text, chair_usage, used_model = call_llm(
                chair_system, chair_user, llm.model_name('analyst'), api_key,
                role='analyst', timeout=180,
                temperature=0.3,
                max_tokens=700 if chair_mode == 'compact' else 900)
        except Exception:
            raise first_error
    chair = _extract_json(chair_text) or {
        '方向': '无法判断', '信心': 0, '共识': '', '分歧': '',
        '综合判断': '模型输出解析失败', '最重要的反面证据': '',
        '什么情况下我错了': '', '给交易者的提醒': '',
        '_原始输出': (chair_text or '')[:800]}
    for k, v in (chair_usage or {}).items():
        if isinstance(v, (int, float)):
            total_usage[k] = total_usage.get(k, 0) + v

    n_analyst = len(results)
    n_ok = sum(1 for r in results if r.get('_格式合规'))
    n_fail = sum(1 for r in results if r.get('_失败'))
    compliance = {
        '分析师模型': '、'.join(f"{a['_名称']}={a.get('_模型')}"
                           for a in results if a.get('_模型')),
        '主持人模型': llm.model_name('chair'),
        '提示词模式': f'分析师={analyst_mode} / 主持人={chair_mode}',
        '分析师格式合规': f'{n_ok}/{n_analyst}',
        '格式合规率': round(n_ok / n_analyst * 100, 1) if n_analyst else 0,
        '调用失败数': n_fail,
        '失败明细': [f"{r['_名称']}({r.get('_模型')}): {r.get('_失败')}"
                    for r in results if r.get('_失败')],
        '总模型调用次数': sum(r.get('_调用次数', 1) for r in results) + 1,
    }

    return {
        '辩论': debate_result,
        '格式合规': compliance,
        '币种': market.normalize_symbol(symbol),
        '时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '当时价格': snap.get('标记价'),
        '数据源': snap.get('交易所'),
        '快照': _clean({k: v for k, v in snap.items()
                        if k in ('标记价', '资金费率', '多空比', '24h涨跌幅',
                                 '持仓量', '24h最高', '24h最低')}),
        '指标': _clean(ind),
        '分析师': _clean(results),
        '主持人': _clean(chair),
        '衍生品趋势': _clean(snap.get('衍生品趋势') or {}),
        '新闻证据': extra_context or '',
        '用量': total_usage,
        '模型': used_model,
    }


# ---------------- 判断记录与事后结算 ----------------

def record_judgment(result, horizon_hours=DEFAULT_HORIZON_HOURS, note=''):
    """把一次判断记下来，到期后可以结算对错。"""
    chair = result.get('主持人') or {}
    row = {
        '记录时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '币种': result.get('币种'),
        '方向': chair.get('方向'),
        '信心': chair.get('信心'),
        '当时价格': result.get('当时价格'),
        '结算小时数': int(horizon_hours),
        '结算时间': (datetime.now() + timedelta(hours=horizon_hours)
                     ).strftime('%Y-%m-%d %H:%M:%S'),
        '结论': chair.get('综合判断'),
        '可证伪条件': chair.get('什么情况下我错了'),
        '备注': note,
        '已结算': False,
        '到期价格': None,
        '涨跌幅%': None,
        '对不对': None,
        '分析师方向': [{'名称': a.get('_名称'), '模型': a.get('_模型'),
                        '方向': a.get('方向'), '信心': a.get('信心')}
                       for a in (result.get('分析师') or [])],
        '用量': result.get('用量') or {},
        '模型': result.get('模型'),
    }
    os.makedirs(os.path.dirname(JUDGMENTS_PATH), exist_ok=True)
    with open(JUDGMENTS_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(_clean(row), ensure_ascii=False) + '\n')
    return row


def load_judgments(limit=None):
    """读取全部判断记录（旧的在前面）。"""
    if not os.path.exists(JUDGMENTS_PATH):
        return []
    rows = []
    with open(JUDGMENTS_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows[-limit:] if limit else rows


def save_judgments(rows):
    os.makedirs(os.path.dirname(JUDGMENTS_PATH), exist_ok=True)
    with open(JUDGMENTS_PATH, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(_clean(r), ensure_ascii=False) + '\n')
    return JUDGMENTS_PATH


def _judge(direction, change_pct):
    """判断一次预测对不对。中性/无法判断不参与准确率统计。"""
    if direction == '偏多':
        return change_pct > 0
    if direction == '偏空':
        return change_pct < 0
    return None


def settle_due(now=None, min_confidence=0, price_getter=None):
    """把到期的判断结算掉。返回 (本次结算条数, 错误列表)。"""
    now = now or datetime.now()
    rows = load_judgments()
    if not rows:
        return 0, []
    getter = price_getter or (lambda sym: market.snapshot(sym, '自动').get('标记价'))
    price_cache, settled, errors = {}, 0, []

    for r in rows:
        if r.get('已结算'):
            continue
        if (r.get('信心') or 0) < min_confidence:
            continue
        try:
            due = datetime.fromisoformat(r['结算时间'])
        except Exception:
            continue
        if now < due:
            continue
        sym = r.get('币种')
        try:
            if sym not in price_cache:
                price_cache[sym] = getter(sym)
            px = price_cache[sym]
            if not px:
                continue
            entry = float(r.get('当时价格') or 0)
            if entry <= 0:
                errors.append(f'{sym}: 记录的当时价格无效，跳过')
                continue
            change = (px - entry) / entry * 100
            r['到期价格'] = px
            r['涨跌幅%'] = round(change, 4)
            r['对不对'] = _judge(r.get('方向'), change)
            r['已结算'] = True
            r['结算执行时间'] = now.strftime('%Y-%m-%d %H:%M:%S')
            settled += 1
        except Exception as e:
            errors.append(f'{sym}: {e}')

    if settled:
        save_judgments(rows)
    return settled, errors


def accuracy_stats(rows=None, min_confidence=DEFAULT_CONFIDENCE_GATE):
    """统计准确率。min_confidence 以上的判断才算「可交易信号」。"""
    rows = rows if rows is not None else load_judgments()
    out = {
        '总判断数': len(rows),
        '已结算': 0,
        '待结算': 0,
        '可统计': 0,
        '命中': 0,
        '准确率': None,
        '高信心准确率': None,
        '平均信心': None,
        '方向分布': {},
    }
    hit_all = tot_all = hit_hi = tot_hi = 0
    confs = []
    for r in rows:
        d = r.get('方向')
        out['方向分布'][d] = out['方向分布'].get(d, 0) + 1
        if r.get('信心') is not None:
            confs.append(r['信心'])
        if not r.get('已结算'):
            out['待结算'] += 1
            continue
        out['已结算'] += 1
        verdict = r.get('对不对')
        if verdict is None:
            continue
        out['可统计'] += 1
        tot_all += 1
        hit_all += 1 if verdict else 0
        if (r.get('信心') or 0) >= min_confidence:
            tot_hi += 1
            hit_hi += 1 if verdict else 0
    if confs:
        out['平均信心'] = round(sum(confs) / len(confs), 1)
    if tot_all:
        out['命中'] = hit_all
        out['准确率'] = round(hit_all / tot_all * 100, 1)
    if tot_hi:
        out['高信心准确率'] = round(hit_hi / tot_hi * 100, 1)
        out['高信心样本数'] = tot_hi
    return out


def model_accuracy(rows=None):
    """统计每个分析师模型的准确率。

    这是「多模型协商」真正的价值验证：
    跑一段时间后，你能看到哪家的模型在这个任务上判断更准，
    以及「主持人综合后」是变准了还是被带偏了。
    """
    rows = rows if rows is not None else load_judgments()
    stats, chair = {}, {'对': 0, '错': 0}
    for r in rows:
        if not r.get('已结算'):
            continue
        chg = r.get('涨跌幅%')
        if chg is None:
            continue
        # 主持人
        v = _judge(r.get('方向'), chg)
        if v is not None:
            chair['对' if v else '错'] += 1
        # 各分析师
        for a in r.get('分析师方向') or []:
            m = a.get('模型')
            if not m:
                continue
            v2 = _judge(a.get('方向'), chg)
            if v2 is None:
                continue
            st = stats.setdefault(m, {'对': 0, '错': 0, '弃权': 0})
            st['对' if v2 else '错'] += 1
    out = {}
    for m, st in stats.items():
        n = st['对'] + st['错']
        out[m] = {'样本': n, '对': st['对'], '错': st['错'],
                  '准确率': round(st['对'] / n * 100, 1) if n else None}
    cn = chair['对'] + chair['错']
    if cn:
        out['【主持人综合】'] = {'样本': cn, '对': chair['对'], '错': chair['错'],
                            '准确率': round(chair['对'] / cn * 100, 1)}
    return out


def honest_verdict(stats):
    """根据真实统计，给一句人话评价。"""
    n = stats.get('可统计', 0)
    if n == 0:
        return ('还没有结算过任何判断。记录满 20 次以上、并且结算过，'
                '这个数字才有意义 —— 现在就当它是个多角度看问题的清单，别当信号。')
    acc = stats.get('准确率')
    if n < 20:
        return (f'只结算了 {n} 次，样本太小（准确率 {acc}% 说明不了什么）。'
                '至少跑到 20 次以上再下结论，50 次以上才比较可信。')
    if acc < 45:
        return (f'⚠️ 结算 {n} 次，准确率 {acc}%，比抛硬币还差。'
                '结论很明确：不要按它的方向做单。它还有价值的部分是「风险提示」和「可证伪条件」，'
                '那是它说自己会在什么情况下看错 —— 这个信息本身有用。')
    if acc < 55:
        return (f'结算 {n} 次，准确率 {acc}%，和抛硬币没区别。'
                '请把它当作「多角度检查清单」使用，不要当作方向信号。')
    return (f'结算 {n} 次，准确率 {acc}%。高于抛硬币，但请注意：'
            '样本内表现好不等于未来有效，而且这个数字可能受市场单边行情影响。'
            '继续记录，看长期是否稳定。')