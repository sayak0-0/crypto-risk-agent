# -*- coding: utf-8 -*-
"""后台任务：把长任务放到独立线程跑，避免被 Streamlit 的界面交互打断。

为什么需要这个（实测踩的坑）：
    Streamlit 的模型是「每点一下按钮，整个脚本从头重跑」。
    所以在「生成方案」（要 5-7 分钟）跑的过程中，用户去点「抓取新闻」，
    会直接打断并杀掉正在跑的任务 —— 用户的操作被白费了。

解法：长任务丢到后台线程，结果写到磁盘。
    · 界面不再阻塞，用户想点什么都行
    · 刷新页面、关掉浏览器再开，结果还在
    · 界面只负责轮询状态
"""
import json
import os
import threading
import time
import traceback
import uuid

from common import DATA_DIR

TASKS_PATH = os.path.join(DATA_DIR, '后台任务.json')
RESULT_DIR = os.path.join(DATA_DIR, '任务结果')
_LOCK = threading.Lock()

os.makedirs(RESULT_DIR, exist_ok=True)


def _read_all():
    if not os.path.exists(TASKS_PATH):
        return {}
    try:
        with open(TASKS_PATH, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_all(d):
    os.makedirs(os.path.dirname(TASKS_PATH), exist_ok=True)
    tmp = TASKS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TASKS_PATH)


def _update(task_id, **kw):
    """线程安全地更新任务状态。后台线程不能直接调 st.*，只能写文件。"""
    with _LOCK:
        d = _read_all()
        t = d.get(task_id) or {}
        t.update(kw)
        t['更新时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
        d[task_id] = t
        # 只保留最近 20 个任务，避免文件无限增长
        if len(d) > 20:
            for k in sorted(d, key=lambda x: d[x].get('开始时间', ''))[:-20]:
                d.pop(k, None)
        _write_all(d)


def result_path(task_id):
    return os.path.join(RESULT_DIR, f'{task_id}.json')


def start(name, fn, *args, **kwargs):
    """在后台线程启动一个任务，立即返回 task_id。

    fn 会收到一个 progress(msg) 回调，用来上报进度。
    fn 的返回值会被 JSON 序列化后存到磁盘。

    ⚠️ 调用约定：progress 是以**关键字参数**传进去的。
       所以 fn 的其他参数必须用关键字传，不能占位置：
           tasks.start('x', fn, symbol='BTC')   ✅
           tasks.start('x', fn, 'BTC')          ❌ 会撞上 progress
    """
    task_id = f'{name}-{time.strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:6]}'

    def progress(msg):
        msg = str(msg)[:200]
        with _LOCK:
            d = _read_all()
            t = d.get(task_id) or {}
            hist = list(t.get('进度历史') or [])
            hist.append(f"{time.strftime('%H:%M:%S')} {msg}")
            t['进度历史'] = hist[-40:]
            t['进度'] = msg
            t['更新时间'] = time.strftime('%Y-%m-%d %H:%M:%S')
            d[task_id] = t
            _write_all(d)

    def runner():
        try:
            _update(task_id, 状态='运行中', 进度='开始')
            out = fn(*args, progress=progress, **kwargs)
            try:
                with open(result_path(task_id), 'w', encoding='utf-8') as f:
                    json.dump(out, f, ensure_ascii=False, default=str)
                _update(task_id, 状态='完成', 进度='完成',
                        _有结果=True)
            except Exception as e:
                _update(task_id, 状态='完成',
                        进度='完成（结果太大无法保存）', _有结果=False,
                        错误=f'结果序列化失败：{e}')
        except Exception as e:
            _update(task_id, 状态='失败',
                    错误=str(e) if str(e) else f'{type(e).__name__}',
                    错误类型=type(e).__name__,
                    堆栈=traceback.format_exc()[-800:])

    with _LOCK:
        d = _read_all()
        d[task_id] = {'任务名': name, '状态': '排队中',
                      '开始时间': time.strftime('%Y-%m-%d %H:%M:%S'),
                      '进度': '等待启动'}
        _write_all(d)

    threading.Thread(target=runner, daemon=True).start()
    return task_id


def status(task_id):
    return (_read_all().get(task_id) or {}) if task_id else {}


def load_result(task_id):
    p = result_path(task_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def running_tasks():
    """还在跑的任务。"""
    return {k: v for k, v in _read_all().items()
            if v.get('状态') in ('排队中', '运行中')}


def latest_done(name=None):
    """最近一个完成的任务（可选按任务名过滤）。"""
    d = _read_all()
    done = [(k, v) for k, v in d.items() if v.get('状态') == '完成']
    if name:
        done = [(k, v) for k, v in done if v.get('任务名') == name]
    if not done:
        return None, {}
    k, v = max(done, key=lambda x: x[1].get('开始时间', ''))
    return k, v


def latest(name=None):
    """最近一个任务（不论状态）。"""
    d = _read_all()
    if name:
        d = {k: v for k, v in d.items() if v.get('任务名') == name}
    if not d:
        return None, {}
    k, v = max(d.items(), key=lambda x: x[1].get('开始时间', ''))
    return k, v


def clear_finished():
    """清掉已完成任务的记录（结果文件保留）。"""
    with _LOCK:
        d = _read_all()
        d = {k: v for k, v in d.items()
             if v.get('状态') in ('排队中', '运行中')}
        _write_all(d)