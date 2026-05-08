"""v7 ASR 导出：SenseVoice + LLM 纠错 + 数字转换 + 短句合并。

相比 v6 主要改进：
1. LLM 拆句时同步做医美领域纠错（位期→外切、提胜→提升等）
2. LLM 同步做中文数字→阿拉伯数字转换
3. 超短噪声段自动合并/过滤
4. 更大的 chunk 给 LLM 更多上下文
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path("src").resolve()))

AUDIO_DIR = Path(r"d:\pyspace\Agent\audio_raw")
OUTPUT_DIR = Path("asr_export")

from smart_badge_api.core.config import get_settings  # noqa: E402
_settings = get_settings()

LLM_BASE_URL = _settings.llm_base_url.rstrip("/")
LLM_API_KEY = _settings.llm_api_key
LLM_MODEL = _settings.llm_model

TAG_RE = re.compile(r"<\|[^|]*\|>")
SEGMENT_SPLIT_RE = re.compile(r"(?=<\|(?:zh|en|ja|ko|yue|nospeech)\|>)")
LANG_RE = re.compile(r"<\|(zh|en|ja|ko|yue|nospeech)\|>")
KEEP_LANGS = {"zh", "yue"}
MIN_VAD_DURATION_MS = 400
GARBAGE_RE = re.compile(r"^[\u3040-\u309F\u30A0-\u30FFa-zA-Z\s.,!?]+$")
# yue 段最小字数：低于此值的 yue 段直接丢弃（多为噪声碎片）
MIN_YUE_CHARS = 3

# ── 粤语字符 → 普通话预替换（SenseVoice 把普通话误识别为粤语时用） ──
_YUE_TO_MANDARIN = {
    "咁": "这么", "嘅": "的", "佢": "他", "嘢": "东西", "冇": "没",
    "嚟": "来", "系": "是", "啲": "的", "唔": "不", "㗎": "啊",
    "翻": "回", "睇": "看", "咩": "什么", "点解": "为什么",
    "而家": "现在", "嗰": "那", "度": "里",
}

def _yue_to_mandarin(text: str) -> str:
    """将粤语书写字符替换为普通话等价词。"""
    for yue, mand in _YUE_TO_MANDARIN.items():
        text = text.replace(yue, mand)
    return text

# ── 中文数字 → 阿拉伯数字 (简易规则) ──
_DIGIT_MAP = {
    "零": "0", "〇": "0", "幺": "1", "一": "1", "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}

def _normalize_phone_numbers(text: str) -> str:
    """将电话号码/卡号中的中文数字序列转成阿拉伯数字。
    例如：七二幺幺零幺五四 → 72110154, 六零七六三四三幺 → 60763431
    """
    digit_chars = set(_DIGIT_MAP.keys()) | {"十"}
    result = []
    i = 0
    while i < len(text):
        # 检查是否开始一串连续数字字符（≥3个说明是序号/号码）
        if text[i] in digit_chars and text[i] != "十":
            seq_start = i
            digits = []
            while i < len(text) and text[i] in _DIGIT_MAP:
                digits.append(_DIGIT_MAP[text[i]])
                i += 1
            if len(digits) >= 3:
                # 3+ 位连续 → 号码
                result.append("".join(digits))
            else:
                # 1-2 位退回原文
                result.append(text[seq_start:i])
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def _normalize_simple_numbers(text: str) -> str:
    """将常见中文数字表达转成阿拉伯数字。
    例如: 六十四 → 64, 三十 → 30, 二十五 → 25, 八点 → 8点
    """
    # X十Y → XY (e.g. 六十四 → 64)
    def _replace_xy(m):
        a = _DIGIT_MAP.get(m.group(1), "")
        b = _DIGIT_MAP.get(m.group(2), "")
        return str(int(a) * 10 + int(b))

    # X十 → X0 (e.g. 三十 → 30)
    def _replace_x0(m):
        a = _DIGIT_MAP.get(m.group(1), "")
        return str(int(a) * 10)

    # 十Y → 1Y (e.g. 十四 → 14)
    def _replace_1y(m):
        b = _DIGIT_MAP.get(m.group(1), "")
        return str(10 + int(b))

    text = re.sub(r"([一二两三四五六七八九])十([一二三四五六七八九])", _replace_xy, text)
    text = re.sub(r"([一二两三四五六七八九])十", _replace_x0, text)
    text = re.sub(r"十([一二三四五六七八九])", _replace_1y, text)

    # 单字 "X百" 暂不处理，只做 X点/X个/X岁 等
    for ch, d in _DIGIT_MAP.items():
        if ch in ("幺", "两", "〇"):
            continue
        # X岁, X点, X个月 等后接量词
        text = re.sub(rf"(?<![一二三四五六七八九十百千万]){re.escape(ch)}(?=岁|点|个|分钟|分|小时|天|年|月|盒|支|只|次|种)", d, text)

    return text


def _normalize_numbers(text: str) -> str:
    """组合数字规范化。"""
    text = _normalize_phone_numbers(text)
    text = _normalize_simple_numbers(text)
    return text


# ── ASR 转写 (SenseVoice + VAD) ──

def _sensevoice_transcribe(audio_path: str) -> list[dict]:
    """使用 FunASR SenseVoice + VAD 进行转写并获取时间戳。"""
    from funasr import AutoModel

    print("  Loading VAD model ...")
    vad_model = AutoModel(
        model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        device="cuda:0",
        disable_update=True,
    )

    print("  Running VAD ...")
    vad_result = vad_model.generate(input=audio_path)
    vad_segments: list[tuple[int, int]] = []
    if vad_result and isinstance(vad_result[0], dict):
        for seg in vad_result[0].get("value", []):
            if isinstance(seg, (list, tuple)) and len(seg) == 2:
                vad_segments.append((int(seg[0]), int(seg[1])))
    print(f"  VAD found {len(vad_segments)} segments")

    del vad_model
    import torch

    torch.cuda.empty_cache()

    print("  Loading SenseVoice model ...")
    asr_model = AutoModel(
        model="FunAudioLLM/SenseVoiceSmall",
        vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        vad_kwargs={"max_single_segment_time": 30000},
        hub="hf",
        device="cuda:0",
        disable_update=True,
    )

    print("  Running SenseVoice ASR (merge_vad=False) ...")
    result = asr_model.generate(
        input=audio_path,
        batch_size_s=300,
        merge_vad=False,
    )

    if not result:
        return []

    raw_text = result[0].get("text", "")
    raw_sections = SEGMENT_SPLIT_RE.split(raw_text)
    raw_sections = [s.strip() for s in raw_sections if s.strip()]
    print(f"  SenseVoice: {len(raw_sections)} sections vs {len(vad_segments)} VAD segments")

    # 1:1 映射 + 质量过滤
    utterances = []
    skipped = {"nospeech": 0, "lang": 0, "short_vad": 0, "empty": 0, "garbage": 0}
    n = min(len(raw_sections), len(vad_segments))
    for i in range(n):
        section = raw_sections[i]
        begin_ms, end_ms = vad_segments[i]

        if "<|nospeech|>" in section:
            skipped["nospeech"] += 1
            continue

        lang_m = LANG_RE.search(section)
        lang = lang_m.group(1) if lang_m else "unknown"
        if lang not in KEEP_LANGS:
            skipped["lang"] += 1
            continue

        duration_ms = end_ms - begin_ms
        if duration_ms < MIN_VAD_DURATION_MS:
            skipped["short_vad"] += 1
            continue

        clean = TAG_RE.sub("", section).strip()
        if not clean:
            skipped["empty"] += 1
            continue

        if GARBAGE_RE.match(clean):
            skipped["garbage"] += 1
            continue

        # yue 段特殊处理：短碎片丢弃，长内容做粤→普替换
        if lang == "yue":
            if len(clean) < MIN_YUE_CHARS:
                skipped["garbage"] += 1
                continue
            clean = _yue_to_mandarin(clean)

        # 数字规范化
        clean = _normalize_numbers(clean)

        utterances.append({
            "begin_ms": begin_ms,
            "end_ms": end_ms,
            "text": clean,
            "_lang": lang,  # 保留语言标签供 LLM 参考
        })

    total_chars = sum(len(u["text"]) for u in utterances)
    print(f"  Mapped: {len(utterances)} utterances, {total_chars} chars")
    print(f"  Skipped: {skipped}")
    return utterances


# ── LLM: 纠错 + 拆句 + 标点（合并为一步） ──
_CORRECT_SPLIT_SYSTEM_PROMPT = """你是中文语音转写后处理助手。你会收到若干段 ASR 语音识别的文本（来自医美/美容咨询场景的面对面对话录音）。

