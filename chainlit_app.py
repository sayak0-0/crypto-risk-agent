# -*- coding: utf-8 -*-
"""Chainlit 原型界面：复用现有对话、方案和风控后端。"""
import asyncio
import re
import time

import chainlit as cl

import chat
import llm
import plan
import rag
import tasks
from common import load_config

APP_NAME = '合约交易助手'

QUICK_ACTIONS = [
    ('生成方案', '帮我分析一下 BTC'),
    ('适合开仓', '适合开仓的币种'),
    ('扫描市场', '扫描一下全市场有什么异动'),
    ('账户余额', '我的币安余额还剩多少'),
    ('查看持仓', '我的持仓怎么样'),
    ('挂单/止盈止损', '我的开单情况和止盈止损在哪里'),
    ('复盘', '帮我复盘一下我亏在哪'),
    ('绩效', '看看我的绩效'),
    ('查行情', 'BTC 现价多少'),
    ('新闻风险', '看看现在有哪些高影响新闻'),
]

SYMBOLS = [
    ('BTC', 'BTCUSDT'), ('ETH', 'ETHUSDT'), ('SOL', 'SOLUSDT'),
    ('BNB', 'BNBUSDT'), ('XRP', 'XRPUSDT'), ('DOGE', 'DOGEUSDT'),
]


def _actions():
    out = [cl.Action(name='quick_action', label=label,
                     payload={'prompt': prompt})
           for label, prompt in QUICK_ACTIONS]
    out += [cl.Action(name='quick_action', label=label,
                      payload={'prompt': f'帮我分析一下 {symbol}'})
            for label, symbol in SYMBOLS]
    return out


def _quote_md(res):
    snap, ind = res.get('快照') or {}, res.get('指标') or {}
    lines = [f"### {res.get('币种') or 'BTCUSDT'} 行情", '']
    if snap.get('标记价') is not None:
        lines.append(f"- 现价：**{snap['标记价']:,.4f}**")
    if snap.get('24h涨跌幅') is not None:
        lines.append(f"- 24小时涨跌：**{snap['24h涨跌幅']:+.2f}%**")
    if snap.get('资金费率') is not None:
        lines.append(f"- 8小时资金费率：**{snap['资金费率'] * 100:+.4f}%**")
    if snap.get('多空比') is not None:
        lines.append(f"- 账户多空比：**{snap['多空比']:.2f}**")
    if ind.get('ATR14百分比') is not None:
        lines.append(f"- 4小时 ATR：**{ind['ATR14百分比']:.2f}%**")
    if ind.get('距MA20百分比') is not None:
        lines.append(f"- 距 MA20：**{ind['距MA20百分比']:+.2f}%**")
    if snap.get('获取时间'):
        lines += ['', f"数据时间：{snap.get('获取时间')}"]
    return '\n'.join(lines)


def _positions_md(res):
    if res.get('提示'):
        return res['提示']
    lines = ['### 当前持仓', '']
    for p in res.get('持仓') or []:
        if p.get('错误'):
            lines.append(f"- {p.get('币种')}：拉取失败")
            continue
        lines.append(
            f"- **{p.get('币种')}** {p.get('方向')} {p.get('杠杆')}x｜"
            f"现价 {p.get('当前价', 0):,.4f}｜浮盈亏 {p.get('浮动盈亏', 0):+,.2f} U"
        )
        if p.get('距爆仓百分比') is not None:
            lines.append(f"  - 距爆仓：{p['距爆仓百分比']:.2f}%")
        if p.get('距止损百分比') is not None:
            lines.append(f"  - 距止损：{p['距止损百分比']:.2f}%")
    return '\n'.join(lines)


