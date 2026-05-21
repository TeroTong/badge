import { CheckCircleOutlined, DeleteOutlined, PlusOutlined, RightOutlined, SearchOutlined, SendOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { message } from 'antd'
import dayjs from 'dayjs'
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import {
  fetchSapConsultationReview,
  fetchSapConsultationReviews,
  fetchSapReviewIndicationOptions,
  pushSapConsultationReview,
  updateSapConsultationReviewBlock,
  updateSapConsultationReviewIndications,
  type SapReviewIndication,
  type SapReviewIndicationOption,
  type SapReviewBlock,
  type SapReviewListItem,
} from '@/api/sap-consultation-reviews'
import { fetchRecordingMediaSource } from '@/api/recordings'
import { fetchTranscripts, type TranscriptUtterance } from '@/api/transcripts'
import { WecomPageIntro } from '@/components/wecom-page-intro'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow, formatBeijingTime } from '@/utils/time'

const PAGE_SIZE = 12
const CONSULTATION_FIELDS = [
  { key: 'chiefComplaint', label: '顾客主诉', rows: 2 },
  { key: 'budget', label: '本次预算', rows: 1 },
  { key: 'concerns', label: '顾客顾虑', rows: 2 },
  { key: 'recommendedPlan', label: '推荐方案', rows: 5 },
  { key: 'seedPlan', label: '种草方案', rows: 4 },
  { key: 'summary', label: '总结信息', rows: 6 },
] as const
const STATUS_FILTER_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: 'pending', label: '待回传' },
  { value: 'sending', label: '回传中' },
  { value: 'succeeded', label: '回传成功' },
  { value: 'failed', label: '回传失败' },
  { value: 'modified_pending', label: '已修改未回传' },
] as const

type ConsultationFieldKey = (typeof CONSULTATION_FIELDS)[number]['key']
type ConsultationFields = Record<ConsultationFieldKey, string>

const FIELD_LABEL_TO_KEY: Record<string, ConsultationFieldKey> = {
  顾客主诉: 'chiefComplaint',
  本次预算: 'budget',
  顾客顾虑: 'concerns',
  推荐方案: 'recommendedPlan',
  种草方案: 'seedPlan',
  总结信息: 'summary',
}

function createEmptyConsultationFields(): ConsultationFields {
  return {
    chiefComplaint: '',
    budget: '',
    concerns: '',
    recommendedPlan: '',
    seedPlan: '',
    summary: '',
  }
}

function formatTime(value: string | null | undefined) {
  if (!value) return '--'
  return formatBeijingTime(value, 'MM/DD HH:mm')
}

