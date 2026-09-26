# -*- coding: utf-8 -*-
"""公开数据 RAG：本地向量索引 + 加密新闻检索，不读取个人交易数据。"""
import json
import os
import threading
import email.utils
from datetime import datetime, timezone
import math
from pathlib import Path

import numpy as np

from common import DATA_DIR

RAG_DIR = Path(DATA_DIR) / 'RAG_新闻'
DOCS_PATH = RAG_DIR / 'documents.jsonl'
VECTORS_PATH = RAG_DIR / 'vectors.npy'
META_PATH = RAG_DIR / 'meta.json'
MODEL_NAME = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'

_MODEL = None
_MODEL_LOCK = threading.Lock()
_CACHE = {'mtime_docs': None, 'mtime_vec': None, 'docs': None, 'vectors': None}


def _model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                from fastembed import TextEmbedding
                _MODEL = TextEmbedding(model_name=MODEL_NAME)
    return _MODEL


def embed(texts, batch_size=64):
    """生成归一化向量；模型在本机运行，不上传文本。"""
    texts = [str(x or '').strip() for x in texts]
    arr = np.asarray(list(_model().embed(texts, batch_size=batch_size)),
                     dtype='float32')
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _doc_text(doc):
    priority = doc.get('币种') or []
    if not isinstance(priority, (list, tuple, set)):
        priority = [priority]
    parts = [
        str(doc.get('标题') or ''),
        str(doc.get('摘要') or ''),
        '币种：' + ','.join(str(x) for x in priority) if priority else '',
        '来源：' + str(doc.get('来源') or '') if doc.get('来源') else '',
    ]
    return '\n'.join(x for x in parts if x and x != 'nan').strip()


def _dedup_key(doc):
    return str(doc.get('id') or '') + '|' + str(doc.get('链接') or '') + '|' + str(doc.get('标题') or '')


def _save(documents, vectors):
    RAG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_docs = DOCS_PATH.with_suffix('.jsonl.tmp')
    with tmp_docs.open('w', encoding='utf-8') as f:
        for doc in documents:
            f.write(json.dumps(doc, ensure_ascii=False) + '\n')
    os.replace(tmp_docs, DOCS_PATH)
    tmp_vec = VECTORS_PATH.with_suffix('.npy.tmp')
    np.save(tmp_vec, vectors)
    os.replace(str(tmp_vec) + '.npy', VECTORS_PATH)
    meta = {'模型': MODEL_NAME, '文档数': len(documents),
            '维度': int(vectors.shape[1])}
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding='utf-8')
    _CACHE.update({'mtime_docs': None, 'mtime_vec': None,
                   'docs': None, 'vectors': None})
    return meta


def append_index(documents):
    """只向量化新增文档，适合新闻实时增量更新。"""
    documents = [dict(x) for x in documents if _doc_text(x)]
    old_docs = load_documents()
    old_vectors = np.load(VECTORS_PATH).astype('float32') if VECTORS_PATH.exists() else None
    seen = {_dedup_key(x) for x in old_docs}
    fresh = []
    for doc in documents:
        key = _dedup_key(doc)
        if key not in seen:
            fresh.append(doc)
            seen.add(key)
    if not fresh:
        return {'模型': MODEL_NAME, '文档数': len(old_docs), '新增': 0}
    new_vectors = embed([_doc_text(x) for x in fresh])
    all_docs = old_docs + fresh
    all_vectors = new_vectors if old_vectors is None else np.vstack([old_vectors, new_vectors])
    meta = _save(all_docs, all_vectors)
    meta['新增'] = len(fresh)
    return meta


def build_index(documents, replace=True):
    """建立或合并索引。返回索引统计。"""
    documents = [dict(x) for x in documents if _doc_text(x)]
    if not documents:
        raise ValueError('没有可索引的文档')
    if not replace:
        return append_index(documents)

    vectors = embed([_doc_text(x) for x in documents])
    return _save(documents, vectors)


