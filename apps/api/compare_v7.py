"""对比 v6 / v7 / 第三方验证版（audioId_122）— 三栏对比。"""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent / "asr_export"


def load_jsonl(name):
    path = BASE / name
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def load_validated():
    path = BASE / "audioId_122_validated.txt"
    if not path.exists():
        return None
    segments = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("文件") or line.startswith("转写") or line.startswith("角色") or line.startswith("="):
                continue
            if line.startswith("["):
                try:
                    ts_end = line.index("]")
                    ts = line[1:ts_end]
                    rest = line[ts_end + 1:].strip()
                    role = ""
                    text = rest
                    if rest.startswith("【"):
                        role_end = rest.index("】")
                        role = rest[1:role_end]
                        text = rest[role_end + 1:]
                    parts = ts.split(":")
                    mins = int(parts[0])
                    secs = float(parts[1])
                    begin_ms = int((mins * 60 + secs) * 1000)
                    segments.append({"role": role, "begin": begin_ms, "text": text.strip()})
                except (ValueError, IndexError):
                    continue
    return segments


def stats(segs):
    total = len(segs)
    chars = sum(len(s["text"]) for s in segs)
    roles = {}
    for s in segs:
        r = s.get("role", "未知")
        roles[r] = roles.get(r, 0) + 1
    return total, chars, roles


