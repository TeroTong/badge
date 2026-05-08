import { CalendarOutlined, IdcardOutlined, ProfileOutlined, RightOutlined, SearchOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { HTTPError } from 'ky'
import { useMemo, useState, type FormEvent } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'

import { isHospitalAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { fetchCustomers, type Customer, type CustomerDateSummary } from '@/api/customers'
import { WecomPageIntro } from '@/components/wecom-page-intro'
import { beijingNow, formatBeijingTime } from '@/utils/time'

type DatePreset = 'all' | 'today' | '3d' | '7d'
type Scope = 'mine' | 'all'
type RecordingFilter = 'linked' | 'all' | 'unlinked'

const GENDER_LABELS: Record<string, string> = {
  male: '男',
  female: '女',
  unknown: '未知',
  男: '男',
  女: '女',
  未知: '未知',
}

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'all', label: '全部时间' },
  { value: 'today', label: '今天' },
  { value: '3d', label: '近3天' },
  { value: '7d', label: '近7天' },
]
const DEFAULT_DATE_PRESET: DatePreset = 'all'
const DATE_PRESET_VALUES = new Set<DatePreset>(DATE_PRESETS.map((item) => item.value))
function formatVisitTime(value: string | null) {
  if (!value) return '暂无来访'
  return formatBeijingTime(value, 'MM/DD HH:mm')
}

function groupCustomersByDate(customers: Customer[]) {
  const groups: { label: string; items: Customer[] }[] = []
  for (const customer of customers) {
    const dateStr = customer.last_visit_at ? formatBeijingTime(customer.last_visit_at, 'YYYY-MM-DD') : '暂无来访'
    const last = groups[groups.length - 1]
    if (last && last.label === dateStr) {
      last.items.push(customer)
    } else {
      groups.push({ label: dateStr, items: [customer] })
    }
  }
  return groups
}

function resolveGroupCustomerSummary(summary: CustomerDateSummary | undefined, items: Customer[]) {
  return `${summary?.total ?? items.length}位`
}

