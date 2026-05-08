import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AudioOutlined,
  EditOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  Avatar,
  Button,
  Card,
  DatePicker,
  Empty,
  Form,
  Input,
  message,
  Modal,
  Pagination,
  Popconfirm,
  Select,
  Spin,
  Tag,
} from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { useNavigate, useSearchParams } from 'react-router-dom'

import * as adminApi from '@/api/admin'
import * as recordingsApi from '@/api/recordings'
import type { Visit } from '@/api/visits'
import { VISIT_STATUS_MAP } from '@/api/visits'
import * as visitsApi from '@/api/visits'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { beijingNow } from '@/utils/time'

const { RangePicker } = DatePicker

type DatePreset = 'all' | 'today' | 'yesterday' | '7d' | '30d' | '90d' | 'custom'

const DATE_PRESETS: Array<{ value: DatePreset; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'today', label: '今日' },
  { value: 'yesterday', label: '昨日' },
  { value: '7d', label: '近7日' },
  { value: '30d', label: '近30日' },
  { value: '90d', label: '近90日' },
]

const STATUS_OPTIONS = Object.entries(VISIT_STATUS_MAP).map(([value, { label }]) => ({
  label,
  value,
}))

function resolveDateRange(
  preset: DatePreset,
  customRange: [Dayjs | null, Dayjs | null] | null,
): { from?: string; to?: string } {
  const today = beijingNow()

  if (preset === 'custom' && customRange?.[0] && customRange?.[1]) {
    return {
      from: customRange[0].format('YYYY-MM-DD'),
      to: customRange[1].format('YYYY-MM-DD'),
    }
  }

  switch (preset) {
    case 'today':
      return { from: today.format('YYYY-MM-DD'), to: today.format('YYYY-MM-DD') }
    case 'yesterday': {
      const yesterday = today.subtract(1, 'day')
      return { from: yesterday.format('YYYY-MM-DD'), to: yesterday.format('YYYY-MM-DD') }
    }
    case '7d':
      return { from: today.subtract(6, 'day').format('YYYY-MM-DD'), to: today.format('YYYY-MM-DD') }
    case '30d':
      return { from: today.subtract(29, 'day').format('YYYY-MM-DD'), to: today.format('YYYY-MM-DD') }
    case '90d':
      return { from: today.subtract(89, 'day').format('YYYY-MM-DD'), to: today.format('YYYY-MM-DD') }
    default:
      return {}
  }
}

function getVisitSummary(
  visit: Visit,
  recordings: Awaited<ReturnType<typeof recordingsApi.fetchRecordings>>['items'],
) {
  const relatedRecordings = recordings
    .filter((recording) => recording.visit_id === visit.id)
    .sort((left, right) => dayjs(right.created_at).valueOf() - dayjs(left.created_at).valueOf())

  return {
    relatedRecordings,
  }
}