重要背景：所有说话人都说**普通话**（没有粤语）。部分说话人普通话不太标准，ASR 引擎将他们的语音误识别为粤语，产生了错误的粤语字符。标有 [方言误识] 的段落就是这种情况，你需要根据上下文推测说话人实际想说的普通话内容。

你有两个任务：
## 任务1：纠正 ASR 错误
根据医美场景上下文修正常见同音字错误，例如：
- "位期" → "外切"（眼袋外切手术）
- "提胜" → "提升"
- "润制" → "润致"
- "领星科" → "领新客"、"领新珂"
- "脸现在" → "脸，现在" 等断句修正
- "大纲讲" → "大概讲"
- "有劝" → "有券"
- "打提肾" → "打提升"
- "外戏" → "外切"
- 其他明显的同音错误请根据上下文修正

对于 [方言误识] 标记的段落，请特别注意：
- 这些内容原本是普通话，但被错误转写成了粤语字符或乱码
- 如果能根据上下文和发音猜出说话人想说什么，就纠正为正确的普通话
- 如果实在无法判断含义，保留原文即可
- 常见的误识别模式："这么"被写成"咁"、"是"被写成"系"、"他"被写成"佢"、"的"被写成"嘅"等

## 任务2：拆分成自然短句
1. 按照说话的自然停顿/话轮来拆分
2. 每句一般 5-30 个字（可以更短！）
3. 为每句话添加合理的中文标点
4. 短回应词（嗯、对、好、可以、噢、行）**必须**拆成独立的句子
5. 话题转换处也要拆分

