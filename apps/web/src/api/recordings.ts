import { api, type PaginatedResponse } from './client'

export type Recording = {
  id: string
  visit_id: string | null
  linked_visit_ids: string[]
  linked_visits: Array<{
    id: string
    external_visit_order_no: string | null
    external_visit_order_seg: string | null
    customer_name: string | null
    is_primary: boolean
  }>
  visit_status: string | null
  staff_id: string | null
  staff_name: string | null
  staff_badge_id: string | null
  staff_role: string | null
  customer_name: string | null
  device_id: string | null
  device_code: string | null
  file_name: string
  file_path?: string
  file_size: number
  duration_seconds: number | null
  status: string
  split_parent_recording_id?: string | null
  split_part_index?: number | null
  split_at_ms?: number | null
  has_transcript: boolean
  created_at: string
}

export type MatchEvidence = {
  type: string
  label: string
  detail: string
  strength: string
}

export type VisitOrderMatchLineItem = {
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

export type VisitOrderMatchCandidate = {
  visit_order_id: string
  local_visit_id: string | null
  associated_local_visit_ids: string[]
  companion_visit_order_refs: string[]
  companion_customer_codes: string[]
  dzdh: string
  dzseg: string | null
  customer_name: string | null
  customer_code: string | null
  customer_type_code: string | null
  customer_type_label: string | null
  visit_date: string | null
  advisor_code: string | null
  fzuer: string | null
  fzuer_long: string | null
  triage_time: string | null
  confidence: number
  decision: string
  method: string
  reasons: string[]
  excluded_reasons: string[]
  identity_conflicts: string[]
  manual_review_required: boolean
  manual_review_reason: string | null
  evidence: MatchEvidence[]
  merged_segments: string[]
  merged_line_items: VisitOrderMatchLineItem[]
  linked_recording_count: number
  linked_recording_names: string[]
}

export type RecordingVisitOrderMatch = {
  recording_id: string
  file_name: string
  record_date: string | null
  advisor_code: string | null
  customer_code: string | null
  customer_name: string | null
  linked_visit_id: string | null
  linked_visit_ids: string[]
  linked_visit_order_refs: string[]
  linked_visit_order_no: string | null
  linked_visit_order_seg: string | null
  auto_applied: boolean
  identity_conflicts: string[]
  manual_review_required: boolean
  manual_review_reason: string | null
  summary: string
  analyzed_at: string
  candidates: VisitOrderMatchCandidate[]
}

export type AnalysisTaskRef = {
  id: string
  file_name: string
  status: 'pending' | 'running' | 'done' | 'failed'
  progress: number
  error_message: string | null
  duration_ms: number | null
  segment_count: number | null
  overall_score: number | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export type RecordingUpdatePayload = {
  visit_id?: string | null
  linked_visit_ids?: string[] | null
  staff_id?: string | null
  device_id?: string | null
  status?: string | null
  duration_seconds?: number | null
}

export type RecordingAnalysisTask = AnalysisTaskRef & {
  result: Record<string, unknown> | null
}

export type RecordingCustomerSegment = {
  id: string
  segment_index: number
  label: string
  begin_ms: number
  end_ms: number
  summary: string
  utterance_count: number
  status: string
  mapped_visit_id: string | null
}

export type RecordingVisitAnalysis = {
  id: string
  recording_id: string
  visit_id: string
  visit_order_no: string | null
  visit_order_seg: string | null
  customer_name: string | null
  customer_code: string | null
  customer_segment_id: string | null
  mapping_status: string
  analysis_status: string
  analysis_task_id: string | null
  analysis_error: string | null
  confirmed_by: string | null
  confirmed_at: string | null
  sap_ready_at: string | null
  sap_push_log_id: string | null
}

export type RecordingMultiCustomerReview = {
  recording_id: string
  required: boolean
  linked_visit_count: number
  status: 'not_required' | 'pending_mapping' | 'analyzing' | 'ready' | 'failed'
  message: string
  segments: RecordingCustomerSegment[]
  visit_analyses: RecordingVisitAnalysis[]
}

export type RecordingMediaSource = {
  url: string
  file_name: string
  media_type?: string | null
}

export const RECORDING_STATUS_MAP: Record<string, { label: string; color: string }> = {
  uploaded: { label: '待转写', color: 'default' },
  transcribing: { label: '转写中', color: 'processing' },
  transcribed: { label: '已转写', color: 'cyan' },
  analyzing: { label: '分析中', color: 'blue' },
  analyzed: { label: '分析完成', color: 'success' },
  failed: { label: '处理失败', color: 'error' },
}

export const STAFF_ROLE_MAP: Record<string, string> = {
  consultant: '咨询师',
  doctor: '医生',
  super_admin: '超级管理员',
  system_admin: '系统管理员',
  hospital_admin: '机构管理员',
  admin: '系统管理员',
  manager: '机构管理员',
  staff: '普通员工',
}

export const fetchRecordings = (params?: {
  visit_id?: string
  hospital_code?: string
  staff_id?: string
  status?: string
  keyword?: string
  customer_keyword?: string
  badge_id?: string
  role?: string
  has_visit?: boolean
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
  fast_page?: boolean
}) => {
  const sp = new URLSearchParams()
  if (params?.visit_id) sp.set('visit_id', params.visit_id)
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.staff_id) sp.set('staff_id', params.staff_id)
  if (params?.status) sp.set('status', params.status)
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.customer_keyword) sp.set('customer_keyword', params.customer_keyword)
  if (params?.badge_id) sp.set('badge_id', params.badge_id)
  if (params?.role) sp.set('role', params.role)
  if (params?.has_visit !== undefined) sp.set('has_visit', String(params.has_visit))
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  if (params?.fast_page !== undefined) sp.set('fast_page', String(params.fast_page))
  const qs = sp.toString()
  return api.get(`recordings${qs ? `?${qs}` : ''}`).json<PaginatedResponse<Recording>>()
}