export function VisitsPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const qc = useQueryClient()
  const hasRestoredScrollRef = useRef(false)
  const initialState = useMemo(() => {
    const keywordParam = searchParams.get('keyword') ?? ''
    const consultantParam = searchParams.get('consultant_id') ?? undefined
    const recordingParam = searchParams.get('recording')
    const presetParam = searchParams.get('date_preset')
    const dateFromParam = searchParams.get('date_from')
    const dateToParam = searchParams.get('date_to')
    const pageParam = Number(searchParams.get('page') ?? '1')
    const pageSizeParam = Number(searchParams.get('page_size') ?? '12')

    const resolvedPreset: DatePreset =
      presetParam === 'today' || presetParam === 'yesterday' || presetParam === '7d' || presetParam === '30d' || presetParam === '90d' || presetParam === 'custom'
        ? presetParam
        : 'all'

    return {
      keyword: keywordParam,
      consultantFilter: consultantParam,
      recordingFilter: (recordingParam === 'linked' || recordingParam === 'unlinked' ? recordingParam : 'all') as 'all' | 'linked' | 'unlinked',
      datePreset: resolvedPreset,
      customRange:
        resolvedPreset === 'custom' && dateFromParam && dateToParam
          ? [dayjs(dateFromParam), dayjs(dateToParam)] as [Dayjs, Dayjs]
          : null,
      page: Number.isFinite(pageParam) && pageParam > 0 ? pageParam : 1,
      pageSize: Number.isFinite(pageSizeParam) && pageSizeParam > 0 ? pageSizeParam : 12,
    }
  }, [searchParams])

  const [keywordInput, setKeywordInput] = useState(initialState.keyword)
  const [keyword, setKeyword] = useState(initialState.keyword)
  const [consultantFilter, setConsultantFilter] = useState<string | undefined>(initialState.consultantFilter)
  const [recordingFilter, setRecordingFilter] = useState<'all' | 'linked' | 'unlinked'>(initialState.recordingFilter)
  const [datePreset, setDatePreset] = useState<DatePreset>(initialState.datePreset)
  const [customRange, setCustomRange] = useState<[Dayjs | null, Dayjs | null] | null>(initialState.customRange)
  const [page, setPage] = useState(initialState.page)
  const [pageSize, setPageSize] = useState(initialState.pageSize)

  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Visit | null>(null)
  const [form] = Form.useForm()
  const visitListSearch = searchParams.toString()
  const scrollRestoreKey = useMemo(
    () => `visits-scroll:${visitListSearch}`,
    [visitListSearch],
  )

  useEffect(() => {
    const nextParams = new URLSearchParams()
    if (keyword) nextParams.set('keyword', keyword)
    if (consultantFilter) nextParams.set('consultant_id', consultantFilter)
    if (recordingFilter !== 'all') nextParams.set('recording', recordingFilter)
    if (datePreset !== 'all') nextParams.set('date_preset', datePreset)
    if (datePreset === 'custom' && customRange?.[0] && customRange?.[1]) {
      nextParams.set('date_from', customRange[0].format('YYYY-MM-DD'))
      nextParams.set('date_to', customRange[1].format('YYYY-MM-DD'))
    }
    if (page !== 1) nextParams.set('page', String(page))
    if (pageSize !== 12) nextParams.set('page_size', String(pageSize))

    if (nextParams.toString() !== searchParams.toString()) {
      setSearchParams(nextParams, { replace: true })
    }
  }, [
    keyword,
    consultantFilter,
    recordingFilter,
    datePreset,
    customRange,
    page,
    pageSize,
    searchParams,
    setSearchParams,
  ])

  const dateRange = resolveDateRange(datePreset, customRange)

  const { data, isLoading } = useQuery({
    queryKey: [
        'visits-workbench',
        keyword,
        consultantFilter,
        recordingFilter,
      dateRange.from ?? '',
      dateRange.to ?? '',
      page,
      pageSize,
    ],
    queryFn: () =>
      visitsApi.fetchVisits({
        keyword: keyword || undefined,
        consultant_id: consultantFilter,
        has_recordings:
          recordingFilter === 'all' ? undefined : recordingFilter === 'linked',
        date_from: dateRange.from,
        date_to: dateRange.to,
        include_date_summaries: false,
        page,
        page_size: pageSize,
      }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  useEffect(() => {
    if (isLoading || hasRestoredScrollRef.current) {
      return
    }

    const raw = sessionStorage.getItem('visits-scroll-state')
    if (!raw) {
      hasRestoredScrollRef.current = true
      return
    }

    try {
      const parsed = JSON.parse(raw) as { key?: string; y?: number }
      if (parsed.key === scrollRestoreKey && typeof parsed.y === 'number') {
        window.requestAnimationFrame(() => {
          window.scrollTo({ top: parsed.y, behavior: 'auto' })
        })
      }
    } catch {
      // Ignore malformed session state and continue with normal rendering.
    }

    sessionStorage.removeItem('visits-scroll-state')
    hasRestoredScrollRef.current = true
  }, [isLoading, scrollRestoreKey])

  const { data: staffData } = useQuery({
    queryKey: ['staff', 'all'],
    queryFn: () => adminApi.fetchStaff({ page_size: 100 }),
  })
  const staff = staffData?.items ?? []
  const consultants = staff.filter((member) => member.role === 'consultant' || member.role === 'manager')

  const { data: recordingsData } = useQuery({
    queryKey: ['recordings', 'visit-workbench'],
    queryFn: () => recordingsApi.fetchRecordings({ page_size: 100 }),
  })
  const recordings = recordingsData?.items ?? []

  const summaryCountParams = useMemo(() => ({
    keyword: keyword || undefined,
    consultant_id: consultantFilter,
    date_from: dateRange.from,
    date_to: dateRange.to,
    include_date_summaries: false,
    page: 1,
    page_size: 1,
  }), [consultantFilter, dateRange.from, dateRange.to, keyword])

  const { data: linkedVisitsStats } = useQuery({
    queryKey: ['visits-workbench', 'stats', 'linked', summaryCountParams],
    queryFn: () => visitsApi.fetchVisits({ ...summaryCountParams, has_recordings: true }),
    staleTime: 30_000,
  })

  const visits = data?.items ?? []
  const total = data?.total ?? 0

  const pendingLinkCount = visits.filter((visit) => visit.recording_count === 0).length
  const linkedRecordingCount = linkedVisitsStats?.total ?? 0

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['visits'] })
    qc.invalidateQueries({ queryKey: ['visits-workbench'] })
    qc.invalidateQueries({ queryKey: ['visit-detail'] })
  }

  const updateMut = useMutation({
    mutationFn: ({ id, data: payload }: { id: string; data: Partial<Visit> }) => visitsApi.updateVisit(id, payload),
    onSuccess: invalidate,
  })
  const deleteMut = useMutation({
    mutationFn: visitsApi.deleteVisit,
    onSuccess: () => {
      invalidate()
      message.success('接诊记录已删除')
    },
  })

  const openModal = (visit?: Visit) => {
    setEditing(visit ?? null)
    if (visit) {
      form.setFieldsValue({
        ...visit,
        visit_date: visit.visit_date ? dayjs(visit.visit_date) : null,
      })
    } else {
      form.resetFields()
    }
    setModalOpen(true)
  }

  const handleSave = async () => {
    if (!editing) {
      setModalOpen(false)
      return
    }

    const values = await form.validateFields()
    const payload = {
      ...values,
      visit_date: values.visit_date ? values.visit_date.format('YYYY-MM-DD') : null,
    }

    await updateMut.mutateAsync({ id: editing.id, data: payload })
    message.success('接诊记录已更新')
    setModalOpen(false)
  }

  const handleReset = () => {
    setKeywordInput('')
    setKeyword('')
    setConsultantFilter(undefined)
    setRecordingFilter('all')
    setDatePreset('all')
    setCustomRange(null)
    setPage(1)
  }

  const openVisitDetail = (visitId: string) => {
    sessionStorage.setItem(
      'visits-scroll-state',
      JSON.stringify({
        key: scrollRestoreKey,
        y: window.scrollY,
      }),
    )
    navigate(`/admin/visits/${visitId}${visitListSearch ? `?${visitListSearch}` : ''}`)
  }

  return (
    <div className="visit-workbench">
      <div className="visit-page__header">
        <div>
          <p className="visit-page__eyebrow">客户中心 / 接诊记录</p>
          <h1>接诊记录</h1>
          <p className="visit-page__summary">
            以接诊单为中心查看客户、顾问、录音和分析结果，先把工作台形态对齐参考系统。
          </p>
        </div>

        <div className="visit-page__hero-stats">
          <div className="visit-stat-chip">
            <span>当前接诊</span>
            <strong>{total}</strong>
          </div>
          <div className="visit-stat-chip">
            <span>已关联录音</span>
            <strong>{linkedRecordingCount}</strong>
          </div>
        </div>
      </div>

      <Card className="visit-toolbar-card" bordered={false}>
        <div className="visit-toolbar__ranges">
          {DATE_PRESETS.map((preset) => (
            <button
              key={preset.value}
              className={`visit-range-pill${datePreset === preset.value ? ' visit-range-pill--active' : ''}`}
              onClick={() => {
                setDatePreset(preset.value)
                if (preset.value !== 'custom') {
                  setCustomRange(null)
                }
                setPage(1)
              }}
              type="button"
            >
              {preset.label}
            </button>
          ))}

          <RangePicker
            value={customRange as [Dayjs, Dayjs] | null}
            onChange={(value) => {
              setCustomRange(value as [Dayjs | null, Dayjs | null] | null)
              setDatePreset(value ? 'custom' : 'all')
              setPage(1)
            }}
            className="visit-toolbar__daterange"
            allowClear
          />
        </div>

        <div className="visit-toolbar__search">
          <Input
            size="large"
            placeholder="客户名 / 客户编码 / 接诊单 ID"
            value={keywordInput}
            onChange={(event) => setKeywordInput(event.target.value)}
            onPressEnter={() => {
              setKeyword(keywordInput.trim())
              setPage(1)
            }}
            prefix={<SearchOutlined />}
          />
          <Button
            type="primary"
            size="large"
            onClick={() => {
              setKeyword(keywordInput.trim())
              setPage(1)
            }}
          >
            查询
          </Button>
          <Button size="large" onClick={handleReset}>
            重置
          </Button>
        </div>

        <div className="visit-toolbar__filters">
          <Select
            allowClear
            showSearch
            placeholder="咨询师"
            optionFilterProp="label"
            options={consultants.map((member) => ({ label: member.name, value: member.id }))}
            value={consultantFilter}
            onChange={(value) => {
              setConsultantFilter(value)
              setPage(1)
            }}
          />
          <Select
            value={recordingFilter}
            options={[
              { label: '全部录音状态', value: 'all' },
              { label: '已关联录音', value: 'linked' },
              { label: '待关联录音', value: 'unlinked' },
            ]}
            onChange={(value) => {
              setRecordingFilter(value)
              setPage(1)
            }}
          />
        </div>

      </Card>

      <div className="visit-pending-banner">
        <div>
          <strong>待补充录音关联</strong>
          <span>当前页有 {pendingLinkCount} 条接诊记录还没有关联录音。</span>
        </div>
        <button
          type="button"
          onClick={() => {
            setRecordingFilter('unlinked')
            setPage(1)
          }}
        >
          去筛选
        </button>
      </div>

      {isLoading ? (
        <div className="visit-grid__loading">
          <Spin size="large" />
        </div>
      ) : visits.length === 0 ? (
        <Card bordered={false}>
          <Empty description="当前筛选条件下没有接诊记录" />
        </Card>
      ) : (
        <div>
          {(() => {
            const groups: { label: string; items: typeof visits }[] = []
            for (const visit of visits) {
              const dateStr = visit.visit_date
                ? dayjs(visit.visit_date).format('YYYY-MM-DD')
                : '未知日期'
              const last = groups[groups.length - 1]
              if (last && last.label === dateStr) {
                last.items.push(visit)
              } else {
                groups.push({ label: dateStr, items: [visit] })
              }
            }
            return groups.map((group) => (
              <div key={group.label} className="date-group">
                <div className="date-group__header">
                  <span className="date-group__line" />
                  <span className="date-group__label">{group.label === '未知日期' ? '未知日期' : dayjs(group.label).format('YYYY年MM月DD日')}</span>
                  <span className="date-group__count">{group.items.length} 条</span>
                  <span className="date-group__line" />
                </div>
                <div className="visit-card-grid">
                  {group.items.map((visit) => {
            const summary = getVisitSummary(visit, recordings)
            return (
              <article key={visit.id} className="visit-card visit-card--interactive" onClick={() => openVisitDetail(visit.id)}>
                <header className="visit-card__header">
	                  <div className="visit-card__identity">
	                    <Avatar size={42} className="visit-card__avatar">
	                      {visit.customer_name.slice(0, 1) || '客'}
	                    </Avatar>
	                    <div className="visit-card__identity-main">
	                      <div className="visit-card__title-row">
	                        <strong>{visit.customer_name}</strong>
	                        {visit.customer_type_label ? (
	                          <Tag
	                            className="visit-card__customer-type"
	                            color={visit.customer_type_code === 'V' ? 'gold' : 'green'}
	                            bordered={false}
	                          >
	                            {visit.customer_type_label}
	                          </Tag>
	                        ) : null}
	                      </div>
	                      <div className="visit-card__badges">
	                        <span className="visit-card__badge visit-card__badge--code" title={visit.customer_code || visit.customer_name}>
	                          {visit.customer_code || visit.customer_name}
	                        </span>
	                        <span className="visit-card__badge visit-card__badge--consultant" title={visit.consultant_name || '-'}>
	                          负责人：{visit.consultant_name || '-'}
	                        </span>
	                      </div>
	                    </div>
	                  </div>
	
	                  <div className="visit-card__meta">
	                    <Tag
	                      className="visit-card__status-tag"
	                      color={visit.deal_status === '已成交' ? 'success' : visit.deal_status === '未成交' ? 'error' : visit.deal_status ? 'processing' : 'default'}
	                    >
	                      {visit.deal_status || '成交未记录'}
	                    </Tag>
                  </div>
                </header>

                <div className="visit-card__body">
                  <div className="visit-card__left">
                    <div className="visit-card__recordings-panel">
                      <div className="visit-card__recordings-title">
                        <AudioOutlined style={{ marginRight: 4 }} />
                        关联录音 ({summary.relatedRecordings.length})
                      </div>
                      {summary.relatedRecordings.length === 0 ? (
                        <div className="visit-card__recordings-empty">暂无关联录音</div>
                      ) : (
                        <div className="visit-card__recordings-list">
                          {summary.relatedRecordings.slice(0, 3).map((rec) => (
                            <div key={rec.id} className="visit-card__recording-item">
                              <div className="visit-card__recording-info">
                                <span className="visit-card__recording-name" title={formatRecordingDisplayName(rec.file_name, rec.created_at)}>
                                  {formatRecordingDisplayName(rec.file_name, rec.created_at)}
                                </span>
                                <span className="visit-card__recording-meta">
                                  {rec.duration_seconds != null
                                    ? `${Math.floor(rec.duration_seconds / 60)}分${Math.round(rec.duration_seconds % 60)}秒`
                                    : '时长未知'}
                                  {rec.staff_name ? ` · ${rec.staff_name}` : ''}
                                </span>
                              </div>
                            </div>
                          ))}
                          {summary.relatedRecordings.length > 3 && (
                            <div className="visit-card__recordings-more">
                              还有 {summary.relatedRecordings.length - 3} 条录音...
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="visit-card__details">
                    <div className="visit-card__facts">
                      <div>
                        <span>到院目的</span>
                        <strong>{visit.arrival_purpose || '-'}</strong>
                      </div>
                      <div>
                        <span>主诊医生</span>
                        <strong>{visit.doctor_name || '-'}</strong>
                      </div>
                      <div>
                        <span>到诊备注</span>
                        <strong>{visit.project_needs || '-'}</strong>
                      </div>
                    </div>
                  </div>
                </div>

	                <footer className="visit-card__footer">
	                  <div className="visit-card__visit-time">
	                    <span>到诊时间</span>
	                    <strong>{visit.visit_date ? dayjs(visit.visit_date).format('MM/DD') : '-'}{visit.visit_time ? ` ${visit.visit_time.slice(0, 5)}` : ''}</strong>
	                  </div>
	                  <div className="visit-card__footer-actions">
                    <Button size="small" icon={<EditOutlined />} onClick={(event) => {
                      event.stopPropagation()
                      openModal(visit)
                    }}>
                      编辑
                    </Button>
                    <Popconfirm
                      title="确定删除这条接诊记录？"
                      description="删除后其关联关系需要重新整理。"
                      onConfirm={() => deleteMut.mutate(visit.id)}
                      onPopupClick={(event) => event.stopPropagation()}
                    >
                      <Button size="small" danger onClick={(event) => event.stopPropagation()}>
                        删除
                      </Button>
                    </Popconfirm>
                  </div>
                </footer>
              </article>
            )
          })}
                </div>
              </div>
            ))
          })()}
        </div>
      )}

      <div className="visit-pagination">
        <span>共 {total} 条</span>
        <Pagination
          current={page}
          pageSize={pageSize}
          total={total}
          showSizeChanger
          pageSizeOptions={[12, 24, 48]}
          onChange={(nextPage, nextPageSize) => {
            setPage(nextPage)
            setPageSize(nextPageSize)
          }}
        />
      </div>

      <Modal
        title="编辑接诊记录"
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
        confirmLoading={updateMut.isPending}
        destroyOnClose
        width={620}
      >
        <Form form={form} layout="vertical" preserve={false}>
          <div className="visit-form-grid">
            <Form.Item name="status" label="接诊状态">
              <Select options={STATUS_OPTIONS} />
            </Form.Item>
            <Form.Item name="visit_date" label="到诊日期">
              <DatePicker style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="consultant_id" label="咨询师">
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="分配咨询师"
                options={consultants.map((member) => ({ label: member.name, value: member.id }))}
              />
            </Form.Item>
            <Form.Item name="doctor_id" label="医生">
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="分配医生"
                options={staff.map((member) => ({ label: member.name, value: member.id }))}
              />
            </Form.Item>
          </div>

          <Form.Item name="notes" label="备注">
            <Input.TextArea rows={4} placeholder="记录客户诉求、顾虑或接待备注" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default VisitsPage