function buildCustomerMetaTags(customer: {
  gender: string | null
  age: number | null
  customer_type_label: string | null
}) {
  const tags: string[] = []
  if (customer.customer_type_label) tags.push(customer.customer_type_label)
  if (customer.gender) tags.push(GENDER_LABELS[customer.gender] ?? customer.gender)
  tags.push(customer.age != null && customer.age > 0 ? `${customer.age}岁` : '-岁')
  return tags
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

function resolveCustomersErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看最近上门客户'
    }
    if (error.response.status >= 500) {
      return '服务器处理最近上门客户列表时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

export function WecomCustomersPage() {
  const auth = useAuth()
  const location = useLocation()
  const userRole = auth.status === 'authenticated' ? auth.user.role : 'staff'
  const canSeeAll = isHospitalAdminOrAbove(userRole)
  const rawStaffId = auth.status === 'authenticated' ? auth.user.staff_id ?? undefined : undefined
  const [searchParams, setSearchParams] = useSearchParams()
  const currentKeyword = searchParams.get('q')?.trim() ?? ''
  const scope: Scope = canSeeAll && searchParams.get('scope') === 'mine' ? 'mine' : 'all'
  const recordingFilter: RecordingFilter =
    searchParams.get('recordings') === 'all' || searchParams.get('recordings') === 'unlinked'
      ? (searchParams.get('recordings') as RecordingFilter)
      : 'linked'
  const datePreset: DatePreset = DATE_PRESET_VALUES.has(searchParams.get('date_preset') as DatePreset)
    ? (searchParams.get('date_preset') as DatePreset)
    : DEFAULT_DATE_PRESET
  const initialPage = Number(searchParams.get('page') ?? '1') || 1
  const page = initialPage < 1 ? 1 : initialPage
  const pageSize = 12
  const [keyword, setKeyword] = useState(currentKeyword)

  const consultantId = scope === 'mine' ? rawStaffId : undefined
  const dateRange = useMemo(() => resolveDateRange(datePreset), [datePreset])

  function updateSearch(next: {
    keyword?: string
    scope?: Scope
    recordingFilter?: RecordingFilter
    datePreset?: DatePreset
    page?: number
  }) {
    const nextParams = new URLSearchParams()
    const nextKeyword = next.keyword ?? currentKeyword
    const nextScope = next.scope ?? scope
    const nextRecordingFilter = next.recordingFilter ?? recordingFilter
    const nextDatePreset = next.datePreset ?? datePreset
    const nextPage = next.page ?? page

    if (nextKeyword) nextParams.set('q', nextKeyword)
    if (canSeeAll && nextScope !== 'all') nextParams.set('scope', nextScope)
    if (nextRecordingFilter !== 'linked') nextParams.set('recordings', nextRecordingFilter)
    if (nextDatePreset !== DEFAULT_DATE_PRESET) nextParams.set('date_preset', nextDatePreset)
    nextParams.set('page', String(nextPage))
    setSearchParams(nextParams)
  }

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['wecom', 'customers', currentKeyword, recordingFilter, datePreset, consultantId, page],
    queryFn: () =>
      fetchCustomers({
        keyword: currentKeyword || undefined,
        consultant_id: consultantId,
        has_recordings:
          recordingFilter === 'all'
            ? undefined
            : recordingFilter === 'linked',
        include_date_summaries: false,
        fast_page: true,
        ...dateRange,
        page,
        page_size: pageSize,
      }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const items = useMemo(() => {
    const rows = [...(data?.items ?? [])]
    rows.sort((a, b) => {
      const aTime = a.last_visit_at ? dayjs(a.last_visit_at).valueOf() : 0
      const bTime = b.last_visit_at ? dayjs(b.last_visit_at).valueOf() : 0
      if (aTime !== bTime) return bTime - aTime
      return dayjs(b.created_at).valueOf() - dayjs(a.created_at).valueOf()
    })
    return rows
  }, [data?.items])
  const groups = useMemo(() => groupCustomersByDate(items), [items])
  const dateSummaryByLabel = useMemo(() => {
    const entries = data?.date_summaries ?? []
    return new Map(entries.map((item) => [item.date || '暂无来访', item]))
  }, [data?.date_summaries])

  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const hasQuery = currentKeyword.length > 0
  const backTo = `${location.pathname}${location.search}`

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    updateSearch({ keyword: keyword.trim(), page: 1 })
  }

  function goToPage(nextPage: number) {
    updateSearch({ page: nextPage })
  }

  const listTitle = hasQuery ? '最近上门客户搜索结果' : '最近上门客户'

  const emptyText = hasQuery ? '没有找到匹配的最近上门客户' : '当前暂无最近上门客户'

  return (
    <div className="wc-page wc-customers-page">
      <WecomPageIntro
        eyebrow="客户工作台"
        tone="mint"
      />

      <section className="wc-card wc-card--sky wc-card--compact wc-customers-page__filters-card">
        <form className="wc-filter-bar wc-filter-bar--embedded wc-filter-bar--compact" onSubmit={submitSearch}>
          <div className="wc-filter-bar__search-row">
            <label className="wc-search wc-search--compact">
              <SearchOutlined />
              <input
                value={keyword}
                onChange={(event) => setKeyword(event.target.value)}
                placeholder="搜索客户编号或姓名"
                type="search"
              />
            </label>
            <button className="wc-btn wc-btn--primary wc-btn--compact" type="submit">搜索</button>
          </div>
          <div className="wc-filter-bar__row wc-filter-bar__row--filters wc-filter-bar__row--filters--3">
            {canSeeAll ? (
              <select
                className="wc-select wc-select--compact wc-filter-bar__select"
                onChange={(event) => updateSearch({ scope: event.target.value as Scope, page: 1 })}
                value={scope}
              >
                <option value="all">全部员工</option>
                <option value="mine">我的</option>
              </select>
            ) : null}
            <select
              className="wc-select wc-select--compact wc-filter-bar__select"
              onChange={(event) => updateSearch({ recordingFilter: event.target.value as RecordingFilter, page: 1 })}
              value={recordingFilter}
            >
              <option value="linked">已关联录音</option>
              <option value="all">全部客户</option>
              <option value="unlinked">未关联录音</option>
            </select>
            <select
              className="wc-select wc-select--compact wc-filter-bar__select"
              onChange={(event) => updateSearch({ datePreset: event.target.value as DatePreset, page: 1 })}
              value={datePreset}
            >
              {DATE_PRESETS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </div>
        </form>
      </section>

      <section className="wc-card wc-card--mint wc-customers-page__list-card">
        <div className="wc-card__head">
          <h2 className="wc-card__title">{listTitle}</h2>
          <span className="wc-chip wc-chip--success">{total}位</span>
        </div>

        {isLoading ? (
          <div className="wc-empty">加载中…</div>
        ) : isError ? (
          <div className="wc-empty">客户列表加载失败：{resolveCustomersErrorMessage(error)}</div>
        ) : items.length === 0 ? (
          <div className="wc-empty">{emptyText}</div>
        ) : (
          <div className="wc-list wc-customers-page__rows">
            {groups.map((group) => (
              <div key={group.label} className="wc-date-sec">
                <div className="wc-date-sec__hd">
                  <span>{group.label === '暂无来访' ? '暂无来访' : dayjs(group.label).format('MM月DD日')}</span>
                  <small>{resolveGroupCustomerSummary(dateSummaryByLabel.get(group.label), group.items)}</small>
                </div>
                {group.items.map((customer) => {
                  const linkParams = new URLSearchParams()
                  linkParams.set('back_to', backTo)
                  if (currentKeyword) linkParams.set('from_keyword', currentKeyword)
                  linkParams.set('from_page', String(page))
                  const metaTags = buildCustomerMetaTags(customer)
                  return (
                    <Link
                      key={customer.id}
                      className={`wc-row wc-row--stacked wc-row--card wc-customer-row ${
                        customer.recording_count > 0 ? 'wc-customer-row--linked' : 'wc-customer-row--unlinked'
                      }`}
                      to={`/wecom/customers/${customer.id}?${linkParams.toString()}`}
                    >
                      <div className="wc-row__main">
                        <div className="wc-customer-row__identity">
                          <div className="wc-customer-row__top">
                            <strong>{customer.name}</strong>
                            <span className="wc-customer-row__meta-item wc-customer-row__meta-item--code">
                              <IdcardOutlined />
                              <span>{customer.external_customer_code || '无客户号'}</span>
                            </span>
                            {metaTags.map((tag) => (
                              <span key={`${customer.id}-${tag}`} className="wc-customer-row__meta-item wc-customer-row__meta-item--tag">
                                <span>{tag}</span>
                              </span>
                            ))}
                          </div>
                          <div className="wc-customer-row__meta wc-customer-row__meta--secondary">
                            <span className="wc-customer-row__meta-item wc-customer-row__meta-item--visit">
                              <CalendarOutlined />
                              <span>最近到诊 {formatVisitTime(customer.last_visit_at)}</span>
                            </span>
                            <span className="wc-customer-row__meta-item wc-customer-row__meta-item--visit">
                              <ProfileOutlined />
                              <span>到诊 {customer.visit_count} 次</span>
                            </span>
                          </div>
                        </div>
                      </div>
                      <div className="wc-row__end">
                        <RightOutlined className="wc-row__arrow" />
                      </div>
                    </Link>
                  )
                })}
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
      </section>
    </div>
  )
}

export default WecomCustomersPage
