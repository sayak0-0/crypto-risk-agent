# -*- coding: utf-8 -*-
"""行情参考数据：资金费率、持仓量、多空比、K线衍生指标。

数据全部来自交易所公开接口，不需要 API Key，也不会下任何单。
注意：这里只提供「事实描述」，不提供买卖信号，更不预测价格。
"""
import time
import requests

from common import get_proxy

TIMEOUT = 8
UA = {'User-Agent': 'Mozilla/5.0 (trading-journal-agent)'}


# ---------------- 代码归一化 ----------------

def normalize_symbol(symbol):
    """把 BTC / btcusdt / BTC-USDT 统一成 BTCUSDT。"""
    s = str(symbol or '').upper().strip()
    s = s.replace('-', '').replace('/', '').replace('_', '').replace(' ', '')
    if not s:
        return ''
    if s.endswith('SWAP'):
        s = s[:-4]
    if s.endswith('PERP'):
        s = s[:-4]
    if not s.endswith('USDT') and not s.endswith('USDC'):
        if s.endswith('USD'):
            s = s[:-3] + 'USDT'
        else:
            s += 'USDT'
    return s


def _okx_inst(symbol):
    """BTCUSDT -> BTC-USDT-SWAP"""
    s = normalize_symbol(symbol)
    if s.endswith('USDT'):
        return f'{s[:-4]}-USDT-SWAP'
    if s.endswith('USDC'):
        return f'{s[:-4]}-USDC-SWAP'
    return s


def _get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT, proxies=get_proxy())
    r.raise_for_status()
    return r.json()


# ---------------- 各交易所 ----------------

def fetch_binance(symbol):
    """币安 USDT 本位合约公开数据。"""
    sym = normalize_symbol(symbol)
    out = {'交易所': '币安', '币种': sym, '原始数据': {}}

    prem = _get('https://fapi.binance.com/fapi/v1/premiumIndex', {'symbol': sym})
    out['标记价'] = float(prem['markPrice'])
    out['资金费率'] = float(prem['lastFundingRate'])
    out['下次资金费时间'] = int(prem.get('nextFundingTime', 0))
    out['原始数据']['premiumIndex'] = prem

    try:
        t24 = _get('https://fapi.binance.com/fapi/v1/ticker/24hr', {'symbol': sym})
        out['24h涨跌幅'] = float(t24['priceChangePercent'])
        out['24h成交额'] = float(t24['quoteVolume'])
        out['24h最高'] = float(t24['highPrice'])
        out['24h最低'] = float(t24['lowPrice'])
        out['原始数据']['ticker24h'] = t24
    except Exception as e:
        out['24h涨跌幅'] = None
        out['警告_24h'] = str(e)

    try:
        oi = _get('https://fapi.binance.com/fapi/v1/openInterest', {'symbol': sym})
        out['持仓量'] = float(oi['openInterest'])
        out['原始数据']['openInterest'] = oi
    except Exception as e:
        out['持仓量'] = None
        out['警告_持仓量'] = str(e)

    try:
        ls = _get('https://fapi.binance.com/futures/data/globalLongShortAccountRatio',
                  {'symbol': sym, 'period': '1h', 'limit': 1})
        if ls:
            out['多空比'] = float(ls[-1]['longShortRatio'])
            out['多头账户占比'] = float(ls[-1]['longAccount'])
            out['原始数据']['longShortRatio'] = ls
    except Exception as e:
        out['多空比'] = None
        out['警告_多空比'] = str(e)

    try:
        kl = _get('https://fapi.binance.com/fapi/v1/klines',
                  {'symbol': sym, 'interval': '4h', 'limit': 120})
        out['K线'] = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                       float(k[4]), float(k[5])] for k in kl]
    except Exception as e:
        out['K线'] = []
        out['警告_K线'] = str(e)
    return out


