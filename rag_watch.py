# -*- coding: utf-8 -*-
"""持续抓取公开实时新闻并增量写入本地 RAG。"""
import argparse
import time
from datetime import datetime

import rag
from rag_import import live_documents, rss_documents


def stamp():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def update_once(include_rss=False, live_limit=3, rss_limit=200):
    live = live_documents(limit=live_limit)
    meta = rag.append_index(live)
    print(f'[{stamp()}] 实时 API {len(live)} 条，新增 {meta.get("新增", 0)} 条，'
          f'总索引 {meta.get("文档数", 0)} 条', flush=True)
    if include_rss:
        rss = rss_documents(limit=rss_limit)
        meta = rag.append_index(rss)
        print(f'[{stamp()}] RSS {len(rss)} 条，新增 {meta.get("新增", 0)} 条，'
              f'总索引 {meta.get("文档数", 0)} 条', flush=True)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=int, default=60,
                    help='实时新闻轮询秒数，默认 60')
    ap.add_argument('--rss-every', type=int, default=10,
                    help='每多少轮补一次 RSS，默认 10')
    ap.add_argument('--once', action='store_true')
    args = ap.parse_args()
    if args.once:
        update_once(include_rss=True)
        return
    rounds = 0
    while True:
        try:
            update_once(include_rss=(rounds % max(1, args.rss_every) == 0))
        except KeyboardInterrupt:
            print('\n已停止')
            return
        except Exception as e:
            print(f'[{stamp()}] 更新失败：{type(e).__name__}: {e}', flush=True)
        rounds += 1
        time.sleep(max(15, int(args.interval)))


if __name__ == '__main__':
    main()