"""说话人角色分类器 — 用 LLM 将 SPEAKER_XX 映射为语义角色。

采样策略：从每个说话人各取前几句话，发给 LLM 判断角色，
然后将映射关系应用到全部 utterance。单次调用仅消耗 ~1000 tokens。
"""

from __future__ import annotations

import json
import logging

import httpx

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)

# 有效角色标签（需与 schemas/segments.py 中的 VALID_SPEAKER_LABELS 保持一致）
VALID_ROLES = {"consultant", "doctor", "customer", "unknown"}

_SYSTEM_PROMPT = """判断医美咨询录音中每个 SPEAKER 的角色，只输出 JSON。
角色只能是：consultant（接待/咨询/报价人员）、doctor（医生/医助面诊意见）、customer（顾客/陪同顾客侧）、unknown。
无法稳定判断则 unknown。示例：{"SPEAKER_00":"consultant","SPEAKER_01":"customer"}"""


def _collect_samples(
    utterances: list[dict],
    max_per_speaker: int = 5,
) -> dict[str, list[str]]:
    """从每个说话人采集前 N 条发言（用于 LLM 分类）。"""
    samples: dict[str, list[str]] = {}
    for utt in utterances:
        speaker = utt.get("speaker", "unknown")
        if speaker == "unknown":
            continue
        if speaker not in samples:
            samples[speaker] = []
        if len(samples[speaker]) < max_per_speaker:
            text = utt.get("text", "").strip()
            if text:
                begin_sec = utt.get("begin_ms", 0) / 1000.0
                samples[speaker].append(f"[{begin_sec:.0f}s] {text}")
    return samples


def _build_prompt(samples: dict[str, list[str]]) -> str:
    """构建用户 prompt。"""
    lines = ["以下是医美咨询录音的部分转写内容：\n"]
    for speaker, texts in sorted(samples.items()):
        lines.append(f"【{speaker}】")
        for t in texts:
            lines.append(f"  {t}")
        lines.append("")
    lines.append("请判断每个 SPEAKER 的角色。")
    return "\n".join(lines)


def classify_speakers(utterances: list[dict]) -> list[dict]:
    """用 LLM 对 utterance 中的 SPEAKER_XX 做角色分类，原地替换标签。

    如果 LLM 调用失败或未配置，保留原始 SPEAKER_XX 标签不变。
    """
    settings = get_settings()
    if not settings.llm_api_key or not settings.llm_base_url:
        logger.warning("LLM not configured, skipping speaker classification")
        return utterances

    # 收集每个说话人的样本发言
    samples = _collect_samples(utterances)
    if len(samples) <= 1:
        # 只有 1 个或 0 个说话人，不需要分类
        logger.info("Only %d speaker(s) detected, skipping classification", len(samples))
        return utterances

    prompt = _build_prompt(samples)
    logger.info(
        "Classifying %d speakers with LLM (%s), samples: %s",
        len(samples),
        settings.llm_model,
        {k: len(v) for k, v in samples.items()},
    )

    try:
        # 同步 HTTP 调用（在线程池中运行，不需要 async）
        base_url = settings.llm_base_url.rstrip("/")
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()

        content = resp.json()["choices"][0]["message"]["content"].strip()
        # 提取 JSON（LLM 可能包裹在 markdown 代码块中）
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        mapping: dict[str, str] = json.loads(content)
        logger.info("LLM speaker role mapping: %s", mapping)

        # 验证并应用映射
        valid_mapping = {}
        for speaker_id, role in mapping.items():
            role_lower = role.lower().strip()
            if role_lower in VALID_ROLES:
                valid_mapping[speaker_id] = role_lower
            else:
                logger.warning("Invalid role '%s' for %s, keeping original", role, speaker_id)

        if valid_mapping:
            for utt in utterances:
                old = utt.get("speaker", "unknown")
                if old in valid_mapping:
                    utt["speaker"] = valid_mapping[old]

            role_counts = {}
            for utt in utterances:
                r = utt["speaker"]
                role_counts[r] = role_counts.get(r, 0) + 1
            logger.info("Speaker classification applied: %s", role_counts)

    except Exception as exc:
        logger.warning("LLM speaker classification failed: %s — keeping SPEAKER_XX labels", exc)

    return utterances