def format_time(ms):
    s = ms / 1000
    m = int(s // 60)
    sec = s % 60
    return f"{m:02d}:{sec:05.2f}"


def seg_rows(segs):
    rows = []
    for s in segs:
        begin = format_time(s.get("begin", 0))
        role = s.get("role", "")
        text = s.get("text", "")
        role_cls = ("role-kefu" if "客服" in role else
                    "role-kehu" if "客户" in role else
                    "role-doctor" if "医生" in role else "role-other")
        rows.append(
            f'<tr><td class="ts">{begin}</td>'
            f'<td class="{role_cls}">{role}</td>'
            f'<td>{text}</td></tr>'
        )
    return "\n".join(rows)


def build_html():
    v6_data = load_jsonl("audioId_122_v6.jsonl")
    v7_data = load_jsonl("audioId_122_v7.jsonl")
    v6_segs = v6_data["transcribeResult"] if v6_data else []
    v7_segs = v7_data["transcribeResult"] if v7_data else []
    val_segs = load_validated() or []

    v6_total, v6_chars, v6_roles = stats(v6_segs)
    v7_total, v7_chars, v7_roles = stats(v7_segs)
    val_total, val_chars, val_roles = stats(val_segs)

    # v7 vs v6 improvements
    improvements = []
    if "外切" in str(v7_segs) and "位期" in str(v6_segs):
        improvements.append("位期 → 外切 (医学术语修正)")
    if "提升" in str(v7_segs):
        improvements.append("提胜 → 提升 (同音字修正)")
    if "72110154" in str(v7_segs):
        improvements.append("七二幺幺零幺五四 → 72110154 (数字转换)")
    if "60763431" in str(v7_segs):
        improvements.append("六零七六三四三幺 → 60763431 (数字转换)")
    if "64岁" in str(v7_segs):
        improvements.append("六十四岁 → 64岁 (数字转换)")
    if "30分钟" in str(v7_segs):
        improvements.append("三十分钟 → 30分钟 (数字转换)")
    if "25分钟" in str(v7_segs):
        improvements.append("二十五分钟 → 25分钟 (数字转换)")
    if "润致" in str(v7_segs):
        improvements.append("润制 → 润致 (产品名修正)")
    if "领新珂" in str(v7_segs):
        improvements.append("领星科 → 领新珂 (品牌名修正)")
    if "有券" in str(v7_segs):
        improvements.append("有劝 → 有券 (同音字修正)")
    if "大概讲" in str(v7_segs):
        improvements.append("大纲讲 → 大概讲 (同音字修正)")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>ASR 对比: audioId_122 (v6 vs v7 vs 第三方)</title>
<style>
    body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 20px; background: #f5f5f5; }}
    h1 {{ color: #333; font-size: 20px; }}
    .summary {{ background: #fff; padding: 15px; border-radius: 8px; margin: 15px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .summary table {{ border-collapse: collapse; width: 100%; }}
    .summary th, .summary td {{ padding: 8px 12px; border: 1px solid #ddd; text-align: center; }}
    .summary th {{ background: #f0f0f0; }}
    .highlight {{ background: #e8f5e9; font-weight: bold; }}
    .new {{ background: #e3f2fd; }}
    .columns {{ display: flex; gap: 10px; margin-top: 20px; }}
    .col {{ flex: 1; background: #fff; border-radius: 8px; padding: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow-x: auto; max-height: 80vh; overflow-y: auto; }}
    .col h2 {{ font-size: 14px; text-align: center; margin: 5px 0 10px; position: sticky; top: 0; background: #fff; padding: 5px 0; z-index: 1; }}
    .col table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .col th {{ background: #f0f0f0; padding: 3px 5px; border: 1px solid #eee; position: sticky; top: 28px; z-index: 1; }}
    .col td {{ padding: 2px 5px; border: 1px solid #eee; vertical-align: top; }}
    .ts {{ color: #999; white-space: nowrap; font-size: 10px; }}
    .role-kefu {{ color: #1565c0; font-weight: bold; white-space: nowrap; }}
    .role-kehu {{ color: #c62828; font-weight: bold; white-space: nowrap; }}
    .role-doctor {{ color: #2e7d32; font-weight: bold; white-space: nowrap; }}
    .role-other {{ color: #666; white-space: nowrap; }}
    .analysis {{ background: #fff3e0; padding: 15px; border-radius: 8px; margin: 15px 0; }}
    .analysis h3 {{ margin-top: 0; color: #e65100; }}
    .improvements {{ background: #e8f5e9; padding: 15px; border-radius: 8px; margin: 15px 0; }}
    .improvements h3 {{ margin-top: 0; color: #2e7d32; }}
    .improvements li {{ margin: 5px 0; }}
</style>
</head>
<body>
<h1>ASR 转写质量对比: audioId_122 — v6 vs v7 vs 第三方</h1>

<div class="summary">
<table>
<tr>
    <th>版本</th><th>段落数</th><th>总字数</th>
    <th>客服</th><th>客户</th><th>医生</th><th>引擎</th>
</tr>
<tr>
    <td>v6（SenseVoice原始）</td><td>{v6_total}</td><td>{v6_chars}</td>
    <td>{v6_roles.get("客服", 0)}</td><td>{v6_roles.get("客户", 0)}</td>
    <td>{v6_roles.get("医生", 0)}</td>
    <td>SenseVoice + LLM拆句</td>
</tr>
<tr class="new">
    <td><b>v7（SenseVoice+纠错）</b></td><td>{v7_total}</td><td>{v7_chars}</td>
    <td>{v7_roles.get("客服", 0)}</td><td>{v7_roles.get("客户", 0)}</td>
    <td>{v7_roles.get("医生", 0)}</td>
    <td>SenseVoice + LLM纠错+拆句</td>
</tr>
<tr class="highlight">
    <td>第三方验证版</td><td>{val_total}</td><td>{val_chars}</td>
    <td>{val_roles.get("客服", 0)}</td><td>{val_roles.get("客户", 0)}</td>
    <td>{val_roles.get("医生", 0)}</td>
    <td>未知</td>
</tr>
</table>
</div>

<div class="improvements">
<h3>v7 相比 v6 的改进</h3>
<ul>
{''.join(f"<li>{item}</li>" for item in improvements)}
</ul>
</div>

<div class="columns">
<div class="col">
<h2>v6 原始 ({v6_total}段/{v6_chars}字)</h2>
<table>
<tr><th>时间</th><th>角色</th><th>内容</th></tr>
{seg_rows(v6_segs)}
</table>
</div>
<div class="col" style="border: 2px solid #1565c0;">
<h2>v7 纠错版 ({v7_total}段/{v7_chars}字)</h2>
<table>
<tr><th>时间</th><th>角色</th><th>内容</th></tr>
{seg_rows(v7_segs)}
</table>
</div>
<div class="col">
<h2>第三方 ({val_total}段/{val_chars}字)</h2>
<table>
<tr><th>时间</th><th>角色</th><th>内容</th></tr>
{seg_rows(val_segs)}
</table>
</div>
</div>

</body>
</html>"""

    out = BASE / "audioId_122_v7_comparison.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved: {out}")
    print(f"v6: {v6_total} segments, {v6_chars} chars")
    print(f"v7: {v7_total} segments, {v7_chars} chars")
    print(f"Validated: {val_total} segments, {val_chars} chars")


if __name__ == "__main__":
    build_html()
