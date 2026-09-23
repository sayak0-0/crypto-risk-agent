# -*- coding: utf-8 -*-
"""命令行常驻监控：关掉浏览器也能一直盯盘。

用法：
    python monitor_cli.py                  # 用界面里配好的规则，每 60 秒检查
    python monitor_cli.py --interval 30    # 每 30 秒检查
    python monitor_cli.py --once           # 只检查一次就退出
    python monitor_cli.py --exchange 币安   # 指定数据源
    python monitor_cli.py --quiet          # 没警报时不打印（只记文件）
    python monitor_cli.py --no-sound       # 关掉提示音

按 Ctrl+C 停止。
"""
import argparse
import sys
import time
from datetime import datetime

import monitor

try:
    import winsound
    HAS_BEEP = True
except ImportError:
    HAS_BEEP = False

# 终端颜色
C = {'提示': '\033[36m', '警告': '\033[33m', '紧急': '\033[41;97m'}
RESET = '\033[0m'
DIM = '\033[90m'


def beep(level):
    if not HAS_BEEP:
        return
    try:
        if level == '紧急':
            for _ in range(3):
                winsound.Beep(1000, 180)
                time.sleep(0.08)
        elif level == '警告':
            winsound.Beep(700, 250)
        else:
            winsound.Beep(500, 120)
    except Exception:
        pass


def stamp():
    return datetime.now().strftime('%H:%M:%S')


def one_round(exchange, quiet=False, sound=True):
    """跑一轮，返回触发的警报数。"""
    alerts, errors, snaps = monitor.run_once(exchange=exchange)

    if snaps and not quiet:
        bright = []
        for sym, s in snaps.items():
            bits = [f'{sym} {s.get("标记价"):,.4f}']
            if s.get('24h涨跌幅') is not None:
                bits.append(f'24h {s["24h涨跌幅"]:+.2f}%')
            if s.get('资金费率') is not None:
                bits.append(f'费率 {s["资金费率"] * 100:+.4f}%')
            if s.get('多空比') is not None:
                bits.append(f'多空比 {s["多空比"]:.2f}')
            bright.append('  '.join(bits))
        print(f'{DIM}[{stamp()}] {(" | ".join(bright))}{RESET}')

    for e in errors:
        print(f'{C["警告"]}[{stamp()}] {e}{RESET}')

    for a in alerts:
        col = C.get(a['等级'], '')
        icon = monitor.LEVEL_ICON.get(a['等级'], '•')
        print(f'{col}{icon} [{a["时间"]}] {a["币种"]} · {a["规则"]}{RESET}')
        print(f'   {a["说明"]}')
        if a.get('备注'):
            print(f'   {DIM}备注：{a["备注"]}{RESET}')
        if sound:
            beep(a['等级'])
    return len(alerts)


def main():
    ap = argparse.ArgumentParser(description='加密货币市场监控（常驻命令行版）')
    ap.add_argument('--interval', type=int, default=60, help='检查间隔秒数，默认 60')
    ap.add_argument('--exchange', default='自动',
                    choices=['自动', '币安', '欧易OKX', 'Bybit'], help='数据源')
    ap.add_argument('--once', action='store_true', help='只检查一次就退出')
    ap.add_argument('--quiet', action='store_true', help='没警报时不打印行情')
    ap.add_argument('--no-sound', action='store_true', help='关掉提示音')
    args = ap.parse_args()

    rules = [r for r in monitor.load_rules() if r.get('启用', True)]
    positions = monitor.load_positions()
    symbols = monitor.collect_symbols(rules, positions)

    print('=' * 62)
    print('  加密货币市场监控')
    print('=' * 62)
    print(f'  规则数    : {len(rules)} 条（其中行情类 '
          f'{sum(1 for r in rules if r["类型"] in monitor.行情类)} 条，'
          f'持仓类 {sum(1 for r in rules if r["类型"] in monitor.持仓类)} 条）')
    print(f'  持仓数    : {len(positions)} 笔')
    print(f'  监控币种  : {", ".join(symbols) if symbols else "（无）"}')
    print(f'  检查间隔  : {args.interval} 秒')
    print(f'  数据源    : {args.exchange}')
    print(f'  提示音    : {"开" if HAS_BEEP and not args.no_sound else "关"}')
    print(f'  警报记录  : {monitor.ALERT_LOG}')
    print('=' * 62)

    if not symbols:
        print('\n还没有任何监控规则或持仓，先运行界面配一下：')
        print('  streamlit run app.py   ->  「🔔 市场监控」页签')
        return 1

    if not rules and positions:
        print('\n只有持仓、没有规则。建议至少加一条「距爆仓不足」规则。')

    print('\n开始监控，按 Ctrl+C 停止。\n')
    rounds = 0
    try:
        while True:
            rounds += 1
            n = one_round(args.exchange, args.quiet, not args.no_sound)
            if args.once:
                print(f'\n本次检查完成，触发 {n} 条警报。')
                break
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        print(f'\n\n已停止。共检查 {rounds} 轮。')
    except Exception as e:
        print(f'\n出错了：{type(e).__name__}: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())