def fetch_okx(symbol):
    """欧易公开数据。"""
    inst = _okx_inst(symbol)
    out = {'交易所': '欧易OKX', '币种': normalize_symbol(symbol), '原始数据': {}}

    fr = _get('https://www.okx.com/api/v5/public/funding-rate', {'instId': inst})
    d = fr['data'][0]
    out['资金费率'] = float(d['fundingRate'])
    out['下次资金费时间'] = int(d.get('fundingTime', 0))
    out['原始数据']['fundingRate'] = d

    try:
        tk = _get('https://www.okx.com/api/v5/market/ticker', {'instId': inst})
        t = tk['data'][0]
        out['标记价'] = float(t['last'])
        out['24h最高'] = float(t['high24h'])
        out['24h最低'] = float(t['low24h'])
        out['24h成交额'] = float(t['volCcy24h'])
        last = float(t['last']); open24 = float(t['open24h'])
        out['24h涨跌幅'] = (last - open24) / open24 * 100 if open24 else None
        out['原始数据']['ticker'] = t
    except Exception as e:
        out['标记价'] = None
        out['警告_行情'] = str(e)

    try:
        oi = _get('https://www.okx.com/api/v5/public/open-interest',
                  {'instType': 'SWAP', 'instId': inst})
        out['持仓量'] = float(oi['data'][0]['oi'])
        out['原始数据']['openInterest'] = oi['data'][0]
    except Exception as e:
        out['持仓量'] = None
        out['警告_持仓量'] = str(e)

    try:
        kl = _get('https://www.okx.com/api/v5/market/candles',
                  {'instId': inst, 'bar': '4H', 'limit': 120})
        rows = list(reversed(kl['data']))
        out['K线'] = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                       float(r[4]), float(r[5])] for r in rows]
    except Exception as e:
        out['K线'] = []
        out['警告_K线'] = str(e)
    return out


def fetch_bybit(symbol):
    """Bybit 公开数据。"""
    sym = normalize_symbol(symbol)
    out = {'交易所': 'Bybit', '币种': sym, '原始数据': {}}
    tk = _get('https://api.bybit.com/v5/market/tickers',
              {'category': 'linear', 'symbol': sym})
    lst = (tk.get('result') or {}).get('list') or []
    if not lst:
        raise ValueError(f'Bybit 没有找到交易对 {sym}')
    t = lst[0]
    out['标记价'] = float(t['lastPrice'])
    out['24h涨跌幅'] = float(t['price24hPcnt']) * 100
    out['持仓量'] = float(t['openInterest']) if t.get('openInterest') else None
    out['资金费率'] = float(t['fundingRate']) if t.get('fundingRate') else None
    out['24h最高'] = float(t['highPrice24h']) if t.get('highPrice24h') else None
    out['24h最低'] = float(t['lowPrice24h']) if t.get('lowPrice24h') else None
    out['24h成交额'] = float(t['turnover24h']) if t.get('turnover24h') else None
    out['原始数据']['tickers'] = t

    try:
        kl = _get('https://api.bybit.com/v5/market/kline',
                  {'category': 'linear', 'symbol': sym, 'interval': '240', 'limit': 120})
        rows = list(reversed((kl.get('result') or {}).get('list') or []))
        out['K线'] = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                       float(r[4]), float(r[5])] for r in rows]
    except Exception as e:
        out['K线'] = []
        out['警告_K线'] = str(e)
    return out


FETCHERS = {'binance': fetch_binance, 'okx': fetch_okx, 'bybit': fetch_bybit}


