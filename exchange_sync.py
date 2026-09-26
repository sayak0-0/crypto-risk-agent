# -*- coding: utf-8 -*-
"""交易所持仓同步：只读你的持仓，用于自动盯盘和自动记账。

⚠️ 安全说明
本项目只调用「查询持仓」接口，不调用任何下单、平仓、划转、提现接口。
请在交易所创建 API Key 时：
    ✅ 只勾选「读取」
    ❌ 不要勾选「提现」
    ❌ 不要勾选「合约交易 / 交易」
    （可选）绑定 IP 白名单更安全

Key 存在 .env 里，.env 已经被 .gitignore 排除，永远不会进 git。
本模块的任何报错信息都不会包含你的 Key 或 Secret。
"""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from common import get_proxy, get_env

TIMEOUT = 15
UA = {'User-Agent': 'trading-journal-agent'}

# .env 里需要的变量名
ENV_KEYS = {
    '币安': ('BINANCE_API_KEY', 'BINANCE_API_SECRET', None),
    '欧易OKX': ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_PASSPHRASE'),
    'Bybit': ('BYBIT_API_KEY', 'BYBIT_API_SECRET', None),
}


def mask(s, keep=4):
    """把密钥打码，用于日志显示（永远不要打印完整 Key）。"""
    if not s:
        return '（未配置）'
    s = str(s)
    if len(s) <= keep * 2:
        return '*' * len(s)
    return f'{s[:keep]}…{s[-keep:]}'


def has_credentials(exchange):
    """检查这个交易所的 Key 是否配好了。"""
    keys = ENV_KEYS.get(exchange)
    if not keys:
        return False, '不支持的交易所'
    missing = [k for k in keys if k and not get_env(k)]
    if missing:
        return False, f'缺少配置：{"、".join(missing)}'
    return True, '已配置'


def _hmac_hex(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _hmac_b64(secret, msg):
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def _request_ip_hint(r):
    """从交易所报错里抠出「它看到的你的 IP」。

    币安 IP 白名单不匹配时会回：
      {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action, request ip: 1.2.3.4"}
    这个 request ip 就是它实际看到的地址 —— 直接拿去填白名单就行，
    省得自己猜是梯子的 IP 还是家宽的 IP。
    """
    try:
        import re
        body = r.json()
    except Exception:
        return None
    msg = ''
    if isinstance(body, dict):
        msg = str(body.get('msg') or body.get('message') or body.get('retMsg') or '')
    m = re.search(r'request ip[:\s]+([0-9a-fA-F:.]+)', msg)
    if m:
        return m.group(1).strip(' .,')
    return None


def _json_or_error(r, exchange):
    """统一处理返回，出错时给出人能看懂的话，且不泄露密钥。"""
    if r.status_code in (401, 403):
        hint = _request_ip_hint(r)
        extra = ''
        if hint:
            extra = ('\n\n💡 交易所看到的你的 IP 是：'
                     f'\n\n    {hint}\n\n'
                     '去把这个 IP 加进 IP 白名单就能通了。'
                     '（如果你挂了代理/VPN，这就是出口 IP）')
        raise PermissionError(
            f'{exchange} 拒绝了请求（HTTP {r.status_code}）。'
            '常见原因：Key 或 Secret 填错、Key 没有「读取」权限、'
            '没开合约权限、或 IP 不在白名单里。' + extra)
    if r.status_code == 429:
        raise RuntimeError(f'{exchange} 请求过于频繁（HTTP 429），等一会儿再试。')
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f'{exchange} 返回了非 JSON 内容（HTTP {r.status_code}）')
    if r.status_code >= 400:
        msg = data.get('msg') or data.get('message') or data.get('retMsg') or str(data)[:200]
        code = data.get('code') or data.get('retCode')
        raise RuntimeError(f'{exchange} 报错（{r.status_code} / {code}）：{msg}')
    return data


def _num(x, default=0.0):
    try:
        v = float(x)
        return default if v != v else v      # NaN 检查
    except (TypeError, ValueError):
        return default


# ---------------- 币安 USDT 本位合约 ----------------

