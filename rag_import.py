# -*- coding: utf-8 -*-
"""导入公开加密新闻到本地 RAG。不读取交易记录或持仓。"""
import argparse
import csv
import hashlib
import heapq
import json
import itertools
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import requests

import news
import rag
from common import get_proxy, DATA_DIR

DATASET_URL = ('https://github.com/soheilrahsaz/cryptoNewsDataset/raw/main/'
               'csvOutput/news_currencies_source_joinedResult.rar')
DOWNLOAD_DIR = Path(DATA_DIR) / 'RAG_新闻' / 'downloads'


def _find_unrar():
    candidates = [
        r'C:\Program Files\WinRAR\UnRAR.exe',
        r'C:\Program Files (x86)\WinRAR\UnRAR.exe',
        'unrar', 'unar', '7z',
    ]
    for p in candidates:
        if os.path.sep in p and Path(p).exists():
            return p
        if os.path.sep not in p:
            from shutil import which
            found = which(p)
            if found:
                return found
    raise RuntimeError('找不到解压工具，请安装 WinRAR 或 7-Zip')


def download_dataset(path=None, force=False):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(path) if path else DOWNLOAD_DIR / 'crypto_news.rar'
    if path.exists() and not force and path.stat().st_size > 1_000_000:
        return path
    print(f'下载公开新闻数据集：{DATASET_URL}')
    with requests.get(DATASET_URL, stream=True, timeout=120,
                      proxies=get_proxy()) as r:
        r.raise_for_status()
        tmp = path.with_suffix('.tmp')
        with tmp.open('wb') as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, path)
    return path


