# -*- coding: utf-8 -*-
"""统一的模型调用层。

只要对方兼容 OpenAI 接口格式，就能用。通过 .env 切换：

    LLM_BASE_URL=https://api.siliconflow.cn/v1     # 硅基流动（默认）
    LLM_API_KEY=sk-xxx
    LLM_MODEL=deepseek-ai/DeepSeek-V3

    # 换成 DeepSeek 官方：
    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_API_KEY=sk-xxx
    LLM_MODEL=deepseek-chat

    # 换成本地 Ollama（免费，不联网）：
    LLM_BASE_URL=http://localhost:11434/v1
    LLM_API_KEY=ollama
    LLM_MODEL=qwen2.5:7b

旧的 SILICONFLOW_* 变量仍然兼容，不用改。
"""
import requests

from common import get_proxy, get_env

DEFAULT_BASE_URL = 'https://api.siliconflow.cn/v1'
DEFAULT_MODEL = 'deepseek-ai/DeepSeek-V3'


def base_url():
    return (get_env('LLM_BASE_URL')
            or get_env('SILICONFLOW_BASE_URL')
            or DEFAULT_BASE_URL).rstrip('/')


def chat_url():
    return base_url() + '/chat/completions'


def models_url():
    return base_url() + '/models'


def api_key():
    return get_env('LLM_API_KEY') or get_env('SILICONFLOW_API_KEY')


def model_name(role=None, default=None):
    """取模型名。

    role='analyst' 时优先用 LLM_MODEL_ANALYST（四位分析师）
    role='chair'   时优先用 LLM_MODEL_CHAIR（主持人）
    都没配就回退到 LLM_MODEL。

    为什么要分开：分析师干的活简单（读几个数字、按格式输出），
    主持人干的活难（综合四方观点、抵抗从众）。好钢用在刀刃上。
    """
    if role == 'analyst':
        v = get_env('LLM_MODEL_ANALYST')
    elif role == 'chair':
        v = get_env('LLM_MODEL_CHAIR')
    elif role == 'planner':
        # 方案官干的活和主持人不同：读候选结构位、做选择、写理由。
        # 实测 V4-Pro 在方案官位置会超时（200 秒级），GLM-5.3 只要 70 秒且推理质量好。
        v = get_env('LLM_MODEL_PLANNER')
    else:
        v = None
    if not v and role == 'planner':
        v = get_env('LLM_MODEL_CHAIR')     # 没单独配 planner 就用主持人的
    return (v or get_env('LLM_MODEL') or get_env('SILICONFLOW_MODEL')
            or default or DEFAULT_MODEL)


def prompt_mode(role=None):
    """决定用「完整提示词」还是「小模型友好提示词」。

    可以手动指定 LLM_PROMPT_MODE=full / compact；
    不指定就按该角色的模型名自动判断（参数量小的用精简版）。
    """
    v = (get_env('LLM_PROMPT_MODE') or '').strip().lower()
    if v in ('full', 'compact'):
        return v
    m = model_name(role).lower()
    small = ('1.5b', '3b', '4b', '7b', '8b', '9b', '14b', 'mini', 'small', 'tiny')
    return 'compact' if any(t in m for t in small) else 'full'


