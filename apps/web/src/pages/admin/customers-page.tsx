import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { SearchOutlined } from '@ant-design/icons'
import {
  Avatar,
  Button,
  Card,
  DatePicker,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Pagination,
  Popconfirm,
  Select,
  Spin,
  Tag,
} from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { useNavigate } from 'react-router-dom'

import * as adminApi from '@/api/admin'
import type { Customer } from '@/api/customers'
import * as customersApi from '@/api/customers'
import { VISIT_STATUS_MAP } from '@/api/visits'
import * as visitsApi from '@/api/visits'
import { beijingNow, formatBeijingTime } from '@/utils/time'

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

const GENDERS = [
  { label: '男', value: 'male' },
  { label: '女', value: 'female' },
  { label: '未知', value: 'unknown' },
]

const GENDER_LABELS: Record<string, string> = { male: '男', female: '女', unknown: '未知' }

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

function formatVisitTimelineTime(visitDate: string | null, visitTime: string | null, createdAt: string) {
  if (visitDate && visitTime) {
    return `${dayjs(visitDate).format('YY/MM/DD')} ${visitTime.slice(0, 5)}`
  }
  if (createdAt) {
    return formatBeijingTime(createdAt, 'YY/MM/DD HH:mm')
  }
  if (visitDate) {
    return dayjs(visitDate).format('YY/MM/DD')
  }
  return '--'
}

