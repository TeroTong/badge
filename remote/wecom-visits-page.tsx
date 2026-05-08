import { AudioOutlined, RightOutlined, SearchOutlined } from '@ant-design/icons'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { HTTPError } from 'ky'
import { Link, useLocation, useSearchParams } from 'react-router-dom'

import { isHospitalAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { fetchVisits, VISIT_STATUS_MAP, type Visit, type VisitDateSummary } from '@/api/visits'
import { WecomPageIntro } from '@/components/wecom-page-intro'
import { beijingNow } from '@/utils/time'

type DatePreset = 'all' | 'today' | '3d' | '7d'
type Scope = 'mine' | 'all'
type RecordingState = 'all' | 'linked' | 'unlinked'

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'all', label: '全部时间' },
  { value: 'today', label: '今天' },
  { value: '3d', label: '近3天' },
  { value: '7d', label: '近7天' },
]
const DATE_PRESET_VALUES = new Set<DatePreset>(DATE_PRESETS.map((item) => item.value))
const EMPTY_VISITS: Visit[] = []

function resolveDateRange(preset: DatePreset) {
  const today = beijingNow()
  switch (preset) {
    case 'today': return { date_from: today.format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '3d': return { date_from: today.subtract(2, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '7d': return { date_from: today.subtract(6, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    default: return {}
  }
}

function groupByDate(visits: Visit[]) {
  const groups: { label: string; items: Visit[] }[] = []
  for (const visit of visits) {
    const dateStr = visit.visit_date ? dayjs(visit.visit_date).format('YYYY-MM-DD') : '未知日期'
    const last = groups[groups.length - 1]
    if (last && last.label === dateStr) { last.items.push(visit) } else { groups.push({ label: dateStr, items: [visit] }) }
  }
  return groups
}

function resolveGroupVisitSummary(summary: VisitDateSummary | undefined, items: Visit[]) {
  return `${summary?.total ?? items.length}条`
}

function resolveVisitsErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看接诊记录'
    }
    if (error.response.status >= 500) {
      return '服务器处理接诊列表时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

export function WecomVisitsPage() {
  const auth = useAuth()
  const location = useLocation()
  const userRole = auth.status === 'authenticated' ? auth.user.role : 'staff'
  const canSeeAll = isHospitalAdminOrAbove(userRole)
  const rawStaffId = auth.status === 'authenticated' ? auth.user.staff_id ?? undefined : undefined

  const [searchParams, setSearchParams] = useSearchParams()
  const currentKeyword = searchParams.get('keyword')?.trim() ?? ''
  const initialPage = Number(searchParams.get('page') ?? '1') || 1
  const page = initialPage < 1 ? 1 : initialPage
  const pageSize = 20
  const scope: Scope = canSeeAll && searchParams.get('scope') === 'mine' ? 'mine' : 'all'
  const recordingState: RecordingState = searchParams.get('recording_state') === 'linked' || searchParams.get('recording_state') === 'unlinked'
    ? (searchParams.get('recording_state') as RecordingState)
    : 'all'
  const datePreset: DatePreset = DATE_PRESET_VALUES.has(searchParams.get('date_preset') as DatePreset)
    ? (searchParams.get('date_preset') as DatePreset)
    : 'all'

  const participantFilterId = canSeeAll && scope === 'mine' ? rawStaffId : undefined

  const [keywordInput, setKeywordInput] = useState(currentKeyword)
  const dateRange = useMemo(() => resolveDateRange(datePreset), [datePreset])

  useEffect(() => {
    setKeywordInput(currentKeyword)
  }, [currentKeyword])

  function updateSearch(next: {
    keyword?: string
    scope?: Scope
    recordingState?: RecordingState
    datePreset?: DatePreset
    page?: number
  }) {
    const nextParams = new URLSearchParams()
    const nextKeyword = next.keyword ?? currentKeyword
    const nextScope = next.scope ?? scope
    const nextRecordingState = next.recordingState ?? recordingState
    const nextDatePreset = next.datePreset ?? datePreset
    const nextPage = next.page ?? page

    if (nextKeyword) nextParams.set('keyword', nextKeyword)
    if (canSeeAll && nextScope !== 'all') nextParams.set('scope', nextScope)
    if (nextRecordingState !== 'all') nextParams.set('recording_state', nextRecordingState)
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

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['wecom', 'visits', userRole, rawStaffId, participantFilterId, currentKeyword, recordingState, datePreset, page],
    queryFn: () => fetchVisits({
      participant_staff_id: participantFilterId,
      keyword: currentKeyword || undefined,
      has_recordings: recordingState === 'all' ? undefined : recordingState === 'linked',
      include_date_summaries: false,
      page,
      page_size: pageSize,
      ...dateRange,
    }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const visits = data?.items ?? EMPTY_VISITS
  const groups = useMemo(() => groupByDate(visits), [visits])
  const dateSummaryByLabel = useMemo(() => {
    const entries = data?.date_summaries ?? []
    return new Map(entries.map((item) => [item.date || '未知日期', item]))
  }, [data?.date_summaries])
  const total = data?.total ?? visits.length
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const activeKeyword = currentKeyword
  const introDescription = activeKeyword
    ? `搜索“${activeKeyword}”`
    : '按时间和录音情况查看接诊单'
  const backTo = `${location.pathname}${location.search}`

  return (
    <div className="wc-page">
      <WecomPageIntro
        description={introDescription}
        eyebrow="接诊工作台"
        title="接诊列表"
        tone="violet"
      />

      <form className="wc-filter-bar wc-filter-bar--compact" onSubmit={submitSearch}>
        <div className="wc-filter-bar__search-row wc-filter-bar__search-row--single-action">
          <label className="wc-search wc-search--compact">
            <SearchOutlined />
            <input onChange={(e) => setKeywordInput(e.target.value)} placeholder="搜索客户 / 接诊单号" type="search" value={keywordInput} />
          </label>
          <button className="wc-btn wc-btn--primary wc-btn--compact" type="submit">搜索</button>
        </div>
        <div className="wc-filter-bar__row wc-filter-bar__row--filters wc-filter-bar__row--filters--4">
          {canSeeAll ? (
            <select
              className="wc-select wc-select--compact wc-filter-bar__select"
              onChange={(e) => updateSearch({ scope: e.target.value as Scope, page: 1 })}
              value={scope}
            >
              <option value="all">全部员工</option>
              <option value="mine">我的</option>
            </select>
          ) : null}
          <select className="wc-select wc-select--compact wc-filter-bar__select" onChange={(e) => updateSearch({ recordingState: e.target.value as RecordingState, page: 1 })} value={recordingState}>
            <option value="all">录音全部</option>
            <option value="linked">已关联录音</option>
            <option value="unlinked">未关联录音</option>
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

      <div className="wc-card">
        <div className="wc-card__head">
          <h2 className="wc-card__title">接诊列表</h2>
          <span className="wc-badge">{total}</span>
        </div>

        {isLoading ? (
          <div className="wc-empty">加载中…</div>
        ) : isError ? (
          <div className="wc-empty">接诊列表加载失败：{resolveVisitsErrorMessage(error)}</div>
        ) : visits.length === 0 ? (
          <div className="wc-empty">当前筛选条件下没有接诊记录</div>
        ) : (
          <div className="wc-list">
            {groups.map((group) => (
              <div key={group.label} className="wc-date-sec">
                <div className="wc-date-sec__hd">
                  <span>{group.label === '未知日期' ? '未知日期' : dayjs(group.label).format('MM月DD日')}</span>
                  <small>{resolveGroupVisitSummary(dateSummaryByLabel.get(group.label), group.items)}</small>
                </div>
                {group.items.map((visit) => (
                  <VisitCard key={visit.id} visit={visit} backTo={backTo} />
                ))}
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

function VisitCard({ visit, backTo }: { visit: Visit; backTo: string }) {
  const status = VISIT_STATUS_MAP[visit.status]
  const summaryParts = [
    visit.arrival_purpose || null,
    visit.doctor_name ? `医生 ${visit.doctor_name}` : null,
    `${visit.recording_count} 条录音`,
  ].filter(Boolean)
  const detailParams = new URLSearchParams()
  detailParams.set('back_to', backTo)

  return (
    <article className="wc-item">
      <div className="wc-item__head">
        <div>
          <h3 className="wc-item__name">{visit.customer_name}</h3>
          <span className="wc-item__sub">
            {visit.consultant_name || '未分配'}
            {visit.visit_time ? ` · ${visit.visit_time.slice(0, 5)}` : ''}
          </span>
        </div>
        <div className="wc-item__chips">
          <span className={`wc-chip ${visit.recording_count > 0 ? 'wc-chip--green' : 'wc-chip--amber'}`}>
            {visit.recording_count > 0 ? '已关联录音' : '待关联录音'}
          </span>
          {visit.customer_type_label ? (
            <span className={`wc-chip ${visit.customer_type_code === 'V' ? 'wc-chip--green' : 'wc-chip--blue'}`}>
              {visit.customer_type_label}
            </span>
          ) : null}
          {visit.deal_status && (
            <span className={`wc-chip wc-chip--${visit.deal_status === '已成交' ? 'success' : visit.deal_status === '未成交' ? 'danger' : 'default'}`}>{visit.deal_status}</span>
          )}
          <span className="wc-chip wc-chip--blue">{status?.label ?? visit.status}</span>
        </div>
      </div>

      <span className="wc-item__sub">
        {visit.visit_date ? dayjs(visit.visit_date).format('MM/DD') : '未知日期'}
        {visit.visit_time ? ` ${visit.visit_time.slice(0, 5)}` : ''}
        {summaryParts.length > 0 ? ` · ${summaryParts.join(' · ')}` : ''}
      </span>

      <div className="wc-item__actions">
        <Link className="wc-btn wc-btn--ghost" to={`/wecom/recordings?visit_id=${visit.id}`} onClick={(e) => e.stopPropagation()}>
          <AudioOutlined /> {visit.recording_count > 0 ? '查看录音' : '去关联录音'}
        </Link>
        <Link className="wc-btn wc-btn--primary" to={`/wecom/visits/${visit.id}?${detailParams.toString()}`} onClick={(e) => e.stopPropagation()}>
          接诊详情 <RightOutlined />
        </Link>
      </div>
    </article>
  )
}

export default WecomVisitsPage
