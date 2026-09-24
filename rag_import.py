# -*- coding: utf-8 -*-
"""导入公开加密新闻到本地 RAG。不读取交易记录或持仓。"""
import argparse
import csv
import hashlib
import heapq
import json
import itertools
import os
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
    ap.add_argument('--source', choices=['dataset', 'rss', 'both'], default='both')
    ap.add_argument('--limit', type=int, default=5000)
    ap.add_argument('--dataset', help='已下载的 RAR 路径')
    ap.add_argument('--force-download', action='store_true')
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
    meta = rag.build_index(docs, replace=True)
    print('索引完成：' + str(meta))


if __name__ == '__main__':
    main()