export function CustomersPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [datePreset, setDatePreset] = useState<DatePreset>('all')
  const [customRange, setCustomRange] = useState<[Dayjs | null, Dayjs | null] | null>(null)
  const [consultantFilter, setConsultantFilter] = useState<string | undefined>()
  const [recordingFilter, setRecordingFilter] = useState<'all' | 'linked' | 'unlinked'>('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(12)

  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Customer | null>(null)
  const [form] = Form.useForm()

  const dateRange = resolveDateRange(datePreset, customRange)

  const { data, isLoading } = useQuery({
    queryKey: [
      'customers-workbench',
      keyword,
      dateRange.from ?? '',
      dateRange.to ?? '',
      consultantFilter,
      recordingFilter,
      page,
      pageSize,
    ],
    queryFn: () =>
      customersApi.fetchCustomers({
        keyword: keyword || undefined,
        consultant_id: consultantFilter,
        has_recordings:
          recordingFilter === 'all'
            ? undefined
            : recordingFilter === 'linked',
        date_from: dateRange.from,
        date_to: dateRange.to,
        include_date_summaries: false,
        page,
        page_size: pageSize,
      }),
    placeholderData: (previousData) => previousData,
    staleTime: 30_000,
  })

  const { data: staffData } = useQuery({
    queryKey: ['staff', 'all'],
    queryFn: () => adminApi.fetchStaff({ page_size: 100 }),
  })
  const staff = staffData?.items ?? []
  const consultants = staff.filter((member) => member.role === 'consultant' || member.role === 'manager')

  const customers = data?.items ?? []
  const total = data?.total ?? 0

  const customerIds = customers.map((customer) => customer.id)
  const { data: customerVisitsData, isLoading: customerVisitsLoading } = useQuery({
    queryKey: ['customer-visits-batch', customerIds, 20],
    queryFn: () => visitsApi.fetchVisitsByCustomers(customerIds, 20),
    enabled: customerIds.length > 0,
    staleTime: 60_000,
  })

  const customerVisitsMap = new Map((customerVisitsData ?? []).map((item) => [item.customer_id, item.visits]))
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['customers'] })
    qc.invalidateQueries({ queryKey: ['customers-workbench'] })
    qc.invalidateQueries({ queryKey: ['customer-detail'] })
  }

  const updateMut = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: Partial<Customer> }) =>
      customersApi.updateCustomer(id, payload),
    onSuccess: invalidate,
  })
  const deleteMut = useMutation({
    mutationFn: customersApi.deleteCustomer,
    onSuccess: () => {
      invalidate()
      message.success('客户档案已删除')
    },
  })

  const openModal = (customer: Customer) => {
    setEditing(customer)
    form.setFieldsValue(
      customer,
    )
    setModalOpen(true)
  }

  const handleSave = async () => {
    if (!editing) return
    const values = await form.validateFields()
    await updateMut.mutateAsync({ id: editing.id, payload: values })
    message.success('客户档案已更新')
    setModalOpen(false)
  }

  const handleReset = () => {
    setKeywordInput('')
    setKeyword('')
    setDatePreset('all')
    setCustomRange(null)
    setConsultantFilter(undefined)
    setRecordingFilter('all')
    setPage(1)
  }

  return (
    <div className="customer-workbench">
      <div className="visit-page__header">
        <div>
          <p className="visit-page__eyebrow">客户中心 / 客户档案</p>
          <h1>客户档案</h1>
          <p className="visit-page__summary">
            以客户为中心查看归属、来访动态与录音沉淀，快速进入客户档案与接待链路。
          </p>
        </div>

        <div className="visit-page__hero-stats">
          <div className="visit-stat-chip">
            <span>当前结果</span>
            <strong>{total}</strong>
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
                if (preset.value !== 'custom') setCustomRange(null)
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
            placeholder="客户编码 / 客户姓名"
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
            placeholder="归属销售"
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
              { label: '全部录音情况', value: 'all' },
              { label: '已关联录音', value: 'linked' },
              { label: '未关联录音', value: 'unlinked' },
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
          <strong>来访客户概览</strong>
          <span>当前页共有 {customers.length} 位客户档案，可继续按归属、录音和时间筛选来访动态。</span>
        </div>
      </div>

      {isLoading ? (
        <div className="visit-grid__loading">
          <Spin size="large" />
        </div>
      ) : customers.length === 0 ? (
        <Card bordered={false}>
          <Empty description="当前筛选条件下没有客户档案" />
        </Card>
      ) : (
        <div>
          {(() => {
            const groups: { label: string; items: { customer: typeof customers[0]; index: number }[] }[] = []
            customers.forEach((customer, idx) => {
              const dateStr = customer.last_visit_at
                ? formatBeijingTime(customer.last_visit_at, 'YYYY-MM-DD')
                : '未知日期'
              const last = groups[groups.length - 1]
              if (last && last.label === dateStr) {
                last.items.push({ customer, index: idx })
              } else {
                groups.push({ label: dateStr, items: [{ customer, index: idx }] })
              }
            })
            return groups.map((group) => (
              <div key={group.label} className="date-group">
                <div className="date-group__header">
                  <span className="date-group__line" />
                  <span className="date-group__label">{group.label === '未知日期' ? '未知日期' : dayjs(group.label).format('YYYY年MM月DD日')}</span>
                  <span className="date-group__count">{group.items.length} 位</span>
                  <span className="date-group__line" />
                </div>
                <div className="customer-card-grid">
                  {group.items.map(({ customer }) => {
            const visits = customerVisitsMap.get(customer.id) ?? []
            const visitsLoading = customerVisitsLoading
            const latestVisit = visits[0]
            const latestConsultant = latestVisit?.consultant_name ?? '待归属'

            return (
              <article
                key={customer.id}
                className="customer-card customer-card--interactive"
                onClick={() => navigate(`/admin/customers/${customer.id}`)}
              >
                <header className="customer-card__header">
                  <div className="customer-card__identity">
                    <Avatar size={48} className="customer-card__avatar">
                      {customer.name.slice(0, 1) || '客'}
                    </Avatar>
                    <div>
                      <div className="customer-card__title-row">
                        <strong>{customer.name}</strong>
                        <div className="customer-card__demographics">
                          {customer.gender && (
                            <Tag bordered={false} color="blue">
                              性别：{GENDER_LABELS[customer.gender] ?? customer.gender}
                            </Tag>
                          )}
                          {customer.age != null && (
                            <Tag bordered={false}>
                              年龄：{customer.age}岁
                            </Tag>
                          )}
                          {customer.customer_type_label && (
                            <Tag
                              bordered={false}
                              color={customer.customer_type_code === 'V' ? 'gold' : 'green'}
                            >
                              {customer.customer_type_label}
                            </Tag>
                          )}
                        </div>
                      </div>
                      <div className="customer-card__badges">
                        <Tag color="blue">归属: {latestConsultant}</Tag>
                        {customer.wechat_external_uid && <Tag>企微ID</Tag>}
                      </div>
                    </div>
                  </div>

                  <div className="customer-card__meta">
                    {customer.external_customer_code ? (
                      <span className="customer-card__code">{customer.external_customer_code}</span>
                    ) : (
                      <span className="customer-card__id">ID {customer.id.slice(0, 8)}</span>
                    )}
                  </div>
                </header>

                <div className="customer-card__body">
                  {/* 基本信息 */}
                  <section className="customer-card__column">
                    <div className="customer-card__section-title">
                      <strong>基本信息</strong>
                    </div>
                    <dl className="customer-card__info-list">
                      <div className="customer-card__info-item">
                        <dt>到访</dt>
                        <dd>{customer.visit_count} 次</dd>
                      </div>
                      <div className="customer-card__info-item">
                        <dt>成交</dt>
                        <dd>
                          {customer.closed_won_count > 0 ? (
                            <Tag color="gold" bordered={false}>{customer.closed_won_count} 次</Tag>
                          ) : (
                            '暂无'
                          )}
                        </dd>
                      </div>
                      <div className="customer-card__info-item">
                        <dt>最近来访</dt>
                        <dd>{customer.last_visit_at ? formatBeijingTime(customer.last_visit_at, 'YY/MM/DD') : '暂无'}</dd>
                      </div>
                      <div className="customer-card__info-item">
                        <dt>建档时间</dt>
                        <dd>{formatBeijingTime(customer.created_at, 'YY/MM/DD')}</dd>
                      </div>
                    </dl>

                    {customer.notes && (
                      <div className="customer-card__note-block">
                        <span>备注</span>
                        <p>{customer.notes}</p>
                      </div>
                    )}
                  </section>

                  {/* 来访时间轴 */}
                  <section className="customer-card__column">
                    <div className="customer-card__section-title">
                      <strong>来访动态</strong>
                      <span>({customer.visit_count})</span>
                    </div>

                    {visitsLoading ? (
                      <div className="customer-card__timeline-loading">
                        <Spin size="small" />
                      </div>
                    ) : visits.length === 0 ? (
                      <div className="customer-card__empty-text">暂无来访记录</div>
                    ) : (
                      <div className="customer-timeline-scroll">
                        <div className="customer-timeline">
                          {visits.map((visit) => (
                            <div key={visit.id} className="customer-timeline__item">
                              <div className="customer-timeline__dot" />
                              <div className="customer-timeline__content">
                                <strong>
                                  {formatVisitTimelineTime(visit.visit_date, visit.visit_time, visit.created_at)}
                                </strong>
                                <span>{VISIT_STATUS_MAP[visit.status]?.label ?? visit.status}</span>
                              </div>
                              <Tag color="blue">{visit.consultant_name || '待分配'}</Tag>
                            </div>
                          ))}
                        </div>
                        {visits.length > 8 && (
                          <div className="customer-timeline__fade" />
                        )}
                      </div>
                    )}
                  </section>
                </div>

                <footer className="customer-card__footer">
                  <div className="customer-card__footer-meta">
                    <Tag color={customer.visit_count > 0 ? 'blue' : 'default'}>
                      {customer.visit_count > 0 ? '已有来访记录' : '待持续跟进'}
                    </Tag>
                  </div>

                  <div className="customer-card__footer-actions">
                    <Button size="small" onClick={(event) => {
                      event.stopPropagation()
                      openModal(customer)
                    }}>
                      编辑
                    </Button>
                    <Popconfirm
                      title="确定删除这份客户档案？"
                      description="删除后其关联接诊记录也会一并清理。"
                      onConfirm={() => deleteMut.mutate(customer.id)}
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
        title="编辑客户档案"
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
        confirmLoading={updateMut.isPending}
        destroyOnClose
        width={620}
      >
        <Form form={form} layout="vertical" preserve={false}>
          <div className="visit-form-grid">
            <Form.Item name="name" label="姓名" rules={[{ required: true, message: '请输入客户姓名' }]}>
              <Input />
            </Form.Item>
            <Form.Item name="gender" label="性别">
              <Select options={GENDERS} allowClear placeholder="选择性别" />
            </Form.Item>
            <Form.Item name="age" label="年龄">
              <InputNumber min={0} max={150} style={{ width: '100%' }} />
            </Form.Item>
          </div>

          <Form.Item name="wechat_external_uid" label="企微外部联系人ID">
            <Input />
          </Form.Item>

          <Form.Item name="notes" label="备注">
            <Input.TextArea rows={4} placeholder="记录客户偏好、标签建议和跟进重点" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default CustomersPage
