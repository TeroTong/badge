import { api, type PaginatedResponse } from './client'

export type Segment = {
  id: string
  recording_id: string
  visit_id: string | null
  segment_index: number
  begin_ms: number
  end_ms: number
  speaker_label: string | null
  text: string | null
  status: string
  has_analysis: boolean
  created_at: string
}

export const SPEAKER_MAP: Record<string, { label: string; color: string }> = {
  consultant: { label: '咨询师', color: 'blue' },
  advisor: { label: '咨询师', color: 'blue' },
  sales: { label: '咨询师', color: 'blue' },
  beauty_consultant: { label: '美容顾问', color: 'blue' },
  badge_owner: { label: '工牌本人', color: 'blue' },
  staff_peer: { label: '员工同事', color: 'cyan' },
  doctor: { label: '医生', color: 'purple' },
  nurse: { label: '护士', color: 'cyan' },
  customer: { label: '客户', color: 'green' },
  patient: { label: '客户', color: 'green' },
  client: { label: '客户', color: 'green' },
  primary_customer: { label: '主客户', color: 'green' },
  visitor_companion: { label: '同行人', color: 'lime' },
  visitor: { label: '访客', color: 'lime' },
  service: { label: '客服', color: 'cyan' },
  assistant: { label: '助理', color: 'cyan' },
  staff: { label: '工作人员', color: 'cyan' },
  unknown: { label: '未知', color: 'default' },
  '工牌本人': { label: '工牌本人', color: 'blue' },
  '员工同事': { label: '员工同事', color: 'cyan' },
  '主客户': { label: '主客户', color: 'green' },
  '同行人': { label: '同行人', color: 'lime' },
  '访客': { label: '访客', color: 'lime' },
  '咨询师': { label: '咨询师', color: 'blue' },
  '医生': { label: '医生', color: 'purple' },
  '客户': { label: '客户', color: 'green' },
  '患者': { label: '客户', color: 'green' },
  '前台': { label: '前台', color: 'cyan' },
  '客服': { label: '客服', color: 'cyan' },
  '助理': { label: '助理', color: 'cyan' },
  '护士': { label: '护士', color: 'cyan' },
  '工作人员': { label: '工作人员', color: 'cyan' },
  '美容顾问': { label: '美容顾问', color: 'blue' },
  '美学顾问': { label: '美学顾问', color: 'blue' },
  '美学设计师': { label: '美学设计师', color: 'purple' },
  '旁观者': { label: '旁观者', color: 'default' },
}

export const fetchSegments = (params?: { recording_id?: string; visit_id?: string; status?: string; page?: number; page_size?: number }) => {
  const sp = new URLSearchParams()
  if (params?.recording_id) sp.set('recording_id', params.recording_id)
  if (params?.visit_id) sp.set('visit_id', params.visit_id)
  if (params?.status) sp.set('status', params.status)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`segments${qs ? `?${qs}` : ''}`).json<PaginatedResponse<Segment>>()
}

export const fetchSegment = (id: string) =>
  api.get(`segments/${id}`).json<Segment>()

export const updateSegment = (id: string, data: { visit_id?: string | null; speaker_label?: string | null }) =>
  api.put(`segments/${id}`, { json: data }).json<Segment>()

export const unlinkSegment = (id: string) =>
  api.post(`segments/${id}/unlink`).json<Segment>()

export const resplitSegments = (recordingId: string) =>
  api.post(`segments/resplit/${recordingId}`).json<Segment[]>()