export const fetchRecording = (id: string) => api.get(`recordings/${id}`).json<Recording>()

export const fetchRecordingMediaSource = (id: string) =>
  api.get(`recordings/${id}/media-url`).json<RecordingMediaSource>()

export const fetchRecordingMediaBlob = (id: string) => api.get(`recordings/${id}/media`).blob()

export const uploadRecording = (
  file: File,
  opts?: { visit_id?: string; staff_id?: string; device_id?: string },
) => {
  const sp = new URLSearchParams()
  if (opts?.visit_id) sp.set('visit_id', opts.visit_id)
  if (opts?.staff_id) sp.set('staff_id', opts.staff_id)
  if (opts?.device_id) sp.set('device_id', opts.device_id)
  const qs = sp.toString()
  const formData = new FormData()
  formData.append('file', file)
  return api
    .post(`recordings/upload${qs ? `?${qs}` : ''}`, { body: formData, timeout: false })
    .json<Recording>()
}

export const updateRecording = (id: string, data: RecordingUpdatePayload) =>
  api.put(`recordings/${id}`, { json: data }).json<Recording>()

export type RecordingVisitOrderLocalVisit = {
  visit_id: string
  visit_order_id: string
  dzdh: string | null
  dzseg: string | null
}

export const ensureRecordingVisitOrderLocalVisit = (id: string, visitOrderId: string) =>
  api
    .post(`recordings/${id}/visit-order-local-visit`, { json: { visit_order_id: visitOrderId } })
    .json<RecordingVisitOrderLocalVisit>()

export const deleteRecording = (id: string) => api.delete(`recordings/${id}`)

export const analyzeRecording = (id: string) => api.post(`recordings/${id}/analyze`).json<AnalysisTaskRef>()

export type RecordingSplitResult = {
  original_recording_id: string
  split_at_ms: number
  message: string
  parts: Array<{
    part_index: number
    archive_item_id: string | null
    recording: Recording
  }>
}

export const splitRecording = (
  id: string,
  data: { split_at_seconds?: number; split_at_ms?: number; confirm: boolean },
) => api.post(`recordings/${id}/split`, { json: data, timeout: false }).json<RecordingSplitResult>()

export const fetchRecordingAnalysis = (id: string) =>
  api.get(`recordings/${id}/analysis`).json<RecordingAnalysisTask | null>()

export const fetchRecordingMultiCustomerReview = (id: string) =>
  api.get(`recordings/${id}/multi-customer-review`).json<RecordingMultiCustomerReview>()

export const confirmRecordingMultiCustomerReview = (
  id: string,
  mappings: Array<{ visit_id: string; customer_segment_id: string }>,
) => api.post(`recordings/${id}/multi-customer-review/confirm`, { json: { mappings } }).json<RecordingMultiCustomerReview>()

export const resetRecordingMultiCustomerReview = (id: string) =>
  api.post(`recordings/${id}/multi-customer-review/reset`).json<RecordingMultiCustomerReview>()