def extract_dataset(rar_path):
    out = DOWNLOAD_DIR / 'extracted'
    out.mkdir(parents=True, exist_ok=True)
    csvs = list(out.rglob('*.csv'))
    if csvs:
        return csvs[0]
    exe = _find_unrar()
    print(f'解压：{exe}')
    subprocess.run([exe, 'x', '-o+', str(rar_path), str(out)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    csvs = list(out.rglob('*.csv'))
    if not csvs:
        raise RuntimeError('解压后没找到 CSV')
    return csvs[0]


def _dt(v):
    s = str(v or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            pass
    return None


def dataset_documents(csv_path, limit=5000, since='2021-01-01'):
    out = csv_path.parent / 'crypto_news_rag_subset.csv'
    if out.exists() and out.stat().st_size > 1000:
        return out
    since_dt = datetime.strptime(since, '%Y-%m-%d')
    heap = []
    serial = itertools.count()
    # 用固定大小的小顶堆，只保留最新 limit 条，避免把 90MB CSV 全读进内存。
    with csv_path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            dt = _dt(row.get('newsDatetime'))
            title = (row.get('title') or '').strip()
            currencies = str(row.get('currencies') or '').strip()
            if not dt or dt < since_dt or not title or not currencies:
                continue
            item = (dt.timestamp(), next(serial), {
                'id': str(row.get('id') or ''),
                '标题': title,
                '摘要': (row.get('description') or '').strip(),
                '来源': (row.get('sourceDomain') or row.get('sourceUrl') or 'Cryptopanic'),
                '时间': dt.strftime('%Y-%m-%d %H:%M:%S'),
                '链接': (row.get('url') or '').strip(),
                '币种': [x.strip().upper() + 'USDT' for x in currencies.split(',') if x.strip()],
                '类型': '公开加密新闻',
                '情绪': {
                    '正面': row.get('positive') == '1',
                    '负面': row.get('negative') == '1',
                    '重要': row.get('important') == '1',
                },
            })
            if len(heap) < int(limit):
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
    rows = [x[2] for x in sorted(heap, reverse=True)]
    with out.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            for row in rows:
                x = dict(row)
                x['币种'] = json.dumps(x['币种'], ensure_ascii=False)
                x['情绪'] = json.dumps(x['情绪'], ensure_ascii=False)
                writer.writerow(x)
    return out


LIVE_API = 'https://cryptocurrency.cv/api/news'


def _symbols_from_text(text):
    t = str(text or '')
    aliases = {
        'bitcoin': 'BTCUSDT', 'btc': 'BTCUSDT', 'ethereum': 'ETHUSDT',
        'eth': 'ETHUSDT', 'solana': 'SOLUSDT', 'sol': 'SOLUSDT',
        'ripple': 'XRPUSDT', 'xrp': 'XRPUSDT', 'dogecoin': 'DOGEUSDT',
        'doge': 'DOGEUSDT', 'cardano': 'ADAUSDT', 'ada': 'ADAUSDT',
        'chainlink': 'LINKUSDT', 'link': 'LINKUSDT', 'bnb': 'BNBUSDT',
        'binance coin': 'BNBUSDT', 'shiba': 'SHIBUSDT', 'avalanche': 'AVAXUSDT',
    }
    low = t.lower()
    out = {sym for word, sym in aliases.items() if word in low}
    for code in re.findall(r'\b(BTC|ETH|SOL|XRP|DOGE|ADA|LINK|BNB|SHIB|AVAX|ZEC|UNI|SUI)\b', t.upper()):
        out.add(code + 'USDT')
    return sorted(out)


def live_documents(limit=200):
    """从免费实时加密新闻 API 拉取当前文章。"""
    r = requests.get(LIVE_API, params={'limit': int(limit)}, timeout=45,
                     proxies=get_proxy())
    r.raise_for_status()
    data = r.json()
    rows = data.get('articles') or data.get('data') or []
    out = []
    for item in rows:
        title = str(item.get('title') or '').strip()
        link = str(item.get('link') or item.get('url') or '').strip()
        if not title or not link:
            continue
        text = title + ' ' + str(item.get('description') or '')
        out.append({
            'id': 'live-' + hashlib.md5(link.encode('utf-8')).hexdigest()[:14],
            '标题': title,
            '摘要': str(item.get('description') or '').strip(),
            '来源': str(item.get('source') or 'cryptocurrency.cv'),
            '时间': str(item.get('pubDate') or '')[:19],
            '链接': link,
            '币种': _symbols_from_text(text),
            '类型': '实时新闻API',
            '分类': str(item.get('category') or ''),
            '可信度': item.get('credibility'),
        })
    return out


def rss_documents(limit=200):
    items, _ = news.fetch_news(limit=limit)
    out = []
    for i, item in enumerate(items):
        title = item.get('标题') or ''
        if not title:
            continue
        out.append({
            'id': 'rss-' + hashlib.md5(
                (title + str(item.get('链接') or '')).encode('utf-8')).hexdigest()[:14],
            '标题': title,
            '摘要': item.get('摘要') or '',
            '来源': item.get('来源') or 'RSS',
            '时间': item.get('时间') or '',
            '链接': item.get('链接') or '',
            '币种': [],
            '类型': '实时RSS新闻',
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['dataset', 'rss', 'live', 'both'], default='both')
    ap.add_argument('--limit', type=int, default=5000)
    ap.add_argument('--dataset', help='已下载的 RAR 路径')
    ap.add_argument('--force-download', action='store_true')
    ap.add_argument('--live-limit', type=int, default=200)
    args = ap.parse_args()

    docs = []
    if args.source in ('dataset', 'both'):
        rar = download_dataset(args.dataset, force=args.force_download)
        csv_path = extract_dataset(rar)
        subset = dataset_documents(csv_path, limit=args.limit)
        import pandas as pd
        df = pd.read_csv(subset)
        df['币种'] = df['币种'].fillna('[]').map(json.loads)
        df['情绪'] = df['情绪'].map(json.loads)
        docs.extend(df.to_dict('records'))
        print(f'公开新闻数据集：{len(df)} 条')
    if args.source in ('rss', 'both'):
        rss = rss_documents(limit=min(200, args.limit))
        docs.extend(rss)
        print(f'实时 RSS：{len(rss)} 条')

    if docs:
        meta = rag.build_index(docs, replace=True)
        print('基础索引：' + str(meta))

    if args.source in ('live', 'both'):
        live = live_documents(limit=args.live_limit)
        meta = rag.append_index(live)
        print(f'实时新闻 API：{len(live)} 条，增量索引：{meta}')
    if not docs and args.source not in ('live', 'both'):
        raise RuntimeError('没有可导入的数据')


if __name__ == '__main__':
    main()