# -*- coding: utf-8 -*-
"""绩效统计：胜率、盈亏比、盈利因子、期望值、R 倍数、最大回撤、分组归因。"""
import pandas as pd

from journal import closed_trades


def _num(x, default=0.0):
    try:
        v = float(x)
        if pd.isna(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def max_streak(values, positive):
    """最大连续同向笔数。positive=True 算连胜，False 算连亏。"""
    best = cur = 0
    for v in values:
        v = _num(v)
        hit = v > 0 if positive else v < 0
        if hit:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def equity_curve(df, start_equity=None):
    """按时间顺序的累计盈亏曲线。"""
    closed = closed_trades(df)
    if len(closed) == 0:
        return pd.DataFrame(columns=['序号', '时间', '单笔盈亏', '累计盈亏', '权益'])

    cum = closed['净盈亏'].fillna(0).cumsum()
    base = _num(start_equity, 0) if start_equity else 0
    return pd.DataFrame({
        '序号': range(1, len(closed) + 1),
        '时间': closed['平仓时间'] if '平仓时间' in closed.columns else range(len(closed)),
        '单笔盈亏': closed['净盈亏'].values,
        '累计盈亏': cum.values,
        '权益': (base + cum).values,
    })


def max_drawdown(df, start_equity=None):
    """最大回撤。返回 (金额, 百分比或 None)。"""
    closed = closed_trades(df)
    if len(closed) == 0:
        return 0.0, None
    cum = closed['净盈亏'].fillna(0).cumsum()
    peak = cum.cummax()
    dd = cum - peak
    worst = float(dd.min()) if len(dd) else 0.0
    pct = None
    base = _num(start_equity, 0)
    if base > 0:
        peak_equity = base + float(peak[dd.idxmin()]) if len(dd) else base
        if peak_equity > 0:
            pct = abs(worst) / peak_equity * 100
    return abs(worst), pct


def summary(df, start_equity=None):
    """核心绩效指标。"""
    closed = closed_trades(df)
    n = len(closed)
    if n == 0:
        return {'交易笔数': 0}

    pnl = closed['净盈亏'].fillna(0)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(abs(losses.mean())) if len(losses) else 0.0

    dd_amt, dd_pct = max_drawdown(closed, start_equity)
    r = closed['R倍数'].dropna() if 'R倍数' in closed.columns else pd.Series(dtype=float)

    hold = None
    if '持仓时长' in closed.columns:
        h = closed['持仓时长'].dropna()
        if len(h) and hasattr(h.iloc[0], 'total_seconds'):
            hold = h.map(lambda x: x.total_seconds() / 3600).mean()

    return {
        '交易笔数': n,
        '盈利笔数': int(len(wins)),
        '亏损笔数': int(len(losses)),
        '平局笔数': int((pnl == 0).sum()),
        '胜率': len(wins) / n * 100,
        '总净盈亏': float(pnl.sum()),
        '总手续费': _num(closed['手续费'].fillna(0).sum()) if '手续费' in closed.columns else 0.0,
        '平均每笔': float(pnl.mean()),
        '平均盈利': avg_win,
        '平均亏损': avg_loss,
        '盈亏比': (avg_win / avg_loss) if avg_loss > 0 else None,
        '盈利因子': (gross_profit / gross_loss) if gross_loss > 0 else None,
        '期望值': float(pnl.mean()),
        '总R': float(r.sum()) if len(r) else None,
        '平均R': float(r.mean()) if len(r) else None,
        '最大单笔盈利': float(pnl.max()),
        '最大单笔亏损': float(pnl.min()),
        '最大连胜': max_streak(pnl.tolist(), True),
        '最大连亏': max_streak(pnl.tolist(), False),
        '最大回撤': dd_amt,
        '最大回撤百分比': dd_pct,
        '平均持仓小时': hold,
        '总名义成交额': _num(closed['名义价值'].fillna(0).sum()) if '名义价值' in closed.columns else 0.0,
    }


def period_pnl(df, days=1):
    """最近 N 天的净盈亏。"""
    closed = closed_trades(df)
    if len(closed) == 0 or '平仓时间' not in closed.columns:
        return 0.0
    t = closed['平仓时间'].dropna()
    if len(t) == 0:
        return 0.0
    end = t.max()
    start = end - pd.Timedelta(days=days)
    return float(closed.loc[closed['平仓时间'] >= start, '净盈亏'].fillna(0).sum())


def by_group(df, col, min_trades=1):
    """按某一列分组统计（币种 / 情绪 / 方向 / 止盈止损原因等）。"""
    closed = closed_trades(df)
    if len(closed) == 0 or col not in closed.columns:
        return pd.DataFrame()

    g = closed.groupby(col, dropna=False)
    rows = []
    for name, sub in g:
        pnl = sub['净盈亏'].fillna(0)
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        rows.append({
            col: name if str(name) != 'nan' else '（未填）',
            '笔数': len(sub),
            '胜率': len(wins) / len(sub) * 100 if len(sub) else 0,
            '净盈亏': float(pnl.sum()),
            '平均每笔': float(pnl.mean()),
            '总亏损': float(abs(losses.sum())),
            '总盈利': float(wins.sum()),
        })
    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out
    out = out[out['笔数'] >= min_trades]
    return out.sort_values('净盈亏').reset_index(drop=True)


def hourly_by(df, col='开仓时间'):
    """按小时统计盈亏，帮你看是不是某些时段特别容易亏。"""
    closed = closed_trades(df)
    if len(closed) == 0 or col not in closed.columns:
        return pd.DataFrame()
    tmp = closed.copy()
    tmp['小时'] = tmp[col].dt.hour
    out = tmp.groupby('小时').agg(
        笔数=('净盈亏', 'size'),
        净盈亏=('净盈亏', 'sum'),
    ).reset_index()
    return out.sort_values('净盈亏')