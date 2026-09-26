# -*- coding: utf-8 -*-
import json
import tempfile
from pathlib import Path
import position_watch as pw

def main():
    old_watch, old_public, old_snap = pw.WATCH_PATH, pw.PUBLIC_PATH, pw.market.snapshot
    old_pos, old_orders = pw.exchange_sync.binance_positions, pw.exchange_sync.binance_open_orders
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pw.WATCH_PATH = root / 'watch.json'
        pw.PUBLIC_PATH = root / 'positions.json'
        pw.market.snapshot = lambda symbol, exchange='自动': {'标记价': 110.0}
        pw.exchange_sync.binance_positions = lambda: [{'币种':'BTCUSDT','方向':'多','数量':1.0,'开仓价':100.0,'杠杆':10,'保证金':10.0}]
        pw.exchange_sync.binance_open_orders = lambda symbol=None: [{'类型':'STOP_MARKET','触发价':95.0},{'类型':'TAKE_PROFIT_MARKET','触发价':120.0}]
        try:
            rec = pw.add_plan({
                '可执行': True, '标的': 'BTCUSDT', '方向': '做多',
                '入场价': 100.0, '止损价': 95.0, '止盈价': 120.0,
                '杠杆': 10,
                '仓位': {'建议数量': 1.0, '占用保证金': 10.0},
            })
            rows = pw.update_all(force=True)
            assert rows[0]['浮动盈亏'] == 10.0
            assert rows[0]['保证金收益率'] == 100.0
            assert pw.PUBLIC_PATH.exists()
        finally:
            pw.WATCH_PATH, pw.PUBLIC_PATH, pw.market.snapshot = old_watch, old_public, old_snap
            pw.exchange_sync.binance_positions, pw.exchange_sync.binance_open_orders = old_pos, old_orders
    print('开仓观察自测通过 3 项')

if __name__ == '__main__':
    main()