def probe(timeout=120):
    """自检：让当前模型做一次结构化输出小测验，看它守不规矩。

    小模型最常见的毛病就是格式不遵守。与其猜，不如让它做一遍看结果。
    """
    import json as _json
    import re as _re
    import time as _time

    system = '你只输出 JSON，不加任何其他文字。'
    user = ('下面是一个加密货币期货的数据：\n'
            '现价 86000，资金费率 0.12%，账户多空比 2.1\n\n'
            '请输出 JSON，包含两个字段：\n'
            '- "方向"：只能填 偏多/偏空/中性/无法判断\n'
            '- "信心"：0 到 100 的整数\n'
            '只输出这个 JSON。')
    t0 = _time.time()
    try:
        text, usage, used = chat(system, user, timeout=timeout,
                                 temperature=0.2, max_tokens=200)
    except Exception as e:
        return {'通过': False, '模型': model_name(), '原因': f'调用失败：{e}'}
    elapsed = _time.time() - t0

    t = (text or '').strip()
    t2 = _re.sub(r'^```(?:json)?\s*', '', t)
    t2 = _re.sub(r'\s*```$', '', t2)
    parsed = None
    try:
        parsed = _json.loads(t2)
    except Exception:
        i, j = t.find('{'), t.rfind('}')
        if i >= 0 and j > i:
            try:
                parsed = _json.loads(t[i:j + 1])
            except Exception:
                pass
    ok_dir = isinstance(parsed, dict) and parsed.get('方向') in (
        '偏多', '偏空', '中性', '无法判断')
    ok_conf = isinstance(parsed, dict) and isinstance(parsed.get('信心'), int)
    return {
        '通过': bool(ok_dir and ok_conf),
        '模型': used,
        '耗时秒': round(elapsed, 1),
        '纯JSON无包裹': t.startswith('{'),
        '方向字段合规': bool(ok_dir),
        '信心字段合规': bool(ok_conf),
        '原始输出': t[:240],
        '用量': usage,
    }


def describe():
    """给界面看的当前配置说明（不泄露密钥）。"""
    key = api_key()
    masked = (key[:4] + '…' + key[-4:]) if key and len(key) > 8 else ('已配置' if key else '未配置')
    return {'服务地址': base_url(),
            '模型': model_name(),
            '分析师模型': model_name('analyst'),
            '主持人模型': model_name('chair'),
            'Key': masked, '已配置': bool(key)}


def chat(system, user, model=None, api_key_override=None, timeout=150,
         temperature=0.4, max_tokens=900):
    """调用模型。返回 (回复文本, 用量字典, 实际使用的模型名)。"""
    key = api_key_override or api_key()
    if not key:
        raise RuntimeError(
            '没有配置模型 API Key。在项目目录的 .env 里加一行：\n'
            'LLM_API_KEY=你的key\n'
            '（要用别的服务商就再加 LLM_BASE_URL 和 LLM_MODEL，'
            '支持任何 OpenAI 兼容接口，比如本地 Ollama）')
    used_model = model or model_name()
    body = {
        'model': used_model,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    try:
        r = requests.post(chat_url(), json=body, timeout=timeout,
                          headers={'Authorization': 'Bearer ' + key,
                                   'Content-Type': 'application/json'},
                          proxies=get_proxy())
    except requests.exceptions.Timeout:
        raise RuntimeError(f'模型响应超时（超过 {timeout} 秒）。'
                           '可以换个小一点的模型，或稍后重试。')
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f'连不上模型服务 {base_url()}。'
                           f'检查网络，或确认本地模型服务已启动。（{type(e).__name__}）')

    if r.status_code == 401:
        raise RuntimeError('模型服务拒绝了请求（401）：Key 不对或已失效。')
    if r.status_code == 402:
        raise RuntimeError(
            '模型服务返回「余额不足」（402）。\n\n'
            '去服务商后台充值即可；如果想省钱，可以：\n'
            '- 换成便宜的模型（在 .env 里改 LLM_MODEL）\n'
            '- 或改用本地模型（Ollama，免费），见 README')
    if r.status_code == 429:
        raise RuntimeError('触发限流（429），等一会儿再试。')
    if r.status_code >= 400:
        raise RuntimeError(f'模型调用失败（HTTP {r.status_code}）：{r.text[:300]}')

    try:
        data = r.json()
        text = data['choices'][0]['message']['content']
    except Exception:
        raise RuntimeError(f'模型返回的内容看不懂：{r.text[:300]}')
    return text, (data.get('usage') or {}), used_model


def list_models(api_key_override=None, timeout=20):
    """列出当前服务商可用的模型，方便挑一个写进 .env。"""
    key = api_key_override or api_key()
    if not key:
        raise RuntimeError('没有配置 API Key，无法列出模型。')
    r = requests.get(models_url(), headers={'Authorization': 'Bearer ' + key},
                     timeout=timeout, proxies=get_proxy())
    if r.status_code >= 400:
        raise RuntimeError(f'获取模型列表失败（HTTP {r.status_code}）：{r.text[:200]}')
    return [m.get('id') for m in (r.json().get('data') or [])]