def _scan_md(res):
    if res.get('错误'):
        return f"扫描失败：{res['错误']}"
    lines = [f"### 市场异动扫描", '',
             f"全市场 {res.get('全市场合约数', 0)} 个合约｜扫描时间 {res.get('扫描时间', '')}", '']
    cats = res.get('分类') or {}
    for name, val in list(cats.items())[:6]:
        rows = (val or {}).get('数据') or []
        lines.append(f"**{name}**：{len(rows)} 个")
        for item in rows[:5]:
            lines.append(
                f"- {item.get('币种', '')}｜24h {item.get('24h涨跌%', 0):+.2f}%｜"
                f"费率 {item.get('资金费率%', 0):+.4f}%｜{item.get('筛选理由', '')}"
            )
        lines.append('')
    lines.append('这是事实筛选，不是买入推荐。')
    return '\n'.join(lines)


def _news_md(res):
    scan = res.get('扫描') or {}
    note = res.get('风险') or {}
    lines = [f"### 新闻风险", '', note.get('提示') or '暂无提示。', '',
             f"共抓取 {scan.get('总条数', 0)} 条，高影响 {scan.get('高影响条数', 0)} 条。", '']
    for item in (scan.get('高影响事件') or [])[:12]:
        title = item.get('标题') or ''
        src = item.get('来源') or ''
        lines.append(f"- {title}（{src}）")
    lines += ['', '新闻只用于判断波动风险，不判断利好利空。']
    return '\n'.join(lines)


def _review_md(res):
    lines = [str(x) for x in (res.get('结论') or [])]
    return '\n\n'.join(lines) or '还没有可复盘的交易记录。'


def _dashboard_md(res):
    s = res.get('指标') or {}
    if not s or not s.get('交易笔数'):
        return '还没有已平仓的交易记录。'
    return '\n'.join([
        '### 绩效',
        '',
        f"- 总净盈亏：**{s.get('总净盈亏', 0):+,.2f} U**",
        f"- 胜率：**{s.get('胜率', 0):.1f}%**",
        f"- 盈亏比：**{s.get('盈亏比', 0):.2f}**",
        f"- 盈利因子：**{s.get('盈利因子', 0):.2f}**",
        f"- 平均 R：**{s.get('平均R', 0):.2f}**",
        f"- 最大回撤：**{s.get('最大回撤', 0):,.2f} U**",
    ])


def is_self_question(text):
    return bool(__import__('re').search(
        r'(你.*模型|什么模型|用的什么模型|你怎么工作|你.*RAG|RAG.*什么|'
        r'知识库|数据来源|你会什么|你是谁|你怎么知道)',
        text or '', __import__('re').I))


def _self_info_md():
    """回答关于助手自身配置的问题，不让模型猜。"""
    try:
        qa_model = llm.model_name('analyst')
        default_model = llm.model_name()
        chair_model = llm.model_name('chair')
        planner_model = llm.model_name('planner')
    except Exception:
        qa_model = default_model = chair_model = planner_model = '读取失败'
    return '\n'.join([
        '### 当前配置',
        '',
        f'- 普通问答/解释：**{qa_model}**',
        f'- 默认模型：**{default_model}**',
        f'- 方案主持人：**{chair_model}**',
        f'- 方案官：**{planner_model}**',
        '- 模型服务商：SiliconFlow（OpenAI 兼容接口）',
        '',
        '### 数据来源',
        '',
        '- 行情：币安 / OKX / Bybit 公开接口',
        '- 新闻：项目配置的公开 RSS 源',
        '- 币安账户：余额、当前持仓、未成交订单和止盈止损（只读）',
        '- 个人数据：本机交易记录、持仓和风控设置',
        '',
        '### RAG 情况',
        '',
        (lambda st: (f"当前已启用本地公开新闻 RAG，索引 {st.get('文档数', 0)} 条。" if st.get('就绪') else '当前还没有建立公开新闻 RAG 索引。'))(rag.status()),
        '普通问答时，代码会把当前行情事实和检索到的公开新闻一起发给模型；',
        'RAG 只索引公开新闻，不读取你的交易记录、持仓或盈亏。',
    ])


