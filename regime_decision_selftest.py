# -*- coding: utf-8 -*-
import tempfile
from pathlib import Path
import regime, decision_log as dl

def main():
    assert regime.classify({'标记价':103,'24h涨跌幅':2}, {'MA20':102,'MA60':98,'ATR14百分比':2})['状态']=='趋势上涨'
    assert regime.classify({'标记价':97,'24h涨跌幅':-2}, {'MA20':98,'MA60':102,'ATR14百分比':2})['状态']=='趋势下跌'
    assert regime.classify({'标记价':100,'24h涨跌幅':12}, {'MA20':100,'MA60':100,'ATR14百分比':9})['状态']=='高波动'
    old_path, old_snap, old_due = dl.PATH, dl.market.snapshot, dl._due
    with tempfile.TemporaryDirectory() as td:
        dl.PATH = Path(td)/'decisions.json'
        dl.market.snapshot = lambda symbol, exchange='自动': {'标记价': 110.0}
        dl._due = lambda row, hours: True
        try:
            analysis={'主持人':{'方向':'偏多','信心':70},'当时价格':100,'市场状态':{'状态':'趋势上涨'}}
            dl.record('BTCUSDT', analysis, {'方向':'做多','入场价':100}, analysis['市场状态'])
            rows, n = dl.update_outcomes()
            assert n == 3
            assert all(v is True for v in rows[0]['是否正确'].values())
        finally:
            dl.PATH, dl.market.snapshot, dl._due = old_path, old_snap, old_due
    print('市场状态与决策验证自测通过 6 项')

if __name__=='__main__': main()