## 输出格式
返回 JSON：{"result": [["第1段句子1", "句子2", ...], ["第2段句子1", ...], ...]}
- 外层数组与输入段落一一对应
- 内层数组是该段纠错+拆分后的各个短句
- 只返回 JSON，不要有其他文字"""


def _llm_correct_and_split(utterances: list[dict]) -> list[list[str]]:
    """LLM 同时做纠错和拆句，减少 API 调用。"""
    if not utterances:
        return []

    chunk_size = 15  # v7: 加大 chunk 给更多上下文
    all_results: list[list[str]] = []

    for start in range(0, len(utterances), chunk_size):
        chunk = utterances[start:start + chunk_size]
        lines = []
        for i, utt in enumerate(chunk):
            begin_sec = utt["begin_ms"] / 1000
            end_sec = utt["end_ms"] / 1000
            b_min, b_sec = int(begin_sec // 60), begin_sec % 60
            e_min, e_sec = int(end_sec // 60), end_sec % 60
            prefix = "[方言误识] " if utt.get("_lang") == "yue" else ""
            lines.append(f"{i+1}. [{b_min:02d}:{b_sec:04.1f}-{e_min:02d}:{e_sec:04.1f}] {prefix}{utt['text']}")

        user_prompt = (
            "请对以下医美咨询对话的语音转写文本进行纠错和拆句。"
            "这是客服、医生和客户之间的面对面对话，注意修正同音字错误，"
            "同时把夹杂的短回应（嗯、对、好、可以等）拆成独立句子：\n\n"
            + "\n".join(lines)
        )

        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    f"{LLM_BASE_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={
                        "model": LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": _CORRECT_SPLIT_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0,
                        "max_tokens": 8000,
                    },
                )
                resp.raise_for_status()

            content = resp.json()["choices"][0]["message"]["content"].strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            sb = content.find("{")
            eb = content.rfind("}")
            if sb != -1 and eb != -1:
                content = content[sb:eb + 1]

            parsed = json.loads(content)
            results = parsed.get("result", parsed)

            if isinstance(results, list) and len(results) == len(chunk):
                all_results.extend(results)
                total_subs = sum(len(r) for r in results)
                print(f"    Correct+Split chunk {start+1}-{start+len(chunk)}: OK ({total_subs} sub-sentences)")
            else:
                print(f"    Correct+Split chunk {start+1}-{start+len(chunk)}: mismatch ({len(results)} vs {len(chunk)}), fallback")
                all_results.extend([[u["text"]] for u in chunk])
        except Exception as e:
            print(f"    Correct+Split chunk {start+1}-{start+len(chunk)} failed: {e}, fallback")
            all_results.extend([[u["text"]] for u in chunk])

    return all_results


def _expand_splits_with_timing(utterances: list[dict], split_results: list[list[str]]) -> list[dict]:
    """将拆分结果按字符比例分配时间戳。"""
    expanded = []
    for utt, sentences in zip(utterances, split_results):
        if len(sentences) <= 1:
            text = sentences[0] if sentences else utt["text"]
            expanded.append({"begin_ms": utt["begin_ms"], "end_ms": utt["end_ms"], "text": text.strip()})
            continue

        total_chars = sum(len(s.strip()) for s in sentences)
        if total_chars == 0:
            expanded.append({"begin_ms": utt["begin_ms"], "end_ms": utt["end_ms"], "text": utt["text"]})
            continue

        total_ms = utt["end_ms"] - utt["begin_ms"]
        current_ms = utt["begin_ms"]
        for i, sent in enumerate(sentences):
            sent = sent.strip()
            if not sent:
                continue
            char_ratio = len(sent) / total_chars
            duration_ms = int(total_ms * char_ratio)
            end_ms = utt["end_ms"] if i == len(sentences) - 1 else current_ms + duration_ms
            expanded.append({"begin_ms": current_ms, "end_ms": end_ms, "text": sent})
            current_ms = end_ms

    return expanded


# ── v7 新增：短句合并/过滤 ──

def _merge_short_segments(utterances: list[dict], min_chars: int = 2) -> list[dict]:
    """合并/过滤超短噪声段。
    规则：
    - 纯单字且非有意义回应的，与相邻段合并
    - 连续多个超短段合并
    - 有意义的短回应（嗯、对、好、可以、是、行、啊）保留为独立段
    """
    MEANINGFUL_SHORT = {"嗯", "对", "好", "可以", "是", "行", "啊", "噢", "哦", "嗯嗯",
                        "好的", "是的", "对的", "行的", "可以的", "没有", "没", "谢谢",
                        "不是", "不", "哎", "诶", "喂", "嗯。", "对。", "好。"}

    if not utterances:
        return utterances

    result = []
    i = 0
    while i < len(utterances):
        utt = utterances[i]
        text = utt["text"].rstrip("，。！？、；：")

        # 有意义的短回应或足够长的文本 → 保留
        if text in MEANINGFUL_SHORT or len(text) >= min_chars:
            result.append(utt)
            i += 1
            continue

        # 单字碎片 → 尝试合并到前一个段
        if result:
            prev = result[-1]
            # 只在时间间隔很近时合并（< 2s）
            gap_ms = utt["begin_ms"] - prev["end_ms"]
            if gap_ms < 2000:
                result[-1] = {
                    "begin_ms": prev["begin_ms"],
                    "end_ms": max(prev["end_ms"], utt["end_ms"]),
                    "text": prev["text"] + utt["text"],
                }
                i += 1
                continue

        # 无法合并，丢弃单字碎片
        i += 1

    return result


# ── LLM 角色标注 ──
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
5. 直接返回 JSON 数组，每个元素是角色名，顺序与输入一一对应
6. 只返回 JSON 数组

例如输入 5 句话，返回：["客服","客户","客服","客户","客服"]"""


