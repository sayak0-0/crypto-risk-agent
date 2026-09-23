# -*- coding: utf-8 -*-
"""条件统计 v2：修资金费率覆盖问题 + 做样本外验证。

v1 的毛病：
  ① 资金费率只拉了 500 条（167天），覆盖不了 500 天的 K 线 → 那几行无效
  ② 只在全样本上找规律 → 就是数据挖掘，挖出来的规律多半是噪声

v2 的改进：
  ① 资金费率分页拉全
  ② 把时间切成前后两半：前一半找规律（样本内），后一半验证（样本外）
     只有「两段都能成立」的规律才值得看
"""
import json
import os
import statistics
import time

import requests

BINANCE = 'https://fapi.binance.com'
UA = {'User-Agent': 'Mozilla/5.0'}
BARS_PER_DAY = 6
HORIZON = 6


def get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_klines(symbol='BTCUSDT', total=3000):
    out, end = [], None
    while len(out) < total:
        need = min(1500, total - len(out))
        params = {'symbol': symbol, 'interval': '4h', 'limit': need}
        if end:
            params['endTime'] = end
        batch = get(f'{BINANCE}/fapi/v1/klines', params)
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        if len(batch) < need:
            break
        time.sleep(0.25)
    return [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
             'l': float(k[3]), 'c': float(k[4])} for k in out]


def fetch_funding(symbol='BTCUSDT', start_ms=None, end_ms=None):
    """分页拉资金费率（单次上限 1000）。"""
    out, cur = [], start_ms
    for _ in range(8):
        params = {'symbol': symbol, 'limit': 1000}
        if cur:
            params['startTime'] = cur
        if end_ms:
            params['endTime'] = end_ms
        try:
            batch = get(f'{BINANCE}/fapi/v1/fundingRate', params)
        except Exception:
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        cur = int(batch[-1]['fundingTime']) + 1
        time.sleep(0.25)
    seen, rows = set(), []
    for x in out:
        t = int(x['fundingTime'])
        if t not in seen:
            seen.add(t)
            rows.append((t, float(x['fundingRate'])))
    rows.sort()
    return rows


def latest_before(seq, ts):
    out = None
    for row in seq:
        if row[0] <= ts:
            out = row
        else:
            break
    return out


def stats(returns):
    if not returns:
        return None
    ups = sum(1 for r in returns if r > 0)
    mean = statistics.mean(returns)
    sd = statistics.pstdev(returns) if len(returns) > 1 else 0
    se = sd / (len(returns) ** 0.5) if returns else 0
    return {
        '样本数': len(returns),
        '平均收益%': round(mean, 3),
        '中位数%': round(statistics.median(returns), 3),
        '上涨概率%': round(ups / len(returns) * 100, 1),
        '标准差%': round(sd, 2),
        't值': round(mean / se, 2) if se > 0 else None,
    }


