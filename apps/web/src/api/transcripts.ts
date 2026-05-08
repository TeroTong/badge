import { api, type PaginatedResponse } from './client'

export type TranscriptUtterance = {
  speaker: string
  text: string
  begin_ms: number
  end_ms: number
}

export type Transcript = {
  id: string
  recording_id: string
  recording_file_name: string | null
  asr_provider: string
  asr_task_id: string | null
  status: string
  full_text: string | null
  utterances: TranscriptUtterance[] | null
  duration_ms: number | null
  error_message: string | null
  created_at: string
  completed_at: string | null
}

export type TranscriptBatchImportItem = {
  source_path: string
  recording_id: string | null
  recording_file_name: string | null
  status: 'imported' | 'skipped' | 'conflict' | 'error'
  message: string
  created_recording: boolean
}

export type TranscriptBatchImportResult = {
  imported: number
  skipped: number
  conflicts: number
  errors: number
  items: TranscriptBatchImportItem[]
}

export const TRANSCRIPT_STATUS_MAP: Record<string, { label: string; color: string }> = {
  pending: { label: '等待中', color: 'default' },
  processing: { label: '转写中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
}

export const fetchTranscripts = (params?: {
  recording_id?: string
  status?: string
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.recording_id) sp.set('recording_id', params.recording_id)
  if (params?.status) sp.set('status', params.status)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`transcripts${qs ? `?${qs}` : ''}`).json<PaginatedResponse<Transcript>>()
}

export const fetchTranscript = (id: string) =>
  api.get(`transcripts/${id}`).json<Transcript>()

export const triggerTranscription = (recordingId: string) =>
  api.post(`transcripts/trigger/${recordingId}`).json<Transcript>()

export const uploadManualTranscript = (
  file: File,
  recordingId: string,
  provider = 'manual',
) => {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('recording_id', recordingId)
  formData.append('provider', provider)
  return api.post('transcripts/upload', { body: formData, timeout: false }).json<Transcript>()
}

export const batchImportTranscripts = (directory: string, provider = 'validated-batch') =>
  api.post('transcripts/batch-import', { json: { directory, provider } }).json<TranscriptBatchImportResult>()
