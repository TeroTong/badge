import type {
  AnalysisDetail,
  ConsultationProcessEvaluationCheckpoint,
  ConsultationProcessEvaluationSection,
  ConsultationResult,
  EvalDimension,
} from '@/api/analysis'
import type { ArchiveRecordingDetail as DingtalkArchiveRecordingDetail } from '@/api/archive-recordings'
import { sanitizeEvaluationDimensionSummary, sanitizeEvaluationSummary } from '@/utils/evaluation-summary'
import { formatRecordingDisplayName } from '@/utils/recording-display'

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function extractAgeText(value: unknown): string | null {
  const text = asText(value)
  if (!text) return null
  const match = text.match(/(\d{2,3}\s*多岁|\d{2,3}\s*岁(?:左右|以上|以下)?)/)
  return match ? match[1].replace(/\s+/g, '') : null
}

function extractSupportedAgeFromEvidence(value: unknown): string | null {
  const text = asText(value)
  if (!text) return null

  const candidates: Array<{ score: number; start: number; value: string }> = []
  const agePattern = /(\d{2,3})\s*(多?岁)/g
  let match: RegExpExecArray | null
  while ((match = agePattern.exec(text)) != null) {
    const age = Number(match[1])
    if (!Number.isFinite(age) || age < 10 || age > 100) continue

    const ageText = `${age}${match[2]}`
    const start = match.index
    const end = start + match[0].length
    const window = text.slice(Math.max(0, start - 28), Math.min(text.length, end + 28))
    const prefix = text.slice(Math.max(0, start - 10), start)

    if (/(到|到了|等到|变到|再到)\s*$/.test(prefix)) continue
    if (/(不可能让你到|变到|再到)$/.test(prefix)) continue
    if (/(不像|不是|不到|案例|顾客|别人|人家|很多人)/.test(window) && !/(今年多大|年龄|你现在|您现在|身份证)/.test(window)) {
      continue
    }

    let score = 0
    if (/(今年)?(多大|几岁|多少岁)|年龄|身份证/.test(window)) score += 10
    if (new RegExp(`(你|您|她|他)(今年|现在)?[^，。；;]{0,14}${ageText}`).test(window)) score += 8
    if (new RegExp(`(今年多大|年龄)[^，。；;]{0,12}(\\d{2,3}\\s*)?${ageText}`).test(window)) score += 8
    if (score > 0) candidates.push({ score, start, value: ageText })
  }

  candidates.sort((a, b) => b.score - a.score || a.start - b.start)
  return candidates[0]?.value ?? null
}

function resolveAgeText(value: unknown, evidence: unknown): string | null {
  return extractSupportedAgeFromEvidence(evidence) || extractAgeText(value)
}

function normalizeProfileTagCategory(value: unknown): string {
  const category = asText(value) || '未分类'
  if (category === '出生日期/年龄') return '出生日期'
  return category
}

function isBirthdateValue(value: string): boolean {
  return /(?:19|20)\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}[日号]?)?)?/.test(value)
}

function formatDurationMs(durationMs?: number | null, durationSeconds?: number | null): string {
  const ms = typeof durationMs === 'number' && durationMs > 0
    ? durationMs
    : typeof durationSeconds === 'number' && durationSeconds > 0
      ? durationSeconds * 1000
      : null
  if (ms == null) return '--:--'
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) {
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
  }
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function uniqueTexts(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const value of values) {
    const text = asText(value)
    if (!text || seen.has(text)) continue
    seen.add(text)
    result.push(text)
  }
  return result
}

type ProfileTagItem = {
  category: string
  value: string
  weight_level?: number
  evidence?: string
}

function isWeakProfileTagValue(value: string): boolean {
  const normalized = value.replace(/\s+/g, '')
  if (!normalized) return true
  return [
    /^(无|暂无|未提及|未明确|未知|其他|其它)$/,
    /^无(?:明显)?风险禁忌$/,
    /^未见(?:明显)?风险禁忌$/,
    /^无(?:相关)?(?:治疗史|医美史)$/,
    /^无(?:负面项目|负面设备|负面原材料|负面项目\/设备\/原材料)$/,
  ].some((pattern) => pattern.test(normalized))
}

