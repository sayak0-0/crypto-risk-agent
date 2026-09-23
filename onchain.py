# -*- coding: utf-8 -*-
"""链上数据接入：交易所资金流、稳定币供应、活跃地址。

数据源（全部免费、无需 Key）：
  · CoinMetrics 社区版 —— 交易所流入流出、活跃地址、流通量
  · DefiLlama        —— 稳定币总供应量（新钱进场的先行指标）

⚠️ 关于预测力：链上数据常被说成"先行指标"，但机构也在用同样的公开数据。
到底有没有用，必须用条件统计 + 分段验证去检验，不能靠感觉。
本模块只负责「取数」，检验方法在 链上数据回测.py 里。
"""
import time
from datetime import datetime, timedelta, timezone

import requests

from common import get_proxy

UA = {'User-Agent': 'Mozilla/5.0'}
COINMETRICS = 'https://community-api.coinmetrics.io/v4/timeseries/asset-metrics'
DEFILLAMA_STABLE = 'https://stablecoins.llama.fi/stablecoincharts/all'


def _get(url, params=None, timeout=25):
    r = requests.get(url, params=params, headers=UA, timeout=timeout, proxies=get_proxy())
    r.raise_for_status()
    return r.json()


def fetch_exchange_flow(asset='btc', days=365):
    """交易所流入/流出（美元）。返回按日期排序的列表。

    净流入 = 流入 - 流出
      净流入为正 → 更多币被送进交易所 → 通常理解为潜在抛压
      净流入为负 → 币被提走（冷钱包）→ 通常理解为囤币
    """
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')
    out, page = [], 1
    # CoinMetrics 单次最多 10000 条，一天一条，一年一次就够
    data = _get(COINMETRICS, {
        'assets': asset,
        'metrics': 'FlowInExUSD,FlowOutExUSD,AdrActCnt,SplyCur,PriceUSD',
        'frequency': '1d',
        'start_time': start,
        'page_size': 10000,
    })
    for row in data.get('data', []):
        try:
            fin = float(row.get('FlowInExUSD') or 0)
            fout = float(row.get('FlowOutExUSD') or 0)
            out.append({
                'date': row['time'][:10],
                'ts': int(datetime.strptime(row['time'][:10], '%Y-%m-%d')
                          .replace(tzinfo=timezone.utc).timestamp() * 1000),
                '流入': fin,
                '流出': fout,
                '净流入': fin - fout,
                '活跃地址': float(row.get('AdrActCnt') or 0),
                '流通量': float(row.get('SplyCur') or 0),
                '价格': float(row.get('PriceUSD') or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x['date'])
    return out


def fetch_stablecoin_supply(days=365):
    """稳定币总供应量。增长 = 场外新钱在进场。"""
    d = _get(DEFILLAMA_STABLE)
    if not isinstance(d, list):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    out = []
    for row in d:
        try:
            ts = int(row['date'])
            if ts < cutoff:
                continue
            total = row['totalCirculating']
            usd = float(total.get('peggedUSD') or 0)
            if usd <= 0:
                continue
            out.append({'date': datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d'),
                        'ts': ts * 1000, '稳定币总量': usd})
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x['date'])
    return out


def enrich(flows, stable=None):
    """给原始数据加上衍生指标。"""
    if not flows:
        return []
    st_map = {s['date']: s['稳定币总量'] for s in (stable or [])}
    out = []
    for i, r in enumerate(flows):
        e = dict(r)
        # 净流入占市值比例（比绝对值更有可比性）
        if r['流通量'] and r['价格']:
            mcap = r['流通量'] * r['价格']
            e['净流入占市值万分之'] = round(r['净流入'] / mcap * 10000, 3) if mcap else None
        # 7 日均值（平滑掉单日噪声）
        if i >= 6:
            win = flows[i - 6:i + 1]
            e['净流入7日均'] = sum(x['净流入'] for x in win) / 7
            e['活跃地址7日均'] = sum(x['活跃地址'] for x in win) / 7
        # 稳定币 7 日变化率
        if r['date'] in st_map and i >= 6:
            d0 = flows[i - 6]['date']
            if d0 in st_map and st_map[d0]:
                e['稳定币7日变化%'] = round(
                    (st_map[r['date']] - st_map[d0]) / st_map[d0] * 100, 4)
        out.append(e)
    return out


def snapshot():
    """给界面用的一句话快照。"""
    try:
        flows = enrich(fetch_exchange_flow(days=30), fetch_stablecoin_supply(days=30))
    except Exception as e:
        return {'错误': f'{type(e).__name__}: {e}'}
    if not flows:
        return {'错误': '拿不到链上数据'}
    last = flows[-1]
    prev7 = flows[-8] if len(flows) >= 8 else flows[0]
    stable_now = next((f.get('稳定币7日变化%') for f in reversed(flows)
                       if f.get('稳定币7日变化%') is not None), None)
    net7 = last.get('净流入7日均')
    notes = []
    if net7 is not None:
        if net7 < 0:
            notes.append(f'交易所 7 日平均净流出 {abs(net7)/1e8:.2f} 亿美元 —— 币在被提走（偏囤币）')
        else:
            notes.append(f'交易所 7 日平均净流入 {net7/1e8:.2f} 亿美元 —— 有潜在抛压')
    if stable_now is not None:
        if stable_now > 0.5:
            notes.append(f'稳定币供应 7 日增长 {stable_now:.2f}% —— 场外新钱在进场')
        elif stable_now < -0.5:
            notes.append(f'稳定币供应 7 日减少 {abs(stable_now):.2f}% —— 资金在离场')
        else:
            notes.append(f'稳定币供应 7 日变化 {stable_now:+.2f}% —— 基本持平')
    return {
        '日期': last['date'],
        '当日流入_亿': round(last['流入'] / 1e8, 2),
        '当日流出_亿': round(last['流出'] / 1e8, 2),
        '当日净流入_亿': round(last['净流入'] / 1e8, 2),
        '7日均净流入_亿': round(net7 / 1e8, 2) if net7 is not None else None,
        '活跃地址': int(last['活跃地址']),
        '稳定币7日变化%': stable_now,
        '解读': notes,
        '历史': flows[-30:],
    }