export const fetchRecordingVisitOrderMatch = (id: string, applyAuto = true, useLlm = true) => {
  const sp = new URLSearchParams()
  sp.set('apply_auto', String(applyAuto))
  sp.set('use_llm', String(useLlm))
  return api
    .get(`recordings/${id}/visit-order-match?${sp.toString()}`, { timeout: 30000 })
    .json<RecordingVisitOrderMatch>()
}

export type SapConsultationPayload = {
  text: string
  user: string
  zxxx: Record<string, string>
  TAB_SYZ: Array<Record<string, string>>
}

export type SapPushTarget = {
  visit_id: string | null
  visit_order_no: string
  visit_order_seg: string | null
  customer_name: string
  customer_code: string
  advisor_name: string
  indication_count: number
  recording_count: number
  is_primary: boolean
}

export type SapPushResult = {
  recording_id: string
  visit_order_no: string
  visit_order_seg: string | null
  customer_name: string
  customer_code: string
  advisor_name: string
  indication_count: number
  recording_count: number
  target_count: number
  targets: SapPushTarget[]
  payloads: SapConsultationPayload[]
}

export const pushRecordingToSap = (id: string) =>
  api.post(`recordings/${id}/push-sap`).json<SapPushResult>()

export type SapPushDispatchRequest = {
  trigger_mode?: 'manual' | 'auto_bind' | 'scheduled'
  async_dispatch?: boolean
  target_visit_id?: string | null
}

export type SapPushAttempt = {
  request_index: number
  success: boolean
  http_status_code: number | null
  gateway_code: number | string | null
  business_status: string | null
  business_message: string | null
  response_body: unknown
}

export type SapPushLog = {
  id: string
  recording_id: string | null
  recording_file_name: string | null
  recording_created_at: string | null
  visit_id: string | null
  visit_order_no: string | null
  visit_order_seg: string | null
  customer_name: string | null
  customer_code: string | null
  advisor_name: string | null
  trigger_mode: string
  status: string
  send_enabled: boolean
  initiated_by: string | null
  request_url: string | null
  trace_id: string | null
  request_payloads: Array<Record<string, unknown>>
  gateway_requests: Array<Record<string, unknown>>
  response_items: SapPushAttempt[]
  http_status_code: number | null
  business_status: string | null
  business_message: string | null
  error_message: string | null
  sent_at: string | null
  created_at: string
  updated_at: string
}

export type SapPushDispatchResult = {
  queued: boolean
  dispatch_mode: 'dramatiq' | 'background' | 'eager'
  send_enabled: boolean
  message: string
  log: SapPushLog
  logs: SapPushLog[]
}

export const dispatchRecordingToSap = (id: string, data?: SapPushDispatchRequest) =>
  api.post(`recordings/${id}/push-sap/dispatch`, { json: data ?? {} }).json<SapPushDispatchResult>()

export const fetchRecordingSapPushLogs = (id: string) =>
  api.get(`recordings/${id}/push-sap/logs`).json<SapPushLog[]>()

export type DailyVisitOrderItem = {
  id: string
  dzdh: string
  dzseg: string | null
  local_visit_id: string | null
  detail_local_visit_id: string | null
  associated_local_visit_ids: string[]
  companion_local_visit_ids: string[]
  companion_visit_order_refs: string[]
  companion_customer_codes: string[]
  ninam: string | null
  kunr: string | null
  customer_type_code: string | null
  customer_type_label: string | null
  sjrq: string | null
  fzsj: string | null
  fzuer: string | null
  fzuer_long: string | null
  advxc_long: string | null
  jcsta_txt: string | null
  remark_dz: string | null
  linked_recording_names: string[]
}

export type DailyVisitOrdersResponse = {
  items: DailyVisitOrderItem[]
  recording_date: string | null
  total: number
  scope_mode: 'self' | 'org'
  keyword: string
}

export const fetchDailyVisitOrdersForRecording = (
  recordingId: string,
  params?: {
    scope_mode?: 'self' | 'org'
    keyword?: string
  },
) => {
  const sp = new URLSearchParams()
  if (params?.scope_mode) sp.set('scope_mode', params.scope_mode)
  if (params?.keyword) sp.set('keyword', params.keyword)
  const qs = sp.toString()
  return api.get(`visit-orders/daily-for-recording/${recordingId}${qs ? `?${qs}` : ''}`).json<DailyVisitOrdersResponse>()
}