function shouldReplaceProfileTag(current: ProfileTagItem, candidate: ProfileTagItem): boolean {
  const currentIsWeak = isWeakProfileTagValue(current.value)
  const candidateIsWeak = isWeakProfileTagValue(candidate.value)
  if (currentIsWeak !== candidateIsWeak) return !candidateIsWeak

  const currentHasEvidence = Boolean(asText(current.evidence))
  const candidateHasEvidence = Boolean(asText(candidate.evidence))
  if (currentHasEvidence !== candidateHasEvidence) return candidateHasEvidence

  const currentWeight = current.weight_level ?? Number.POSITIVE_INFINITY
  const candidateWeight = candidate.weight_level ?? Number.POSITIVE_INFINITY
  if (currentWeight !== candidateWeight) return candidateWeight < currentWeight

  if (current.value.length !== candidate.value.length) {
    return candidate.value.length > current.value.length
  }

  return false
}

function dedupeProfileTags(items: ProfileTagItem[]): ProfileTagItem[] {
  const order: string[] = []
  const map = new Map<string, ProfileTagItem>()

  for (const item of items) {
    const category = item.category.trim() || '未分类'
    const value = item.value.trim()
    if (!value) continue

    const candidate: ProfileTagItem = {
      category,
      value,
      weight_level: item.weight_level,
      evidence: asText(item.evidence) || undefined,
    }

    const key = `${category}\u0000${value}`
    const current = map.get(key)
    if (!current) {
      order.push(key)
      map.set(key, candidate)
      continue
    }

    if (shouldReplaceProfileTag(current, candidate)) {
      map.set(key, candidate)
    }
  }

  return order
    .map((key) => map.get(key))
    .filter((item): item is ProfileTagItem => Boolean(item))
}

function buildEvalDimensions(value: unknown): EvalDimension[] {
  return asArray(value)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => {
      const pointScore = asNumber(item.point_score)
      const score = asNumber(item.score)
      return {
        name: asText(item.name) || '未命名维度',
        score: score ?? (pointScore != null ? Math.round(pointScore * 10 * 100) / 100 : undefined),
        point_score: pointScore ?? (score != null ? Math.round((score / 10) * 100) / 100 : undefined),
        max_score: asNumber(item.max_score) ?? 1,
        comment: asText(item.comment) || undefined,
        status: asText(item.status) || '未达标',
        summary: sanitizeEvaluationDimensionSummary(asText(item.summary) || ''),
        issues: asArray(item.issues)
          .map((issue) => asRecord(issue))
          .filter((issue): issue is Record<string, unknown> => Boolean(issue))
          .map((issue) => ({
            description: asText(issue.description) || '未提供问题描述',
            evidence: asText(issue.evidence) || '',
          })),
      }
    })
}

function countEvalIssues(dimensions: EvalDimension[]): number {
  return dimensions.reduce((total, item) => {
    if ('issues' in item) return total + item.issues.length
    return total
  }, 0)
}

function buildConsultationResult(value: unknown): ConsultationResult {
  const source = asRecord(value)
  const chief = asRecord(source?.chief_complaint_and_indications)
  const profile = asRecord(source?.customer_profile_summary)
  const factors = asRecord(source?.deal_factors)
  const plan = asRecord(source?.recommended_plan)
  const outcome = asRecord(source?.deal_outcome)

  const rawProfileSummaryTags = asArray(profile?.tags)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      category: normalizeProfileTagCategory(item.category),
      value: asText(item.value) || '',
      weight_level: asNumber(item.weight_level) ?? undefined,
      evidence: asText(item.evidence) || undefined,
    }))
  const ageFromProfileSummaryTags = rawProfileSummaryTags
    .map((item) => extractAgeText(item.value) || extractAgeText(item.category))
    .find((item): item is string => Boolean(item))
  const ageEvidenceFromProfileSummaryTags = rawProfileSummaryTags.find(
    (item) => extractAgeText(item.value) || extractAgeText(item.category),
  )?.evidence
  const profileSummaryTags = dedupeProfileTags(rawProfileSummaryTags.filter((item) => {
    if (item.category === '年龄') return false
    if (item.category === '出生日期' && !isBirthdateValue(item.value)) return false
    return true
  }))
  const profileSummaryTagCount = asNumber(profile?.extracted_tag_count)
  const profileSummaryText = profileSummaryTags.length > 0
    ? asText(profile?.summary) || ''
    : ''

  return {
    chief_complaint_and_indications: {
      summary: asText(chief?.summary) || '',
      primary_demands: asArray(chief?.primary_demands).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
      standardized_indications: asArray(chief?.standardized_indications).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
    },
    customer_profile_summary: {
      summary: profileSummaryText,
      extracted_tag_count: profileSummaryTagCount != null
        ? Math.min(profileSummaryTagCount, profileSummaryTags.length)
        : profileSummaryTags.length,
      age: resolveAgeText(profile?.age, profile?.age_evidence) || ageFromProfileSummaryTags,
      age_evidence: asText(profile?.age_evidence) || ageEvidenceFromProfileSummaryTags,
      tags: profileSummaryTags,
    },
    deal_factors: {
      summary: asText(factors?.summary) || '',
      budget: asText(factors?.budget),
      concerns: asArray(factors?.concerns).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
      decision_factors: asArray(factors?.decision_factors).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
    },
    recommended_plan: {
      summary: asText(plan?.summary) || '',
      items: asArray(plan?.items)
        .map((item) => asRecord(item))
        .filter((item): item is Record<string, unknown> => Boolean(item))
        .map((item) => ({
          plan: asText(item.plan) || '',
          acceptance: asText(item.acceptance),
          evidence: asText(item.evidence),
        })),
    },
    deal_outcome: {
      status: asText(outcome?.status) || '未明确',
      summary: asText(outcome?.summary) || '',
      deal_items: asArray(outcome?.deal_items).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
      amount: asText(outcome?.amount),
      loss_reasons: asArray(outcome?.loss_reasons).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
    },
  }
}