def load_documents():
    if not DOCS_PATH.exists():
        return []
    out = []
    with DOCS_PATH.open(encoding='utf-8') as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _load_cache():
    if not DOCS_PATH.exists() or not VECTORS_PATH.exists():
        return [], None
    md, mv = DOCS_PATH.stat().st_mtime_ns, VECTORS_PATH.stat().st_mtime_ns
    if (_CACHE['docs'] is None or _CACHE['vectors'] is None
            or _CACHE['mtime_docs'] != md or _CACHE['mtime_vec'] != mv):
        _CACHE['docs'] = load_documents()
        _CACHE['vectors'] = np.load(VECTORS_PATH).astype('float32')
        _CACHE['mtime_docs'] = md
        _CACHE['mtime_vec'] = mv
    return _CACHE['docs'], _CACHE['vectors']


def is_ready():
    return DOCS_PATH.exists() and VECTORS_PATH.exists()


def status():
    if not is_ready():
        return {'就绪': False, '文档数': 0, '模型': MODEL_NAME}
    try:
        meta = json.loads(META_PATH.read_text(encoding='utf-8'))
    except Exception:
        meta = {}
    return {'就绪': True, **meta}


def _parse_time(value):
    t = str(value or '').strip()
    try:
        dt = email.utils.parsedate_to_datetime(t)
        return dt.replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(t[:19], fmt)
        except Exception:
            pass
    return None


def _recency_bonus(doc):
    dt = _parse_time(doc.get('时间'))
    if not dt:
        return 0.0
    age_days = max(0.0, (datetime.now() - dt).total_seconds() / 86400)
    return 0.2 * math.exp(-age_days / 30.0)


def search(query, top_k=5, symbols=None):
    """向量检索。symbols 是币种集合时先按币种过滤。"""
    docs, vectors = _load_cache()
    if not docs or vectors is None or not str(query or '').strip():
        return []
    q = embed([query])[0]
    scores = vectors @ q
    want = {str(x).upper() for x in (symbols or []) if x}
    pairs = []
    for i, score in enumerate(scores):
        doc = docs[i]
        if want:
            ds = {str(x).upper() for x in (doc.get('币种') or [])}
            if not ds.intersection(want):
                continue
        final_score = float(score) + _recency_bonus(doc)
        pairs.append((final_score, i))
    pairs.sort(reverse=True)
    out = []
    for score, i in pairs[:max(1, int(top_k))]:
        item = dict(docs[i])
        item['_score'] = round(score, 4)
        out.append(item)
    return out


def format_sources(docs):
    if not docs:
        return ''
    lines = []
    for i, d in enumerate(docs, 1):
        title = str(d.get('标题') or '未命名资料')
        url = str(d.get('链接') or '')
        src = str(d.get('来源') or '')
        dt = str(d.get('时间') or '')[:19]
        meta = '｜'.join(x for x in (src, dt) if x)
        if url:
            lines.append(f'{i}. [{title}]({url})' + (f' — {meta}' if meta else ''))
        else:
            lines.append(f'{i}. {title}' + (f' — {meta}' if meta else ''))
    return '\n'.join(lines)


def format_context(docs, max_chars=700):
    if not docs:
        return ''
    lines = []
    for i, d in enumerate(docs, 1):
        text = str(d.get('摘要') or '').strip().replace('\n', ' ')
        if text == 'nan':
            text = ''
        if len(text) > max_chars:
            text = text[:max_chars] + '…'
        meta = []
        if d.get('来源'):
            meta.append(str(d['来源']))
        if d.get('时间'):
            meta.append(str(d['时间'])[:19])
        if d.get('币种'):
            meta.append(' '.join(d['币种'][:6]))
        lines.append(
            f"[{i}] {d.get('标题') or ''}\n"
            + (f"   资料：{text}\n" if text else '')
            + (f"   元数据：{'｜'.join(meta)}\n" if meta else '')
            + (f"   链接：{d.get('链接')}\n" if d.get('链接') else '')
        )
    return '\n'.join(lines)