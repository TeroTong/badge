"""重新对 v7 结果做角色标注（修复 JSON 解析问题）。"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path("src").resolve()))

from smart_badge_api.core.config import get_settings  # noqa: E402
_settings = get_settings()

LLM_BASE_URL = _settings.llm_base_url.rstrip("/")
LLM_API_KEY = _settings.llm_api_key
LLM_MODEL = _settings.llm_model

OUTPUT_DIR = Path("asr_export")

_ROLE_SYSTEM_PROMPT = """你是医美咨询录音的说话人标注助手。你会收到一段录音的 ASR 转写结果（按时间顺序排列）。

请为每一句话标注说话人角色。可能的角色：
- 客服（美容顾问/咨询师/接待）：介绍项目、报价、引导客户、打电话查询
- 医生：提供专业医学建议、诊断
- 客户：咨询、提问、简短回应（嗯、对、好、可以等）

规则：
1. 根据对话内容和上下文推断角色
2. 这是面对面咨询，对话有自然的来回交替
3. 客户的短回应（嗯、对、好的、可以等）很常见，不要全归为客服
4. 如果客服在说一段长话，中间夹杂的"嗯""对"很可能是客户的回应
5. 返回一个 JSON 数组，每个元素是角色名（"客服"/"客户"/"医生"），顺序与输入一一对应
6. 只返回 JSON 数组，不要有其他文字，不要换行后再接一个数组

例如输入 5 句话，返回：["客服","客户","客服","客户","客服"]"""


def _parse_json_array(content: str) -> list:
    """健壮的 JSON 数组解析，处理 LLM 可能返回的各种格式问题。"""
    content = content.strip()
    # 去掉 markdown code block
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    # 找到第一个 [ 和与之匹配的 ]
    start = content.find("[")
    if start == -1:
        raise ValueError(f"No JSON array found in: {content[:200]}")

    depth = 0
    for i in range(start, len(content)):
        if content[i] == "[":
            depth += 1
        elif content[i] == "]":
            depth -= 1
            if depth == 0:
                json_str = content[start:i+1]
                return json.loads(json_str)

    raise ValueError(f"Unmatched brackets in: {content[:200]}")


def _llm_label_speakers(utterances: list[dict]) -> list[str]:
    chunk_size = 50  # 稍小的 chunk 减少出错概率
    all_roles: list[str] = []

    for start in range(0, len(utterances), chunk_size):
        chunk = utterances[start:start + chunk_size]
        lines = []
        for i, utt in enumerate(chunk):
            begin_sec = utt["begin"] / 1000
            mins = int(begin_sec // 60)
            secs = begin_sec % 60
            lines.append(f"{i+1}. [{mins:02d}:{secs:04.1f}] {utt['text']}")

        user_prompt = (
            f"以下是第 {start+1} 到 {start+len(chunk)} 句（共{len(chunk)}句），"
            "请标注每句的说话人角色。"
            f"请返回恰好 {len(chunk)} 个元素的 JSON 数组：\n\n" + "\n".join(lines)
        )

        max_retries = 2
        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=120) as client:
                    resp = client.post(
                        f"{LLM_BASE_URL}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                        json={
                            "model": LLM_MODEL,
                            "messages": [
                                {"role": "system", "content": _ROLE_SYSTEM_PROMPT},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0,
                            "max_tokens": 2000,
                        },
                    )
                    resp.raise_for_status()

                content = resp.json()["choices"][0]["message"]["content"].strip()
                roles = _parse_json_array(content)
                if isinstance(roles, list) and len(roles) == len(chunk):
                    all_roles.extend(roles)
                    print(f"    Role chunk {start+1}-{start+len(chunk)}: OK (attempt {attempt+1})")
                    break
                else:
                    print(f"    Role chunk {start+1}-{start+len(chunk)}: got {len(roles)} vs expected {len(chunk)} (attempt {attempt+1})")
                    if attempt == max_retries - 1:
                        roles_padded = (roles + ["未知"] * len(chunk))[:len(chunk)]
                        all_roles.extend(roles_padded)
            except Exception as e:
                print(f"    Role chunk {start+1}-{start+len(chunk)} attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    all_roles.extend(["未知"] * len(chunk))

    return all_roles


def main():
    # 读取 v7 JSONL
    jsonl_path = OUTPUT_DIR / "audioId_122_v7.jsonl"
    with open(jsonl_path, encoding="utf-8") as f:
        data = json.loads(f.readline())

    results = data["transcribeResult"]
    print(f"Loaded {len(results)} utterances from v7")

    # 重新标注
    t0 = time.time()
    roles = _llm_label_speakers(results)
    print(f"Role labeling done in {time.time()-t0:.1f}s")

    # 更新角色
    for i, item in enumerate(results):
        item["role"] = roles[i] if i < len(roles) else "未知"

    # 统计
    role_counts = {}
    for item in results:
        r = item["role"]
        role_counts[r] = role_counts.get(r, 0) + 1
    total_chars = sum(len(item["text"]) for item in results)
    print(f"Roles: {role_counts}")

    # 保存
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")

    audio_duration = max(r["end"] for r in results) / 1000
    txt_file = OUTPUT_DIR / "audioId_122_v7.txt"
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write("文件: audioId_122.mp3 (v7 - SenseVoice + LLM纠错)\n")
        f.write(f"音频时长: {audio_duration:.0f}秒 ({audio_duration/60:.1f}分钟)\n")
        f.write(f"转写段落: {len(results)}\n")
        f.write(f"总字数: {total_chars}\n")
        f.write(f"角色分布: {role_counts}\n")
        f.write("=" * 60 + "\n\n")
        for item in results:
            begin_sec = item["begin"] / 1000
            mins = int(begin_sec // 60)
            secs = begin_sec % 60
            f.write(f"[{mins:02d}:{secs:05.2f}] 【{item['role']}】{item['text']}\n")

    print(f"Updated: {jsonl_path}")
    print(f"Updated: {txt_file}")


if __name__ == "__main__":
    main()