function formatMs(value: number | null | undefined) {
  const totalSeconds = Math.max(0, Math.floor((value ?? 0) / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function formatTranscriptSpeaker(value: string | null | undefined) {
  const speaker = String(value ?? '').trim()
  if (!speaker) return '未知'
  if (['customer', 'patient', 'client'].includes(speaker)) return '客户'
  if (['consultant', 'staff', 'advisor', 'doctor'].includes(speaker)) return '员工'
  return speaker
}

function statusClass(status: string) {
  if (status === 'succeeded' || status === 'modified_succeeded') return 'wc-chip wc-chip--green'
  if (status === 'failed' || status === 'modified_failed') return 'wc-chip wc-chip--red'
  if (status === 'sending' || status === 'modified_sending') return 'wc-chip wc-chip--blue'
  if (status === 'not_generated') return 'wc-chip wc-chip--blue'
  if (status === 'pending') return 'wc-chip wc-chip--amber'
  if (status === 'modified_pending') return 'wc-chip wc-chip--amber'
  return 'wc-chip'
}

function recordingNamesText(item: SapReviewListItem) {
  const names = (item.recording_files?.length
    ? item.recording_files.map((recording) => {
        if (recording.file_name) {
          return formatRecordingDisplayName(recording.file_name, recording.created_at)
        }
        return recording.created_at ? `${formatBeijingTime(recording.created_at, 'MMDD_HHmmss')}.mp3` : recording.recording_id
      })
    : (item.recording_file_names ?? [])
        .map((name) => name.trim())
        .filter(Boolean)
        .map((name) => formatRecordingDisplayName(name, item.latest_recording_at))
  ).map((name) => name.trim()).filter(Boolean)
  if (names.length === 0) return '录音 --'
  if (names.length === 1) return `录音 ${names[0]}`
  const joined = names.join('、')
  if (joined.length <= 42) return `录音 ${joined}`
  return `录音 ${names[0]} 等 ${names.length} 条`
}

function pushInfoText(item: SapReviewListItem) {
  const isCurrentSuccess = item.status === 'succeeded' || item.status === 'modified_succeeded'
  if (isCurrentSuccess && item.last_success_push_at) {
    const suffix = item.last_push_consultation_no ? `｜咨询单 ${item.last_push_consultation_no}` : ''
    return `回传成功 ${formatTime(item.last_success_push_at)}${suffix}`
  }
  if (item.next_auto_push_at) {
    return `预计回传 ${formatTime(item.next_auto_push_at)}`
  }
  if (item.last_success_push_at) {
    const suffix = item.last_push_consultation_no ? `｜咨询单 ${item.last_push_consultation_no}` : ''
    return `最近成功 ${formatTime(item.last_success_push_at)}${suffix}`
  }
  return '回传 -'
}

function indicationTextValue(item: unknown, keys: string[]) {
  const record = (item ?? {}) as Record<string, unknown>
  for (const key of keys) {
    const value = String(record[key] ?? '').trim()
    if (value) return value
  }
  return ''
}

function formatIndicationLabel(item: SapReviewIndication) {
  const name = indicationTextValue(item, ['indication_name', 'name'])
  const body = indicationTextValue(item, ['body_part_name', 'body_part'])
  if (name && body) return `${name}（${body}）`
  if (name) return name
  const code = indicationTextValue(item, ['CCSYZ', 'indication_code'])
  const bodyCode = indicationTextValue(item, ['CCBW', 'body_part_code'])
  return [code || '--', bodyCode || '--'].join(' / ')
}

function formatIndicationCode(item: SapReviewIndication) {
  const departmentCode = indicationTextValue(item, ['CCKS', 'department_code'])
  const indicationCode = indicationTextValue(item, ['CCSYZ', 'indication_code'])
  const bodyPartCode = indicationTextValue(item, ['CCBW', 'body_part_code'])
  return [departmentCode, indicationCode, bodyPartCode].filter(Boolean).join(' / ')
}

function formatIndicationDepartment(item: SapReviewIndication) {
  return indicationTextValue(item, ['department_name', 'department'])
}

function indicationCodeKey(item: SapReviewIndication | SapReviewIndicationOption) {
  const departmentCode = indicationTextValue(item, ['CCKS', 'department_code'])
  const indicationCode = indicationTextValue(item, ['CCSYZ', 'indication_code'])
  const bodyPartCode = indicationTextValue(item, ['CCBW', 'body_part_code'])
  return [departmentCode, indicationCode, bodyPartCode].join('|')
}

function indicationOptionToPayload(option: SapReviewIndicationOption): SapReviewIndication {
  return {
    CCKS: option.department_code,
    CCSYZ: option.indication_code,
    CCBW: option.body_part_code,
    department_code: option.department_code,
    department_name: option.department_name,
    indication_code: option.indication_code,
    indication_name: option.indication_name,
    body_part_code: option.body_part_code,
    body_part_name: option.body_part_name,
  }
}

function sameIndicationList(left: SapReviewIndication[], right: SapReviewIndication[]) {
  const leftKeys = left.map(indicationCodeKey)
  const rightKeys = right.map(indicationCodeKey)
  if (leftKeys.length !== rightKeys.length) return false
  return leftKeys.every((key, index) => key === rightKeys[index])
}

function uniqueByKey<T>(items: T[], getKey: (item: T) => string) {
  const seen = new Set<string>()
  const result: T[] = []
  for (const item of items) {
    const key = getKey(item)
    if (!key || seen.has(key)) continue
    seen.add(key)
    result.push(item)
  }
  return result
}

function resolveReviewDate(item: SapReviewListItem) {
  return item.latest_recording_at || item.updated_at || item.last_push_at || null
}

function resolveReviewSortTimestamp(item: SapReviewListItem) {
  const value = resolveReviewDate(item)
  return value ? dayjs(value).valueOf() : 0
}

function groupReviewsByDate(items: SapReviewListItem[]) {
  const sorted = [...items].sort((left, right) => {
    const leftValue = resolveReviewDate(left)
    const rightValue = resolveReviewDate(right)
    const leftDate = leftValue ? formatBeijingTime(leftValue, 'YYYY-MM-DD') : '未知日期'
    const rightDate = rightValue ? formatBeijingTime(rightValue, 'YYYY-MM-DD') : '未知日期'
    if (leftDate !== rightDate) return rightDate.localeCompare(leftDate)
    return resolveReviewSortTimestamp(right) - resolveReviewSortTimestamp(left)
  })
  const groups: { label: string; items: SapReviewListItem[] }[] = []
  for (const item of sorted) {
    const date = resolveReviewDate(item)
    const label = date ? formatBeijingTime(date, 'YYYY-MM-DD') : '未知日期'
    const last = groups[groups.length - 1]
    if (last && last.label === label) {
      last.items.push(item)
    } else {
      groups.push({ label, items: [item] })
    }
  }
  return groups
}

function formatReviewGroupLabel(label: string) {
  if (label === '未知日期') return label
  return dayjs(label).format('MM月DD日')
}

function isReviewGroupExpandedByDefault(label: string, todayLabel: string) {
  return label === todayLabel
}

function parseRemarkPerson(header: string, fallbackName: string) {
  const match = strTrim(header).match(/^●\s*(?:备注人员|接诊人员)\s*[：:]\s*(.*)$/)
  return strTrim(match?.[1]) || fallbackName || '无'
}

function strTrim(value: unknown) {
  return String(value ?? '').trim()
}

function parseConsultationBody(text: string) {
  const fields = createEmptyConsultationFields()
  const present: Partial<Record<ConsultationFieldKey, boolean>> = {}
  const extraLines: string[] = []
  let activeKey: ConsultationFieldKey | null = null

  for (const rawLine of String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n')) {
    const line = rawLine.trimEnd()
    const match = line.trimStart().match(/^●\s*(顾客主诉|本次预算|顾客顾虑|推荐方案|种草方案|总结信息)\s*[：:]\s*(.*)$/)
    if (match) {
      activeKey = FIELD_LABEL_TO_KEY[match[1]]
      present[activeKey] = true
      fields[activeKey] = appendFieldLine(fields[activeKey], match[2] ?? '')
      continue
    }
    if (activeKey) {
      fields[activeKey] = appendFieldLine(fields[activeKey], line)
    } else if (line.trim()) {
      extraLines.push(line)
    }
  }

  for (const key of Object.keys(fields) as ConsultationFieldKey[]) {
    fields[key] = fields[key].trim()
  }
  return { fields, present, extraLines }
}

function appendFieldLine(current: string, line: string) {
  return current ? `${current}\n${line}` : line
}

function composeConsultationBody(
  fields: ConsultationFields,
  options: { includeSummary: boolean; extraLines?: string[] },
) {
  const lines: string[] = []
  for (const field of CONSULTATION_FIELDS) {
    if (field.key === 'summary' && !options.includeSummary) continue
    lines.push(`●${field.label}：${fields[field.key].trim() || '无'}`)
  }
  const extra = (options.extraLines ?? []).map((line) => line.trim()).filter(Boolean)
  return [...lines, ...extra].join('\n')
}

function ReviewListPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const page = Math.max(1, Number(searchParams.get('page') || '1') || 1)
  const keyword = searchParams.get('keyword')?.trim() || ''
  const statusFilter = searchParams.get('status')?.trim() || 'all'
  const [keywordInput, setKeywordInput] = useState(keyword)
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({})
  const todayGroupLabel = beijingNow().format('YYYY-MM-DD')

  useEffect(() => {
    setKeywordInput(keyword)
  }, [keyword])

  const { data, isLoading, isFetching, isError, error } = useQuery({
    queryKey: ['wecom', 'sap-reviews', page, keyword, statusFilter],
    queryFn: () => fetchSapConsultationReviews({ page, page_size: PAGE_SIZE, keyword, status: statusFilter }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const items = data?.items ?? []
  const groups = useMemo(() => groupReviewsByDate(items), [items])
  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  useEffect(() => {
    setExpandedGroups((prev) => {
      const next: Record<string, boolean> = {}
      let changed = false
      for (const group of groups) {
        if (Object.prototype.hasOwnProperty.call(prev, group.label)) {
          next[group.label] = prev[group.label]
          continue
        }
        next[group.label] = isReviewGroupExpandedByDefault(group.label, todayGroupLabel)
        changed = true
      }
      if (!changed && Object.keys(prev).length === Object.keys(next).length) {
        return prev
      }
      return next
    })
  }, [groups, todayGroupLabel])

  function updateSearch(next: { page?: number; keyword?: string; status?: string }) {
    const params = new URLSearchParams()
    const nextKeyword = next.keyword ?? keyword
    const nextStatus = next.status ?? statusFilter
    const nextPage = next.page ?? page
    if (nextKeyword.trim()) params.set('keyword', nextKeyword.trim())
    if (nextStatus && nextStatus !== 'all') params.set('status', nextStatus)
    if (nextPage > 1) params.set('page', String(nextPage))
    setSearchParams(params, { replace: true })
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    updateSearch({ keyword: keywordInput.trim(), page: 1 })
  }

  function toggleGroup(label: string) {
    setExpandedGroups((prev) => ({
      ...prev,
      [label]: !(prev[label] ?? isReviewGroupExpandedByDefault(label, todayGroupLabel)),
    }))
  }

  return (
    <div className="wc-page wc-sap-review-page">
      <WecomPageIntro eyebrow="SAP回写" title="咨询备注" tone="sky" />

      <section className="wc-card wc-card--sky wc-card--compact">
        <form className="wc-filter-bar wc-filter-bar--embedded wc-filter-bar--compact" onSubmit={submitSearch}>
          <div className="wc-filter-bar__search-row wc-filter-bar__search-row--single-action">
            <label className="wc-search wc-search--compact">
              <SearchOutlined />
              <input
                onChange={(event) => setKeywordInput(event.target.value)}
                placeholder="搜索到诊单号 / 录音"
                type="search"
                value={keywordInput}
              />
            </label>
            <button className="wc-btn wc-btn--primary wc-btn--compact" type="submit">搜索</button>
          </div>
          <label className="wc-sap-review-status-select">
            <span>状态</span>
            <select
              aria-label="SAP 回写状态筛选"
              onChange={(event) => updateSearch({ status: event.currentTarget.value, page: 1 })}
              value={statusFilter}
            >
              {STATUS_FILTER_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
        </form>
      </section>

      <section className="wc-card wc-sap-review-page__list-card" aria-busy={isFetching && !isLoading}>
        <div className="wc-recordings-page__summary">
          <span className="wc-recordings-page__summary-label">我的 SAP 咨询备注</span>
          <span className="wc-recordings-page__summary-value">
            {isError ? '异常' : `${total} 条`}
            {isFetching && !isLoading ? <small className="wc-sap-review-page__fetching">更新中</small> : null}
          </span>
        </div>

        {isLoading ? (
          <div className="wc-empty">加载中…</div>
        ) : isError ? (
          <div className="wc-empty">SAP 回写列表加载失败：{error instanceof Error ? error.message : '请稍后重试'}</div>
        ) : items.length === 0 ? (
          <div className="wc-empty">暂无需要查看或修改的 SAP 咨询备注</div>
        ) : (
          <div className="wc-list wc-sap-review-page__grouped-list">
            {groups.map((group) => (
              <div key={group.label} className="wc-date-sec wc-sap-review-date-sec">
                <button
                  className={`wc-date-sec__hd wc-date-sec__hd--toggle ${
                    expandedGroups[group.label] ?? isReviewGroupExpandedByDefault(group.label, todayGroupLabel)
                      ? 'is-expanded'
                      : 'is-collapsed'
                  }`}
                  onClick={() => toggleGroup(group.label)}
                  type="button"
                >
                  <span>{formatReviewGroupLabel(group.label)}</span>
                  <small>{group.items.length} 条</small>
                  <RightOutlined className="wc-date-sec__toggle-icon" />
                </button>
                {(expandedGroups[group.label] ?? isReviewGroupExpandedByDefault(group.label, todayGroupLabel)) ? (
                  <div className="wc-sap-review-date-sec__body">
                    {group.items.map((item) => (
                      <ReviewListCard key={item.visit_id} item={item} />
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}

        {totalPages > 1 ? (
          <div className="wc-pagination">
            <button className="wc-btn wc-btn--ghost" disabled={page <= 1 || isFetching} onClick={() => updateSearch({ page: page - 1 })} type="button">
              上一页
            </button>
            <span className="wc-pagination__meta">第 {page} / {totalPages} 页</span>
            <button className="wc-btn wc-btn--ghost" disabled={page >= totalPages || isFetching} onClick={() => updateSearch({ page: page + 1 })} type="button">
              下一页
            </button>
          </div>
        ) : null}
      </section>
    </div>
  )
}

function ReviewListCard({ item }: { item: SapReviewListItem }) {
  const title = item.customer_name || '未识别客户'
  const customerCode = item.customer_code || '--'
  const order = item.visit_order_no ? `${item.visit_order_no}${item.visit_order_seg ? `-${item.visit_order_seg}` : ''}` : '未关联 SAP 单号'
  return (
    <Link className="wc-row wc-row--stacked wc-row--card wc-sap-review-card" to={`/wecom/sap-reviews/${item.visit_id}`}>
      <div className="wc-row__main">
        <div className="wc-sap-review-card__top">
          <strong>{title}</strong>
          <span className={statusClass(item.status)}>{item.status_label}</span>
        </div>
        <div className="wc-sap-review-card__meta">
          <span>客户编号 {customerCode}</span>
          <span>到诊单 {order}</span>
        </div>
        <div className="wc-sap-review-card__foot">
          <span className="wc-sap-review-card__recordings" title={recordingNamesText(item)}>{recordingNamesText(item)}</span>
          <span>{pushInfoText(item)}</span>
        </div>
        {item.last_push_error ? <div className="wc-sap-review-card__error">{item.last_push_error}</div> : null}
      </div>
      <div className="wc-row__end"><RightOutlined className="wc-row__arrow" /></div>
    </Link>
  )
}

function IndicationEditor({
  draft,
  options,
  optionsLoading,
  saving,
  savedItems,
  onChange,
  onSave,
}: {
  draft: SapReviewIndication[]
  options: SapReviewIndicationOption[]
  optionsLoading: boolean
  saving: boolean
  savedItems: SapReviewIndication[]
  onChange: (items: SapReviewIndication[]) => void
  onSave: () => void
}) {
  const [departmentCode, setDepartmentCode] = useState('')
  const [bodyPartCode, setBodyPartCode] = useState('')
  const [indicationCode, setIndicationCode] = useState('')
  const changed = !sameIndicationList(draft, savedItems)
  const departments = useMemo(() => (
    uniqueByKey(options, (item) => item.department_code)
  ), [options])
  const bodyParts = useMemo(() => (
    uniqueByKey(
      options.filter((item) => item.department_code === departmentCode),
      (item) => item.body_part_code,
    )
  ), [options, departmentCode])
  const indications = useMemo(() => (
    uniqueByKey(
      options.filter((item) => item.department_code === departmentCode && item.body_part_code === bodyPartCode),
      (item) => item.indication_code,
    )
  ), [options, departmentCode, bodyPartCode])
  const selectedOption = options.find((item) => (
    item.department_code === departmentCode
    && item.body_part_code === bodyPartCode
    && item.indication_code === indicationCode
  ))

  useEffect(() => {
    setBodyPartCode('')
    setIndicationCode('')
  }, [departmentCode])

  useEffect(() => {
    setIndicationCode('')
  }, [bodyPartCode])

  function addSelected() {
    if (!selectedOption) {
      message.info('请先选择科室、部位和适应症')
      return
    }
    const next = indicationOptionToPayload(selectedOption)
    const key = indicationCodeKey(next)
    if (draft.some((item) => indicationCodeKey(item) === key)) {
      message.info('该适应症已添加')
      return
    }
    onChange([...draft, next])
  }

  function removeItem(index: number) {
    onChange(draft.filter((_item, itemIndex) => itemIndex !== index))
  }

  function handleSave() {
    if (!changed) {
      message.info('适应症未修改，无需保存')
      return
    }
    onSave()
  }

  return (
    <div className="wc-sap-review-indication-editor">
      <div className="wc-sap-review-detail__indications">
        {draft.length ? draft.map((item, index) => {
          const label = formatIndicationLabel(item)
          const code = formatIndicationCode(item)
          const department = formatIndicationDepartment(item)
          return (
            <span key={`${code || label}-${index}`} className="wc-chip wc-chip--blue wc-sap-review-indication" title={code}>
              <strong>{label}</strong>
              <small>{[department, code].filter(Boolean).join(' · ')}</small>
              <button aria-label={`删除${label}`} onClick={() => removeItem(index)} type="button">
                <DeleteOutlined />
              </button>
            </span>
          )
        }) : <span className="wc-muted">暂无适应症，可在下方添加</span>}
      </div>

      <div className="wc-sap-review-indication-picker">
        <label>
          <span>科室</span>
          <select disabled={optionsLoading} onChange={(event) => setDepartmentCode(event.currentTarget.value)} value={departmentCode}>
            <option value="">{optionsLoading ? '加载中…' : '选择科室'}</option>
            {departments.map((item) => (
              <option key={item.department_code} value={item.department_code}>{item.department_name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>部位</span>
          <select disabled={!departmentCode} onChange={(event) => setBodyPartCode(event.currentTarget.value)} value={bodyPartCode}>
            <option value="">选择部位</option>
            {bodyParts.map((item) => (
              <option key={item.body_part_code} value={item.body_part_code}>{item.body_part_name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>适应症</span>
          <select disabled={!bodyPartCode} onChange={(event) => setIndicationCode(event.currentTarget.value)} value={indicationCode}>
            <option value="">选择适应症</option>
            {indications.map((item) => (
              <option key={item.indication_code} value={item.indication_code}>{item.indication_name}</option>
            ))}
          </select>
        </label>
        <button className="wc-btn wc-btn--ghost wc-sap-review-indication-picker__add" disabled={!selectedOption} onClick={addSelected} type="button">
          <PlusOutlined /> 添加
        </button>
      </div>

      {selectedOption?.indication_note ? (
        <div className="wc-sap-review-indication-note">{selectedOption.indication_note}</div>
      ) : null}

      <div className="wc-sap-review-indication-actions">
        <button className="wc-btn wc-btn--primary" disabled={saving} onClick={handleSave} type="button">
          {saving ? '保存中…' : '保存适应症'}
        </button>
      </div>
    </div>
  )
}

function ReviewDetailPage({ visitId }: { visitId: string }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [indicationDraft, setIndicationDraft] = useState<SapReviewIndication[]>([])

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['wecom', 'sap-review', visitId],
    queryFn: () => fetchSapConsultationReview(visitId),
    staleTime: 5_000,
  })
  const { data: indicationOptions = [], isLoading: indicationOptionsLoading } = useQuery({
    queryKey: ['wecom', 'sap-review-indication-options'],
    queryFn: fetchSapReviewIndicationOptions,
    staleTime: 10 * 60_000,
  })

  useEffect(() => {
    if (!data) return
    setDrafts((current) => {
      const next: Record<string, string> = {}
      for (const block of data.blocks) {
        next[block.recording_id] = current[block.recording_id] ?? block.effective_body
      }
      return next
    })
    setIndicationDraft(data.indication_payload ?? [])
  }, [data])

  const updateMutation = useMutation({
    mutationFn: ({ recordingId, text }: { recordingId: string; text: string }) => (
      updateSapConsultationReviewBlock(visitId, recordingId, text)
    ),
    onSuccess: (next) => {
      queryClient.setQueryData(['wecom', 'sap-review', visitId], next)
      queryClient.invalidateQueries({ queryKey: ['wecom', 'sap-reviews'] })
      message.success('已保存咨询备注')
    },
    onError: (err) => {
      message.error(err instanceof Error ? err.message : '保存失败')
    },
  })

  const updateIndicationsMutation = useMutation({
    mutationFn: (items: SapReviewIndication[]) => updateSapConsultationReviewIndications(visitId, items),
    onSuccess: (next) => {
      queryClient.setQueryData(['wecom', 'sap-review', visitId], next)
      queryClient.invalidateQueries({ queryKey: ['wecom', 'sap-reviews'] })
      message.success('已保存适应症')
    },
    onError: (err) => {
      message.error(err instanceof Error ? err.message : '适应症保存失败')
    },
  })

  const pushMutation = useMutation({
    mutationFn: () => pushSapConsultationReview(visitId),
    onSuccess: (result) => {
      message.success(result.message || '已提交回写')
      queryClient.invalidateQueries({ queryKey: ['wecom', 'sap-review', visitId] })
      queryClient.invalidateQueries({ queryKey: ['wecom', 'sap-reviews'] })
    },
    onError: (err) => {
      message.error(err instanceof Error ? err.message : '提交回写失败')
    },
  })

  if (isLoading) {
    return <div className="wc-page"><div className="wc-empty">加载中…</div></div>
  }
  if (isError || !data) {
    return (
      <div className="wc-page">
        <div className="wc-empty">SAP 咨询备注加载失败：{error instanceof Error ? error.message : '请稍后重试'}</div>
      </div>
    )
  }

  const order = data.visit_order_no ? `${data.visit_order_no}${data.visit_order_seg ? `-${data.visit_order_seg}` : ''}` : '未关联 SAP 单号'
  const hasUnsavedRemarkChanges = data.blocks.some((block) => (
    (drafts[block.recording_id] ?? block.effective_body).trim() !== block.effective_body.trim()
  ))
  const hasUnsavedIndicationChanges = !sameIndicationList(indicationDraft, data.indication_payload ?? [])
  const canManualPush = data.status === 'modified_pending' || data.status === 'modified_failed'
  const handleManualPush = () => {
    if (hasUnsavedRemarkChanges) {
      message.info('请先保存修改后的咨询备注，再提交回传')
      return
    }
    if (hasUnsavedIndicationChanges) {
      message.info('请先保存修改后的适应症，再提交回传')
      return
    }
    if (!canManualPush) {
      message.info('咨询备注未修改，无需手动提交回传')
      return
    }
    pushMutation.mutate()
  }

  return (
    <div className="wc-page wc-sap-review-page">
      <section className="wc-sap-review-hero">
        <div className="wc-sap-review-hero__top">
          <div className="wc-sap-review-hero__identity">
            <span>SAP 咨询单</span>
            <h2>{data.customer_name || '未识别客户'}</h2>
            <p>客户编号 {data.customer_code || '--'} · {order}</p>
          </div>
          <span className={statusClass(data.status)}>{data.status_label}</span>
        </div>

        <div className="wc-sap-review-hero__actions">
          <button
            className="wc-btn wc-btn--primary"
            disabled={pushMutation.isPending}
            onClick={handleManualPush}
            type="button"
          >
            <SendOutlined /> {pushMutation.isPending ? '提交中…' : '提交回传'}
          </button>
          <button className="wc-btn wc-btn--ghost" onClick={() => navigate('/wecom/sap-reviews')} type="button">
            返回列表
          </button>
        </div>
      </section>

      <section className="wc-card wc-card--compact wc-sap-review-indications-card">
        <div className="wc-sap-review-detail__section-title">
          <CheckCircleOutlined />
          <span>适应症</span>
        </div>
        <IndicationEditor
          draft={indicationDraft}
          options={indicationOptions}
          optionsLoading={indicationOptionsLoading}
          saving={updateIndicationsMutation.isPending}
          savedItems={data.indication_payload ?? []}
          onChange={setIndicationDraft}
          onSave={() => updateIndicationsMutation.mutate(indicationDraft)}
        />
      </section>

      <section className="wc-sap-review-detail__blocks">
        {data.blocks.map((block) => (
          <ReviewBlockEditor
            key={block.recording_id}
            block={block}
            draft={drafts[block.recording_id] ?? block.effective_body}
            saving={updateMutation.isPending}
            onChange={(text) => setDrafts((current) => ({ ...current, [block.recording_id]: text }))}
            onSave={() => updateMutation.mutate({ recordingId: block.recording_id, text: drafts[block.recording_id] ?? block.effective_body })}
          />
        ))}
      </section>
    </div>
  )
}

function ReviewBlockEditor({
  block,
  draft,
  saving,
  onChange,
  onSave,
}: {
  block: SapReviewBlock
  draft: string
  saving: boolean
  onChange: (text: string) => void
  onSave: () => void
}) {
  const changed = draft.trim() !== block.effective_body.trim()
  const parsedDraft = useMemo(() => parseConsultationBody(draft), [draft])
  const parsedGenerated = useMemo(() => parseConsultationBody(block.generated_body), [block.generated_body])
  const includeSummary = block.sap_summary_enabled !== false && Boolean(parsedGenerated.present.summary)
  const remarkPerson = parseRemarkPerson(block.locked_header, block.staff_name)
  const recordingName = block.file_name
    ? formatRecordingDisplayName(block.file_name, block.recording_created_at)
    : (block.recording_created_at ? `${formatBeijingTime(block.recording_created_at, 'MMDD_HHmmss')}.mp3` : block.recording_id)
  const updateField = (key: ConsultationFieldKey, value: string) => {
    onChange(composeConsultationBody(
      { ...parsedDraft.fields, [key]: value },
      { includeSummary, extraLines: parsedDraft.extraLines },
    ))
  }
  const handleSave = () => {
    if (!changed) {
      message.info('内容未修改，无需保存')
      return
    }
    onSave()
  }

  return (
    <article className={`wc-card wc-card--compact wc-sap-review-block${block.can_edit ? '' : ' is-readonly'}`}>
      <div className="wc-sap-review-block__head">
        <div className="wc-sap-review-block__title">
          <strong>备注人员：{remarkPerson}</strong>
          <span className="wc-sap-review-block__file">录音：{recordingName}</span>
        </div>
        <div className="wc-sap-review-block__head-meta">
          <span className={block.can_edit ? 'wc-chip wc-chip--green' : 'wc-chip'}>{block.can_edit ? '可编辑' : '只读'}</span>
        </div>
      </div>
      <ReviewBlockMedia recordingId={block.recording_id} />
      <div className="wc-sap-review-fields">
        {CONSULTATION_FIELDS.filter((field) => field.key !== 'summary' || includeSummary).map((field) => (
          <label key={field.key} className={`wc-sap-review-field wc-sap-review-field--${field.key}`}>
            <span className="wc-sap-review-field__label">{field.label}</span>
            <textarea
              className="wc-sap-review-field__control"
              disabled={!block.can_edit}
              onChange={(event) => updateField(field.key, event.target.value)}
              rows={field.rows}
              value={parsedDraft.fields[field.key]}
            />
          </label>
        ))}
      </div>
      {block.can_edit ? (
        <div className="wc-sap-review-block__actions">
          <button className="wc-btn wc-btn--primary" disabled={saving} onClick={handleSave} type="button">
            {saving ? '保存中…' : '保存本段'}
          </button>
          <button className="wc-btn wc-btn--ghost" disabled={saving} onClick={() => onChange(block.generated_body)} type="button">
            恢复系统内容
          </button>
        </div>
      ) : null}
    </article>
  )
}

function ReviewBlockMedia({ recordingId }: { recordingId: string }) {
  const [expanded, setExpanded] = useState(false)
  const [playbackMs, setPlaybackMs] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const transcriptListRef = useRef<HTMLDivElement | null>(null)
  const transcriptItemRefs = useRef<Map<string, HTMLElement>>(new Map())
  const {
    data: audioSource,
    isLoading: audioLoading,
    isError: audioError,
  } = useQuery({
    queryKey: ['wecom', 'sap-review-recording-media-source', recordingId],
    queryFn: () => fetchRecordingMediaSource(recordingId),
    enabled: Boolean(recordingId),
    retry: false,
    staleTime: 60_000,
  })
  const { data: transcriptsData, isLoading: transcriptLoading } = useQuery({
    queryKey: ['wecom', 'sap-review-transcripts', recordingId],
    queryFn: () => fetchTranscripts({ recording_id: recordingId, page_size: 100 }),
    enabled: Boolean(recordingId) && expanded,
    staleTime: 60_000,
  })
  const utterances = (transcriptsData?.items?.[0]?.utterances ?? []) as TranscriptUtterance[]
  const transcriptText = transcriptsData?.items?.[0]?.full_text?.trim() || ''
  const activeUtteranceIndex = utterances.findIndex((utterance, index) => {
    if (playbackMs == null) return false
    const beginMs = Number(utterance.begin_ms || 0)
    const nextBeginMs = utterances[index + 1]?.begin_ms
    const endMs = Number(utterance.end_ms || 0) > beginMs
      ? Number(utterance.end_ms)
      : Number(nextBeginMs || beginMs + 5000)
    return playbackMs >= beginMs && playbackMs < endMs
  })
  const activeUtteranceKey = activeUtteranceIndex >= 0
    ? `${utterances[activeUtteranceIndex]?.begin_ms}-${activeUtteranceIndex}`
    : null

  useEffect(() => {
    if (!expanded || !activeUtteranceKey) return
    const container = transcriptListRef.current
    const element = transcriptItemRefs.current.get(activeUtteranceKey)
    if (!container || !element) return
    const containerRect = container.getBoundingClientRect()
    const elementRect = element.getBoundingClientRect()
    const elementTopInContainer = elementRect.top - containerRect.top + container.scrollTop
    const centeredTop = elementTopInContainer - Math.max(0, (container.clientHeight - elementRect.height) / 2)
    const maxTop = Math.max(0, container.scrollHeight - container.clientHeight)
    const nextTop = Math.min(maxTop, Math.max(0, centeredTop))
    container.scrollTo({ top: nextTop, behavior: 'smooth' })
  }, [activeUtteranceKey, expanded])

  function jumpToUtterance(utterance: TranscriptUtterance) {
    if (!audioRef.current || !audioSource?.url) return
    audioRef.current.currentTime = Math.max(0, Number(utterance.begin_ms || 0) / 1000)
    audioRef.current.play().catch(() => undefined)
  }

  return (
    <section className="wc-sap-review-media">
      <div className="wc-sap-review-media__head">
        <strong>录音参考</strong>
        <span>展开原文后自动跟随播放</span>
      </div>

      <div className="wc-sap-review-audio">
        {audioSource?.url ? (
          <audio
            ref={audioRef}
            controls
            preload="metadata"
            src={audioSource.url}
            onTimeUpdate={(event) => setPlaybackMs(Math.round(event.currentTarget.currentTime * 1000))}
            onSeeked={(event) => setPlaybackMs(Math.round(event.currentTarget.currentTime * 1000))}
            onEnded={() => setPlaybackMs(null)}
          >
            您的浏览器暂不支持音频播放。
          </audio>
        ) : audioLoading ? (
          <div className="wc-sap-review-media__empty">正在加载录音音频…</div>
        ) : audioError ? (
          <div className="wc-sap-review-media__empty">音频加载失败，请稍后重试</div>
        ) : (
          <div className="wc-sap-review-media__empty">暂无可播放音频</div>
        )}
      </div>

      <details className="wc-sap-review-transcript" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary>
          <span>查看转写原文</span>
          <small>{expanded ? (transcriptLoading ? '加载中' : utterances.length ? `${utterances.length} 句` : transcriptText ? '全文' : '暂无') : '展开'}</small>
        </summary>
        {transcriptLoading ? (
          <div className="wc-sap-review-media__empty">正在加载转写原文…</div>
        ) : utterances.length ? (
          <div ref={transcriptListRef} className="wc-sap-review-transcript__list">
            {utterances.map((utterance, index) => (
              <button
                key={`${utterance.begin_ms}-${index}`}
                ref={(element) => {
                  const key = `${utterance.begin_ms}-${index}`
                  if (element) {
                    transcriptItemRefs.current.set(key, element)
                  } else {
                    transcriptItemRefs.current.delete(key)
                  }
                }}
                className={`wc-sap-review-transcript__line${activeUtteranceKey === `${utterance.begin_ms}-${index}` ? ' is-active' : ''}`}
                onClick={() => jumpToUtterance(utterance)}
                type="button"
              >
                <div className="wc-sap-review-transcript__meta">
                  <span>{formatTranscriptSpeaker(utterance.speaker)}</span>
                  <small>{formatMs(utterance.begin_ms)}</small>
                </div>
                <p>{utterance.text}</p>
              </button>
            ))}
          </div>
        ) : transcriptText ? (
          <pre className="wc-sap-review-transcript__plain">{transcriptText}</pre>
        ) : (
          <div className="wc-sap-review-media__empty">暂无转写原文</div>
        )}
      </details>
    </section>
  )
}

export function WecomSapReviewsPage() {
  const { visitId } = useParams()
  if (visitId) {
    return <ReviewDetailPage visitId={visitId} />
  }
  return <ReviewListPage />
}

export default WecomSapReviewsPage
