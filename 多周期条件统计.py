# -*- coding: utf-8 -*-
"""不同持有周期下，条件的效应有多强？（含手续费后净收益）

目的：回答「拉长周期能不能提高可用的准确率」
持有周期：24h / 3天 / 7天 / 14天 / 30天
每个条件都报「毛收益」和「扣掉往返手续费后的净收益」
"""
import json
import os
import statistics
import time

import requests

BINANCE = 'https://fapi.binance.com'
UA = {'User-Agent': 'Mozilla/5.0'}
BARS_PER_DAY = 6
BARS_PER_WEEK = 42
FEE_ROUND_TRIP = 0.10      # 单边 0.05% × 2


def get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_klines(symbol='BTCUSDT', total=3000):
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
    return [{'t': int(k[0]), 'c': float(k[4]), 'h': float(k[2]),
             'l': float(k[3])} for k in out]


HORIZONS = [('1天', 6), ('3天', 18), ('7天', 42), ('14天', 84), ('30天', 180)]


def main():
    print('拉取数据……')
    kl = fetch_klines(total=3000)
    closes = [k['c'] for k in kl]
    print(f'  {len(kl)} 根 4h K线（{len(kl)*4/24:.0f} 天）')

    base_i = 65
    max_h = max(h[1] for h in HORIZONS)
    rows = []
    for i in range(base_i, len(kl) - max_h):
        price = closes[i]
        ma20 = sum(closes[i - 19:i + 1]) / 20
        ma60 = sum(closes[i - 59:i + 1]) / 60
        chg = [(closes[j] - closes[j - 1]) / closes[j - 1] * 100
               for j in range(i - 2, i + 1)]
        ds = 0
        for c in reversed(chg):
            if c < 0:
                ds += 1
            else:
                break
        rows.append({
            'i': i, 'price': price, 'ma20': ma20, 'ma60': ma60,
            'down3': ds >= 3,
            'near_high': (price - max(k['h'] for k in kl[i - 29:i + 1])) / price * 100 > -1,
            'above_ma': price > ma20 > ma60,
            'chg24': (price - closes[i - 6]) / closes[i - 6] * 100,
        })

    CONDITIONS = {
        '连跌3根': lambda r: r['down3'],
        '贴近30根新高': lambda r: r['near_high'],
        '多头排列': lambda r: r['above_ma'],
        '24h跌超5%': lambda r: r['chg24'] < -5,
    }

    print('\n' + '=' * 112)
    print('  各持有周期下的平均收益（毛）／扣掉 0.10% 手续费后的净收益')
    print('=' * 112)
    print(f"\n  {'条件':<16}{'':<2}" + "".join(f"{h[0]:>19}" for h in HORIZONS))
    print("  " + "-" * 106)
    header = "  " + " " * 18 + "".join(f"{'毛/净':>19}" for _ in HORIZONS)
    print(header)

    out = {}
    for name, fn in CONDITIONS.items():
        line = f"  {name:<16}  "
        vals = {}
        for label, h in HORIZONS:
            rets = []
            for r in rows:
                if not fn(r):
                    continue
                j = r['i'] + h
                if j >= len(closes):
                    continue
                rets.append((closes[j] - r['price']) / r['price'] * 100)
            if len(rets) < 10:
                line += f"{'样本不足':>19}"
                vals[label] = None
                continue
            gross = statistics.mean(rets)
            net = gross - FEE_ROUND_TRIP
            ups = sum(1 for x in rets if x > 0) / len(rets) * 100
            line += f"{gross:>8.2f}%/{net:>7.2f}%"
            vals[label] = {'样本': len(rets), '毛收益': round(gross, 3),
                           '净收益': round(net, 3), '胜率': round(ups, 1)}
        print(line)
        out[name] = vals

    # 基准
    line = f"  {'基准（全样本）':<16}  "
    for label, h in HORIZONS:
        rets = [(closes[r['i'] + h] - r['price']) / r['price'] * 100
                for r in rows if r['i'] + h < len(closes)]
        gross = statistics.mean(rets)
        line += f"{gross:>8.2f}%/{gross - FEE_ROUND_TRIP:>7.2f}%"
    print("  " + "-" * 106)
    print(line)

    print('\n' + '=' * 112)
    print('  关键判断：净收益为正才算「可利用」')
    print('=' * 112)
    for name, vals in out.items():
        good = [k for k, v in vals.items() if v and v['净收益'] > 0]
        print(f"\n  【{name}】")
        for label, v in vals.items():
            if not v:
                continue
            mark = "✅" if v['净收益'] > 0 else "❌"
            print(f"    {mark} {label:<6} 毛 {v['毛收益']:+.2f}%  净 {v['净收益']:+.2f}%"
                  f"  胜率 {v['胜率']:.1f}%  样本 {v['样本']}")
        if good:
            print(f"    → 净收益为正的周期：{', '.join(good)}")
        else:
            print("    → 没有任何周期能覆盖手续费")

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', '多周期条件统计.json')
    json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n  明细已存：{path}')


if __name__ == '__main__':
    main()