# -*- coding: utf-8 -*-
"""Bybit 公共成交流 -> CVD JSON，用于补充订单流数据。"""
import asyncio, json, time
from pathlib import Path
import websockets
from common import DATA_DIR

OUT = Path(DATA_DIR) / 'bybit_orderflow.json'
URL = 'wss://stream.bybit.com/v5/public/linear'
SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','BNBUSDT','LINKUSDT','AVAXUSDT','SUIUSDT','ARBUSDT','OPUSDT','APTUSDT','SEIUSDT','PEPEUSDT','WIFUSDT']

def _save(symbols, status='ok', error=''):
    now = time.time(); rows = {}
    for sym, x in symbols.items():
        net = x['buy'] - x['sell']; ratio = x['buy'] / x['sell'] if x['sell'] else None
        signal = 'STRONG_BUY' if ratio and ratio >= 1.8 else 'BUY' if ratio and ratio >= 1.15 else 'STRONG_SELL' if ratio and ratio <= .55 else 'SELL' if ratio and ratio <= .87 else 'NEUTRAL'
        rows[sym] = {'累计买入': round(x['buy'],6), '累计卖出': round(x['sell'],6),
                     '净成交': round(net,6), '主动买卖比': round(ratio,4) if ratio else None,
                     '信号': signal, '成交笔数': x['count']}
    data = {'更新时间': time.strftime('%Y-%m-%d %H:%M:%S'), '状态': status,
            '错误': error, '交易所': 'bybit', '数据': rows}
    OUT.parent.mkdir(parents=True, exist_ok=True); tmp = OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'); tmp.replace(OUT)

async def run():
    symbols = {s: {'buy':0.0,'sell':0.0,'count':0} for s in SYMBOLS}
    _save(symbols, 'connecting')
    while True:
        try:
            async with websockets.connect(URL, ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
                _save(symbols, 'ok')
                args=[f'publicTrade.{s}' for s in SYMBOLS]
                for i in range(0, len(args), 10):
                    await ws.send(json.dumps({'op':'subscribe','args':args[i:i+10]}))
                    await asyncio.sleep(0.2)
                last_save = time.time()
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get('topic','').startswith('publicTrade.'):
                        for t in msg.get('data') or []:
                            sym = t.get('s'); x = symbols.get(sym)
                            if not x: continue
                            vol = float(t.get('v') or 0)
                            if str(t.get('S','')).lower() == 'buy': x['buy'] += vol
                            else: x['sell'] += vol
                            x['count'] += 1
                    if time.time() - last_save >= 2:
                        _save(symbols, 'ok'); last_save = time.time()
        except asyncio.CancelledError:
            return
        except Exception as e:
            _save(symbols, 'error', f'{type(e).__name__}: {e}')
            await asyncio.sleep(5)

if __name__ == '__main__':
    asyncio.run(run())