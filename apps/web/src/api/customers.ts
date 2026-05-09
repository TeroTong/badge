import { api, type PaginatedResponse } from './client'

export type Customer = {
  id: string
  name: string
  gender: string | null
  age: number | null
  wechat_external_uid: string | null
  external_customer_code: string | null
  source: string | null
  notes: string | null
  is_active: boolean
  visit_count: number
  recording_count: number
  closed_won_count: number
  total_deposit_principal: number | null
  customer_type_code: string | null
  customer_type_label: string | null
  customer_type_institution_code: string | null
  last_visit_at: string | null
  created_at: string
}

export type CustomerDateSummary = {
  date: string | null
  total: number
}

export type CustomerPage = PaginatedResponse<Customer> & {
  date_summaries?: CustomerDateSummary[]
}

export type CustomerDetailRecording = {
  id: string
  visit_id: string | null
  file_name: string
  device_id: string | null
  staff_name: string | null
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
  analysis_summary: string | null
  analysis_profile_tags: string[]
  analysis_primary_demands: string[]
  analysis_concerns: string[]
  analysis_recommendations: string[]
  analysis_evaluation_dimensions: {
    name: string
    point_score: number | null
    max_score: number
    summary: string | null
  }[]
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

export type CustomerDetailVisit = {
  id: string
  status: string
  external_visit_order_no: string | null
  visit_date: string | null
  visit_time: string | null
  consultant_name: string | null
  doctor_name: string | null
  deal_status: string | null
  deposit_principal: number | null
  deposit_bonus: number | null
  arrival_purpose: string | null
  project_needs: string | null
  notes: string | null
  created_at: string
  recordings: CustomerDetailRecording[]
  pending_archive_recordings: PendingArchiveRecording[]
  sap_consultation_texts: string[]
  visit_order_summary: {
    dzdh: string | null
    jgbm: string | null
    crtdt: string | null
    crttm: string | null
    dzsta_txt: string | null
    dzly_txt: string | null
    dymd_txt: string | null
    dztyp_txt: string | null
    jgks_txt: string | null
    fzuer_long: string | null
    vipkf: string | null
    kulvl_dq: string | null
    kutyp_dq_txt: string | null
    kut30_dq_txt: string | null
    kusta_dq_txt: string | null
    remark_dz: string | null
    line_items: Array<{
      fzdh: string | null
      advxc_long: string | null
      assxc: string | null
      fzsj: string | null
      fzsta_txt: string | null
      jcsta_txt: string | null
    }>
  } | null
}

export type CustomerDetail = Customer & {
  recording_count: number
  transcript_count: number
  analyzed_recording_count: number
  visits: CustomerDetailVisit[]
}

export type CustomerMergedTheme = {
  label: string
  detail: string | null
  count: number
  latest_seen_at: string | null
}

export type CustomerMergedDimension = {
  name: string
  average_score: number
  latest_score: number | null
  mention_count: number
  latest_comment: string | null
}

export type CustomerMergedTimelineItem = {
  task_id: string
  recording_id: string | null
  recording_name: string | null
  visit_id: string | null
  visit_status: string | null
  project_name: string | null
  deal_amount: number | null
  overall_score: number | null
  quality_label: string
  completed_at: string | null
}

export type CustomerMergedAnalysis = {
  customer_id: string
  customer_name: string
  total_visits: number
  total_recordings: number
  analyzed_recordings: number
  average_score: number | null
  latest_score: number | null
  score_delta: number | null
  score_trend: 'improving' | 'declining' | 'stable'
  merged_summary: string
  latest_task_id: string | null
  latest_recording_id: string | null
  last_analyzed_at: string | null
  recurring_focus_areas: CustomerMergedTheme[]
  recurring_concerns: CustomerMergedTheme[]
  profile_tags: CustomerMergedTheme[]
  dimension_averages: CustomerMergedDimension[]
  timeline: CustomerMergedTimelineItem[]
}

export const fetchCustomers = (params?: {
  keyword?: string
  hospital_code?: string
  is_active?: boolean
  consultant_id?: string
  has_visits?: boolean
  has_recordings?: boolean
  has_positive_recharge?: boolean
  date_from?: string
  date_to?: string
  include_date_summaries?: boolean
  fast_page?: boolean
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.is_active !== undefined) sp.set('is_active', String(params.is_active))
  if (params?.consultant_id) sp.set('consultant_id', params.consultant_id)
  if (params?.has_visits !== undefined) sp.set('has_visits', String(params.has_visits))
  if (params?.has_recordings !== undefined) sp.set('has_recordings', String(params.has_recordings))
  if (params?.has_positive_recharge !== undefined) sp.set('has_positive_recharge', String(params.has_positive_recharge))
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.include_date_summaries !== undefined) sp.set('include_date_summaries', String(params.include_date_summaries))
  if (params?.fast_page !== undefined) sp.set('fast_page', String(params.fast_page))
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`customers${qs ? `?${qs}` : ''}`).json<CustomerPage>()
}

export const fetchCustomer = (id: string) => api.get(`customers/${id}`).json<Customer>()

export const fetchCustomerDetail = (id: string) =>
  api.get(`customers/${id}/detail`).json<CustomerDetail>()

export const fetchCustomerMergedAnalysis = (id: string) =>
  api.get(`customers/${id}/merged-analysis`).json<CustomerMergedAnalysis>()

export const createCustomer = (
  data: Omit<
    Customer,
    | 'id'
    | 'is_active'
    | 'visit_count'
    | 'recording_count'
    | 'closed_won_count'
    | 'total_deposit_principal'
    | 'customer_type_code'
    | 'customer_type_label'
    | 'customer_type_institution_code'
    | 'last_visit_at'
    | 'created_at'
  >,
) => api.post('customers', { json: data }).json<Customer>()

export const updateCustomer = (id: string, data: Partial<Customer>) =>
  api.put(`customers/${id}`, { json: data }).json<Customer>()

export const deleteCustomer = (id: string) => api.delete(`customers/${id}`)

// ── 标签完成度 ──────────────────────────────────

export type TagExtractionItem = {
  category_id: string
  category_name: string
  weight_level: number | null
  available_tags: string[]
  extracted_values: string[]
  evidence: string | null
  status: 'extracted' | 'not_extracted'
  last_seen_at: string | null
}

export type TagCompletion = {
  customer_id: string
  total_categories: number
  extracted_categories: number
  completion_rate: number
  categories: TagExtractionItem[]
}

export const fetchCustomerTagCompletion = (id: string) =>
  api.get(`customers/${id}/tag-completion`).json<TagCompletion>()

// ── 到诊单历史 ──────────────────────────────────

export type VisitOrderItem = {
  dzseg: string | null
  jcsta_txt: string | null
  remark_dz: string | null
}

export type VisitOrderGroup = {
  dzdh: string
  visit_date: string | null
  consultant_name: string | null
  status_text: string | null
  customer_type: string | null
  customer_type_t30: string | null
  member_level: string | null
  remark: string | null
  items: VisitOrderItem[]
}

export type CustomerVisitOrders = {
  customer_id: string
  customer_code: string | null
  total_visits: number
  visit_groups: VisitOrderGroup[]
}

export const fetchCustomerVisitOrders = (id: string) =>
  api.get(`customers/${id}/visit-orders`).json<CustomerVisitOrders>()
