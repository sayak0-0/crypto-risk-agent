# -*- coding: utf-8 -*-
"""主持人能力专项测试。

之前测的都是算术题，测不出「综合判断」能力。这个测试专门考主持人真正要干的事：
  T1 抵抗多数   —— 四个分析师都偏多，但数据里埋了一个明显的空头事实，看它会不会盲从
  T2 抓数据错误 —— 辩论里有一方引用了错误数字，看它的「数据核对」能不能揪出来
  T3 拒绝硬答   —— 数据严重不足时，看它会不会硬给方向
  T4 可证伪条件 —— 必须给出带具体数字的认错条件

全部可客观判定，不靠「说得像不像」。
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm

# ---- 测试 1：抵抗多数。四个都偏多，但资金费率 0.15% 是极端高位（明显空头信号）----
T1_VIEWS = [
    {"分析师": "技术面", "方向": "偏多", "信心": 70, "核心理由": "价格86,000站稳MA20之上"},
    {"分析师": "情绪面", "方向": "偏多", "信心": 65, "核心理由": "多空比0.9偏空但价格不跌"},
    {"分析师": "风控官", "方向": "偏多", "信心": 60, "核心理由": "距近期低点有缓冲"},
    {"分析师": "资金面", "方向": "偏多", "信心": 55, "核心理由": "持仓量在增长"},
]
T1_CTX = {
    "币种": "BTCUSDT", "现价": "86,000 USDT",
    "24小时涨跌幅": "-0.10%",
    "资金费率（每8小时，单位%）": "0.1500%（正=多头付钱；常规基准约0.01%）",
    "账户多空比（>1=多头账户更多）": "0.93",
    "4小时ATR百分比": "2.4%",
}
T1_KEY = "0.15"      # 极端费率这个事实必须被提到

# ---- 测试 2：辩论里有一方引用了错误数字 ----
T2_DEBATE = {
    "看多第一轮": {"立场": "看多", "核心论证": "24小时涨跌幅为 +2.5%，多头强势，继续看涨"},
    "看空第一轮": {"立场": "看空", "核心论证": "资金费率高位，多头拥挤"},
    "看多反驳": {"对方的破绽": "对方过度解读费率", "我的回应": "+2.5%的涨幅证明多头占优"},
    "看空反驳": {"对方的破绽": "看多方引用的 +2.5% 与实际数据 -0.10% 严重不符",
                 "我的回应": "基于错误数据的结论不可信"},
}
T2_CTX = dict(T1_CTX, **{"24小时涨跌幅": "-0.10%"})
BULL_WRONG = "+2.5"
ACTUAL = "-0.10"

# ---- 测试 3：数据严重不足 ----
T3_VIEWS = [
    {"分析师": "技术面", "方向": "无法判断", "信心": 10, "核心理由": "数据不足"},
    {"分析师": "情绪面", "方向": "无法判断", "信心": 10, "核心理由": "只有费率，其他都不知道"},
]
T3_CTX = {"币种": "BTCUSDT", "资金费率（每8小时，单位%）": "0.0050%"}

# ---- 测试 4：可证伪条件 ----
T4_VIEWS = T1_VIEWS
T4_CTX = T1_CTX


def ask_chair(model, views, context, debate=None):
    sys_p = '你是严谨的金融市场分析主持人，负责汇总多位分析师的独立观点。'
    user = (f"四位分析师的结论：\n{json.dumps(views, ensure_ascii=False, indent=1)}\n\n"
            f"数据背景：{json.dumps(context, ensure_ascii=False, indent=1)}")
    if debate:
        user += (f"\n\n这里有一场多空辩论：\n{json.dumps(debate, ensure_ascii=False, indent=1)}\n\n"
                 "【强制步骤：数据核对】逐个核对辩论中引用的关键数字是否与数据背景一致。"
                 "如果有引用错误，明确指出是谁错了。")
    user += ('\n\n请输出 JSON：{"方向":"偏多/偏空/中性/无法判断","信心":0-100整数,'
             '"数据核对":"逐个核对辩论中的数字",'
             '"共识":"...","分歧":"...","综合判断":"...",'
             '"什么情况下我错了":"一个可验证的条件","给交易者的提醒":"..."}'
             '\n只输出 JSON，不要其他文字。')
    txt, usage, used = llm.chat(sys_p, user, model=model, timeout=240,
                                temperature=0.3, max_tokens=1200)
    t = (txt or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t), usage
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1]), usage
            except Exception:
                pass
    return None, usage


def grade(task, obj):
    """返回 True/False/None。"""
    if not isinstance(obj, dict):
        return None
    if task == "T1抵抗多数":
        # 必须提到 0.15% 这个极端费率，且不能给太高的信心
        blob = json.dumps(obj, ensure_ascii=False)
        mentioned = (T1_KEY in blob)
        conf = obj.get("信心")
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = None
        return bool(mentioned and conf is not None and conf <= 60)
    if task == "T2抓数据错误":
        blob = json.dumps(obj, ensure_ascii=False)
        # 必须同时提到错误值 +2.5 和正确值 -0.10
        return (BULL_WRONG in blob) and (ACTUAL in blob)
    if task == "T3拒绝硬答":
        return obj.get("方向") in ("无法判断", "中性")
    if task == "T4可证伪条件":
        cond = str(obj.get("什么情况下我错了") or "")
        # 必须有数字（具体到价格/百分比），而且不能是空话
        has_num = bool(re.search(r"\d", cond))
        return has_num and len(cond) >= 8
    return None


TASKS = [
    ("T1抵抗多数", lambda m: ask_chair(m, T1_VIEWS, T1_CTX)),
    ("T2抓数据错误", lambda m: ask_chair(m, T1_VIEWS, T2_CTX, T2_DEBATE)),
    ("T3拒绝硬答", lambda m: ask_chair(m, T3_VIEWS, T3_CTX)),
    ("T4可证伪条件", lambda m: ask_chair(m, T4_VIEWS, T4_CTX)),
]

MODELS = [
    "deepseek-ai/DeepSeek-V4-Pro",
    "zai-org/GLM-5.3",
    "Qwen/Qwen3.5-122B-A10B",
    "Qwen/Qwen3.6-27B",
    "Pro/moonshotai/Kimi-K2.6",
    "deepseek-ai/DeepSeek-V4-Flash",
]
RUNS = 3


def one(model, task_name, fn):
    t0 = time.time()
    try:
        obj, usage = fn(model)
        g = grade(task_name, obj)
        return {"模型": model, "题": task_name,
                "结果": "对" if g is True else ("错" if g is False else "解析失败"),
                "耗时": time.time() - t0,
                "作答": json.dumps(obj, ensure_ascii=False)[:160] if obj else "（无法解析）"}
    except Exception as e:
        return {"模型": model, "题": task_name, "结果": "错误",
                "耗时": time.time() - t0, "作答": f"{type(e).__name__}: {str(e)[:80]}"}


jobs = [(m, tn, fn) for m in MODELS for tn, fn in TASKS for _ in range(RUNS)]
print(f"共 {len(jobs)} 次调用（{len(MODELS)} 模型 × {len(TASKS)} 题 × {RUNS} 次）")
t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as pool:
    rows = list(pool.map(lambda a: one(*a), jobs))
print(f"总耗时 {time.time()-t0:.0f} 秒\n")

print("=" * 100)
print(f"  {'模型':<36}{'总分':>7}{'正确率':>8}{'稳定性':>8}   抗多数  抓错误  拒硬答  可证伪")
print("-" * 100)
summ = {}
for m in MODELS:
    rs = [r for r in rows if r["模型"] == m]
    ok = sum(1 for r in rs if r["结果"] == "对")
    per = {tn: sum(1 for r in rs if r["题"] == tn and r["结果"] == "对")
           for tn, _ in TASKS}
    stable = sum(1 for tn in per if per[tn] in (0, RUNS))
    summ[m] = {"正确": ok, "总": len(rs), "每题": per, "稳定": stable,
               "平均耗时": round(sum(r["耗时"] for r in rs) / len(rs), 1)}
    print(f"  {m:<36}{ok}/{len(rs):<5}{ok/len(rs)*100:>7.0f}%{stable}/{len(TASKS):>7}"
          f"   {per['T1抵抗多数']}/{RUNS}    {per['T2抓数据错误']}/{RUNS}"
          f"     {per['T3拒绝硬答']}/{RUNS}     {per['T4可证伪条件']}/{RUNS}")

print("\n" + "=" * 100)
print("  逐题看谁翻车")
print("=" * 100)
for tn, _ in TASKS:
    print(f"\n【{tn}】")
    for m in MODELS:
        rs = [r for r in rows if r["模型"] == m and r["题"] == tn]
        ok = sum(1 for r in rs if r["结果"] == "对")
        mark = "✅" if ok == RUNS else ("⚠️" if ok else "❌")
        print(f"  {mark} {m:<36} {ok}/{RUNS}   {rs[0]['作答'][:88]}")

print("\n" + "=" * 100)
print("  平均耗时")
print("=" * 100)
for m in sorted(MODELS, key=lambda x: summ[x]["平均耗时"]):
    print(f"  {m:<36} {summ[m]['平均耗时']:>7.1f} 秒")

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "主持人专项测试.json")
json.dump({"summary": summ, "rows": rows}, open(out, "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"\n明细已存 {out}")