def snapshot(symbol, exchange='自动'):
    """拉一份行情快照。自动模式会依次尝试三个交易所。"""
    order = ['binance', 'okx', 'bybit']
    if exchange and exchange != '自动':
        key = {'币安': 'binance', '欧易OKX': 'okx', 'Bybit': 'bybit'}.get(exchange, exchange)
        order = [key] + [k for k in order if k != key]

    errors = []
    for name in order:
        fn = FETCHERS.get(name)
        if not fn:
            continue
        try:
            snap = fn(symbol)
            if snap.get('标记价'):
                snap['数据源'] = name
                snap['获取时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
                snap['警告'] = errors
                return snap
            errors.append(f'{name}: 没有拿到价格')
        except Exception as e:
            errors.append(f'{name}: {e}')
    raise RuntimeError('所有数据源都失败了，请检查网络。详情：' + ' | '.join(errors))


# ---------------- K 线衍生指标 ----------------

def compute_indicators(klines, price=None):
    """从 K 线算均线、ATR、近期高低点距离。"""
    if not klines or len(klines) < 5:
        return {}
    closes = [k[4] for k in klines]
    highs = [k[2] for k in klines]
    lows = [k[3] for k in klines]
    price = price or closes[-1]

    out = {'价格': price}
    for n in (20, 60):
        if len(closes) >= n:
            out[f'MA{n}'] = sum(closes[-n:]) / n

    if 'MA20' in out:
        out['距MA20百分比'] = (price - out['MA20']) / out['MA20'] * 100
    if 'MA60' in out:
        out['距MA60百分比'] = (price - out['MA60']) / out['MA60'] * 100

    if len(klines) >= 15:
        trs = []
        for i in range(1, len(klines)):
            h, l, pc = highs[i], lows[i], closes[i - 1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        out['ATR14'] = sum(trs[-14:]) / 14
        out['ATR14百分比'] = out['ATR14'] / price * 100

    window = min(30, len(highs))
    hi = max(highs[-window:])
    lo = min(lows[-window:])
    out['近期高点'] = hi
    out['近期低点'] = lo
    out['距近期高点百分比'] = (price - hi) / hi * 100
    out['距近期低点百分比'] = (price - lo) / lo * 100

    recent = closes[-6:]
    out['近24小时涨跌幅'] = (recent[-1] - recent[0]) / recent[0] * 100 if len(recent) >= 2 else None
    return out


def market_notes(snap, ind=None):
    """把行情数据翻译成大白话的「事实描述」，不是买卖建议。"""
    notes = []
    fr = snap.get('资金费率')
    if fr is not None:
        f = fr * 100
        if fr > 0.0005:
            notes.append(f'资金费率 {f:.4f}%（偏正且较高）：做多的人要付钱给做空的人，'
                         '说明多头比较拥挤，追多的成本偏高，容易出现多杀多。')
        elif fr > 0:
            notes.append(f'资金费率 {f:.4f}%（小幅为正）：多头略占优，做多需支付资金费。')
        elif fr < -0.0005:
            notes.append(f'资金费率 {f:.4f}%（偏负）：做空的人要付钱给做多的人，'
                         '空头比较拥挤，这时候逼空（价格快速上冲）的风险上升。')
        else:
            notes.append(f'资金费率 {f:.4f}%：接近中性。')

    lr = snap.get('多空比')
    if lr is not None:
        if lr > 1.5:
            notes.append(f'账户多空比 {lr:.2f}：散户做多的明显更多。散户一致性高的时候，'
                         '往往是被反向收割的一方。')
        elif lr < 0.7:
            notes.append(f'账户多空比 {lr:.2f}：散户做空的明显更多。')
        else:
            notes.append(f'账户多空比 {lr:.2f}：多空相对均衡。')

    if ind:
        atr = ind.get('ATR14百分比')
        if atr:
            notes.append(f'4小时 ATR 约 {atr:.2f}%：这是当前的平均波动幅度。'
                         f'如果你的止损距离小于这个数，很容易被正常波动扫掉。')
        if ind.get('距近期高点百分比') is not None and ind['距近期高点百分比'] > -0.5:
            notes.append(f'价格距 30 根 K 线的高点只有 {abs(ind["距近期高点百分比"]):.2f}%，'
                         '处在近期高位。在这里追多，止损通常要放得比较远。')
        if ind.get('距近期低点百分比') is not None and ind['距近期低点百分比'] < 0.5:
            notes.append(f'价格距 30 根 K 线的低点只有 {ind["距近期低点百分比"]:.2f}%，'
                         '处在近期低位。在这里追空，止损同样要放远。')
    return notes