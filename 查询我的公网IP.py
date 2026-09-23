# -*- coding: utf-8 -*-
"""查询你电脑出口的公网 IP —— 也就是币安会看到的那个 IP。

为什么要单独做这个：IP 白名单要填的是「你的电脑访问币安时用的那个 IP」，
不是路由器上的 192.168.x.x，也不是别的机器查出来的结果。

直接双击根目录的「查询我的公网IP.bat」即可。
"""
import socket
import sys

import requests

UA = {'User-Agent': 'Mozilla/5.0'}


def try_get(url, key=None):
    try:
        d = requests.get(url, headers=UA, timeout=12).json()
        return d.get(key) if key else (d.get('ip') or d.get('ip_address'))
    except Exception:
        return None


def main():
    print('=' * 60)
    print('  查询你电脑的公网出口 IP')
    print('=' * 60)
    print()

    ipv4 = try_get('https://api.ipify.org?format=json', 'ip')
    ipv6 = try_get('https://api64.ipify.org?format=json', 'ip')
    info = None
    try:
        info = requests.get('https://ipinfo.io/json', headers=UA, timeout=12).json()
    except Exception:
        pass

    if not ipv4 and not ipv6:
        print('❌ 查不到公网 IP。可能原因：')
        print('   - 网络不通（如果不挂梯子访问不了境外网站，这很正常）')
        print('   - 公司/学校的网络做了限制')
        print()
        print('换个办法：用浏览器打开 https://ip.sb 或搜索「我的IP」，看显示的地址。')
        return 1

    main_ip = ipv4 or ipv6
    print(f'你当前的公网 IP：  {main_ip}')
    if ipv4 and ipv6 and ipv4 != ipv6:
        print(f'  （IPv4：{ipv4}）')
        print(f'  （IPv6：{ipv6}）')
    print()

    if info:
        org = info.get('org') or info.get('isp') or ''
        city = ', '.join(x for x in [info.get('city'), info.get('region'),
                                     info.get('country')] if x)
        print(f'归属地：  {city or "未知"}')
        print(f'运营方：  {org or "未知"}')
        print()
        low = org.lower()
        dc = ['azure', 'amazon', 'aws', 'google', 'cloud', 'digitalocean',
              'vultr', 'linode', 'ovh', 'alibaba', 'tencent', 'microsoft',
              'oracle', 'hosting', 'datacenter', 'server']
        if any(k in low for k in dc):
            print('⚠️ 这个 IP 属于云服务器/机房，不是你家的宽带。')
            print('   说明你现在挂着代理或 VPN。')
            print('   → 币安白名单要填的是这个代理/VPN 的出口 IP（就是上面这个）。')
        else:
            print('✅ 看起来是你本地宽带的 IP（不是机房 IP），适合填进白名单。')
        print()

    print('─' * 60)
    print('要填进币安 IP 白名单的就是这个：')
    print()
    print(f'    {main_ip}')
    print()
    print('─' * 60)
    print()
    if ipv4 and ipv6 and ipv4 != ipv6:
        print('❓ 上面显示了两行（IPv4 和 IPv6），填哪个？')
        print()
        print('　→ 填 IPv4，也就是纯数字加点的那种：')
        print(f'       {ipv4}')
        print()
        print(f'　→ 不要填 IPv6（上面带冒号的那个 {ipv6}），')
        print('　   币安的白名单基本只认 IPv4。')
        print()
        print('─' * 60)
    print()
    print('⚠️ 三个必须知道的坑：')
    print()
    print('1. 如果你挂梯子访问币安，务必在【挂着梯子的状态下】跑这个脚本。')
    print('   梯子关了再查，查到的 IP 是错的，填进去同步会失败。')
    print()
    print('2. 家宽 IP 会变。中国电信/联通/移动的家用宽带经常重新分配 IP，')
    print('   一般是重启光猫或过几天就变。IP 变了同步会报 401/403，')
    print('   回来重新跑一次这个脚本，去币安更新白名单就行。')
    print()
    print('3. 如果你的 IP 变化太频繁，两个选择：')
    print('   - 先把白名单留空（安全性下降，但能用）')
    print('   - 换 OKX / Bybit —— 它们的只读权限不用绑 IP 也能读合约持仓')
    print()
    print('4. 这个 IP 属于你的网络信息，别随便发到群里或截图外发。')

    print()
    print('（按回车关闭）')
    try:
        input()
    except EOFError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())