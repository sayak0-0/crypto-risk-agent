# -*- coding: utf-8 -*-
"""交易日志：读写 CSV、自动计算盈亏和 R 倍数。

CSV 用中文表头，方便直接用 Excel 打开看。
"""
import os
import pandas as pd

from common import TRADES_CSV

# 新建日志时的列顺序
COLUMNS = [
    '编号', '开仓时间', '平仓时间', '交易所', '币种', '方向', '杠杆',
    '开仓价', '平仓价', '数量', '止损价', '止盈价', '手续费',
    '净盈亏', 'R倍数', '情绪', '入场理由', '复盘笔记',
]

NUMERIC_COLUMNS = ['杠杆', '开仓价', '平仓价', '数量', '止损价', '止盈价',
                   '手续费', '净盈亏', 'R倍数']

情绪选项 = ['平静', '自信', '着急', 'FOMO追单', '报复性交易', '贪婪',
            '恐惧', '无聊', '亏损后补仓', '听说/跟单']

方向选项 = ['多', '空']

交易所选项 = ['币安', '欧易OKX', 'Bybit', 'Gate', '其他']

TIME_COLUMNS = ['开仓时间', '平仓时间']


def empty_df():
    """空日志。"""
    return pd.DataFrame(columns=COLUMNS)


def load(path=None):
    """读取交易日志，不存在就返回空表。"""
    path = path or TRADES_CSV
    if not os.path.exists(path):
        return empty_df()
    try:
        df = pd.read_csv(path, encoding='utf-8-sig', dtype=str)
    except pd.errors.EmptyDataError:
        return empty_df()
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ''
    extra = [c for c in df.columns if c not in COLUMNS]
    df = df[COLUMNS + extra]
    return compute_derived(df)


def save(df, path=None):
    """保存交易日志。"""
    path = path or TRADES_CSV
    os.makedirs(os.path.dirname(path), exist_ok=True)
    to_write = df.copy()
    to_write.to_csv(path, index=False, encoding='utf-8-sig')
    return path


def add_trade(trade, path=None):
    """追加一笔交易记录。"""
    df = load(path)
    row = {c: trade.get(c, '') for c in COLUMNS}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df = compute_derived(df)
    save(df, path)
    return df


def _to_num(s):
    return pd.to_numeric(s, errors='coerce')


def compute_derived(df):
    """自动计算净盈亏、R 倍数、名义价值。已有值不覆盖（除非是空）。"""
    if df is None or len(df) == 0:
        out = empty_df() if df is None else df.copy()
        for col in ['名义价值', '持仓时长']:
            if col not in out.columns:
                out[col] = ''
        return out

    out = df.copy()
    for col in NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = _to_num(out[col])

    sign = out['方向'].map({'多': 1, '空': -1}) if '方向' in out.columns else pd.Series(0, index=out.index)
    entry = out['开仓价']
    exit_ = out['平仓价']
    qty = out['数量']
    fee = out['手续费'].fillna(0) if '手续费' in out.columns else 0

    gross = (exit_ - entry) * qty * sign
    net_calc = gross - fee
    out['毛盈亏'] = gross
    if '净盈亏' in out.columns:
        out['净盈亏'] = out['净盈亏'].where(out['净盈亏'].notna(), net_calc)
    else:
        out['净盈亏'] = net_calc
    out['名义价值'] = entry * qty

    risk = (entry - out['止损价']).abs() * qty
    risk = risk.where(risk > 0)
    r_calc = out['净盈亏'] / risk
    if 'R倍数' in out.columns:
        out['R倍数'] = out['R倍数'].where(out['R倍数'].notna(), r_calc)
    else:
        out['R倍数'] = r_calc

    for col in TIME_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors='coerce')
    if '开仓时间' in out.columns and '平仓时间' in out.columns:
        delta = out['平仓时间'] - out['开仓时间']
        out['持仓时长'] = delta
    else:
        out['持仓时长'] = pd.NaT
    return out


def new_id(df):
    """生成下一个编号，例如 T0007。"""
    if df is None or len(df) == 0 or '编号' not in df.columns:
        return 'T0001'
    nums = []
    for v in df['编号'].astype(str):
        digits = ''.join(ch for ch in v if ch.isdigit())
        if digits:
            nums.append(int(digits))
    return f'T{(max(nums) + 1 if nums else 1):04d}'


def closed_trades(df):
    """只保留已平仓（有平仓价和净盈亏）的记录，按平仓时间排序。"""
    if df is None or len(df) == 0:
        return empty_df()
    if '平仓价' not in df.columns or '净盈亏' not in df.columns:
        return empty_df()
    out = df[df['平仓价'].notna() & df['净盈亏'].notna()].copy()
    if '平仓时间' in out.columns:
        out = out.sort_values('平仓时间', na_position='last')
    return out.reset_index(drop=True)


def recent_state(df):
    """返回开仓前检查需要的状态：今日笔数、连亏笔数、距上次亏损多少分钟。"""
    import datetime as dt
    state = {'today_trades': 0, 'consecutive_losses': 0,
             'minutes_since_last_loss': None, 'last_trade_time': None}
    closed = closed_trades(df)
    if len(closed) == 0:
        return state

    now = pd.Timestamp.now()
    if '开仓时间' in closed.columns:
        today = closed[closed['开仓时间'].dt.date == now.date()]
        state['today_trades'] = int(len(today))

    streak = 0
    for v in reversed(closed['净盈亏'].tolist()):
        if v is not None and v < 0:
            streak += 1
        else:
            break
    state['consecutive_losses'] = streak

    losses = closed[closed['净盈亏'] < 0]
    if len(losses):
        last = losses.iloc[-1]
        t = last.get('平仓时间')
        if pd.isna(t):
            t = last.get('开仓时间')
        if pd.notna(t):
            mins = (now - pd.Timestamp(t).tz_localize(None)).total_seconds() / 60
            state['minutes_since_last_loss'] = max(0.0, mins)
    if '平仓时间' in closed.columns and closed['平仓时间'].notna().any():
        state['last_trade_time'] = closed['平仓时间'].dropna().iloc[-1]
    return state