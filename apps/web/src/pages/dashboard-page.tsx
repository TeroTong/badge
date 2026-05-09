import { useQuery } from '@tanstack/react-query'
import { HTTPError } from 'ky'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { isHospitalAdminOrAbove, roleLabel } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import {
  fetchDashboard,
  type ConcernTypeItem,
  type DashboardBreakdownItem,
  type ProcessEvaluationIssueItem,
  type ProcessEvaluationSectionStats,
  type ProcessEvaluationSummaryStats,
  type StaffStatsItem,
} from '@/api/dashboard'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow, formatBeijingTime } from '@/utils/time'

type DatePreset = 'today' | '3d' | '7d' | 'all'
type DashboardScopeMode = 'all' | 'mine'
type OverviewMetricTone = 'blue' | 'green' | 'amber' | 'slate'

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'today', label: '今天' },
  { value: '3d', label: '近3天' },
  { value: '7d', label: '近7天' },
  { value: 'all', label: '全部时间' },
]

const STAFF_PAGE_SIZE = 12

function resolveStaffScope(staffId: string | null | undefined) {
  return staffId ?? undefined
}

function resolveDateRange(preset: DatePreset) {
  const today = beijingNow()
  switch (preset) {
    case 'today':
      return { date_from: today.format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '3d':
      return { date_from: today.subtract(2, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    case '7d':
      return { date_from: today.subtract(6, 'day').format('YYYY-MM-DD'), date_to: today.format('YYYY-MM-DD') }
    default:
      return {}
  }
}

function formatInteger(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '--'
  return Math.round(value).toLocaleString('zh-CN')
}

function formatDecimal(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '--'
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function formatPercentValue(value: number | null | undefined) {
  if (value == null || Number.isNaN(value)) return '--'
  return `${Math.round(value)}%`
}

function resolveOverviewErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看'
    }
    if (error.response.status >= 500) {
      return '服务器处理请求时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

function getBreakdownLabel(item: DashboardBreakdownItem) {
  return item.label || item.detail || item.key || '未命名'
}

function getBreakdownMeta(item: DashboardBreakdownItem) {
  const parts = [`${formatInteger(item.count)}次`]
  if (item.customer_count > 0) parts.push(`${formatInteger(item.customer_count)}客`)
  if (item.task_count > 0) parts.push(`${formatInteger(item.task_count)}录音`)
  return parts.join(' · ')
}

function OverviewMetricCard({
  label,
  value,
  hint,
  tone = 'slate',
}: {
  label: string
  value: string
  hint?: string
  tone?: OverviewMetricTone
}) {
  return (
    <article className={`admin-overview-metric admin-overview-metric--${tone}`}>
      <div className="admin-overview-metric__top">
        <span className="admin-overview-metric__label">{label}</span>
        <span className="admin-overview-metric__dot" aria-hidden="true" />
      </div>
      <strong className="admin-overview-metric__value">{value}</strong>
      <small
        aria-hidden={!hint}
        className={`admin-overview-metric__hint${hint ? '' : ' admin-overview-metric__hint--placeholder'}`}
      >
        {hint ?? '\u00A0'}
      </small>
    </article>
  )
}

function formatBreakdownPercent(count: number, maxCount: number) {
  if (!maxCount || maxCount <= 0) return '0%'
  return `${Math.max(6, Math.min(100, Math.round((count / maxCount) * 100)))}%`
}

function BreakdownList({
  items,
  emptyText,
}: {
  items: DashboardBreakdownItem[]
  emptyText: string
}) {
  if (items.length === 0) {
    return <div className="admin-overview-empty admin-overview-empty--compact">{emptyText}</div>
  }

  const maxCount = Math.max(...items.map((item) => item.count), 1)

  return (
    <div className="admin-overview-breakdown">
      {items.map((item, index) => (
        <div key={`${item.key}-${index}`} className="admin-overview-breakdown__item">
          <span className="admin-overview-breakdown__rank">{index + 1}</span>
          <div className="admin-overview-breakdown__content">
            <strong>{getBreakdownLabel(item)}</strong>
            <small>{getBreakdownMeta(item)}</small>
            <span className="admin-overview-breakdown__meter" aria-hidden="true">
              <i style={{ width: formatBreakdownPercent(item.count, maxCount) }} />
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

function ConcernList({ items }: { items: ConcernTypeItem[] }) {
  if (items.length === 0) {
    return <div className="admin-overview-empty admin-overview-empty--compact">当前范围内还没有提取到客户顾虑。</div>
  }

  const maxCount = Math.max(...items.map((item) => item.count), 1)

  return (
    <div className="admin-overview-breakdown admin-overview-breakdown--concerns">
      {items.map((item, index) => (
        <div key={`${item.type}-${index}`} className="admin-overview-breakdown__item">
          <span className="admin-overview-breakdown__rank">{index + 1}</span>
          <div className="admin-overview-breakdown__content">
            <strong>{item.type || '未分类顾虑'}</strong>
            <small>{formatInteger(item.count)}次提及</small>
            <span className="admin-overview-breakdown__meter" aria-hidden="true">
              <i style={{ width: formatBreakdownPercent(item.count, maxCount) }} />
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

function ProcessIssuePanel({
  issues,
  title,
  onClose,
}: {
  issues: ProcessEvaluationIssueItem[]
  title: string
  onClose: () => void
}) {
  return (
    <div className="admin-overview-issues">
      <div className="admin-overview-issues__head">
        <div>
          <strong>{title}</strong>
          <span>{formatInteger(issues.length)} 个具体问题</span>
        </div>
        <button onClick={onClose} type="button">收起</button>
      </div>
      {issues.length === 0 ? (
        <div className="admin-overview-empty admin-overview-empty--compact">当前范围内没有可展示的问题明细。</div>
      ) : (
        <div className="admin-overview-issues__list">
          {issues.map((issue, index) => {
            const displayName = formatRecordingDisplayName(issue.file_name, issue.recorded_at)
            const recordedAt = issue.recorded_at ? formatBeijingTime(issue.recorded_at, 'MM-DD HH:mm') : '未知时间'
            const checkpoint = [issue.checkpoint_code, issue.checkpoint_name].filter(Boolean).join(' ')
            return (
              <article
                key={`${issue.analysis_task_id}-${issue.section_code}-${issue.checkpoint_code ?? 'section'}-${index}`}
                className="admin-overview-issue-card"
              >
                <div className="admin-overview-issue-card__top">
                  <span>{issue.section_name}</span>
                  {checkpoint ? <em>{checkpoint}</em> : null}
                </div>
                <p>{issue.description || '未填写问题描述'}</p>
                <div className="admin-overview-issue-card__meta">
                  <span>{issue.staff_name || '未绑定员工'}</span>
                  <span>{recordedAt}</span>
                  <Link to={`/admin/recordings/${issue.recording_id}`}>{displayName}</Link>
                </div>
                {issue.evidence ? (
                  <details className="admin-overview-issue-card__evidence">
                    <summary>查看原文证据</summary>
                    <pre>{issue.evidence}</pre>
                  </details>
                ) : null}
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ProcessEvaluationCard({
  summary,
  sections,
  issues,
  className,
}: {
  summary?: ProcessEvaluationSummaryStats
  sections: ProcessEvaluationSectionStats[]
  issues: ProcessEvaluationIssueItem[]
  className?: string
}) {
  const [selectedIssueSection, setSelectedIssueSection] = useState<string | null>(null)
  const cardClassName = ['admin-overview-card', className].filter(Boolean).join(' ')

  if (!summary || summary.evaluated_count === 0) {
    return (
      <section className={cardClassName}>
        <div className="admin-overview-card__head">
          <h3>面诊过程评价</h3>
          <span>暂无统计</span>
        </div>
        <div className="admin-overview-empty">当前范围内还没有可统计的面诊过程评价。</div>
      </section>
    )
  }

  const selectedIssues = selectedIssueSection === 'all'
    ? issues
    : issues.filter((issue) => issue.section_code === selectedIssueSection)
  const selectedSectionName = selectedIssueSection === 'all'
    ? '全部面诊过程问题'
    : sections.find((section) => section.code === selectedIssueSection)?.name ?? '面诊过程问题'

  return (
    <section className={cardClassName}>
      <div className="admin-overview-card__head">
        <h3>面诊过程评价</h3>
        <span>{formatInteger(summary.evaluated_count)}条已评价</span>
      </div>
      <div className="admin-overview-process">
        <div className="admin-overview-process__summary">
          <div>
            <span>平均得分</span>
            <strong>{formatDecimal(summary.avg_total_score)}分</strong>
            <small>满分 {formatDecimal(summary.max_total_score)}分</small>
          </div>
          <div>
            <span>达标率</span>
            <strong>{formatPercentValue(summary.pass_rate)}</strong>
            <small>平均达标 {formatDecimal(summary.avg_passed_sections)} 项</small>
          </div>
          <button
            className="admin-overview-process__issue-trigger"
            disabled={summary.issue_count <= 0}
            onClick={() => setSelectedIssueSection((current) => (current === 'all' ? null : 'all'))}
            type="button"
          >
            <span>问题数</span>
            <strong>{formatInteger(summary.issue_count)}</strong>
          </button>
        </div>
        <div className="admin-overview-process__sections">
          {sections.map((section) => (
            <div key={section.code || section.name} className="admin-overview-process__section">
              <div className="admin-overview-process__section-top">
                <div className="admin-overview-process__section-copy">
                  <strong>{section.name}</strong>
                  <div className="admin-overview-process__section-meta">
                    <small>达标 {formatPercentValue(section.pass_rate)}</small>
                    {section.issue_count > 0 ? (
                      <button
                        className="admin-overview-process__section-btn"
                        onClick={() => setSelectedIssueSection((current) => (current === section.code ? null : section.code))}
                        type="button"
                      >
                        {formatInteger(section.issue_count)}问题
                      </button>
                    ) : null}
                  </div>
                </div>
                <span>{formatDecimal(section.avg_score)}分</span>
              </div>
              <div className="admin-overview-process__section-bar">
                <i style={{ width: `${Math.max(0, Math.min(section.pass_rate, 100))}%` }} />
              </div>
            </div>
          ))}
        </div>
      </div>
      {selectedIssueSection ? (
        <ProcessIssuePanel
          issues={selectedIssues}
          onClose={() => setSelectedIssueSection(null)}
          title={selectedSectionName}
        />
      ) : null}
    </section>
  )
}

function StaffStatsTable({ items }: { items: StaffStatsItem[] }) {
  if (items.length === 0) {
    return <div className="admin-overview-empty admin-overview-empty--compact">当前范围内还没有绑定工牌员工数据。</div>
  }

  return (
    <div className="admin-overview-staff-table" role="table" aria-label="员工统计明细">
      <div className="admin-overview-staff-table__row admin-overview-staff-table__row--head" role="row">
        <span role="columnheader">员工</span>
        <span role="columnheader">录音</span>
        <span role="columnheader">关联</span>
        <span role="columnheader">接诊</span>
        <span role="columnheader">成交</span>
        <span role="columnheader">均分</span>
      </div>
      {items.map((item) => (
        <div key={item.staff_id} className="admin-overview-staff-table__row" role="row">
          <div className="admin-overview-staff-table__staff" role="cell">
            <strong>{item.staff_name}</strong>
            {item.job_label ? <small>{item.job_label}</small> : null}
          </div>
          <span role="cell">{formatInteger(item.recording_count)}</span>
          <span role="cell">{formatInteger(item.linked_visit_count)}</span>
          <span role="cell">{formatInteger(item.visit_count)}</span>
          <span role="cell">{formatInteger(item.closed_won_count)}</span>
          <span role="cell">{item.avg_score == null ? '--' : formatDecimal(item.avg_score)}</span>
        </div>
      ))}
    </div>
  )
}

export function DashboardPage() {
  const auth = useAuth()
  const userRole = auth.status === 'authenticated' ? auth.user.role : 'staff'
  const canSeeScopedData = isHospitalAdminOrAbove(userRole)
  const rawStaffId = auth.status === 'authenticated' ? resolveStaffScope(auth.user.staff_id) : undefined
  const authHospitalName = auth.status === 'authenticated' ? auth.user.hospital_name ?? null : null

  const [datePreset, setDatePreset] = useState<DatePreset>('today')
  const [dashboardScope, setDashboardScope] = useState<DashboardScopeMode>('all')
  const [selectedHospitalCode, setSelectedHospitalCode] = useState('')
  const [selectedStaffId, setSelectedStaffId] = useState('')
  const [staffPage, setStaffPage] = useState(1)

  const dateRange = useMemo(() => resolveDateRange(datePreset), [datePreset])
  const requestedScope: DashboardScopeMode = !canSeeScopedData && rawStaffId
    ? 'mine'
    : dashboardScope === 'mine' && rawStaffId
      ? 'mine'
      : 'all'
  const requestedHospitalCode = requestedScope === 'all' ? selectedHospitalCode || undefined : undefined
  const requestedStaffId = requestedScope === 'all' ? selectedStaffId || undefined : undefined

  const {
    data: dashboard,
    error: dashboardError,
    isError: dashboardIsError,
    isLoading: dashboardLoading,
  } = useQuery({
    queryKey: ['admin', 'dashboard', userRole, rawStaffId, datePreset, requestedScope, requestedHospitalCode ?? null, requestedStaffId ?? null],
    queryFn: () =>
      fetchDashboard({
        hospital_code: requestedHospitalCode,
        scope_mode: requestedScope,
        staff_id: requestedStaffId,
        detail_level: 'summary',
        ...dateRange,
      }),
  })

  const canSelectOverviewScope = dashboard?.dashboard_can_select_scope ?? (canSeeScopedData && Boolean(rawStaffId))
  const canSelectHospital = requestedScope === 'all' && (dashboard?.dashboard_can_select_hospital ?? false)
  const activeHospitalCode = dashboard?.dashboard_hospital_code ?? requestedHospitalCode ?? null
  const hospitalSelectValue = requestedHospitalCode ?? activeHospitalCode ?? ''
  const canSelectScopeTarget = canSelectOverviewScope || (dashboard?.dashboard_can_select_staff ?? false)
  const scopeSelectValue = requestedStaffId
    ? `staff:${requestedStaffId}`
    : requestedScope
  const displayHospitalName = dashboard?.dashboard_hospital_name ?? authHospitalName
  const totalRecordings = dashboard?.total_recordings ?? 0
  const recordingsWithVisits = dashboard?.recordings_with_visits ?? 0
  const processSummary = dashboard?.process_evaluation_summary
  const analysisSampleCount = processSummary?.evaluated_count
    ?? dashboard?.result_analysis_modules?.[0]?.analyzed_count
    ?? dashboard?.done_count
    ?? 0
  const hotIndications = (dashboard?.indication_breakdown ?? []).slice(0, 5)
  const hotTags = (dashboard?.tag_breakdown ?? []).slice(0, 5)
  const hotConcerns = (dashboard?.concern_types ?? []).slice(0, 5)
  const staffStats = useMemo(() => {
    const items = [...(dashboard?.staff_stats ?? [])]
    const scopedItems = requestedStaffId
      ? items.filter((item) => item.staff_id === requestedStaffId)
      : canSeeScopedData || !rawStaffId
        ? items
        : items.filter((item) => item.staff_id === rawStaffId)
    return scopedItems.sort((left, right) => (
      right.recording_count - left.recording_count
      || right.linked_visit_count - left.linked_visit_count
      || right.visit_count - left.visit_count
      || right.closed_won_count - left.closed_won_count
      || (right.avg_score ?? -1) - (left.avg_score ?? -1)
      || left.staff_name.localeCompare(right.staff_name, 'zh-CN')
    ))
  }, [dashboard, canSeeScopedData, rawStaffId, requestedStaffId])

  const staffPageCount = Math.max(1, Math.ceil(staffStats.length / STAFF_PAGE_SIZE))
  const normalizedStaffPage = Math.min(staffPage, staffPageCount)
  const visibleStaffStats = staffStats.slice(
    (normalizedStaffPage - 1) * STAFF_PAGE_SIZE,
    normalizedStaffPage * STAFF_PAGE_SIZE,
  )

  return (
    <div className="admin-overview-page">
      <header className="admin-overview-page__header">
        <div className="admin-overview-page__header-copy">
          <p className="admin-overview-page__eyebrow">管理驾驶舱</p>
          <h1>数据总览</h1>
          <p>
            聚合录音样本、过程评价、客户信号和员工表现，帮助管理者快速定位复盘重点。
          </p>
        </div>
        <div className="admin-overview-page__context">
          <span>{roleLabel(userRole)}</span>
          <strong>{displayHospitalName || '全局视角'}</strong>
        </div>
      </header>

      <section className="admin-overview-card admin-overview-card--toolbar">
        <div className="admin-overview-toolbar">
          <div className="admin-overview-toolbar__filters">
            {canSelectHospital ? (
              <label className="admin-overview-toolbar__field">
                <span>机构范围</span>
                <select
                  onChange={(event) => {
                    setSelectedHospitalCode(event.target.value)
                    setSelectedStaffId('')
                    setStaffPage(1)
                  }}
                  value={hospitalSelectValue}
                >
                  {dashboard?.dashboard_hospital_options.map((item) => (
                    <option key={item.hospital_code} value={item.hospital_code}>
                      {item.hospital_name}
                    </option>
                  ))}
                </select>
              </label>
            ) : canSeeScopedData && displayHospitalName ? (
              <div className="admin-overview-toolbar__chip">{displayHospitalName}</div>
            ) : null}
            {canSelectScopeTarget ? (
              <label className="admin-overview-toolbar__field">
                <span>查看范围</span>
                <select
                  onChange={(event) => {
                    const nextValue = event.target.value
                    if (nextValue === 'all') {
                      setDashboardScope('all')
                      setSelectedStaffId('')
                    } else if (nextValue === 'mine') {
                      setDashboardScope('mine')
                      setSelectedStaffId('')
                    } else if (nextValue.startsWith('staff:')) {
                      setDashboardScope('all')
                      setSelectedStaffId(nextValue.slice('staff:'.length))
                    }
                    setStaffPage(1)
                  }}
                  value={scopeSelectValue}
                >
                  <option value="all">全部员工</option>
                  {canSelectOverviewScope ? <option value="mine">我的</option> : null}
                  {(dashboard?.dashboard_staff_options ?? []).map((item) => (
                    <option key={item.staff_id} value={`staff:${item.staff_id}`}>{item.staff_name}</option>
                  ))}
                </select>
              </label>
            ) : null}
            <label className="admin-overview-toolbar__field">
              <span>时间范围</span>
              <select
                onChange={(event) => {
                  setDatePreset(event.target.value as DatePreset)
                  setStaffPage(1)
                }}
                value={datePreset}
              >
                {DATE_PRESETS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
          </div>
        </div>
      </section>

      {dashboardLoading ? (
        <div className="admin-overview-empty">总览加载中…</div>
      ) : dashboardIsError || !dashboard ? (
        <div className="admin-overview-empty">总览加载失败：{resolveOverviewErrorMessage(dashboardError)}</div>
      ) : (
        <>
          <section className="admin-overview-hero">
            <OverviewMetricCard
              label="录音样本"
              tone="blue"
              value={formatInteger(totalRecordings)}
            />
            <OverviewMetricCard
              hint={formatPercentValue(totalRecordings > 0 ? (recordingsWithVisits / totalRecordings) * 100 : null)}
              label="关联接诊"
              tone="green"
              value={formatInteger(recordingsWithVisits)}
            />
            <OverviewMetricCard
              label="到院次数"
              tone="amber"
              value={formatInteger(dashboard.total_customers)}
            />
            <OverviewMetricCard
              hint={`满分${formatDecimal(processSummary?.max_total_score ?? 9)}`}
              label="过程均分"
              tone="slate"
              value={processSummary?.avg_total_score == null ? '--' : `${formatDecimal(processSummary.avg_total_score)}分`}
            />
          </section>

          <div className="admin-overview-board">
            <ProcessEvaluationCard
              className="admin-overview-card--process"
              issues={dashboard.process_evaluation_issues ?? []}
              sections={dashboard.process_evaluation_sections ?? []}
              summary={processSummary}
            />

            <div className="admin-overview-board__side">
              <article className="admin-overview-card admin-overview-card--result">
                <div className="admin-overview-card__head">
                  <h3>面诊结果分析</h3>
                  <span>{formatInteger(analysisSampleCount)}条样本</span>
                </div>
                <div className="admin-overview-insights">
                  <div>
                    <span>平均适应症数量</span>
                    <strong>{formatDecimal(dashboard.avg_indication_count)}</strong>
                  </div>
                  <div>
                    <span>平均顾客标签数</span>
                    <strong>{formatDecimal(dashboard.avg_tag_count)}</strong>
                  </div>
                </div>
              </article>

              <section className="admin-overview-card admin-overview-card--signals">
                <div className="admin-overview-card__head">
                  <h3>业务信号</h3>
                </div>
                <div className="admin-overview-signals">
                  <article className="admin-overview-signal-card">
                    <div className="admin-overview-signal-card__head">
                      <strong>高频适应症</strong>
                      <span>Top 5</span>
                    </div>
                    <BreakdownList emptyText="当前范围内还没有适应症数据。" items={hotIndications} />
                  </article>
                  <article className="admin-overview-signal-card">
                    <div className="admin-overview-signal-card__head">
                      <strong>客户顾虑</strong>
                      <span>Top 5</span>
                    </div>
                    <ConcernList items={hotConcerns} />
                  </article>
                  <article className="admin-overview-signal-card">
                    <div className="admin-overview-signal-card__head">
                      <strong>顾客标签</strong>
                      <span>Top 5</span>
                    </div>
                    <BreakdownList emptyText="当前范围内还没有顾客标签数据。" items={hotTags} />
                  </article>
                </div>
              </section>
            </div>

            <section className="admin-overview-card admin-overview-card--staff">
              <div className="admin-overview-card__head">
                <h3>员工统计明细</h3>
                <span>{formatInteger(staffStats.length)}人</span>
              </div>
              <StaffStatsTable items={visibleStaffStats} />
              {staffPageCount > 1 ? (
                <div className="admin-overview-staff-pagination">
                  <button
                    className="admin-overview-staff-pagination__btn"
                    disabled={normalizedStaffPage <= 1}
                    onClick={() => setStaffPage((page) => Math.max(1, page - 1))}
                    type="button"
                  >
                    上一页
                  </button>
                  <span className="admin-overview-staff-pagination__indicator">
                    {normalizedStaffPage} / {staffPageCount}
                  </span>
                  <button
                    className="admin-overview-staff-pagination__btn"
                    disabled={normalizedStaffPage >= staffPageCount}
                    onClick={() => setStaffPage((page) => Math.min(staffPageCount, page + 1))}
                    type="button"
                  >
                    下一页
                  </button>
                </div>
              ) : null}
            </section>
          </div>
        </>
      )}
    </div>
  )
}

export default DashboardPage
