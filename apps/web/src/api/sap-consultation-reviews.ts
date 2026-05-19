import { api, type PaginatedResponse } from '@/api/client'

export type SapReviewBlock = {
  recording_id: string
  file_name: string | null
  recording_created_at: string | null
  sap_summary_enabled: boolean
  staff_id: string | null
  staff_name: string
  locked_header: string
  generated_body: string
  edited_body: string | null
  effective_body: string
  can_edit: boolean
  sort_index: number
}

export type SapReviewRecordingFile = {
  recording_id: string
  file_name: string | null
  created_at: string | null
}

export type SapReviewListItem = {
  visit_id: string
  review_id: string | null
  visit_order_no: string | null
  visit_order_seg: string | null
  customer_name: string | null
  customer_code: string | null
  hospital_code: string | null
  recording_count: number
  recording_file_names: string[]
  recording_files: SapReviewRecordingFile[]
  editable_block_count: number
  status: string
  status_label: string
  latest_recording_at: string | null
  last_push_at: string | null
  last_success_push_at: string | null
  next_auto_push_at: string | null
  last_push_consultation_no: string | null
  last_push_error: string | null
  updated_at: string | null
}

export type SapReviewIndication = {
  CCKS?: string | null
  CCSYZ?: string | null
  CCBW?: string | null
  department_code?: string | null
  department_name?: string | null
  indication_code?: string | null
  indication_name?: string | null
  body_part_code?: string | null
  body_part_name?: string | null
  [key: string]: unknown
}

export type SapReviewDetail = SapReviewListItem & {
  generated_text: string
  effective_text: string
  blocks: SapReviewBlock[]
  indication_payload: SapReviewIndication[]
  payload_snapshot: Array<Record<string, unknown>>
  latest_push_log: Record<string, unknown> | null
}

export type SapReviewPushResult = {
  queued: boolean
  dispatch_mode: string
  send_enabled: boolean
  message: string
  log: Record<string, unknown>
}

export type FetchSapReviewsParams = {
  page?: number
  page_size?: number
  keyword?: string
  status?: string
}

export function fetchSapConsultationReviews(params: FetchSapReviewsParams = {}) {
  const sp = new URLSearchParams()
  if (params.page) sp.set('page', String(params.page))
  if (params.page_size) sp.set('page_size', String(params.page_size))
  if (params.keyword?.trim()) sp.set('keyword', params.keyword.trim())
  if (params.status?.trim() && params.status.trim() !== 'all') sp.set('status', params.status.trim())
  const qs = sp.toString()
  return api.get(`sap-consultation-reviews${qs ? `?${qs}` : ''}`).json<PaginatedResponse<SapReviewListItem>>()
}

export function fetchSapConsultationReview(visitId: string) {
  return api.get(`sap-consultation-reviews/visits/${visitId}`).json<SapReviewDetail>()
}

export function updateSapConsultationReviewBlock(visitId: string, recordingId: string, editableText: string) {
  return api
    .patch(`sap-consultation-reviews/visits/${visitId}/blocks/${recordingId}`, {
      json: { editable_text: editableText },
    })
    .json<SapReviewDetail>()
}

export function pushSapConsultationReview(visitId: string) {
  return api.post(`sap-consultation-reviews/visits/${visitId}/push`).json<SapReviewPushResult>()
}
