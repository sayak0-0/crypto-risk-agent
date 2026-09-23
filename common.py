# -*- coding: utf-8 -*-
"""公共配置：路径、.env 读取、通用小工具。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
ENV_PATH = os.path.join(BASE_DIR, '.env')
TRADES_CSV = os.path.join(DATA_DIR, '我的交易记录.csv')
CONFIG_JSON = os.path.join(DATA_DIR, '我的风控设置.json')

os.makedirs(DATA_DIR, exist_ok=True)

# 风控默认参数（都可改）
DEFAULT_CONFIG = {
    '本金': 1000.0,           # USDT，用于算仓位的账户权益
    '单笔风险百分比': 1.0,     # 每笔最多亏本金的百分之几
    '杠杆': 10.0,
    '手续费率': 0.0005,       # 单边 taker 费率，Binance USDT-M 约 0.05%
    '维持保证金率': 0.005,     # 低档位近似值，实际看交易所分层表
    '最低盈亏比': 1.5,
    '每日最多交易笔数': 3,
    '连亏几笔后停手': 2,
}


def get_env(key, default=None):
    """优先读环境变量，其次读本目录 .env，最后读工作区根目录 .env。"""
    val = os.environ.get(key)
    if val:
        return val.strip()
    for env_path in (os.path.join(BASE_DIR, '.env'),
                     os.path.join(os.path.dirname(BASE_DIR), '.env')):
        if not os.path.exists(env_path):
            continue
        with open(env_path, encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k.strip() == key:
                    got = v.strip().strip('"').strip("'")
                    if got:
                        return got
    return default


def update_env(updates, path=None):
    """安全地更新 .env：只改指定的键，其他行原样保留。

    不存在就创建。不会打印任何值到日志。
    """
    path = path or ENV_PATH
    lines = []
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig') as f:
            lines = f.read().splitlines()

    done = set()
    out = []
    for line in lines:
        raw = line.strip()
        if raw and not raw.startswith('#') and '=' in raw:
            k = raw.split('=', 1)[0].strip()
            if k in updates:
                out.append(f'{k}={updates[k]}')
                done.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append(f'{k}={v}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out).rstrip() + '\n')
    return path


def load_config():
    """读取风控设置，缺项用默认值补齐。"""
    import json
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_JSON):
        try:
            with open(CONFIG_JSON, encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    """保存风控设置。"""
    import json
    with open(CONFIG_JSON, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return CONFIG_JSON


def fmt_usdt(x, digits=2):
    """格式化金额，正数带 + 号。"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return '-'
    sign = '+' if x > 0 else ''
    return f'{sign}{x:,.{digits}f}'


def pct(x, digits=2):
    """格式化百分比。"""
    try:
        return f'{float(x):.{digits}f}%'
    except (TypeError, ValueError):
        return '-'