def _order_type_cn(t):
    return {
        'STOP_MARKET': '止损市价', 'TAKE_PROFIT_MARKET': '止盈市价',
        'STOP': '止损限价', 'TAKE_PROFIT': '止盈限价',
        'LIMIT': '限价单', 'MARKET': '市价单',
        'TRAILING_STOP_MARKET': '追踪止损',
    }.get(str(t or ''), str(t or '订单'))


def _account_md(res):
    if res.get('错误'):
        return '读取币安账户失败：\n\n' + str(res['错误'])
    a = res.get('账户') or res
    lines = ['### 币安账户', '']
    for key in ('钱包余额', '未实现盈亏', '保证金余额', '可用余额',
                '初始保证金', '维持保证金'):
        if a.get(key) is not None:
            lines.append(f'- {key}：**{float(a.get(key) or 0):,.2f} USDT**')
    positions = res.get('持仓') or []
    lines += ['', f'### 当前持仓（{len(positions)} 个）', '']
    if not positions:
        lines.append('当前没有持仓。')
    for p in positions:
        lines.append(
            f"- **{p.get('币种')}** {p.get('方向')} {p.get('杠杆')}x｜"
            f"开仓 {float(p.get('开仓价') or 0):,.6f}｜"
            f"标记 {float(p.get('标记价') or 0):,.6f}｜"
            f"浮盈亏 {float(p.get('未实现盈亏') or 0):+,.2f} USDT｜"
            f"爆仓 {float(p.get('爆仓价') or 0):,.6f}"
        )
    orders = res.get('挂单') or []
    lines += ['', f'### 挂单 / 止盈止损（{len(orders)} 个）', '']
    if not orders:
        lines.append('当前没有未成交挂单。')
    for o in orders:
        price = float(o.get('触发价') or o.get('价格') or 0)
        lines.append(
            f"- **{o.get('币种')}** {_order_type_cn(o.get('类型'))}｜"
            f"{o.get('买卖')}｜触发/价格 {price:,.6f}｜数量 {float(o.get('数量') or 0):.6f}｜"
            f"已成交 {float(o.get('已成交') or 0):.6f}｜"
            f"{'全平仓' if o.get('全平仓单') else '普通'}"
        )
    return '\n'.join(lines)


def _pick_md(res):
    if res.get('错误'):
        return f"筛选失败：{res['错误']}"
    lines = [f"### 可观察币种", '', res.get('说明') or '', '',
             f"筛选条件：{res.get('筛选条件') or ''}", '']
    for i, row in enumerate(res.get('候选') or [], 1):
        lines.append(
            f"{i}. **{row.get('币种')}**　现价 {row.get('标记价', 0):,.6f}　"
            f"24h {row.get('24h涨跌%', 0):+.2f}%　"
            f"费率 {row.get('资金费率%', 0):+.4f}%　"
            f"成交额第 {row.get('成交额排名', '—')} 名"
        )
        reason = row.get('筛选理由')
        if reason:
            lines.append(f"   - {reason}")
    lines += ['', '这只是观察名单，不是买入推荐；每个标的仍然要等自己的入场和止损。']
    return '\n'.join(lines)


def _result_md(intent, res):
    if intent == 'plan':
        return None
    kind = res.get('类型')
    if kind in ('账户', '订单'):
        return _account_md(res)
    if kind == '候选':
        return _pick_md(res)
    if kind == '行情':
        return _quote_md(res)
    if kind == '持仓':
        return _positions_md(res)
    if kind == '扫描':
        return _scan_md(res)
    if kind == '新闻':
        return _news_md(res)
    if kind == '复盘':
        return _review_md(res)
    if kind == '绩效':
        return _dashboard_md(res)
    if kind == '对话':
        return res.get('内容') or ''
    return str(res)


