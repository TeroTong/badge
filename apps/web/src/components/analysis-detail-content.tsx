import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Tag } from 'antd'
import { DownOutlined, RightOutlined, LinkOutlined } from '@ant-design/icons'

import {
  type ConsultationProcessEvaluationCheckpoint,
  type ConsultationProcessEvaluationSection,
  extractRecordingIdFromAnalysisFileId,
  type AnalysisDetail,
  type StandardizedIndicationItem,
} from '@/api/analysis'
import { ANALYSIS_TAG_CATALOG_GROUPS } from '@/constants/tag-catalog'
import type { TranscriptUtteranceLite } from '@/components/transcript-playback-panel'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

function formatChiefIndicationLine(item: StandardizedIndicationItem): string {
  const departmentPart = item.department_code
    ? `${item.department_name}（${item.department_code}）`
    : item.department_name
  const indicationPart = item.indication_code
    ? `${item.indication_name}（${item.indication_code}）`
    : item.indication_name
  const bodyPart = item.body_part_code
    ? `${item.body_part_name}（${item.body_part_code}）`
    : item.body_part_name
  return [departmentPart, indicationPart, bodyPart].filter(Boolean).join('｜')
}

function sapPreviewValue(value: unknown): string {
  return String(value ?? '').trim()
}

function sapPreviewText(value: AnalysisDetail['sap_consultation_preview']): string {
  const firstPayload = value?.payloads?.find((item) => Boolean(item && typeof item === 'object'))
  return sapPreviewValue(firstPayload?.text)
}

function normalizeEvidenceKey(value: string | null | undefined): string {
  return String(value ?? '')
    .trim()
    .replace(/\s+/g, '')
    .toLowerCase()
}

function valuesLooselyMatch(a: string | null | undefined, b: string | null | undefined): boolean {
  const left = normalizeEvidenceKey(a)
  const right = normalizeEvidenceKey(b)
  if (!left || !right) return false
  return left === right || left.includes(right) || right.includes(left)
}
const PRIORITY_COLORS = ['blue', 'purple', 'geekblue', 'volcano', 'magenta', 'gold', 'lime'] as const

function buildRecordingDetailLink(recordingLinkBase: string, recordingId: string, from = 'llm') {
  const qs = new URLSearchParams()
  if (from) qs.set('from', from)
  const query = qs.toString()
  return `${recordingLinkBase}/${recordingId}${query ? `?${query}` : ''}`
}

const EvidenceUtteranceContext = createContext<TranscriptUtteranceLite[]>([])
const EVIDENCE_CONTEXT_MAX_LINES = 3
const EVIDENCE_TIMESTAMP_TOLERANCE_MS = 45_000

type EvidenceContextLine = TranscriptUtteranceLite & {
  contextIndex: number
  isHit: boolean
}

type EvidenceAnchor = {
  timeMs: number | null
  text: string
}

function coerceNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function extractEvidenceUtterances(transcript: unknown): TranscriptUtteranceLite[] {
  if (!transcript || typeof transcript !== 'object') return []
  const obj = transcript as Record<string, unknown>
  const list = obj.utterances ?? obj.segments ?? obj.items
  if (!Array.isArray(list)) return []

  return list
    .map((raw): TranscriptUtteranceLite | null => {
      if (!raw || typeof raw !== 'object') return null
      const item = raw as Record<string, unknown>
      const beginMs = [
        item.begin_ms,
        item.beginMs,
        item.start_ms,
        item.startMs,
        item.begin_time,
        item.beginTime,
      ].map(coerceNumber).find((value): value is number => value != null) ?? null
      const endMs = [
        item.end_ms,
        item.endMs,
        item.stop_ms,
        item.stopMs,
        item.end_time,
        item.endTime,
      ].map(coerceNumber).find((value): value is number => value != null) ?? null
      const text = typeof item.text === 'string'
        ? item.text
        : typeof item.content === 'string'
          ? item.content
          : ''

      return {
        speaker: typeof item.speaker === 'string' ? item.speaker : (item.speaker_id != null ? String(item.speaker_id) : null),
        speaker_role: typeof item.speaker_role === 'string' ? item.speaker_role : null,
        speaker_business_role: typeof item.speaker_business_role === 'string' ? item.speaker_business_role : null,
        speaker_display_label: typeof item.speaker_display_label === 'string' ? item.speaker_display_label : null,
        speaker_staff_name: typeof item.speaker_staff_name === 'string' ? item.speaker_staff_name : null,
        speaker_identity_type: typeof item.speaker_identity_type === 'string' ? item.speaker_identity_type : null,
        speaker_id: item.speaker_id != null ? String(item.speaker_id) : null,
        text,
        begin_ms: beginMs,
        end_ms: endMs,
      }
    })
    .filter((item): item is TranscriptUtteranceLite => Boolean(item && item.text?.trim()))
}

function parseEvidenceTimeToMs(value: string): number | null {
  const parts = value.split(':').map((part) => Number(part))
  if (parts.some((part) => !Number.isFinite(part) || part < 0)) return null
  if (parts.length === 2) {
    const [minutes, seconds] = parts
    return (minutes * 60 + seconds) * 1000
  }
  if (parts.length === 3) {
    const [hours, minutes, seconds] = parts
    return (hours * 3600 + minutes * 60 + seconds) * 1000
  }
  return null
}

function extractEvidenceTimesMs(evidence: string): number[] {
  const result: number[] = []
  for (const match of evidence.matchAll(/\[(\d{1,3}:\d{2}(?::\d{2})?)(?:\s*[-~—至]\s*\d{1,3}:\d{2}(?::\d{2})?)?\]/g)) {
    const ms = parseEvidenceTimeToMs(match[1])
    if (ms != null) result.push(ms)
  }
  return Array.from(new Set(result)).sort((a, b) => a - b)
}

