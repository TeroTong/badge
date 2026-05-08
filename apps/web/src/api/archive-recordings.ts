import { api, type PaginatedResponse } from './client'

export type ArchiveRecording = {
  id: string
  stage_key?: string | null
  sn?: string | null
  device_code?: string | null
  file_id: string
  display_file_name: string
  archive_file_name?: string | null
  staged_file_name?: string | null
  remote_file_name?: string | null
  audio_path?: string | null
  archive_audio_path?: string | null
  stage_audio_path?: string | null
  duration_ms?: number | null
  duration_seconds?: number | null
  file_size?: number | null
  create_time?: string | null
  downloaded_at?: string | null
  updated_at?: string | null
  staff_id?: string | null
  staff_name?: string | null
  staff_role?: string | null
  pipeline_status?: string | null
  quality_stage?: string | null
  quality_reason?: string | null
  error_message?: string | null
  recording_id?: string | null
  visit_id?: string | null
  linked_visit_ids: string[]
  linked_visit_order_refs: string[]
  linked_customer_names: string[]
  has_visit_link: boolean
  needs_visit_link: boolean
  utterance_count?: number | null
  full_text_length?: number | null
  has_transcript: boolean
  has_analysis: boolean
  analysis_summary?: Record<string, unknown> | null
}

export type ArchiveRecordingDateSummary = {
  date: string | null
  total: number
  linked_count: number
  needs_link_count: number
}

export type ArchiveRecordingPage = PaginatedResponse<ArchiveRecording> & {
  date_summaries?: ArchiveRecordingDateSummary[]
}

export type ArchiveRecordingDetail = ArchiveRecording & {
  manifest?: Record<string, unknown> | null
  archive_metadata?: Record<string, unknown> | null
  transcript?: Record<string, unknown> | null
  analysis_result?: Record<string, unknown> | null
  analysis_summary?: Record<string, unknown> | null
}

export type ArchiveRecordingEnsureResult = {
  item_id: string
  recording_id: string
  file_name: string
  display_file_name: string
  created_new_recording: boolean
  visit_id?: string | null
  linked_visit_ids: string[]
  linked_visit_order_refs: string[]
  linked_customer_names: string[]
}

export type RecordingMediaSource = {
  url: string
  file_name: string
  media_type?: string | null
}

export const fetchArchiveRecordings = (params?: {
  visit_id?: string
  staff_id?: string
  hospital_code?: string
  status?: string
  keyword?: string
  link_state?: 'linked' | 'unlinked' | 'needs_link'
  sort_mode?: 'date_grouped_link_state'
  exclude_filtered?: boolean
  exclude_quality_filtered?: boolean
  problem_only?: boolean
  include_date_summaries?: boolean
  include_analysis_summary?: boolean
  fast_page?: boolean
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.visit_id) sp.set('visit_id', params.visit_id)
  if (params?.staff_id) sp.set('staff_id', params.staff_id)
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.status) sp.set('status', params.status)
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.link_state) sp.set('link_state', params.link_state)
  if (params?.sort_mode) sp.set('sort_mode', params.sort_mode)
  if (params?.exclude_filtered !== undefined) sp.set('exclude_filtered', String(params.exclude_filtered))
  if (params?.exclude_quality_filtered !== undefined) sp.set('exclude_quality_filtered', String(params.exclude_quality_filtered))
  if (params?.problem_only !== undefined) sp.set('problem_only', String(params.problem_only))
  if (params?.include_date_summaries !== undefined) sp.set('include_date_summaries', String(params.include_date_summaries))
  if (params?.include_analysis_summary !== undefined) sp.set('include_analysis_summary', String(params.include_analysis_summary))
  if (params?.fast_page !== undefined) sp.set('fast_page', String(params.fast_page))
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`recordings/archive${qs ? `?${qs}` : ''}`).json<ArchiveRecordingPage>()
}

export const fetchArchiveRecordingDetail = (itemId: string) =>
  api.get(`recordings/archive/${itemId}`).json<ArchiveRecordingDetail>()

export const fetchArchiveRecordingMediaSource = (itemId: string) =>
  api.get(`recordings/archive/${itemId}/media-url`).json<RecordingMediaSource>()

export const fetchArchiveRecordingMediaBlob = (itemId: string) =>
  api.get(`recordings/archive/${itemId}/media`).blob()

export const ensureArchiveRecording = (itemId: string) =>
  api.post(`recordings/archive/${itemId}/ensure-recording`).json<ArchiveRecordingEnsureResult>()