def main():
    print('拉取历史数据（分页）……')
    kl = fetch_klines(total=3000)
    fund = fetch_funding(start_ms=kl[0]['t'])
    print(f'  K线 {len(kl)} 根（{len(kl)*4/24:.0f} 天）')
    print(f'  资金费率 {len(fund)} 条（覆盖 {(fund[-1][0]-fund[0][0])/86400000:.0f} 天）')
    if fund:
        frs = [f[1] * 100 for f in fund]
        print(f'  费率范围 {min(frs):.4f}% ~ {max(frs):.4f}%，'
              f'中位 {statistics.median(frs):.4f}%')

    closes = [k['c'] for k in kl]
    highs = [k['h'] for k in kl]
    lows = [k['l'] for k in kl]

    rows = []
    for i in range(65, len(kl) - HORIZON):
        price = closes[i]
        fwd = (closes[i + HORIZON] - price) / price * 100
        ma20 = sum(closes[i - 19:i + 1]) / 20
        ma60 = sum(closes[i - 59:i + 1]) / 60
        trs = [max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]),
                   abs(lows[j] - closes[j - 1])) for j in range(i - 14, i + 1)]
        atr_pct = (sum(trs) / len(trs)) / price * 100
        f = latest_before(fund, kl[i]['t'])
        fr = f[1] * 100 if f else None
        chg = [(closes[j] - closes[j - 1]) / closes[j - 1] * 100
               for j in range(i - 4, i + 1)]
        ds = us = 0
        for c in reversed(chg):
            if c < 0: ds += 1
            else: break
        for c in reversed(chg):
            if c > 0: us += 1
            else: break
        rows.append({
            'i': i, 't': kl[i]['t'], 'fwd': fwd, 'price': price,
            'ma20': ma20, 'ma60': ma60, 'atr_pct': atr_pct, 'fr': fr,
            'down_streak': ds, 'up_streak': us,
            'chg24': (price - closes[i - 6]) / closes[i - 6] * 100,
            'dist_hi30': (price - max(highs[i - 29:i + 1])) / price * 100,
            'dist_lo30': (price - min(lows[i - 29:i + 1])) / price * 100,
        })

    # 用全部数据算 ATR 分位数阈值
    atrs = sorted(r['atr_pct'] for r in rows)
    atr_hi = atrs[int(len(atrs) * 0.8)]
    atr_lo = atrs[int(len(atrs) * 0.2)]
    frs_all = [r['fr'] for r in rows if r['fr'] is not None]
    fr_hi = statistics.quantiles(frs_all, n=10)[8] if len(frs_all) > 20 else 0.02
    fr_lo = statistics.quantiles(frs_all, n=10)[1] if len(frs_all) > 20 else -0.02

    CONDITIONS = {
        '基准': lambda r: True,
        '连跌 3 根以上': lambda r: r['down_streak'] >= 3,
        '连跌 5 根以上': lambda r: r['down_streak'] >= 5,
        '连涨 3 根以上': lambda r: r['up_streak'] >= 3,
        '连涨 5 根以上': lambda r: r['up_streak'] >= 5,
        '多头排列': lambda r: r['price'] > r['ma20'] > r['ma60'],
        '空头排列': lambda r: r['price'] < r['ma20'] < r['ma60'],
        '贴近30根新高': lambda r: r['dist_hi30'] > -1,
        '贴近30根新低': lambda r: r['dist_lo30'] < 1,
        '24h跌超5%': lambda r: r['chg24'] < -5,
        '24h涨超5%': lambda r: r['chg24'] > 5,
        '高波动(ATR前20%)': lambda r: r['atr_pct'] >= atr_hi,
        '低波动(ATR后20%)': lambda r: r['atr_pct'] <= atr_lo,
        f'费率高于{fr_hi:.4f}%': lambda r, h=fr_hi: r['fr'] is not None and r['fr'] >= h,
        f'费率低于{fr_lo:.4f}%': lambda r, l=fr_lo: r['fr'] is not None and r['fr'] <= l,
    }

    # 时间序列切成前后两半
    mid_t = rows[len(rows) // 2]['t']
    seg1 = [r for r in rows if r['t'] < mid_t]
    seg2 = [r for r in rows if r['t'] >= mid_t]

    def pct(v):
        import datetime
        return datetime.datetime.utcfromtimestamp(v / 1000).strftime('%Y-%m-%d')

    print(f'\n  样本内（前半段）：{pct(seg1[0]["t"])} ~ {pct(seg1[-1]["t"])}，{len(seg1)} 个点')
    print(f'  样本外（后半段）：{pct(seg2[0]["t"])} ~ {pct(seg2[-1]["t"])}，{len(seg2)} 个点')

    print('\n' + '=' * 104)
    print(f"  {'条件':<24}{'前半样本':>9}{'前半胜率':>9}{'前半均值':>9}"
          f"{'后半样本':>9}{'后半胜率':>9}{'后半均值':>9}   两段一致?")
    print('  ' + '-' * 100)

    out = {}
    for name, fn in CONDITIONS.items():
        a = stats([r['fwd'] for r in seg1 if fn(r)])
        b = stats([r['fwd'] for r in seg2 if fn(r)])
        out[name] = {'前半': a, '后半': b}
        if not a or not b:
            print(f"  {name:<24}{'样本不足':>9}")
            continue
        # 只有「两段方向一致且都偏离基准」才值得标记
        same = (a['平均收益%'] > 0) == (b['平均收益%'] > 0)
        flag = '✅ 方向一致' if same else '❌ 方向相反'
        if name == '基准':
            flag = ''
        print(f"  {name:<24}{a['样本数']:>9}{a['上涨概率%']:>8.1f}%"
              f"{a['平均收益%']:>8.2f}%{b['样本数']:>9}{b['上涨概率%']:>8.1f}%"
              f"{b['平均收益%']:>8.2f}%   {flag}")

    print('\n' + '=' * 104)
    print('  怎么读')
    print('=' * 104)
    print('''
  · 重点是最后一列：**前一半发现的规律，后一半还成立吗？**
  · 「方向相反」的条件 = 数据挖掘出来的噪声，不可信
  · 「方向一致」但样本 < 50 = 样本太少，还是不能信
  · 即使方向一致，还要扣掉手续费（单边 0.05%，往返 0.1%）
  · 这只用了 500 天、一个币种、一个时间段。真正的验证要跨越牛熊多个周期
''')

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', '条件统计_样本外.json')
    json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'  明细已存：{path}')


if __name__ == '__main__':
    main()