function normalizeEvidenceSnippet(value: string): string {
  return value
    .replace(/\s+/g, '')
    .replace(/[，。！？!?、,.;；:："'“”‘’（）()[\]【】]/g, '')
    .toLowerCase()
}

function extractEvidenceAnchors(evidence: string): EvidenceAnchor[] {
  const anchors: EvidenceAnchor[] = []
  const timestampedPattern = /\[(\d{1,3}:\d{2}(?::\d{2})?)(?:\s*[-~—至]\s*\d{1,3}:\d{2}(?::\d{2})?)?\]\s*([^[]*)/g
  for (const match of evidence.matchAll(timestampedPattern)) {
    const timeMs = parseEvidenceTimeToMs(match[1])
    const text = String(match[2] ?? '').replace(/\s+/g, ' ').trim()
    if (timeMs != null || text) anchors.push({ timeMs, text })
  }
  if (anchors.length > 0) return anchors
  return extractEvidenceTimesMs(evidence).map((timeMs) => ({ timeMs, text: '' }))
}

function utteranceBeginMs(utterance: TranscriptUtteranceLite): number | null {
  return typeof utterance.begin_ms === 'number' && Number.isFinite(utterance.begin_ms) ? utterance.begin_ms : null
}

function utteranceEndMs(utterance: TranscriptUtteranceLite): number | null {
  return typeof utterance.end_ms === 'number' && Number.isFinite(utterance.end_ms) ? utterance.end_ms : null
}

function findEvidenceHitIndex(utterances: TranscriptUtteranceLite[], targetMs: number): number {
  let bestIndex = -1
  let bestDistance = Number.POSITIVE_INFINITY

  for (let index = 0; index < utterances.length; index += 1) {
    const begin = utteranceBeginMs(utterances[index])
    if (begin == null) continue
    const end = utteranceEndMs(utterances[index]) ?? begin
    if (targetMs >= begin - 1000 && targetMs <= end + 1000) return index

    const distance = Math.min(Math.abs(targetMs - begin), Math.abs(targetMs - end))
    if (distance < bestDistance) {
      bestDistance = distance
      bestIndex = index
    }
  }

  return bestDistance <= EVIDENCE_TIMESTAMP_TOLERANCE_MS ? bestIndex : -1
}

function findEvidenceTextHitIndex(
  utterances: TranscriptUtteranceLite[],
  evidenceText: string,
  targetMs: number | null,
): number {
  const snippet = normalizeEvidenceSnippet(evidenceText)
  if (snippet.length < 8 && !isSelfContainedShortEvidenceText(snippet)) return -1

  let bestIndex = -1
  let bestScore = Number.POSITIVE_INFINITY
  for (let index = 0; index < utterances.length; index += 1) {
    const utteranceText = normalizeEvidenceSnippet(String(utterances[index].text ?? ''))
    if (!utteranceText) continue

    const exactContains = utteranceText.includes(snippet)
    const snippetContains = snippet.includes(utteranceText) && utteranceText.length >= 12
    if (!exactContains && !snippetContains) continue

    const begin = utteranceBeginMs(utterances[index])
    const end = utteranceEndMs(utterances[index]) ?? begin
    const distance = targetMs == null || begin == null
      ? 0
      : Math.min(Math.abs(targetMs - begin), end == null ? Number.POSITIVE_INFINITY : Math.abs(targetMs - end))
    if (targetMs != null && distance > EVIDENCE_TIMESTAMP_TOLERANCE_MS) continue

    const score = distance + (exactContains ? 0 : 5000) + (isCompleteEvidenceUtterance(utterances[index]) ? 0 : 1000)
    if (score < bestScore) {
      bestScore = score
      bestIndex = index
    }
  }
  return bestIndex
}

function isShortEvidenceAnswer(utterance: TranscriptUtteranceLite): boolean {
  const compact = String(utterance.text ?? '').replace(/\s+/g, '')
  if (!compact) return false
  if (isSelfContainedShortEvidenceText(compact)) return false
  if (compact.length <= 12) return true
  return /^(对|对的|嗯|嗯嗯|是|是的|可以|行|好|好的|没有|有|包括|就是|这个|那个|害怕|贵|太贵)[。！？!?，,、]*$/.test(compact)
}

function isSelfContainedShortEvidenceText(value: string): boolean {
  const compact = value.replace(/\s+/g, '')
  if (!compact) return false
  return (
    /(我|自己).{0,8}(想|考虑|希望|要|准备|打算).{0,12}(手术|微创|皮肤|做|打|动|微信)/.test(compact)
    || /(我|自己).{0,8}(做过|打过|动过|加过).{0,12}(手术|针|玻尿酸|双眼皮|微信)/.test(compact)
    || /加.{0,3}微信/.test(compact)
  )
}

function isCompleteEvidenceUtterance(utterance: TranscriptUtteranceLite): boolean {
  const compact = String(utterance.text ?? '').replace(/\s+/g, '')
  return (compact.length >= 18 || isSelfContainedShortEvidenceText(compact)) && !isShortEvidenceAnswer(utterance)
}

function buildEvidenceContextLines(evidence: string, utterances: TranscriptUtteranceLite[]): EvidenceContextLine[] {
  if (!evidence || utterances.length === 0) return []
  const hitIndexes = new Set<number>()
  for (const anchor of extractEvidenceAnchors(evidence)) {
    const textHitIndex = findEvidenceTextHitIndex(utterances, anchor.text, anchor.timeMs)
    const index = textHitIndex >= 0
      ? textHitIndex
      : anchor.timeMs == null
        ? -1
        : findEvidenceHitIndex(utterances, anchor.timeMs)
    if (index >= 0) hitIndexes.add(index)
  }
  if (hitIndexes.size === 0) return []

  const orderedHits = Array.from(hitIndexes).sort((a, b) => a - b)
  if (orderedHits.length > 1) {
    return orderedHits
      .slice(0, EVIDENCE_CONTEXT_MAX_LINES)
      .map((index) => ({
        ...utterances[index],
        contextIndex: index,
        isHit: true,
      }))
  }

  const completeHit = orderedHits.find((index) => isCompleteEvidenceUtterance(utterances[index]))
  if (completeHit != null) {
    return [{
      ...utterances[completeHit],
      contextIndex: completeHit,
      isHit: true,
    }]
  }

  const selectedIndexes = new Set<number>()
  const primaryHit = orderedHits[0]
  if (primaryHit > 0) selectedIndexes.add(primaryHit - 1)
  selectedIndexes.add(primaryHit)
  if (selectedIndexes.size < EVIDENCE_CONTEXT_MAX_LINES && primaryHit < utterances.length - 1) {
    selectedIndexes.add(primaryHit + 1)
  }
  for (const hitIndex of orderedHits.slice(1)) {
    selectedIndexes.add(hitIndex)
    if (selectedIndexes.size >= EVIDENCE_CONTEXT_MAX_LINES) break
  }

  return Array.from(selectedIndexes)
    .sort((a, b) => a - b)
    .slice(0, EVIDENCE_CONTEXT_MAX_LINES)
    .map((index) => ({
      ...utterances[index],
      contextIndex: index,
      isHit: hitIndexes.has(index),
    }))
}

function formatEvidenceMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '--:--'
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

const GENERIC_SPEAKER_LABELS = new Set([
  '客户',
  '主客户',
  '顾客',
  '患者',
  '访客',
  '同行人',
  '工牌本人',
  '咨询师',
  '员工',
  '员工同事',
  '前台',
  '医生',
  '护士',
  '其他在场人员',
  '未知',
])

function isRawSpeakerToken(value: string | null | undefined): boolean {
  return /^speaker[_-]?\d+$/i.test(String(value ?? '').trim())
}

function speakerIdentityKey(utterance: TranscriptUtteranceLite): string | null {
  const speakerId = utterance.speaker_id?.trim()
  if (speakerId) return speakerId
  const speaker = utterance.speaker?.trim()
  if (speaker && isRawSpeakerToken(speaker)) return speaker
  return null
}

function compactDisplayLabel(value: string | null | undefined): string {
  const label = String(value ?? '').trim()
  if (!label || isRawSpeakerToken(label)) return ''
  const withoutRole = label.replace(/[（(](?:工牌本人|咨询师|员工同事|员工|医生|护士|前台|客户|主客户|同行人|访客)[）)]/g, '').trim()
  return withoutRole || label
}

function roleSpeakerLabel(utterance: TranscriptUtteranceLite): string {
  const normalized = [
    utterance.speaker_role,
    utterance.speaker_business_role,
    utterance.speaker,
    utterance.speaker_identity_type,
  ]
    .map((value) => String(value ?? '').trim().toLowerCase())
    .filter(Boolean)
    .join(' ')

  if (/primary_customer|主客户/.test(normalized)) return '主客户'
  if (/visitor_companion|companion|同行/.test(normalized)) return '同行人'
  if (/customer|client|patient|visitor|客户|顾客|患者|访客/.test(normalized)) return '主客户'
  if (/badge_owner|工牌/.test(normalized)) return '工牌本人'
  if (/doctor|医生/.test(normalized)) return '医生'
  if (/frontdesk|reception|前台|客服/.test(normalized)) return '前台'
  if (/staff_peer|员工同事/.test(normalized)) return '员工同事'
  if (/consultant|advisor|sales|员工|咨询|顾问/.test(normalized)) return '咨询师'
  return ''
}

function buildEvidenceSpeakerLabelMap(utterances: TranscriptUtteranceLite[]): Map<string, string> {
  const labels = new Map<string, string>()
  for (const utterance of utterances) {
    const key = speakerIdentityKey(utterance)
    if (!key || labels.has(key)) continue

    const staffName = compactDisplayLabel(utterance.speaker_staff_name)
    const displayLabel = compactDisplayLabel(utterance.speaker_display_label)
    const candidate = staffName || displayLabel
    if (candidate && !GENERIC_SPEAKER_LABELS.has(candidate)) {
      labels.set(key, candidate)
    }
  }
  return labels
}

function evidenceSpeakerLabel(utterance: TranscriptUtteranceLite, labelMap: Map<string, string>): string {
  const key = speakerIdentityKey(utterance)
  if (key && labelMap.has(key)) return labelMap.get(key) as string

  const roleLabel = roleSpeakerLabel(utterance)
  const staffName = compactDisplayLabel(utterance.speaker_staff_name)
  if (staffName && !GENERIC_SPEAKER_LABELS.has(staffName)) return staffName

  const displayLabel = compactDisplayLabel(utterance.speaker_display_label)
  if (displayLabel && !GENERIC_SPEAKER_LABELS.has(displayLabel)) return displayLabel
  if (roleLabel) return roleLabel
  if (displayLabel) return displayLabel
  if (staffName) return staffName
  return utterance.speaker || '未知'
}

function fullSpeakerLabel(utterance: TranscriptUtteranceLite, labelMap: Map<string, string>): string {
  const compactLabel = evidenceSpeakerLabel(utterance, labelMap)
  const roleLabel = roleSpeakerLabel(utterance)
  if (roleLabel && compactLabel !== roleLabel) return `${compactLabel}（${roleLabel}）`
  return compactLabel
}

function EvidenceToggle({
  evidence,
  recordingId,
  recordingLinkBase,
}: {
  evidence: string
  recordingId: string | null
  recordingLinkBase: string | null
}) {
  const [open, setOpen] = useState(false)
  const utterances = useContext(EvidenceUtteranceContext)
  const contextLines = useMemo(
    () => buildEvidenceContextLines(evidence, utterances),
    [evidence, utterances],
  )
  const speakerLabelMap = useMemo(() => buildEvidenceSpeakerLabelMap(utterances), [utterances])
  const fallbackEvidenceTime = useMemo(() => extractEvidenceTimesMs(evidence)[0] ?? null, [evidence])
  if (!evidence) return null

  return (
    <div className="ad-evidence">
      <button type="button" className="ad-evidence__toggle" onClick={() => setOpen(!open)}>
        {open ? <DownOutlined /> : <RightOutlined />}
        <span>{open ? '收起原文' : '查看录音原文'}</span>
      </button>
      {open && (
        <div className="ad-evidence__content">
          <div className="ad-evidence-context">
            <div className="ad-evidence-context__title">证据</div>
            <div className="ad-evidence-context__list">
              {contextLines.map((line) => (
                <span
                  key={`${line.contextIndex}-${utteranceBeginMs(line) ?? 'na'}`}
                  className={`ad-evidence-context__item${line.isHit ? ' ad-evidence-context__item--hit' : ''}`}
                >
                  <span className="ad-evidence-context__time">{formatEvidenceMs(utteranceBeginMs(line))}</span>
                  <span className="ad-evidence-context__speaker" title={fullSpeakerLabel(line, speakerLabelMap)}>
                    {evidenceSpeakerLabel(line, speakerLabelMap)}：
                  </span>
                  <span className="ad-evidence-context__text">{line.text}</span>
                </span>
              ))}
              {contextLines.length === 0 ? (
                <span className="ad-evidence-context__item">
                  <span className="ad-evidence-context__time">{formatEvidenceMs(fallbackEvidenceTime)}</span>
                  <span className="ad-evidence-context__speaker">原文：</span>
                  <span className="ad-evidence-context__text">{evidence}</span>
                </span>
              ) : null}
            </div>
          </div>
          {recordingId && recordingLinkBase && (
            <Link to={buildRecordingDetailLink(recordingLinkBase, recordingId, '')} className="ad-evidence__link">
              <LinkOutlined /> 跳转对话原文
            </Link>
          )}
        </div>
      )}
    </div>
  )
}

function SectionTitle({
  title,
  count,
  as = 'h2',
}: {
  title: string
  count?: ReactNode
  as?: 'h2' | 'span'
}) {
  const Component = as
  return (
    <Component className="ad-section__title">
      {title}
      {count != null && <span className="ad-section__count">{count}</span>}
    </Component>
  )
}

function AnalysisSection({
  title,
  count,
  embedded,
  defaultOpen = true,
  children,
}: {
  title: string
  count?: ReactNode
  embedded: boolean
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)

  if (!embedded) {
    return (
      <section className="ad-section">
        <SectionTitle title={title} count={count} />
        {children}
      </section>
    )
  }

  return (
    <section className="ad-section ad-section--collapsible">
      <button
        type="button"
        className="ad-section__toggle"
        onClick={() => setOpen((prev) => !prev)}
      >
        <SectionTitle title={title} count={count} as="span" />
        <span className="ad-section__chevron">{open ? <DownOutlined /> : <RightOutlined />}</span>
      </button>
      {open && <div className="ad-section__body">{children}</div>}
    </section>
  )
}

function formatPointScore(value: number | null, maxScore: number): string {
  if (value == null) return `0 / ${maxScore}`
  const normalized = Number.isInteger(value) ? value.toString() : value.toFixed(2).replace(/\.?0+$/, '')
  const maxText = Number.isInteger(maxScore) ? maxScore.toString() : maxScore.toFixed(2).replace(/\.?0+$/, '')
  return `${normalized} / ${maxText}`
}

function formatScoreWithUnit(value: number | null): string {
  if (value == null) return '0分'
  return `${Number.isInteger(value) ? value.toString() : value.toFixed(2).replace(/\.?0+$/, '')}分`
}

function getProcessPointScore(value: { point_score?: number | null }): number | null {
  return typeof value.point_score === 'number' && Number.isFinite(value.point_score) ? value.point_score : null
}

function getProcessMaxScore(value: { max_score?: number }): number {
  return typeof value.max_score === 'number' && Number.isFinite(value.max_score) && value.max_score > 0 ? value.max_score : 1
}

function getProcessVariant(value: { point_score?: number | null; max_score?: number; status?: string; issues?: { description: string; evidence: string }[] }) {
  const pointScore = getProcessPointScore(value)
  const maxScore = getProcessMaxScore(value)
  const hasIssues = (value.issues?.length ?? 0) > 0
  const status = value.status ?? ''
  if (hasIssues || /未达标|风险|问题/.test(status)) return 'alert'
  if (pointScore == null) return 'neutral'
  if (pointScore >= maxScore) return 'ok'
  if (pointScore <= 0) return 'alert'
  return 'neutral'
}

function getProcessStatusText(value: { point_score?: number | null; max_score?: number; status?: string; issues?: { description: string; evidence: string }[] }) {
  const status = value.status?.trim()
  if (status) return status
  const pointScore = getProcessPointScore(value)
  const maxScore = getProcessMaxScore(value)
  if (pointScore == null) return '待补充'
  if (pointScore >= maxScore) return '达标'
  if (pointScore <= 0) return '未达标'
  return '部分达标'
}

function formatProcessScore(value: { point_score?: number | null; max_score?: number }) {
  const pointScore = getProcessPointScore(value)
  const maxScore = getProcessMaxScore(value)
  return formatPointScore(pointScore, maxScore)
}

function ProcessCheckpointItem({
  item,
  recordingId,
  recordingLinkBase,
  simplified = false,
}: {
  item: ConsultationProcessEvaluationCheckpoint
  recordingId: string | null
  recordingLinkBase: string | null
  simplified?: boolean
}) {
  const variant = getProcessVariant(item)
  const statusText = getProcessStatusText(item)

  return (
    <div className={`ad-process-checkpoint ad-process-checkpoint--${variant}`}>
      <div className="ad-process-checkpoint__head">
        <div className="ad-process-checkpoint__title">
          <strong>{item.code ? `${item.code} ${item.name}` : item.name}</strong>
          <Tag color={variant === 'ok' ? 'success' : variant === 'alert' ? 'error' : 'default'}>{statusText}</Tag>
        </div>
        <span className="ad-process-checkpoint__score">{formatProcessScore(item)}</span>
      </div>
      {!simplified ? <p className="ad-process-checkpoint__summary">{item.summary || '当前未识别到明确过程评价结论。'}</p> : null}
      {item.evidence.length > 0 ? (
        <EvidenceToggle
          evidence={item.evidence.join('\n')}
          recordingId={recordingId}
          recordingLinkBase={recordingLinkBase}
        />
      ) : null}
      {(() => {
        // Drop issues whose description merely repeats the summary text (a common
        // backend pattern for unmet checkpoints) and which carry no extra evidence.
        const summaryText = (item.summary || '').trim()
        const visibleIssues = item.issues.filter((issue) => {
          const desc = (issue.description || '').trim()
          if (!desc && !issue.evidence) return false
          if (!issue.evidence && summaryText && desc === summaryText) return false
          return true
        })
        if (visibleIssues.length === 0) return null
        return (
          <div className="ad-process-checkpoint__issues">
            {visibleIssues.map((issue, index) => (
              <div key={`${item.code}-${index}`} className="ad-process-checkpoint__issue">
                <span className="ad-process-checkpoint__issue-dot" />
                <div>
                  {issue.description ? (
                    <p className="ad-process-checkpoint__issue-desc">{issue.description}</p>
                  ) : null}
                  {issue.evidence ? (
                    <EvidenceToggle
                      evidence={issue.evidence}
                      recordingId={recordingId}
                      recordingLinkBase={recordingLinkBase}
                    />
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )
      })()}
    </div>
  )
}

function ProcessEvaluationSectionBlock({
  section,
  recordingId,
  recordingLinkBase,
  simplified = false,
}: {
  section: ConsultationProcessEvaluationSection
  recordingId: string | null
  recordingLinkBase: string | null
  simplified?: boolean
}) {
  const variant = getProcessVariant(section)
  const statusText = getProcessStatusText(section)
  const [open, setOpen] = useState(() => !simplified || variant === 'alert')

  if (simplified) {
    return (
      <div className={`ad-process-section-card ad-process-section-card--${variant} ad-process-section-card--collapsible`}>
        <button type="button" className="ad-process-section-card__toggle" onClick={() => setOpen((prev) => !prev)}>
          <div className="ad-process-section-card__toggle-main">
            <div className="ad-process-section-card__head">
              <div>
                <h3 className="ad-process-section-card__title">{section.name}</h3>
              </div>
              <div className="ad-process-section-card__meta">
                <Tag color={variant === 'ok' ? 'success' : variant === 'alert' ? 'error' : 'default'}>{statusText}</Tag>
                <span>{formatProcessScore(section)}</span>
              </div>
            </div>
          </div>
          <span className="ad-process-section-card__chevron">{open ? <DownOutlined /> : <RightOutlined />}</span>
        </button>
        {open ? (
          <div className="ad-process-section-card__body">
            <div className="ad-process-checkpoint-list">
              {section.checkpoints.map((checkpoint) => (
                <ProcessCheckpointItem
                  key={`${section.code}-${checkpoint.code || checkpoint.name}`}
                  item={checkpoint}
                  recordingId={recordingId}
                  recordingLinkBase={recordingLinkBase}
                  simplified={simplified}
                />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <div className={`ad-process-section-card ad-process-section-card--${variant}`}>
      <div className="ad-process-section-card__head">
        <div>
          <h3 className="ad-process-section-card__title">{section.name}</h3>
          {!simplified ? <p className="ad-process-section-card__summary">{section.summary || '当前未识别到该阶段的明确动作总结。'}</p> : null}
        </div>
        <div className="ad-process-section-card__meta">
          <Tag color={variant === 'ok' ? 'success' : variant === 'alert' ? 'error' : 'default'}>{statusText}</Tag>
          <span>{formatProcessScore(section)}</span>
        </div>
      </div>
      <div className="ad-process-checkpoint-list">
        {section.checkpoints.map((checkpoint) => (
          <ProcessCheckpointItem
            key={`${section.code}-${checkpoint.code || checkpoint.name}`}
            item={checkpoint}
            recordingId={recordingId}
            recordingLinkBase={recordingLinkBase}
            simplified={simplified}
          />
        ))}
      </div>
    </div>
  )
}

type AnalysisDetailContentProps = {
  data: AnalysisDetail
  recordingId?: string | null
  recordingLinkBase?: string | null
  showHeader?: boolean
  embedded?: boolean
  embeddedSectionDefaultOpen?: boolean
  showCustomerTags?: boolean
  customerTagDisplayMode?: 'all' | 'extracted'
  title?: string
  backTo?: string | null
  backLabel?: string
  embeddedSimplified?: boolean
}

export function AnalysisDetailContent({
  data,
  recordingId,
  recordingLinkBase = '/admin/recordings',
  showHeader = false,
  embedded = false,
  embeddedSectionDefaultOpen = true,
  showCustomerTags = true,
  customerTagDisplayMode = 'all',
  title = '分析结果',
  backTo = null,
  backLabel = '← 返回列表',
  embeddedSimplified = false,
}: AnalysisDetailContentProps) {
  const linkedRecordingId = recordingId ?? extractRecordingIdFromAnalysisFileId(data.file_id)
  const evidenceUtterances = useMemo(() => extractEvidenceUtterances(data.transcript), [data.transcript])
  const time = data.recorded_at ? formatBeijingTime(data.recorded_at) : '未知时间'
  const primaryDemands = data.customer_primary_demands
  const recommendations = data.staff_recommendations
  const standardizedIndications = data.standardized_indications
  const consumptionIntent = data.consumption_intent
  const concerns = data.customer_concerns
  const profile = data.customer_profile
  const consultationResult = data.consultation_result
  const processEvaluation = data.consultation_process_evaluation
  const primarySapText = sapPreviewText(data.sap_consultation_preview)
  const processTotalScore = processEvaluation.total_score
    ?? processEvaluation.sections.reduce((sum, section) => sum + (getProcessPointScore(section) ?? 0), 0)
  const chiefDemandItems = (primaryDemands?.items?.length ?? 0) > 0
    ? [...(primaryDemands?.items ?? [])]
        .sort((a, b) => a.priority - b.priority)
        .map((item) => ({
          text: item.demand,
          evidence: item.evidence || null,
        }))
    : consultationResult.chief_complaint_and_indications.primary_demands.map((item) => ({ text: item, evidence: null }))
  const chiefIndicationItems = standardizedIndications?.items?.length
    ? standardizedIndications.items.map((item) => ({
        text: formatChiefIndicationLine(item),
        evidence: item.evidence || null,
      }))
    : consultationResult.chief_complaint_and_indications.standardized_indications.map((item) => ({ text: item, evidence: null }))
  const hasChiefDetails = chiefDemandItems.length > 0 || chiefIndicationItems.length > 0
  const recommendedPlanItemsWithRelations = (
    consultationResult.recommended_plan.items.length > 0
      ? consultationResult.recommended_plan.items
      : (recommendations?.items ?? []).map((item) => ({
          plan: item.product_or_solution || item.recommendation || '',
          acceptance: item.customer_response || null,
          evidence: item.evidence || null,
        }))
  ).map((item) => {
    const matchedRecommendations = (recommendations?.items ?? []).filter((recommendation) =>
      valuesLooselyMatch(recommendation.product_or_solution, item.plan)
      || valuesLooselyMatch(recommendation.recommendation, item.plan)
      || (item.evidence ? valuesLooselyMatch(recommendation.evidence, item.evidence) : false),
    )
    const relatedPriorities = Array.from(
      new Set(
        matchedRecommendations.flatMap((recommendation) =>
          Array.isArray(recommendation.demand_priority) ? recommendation.demand_priority : [],
        ),
      ),
    ).filter((priority) => Number.isFinite(priority))
      .sort((a, b) => a - b)

    return {
      ...item,
      relatedDemandPriorities: relatedPriorities,
    }
  })
  // Only surface budget evidence when:
  //   1. An actual budget value was extracted (otherwise no figure to support);
  //   2. Limited to evidence lines that actually mention price / money keywords —
  //      the raw evidence array is the entire consultation transcript and is not
  //      a faithful citation of the budget value;
  //   3. Capped to a small number of lines so the popover stays readable.
  const BUDGET_KEYWORD_RE = /(钱|价|元|万|千|块|费用|预算|价位|价格)/
  const budgetEvidence = (() => {
    if (!consultationResult.deal_factors.budget) return null
    const raw = consumptionIntent?.evidence ?? []
    const relevant = raw.filter((line) => typeof line === 'string' && BUDGET_KEYWORD_RE.test(line))
    if (relevant.length === 0) return null
    return relevant.slice(0, 3).join('\n')
  })()
  const concernItemsWithEvidence = consultationResult.deal_factors.concerns.map((item) => {
    const matchedConcern = concerns.items.find((concern) => valuesLooselyMatch(concern.content, item))
    return {
      text: item,
      evidence: matchedConcern?.evidence || null,
    }
  })
  const decisionFactorItems = consultationResult.deal_factors.decision_factors.map((item) => {
    const matchedEvidence = (consumptionIntent?.evidence ?? []).find((line) => valuesLooselyMatch(line, item))
    return {
      text: item,
      evidence: matchedEvidence || null,
    }
  })
  const hasDealFactorDetails = Boolean(consultationResult.deal_factors.budget)
    || concernItemsWithEvidence.length > 0
    || decisionFactorItems.length > 0
  const dealOutcomeStatus = consultationResult.deal_outcome.status || '未明确'
  const shouldShowClosedDeal = dealOutcomeStatus === '已成交'
  const shouldShowLossReasons = dealOutcomeStatus === '未成交'
  const dealOutcomeTone = shouldShowClosedDeal ? 'success' : shouldShowLossReasons ? 'loss' : 'neutral'
  const dealOutcomeAmount = consultationResult.deal_outcome.amount?.trim() || ''
  const dealOutcomeDetailItems = shouldShowClosedDeal
    ? consultationResult.deal_outcome.deal_items
    : shouldShowLossReasons
      ? consultationResult.deal_outcome.loss_reasons
      : []
  const dealOutcomeDetailTitle = shouldShowClosedDeal ? '成交方案' : shouldShowLossReasons ? '未成交原因' : '结果备注'
  const shouldShowDealOutcomeDetails = dealOutcomeDetailItems.length > 0 || (!embeddedSimplified && (shouldShowClosedDeal || shouldShowLossReasons))
  const dealOutcomeSummaryText = consultationResult.deal_outcome.summary
    || (shouldShowClosedDeal
      ? '已识别到成交结果，请结合成交方案、金额和证据复核。'
      : shouldShowLossReasons
        ? '本次暂未成交，请优先关注未成交原因和后续跟进点。'
        : '当前录音暂未识别到明确成交结论。')
  const acceptedRecommendationEvidence = shouldShowClosedDeal ? (recommendations?.items ?? [])
    .filter((item) => item.customer_response === '接受' && item.evidence)
    .map((item) => item.evidence) : []
  const lossReasonEvidence = shouldShowLossReasons ? consultationResult.deal_outcome.loss_reasons.flatMap((reason) => {
    const matchedConcern = concerns.items.find((concern) => valuesLooselyMatch(concern.content, reason))
    return matchedConcern?.evidence ? [matchedConcern.evidence] : []
  }) : []
  const dealOutcomeEvidence = Array.from(new Set([
    ...acceptedRecommendationEvidence,
    ...lossReasonEvidence,
    ...(budgetEvidence ? [budgetEvidence] : []),
  ])).filter(Boolean).join('\n') || null
  const hasRecommendedPlanDetails = recommendedPlanItemsWithRelations.some((item) => Boolean(item.plan && item.plan !== '-'))
  const processScoreTitle = formatScoreWithUnit(processTotalScore ?? 0)
  const profileAge = consultationResult.customer_profile_summary.age || profile.age || null
  const profileAgeEvidence = consultationResult.customer_profile_summary.age_evidence || profile.age_evidence || null
  const shouldOnlyShowExtractedTags = customerTagDisplayMode === 'extracted'
  const extractedProfileTags = profile.tags.filter((tag) => Boolean(tag.category?.trim()) && Boolean(tag.value?.trim()))
  const profileTagsForDisplay = shouldOnlyShowExtractedTags ? extractedProfileTags : profile.tags
  const hasProfileDetails = Boolean(profileAge) || profileTagsForDisplay.length > 0
  const customerTagsContent = (() => {
    const allItems = ANALYSIS_TAG_CATALOG_GROUPS.flatMap((group) => group.items)
    const catalogNameSet = new Set(allItems.map((item) => item.name))
    const parentGroupNames = new Set(allItems.filter((item) => item.group !== item.name).map((item) => item.group))

    const hitMap = new Map<string, string[]>()
    const evidenceMap = new Map<string, string[]>()
    const groupHits = new Map<string, string[]>()
    const groupEvidence = new Map<string, string[]>()
    const unmatchedTags: { category: string; value: string; evidence?: string }[] = []
    const appendMapValue = (map: Map<string, string[]>, key: string, value?: string) => {
      const normalized = (value ?? '').trim()
      if (!normalized) return
      const list = map.get(key) ?? []
      if (!list.includes(normalized)) {
        list.push(normalized)
        map.set(key, list)
      }
    }
    const formatTagValues = (values?: string[]) => values?.join('；') || '—'

    for (const tag of profileTagsForDisplay) {
      if (!tag.category || !tag.value) continue
      const category = tag.category

      if (catalogNameSet.has(category)) {
        appendMapValue(hitMap, category, tag.value)
        appendMapValue(evidenceMap, category, tag.evidence)
        continue
      }

      if (parentGroupNames.has(category)) {
        appendMapValue(groupHits, category, tag.value)
        if (tag.evidence) {
          appendMapValue(groupEvidence, category, tag.evidence)
        }
        continue
      }

      const underscoreIndex = category.indexOf('_')
      if (underscoreIndex > 0) {
        const child = category.slice(underscoreIndex + 1)
        if (catalogNameSet.has(child)) {
          appendMapValue(hitMap, child, tag.value)
          appendMapValue(evidenceMap, child, tag.evidence)
          continue
        }
      }

      const partial = allItems.find((item) => item.name.includes(category) || category.includes(item.name))
      if (partial) {
        appendMapValue(hitMap, partial.name, tag.value)
        appendMapValue(evidenceMap, partial.name, tag.evidence)
        continue
      }

      unmatchedTags.push({ category, value: tag.value, evidence: tag.evidence || undefined })
    }

    const hitCount = hitMap.size + groupHits.size
    const displayedTagCount = hitCount + unmatchedTags.length
    const totalCatalogTagCount = allItems.length

    if (shouldOnlyShowExtractedTags && displayedTagCount === 0) {
      return <div className="ad-empty-inline">当前未提取到客户标签。</div>
    }

    return (
      <>
        {!embeddedSimplified ? (
          <p className="ad-tag-stats">
            {shouldOnlyShowExtractedTags ? (
              <>已提取 <strong>{displayedTagCount}</strong> 项</>
            ) : (
              <>已命中 <strong>{hitCount}</strong> / {totalCatalogTagCount} 项</>
            )}
          </p>
        ) : null}

        {ANALYSIS_TAG_CATALOG_GROUPS.map((group) => {
          const subGroups: { label: string; items: typeof group.items }[] = []
          let currentGroup = ''

          for (const item of group.items) {
            if (item.group !== currentGroup) {
              subGroups.push({ label: item.group, items: [] })
              currentGroup = item.group
            }
            subGroups[subGroups.length - 1].items.push(item)
          }

          const childHits = group.items.filter((item) => hitMap.has(item.name)).length
          const groupLevelHits = subGroups.filter((subGroup) => groupHits.has(subGroup.label)).length
          if (shouldOnlyShowExtractedTags && childHits + groupLevelHits === 0) return null

          return (
            <div key={group.weight} className="ad-tag-group">
              <h4 className="ad-tag-group__title">
                <Tag color={group.color}>{group.label}</Tag>
                {shouldOnlyShowExtractedTags ? `已提取 ${childHits + groupLevelHits}` : `${childHits + groupLevelHits}/${group.items.length}`}
              </h4>
              {subGroups.map((subGroup) => {
                const isStandalone = subGroup.items.length === 1 && subGroup.items[0].name === subGroup.label
                const groupHitValues = groupHits.get(subGroup.label)
                const hasGroupHit = groupHitValues != null
                const hitItems = subGroup.items.filter((item) => hitMap.has(item.name))
                if (shouldOnlyShowExtractedTags && !hasGroupHit && hitItems.length === 0) return null

                if (isStandalone) {
                  const value = hitMap.get(subGroup.items[0].name)
                  const hit = value != null
                  if (shouldOnlyShowExtractedTags && !hit) return null
                  return (
                    <div key={subGroup.label} className={`ad-tag-item ${hit ? 'ad-tag-item--hit' : 'ad-tag-item--miss'}`}>
                      <div className="ad-tag-item__main">
                        <span className="ad-tag-item__category">{subGroup.items[0].name}</span>
                        <span className="ad-tag-item__value">{formatTagValues(value)}</span>
                      </div>
                      {hit && evidenceMap.get(subGroup.items[0].name) ? (
                        <EvidenceToggle
                          evidence={(evidenceMap.get(subGroup.items[0].name) || []).join('\n')}
                          recordingId={linkedRecordingId}
                          recordingLinkBase={recordingLinkBase}
                        />
                      ) : null}
                    </div>
                  )
                }

                return (
                  <div key={subGroup.label} className={`ad-tag-parent-group ${hasGroupHit ? 'ad-tag-parent-group--hit' : ''}`}>
                    <h5 className="ad-tag-parent-group__title">
                      {subGroup.label}
                      {groupHitValues && (
                        <span className="ad-tag-parent-group__value">{groupHitValues.join('；')}</span>
                      )}
                    </h5>
                    {hasGroupHit && (groupEvidence.get(subGroup.label)?.length ?? 0) > 0 ? (
                      <EvidenceToggle
                        evidence={Array.from(new Set(groupEvidence.get(subGroup.label) || [])).join('\n')}
                        recordingId={linkedRecordingId}
                        recordingLinkBase={recordingLinkBase}
                      />
                    ) : null}
                    <div className="ad-tag-parent-group__children">
                      {(shouldOnlyShowExtractedTags ? hitItems : subGroup.items).map((item) => {
                        const value = hitMap.get(item.name)
                        const hit = value != null
                        const evidence = evidenceMap.get(item.name)
                        return (
                          <div key={item.name} className={`ad-tag-item ${hit ? 'ad-tag-item--hit' : 'ad-tag-item--miss'}`}>
                            <div className="ad-tag-item__main">
                              <span className="ad-tag-item__category">{item.name}</span>
                              <span className="ad-tag-item__value">{hit ? formatTagValues(value) : '—'}</span>
                            </div>
                            {hit && evidence && evidence.length > 0 ? (
                              <EvidenceToggle
                                evidence={evidence.join('\n')}
                                recordingId={linkedRecordingId}
                                recordingLinkBase={recordingLinkBase}
                              />
                            ) : null}
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
            </div>
          )
        })}

        {unmatchedTags.length > 0 && (
          <div className="ad-tag-group">
            <h4 className="ad-tag-group__title">
              <Tag color="#722ed1">其他</Tag>
              {unmatchedTags.length}
            </h4>
            <div className="ad-tags-grid">
              {unmatchedTags.map((tag, index) => (
                <div key={index} className="ad-tag-item ad-tag-item--hit">
                  <div className="ad-tag-item__main">
                    <span className="ad-tag-item__category">{tag.category}</span>
                    <span className="ad-tag-item__value">{tag.value}</span>
                  </div>
                  {tag.evidence ? (
                    <EvidenceToggle
                      evidence={tag.evidence}
                      recordingId={linkedRecordingId}
                      recordingLinkBase={recordingLinkBase}
                    />
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        )}
      </>
    )
  })()

  const pageClassName = `ad-page${embedded || !showHeader ? ' ad-page--embedded' : ''}${embeddedSimplified ? ' ad-page--embedded-simplified' : ''}`

  return (
    <EvidenceUtteranceContext.Provider value={evidenceUtterances}>
    <section className={pageClassName}>
      {showHeader && (
        <header className="ad-header">
          {backTo ? <Link to={backTo} className="ad-header__back">{backLabel}</Link> : null}
          <h1 className="ad-header__title">{title}</h1>
          <div className="ad-header__meta">
            <span>{time}</span>
            <span>时长 {data.duration_display}</span>
            {data.recording_file_name && (
              <span className="ad-header__file">
                {formatRecordingDisplayName(data.recording_file_name, data.audio_start_time || data.recorded_at)}
              </span>
            )}
            {linkedRecordingId && recordingLinkBase && (
              <Link to={buildRecordingDetailLink(recordingLinkBase, linkedRecordingId)} className="ad-header__rec-link">
                <LinkOutlined /> 查看录音详情
              </Link>
            )}
          </div>
        </header>
      )}

      <div className="ad-analysis-columns">

        <AnalysisSection
          title="面诊结果分析"
          embedded={embedded}
          defaultOpen={embeddedSectionDefaultOpen}
        >
          <div className="ad-result-grid">
            <div className={`ad-result-grid__split${showCustomerTags ? '' : ' ad-result-grid__split--no-tags'}`}>
            <div className="ad-result-grid__col-left">
            <div className="ad-result-card ad-result-card--hero">
              <span className="ad-result-card__eyebrow">1. 探寻顾客主诉与初步适应症</span>
              {!embeddedSimplified && !hasChiefDetails ? (
                <p className="ad-result-card__summary">
                  {consultationResult.chief_complaint_and_indications.summary || '当前未生成主诉与适应症汇总。'}
                </p>
              ) : null}
              <div className="ad-result-card__groups ad-result-card__groups--chief">
                <div className="ad-result-card__group">
                  <strong>顾客主诉</strong>
                  {(chiefDemandItems.length > 0) ? (
                    <div className="ad-demand-list">
                      {chiefDemandItems.map((item, index) => (
                        <div key={`chief-demand-${index}-${item.text}`} className="ad-demand-item">
                          <div className="ad-demand-item__header">
                            <Tag color={PRIORITY_COLORS[index % PRIORITY_COLORS.length]}>主诉 #{index + 1}</Tag>
                          </div>
                          <p className="ad-demand-item__text">{item.text}</p>
                          {item.evidence ? (
                            <EvidenceToggle evidence={item.evidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="ad-demand-item">
                      <p className="ad-demand-item__text">-</p>
                    </div>
                  )}
                </div>
                <div className="ad-result-card__group">
                  <strong>初步适应症</strong>
                  {(chiefIndicationItems.length > 0) ? (
                    <div className="ad-indications ad-indications--inline">
                      <div className="ad-indications__list">
                        {chiefIndicationItems.map((item, index) => (
                          <div key={`indication-${index}-${item.text}`} className="ad-indication-chip">
                            <strong>{item.text}</strong>
                            {item.evidence ? (
                              <EvidenceToggle evidence={item.evidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <div className="ad-demand-item">
                      <p className="ad-demand-item__text">-</p>
                    </div>
                  )}
                </div>
              </div>
            </div>

            <div className="ad-result-card ad-result-card--factors">
              <span className="ad-result-card__eyebrow">2. 成交影响因素</span>
              {!embeddedSimplified && !hasDealFactorDetails ? (
                <p className="ad-result-card__summary">
                  {consultationResult.deal_factors.summary || '当前未生成成交影响因素总结。'}
                </p>
              ) : null}
              <div className="ad-result-card__groups">
                {(consultationResult.deal_factors.budget || !embeddedSimplified) ? (
                  <div className="ad-result-card__group">
                    <strong>本次预算</strong>
                    <p>{consultationResult.deal_factors.budget || '-'}</p>
                    {budgetEvidence ? (
                      <EvidenceToggle evidence={budgetEvidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                    ) : null}
                  </div>
                ) : null}
                <div className="ad-result-card__group">
                  <strong>客户顾虑</strong>
                  {(concernItemsWithEvidence.length > 0) ? (
                    <div className="ad-concern-list">
                      {concernItemsWithEvidence.map((item) => (
                        <div key={`result-concern-${item.text}`} className="ad-concern-item">
                          <p className="ad-concern-item__text">{item.text}</p>
                          {item.evidence ? (
                            <EvidenceToggle evidence={item.evidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <ul><li>-</li></ul>
                  )}
                </div>
                <div className="ad-result-card__group">
                  <strong>其他影响因素</strong>
                  {(decisionFactorItems.length > 0) ? (
                    <div className="ad-concern-list ad-concern-list--objective">
                      {decisionFactorItems.map((item) => (
                        <div key={`result-decision-factor-${item.text}`} className="ad-concern-item ad-concern-item--objective">
                          <p className="ad-concern-item__text">{item.text}</p>
                          {item.evidence ? (
                            <EvidenceToggle evidence={item.evidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <ul><li>-</li></ul>
                  )}
                </div>
              </div>
            </div>

            <div className="ad-result-card ad-result-card--recommend-panel">
              <span className="ad-result-card__eyebrow">3. 推荐给顾客的方案和认可程度</span>
              {!embeddedSimplified && !hasRecommendedPlanDetails ? (
                <p className="ad-result-card__summary">
                  {consultationResult.recommended_plan.summary || '当前未生成推荐方案总结。'}
                </p>
              ) : null}
              <div className="ad-demand-list ad-demand-list--recommend">
                {(recommendedPlanItemsWithRelations.length > 0
                  ? recommendedPlanItemsWithRelations
                  : [{ plan: '-', acceptance: null, evidence: null, relatedDemandPriorities: [] as number[] }]
                ).map((item, index) => (
                  <div key={`${item.plan}-${index}`} className="ad-demand-item ad-demand-item--recommend">
                    <div className="ad-demand-item__header">
                      <Tag color="cyan">方案 #{index + 1}</Tag>
                      {item.relatedDemandPriorities.length > 0 ? (
                        <span className="ad-demand-item__header-relation">
                          对应主诉 {item.relatedDemandPriorities.map((priority) => `#${priority}`).join('/')}
                        </span>
                      ) : null}
                      {(!embeddedSimplified || (item.acceptance && item.acceptance !== '未明确回应')) ? (
                        <span
                          className={`ad-recommend-item__response ad-recommend-item__response--${
                            item.acceptance === '接受' ? 'accept' : item.acceptance === '拒绝' ? 'reject' : 'neutral'
                          }`}
                        >
                          {item.acceptance || '未明确回应'}
                        </span>
                      ) : null}
                    </div>
                    <p className="ad-demand-item__text">{item.plan || '-'}</p>
                    {item.evidence ? (
                      <EvidenceToggle evidence={item.evidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                    ) : null}
                  </div>
                ))}
              </div>
            </div>

            <div className="ad-result-card ad-result-card--outcome">
              <span className="ad-result-card__eyebrow">4. 成交情况总结</span>
              <div className={`ad-deal-summary ad-deal-summary--${dealOutcomeTone}`}>
                <div className="ad-deal-summary__head">
                  <span>成交状态</span>
                  <strong>{dealOutcomeStatus}</strong>
                </div>
                <p>{dealOutcomeSummaryText}</p>
              </div>
              {shouldShowClosedDeal ? (
                <div className="ad-deal-kpis">
                  <div className="ad-deal-kpi ad-deal-kpi--amount">
                    <span>成交金额</span>
                    <strong>{dealOutcomeAmount || '未明确'}</strong>
                  </div>
                </div>
              ) : null}
              {shouldShowDealOutcomeDetails ? (
                <div className="ad-deal-list">
                  <div className="ad-deal-list__title">
                    <span>{dealOutcomeDetailTitle}</span>
                    <small>{dealOutcomeDetailItems.length > 0 ? `${dealOutcomeDetailItems.length} 项` : '未提取'}</small>
                  </div>
                  {dealOutcomeDetailItems.length > 0 ? (
                    <ul>
                      {dealOutcomeDetailItems.map((item) => (
                        <li key={`deal-outcome-${item}`}>
                          <span>{item}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="ad-deal-list__empty">当前未提取到{dealOutcomeDetailTitle}。</p>
                  )}
                </div>
              ) : null}
              {dealOutcomeEvidence ? (
                <div className="ad-deal-evidence">
                  <EvidenceToggle evidence={dealOutcomeEvidence} recordingId={linkedRecordingId} recordingLinkBase={recordingLinkBase} />
                </div>
              ) : null}
            </div>
            </div>

            {showCustomerTags ? (
            <div className="ad-result-grid__col-right">
            <div className="ad-result-card ad-result-card--tags-panel">
              <span className="ad-result-card__eyebrow">5. 获得顾客标签信息</span>
              {!embeddedSimplified && !hasProfileDetails ? (
                <p className="ad-result-card__summary">
                  {consultationResult.customer_profile_summary.summary || '当前未生成画像标签总结。'}
                </p>
              ) : null}
              {(!shouldOnlyShowExtractedTags || profileAge) ? (
                <div className="ad-profile-facts">
                <div className={`ad-profile-fact ${profileAge ? 'ad-profile-fact--hit' : 'ad-profile-fact--miss'}`}>
                  <div className="ad-profile-fact__main">
                    <span>顾客年龄</span>
                    <strong>{profileAge || '未提取'}</strong>
                  </div>
                  {profileAgeEvidence ? (
                    <EvidenceToggle
                      evidence={profileAgeEvidence}
                      recordingId={linkedRecordingId}
                      recordingLinkBase={recordingLinkBase}
                    />
                  ) : null}
                </div>
              </div>
              ) : null}
              <div className="ad-result-card__groups ad-result-card__groups--tags">
                {customerTagsContent}
              </div>
            </div>
            </div>
            ) : null}
            </div>
          </div>
        </AnalysisSection>

        {!embeddedSimplified ? (
          <AnalysisSection
            title="SAP预回写内容"
            embedded={embedded}
            defaultOpen={embeddedSectionDefaultOpen}
          >
            <div className="ad-sap-preview">
              <div className="ad-sap-preview__panel ad-sap-preview__panel--text">
                <h3 className="ad-sap-preview__title">咨询备注</h3>
                {primarySapText ? (
                  <pre className="ad-sap-preview__text">{primarySapText}</pre>
                ) : (
                  <p className="ad-sap-preview__empty">
                    暂未生成SAP预回写内容。录音完成LLM分析后会在这里展示预回写给SAP的咨询备注。
                  </p>
                )}
              </div>
            </div>
          </AnalysisSection>
        ) : null}

        <AnalysisSection
          title="面诊过程评价"
          count={processScoreTitle}
          embedded={embedded}
          defaultOpen={embeddedSectionDefaultOpen}
        >
          {processEvaluation.overall_summary && !embeddedSimplified ? (
            <p className="ad-section__summary">{processEvaluation.overall_summary}</p>
          ) : null}
          <div className="ad-process-section-list">
            {processEvaluation.sections.map((section) => (
              <ProcessEvaluationSectionBlock
                key={section.code || section.name}
                section={section}
                recordingId={linkedRecordingId}
                recordingLinkBase={recordingLinkBase}
                simplified={embeddedSimplified}
              />
            ))}
          </div>
        </AnalysisSection>
      </div>
    </section>
    </EvidenceUtteranceContext.Provider>
  )
}
