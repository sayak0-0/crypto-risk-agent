# -*- coding: utf-8 -*-
"""模型回测：用历史行情检验模型判断方向的准确率。

核心原则：**绝不偷看未来**。
每个采样点只喂「那一刻之前」的数据，然后用 24 小时后的真实价格判对错。

用法：
    python 模型回测.py                    # 默认 4 个模型 × 20 个采样点
    python 模型回测.py --samples 30
    python 模型回测.py --models zai-org/GLM-5.3 deepseek-ai/DeepSeek-V4-Pro
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from common import DATA_DIR, get_env

BINANCE = 'https://fapi.binance.com'
UA = {'User-Agent': 'Mozilla/5.0'}
CANDLES_PER_DAY = 6          # 4 小时一根
HOLD_CANDLES = 6             # 持有 24 小时

DEFAULT_MODELS = [
    'zai-org/GLM-5.3',
    'deepseek-ai/DeepSeek-V4-Pro',
    'deepseek-ai/DeepSeek-V3.2',
    'deepseek-ai/DeepSeek-V3',
]


def get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_history(symbol='BTCUSDT', candles=1000):
    """拉历史 K 线、资金费率、多空比。"""
    kl = get(f'{BINANCE}/fapi/v1/klines',
             {'symbol': symbol, 'interval': '4h', 'limit': candles})
    klines = [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
               'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])} for k in kl]

    start = klines[0]['t']
    try:
        fr = get(f'{BINANCE}/fapi/v1/fundingRate',
                 {'symbol': symbol, 'startTime': start, 'limit': 1000})
        funding = [(int(x['fundingTime']), float(x['fundingRate'])) for x in fr]
    except Exception:
        funding = []

    try:
        ls = get(f'{BINANCE}/futures/data/globalLongShortAccountRatio',
                 {'symbol': symbol, 'period': '4h', 'limit': 500})
        ratio = [(int(x['timestamp']), float(x['longShortRatio']),
                  float(x['longAccount'])) for x in ls]
    except Exception:
        ratio = []
    return klines, funding, ratio


def _latest_before(seq, ts):
    """取时间戳在 ts 之前的最后一条（避免偷看未来）。"""
    out = None
    for row in seq:
        if row[0] <= ts:
            out = row
        else:
            break
    return out


def build_sample(klines, funding, ratio, i):
    """构造第 i 根 K 线收盘时的「可见数据」，以及 24 小时后的结果。"""
    if i < 65 or i + HOLD_CANDLES >= len(klines):
        return None
    closes = [k['c'] for k in klines[:i + 1]]
    highs = [k['h'] for k in klines[:i + 1]]
    lows = [k['l'] for k in klines[:i + 1]]
    price = closes[-1]

    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    trs = []
    for j in range(i - 14, i + 1):
        h, l, pc = highs[j], lows[j], closes[j - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)
    chg24 = (price - closes[-1 - CANDLES_PER_DAY]) / closes[-1 - CANDLES_PER_DAY] * 100
    hi30, lo30 = max(highs[-30:]), min(lows[-30:])

    ts = klines[i]['t']
    f_row = _latest_before(funding, ts)
    r_row = _latest_before(ratio, ts)

    future_price = klines[i + HOLD_CANDLES]['c']
    return {
        '时间': time.strftime('%Y-%m-%d %H:%M', time.localtime(ts / 1000)),
        '现价': round(price, 2),
        'MA20': round(ma20, 2),
        'MA60': round(ma60, 2),
        '距MA20百分比': round((price - ma20) / ma20 * 100, 2),
        '距MA60百分比': round((price - ma60) / ma60 * 100, 2),
        'ATR百分比': round(atr / price * 100, 2),
        '24小时涨跌幅': round(chg24, 2),
        '30根K线高点': round(hi30, 2),
        '30根K线低点': round(lo30, 2),
        '资金费率百分比': round(f_row[1] * 100, 4) if f_row else None,
        '账户多空比': round(r_row[1], 2) if r_row else None,
        '_未来价格': future_price,
        '_真实涨跌幅': round((future_price - price) / price * 100, 2),
    }


PROMPT = """下面是某加密货币合约在 {时间} 收盘时的数据（你只能看到这些）：

{data}

请判断接下来 24 小时的方向倾向。