def _side_md(side):
    if not side:
        return '这个方向没有算出合理参数。'
    pos = side.get('仓位') or {}
    return '\n'.join([
        f"- 入场价：**{side.get('入场价', 0):,.4f}**",
        f"- 止损价：**{side.get('止损价', 0):,.4f}**"
        f"（距入场 {side.get('止损依据', {}).get('距离百分比', 0):.2f}%）",
        f"- 止盈价：**{side.get('止盈价', 0):,.4f}**",
        f"- 建议数量：**{pos.get('建议数量', 0):,.6f}**",
        f"- 名义价值：**{pos.get('名义价值', 0):,.2f} USDT**",
        f"- 止损亏损：**{pos.get('止损时实际亏损', 0):,.2f} USDT**"
        f"（本金 {pos.get('止损时实际亏损比例', 0):.2f}%）",
        f"- 估算爆仓价：**{pos.get('爆仓价', 0):,.4f}**",
        f"- 纪律检查：**{(side.get('纪律检查') or {}).get('结论', '未检查')}**",
    ])


def _plan_md(result):
    if not result:
        return '任务完成，但结果读取失败。'
    if result.get('双向'):
        ai = result.get('AI判断') or {}
        out = [f"### {result.get('标的', '')} 双向方案", '',
               f"AI 方向判断：**{ai.get('方向', '无法判断')}**（信心 {ai.get('信心', '—')}）", '',
               ai.get('说明') or '', '']
        for key in ('做多', '做空'):
            out += [f"## {key}", '', _side_md(result.get(key)), '']
        return '\n'.join(out)
    out = [f"### {result.get('标的', '')} {result.get('方向', '')}", '',
           _side_md(result), '']
    if result.get('主要风险'):
        out += [f"主要风险：{result['主要风险']}", '']
    if result.get('可执行条件'):
        out += [f"方案失效条件：{result['可执行条件']}"]
    return '\n'.join(out)


_CN_NUM = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}


def _resolve_reference_values(text, candidates=None, last_symbol=''):
    raw = str(text or '').strip()
    candidates = list(candidates or [])
    last_symbol = str(last_symbol or '').strip()
    explicit = chat.find_symbol(raw)
    if explicit:
        text = re.sub(r'这个币|这个|它|该币|这只', explicit, raw, count=1)
        return text, explicit
    m = re.search(r'第\s*([0-9]+|[一二三四五六七八九十]+)\s*个', raw)
    if m and candidates:
        token = m.group(1)
        n = int(token) if token.isdigit() else _CN_NUM.get(token, 0)
        if 1 <= n <= len(candidates):
            sym = candidates[n - 1]
            if re.search(r'方案|开仓|分析|怎么看|能做', raw):
                return f'给我 {sym} 的开仓方案', sym
            return f'帮我分析一下 {sym}', sym
    if re.search(r'这个|这个币|它|该币|这只', raw) and last_symbol:
        return re.sub(r'这个币|这个|它|该币|这只', last_symbol, raw, count=1), last_symbol
    if re.search(r'开仓方案|生成方案', raw) and not chat.find_symbol(raw) and last_symbol:
        return f'给我 {last_symbol} 的开仓方案', last_symbol
    return raw, chat.find_symbol(raw)


def _resolve_reference(text):
    """把“第六个币/这个/它”解析成上一轮候选或币种。"""
    return _resolve_reference_values(
        text,
        candidates=cl.user_session.get('last_candidates') or [],
        last_symbol=cl.user_session.get('last_symbol') or '',
    )