# ---------------- 任意模型的客观题体检 ----------------
# 用「有标准答案」的题检验一个模型，比看它说得漂不漂亮可靠得多。
# 三道题分别对应本工具真正需要的能力，最后一道是核心陷阱题。

CHECKUP_TASKS = [
    {
        '名称': '事实提取',
        '题': ('数据：现价 86000，4小时MA20 84200。\n'
                  '现价在 MA20 上方还是下方？\n'
                  '输出 JSON：{"答案": "上方" 或 "下方"}'),
        '字段': '答案', '标准': '上方',
    },
    {
        '名称': '仓位计算',
        '题': ('本金 10000 USDT，单笔风险 1%，'
                  '开仓价 80000，止损价 78400。\n'
                  '公式：数量 = (本金×风险%) ÷ '
                  '(开仓价-止损价)\n'
                  '输出 JSON：{"数量": 数字}'),
        '字段': '数量', '标准': 0.0625, '容差': 0.05,
    },
    {
        '名称': '止损爆仓陷阱',
        '题': ('做多，开仓价 100，100 倍杠杆，'
                  '维持保证金率 0.005，止损设 95。\n'
                  '公式：爆仓价 = 开仓价 × (1 - 1/杠杆) '
                  '÷ (1 - 维持保证金率)\n'
                  '问：价格下跌时，是止损先触发'
                  '还是爆仓先触发？\n'
                  '输出 JSON：{"先触发": "止损" 或 "爆仓"}'),
        '字段': '先触发', '标准': '爆仓',
    },
]


def _extract_json(text):
    import json as _json
    import re as _re
    t = (text or '').strip()
    t2 = _re.sub(r'^```(?:json)?\s*', '', t)
    t2 = _re.sub(r'\s*```$', '', t2)
    for c in (t2, t[t.find('{'):t.rfind('}') + 1] if '{' in t else ''):
        try:
            return _json.loads(c)
        except Exception:
            continue
    return None


def checkup(model=None, api_key_override=None, timeout=180):
    """对任意模型跑 3 道客观题，返回逐题结果和总分。

    这是筛模型最可靠的方式：有标准答案，不看你说了什么，只看对不对。
    """
    used_model = model or model_name()
    results = []
    for t in CHECKUP_TASKS:
        item = {'题目': t['名称'], '模型': used_model}
        try:
            text, usage, _ = chat('你只输出 JSON，不加任何其他文字。', t['题'],
                                  model=used_model,
                                  api_key_override=api_key_override,
                                  timeout=timeout, temperature=0.2, max_tokens=300)
            obj = _extract_json(text)
        except Exception as e:
            item.update({'结果': '调用失败', '说明': str(e)[:120],
                         '标准答案': t['标准']})
            results.append(item)
            continue
        if not isinstance(obj, dict):
            # 解析失败时也要带上标准答案，否则界面上会显示成 None
            item.update({'结果': '解析失败',
                         '说明': (text or '（空回复）')[:100],
                         '标准答案': t['标准']})
            results.append(item)
            continue
        got = obj.get(t['字段'])
        if isinstance(t['标准'], float):
            try:
                ok = abs(float(got) - t['标准']) / t['标准'] < t.get('容差', 0.05)
            except (TypeError, ValueError):
                ok = False
        else:
            ok = got == t['标准']
        item.update({'结果': '正确' if ok else '错误',
                     '作答': str(got)[:40],
                     '标准答案': t['标准']})
        results.append(item)

    right = sum(1 for r in results if r['结果'] == '正确')
    return {'模型': used_model, '得分': f'{right}/{len(results)}',
            '正确率': round(right / len(results) * 100, 1),
            '是否通过': right == len(results), '明细': results}

