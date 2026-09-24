# -*- coding: utf-8 -*-
"""公开新闻 RAG 自测：不下载模型，不访问交易所。"""
import sys
import tempfile
from pathlib import Path

import numpy as np

import rag
from rag_import import _symbols_from_text

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def fake_embed(texts, batch_size=64):
    out = []
    for text in texts:
        t = str(text).lower()
        if 'etf' in t or '资金流入' in t:
            v = [1.0, 0.0, 0.0]
        elif 'solana' in t or 'sol' in t:
            v = [0.0, 1.0, 0.0]
        else:
            v = [0.0, 0.0, 1.0]
        out.append(v)
    arr = np.asarray(out, dtype='float32')
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def main():
    old = {k: getattr(rag, k) for k in
           ('RAG_DIR', 'DOCS_PATH', 'VECTORS_PATH', 'META_PATH', 'embed')}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        rag.RAG_DIR = root
        rag.DOCS_PATH = root / 'documents.jsonl'
        rag.VECTORS_PATH = root / 'vectors.npy'
        rag.META_PATH = root / 'meta.json'
        rag.embed = fake_embed
        rag._CACHE.update({'mtime_docs': None, 'mtime_vec': None,
                           'docs': None, 'vectors': None})
        docs = [
            {'id': '1', '标题': 'Bitcoin ETF 资金流入创纪录', '摘要': 'ETF flows',
             '来源': 'test', '时间': '2026-01-01', '币种': ['BTCUSDT'],
             '链接': 'https://example.com/1'},
            {'id': '2', '标题': 'Solana network activity rises', '摘要': 'SOL',
             '来源': 'test', '时间': '2026-01-02', '币种': ['SOLUSDT'],
             '链接': 'https://example.com/2'},
        ]
        meta = rag.build_index(docs, replace=True)
        assert meta['文档数'] == 2
        hits = rag.search('比特币 ETF 资金流入', top_k=1)
        assert hits and hits[0]['id'] == '1', hits
        context = rag.format_context(hits)
        assert 'Bitcoin ETF' in context and 'https://example.com/1' in context
        sources = rag.format_sources(hits)
        assert 'Bitcoin ETF' in sources and 'https://example.com/1' in sources
        sol = rag.search('Solana 有什么新闻', top_k=2, symbols=['SOLUSDT'])
        assert sol and sol[0]['id'] == '2', sol
        assert rag.status()['就绪'] is True
        extra = rag.append_index([docs[0], {'id': '3', '标题': 'Ethereum ETF update', '摘要': 'ETH ETF', '来源': 'test', '时间': '2026-01-03', '币种': ['ETHUSDT'], '链接': 'https://example.com/3'}])
        assert extra.get('新增') == 1, extra
        assert extra.get('文档数') == 3, extra
        syms = _symbols_from_text('Bitcoin and Ethereum ETF news')
        assert 'BTCUSDT' in syms and 'ETHUSDT' in syms, syms
    for k, v in old.items():
        setattr(rag, k, v)
    print('公开新闻 RAG 自测通过 4 项')


if __name__ == '__main__':
    main()