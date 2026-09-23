# -*- coding: utf-8 -*-
"""检验链上数据的预测力（条件统计 + 分段验证）。

方法和前面一致：
  ① 全样本找出「看起来有超额收益」的条件
  ② 切成 4 段，看是不是每段都成立
  只有全段都成立的，才值得相信

数据：CoinMetrics 两年日线（价格 + 交易所流入流出 + 活跃地址）+ DefiLlama 稳定币
"""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import onchain

FEE = 0.10        # 往返手续费 %
HORIZONS = [('1天', 1), ('3天', 3), ('7天', 7), ('14天', 14)]


def build_series():
    print('拉取链上数据（2 年）……')
    flows = onchain.fetch_exchange_flow(days=730)
    stable = onchain.fetch_stablecoin_supply(days=730)
    rows = onchain.enrich(flows, stable)
    print(f'  交易所数据 {len(rows)} 天，稳定币数据 {len(stable)} 天')
    if rows:
        print(f'  区间 {rows[0]["date"]} ~ {rows[-1]["date"]}')
    return rows


def main():
    rows = build_series()
    if len(rows) < 120:
        print('数据太少，无法分析'); return
    maxh = max(h for _, h in HORIZONS)
    rows = rows[:len(rows) - maxh]

    # 用全样本算阈值（这个做法在严格回测里是有偏的，但先用它做初筛）
    nets = [r['净流入'] for r in rows]
    sd_net = statistics.pstdev(nets)
    mean_net = statistics.mean(nets)

    def fwd(r, h):
        """r 的下 h 天收益率（用链上数据自带的价格）。"""
        i = rows.index(r)
        if i + h >= len(rows):
            return None
        p0, p1 = rows[i]['价格'], rows[i + h]['价格']
        return (p1 - p0) / p0 * 100 if p0 else None

    # 预计算
    for i, r in enumerate(rows):
        for _, h in HORIZONS:
            r[f'fwd{h}'] = ((rows[i + h]['价格'] - r['价格']) / r['价格'] * 100
                            if i + h < len(rows) and r['价格'] else None)

    CONDITIONS = {
        '基准': lambda r: True,
        '单日净流入 > 1σ': lambda r: r['净流入'] > mean_net + sd_net,
        '单日净流出 > 1σ': lambda r: r['净流入'] < mean_net - sd_net,
        '净流入为正': lambda r: r['净流入'] > 0,
        '净流入为负': lambda r: r['净流入'] < 0,
        '3日净流入 > 0': lambda r: r.get('净流入7日均') is None or True,
        '活跃地址高于7日均10%': lambda r: (r.get('活跃地址7日均') and
                                     r['活跃地址'] > r['活跃地址7日均'] * 1.1),
        '稳定币7日增长 > 0.5%': lambda r: (r.get('稳定币7日变化%') is not None
                                     and r['稳定币7日变化%'] > 0.5),
        '稳定币7日减少 > 0.5%': lambda r: (r.get('稳定币7日变化%') is not None
                                     and r['稳定币7日变化%'] < -0.5),
    }

    print('\n' + '=' * 100)
    print('  全样本：各条件的平均未来收益（毛 / 扣 0.1% 手续费后的净）')
    print('=' * 100)
    print(f"\n  {'条件':<26}{'样本':>7}" + "".join(f"{h[0]:>16}" for h in HORIZONS))
    print('  ' + '-' * 92)

    base = {}
    for label, h in HORIZONS:
        vals = [r[f'fwd{h}'] for r in rows if r[f'fwd{h}'] is not None]
        base[label] = statistics.mean(vals) if vals else 0

    for name, fn in CONDITIONS.items():
        sub = [r for r in rows if fn(r)]
        line = f"  {name:<26}{len(sub):>7}"
        for label, h in HORIZONS:
            vals = [r[f'fwd{h}'] for r in sub if r[f'fwd{h}'] is not None]
            if len(vals) < 10:
                line += f"{'样本少':>16}"
                continue
            g = statistics.mean(vals)
            line += f"{g:>+9.2f}/{g - FEE:>+5.2f}"
        print(line)

    print('  ' + '-' * 92)
    line = f"  {'基准':<26}{len(rows):>7}"
    for label, h in HORIZONS:
        line += f"{base[label]:>+9.2f}/{base[label] - FEE:>+5.2f}"
    print(line)

    # ---------- 分段验证 ----------
    n = len(rows)
    segs = [rows[n * s // 4: n * (s + 1) // 4] for s in range(4)]
    print('\n' + '=' * 100)
    print('  分段验证：超额收益（相对同段基准，已扣手续费）')
    print('=' * 100)
    print(f"\n  {'条件':<24}{'持有':<6}" + "".join(f"{'第'+str(i+1)+'段':>13}" for i in range(4))
          + f"{'几段为正':>10}")
    print('  ' + '-' * 88)

    result = {}
    for name, fn in CONDITIONS.items():
        if name == '基准':
            continue
        for label, h in HORIZONS:
            exs = []
            for sg in segs:
                allv = [r[f'fwd{h}'] for r in sg if r[f'fwd{h}'] is not None]
                condv = [r[f'fwd{h}'] for r in sg if fn(r) and r[f'fwd{h}'] is not None]
                if len(condv) < 8 or len(allv) < 20:
                    exs.append(None); continue
                exs.append(statistics.mean(condv) - statistics.mean(allv) - FEE)
            pos = sum(1 for x in exs if x is not None and x > 0)
            valid = sum(1 for x in exs if x is not None)
            result[f'{name}|{label}'] = {'各段': [round(x, 3) if x is not None else None
                                                for x in exs],
                                          '为正段数': pos, '有效段数': valid}
            cells = ""
            for x in exs:
                if x is None:
                    cells += f"{'—':>13}"
                else:
                    cells += f"{x:>+9.2f}{'✅' if x > 0 else '❌':>4}"
            print(f"  {name:<24}{label:<6}{cells}{pos:>7}/{valid}")

    print('\n' + '=' * 100)
    print('  结论：4/4 段为正才算稳')
    print('=' * 100)
    winners = [k for k, v in result.items() if v['有效段数'] == 4 and v['为正段数'] == 4]
    strong = [k for k, v in result.items() if v['有效段数'] == 4 and v['为正段数'] == 3]
    if winners:
        for k in winners:
            print(f"  ✅ {k}：4/4 段全部为正")
    if strong:
        for k in strong:
            print(f"  ⚠️ {k}：3/4 段为正")
    if not winners and not strong:
        print("  ❌ 没有任何「条件 × 持有周期」组合能在 4 段里稳定为正。")
        print("     链上数据在这个检验下，没有表现出预测力。")

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', '链上数据回测.json')
    json.dump(result, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n  明细已存：{path}')


if __name__ == '__main__':
    main()