async def _handle_prompt(text):
    seq = int(cl.user_session.get('request_seq') or 0) + 1
    cl.user_session.set('request_seq', seq)
    effective_text, resolved_symbol = _resolve_reference(text)
    cfg = load_config()
    intent, symbol = chat.classify(effective_text)
    symbol = symbol or resolved_symbol
    label = chat.LABELS.get(intent, '对话')
    if symbol:
        cl.user_session.set('last_symbol', symbol)

    status = cl.Message(content='正在思考…')
    await status.send()

    def stale():
        return int(cl.user_session.get('request_seq') or 0) != seq

    try:
        if is_self_question(effective_text):
            status.content = _self_info_md()
            await status.update()
            return

        async with cl.Step(name=f'识别问题：{label}', type='tool') as step:
            step.input = effective_text
            step.output = '正在处理'

        if intent == 'plan':
            sym = symbol or 'BTCUSDT'
            cl.user_session.set('last_symbol', sym)
            status.content = f'正在生成 **{sym}** 方案…'
            await status.update()
            tid = await cl.make_async(chat.run_plan)(sym, cfg)
            started = time.time()
            while True:
                await asyncio.sleep(2)
                if stale():
                    status.content = '这个任务已被新的请求替换。'
                    await status.update()
                    return
                stt = tasks.status(tid)
                state = stt.get('状态')
                if state in ('排队中', '运行中'):
                    hist = stt.get('进度历史') or []
                    lines = '\n'.join(f'- {x}' for x in hist[-10:])
                    status.content = (
                        f"正在生成 **{sym}** 方案…\n\n"
                        f"**模型团队进度**\n{lines or '- 等待启动'}\n\n"
                        f"已运行：{int(time.time() - started)} 秒"
                    )
                    await status.update()
                    continue
                if state == '失败':
                    status.content = f"方案生成失败：{stt.get('错误') or '未知错误'}"
                    await status.update()
                    return
                loaded = tasks.load_result(tid) or {}
                status.content = _plan_md(loaded.get('方案'))
                await status.update()
                return

        rag_docs = []
        rag_context = ''
        if rag.is_ready() and intent not in ('plan', 'quote', 'dashboard', 'review', 'account', 'orders'):
            rag_docs = await cl.make_async(rag.search)(
                effective_text, top_k=5, symbols=[symbol] if symbol else None)
            rag_context = rag.format_context(rag_docs)

        async with cl.Step(name='正在调用数据工具', type='tool') as step:
            step.input = effective_text
            _, result = await cl.make_async(chat.dispatch)(
                effective_text, cfg, allow_llm=True, extra_context=rag_context)
            step.output = f'完成，使用 {len(rag_docs)} 条公开资料' if rag_docs else '完成'

        if stale():
            return
        if intent == 'pick':
            cl.user_session.set('last_candidates', [
                x.get('币种') for x in (result.get('候选') or []) if x.get('币种')])
        elif intent == 'scan':
            cand = []
            for v in (result.get('分类') or {}).values():
                for row in (v.get('数据') or []):
                    if row.get('币种') and row['币种'] not in cand:
                        cand.append(row['币种'])
            cl.user_session.set('last_candidates', cand[:20])
        content = _result_md(intent, result)
        if rag_docs and intent not in ('plan', 'quote', 'dashboard', 'review', 'account', 'orders'):
            sources = rag.format_sources(rag_docs)
            if sources:
                content += '\n\n---\n**本次检索到的公开资料**\n\n' + sources
        status.content = content
        await status.update()
    except Exception as e:
        status.content = f'处理失败：`{type(e).__name__}: {e}`'
        await status.update()


@cl.on_chat_start
async def on_chat_start():
    await cl.Message(
        content=(
            f"# {APP_NAME}\n\n"
            "直接输入你想看什么。左下角也可以点快捷操作。\n\n"
            "我会给方向倾向、置信度和完整风控方案，但不保证预测一定正确。"
        ),
        actions=_actions(),
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    text = (message.content or '').strip()
    if text:
        await _handle_prompt(text)


@cl.action_callback('quick_action')
async def on_quick_action(action: cl.Action):
    prompt = (action.payload or {}).get('prompt')
    if prompt:
        await _handle_prompt(prompt)


@cl.set_starters
async def starters(user=None):
    return [cl.Starter(label=label, message=prompt)
            for label, prompt in QUICK_ACTIONS[:4]]