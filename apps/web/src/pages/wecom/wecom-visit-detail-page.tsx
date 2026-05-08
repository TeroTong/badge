import { RightOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { HTTPError } from 'ky'
import { Link, useLocation, useParams } from 'react-router-dom'

import { fetchVisitDetail, VISIT_STATUS_MAP } from '@/api/visits'
import {
  buildVisitOrderLineItemMeta,
  formatVisitOrderLineItemRef,
} from '@/utils/visit-order-line-items'
import { formatBeijingTime } from '@/utils/time'
type AnalysisTag = { category?: string; value?: string }

type VisitAnalysis = {
  customer_profile?: {
    tags?: AnalysisTag[]
  }
}

function formatDateTime(date: string | null, time?: string | null) {
  if (!date) return '-'
  return `${dayjs(date).format('MM/DD')}${time ? ` ${time.slice(0, 5)}` : ''}`
}

function formatClock(value: string | null | undefined) {
  if (!value) return '未记录'
  const normalized = value.replace(/[^0-9]/g, '')
  if (normalized.length < 4) return value
  const padded = normalized.padStart(6, '0')
  return `${padded.slice(0, 2)}:${padded.slice(2, 4)}${padded.length >= 6 ? `:${padded.slice(4, 6)}` : ''}`
}

function buildProfileTags(analysis: VisitAnalysis | null) {
  return (analysis?.customer_profile?.tags ?? [])
    .map((tag) => `${tag.category || '标签'}：${tag.value || '未标注'}`)
    .filter(Boolean)
    .slice(0, 8)
}

function resolveVisitDetailErrorMessage(error: unknown) {
  if (error instanceof HTTPError) {
    if (error.response.status === 403 || error.response.status === 404) {
      return '当前账号暂无权限查看该接诊记录'
    }
    if (error.response.status >= 500) {
      return '服务器处理接诊详情时出错，请稍后重试'
    }
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message
  }
  return '请稍后重试'
}

export function WecomVisitDetailPage() {
  const { visitId } = useParams<{ visitId: string }>()
  const location = useLocation()
  const backTo = `${location.pathname}${location.search}`

  const { data: visit, isLoading, isError, error } = useQuery({
    queryKey: ['wecom', 'visit-detail', visitId],
    queryFn: () => fetchVisitDetail(visitId!),
    enabled: !!visitId,
  })

  if (isError) {
    return <div className="wc-empty">接诊详情加载失败：{resolveVisitDetailErrorMessage(error)}</div>
  }

  if (isLoading || !visit) {
    return <div className="wc-empty">加载中…</div>
  }

  const status = VISIT_STATUS_MAP[visit.status]
  const orderContext = visit.visit_order_context
  const analysis = (visit.latest_analysis_result ?? null) as VisitAnalysis | null
  const profileTags = buildProfileTags(analysis)
  const consultant = visit.consultant_name || '待分配'
  const doctor = visit.doctor_name || '待分配'
  const lineItems = orderContext?.line_items ?? []
  const visitNotes = Array.from(
    new Set(
      [
        visit.notes,
        orderContext?.demand_remark,
      ]
        .map((item) => item?.trim())
        .filter((item): item is string => Boolean(item)),
    ),
  )
  const customerDetailLink = (() => {
    if (!visit.customer_id) return null
    const params = new URLSearchParams()
    params.set('from_visit_id', visit.id)
    params.set('back_to', backTo)
    return `/wecom/customers/${visit.customer_id}?${params.toString()}`
  })()

  return (
    <div className="wc-page wc-visit-detail-page">
      <div className="wc-detail-header wc-visit-detail-page__header">
        <div className="wc-detail-header__top">
          <h1 className="wc-detail-header__title">{visit.customer_name}</h1>
          <span className="wc-chip wc-chip--blue">{status?.label ?? visit.status}</span>
        </div>
        <p className="wc-detail-header__meta">
          {visit.customer_code || '无客户编码'} · {formatDateTime(visit.visit_date, visit.visit_time)}
        </p>
        {visit.deal_status && (
          <div className="wc-tag-wrap wc-visit-detail-page__header-tags">
            {visit.customer_type_label ? (
              <span className={`wc-tag ${visit.customer_type_code === 'V' ? 'wc-tag--green' : 'wc-tag--blue'}`}>
                {visit.customer_type_label}
              </span>
            ) : null}
            <span className="wc-tag wc-tag--blue">{visit.deal_status}</span>
          </div>
        )}
        {!visit.deal_status && visit.customer_type_label ? (
          <div className="wc-tag-wrap wc-visit-detail-page__header-tags">
            <span className={`wc-tag ${visit.customer_type_code === 'V' ? 'wc-tag--green' : 'wc-tag--blue'}`}>
              {visit.customer_type_label}
            </span>
          </div>
        ) : null}
      </div>

      <div className="wc-card wc-card--sky">
        <div className="wc-card__head">
          <h2 className="wc-card__title">本次接诊</h2>
          {customerDetailLink ? (
            <Link className="wc-more-link" to={customerDetailLink}>
              查看客户档案 <RightOutlined />
            </Link>
          ) : null}
        </div>
        <div className="wc-item__grid">
          <div><label>到院目的</label><span>{visit.arrival_purpose || orderContext?.visit_purpose || '-'}</span></div>
          <div><label>到诊备注</label><span>{orderContext?.demand_remark || visit.notes || '-'}</span></div>
          <div><label>咨询师</label><span>{consultant}</span></div>
          <div><label>主诊医生</label><span>{doctor}</span></div>
          <div><label>分诊时间</label><span>{formatClock(orderContext?.triage_time)}</span></div>
          <div><label>创建时间</label><span>{visit.created_at ? formatBeijingTime(visit.created_at, 'MM/DD HH:mm') : '-'}</span></div>
        </div>
        {lineItems.length > 0 && (
          <div className="wc-visit-detail-page__line-items">
            <label>分诊明细{lineItems.length > 1 ? `（合并 ${lineItems.length} 条）` : ''}</label>
            <div className="wc-visit-detail-page__line-item-grid">
              {lineItems.map((item, index) => {
                const metaLines = buildVisitOrderLineItemMeta(item)
                return (
                  <div key={`${item.fzdh ?? item.dzseg ?? 'line-item'}-${index}`} className="wc-visit-detail-page__line-item">
                    <strong>{formatVisitOrderLineItemRef(item)}</strong>
                    {metaLines.map((line) => <span key={line}>{line}</span>)}
                    {item.note_summary ? <span className="wc-visit-detail-page__line-item-note">备注：{item.note_summary}</span> : null}
                  </div>
                )
              })}
            </div>
          </div>
        )}
        {visitNotes.length > 0 && (
          <div className="wc-summary-block wc-visit-detail-page__note-block">
            <label>接待记录</label>
            <p>{visitNotes.join('；')}</p>
          </div>
        )}
      </div>

      {profileTags.length > 0 && (
        <div className="wc-card wc-card--violet">
          <div className="wc-card__head">
            <h2 className="wc-card__title">客户画像与标签</h2>
          </div>
          <div className="wc-tag-wrap">
            {profileTags.map((tag) => (
              <span key={tag} className="wc-tag wc-tag--purple">{tag}</span>
            ))}
          </div>
        </div>
      )}

    </div>
  )
}

export default WecomVisitDetailPage