function buildConsultationProcessCheckpoint(value: unknown): ConsultationProcessEvaluationCheckpoint | null {
  const source = asRecord(value)
  if (!source) return null
  return {
    code: asText(source.code) || '',
    name: asText(source.name) || '未命名检查点',
    point_score: asNumber(source.point_score),
    max_score: asNumber(source.max_score) ?? 1,
    status: asText(source.status) || '',
    summary: asText(source.summary) || '',
    evidence: asArray(source.evidence).map((item) => asText(item)).filter((item): item is string => Boolean(item)),
    issues: asArray(source.issues)
      .map((item) => asRecord(item))
      .filter((item): item is Record<string, unknown> => Boolean(item))
      .map((item) => ({
        description: asText(item.description) || '未提供问题描述',
        evidence: asText(item.evidence) || '',
      })),
  }
}

function buildConsultationProcessSection(value: unknown): ConsultationProcessEvaluationSection | null {
  const source = asRecord(value)
  if (!source) return null
  return {
    code: asText(source.code) || '',
    name: asText(source.name) || '未命名评价项',
    point_score: asNumber(source.point_score),
    max_score: asNumber(source.max_score) ?? 1,
    status: asText(source.status) || '',
    summary: asText(source.summary) || '',
    checkpoints: asArray(source.checkpoints)
      .map((item) => buildConsultationProcessCheckpoint(item))
      .filter((item): item is ConsultationProcessEvaluationCheckpoint => Boolean(item)),
  }
}

