import { api, type PaginatedResponse } from './client'

export type Visit = {
  id: string
  customer_id: string
  customer_name: string
  customer_code: string | null
  customer_source: string | null
  consultant_id: string | null
  consultant_name: string | null
  doctor_id: string | null
  doctor_name: string | null
  status: string
  deal_status: string | null
  visit_date: string | null
  visit_time: string | null
  deposit_principal: number | null
  deposit_bonus: number | null
  recording_count: number
  customer_type_code: string | null
  customer_type_label: string | null
  arrival_purpose: string | null
  project_needs: string | null
  notes: string | null
  created_at: string
}

export type VisitDateSummary = {
  date: string | null
  total: number
}

export type VisitPage = PaginatedResponse<Visit> & {
  date_summaries?: VisitDateSummary[]
}

export type VisitDetailRecording = {
  id: string
  file_name: string
  is_primary: boolean
  device_id: string | null
  device_code: string | null
  staff_name: string | null
  staff_badge_id: string | null
  status: string
  duration_seconds: number | null
  created_at: string
  transcript_id: string | null
  transcript_status: string | null
  transcript_provider: string | null
  transcript_excerpt: string | null
  analysis_task_id: string | null
  analysis_status: string | null
  analysis_overall_score: number | null
  analysis_completed_at: string | null
  analysis_result: Record<string, unknown> | null
}

export type PendingArchiveRecording = {
  id: string
  display_file_name: string
  create_time: string | null
  duration_seconds: number | null
  staff_id: string | null
  staff_name: string | null
  device_code: string | null
  pipeline_status: string | null
  recording_id: string | null
  has_transcript: boolean
  has_analysis: boolean
  match_score: number
  match_reasons: string[]
}

export type VisitOrderLineItem = {
  fzdh: string | null
  dzseg: string | null
  triage_staff_code: string | null
  triage_staff_name: string | null
  triage_time: string | null
  consult_time: string | null
  triage_status_text: string | null
  deal_status_text: string | null
  consult_project: string | null
  note_summary: string | null
}

export type VisitOrderContext = {
  jgbm: string | null
  customer_type_code: string | null
  customer_type_label: string | null
  triage_time: string | null
  consult_time: string | null
  arrival_status: string | null
  deal_status_text: string | null
  visit_purpose: string | null
  consult_project: string | null
  demand_remark: string | null
  line_items: VisitOrderLineItem[]
}

export type VisitDetail = Visit & {
  customer_gender: string | null
  customer_age: number | null
  customer_wechat_external_uid: string | null
  customer_notes: string | null
  transcript_count: number
  analyzed_recording_count: number
  latest_recording_id: string | null
  latest_transcript_id: string | null
  latest_analysis_task_id: string | null
  latest_analysis_status: string | null
  latest_analysis_overall_score: number | null
  latest_analysis_completed_at: string | null
  latest_analysis_result: Record<string, unknown> | null
  latest_transcript_excerpt: string | null
  visit_order_context: VisitOrderContext | null
  recordings: VisitDetailRecording[]
  pending_archive_recordings: PendingArchiveRecording[]
}

export type CustomerVisitBatch = {
  customer_id: string
  visits: Visit[]
}

export const VISIT_STATUS_MAP: Record<string, { label: string; color: string }> = {
  created: { label: '已创建', color: 'default' },
  assigned: { label: '已分配', color: 'processing' },
  consulting: { label: '咨询中', color: 'blue' },
  consulted: { label: '咨询完成', color: 'cyan' },
  needs_diagnosis: { label: '待面诊', color: 'orange' },
  diagnosing: { label: '面诊中', color: 'purple' },
  diagnosed: { label: '面诊完成', color: 'geekblue' },
  closed_won: { label: '已成交', color: 'success' },
  closed_lost: { label: '未成交', color: 'error' },
}

export const fetchVisits = (params?: {
  customer_id?: string
  hospital_code?: string
  status?: string
  has_recharge?: boolean
  keyword?: string
  consultant_id?: string
  participant_staff_id?: string
  source?: string
  date_from?: string
  date_to?: string
  has_recordings?: boolean
  include_date_summaries?: boolean
  fast_page?: boolean
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.customer_id) sp.set('customer_id', params.customer_id)
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.status) sp.set('status', params.status)
  if (params?.has_recharge !== undefined) sp.set('has_recharge', String(params.has_recharge))
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.consultant_id) sp.set('consultant_id', params.consultant_id)
  if (params?.participant_staff_id) sp.set('participant_staff_id', params.participant_staff_id)
  if (params?.source) sp.set('source', params.source)
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.has_recordings !== undefined) sp.set('has_recordings', String(params.has_recordings))
  if (params?.include_date_summaries !== undefined) sp.set('include_date_summaries', String(params.include_date_summaries))
  if (params?.fast_page !== undefined) sp.set('fast_page', String(params.fast_page))
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`visits${qs ? `?${qs}` : ''}`).json<VisitPage>()
}

export const fetchVisit = (id: string) => api.get(`visits/${id}`).json<Visit>()

export const fetchVisitDetail = (id: string) => api.get(`visits/${id}/detail`).json<VisitDetail>()

export const fetchVisitsByCustomers = (customerIds: string[], perCustomerLimit = 20) => {
  const sp = new URLSearchParams()
  customerIds.forEach((customerId) => sp.append('customer_id', customerId))
  sp.set('per_customer_limit', String(perCustomerLimit))
  return api.get(`visits/by-customers?${sp.toString()}`).json<CustomerVisitBatch[]>()
}

export const createVisit = (data: {
  customer_id: string
  consultant_id?: string | null
  doctor_id?: string | null
  status?: string
  visit_date?: string | null
  deposit_principal?: number | null
  deposit_bonus?: number | null
  notes?: string | null
}) => api.post('visits', { json: data }).json<Visit>()

export const updateVisit = (id: string, data: Partial<Visit>) =>
  api.put(`visits/${id}`, { json: data }).json<Visit>()

export const deleteVisit = (id: string) => api.delete(`visits/${id}`)