def _llm_label_speakers(utterances: list[dict]) -> list[str]:
    if not utterances:
        return []

    chunk_size = 60
    all_roles: list[str] = []

    for start in range(0, len(utterances), chunk_size):
        chunk = utterances[start:start + chunk_size]
        lines = []
        for i, utt in enumerate(chunk):
            begin_sec = utt["begin_ms"] / 1000
            mins = int(begin_sec // 60)
            secs = begin_sec % 60
            lines.append(f"{i+1}. [{mins:02d}:{secs:04.1f}] {utt['text']}")

        user_prompt = (
            f"以下是第 {start+1} 到 {start+len(chunk)} 句（共{len(chunk)}句），"
            "请标注每句的说话人角色：\n\n" + "\n".join(lines)
        )

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
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            roles = json.loads(content)
            if isinstance(roles, list) and len(roles) == len(chunk):
                all_roles.extend(roles)
                print(f"    Role chunk {start+1}-{start+len(chunk)}: OK")
            else:
                print(f"    Role chunk {start+1}-{start+len(chunk)}: mismatch, padding")
                roles_padded = (roles + ["未知"] * len(chunk))[:len(chunk)]
                all_roles.extend(roles_padded)
        except Exception as e:
            print(f"    Role chunk {start+1}-{start+len(chunk)} failed: {e}")
            all_roles.extend(["未知"] * len(chunk))

    return all_roles


def process_one(audio_path: Path):
    audio_id = audio_path.stem.replace("audioId_", "")
    print(f"\n{'='*60}")
    print(f"Processing {audio_path.name} (audioId={audio_id})")
    print(f"{'='*60}")

    t0 = time.time()

    # Step 1: SenseVoice ASR
    utterances = _sensevoice_transcribe(str(audio_path))
    if not utterances:
        print("  ERROR: No transcription result!")
        return None

    audio_duration = max(u["end_ms"] for u in utterances) / 1000
    elapsed_asr = time.time() - t0
    print(f"  ASR done: {len(utterances)} segments, {audio_duration:.0f}s audio in {elapsed_asr:.1f}s")

    # Step 2: LLM 纠错 + 拆句
    t1 = time.time()
    print("  Step 2: LLM correction + sentence splitting ...")
    split_results = _llm_correct_and_split(utterances)
    expanded = _expand_splits_with_timing(utterances, split_results)
    print(f"  Correct+Split done: {len(utterances)} → {len(expanded)} utterances ({time.time()-t1:.1f}s)")

    # Step 3: 短句合并/过滤
    before_merge = len(expanded)
    expanded = _merge_short_segments(expanded)
    print(f"  Merge: {before_merge} → {len(expanded)} utterances")

    # Step 4: LLM 角色标注
    t2 = time.time()
    print("  Step 4: LLM speaker role labeling ...")
    roles = _llm_label_speakers(expanded)
    print(f"  Roles done in {time.time()-t2:.1f}s")

    # Step 5: 组装输出
    transcribe_result = []
    for i, utt in enumerate(expanded):
        role = roles[i] if i < len(roles) else "未知"
        transcribe_result.append({
            "role": role,
            "begin": utt["begin_ms"],
            "end": utt["end_ms"],
            "text": utt["text"],
        })

    payload = {
        "audioId": int(audio_id) if audio_id.isdigit() else audio_id,
        "audioUrl": "",
        "audioStartTime": "",
        "audioEndTime": "",
        "FZUER": "",
        "transcribeResult": transcribe_result,
    }

    suffix = "_v7"
    out_jsonl = OUTPUT_DIR / f"audioId_{audio_id}{suffix}.jsonl"
    with open(out_jsonl, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    role_counts: dict[str, int] = {}
    for item in transcribe_result:
        r = item["role"]
        role_counts[r] = role_counts.get(r, 0) + 1

    total_chars = sum(len(item["text"]) for item in transcribe_result)
    total_time = time.time() - t0

    print(f"  Saved: {out_jsonl}")
    print(f"  Utterances: {len(transcribe_result)}, Chars: {total_chars}, Roles: {role_counts}")
    print(f"  Total: {total_time:.1f}s")

    # 可读文本
    txt_file = OUTPUT_DIR / f"audioId_{audio_id}{suffix}.txt"
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(f"文件: {audio_path.name} (v7 - SenseVoice + LLM纠错)\n")
        f.write(f"音频时长: {audio_duration:.0f}秒 ({audio_duration/60:.1f}分钟)\n")
        f.write(f"转写段落: {len(transcribe_result)}\n")
        f.write(f"总字数: {total_chars}\n")
        f.write(f"角色分布: {role_counts}\n")
        f.write("=" * 60 + "\n\n")
        for item in transcribe_result:
            begin_sec = item["begin"] / 1000
            mins = int(begin_sec // 60)
            secs = begin_sec % 60
            f.write(f"[{mins:02d}:{secs:05.2f}] 【{item['role']}】{item['text']}\n")

    return {
        "utterances": len(transcribe_result),
        "chars": total_chars,
        "roles": role_counts,
        "engine": "SenseVoice + LLM纠错 v7",
    }


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    target = AUDIO_DIR / "audioId_122.mp3"
    if not target.exists():
        print(f"ERROR: {target} not found!")
        return

    result = process_one(target)
    if result:
        print(f"\nDone! Engine: {result['engine']}")
        print(f"Utterances: {result['utterances']}, Chars: {result['chars']}, Roles: {result['roles']}")


if __name__ == "__main__":
    main()