只输出 JSON，不要 markdown 代码块、不要任何其他文字：
{{"方向": "偏多 或 偏空 或 中性 或 无法判断", "信心": 0到100的整数, "理由": "一句话，引用上面数字"}}
"""


def parse_json(t):
    t = (t or '').strip()
    t2 = re.sub(r'^```(?:json)?\s*', '', t)
    t2 = re.sub(r'\s*```$', '', t2)
    for c in (t2, t[t.find('{'):t.rfind('}') + 1] if '{' in t else ''):
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


def ask(model, sample, key, url):
    data = {k: v for k, v in sample.items() if not k.startswith('_') and k != '时间'}
    body = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': '你只输出 JSON，不加任何其他文字。'},
            {'role': 'user', 'content': PROMPT.format(**{
                '时间': sample['时间'],
                'data': json.dumps(data, ensure_ascii=False, indent=1)})},
        ],
        'temperature': 0.3,
        'max_tokens': 250,
    }
    try:
        r = requests.post(url, json=body, timeout=180,
                          headers={'Authorization': 'Bearer ' + key,
                                   'Content-Type': 'application/json'})
        if r.status_code != 200:
            return {'方向': None, '错误': f'HTTP {r.status_code}'}
        obj = parse_json(r.json()['choices'][0]['message']['content']) or {}
        obj['错误'] = None
        return obj
    except Exception as e:
        return {'方向': None, '错误': type(e).__name__}


def judge(direction, change):
    """用真实涨跌判对错。中性/无法判断不参与统计。"""
    if direction == '偏多':
        return change > 0
    if direction == '偏空':
        return change < 0
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--samples', type=int, default=20)
    ap.add_argument('--models', nargs='*', default=DEFAULT_MODELS)
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--step', type=int, default=0, help='采样间隔（K线根数），默认自动')
    args = ap.parse_args()

    key = get_env('LLM_API_KEY') or get_env('SILICONFLOW_API_KEY')
    if not key:
        print('没有配置模型 API Key'); return 1
    url = (get_env('LLM_BASE_URL') or 'https://api.siliconflow.cn/v1').rstrip('/') + '/chat/completions'

    print('正在拉取历史数据……')
    klines, funding, ratio = fetch_history(args.symbol)
    print(f'  K线 {len(klines)} 根（{len(klines) * 4 / 24:.0f} 天）｜资金费率 {len(funding)} 条'
          f'｜多空比 {len(ratio)} 条')

    lo, hi = 65, len(klines) - HOLD_CANDLES - 1
    step = args.step or max(1, (hi - lo) // args.samples)
    idxs = list(range(hi, lo, -step))[:args.samples]
    idxs.reverse()
    samples = [build_sample(klines, funding, ratio, i) for i in idxs]
    samples = [s for s in samples if s]
    print(f'  采样点 {len(samples)} 个，区间 {samples[0]["时间"]} ~ {samples[-1]["时间"]}')
    print(f'  模型 {len(args.models)} 个，共 {len(samples) * len(args.models)} 次调用\n')

    jobs = [(m, s) for m in args.models for s in samples]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda a: ask(a[0], a[1], key, url), jobs))
    print(f'调用完成，耗时 {time.time() - t0:.0f} 秒\n')

    stats = {}
    for m in args.models:
        st = {'对': 0, '错': 0, '中性': 0, '失败': 0, '明细': []}
        k = 0
        for s in samples:
            a = answers[args.models.index(m) * len(samples) + k]
            k += 1
            if not a or not a.get('方向') or a.get('错误'):
                st['失败'] += 1
                continue
            v = judge(a['方向'], s['_真实涨跌幅'])
            if v is None:
                st['中性'] += 1
            elif v:
                st['对'] += 1
            else:
                st['错'] += 1
            st['明细'].append({'时间': s['时间'], '判断': a['方向'],
                               '信心': a.get('信心'),
                               '实际涨跌': s['_真实涨跌幅'], '结果': v})
        n = st['对'] + st['错']
        st['准确率'] = round(st['对'] / n * 100, 1) if n else None
        stats[m] = st

    print('=' * 84)
    print(f"{'模型':<40}{'准确率':>8}{'对':>5}{'错':>5}{'弃权':>6}{'失败':>6}")
    print('-' * 84)
    for m in sorted(args.models, key=lambda x: -(stats[x]['准确率'] or -1)):
        s = stats[m]
        acc = f"{s['准确率']}%" if s['准确率'] is not None else '—'
        print(f"{m:<40}{acc:>8}{s['对']:>5}{s['错']:>5}{s['中性']:>6}{s['失败']:>6}")

    print('\n' + '=' * 84)
    print('样本本身的市场分布（用来判断这个基准是否公平）：')
    ups = sum(1 for s in samples if s['_真实涨跌幅'] > 0)
    print(f"  24 小时后上涨 {ups} 次 / 下跌 {len(samples) - ups} 次"
          f"  -> 无脑猜多的基准是 {ups / len(samples) * 100:.1f}%")

    out = os.path.join(DATA_DIR, '模型回测结果.json')
    json.dump({'采样点': samples, '统计': stats,
               '基准_无脑猜多': ups / len(samples) * 100},
              open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n明细已存：{out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())