# ---------------- 服务商预设 ----------------
# 只要对方提供 OpenAI 兼容接口，就能用。换服务商只要改 base_url + key。
# 模型名请用「拉取可用模型」现场获取 —— 各家模型名变动频繁，我不写死免得报错。
PROVIDER_PRESETS = {
    '硅基流动（默认）': {
        'base': 'https://api.siliconflow.cn/v1', 'need_key': True,
        '备注': '一个 key 用 50+ 个国产模型，便宜',
    },
    'DeepSeek 官方': {
        'base': 'https://api.deepseek.com', 'need_key': True,
        '备注': '官方直连，deepseek-chat / deepseek-reasoner',
    },
    '阿里通义千问': {
        'base': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'need_key': True,
        '备注': 'DashScope 兼容模式，qwen-max / qwen-plus',
    },
    '智谱 GLM': {
        'base': 'https://open.bigmodel.cn/api/paas/v4', 'need_key': True,
        '备注': 'GLM 系列',
    },
    '月之暗面 Kimi': {
        'base': 'https://api.moonshot.cn/v1', 'need_key': True,
        '备注': 'Kimi 系列',
    },
    '字节豆包（火山方舟）': {
        'base': 'https://ark.cn-beijing.volces.com/api/v3', 'need_key': True,
        '备注': '豆包系列',
    },
    'OpenAI': {
        'base': 'https://api.openai.com/v1', 'need_key': True,
        '备注': 'GPT / o 系列。国内需代理',
    },
    'Google Gemini': {
        'base': 'https://generativelanguage.googleapis.com/v1beta/openai', 'need_key': True,
        '备注': 'Gemini 系列。国内需代理',
    },
    '本地 Ollama（免费）': {
        'base': 'http://localhost:11434/v1', 'need_key': False,
        '备注': '不联网、不花钱。装好 Ollama 后 key 随便填（如 ollama）',
    },
    '其他（自己填）': {
        'base': '', 'need_key': True,
        '备注': '任何 OpenAI 兼容接口：Claude 兼容层 / Grok / vLLM / LM Studio 等',
    },
}


def provider_of(base_url_value=None):
    """反查当前配置对应哪个预设。"""
    cur = (base_url_value or base_url()).rstrip('/')
    for name, cfg in PROVIDER_PRESETS.items():
        if cfg['base'] and cfg['base'].rstrip('/') == cur:
            return name
    return '其他（自己填）'

# ---------------- 多模型协商：每个分析师一个模型 ----------------
# 为什么要这样：如果四个分析师都用同一个模型，它们的「分歧」只是
# 数据切片造成的，不是真的观点不同。用四个不同厂商的模型，
# 训练背景不同，分歧才是真分歧 —— 那才是「协商」的意义。
#
# 配置：.env 里写逗号分隔的四个模型名，对应 技术面/资金面/情绪面/风控官
#   LLM_MODELS_ANALYSTS=Qwen/Qwen3.5-27B,zai-org/GLM-5.3,deepseek-ai/DeepSeek-V4-Pro,Pro/moonshotai/Kimi-K2.6

DEFAULT_ANALYST_MODELS = [
    'Qwen/Qwen3.5-27B',              # 阿里
    'zai-org/GLM-5.3',               # 智谱
    'deepseek-ai/DeepSeek-V4-Pro',   # DeepSeek
    'Pro/moonshotai/Kimi-K2.6',      # 月之暗面
]


def analyst_models():
    """返回四个分析师各自用的模型名。"""
    raw = get_env('LLM_MODELS_ANALYSTS') or ''
    got = [m.strip() for m in raw.split(',') if m.strip()]
    if len(got) == 4:
        return got
    # 没配或配得不全，用默认四家不同的
    if got:
        for i in range(len(got), 4):
            got.append(DEFAULT_ANALYST_MODELS[i])
        return got
    return list(DEFAULT_ANALYST_MODELS)


def analyst_model(index):
    """第 index 个分析师（0-3）用哪个模型。"""
    ms = analyst_models()
    return ms[index] if 0 <= index < len(ms) else model_name('analyst')
