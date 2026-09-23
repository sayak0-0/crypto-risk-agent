# -*- coding: utf-8 -*-
"""对「贴近30根新高」做样本外验证 + 分段滚动验证。

上一轮吃过亏：24h跌超5% 在前半段看起来是超级信号（+1.16%），后半段翻脸（-0.20%）。
所以这次必须切成多段，看这个规律是不是每一段都成立。
"""
import json
import os
import statistics
import time

import requests

BINANCE = 'https://fapi.binance.com'
UA = {'User-Agent': 'Mozilla/5.0'}
FEE = 0.10
HORIZONS = [('1天', 6), ('3天', 18), ('7天', 42), ('14天', 84)]


def get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_klines(symbol='BTCUSDT', total=4000):
    out, end = [], None
    while len(out) < total:
        need = min(1500, total - len(out))
        p = {'symbol': symbol, 'interval': '4h', 'limit': need}
        if end:
            p['endTime'] = end
        b = get(f'{BINANCE}/fapi/v1/klines', p)
        if not b:
            break
        out = b + out
        end = b[0][0] - 1
        if len(b) < need:
            break
        time.sleep(0.25)
    return [{'t': int(k[0]), 'c': float(k[4]), 'h': float(k[2])} for k in out]


def main():
    print('拉取数据（4000 根 4h ≈ 667 天）……')
    kl = fetch_klines(total=4000)
    closes = [k['c'] for k in kl]
    print(f'  实际 {len(kl)} 根，{len(kl)*4/24:.0f} 天')

    rows = []
    for i in range(65, len(kl) - max(h for _, h in HORIZONS)):
        price = closes[i]
        hi30 = max(k['h'] for k in kl[i - 29:i + 1])
        rows.append({'i': i, 't': kl[i]['t'], 'price': price,
                     'near_high': (price - hi30) / price * 100 > -1})

    # 切成 4 段做滚动验证
    n = len(rows)
    segs = []
    for s in range(4):
        a, b = n * s // 4, n * (s + 1) // 4
        segs.append(rows[a:b])

    import datetime
    def dt(ts):
        return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).strftime('%Y-%m-%d')

    print('\n  分段：')
    for i, sg in enumerate(segs):
        print(f'    第{i+1}段  {dt(sg[0]["t"])} ~ {dt(sg[-1]["t"])}   {len(sg)} 个点')

    print('\n' + '=' * 100)
    print('  「贴近30根新高」的超额收益（相对同段基准）—— 分段验证')
    print('=' * 100)
    print(f"\n  {'持有':<8}" + "".join(f"{'第'+str(i+1)+'段':>16}" for i in range(4))
          + f"{'全段':>12}{'几段为正':>10}")
    print('  ' + '-' * 88)

    summary = {}
    for label, h in HORIZONS:
        line = f"  {label:<8}"
        positives = 0
        allex = []
        for sg in segs:
            all_r = [(closes[r['i'] + h] - r['price']) / r['price'] * 100
                     for r in sg if r['i'] + h < len(closes)]
            cond_r = [(closes[r['i'] + h] - r['price']) / r['price'] * 100
                      for r in sg if r['near_high'] and r['i'] + h < len(closes)]
            if len(cond_r) < 10 or len(all_r) < 10:
                line += f"{'样本不足':>16}"
                continue
            ex = statistics.mean(cond_r) - statistics.mean(all_r)
            ex_net = ex - FEE
            if ex_net > 0:
                positives += 1
            allex.append(ex)
            mark = "✅" if ex_net > 0 else "❌"
            line += f"{ex:>+10.2f}/{ex_net:>+4.2f}{mark}"
        if allex:
            line += f"{statistics.mean(allex):>+11.2f}%{positives:>8}/4"
        print(line)
        summary[label] = {'各段超额': [round(x, 3) for x in allex],
                          '净收益为正的段数': positives}

    print('\n' + '=' * 100)
    print('  结论')
    print('=' * 100)
    for label, s in summary.items():
        if s['净收益为正的段数'] == 4:
            print(f"  ✅ {label}：4 段全部为正 —— 这个规律是稳的")
        elif s['净收益为正的段数'] >= 3:
            print(f"  ⚠️ {label}：{s['净收益为正的段数']}/4 段为正 —— 大部分时候成立")
        else:
            print(f"  ❌ {label}：只有 {s['净收益为正的段数']}/4 段为正 —— 不可靠")

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', '新高条件_分段验证.json')
    json.dump(summary, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n  明细已存：{path}')


if __name__ == '__main__':
    main()