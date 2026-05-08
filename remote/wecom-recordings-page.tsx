import { CloseCircleOutlined, RightOutlined, SearchOutlined } from '@ant-design/icons'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { HTTPError } from 'ky'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import { useAuth } from '@/app/use-auth'
import { fetchArchiveRecordings, type ArchiveRecording, type ArchiveRecordingDateSummary } from '@/api/archive-recordings'
import { WecomPageIntro } from '@/components/wecom-page-intro'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow, formatBeijingTime } from '@/utils/time'

type DatePreset = 'all' | 'today' | '3d' | '7d'
type Scope = 'mine' | 'all'
type LinkState = 'all' | 'linked' | 'needs_link'

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'all', label: '全部时间' },
  { value: 'today', label: '今天' },
  { value: '3d', label: '近3天' },
  { value: '7d', label: '近7天' },
]
const DATE_PRESET_VALUES = new Set<DatePreset>(DATE_PRESETS.map((item) => item.value))
const EMPTY_RECORDINGS: ArchiveRecording[] = []

function resolveDateRange(preset: DatePreset) {
  const today = beijingNow()
  switch (preset) {
    case 'today': return { date_from: today.format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '3d': return { date_from: today.subtract(2, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '7d': return { date_from: today.subtract(6, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    default: return {}
  }
}

function formatDuration(seconds: number | null) {
  if (seconds == null) return '--'
  const mins = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${mins}:${String(secs).padStart(2, '0')}`
}

function formatRecordingDateTime(value: string | null) {
  if (!value) return '时间未识别'
  return formatBeijingTime(value, 'MM/DD HH:mm')
}

function resolveArchiveCreatedAt(recording: ArchiveRecording) {
  return recording.create_time || recording.downloaded_at || recording.updated_at || null
}

function resolveRecordingSortTimestamp(recording: ArchiveRecording) {
  const createdAt = resolveArchiveCreatedAt(recording)
  return createdAt ? dayjs(createdAt).valueOf() : 0
}

function resolveRecordingGroupedLinkRank(recording: ArchiveRecording) {
  const status = `${recording.pipeline_status || ''}`.trim().toLowerCase()
  if (!recording.has_visit_link && status !== 'filtered') return 2
  if (recording.has_visit_link) return 1
  return 0
}

function groupByDate(recordings: ArchiveRecording[]) {
  const sorted = [...recordings].sort((left, right) => {
    const leftDate = resolveArchiveCreatedAt(left) ? formatBeijingTime(resolveArchiveCreatedAt(left), 'YYYY-MM-DD') : '未知日期'
    const rightDate = resolveArchiveCreatedAt(right) ? formatBeijingTime(resolveArchiveCreatedAt(right), 'YYYY-MM-DD') : '未知日期'
    if (leftDate !== rightDate) return rightDate.localeCompare(leftDate)
    const leftRank = resolveRecordingGroupedLinkRank(left)
    const rightRank = resolveRecordingGroupedLinkRank(right)
    if (leftRank !== rightRank) return rightRank - leftRank
    return resolveRecordingSortTimestamp(right) - resolveRecordingSortTimestamp(left)
  })
  const groups: { label: string; items: ArchiveRecording[] }[] = []
  for (const rec of sorted) {
    const createdAt = resolveArchiveCreatedAt(rec)
    const dateStr = createdAt ? formatBeijingTime(createdAt, 'YYYY-MM-DD') : '未知日期'
    const last = groups[groups.length - 1]
    if (last && last.label === dateStr) { last.items.push(rec) } else { groups.push({ label: dateStr, items: [rec] }) }
  }
  return groups
}

function resolveGroupLinkSummary(summary: ArchiveRecordingDateSummary | undefined, items: ArchiveRecording[]) {
  if (summary) {
    return `待关联${summary.needs_link_count}条/已关联${summary.linked_count}条`
  }
  const linkedCount = items.filter((item) => item.has_visit_link).length
  const needsLinkCount = items.length - linkedCount
  return `待关联${needsLinkCount}条/已关联${linkedCount}条`
}

function resolveSummaryLabel(linkState: LinkState) {
  switch (linkState) {
    case 'linked':
      return '已关联录音'
    case 'needs_link':
      return '待关联录音'
    default:
      return '管理范围录音'
  }
}

function isGroupExpandedByDefault(label: string, todayLabel: string) {
  return label === todayLabel
}

function resolveRecordingErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看这些录音'
    }
    if (error.response.status >= 500) {
      return '服务器处理录音列表时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

export function WecomRecordingsPage() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const rawStaffId = auth.status === 'authenticated' ? auth.user.staff_id ?? undefined : undefined
  const canFilterMine = Boolean(rawStaffId)

  const [searchParams, setSearchParams] = useSearchParams()
  const tabIntent = searchParams.get('tab')?.trim() === 'recordings' ? 'recordings' : ''
  const visitId = searchParams.get('visit_id') ?? ''
  const currentKeyword = searchParams.get('keyword')?.trim() ?? ''
  const initialPage = Number(searchParams.get('page') ?? '1') || 1
  const page = initialPage < 1 ? 1 : initialPage
  const pageSize = 20
  const scope: Scope = canFilterMine && searchParams.get('scope') === 'mine' ? 'mine' : 'all'
  const rawLinkState = searchParams.get('link_state')
  const linkState: LinkState =
    rawLinkState === 'all'
      ? 'all'
      : rawLinkState === 'linked'
      ? 'linked'
      : rawLinkState === 'needs_link' || rawLinkState === 'unlinked'
        ? 'needs_link'
        : 'all'
  const datePreset: DatePreset = DATE_PRESET_VALUES.has(searchParams.get('date_preset') as DatePreset)
    ? (searchParams.get('date_preset') as DatePreset)
    : 'all'
  const staffId = scope === 'mine' ? rawStaffId : undefined

  const [keywordInput, setKeywordInput] = useState(currentKeyword)
  const dateRange = useMemo(() => resolveDateRange(datePreset), [datePreset])
  const todayGroupLabel = beijingNow().format('YYYY-MM-DD')
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({})

  useEffect(() => {
    setKeywordInput(currentKeyword)
  }, [currentKeyword])

  useEffect(() => {
    // 兼容历史企微入口直接落到裸 /wecom/recordings 的情况，统一回到工牌首页。
    if (location.pathname !== '/wecom/recordings' || searchParams.toString()) {
      return
    }
    navigate('/wecom/badge', { replace: true })
  }, [location.pathname, navigate, searchParams])

  function updateSearch(next: {
    keyword?: string
    scope?: Scope
    linkState?: LinkState
    datePreset?: DatePreset
    page?: number
  }) {
    const nextParams = new URLSearchParams()
    const nextKeyword = next.keyword ?? currentKeyword
    const nextScope = next.scope ?? scope
    const nextLinkState = next.linkState ?? linkState
    const nextDatePreset = next.datePreset ?? datePreset
    const nextPage = next.page ?? page

    if (tabIntent) nextParams.set('tab', tabIntent)
    if (visitId) nextParams.set('visit_id', visitId)
    if (nextKeyword) nextParams.set('keyword', nextKeyword)
    if (canFilterMine && nextScope !== 'all') nextParams.set('scope', nextScope)
    if (nextLinkState !== 'all') nextParams.set('link_state', nextLinkState)
    if (nextDatePreset !== 'all') nextParams.set('date_preset', nextDatePreset)
    if (nextPage > 1) nextParams.set('page', String(nextPage))
    setSearchParams(nextParams, { replace: true })
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    updateSearch({ keyword: keywordInput.trim(), page: 1 })
  }

  function goToPage(nextPage: number) {
    updateSearch({ page: nextPage })
  }

  function toggleGroup(label: string) {
    setExpandedGroups((prev) => ({
      ...prev,
      [label]: !(prev[label] ?? isGroupExpandedByDefault(label, todayGroupLabel)),
    }))
  }

  const { data, error, isError, isLoading } = useQuery({
    queryKey: ['wecom', 'recordings', staffId, visitId, currentKeyword, linkState, datePreset, page],
    queryFn: () => fetchArchiveRecordings({
      staff_id: staffId,
      visit_id: visitId || undefined,
      keyword: currentKeyword || undefined,
      link_state: linkState === 'all' ? undefined : linkState,
      sort_mode: linkState === 'all' ? 'date_grouped_link_state' : undefined,
      exclude_filtered: false,
      exclude_quality_filtered: true,
      include_date_summaries: false,
      page,
      page_size: pageSize,
      ...dateRange,
    }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const recordings = data?.items ?? EMPTY_RECORDINGS
  const groups = useMemo(() => groupByDate(recordings), [recordings])
  const dateSummaryByLabel = useMemo(() => {
    const entries = data?.date_summaries ?? []
    return new Map(entries.map((item) => [item.date || '未知日期', item]))
  }, [data?.date_summaries])
  const total = data?.total ?? recordings.length
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const backTo = `${location.pathname}${location.search}`
  const summaryLabel = resolveSummaryLabel(linkState)

  useEffect(() => {
    setExpandedGroups((prev) => {
      const next: Record<string, boolean> = {}
      let changed = false
      for (const group of groups) {
        if (Object.prototype.hasOwnProperty.call(prev, group.label)) {
          next[group.label] = prev[group.label]
          continue
        }
        next[group.label] = isGroupExpandedByDefault(group.label, todayGroupLabel)
        changed = true
      }
      if (!changed && Object.keys(prev).length === Object.keys(next).length) {
        return prev
      }
      return next
    })
  }, [groups, todayGroupLabel])

  return (
    <div className="wc-page">
      <WecomPageIntro
        eyebrow="录音工作台"
        title="录音列表"
        tone="amber"
      />

      <section className="wc-card wc-card--sky wc-card--compact wc-recordings-page__filters-card">
        <form className="wc-filter-bar wc-filter-bar--embedded wc-filter-bar--compact" onSubmit={submitSearch}>
          <div className="wc-filter-bar__search-row wc-filter-bar__search-row--single-action">
            <label className="wc-search wc-search--compact">
              <SearchOutlined />
              <input onChange={(e) => setKeywordInput(e.target.value)} placeholder="搜索录音 / 客户 / 员工" type="search" value={keywordInput} />
            </label>
            <button className="wc-btn wc-btn--primary wc-btn--compact" type="submit">搜索</button>
          </div>
          <div className="wc-filter-bar__row wc-filter-bar__row--filters wc-filter-bar__row--filters--3">
            {canFilterMine ? (
              <select
                className="wc-select wc-select--compact wc-filter-bar__select"
                onChange={(e) => updateSearch({ scope: e.target.value as Scope, page: 1 })}
                value={scope}
              >
                <option value="all">管理范围</option>
                <option value="mine">我的</option>
              </select>
            ) : null}
            <select className="wc-select wc-select--compact wc-filter-bar__select" onChange={(e) => updateSearch({ linkState: e.target.value as LinkState, page: 1 })} value={linkState}>
              <option value="all">全部录音</option>
              <option value="needs_link">待关联接诊</option>
              <option value="linked">已关联接诊</option>
            </select>
            <select
              className="wc-select wc-select--compact wc-filter-bar__select"
              onChange={(e) => updateSearch({ datePreset: e.target.value as DatePreset, page: 1 })}
              value={datePreset}
            >
              {DATE_PRESETS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </div>
        </form>
      </section>

      {visitId && (
        <div className="wc-notice">
          <span>正在查看关联接诊的录音</span>
          <button className="wc-notice__close" onClick={() => setSearchParams((next) => { next.delete('visit_id'); return next })} type="button">
            <CloseCircleOutlined /> 清除
          </button>
        </div>
      )}

      <div className="wc-card wc-recordings-page__list-card">
        <div className="wc-recordings-page__summary">
          <span className="wc-recordings-page__summary-label">{summaryLabel}</span>
          <span className="wc-recordings-page__summary-value">{isError ? '异常' : `${total} 条`}</span>
        </div>

        {isLoading ? (
          <div className="wc-empty">加载中…</div>
        ) : isError ? (
          <div className="wc-empty">录音列表加载失败：{resolveRecordingErrorMessage(error)}</div>
        ) : recordings.length === 0 ? (
          <div className="wc-empty">当前筛选条件下没有录音</div>
        ) : (
          <div className="wc-list">
            {groups.map((group) => (
              <div key={group.label} className="wc-date-sec">
                <button
                  className={`wc-date-sec__hd wc-date-sec__hd--toggle ${
                    expandedGroups[group.label] ?? isGroupExpandedByDefault(group.label, todayGroupLabel)
                      ? 'is-expanded'
                      : 'is-collapsed'
                  }`}
                  onClick={() => toggleGroup(group.label)}
                  type="button"
                >
                  <span>{group.label === '未知日期' ? '未知日期' : dayjs(group.label).format('MM月DD日')}</span>
                  <small>{resolveGroupLinkSummary(dateSummaryByLabel.get(group.label), group.items)}</small>
                  <RightOutlined className="wc-date-sec__toggle-icon" />
                </button>
                {(expandedGroups[group.label] ?? isGroupExpandedByDefault(group.label, todayGroupLabel)) ? (
                  group.items.map((recording) => (
                    <RecordingCard key={recording.id} recording={recording} visitId={visitId || undefined} backTo={backTo} />
                  ))
                ) : null}
              </div>
            ))}
          </div>
        )}

        {totalPages > 1 ? (
          <div className="wc-pagination">
            <button className="wc-btn wc-btn--ghost" type="button" disabled={page <= 1} onClick={() => goToPage(page - 1)}>
              上一页
            </button>
            <span className="wc-pagination__meta">第 {page} / {totalPages} 页</span>
            <button className="wc-btn wc-btn--ghost" type="button" disabled={page >= totalPages} onClick={() => goToPage(page + 1)}>
              下一页
            </button>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function RecordingCard({ recording, visitId, backTo }: { recording: ArchiveRecording; visitId?: string; backTo: string }) {
  const params = new URLSearchParams()
  params.set('archive_item_id', recording.id)
  params.set('back_to', backTo)
  if (visitId) params.set('from_visit_id', visitId)
  const detailId = recording.recording_id || recording.id
  const detailTo = `/wecom/recordings/${detailId}?${params.toString()}`
  const linked = recording.has_visit_link
  const linkedVisitLabel = recording.linked_visit_order_refs[0] || null
  const linkedCustomerName = recording.linked_customer_names[0] || null
  const createdAt = resolveArchiveCreatedAt(recording)
  const durationSeconds = recording.duration_seconds ?? (recording.duration_ms != null ? Math.max(1, Math.round(recording.duration_ms / 1000)) : null)
  const title = formatRecordingDisplayName(recording.display_file_name, createdAt)
  const uploaderName = recording.staff_name || recording.sn || recording.device_code || '未识别员工'
  const badgeLabel = linked ? '已关联接诊' : '待关联接诊'
  const badgeClass = linked ? 'wc-chip--green' : 'wc-chip--amber'
  const cardStateClass = linked ? 'wc-recording-card--linked' : 'wc-recording-card--needs-link'

  return (
    <Link
      className={`wc-row wc-row--stacked wc-row--card wc-recording-card ${cardStateClass}`}
      to={detailTo}
    >
      <div className="wc-row__main">
        <div className="wc-recording-card__top">
          <strong title={title}>{title}</strong>
          <span className={`wc-chip ${badgeClass}`}>
            {badgeLabel}
          </span>
        </div>
        <div className="wc-recording-card__meta">
          <span className="wc-recording-card__meta-item wc-recording-card__meta-item--staff">{uploaderName}</span>
          <span className="wc-recording-card__meta-item wc-recording-card__meta-item--time">{formatRecordingDateTime(createdAt)}</span>
          <span className="wc-recording-card__meta-item wc-recording-card__meta-item--duration">{formatDuration(durationSeconds)}</span>
        </div>
        {linkedCustomerName || linkedVisitLabel ? (
          <div className="wc-recording-card__relation">
            {linkedCustomerName ? (
              <span className="wc-recording-card__relation-pill wc-recording-card__relation-pill--customer">
                客户 {linkedCustomerName}
              </span>
            ) : null}
            {linkedVisitLabel ? (
              <span className="wc-recording-card__relation-pill wc-recording-card__relation-pill--visit">
                接诊 {linkedVisitLabel}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="wc-row__end">
        <RightOutlined className="wc-row__arrow" />
      </div>
    </Link>
  )
}

export default WecomRecordingsPage