export function buildArchiveAnalysisDetail(detail: DingtalkArchiveRecordingDetail | null | undefined): AnalysisDetail | null {
  if (!detail) return null

  const result = asRecord(detail.analysis_result)
  if (!result) return null

  const summary = asRecord(detail.analysis_summary)
  const transcript = asRecord(detail.transcript)
  const customerPrimaryDemands = asRecord(result.customer_primary_demands)
  const standardizedIndications = asRecord(result.standardized_indications)
  const customerDemands = asRecord(result.customer_demands)
  const expectation = asRecord(customerDemands?.expectation)
  const productPreference = asRecord(customerDemands?.product_preference)
  const customerConcerns = asRecord(result.customer_concerns)
  const customerProfile = asRecord(result.customer_profile)
  const consumptionIntent = asRecord(result.consumption_intent)
  const staffRecommendations = asRecord(result.staff_recommendations)
  const evaluation = asRecord(result.consultation_evaluation)
  const consultationResult = buildConsultationResult(result.consultation_result)
  const consultationProcessEvaluation = {
    total_score: asNumber(asRecord(result.consultation_process_evaluation)?.total_score) ?? undefined,
    max_total_score: asNumber(asRecord(result.consultation_process_evaluation)?.max_total_score) ?? undefined,
    overall_score: asNumber(asRecord(result.consultation_process_evaluation)?.overall_score) ?? undefined,
    overall_summary: asText(asRecord(result.consultation_process_evaluation)?.overall_summary) || '',
    sections: asArray(asRecord(result.consultation_process_evaluation)?.sections)
      .map((item) => buildConsultationProcessSection(item))
      .filter((item): item is ConsultationProcessEvaluationSection => Boolean(item)),
  }

  const demandItems = asArray(customerPrimaryDemands?.items)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      priority: asNumber(item.priority) ?? 1,
      demand: asText(item.demand) || '未命名主诉',
      body_part: asText(item.body_part),
      evidence: asText(item.evidence) || '',
    }))

  const indicationItems = asArray(standardizedIndications?.items)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      department_code: asText(item.department_code) || '',
      department_name: asText(item.department_name) || '',
      indication_code: asText(item.indication_code) || '',
      indication_name: asText(item.indication_name) || '',
      body_part_code: asText(item.body_part_code) || '',
      body_part_name: asText(item.body_part_name) || '',
      evidence: asText(item.evidence) || '',
    }))

  const concernItems = asArray(customerConcerns?.items)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      type: asText(item.type) || '未分类',
      content: asText(item.content) || '',
      evidence: asText(item.evidence) || '',
    }))

  const rawTagItems = asArray(customerProfile?.tags)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      category: normalizeProfileTagCategory(item.category),
      value: asText(item.value) || '',
      weight_level: asNumber(item.weight_level) ?? undefined,
      evidence: asText(item.evidence) || undefined,
    }))
  const ageFromTags = rawTagItems
    .map((item) => extractAgeText(item.value) || extractAgeText(item.category))
    .find((item): item is string => Boolean(item))
  const ageEvidenceFromTags = rawTagItems.find((item) => extractAgeText(item.value) || extractAgeText(item.category))?.evidence
  const tagItems = dedupeProfileTags(rawTagItems.filter((item) => {
    if (item.category === '年龄') return false
    if (item.category === '出生日期' && !isBirthdateValue(item.value)) return false
    return true
  }))

  const recommendationItems = asArray(staffRecommendations?.items)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      recommendation: asText(item.recommendation) || '未命名推荐',
      product_or_solution: asText(item.product_or_solution),
      body_part: asText(item.body_part),
      evidence: asText(item.evidence) || '',
      customer_response: asText(item.customer_response) || '未明确回应',
      demand_priority: asArray(item.demand_priority)
        .map((entry) => asNumber(entry))
        .filter((entry): entry is number => entry != null),
    }))

  const focusAreas = asArray(customerDemands?.focus_areas)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .map((item) => ({
      area: asText(item.area) || '未命名部位',
      surface_need: asText(item.surface_need) || '',
      deep_need: asText(item.deep_need) || '',
      discovery_process: asText(item.discovery_process) || '',
    }))

  const dimensions = buildEvalDimensions(evaluation?.dimensions)
  const processIssueCount = consultationProcessEvaluation.sections.reduce(
    (total, section) => total + section.checkpoints.reduce((sum, checkpoint) => sum + checkpoint.issues.length, 0),
    0,
  )
  const evalIssueCount = processIssueCount || countEvalIssues(dimensions)
  const durationMs = detail.duration_ms ?? asNumber(transcript?.durationMs) ?? asNumber(transcript?.duration_ms) ?? 0

  return {
    file_id: detail.stage_key || detail.file_id,
    recorded_at: detail.create_time || null,
    audio_start_time: null,
    audio_end_time: null,
    duration_ms: durationMs,
    duration_display: formatDurationMs(durationMs, detail.duration_seconds),
    segment_count: detail.utterance_count ?? asArray(transcript?.utterances).length,
    overall_score:
      consultationProcessEvaluation.overall_score
      ?? asNumber(evaluation?.overall_score)
      ?? asNumber(summary?.overall_score)
      ?? 0,
    eval_issue_count: evalIssueCount,
    overall_summary: sanitizeEvaluationSummary(
      consultationProcessEvaluation.overall_summary
      || asText(evaluation?.overall_summary)
      || asText(summary?.overall_summary)
      || '',
    ),
    dialogue_type: asText(expectation?.dialogue_type) || asText(summary?.dialogue_type) || '',
    primary_demand_summary: consultationResult.chief_complaint_and_indications.summary || asText(customerPrimaryDemands?.summary),
    focus_areas: uniqueTexts([
      ...focusAreas.map((item) => item.area),
      ...indicationItems.map((item) => item.body_part_name),
      ...asArray(summary?.focus_areas).map((item) => asText(item)),
    ]),
    recommendation_count: consultationResult.recommended_plan.items.length || recommendationItems.length,
    standardized_indication_count: indicationItems.length,
    indication_names: uniqueTexts(indicationItems.map((item) => item.indication_name)),
    concern_count: consultationResult.deal_factors.concerns.length || concernItems.length,
    tag_count: consultationResult.customer_profile_summary.extracted_tag_count || tagItems.length,
    weight_1_tag_count: tagItems.filter((item) => item.weight_level === 1).length,
    consumption_intent_present: Boolean(consumptionIntent),
    inference_note: uniqueTexts([
      asText(customerPrimaryDemands?.inference_note),
      asText(standardizedIndications?.inference_note),
      asText(customerDemands?.inference_note),
      asText(customerConcerns?.inference_note),
      asText(customerProfile?.inference_note),
    ])[0] ?? null,
    analysis_version: 'new',
    recording_file_name: formatRecordingDisplayName(detail.display_file_name, detail.create_time),
    transcript: detail.transcript ?? null,
    customer_primary_demands: {
      inference_note: asText(customerPrimaryDemands?.inference_note),
      summary: asText(customerPrimaryDemands?.summary) || '暂无主诉摘要',
      items: demandItems,
    },
    staff_recommendations: {
      summary: asText(staffRecommendations?.summary) || '暂无推荐摘要',
      items: recommendationItems,
    },
    standardized_indications: {
      inference_note: asText(standardizedIndications?.inference_note),
      summary: asText(standardizedIndications?.summary) || '对话中未识别出可标准化的适应症',
      items: indicationItems,
    },
    customer_demands: {
      inference_note: asText(customerDemands?.inference_note),
      focus_areas: focusAreas,
      expectation: {
        dialogue_type: asText(expectation?.dialogue_type) || '',
        entry_state: asText(expectation?.entry_state) || '',
        exit_state: asText(expectation?.exit_state) || '',
        turning_points: asArray(expectation?.turning_points)
          .map((item) => asText(item))
          .filter((item): item is string => Boolean(item)),
        specific_standards: asText(expectation?.specific_standards),
      },
      product_preference: {
        preferred_products: asArray(productPreference?.preferred_products)
          .map((item) => asText(item))
          .filter((item): item is string => Boolean(item)),
        information_sources: asArray(productPreference?.information_sources)
          .map((item) => asText(item))
          .filter((item): item is string => Boolean(item)),
        comparison_factors: asArray(productPreference?.comparison_factors)
          .map((item) => asText(item))
          .filter((item): item is string => Boolean(item)),
        consultant_influence: asText(productPreference?.consultant_influence) || '',
      },
    },
    customer_concerns: {
      inference_note: asText(customerConcerns?.inference_note),
      summary: asText(customerConcerns?.summary) || '对话中未明确表达独立顾虑',
      items: concernItems,
    },
    customer_profile: {
      inference_note: asText(customerProfile?.inference_note),
      age: resolveAgeText(customerProfile?.age, customerProfile?.age_evidence) || ageFromTags,
      age_evidence: asText(customerProfile?.age_evidence) || ageEvidenceFromTags,
      tags: tagItems,
    },
    consumption_intent: consumptionIntent ? {
      budget: asText(consumptionIntent.budget),
      willingness: asText(consumptionIntent.willingness) || '未明确',
      decision_factors: asArray(consumptionIntent.decision_factors)
        .map((item) => asText(item))
        .filter((item): item is string => Boolean(item)),
      evidence: asArray(consumptionIntent.evidence)
        .map((item) => asText(item))
        .filter((item): item is string => Boolean(item)),
    } : null,
    consultation_evaluation: {
      total_score: asNumber(evaluation?.total_score) ?? undefined,
      max_total_score: asNumber(evaluation?.max_total_score) ?? undefined,
      overall_score: asNumber(evaluation?.overall_score) ?? undefined,
      overall_summary: sanitizeEvaluationSummary(asText(evaluation?.overall_summary) || asText(summary?.overall_summary) || undefined),
      dimensions,
    },
    consultation_result: consultationResult,
    consultation_process_evaluation: consultationProcessEvaluation,
  }
}