def binance_positions(api_key=None, api_secret=None):
    api_key = api_key or get_env('BINANCE_API_KEY')
    api_secret = api_secret or get_env('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        raise RuntimeError('没有配置 BINANCE_API_KEY / BINANCE_API_SECRET')

    qs = urlencode({'timestamp': int(time.time() * 1000), 'recvWindow': 5000})
    sig = _hmac_hex(api_secret, qs)
    r = requests.get(f'https://fapi.binance.com/fapi/v2/positionRisk?{qs}&signature={sig}',
                     headers={'X-MBX-APIKEY': api_key, **UA}, timeout=TIMEOUT, proxies=get_proxy())
    data = _json_or_error(r, '币安')
    if isinstance(data, dict):
        data = [data]

    out = []
    for p in data:
        amt = _num(p.get('positionAmt'))
        if amt == 0:
            continue
        out.append({
            '交易所': '币安',
            '币种': p.get('symbol'),
            '方向': '多' if amt > 0 else '空',
            '数量': abs(amt),
            '开仓价': _num(p.get('entryPrice')),
            '标记价': _num(p.get('markPrice')),
            '杠杆': _num(p.get('leverage'), 1),
            '未实现盈亏': _num(p.get('unRealizedProfit')),
            '爆仓价': _num(p.get('liquidationPrice')),
            '保证金': _num(p.get('isolatedMargin')) or _num(p.get('positionInitialMargin')),
        })
    return out


def _binance_signed_get(path, params=None, api_key=None, api_secret=None):
    api_key = api_key or get_env('BINANCE_API_KEY')
    api_secret = api_secret or get_env('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        raise RuntimeError('没有配置 BINANCE_API_KEY / BINANCE_API_SECRET')
    payload = dict(params or {})
    payload.update({'timestamp': int(time.time() * 1000), 'recvWindow': 5000})
    qs = urlencode(payload)
    sig = _hmac_hex(api_secret, qs)
    r = requests.get(f'https://fapi.binance.com{path}?{qs}&signature={sig}',
                     headers={'X-MBX-APIKEY': api_key, **UA},
                     timeout=TIMEOUT, proxies=get_proxy())
    return _json_or_error(r, '币安')


def binance_account(api_key=None, api_secret=None):
    """读取币安 U 本位合约账户余额（只读）。"""
    d = _binance_signed_get('/fapi/v2/account', api_key=api_key,
                            api_secret=api_secret)
    assets = []
    for a in d.get('assets') or []:
        wallet = _num(a.get('walletBalance'))
        available = _num(a.get('availableBalance'))
        unrealized = _num(a.get('unrealizedProfit'))
        margin = _num(a.get('marginBalance'))
        if wallet or available or unrealized or margin:
            assets.append({
                '资产': a.get('asset'), '钱包余额': wallet,
                '可用余额': available, '未实现盈亏': unrealized,
                '保证金余额': margin,
            })
    return {
        '交易所': '币安',
        '钱包余额': _num(d.get('totalWalletBalance')),
        '未实现盈亏': _num(d.get('totalUnrealizedProfit')),
        '保证金余额': _num(d.get('totalMarginBalance')),
        '可用余额': _num(d.get('availableBalance')),
        '初始保证金': _num(d.get('totalInitialMargin')),
        '维持保证金': _num(d.get('totalMaintMargin')),
        '可提余额': _num(d.get('availableBalance')),
        '资产': assets,
        '更新时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def binance_open_orders(symbol=None, api_key=None, api_secret=None):
    """读取币安 U 本位未成交订单，包括止盈止损条件单（只读）。"""
    params = {'symbol': symbol} if symbol else None
    data = _binance_signed_get('/fapi/v1/openOrders', params=params,
                               api_key=api_key, api_secret=api_secret)
    if isinstance(data, dict):
        data = [data]
    out = []
    for o in data or []:
        out.append({
            '订单ID': o.get('orderId'),
            '币种': o.get('symbol'),
            '买卖': o.get('side'),
            '仓位方向': o.get('positionSide'),
            '类型': o.get('type'),
            '状态': o.get('status'),
            '价格': _num(o.get('price')),
            '触发价': _num(o.get('stopPrice')),
            '数量': _num(o.get('origQty')),
            '已成交': _num(o.get('executedQty')),
            '只减仓': bool(o.get('reduceOnly')),
            '全平仓单': bool(o.get('closePosition')),
            '创建时间': (datetime.fromtimestamp(o['time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                         if o.get('time') else None),
            '更新时间': (datetime.fromtimestamp(o['updateTime'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                         if o.get('updateTime') else None),
        })
    return out


def binance_summary():
    """一次读取账户余额、当前持仓和未成交订单。"""
    return {
        '账户': binance_account(),
        '持仓': binance_positions(),
        '挂单': binance_open_orders(),
    }


def binance_key_report(api_key=None, api_secret=None):
    """给币安 API Key 做一次权限体检。

    ⚠️ 重要背景：币安读 U 本位合约持仓，Key 必须开「允许合约」，
    而这个权限同时也允许下单。币安没有「只读合约」这个选项。
    所以「IP 白名单」和「关闭提现」这两条特别重要。

    返回结构化报告；接口不可用时返回 None。
    """
    api_key = api_key or get_env('BINANCE_API_KEY')
    api_secret = api_secret or get_env('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        return None
    qs = urlencode({'timestamp': int(time.time() * 1000)})
    sig = _hmac_hex(api_secret, qs)
    try:
        r = requests.get(
            f'https://api.binance.com/sapi/v1/account/apiRestrictions?{qs}&signature={sig}',
            headers={'X-MBX-APIKEY': api_key, **UA}, timeout=TIMEOUT)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    d = r.json()

    report = {
        '读取权限': bool(d.get('enableReading')),
        '合约权限': bool(d.get('enableFutures')),
        '现货交易': bool(d.get('enableSpotAndMarginTrading')),
        '提现权限': bool(d.get('enableWithdrawals')),
        '划转权限': bool(d.get('permitsUniversalTransfer')),
        'IP白名单': bool(d.get('ipRestrict')),
        '创建时间': (datetime.fromtimestamp(d['createTime'] / 1000).strftime('%Y-%m-%d')
                     if d.get('createTime') else None),
    }

    # 评级
    if report['提现权限']:
        report['风险等级'] = '危险'
    elif report['合约权限'] and not report['IP白名单']:
        report['风险等级'] = '偏高'
    elif report['合约权限']:
        report['风险等级'] = '可接受'
    else:
        report['风险等级'] = '安全'

    tips = []
    if report['提现权限']:
        tips.append('❗ 开了「允许提现」，这是最危险的权限。立刻去币安关掉 —— '
                    '本工具完全不需要它。')
    if report['合约权限'] and not report['IP白名单']:
        tips.append('⚠️ 开了合约权限但没设 IP 白名单。你无法避免开合约权限'
                    '（读持仓需要它），但**IP 白名单能补上这个洞**：'
                    '绑了之后，不是你的 IP 就算拿到 Key 也用不了。强烈建议去绑。')
    if report['合约权限'] and report['IP白名单']:
        tips.append('✅ 合约权限 + IP 白名单，这是本场景下能做到的最稳配置。')
    if report['现货交易']:
        tips.append('💡 开了现货交易权限，本工具不需要它，建议关掉（减小暴露面）。')
    if report['划转权限']:
        tips.append('💡 开了万能划转权限，本工具不需要，建议关掉。')
    if not report['合约权限']:
        tips.append('ℹ️ 没开合约权限。这样最安全，但**读不到 U 本位合约持仓**，'
                    '自动同步会失败。要用同步功能就只能开它。')
    if not report['读取权限']:
        tips.append('❗ 没开读取权限，本工具什么都读不到。')
    if not tips:
        tips.append('✅ 权限配置没问题。')
    report['建议'] = tips
    return report


def binance_key_check(api_key=None, api_secret=None):
    """兼容旧接口：返回一段文字提示。"""
    rep = binance_key_report(api_key, api_secret)
    if rep is None:
        return None
    return rep['建议']


# ---------------- 欧易 OKX ----------------

def _okx_headers(api_key, api_secret, passphrase, method, path, body=''):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + \
        f'{datetime.now(timezone.utc).microsecond // 1000:03d}Z'
    sign = _hmac_b64(api_secret, f'{ts}{method}{path}{body}')
    return {
        'OK-ACCESS-KEY': api_key,
        'OK-ACCESS-SIGN': sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': passphrase,
        'Content-Type': 'application/json',
        **UA,
    }


def okx_positions(api_key=None, api_secret=None, passphrase=None):
    api_key = api_key or get_env('OKX_API_KEY')
    api_secret = api_secret or get_env('OKX_API_SECRET')
    passphrase = passphrase or get_env('OKX_PASSPHRASE')
    if not api_key or not api_secret or not passphrase:
        raise RuntimeError('没有配置 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE')

    path = '/api/v5/account/positions?instType=SWAP'
    r = requests.get('https://www.okx.com' + path,
                     headers=_okx_headers(api_key, api_secret, passphrase, 'GET', path),
                     timeout=TIMEOUT, proxies=get_proxy())
    body = _json_or_error(r, '欧易OKX')
    out = []
    for p in body.get('data', []):
        pos = _num(p.get('pos'))
        if pos == 0:
            continue
        mark = _num(p.get('markPx'))
        notional = _num(p.get('notionalUsd'))
        # OKX 的 pos 是「张数」，用 notionalUsd / 标记价 折算成币的数量
        qty = (notional / mark) if (mark and notional) else abs(pos)
        side = p.get('posSide')
        if side == 'net':
            side_dir = '多' if pos > 0 else '空'
        else:
            side_dir = '多' if side == 'long' else '空'
        out.append({
            '交易所': '欧易OKX',
            '币种': str(p.get('instId', '')).replace('-SWAP', '').replace('-', ''),
            '方向': side_dir,
            '数量': abs(qty),
            '开仓价': _num(p.get('avgPx')),
            '标记价': mark,
            '杠杆': _num(p.get('lever'), 1),
            '未实现盈亏': _num(p.get('upl')),
            '爆仓价': _num(p.get('liqPx')),
            '保证金': _num(p.get('margin')),
        })
    return out


# ---------------- Bybit ----------------

def _bybit_signed_get(path, params='', api_key=None, api_secret=None):
    api_key = api_key or get_env('BYBIT_API_KEY')
    api_secret = api_secret or get_env('BYBIT_API_SECRET')
    if not api_key or not api_secret:
        raise RuntimeError('没有配置 BYBIT_API_KEY / BYBIT_API_SECRET')
    ts, recv = str(int(time.time()*1000)), '5000'
    sign = _hmac_hex(api_secret, ts + api_key + recv + params)
    r = requests.get(f'https://api.bybit.com{path}' + (f'?{params}' if params else ''),
                     headers={'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': ts,
                              'X-BAPI-SIGN': sign, 'X-BAPI-RECV-WINDOW': recv, **UA},
                     timeout=TIMEOUT, proxies=get_proxy())
    return _json_or_error(r, 'Bybit')


def bybit_account(api_key=None, api_secret=None):
    body = _bybit_signed_get('/v5/account/wallet-balance', 'accountType=UNIFIED', api_key, api_secret)
    rows = (body.get('result') or {}).get('list') or []
    usdt = next((x for r in rows for x in (r.get('coin') or []) if x.get('coin') == 'USDT'), {})
    return {'交易所':'Bybit', '钱包余额':_num(usdt.get('walletBalance')),
            '保证金余额':_num(usdt.get('equity')), '可用余额':_num(usdt.get('availableToWithdraw') or usdt.get('availableBalance')),
            '未实现盈亏':_num(usdt.get('unrealisedPnl')), '资产':[usdt]}


def bybit_open_orders(symbol=None, api_key=None, api_secret=None):
    qs = 'category=linear&settleCoin=USDT&limit=200' + (f'&symbol={symbol}' if symbol else '')
    body = _bybit_signed_get('/v5/order/realtime', qs, api_key, api_secret)
    items = (body.get('result') or {}).get('list') or []
    return [{'订单ID':o.get('orderId'),'币种':o.get('symbol'),'买卖':o.get('side'),
             '类型':o.get('stopOrderType') or o.get('orderType'),'状态':o.get('orderStatus'),
             '价格':_num(o.get('price')),'触发价':_num(o.get('triggerPrice')),
             '数量':_num(o.get('qty')),'已成交':_num(o.get('cumExecQty')),
             '只减仓':bool(o.get('reduceOnly')),'全平仓单':bool(o.get('closeOnTrigger'))} for o in items]


def active_exchange():
    v = (get_env('ACTIVE_EXCHANGE') or '').strip()
    if v: return 'Bybit' if v.lower() in ('bybit','by') else '币安' if v.lower() in ('binance','bn') else v
    return 'Bybit' if get_env('BYBIT_API_KEY') and get_env('BYBIT_API_SECRET') else '币安'


def account_summary():
    ex = active_exchange()
    return bybit_account() if ex == 'Bybit' else binance_account()


def open_orders(exchange=None, symbol=None):
    ex = exchange or active_exchange()
    return bybit_open_orders(symbol) if ex == 'Bybit' else binance_open_orders(symbol)


def positions():
    ex = active_exchange()
    return bybit_positions() if ex == 'Bybit' else binance_positions()


def bybit_positions(api_key=None, api_secret=None):
    api_key = api_key or get_env('BYBIT_API_KEY')
    api_secret = api_secret or get_env('BYBIT_API_SECRET')
    if not api_key or not api_secret:
        raise RuntimeError('没有配置 BYBIT_API_KEY / BYBIT_API_SECRET')

    qs = 'category=linear&settleCoin=USDT&limit=200'
    ts = str(int(time.time() * 1000))
    recv = '5000'
    sign = _hmac_hex(api_secret, ts + api_key + recv + qs)
    r = requests.get(f'https://api.bybit.com/v5/position/list?{qs}',
                     headers={'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': ts,
                              'X-BAPI-SIGN': sign, 'X-BAPI-RECV-WINDOW': recv, **UA},
                     timeout=TIMEOUT, proxies=get_proxy())
    body = _json_or_error(r, 'Bybit')
    items = (body.get('result') or {}).get('list') or []
    out = []
    for p in items:
        size = _num(p.get('size'))
        if size == 0:
            continue
        out.append({
            '交易所': 'Bybit',
            '币种': p.get('symbol'),
            '方向': '多' if p.get('side') == 'Buy' else '空',
            '数量': abs(size),
            '开仓价': _num(p.get('avgPrice')),
            '标记价': _num(p.get('markPrice')),
            '杠杆': _num(p.get('leverage'), 1),
            '未实现盈亏': _num(p.get('unrealisedPnl')),
            '爆仓价': _num(p.get('liqPrice')),
            '保证金': _num(p.get('positionIM')),
        })
    return out


FETCHERS = {'币安': binance_positions, '欧易OKX': okx_positions, 'Bybit': bybit_positions}


def fetch_positions(exchange='币安'):
    """拉取某个交易所的当前持仓。返回列表（可能是空的）。"""
    fn = FETCHERS.get(exchange)
    if not fn:
        raise ValueError(f'暂不支持 {exchange}，目前支持：{"、".join(FETCHERS)}')
    ok, msg = has_credentials(exchange)
    if not ok:
        raise RuntimeError(f'{exchange} {msg}')
    return fn()


def sync_to_local(positions, keep_stop=True):
    """把交易所持仓写进本地持仓文件（供监控用）。

    keep_stop=True 时保留你本地已经填过的止损价（交易所接口不返回你自己的止损价）。
    """
    import monitor

    old = {p.get('币种'): p for p in monitor.load_positions()}
    merged = []
    for p in positions:
        prev = old.get(p['币种']) or {}
        merged.append({
            '币种': p['币种'],
            '方向': p['方向'],
            '开仓价': p['开仓价'],
            '数量': p['数量'],
            '杠杆': p['杠杆'],
            '止损价': (prev.get('止损价') or 0) if keep_stop else 0,
            '爆仓价': p.get('爆仓价') or prev.get('爆仓价') or 0,
            '交易所': p.get('交易所'),
            '未实现盈亏': p.get('未实现盈亏'),
            '同步时间': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
    monitor.save_positions(merged)
    return merged


def diff_positions(old, new):
    """对比两次持仓，返回变化说明（用于自动记账）。"""
    old_map = {p['币种']: p for p in old}
    new_map = {p['币种']: p for p in new}
    events = []
    for sym, np in new_map.items():
        op = old_map.get(sym)
        if not op:
            events.append({'类型': '开仓', '币种': sym, '持仓': np,
                           '说明': f'{sym} 新开 {np["方向"]} 仓 {np["数量"]}'})
        else:
            if np['方向'] != op['方向']:
                events.append({'类型': '反手', '币种': sym, '持仓': np,
                               '说明': f'{sym} 方向从 {op["方向"]} 变成 {np["方向"]}'})
            elif abs(np['数量'] - op['数量']) > 1e-12:
                ratio = np['数量'] / op['数量'] if op['数量'] else 0
                kind = '加仓' if ratio > 1 else '减仓'
                events.append({'类型': kind, '币种': sym, '持仓': np,
                               '说明': f'{sym} {kind}：{op["数量"]} → {np["数量"]}'})
    for sym, op in old_map.items():
        if sym not in new_map:
            events.append({'类型': '平仓', '币种': sym, '持仓': op,
                           '说明': f'{sym} 持仓已平掉'})
    return events