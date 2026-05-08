import { api, type PaginatedResponse } from './client'

// ── 企业工牌配置 ──

export type ConfigureBadgePayload = {
  code_identity?: string
  status?: string
}

export type BadgeConfigResult = {
  codeIdentity: string
  corpId: string
  status: string
  extInfo?: Record<string, unknown>
}

export async function configureBadge(payload: ConfigureBadgePayload = {}): Promise<BadgeConfigResult> {
  return api.post('dingtalk/configure-badge', { json: payload }).json()
}

// ── 电子码 CRUD ──

export type CreateBadgeCodePayload = {
  request_id: string
  code_identity: string
  user_identity: string
  user_corp_relation_type?: string
  status?: string
  code_value?: string
  gmt_expired?: string
  available_times?: { gmtStart: string; gmtEnd: string }[]
  ext_info?: Record<string, string>
}

export type BadgeCodeResult = {
  codeId: string
  codeDetailUrl?: string
}

export async function createBadgeCode(payload: CreateBadgeCodePayload): Promise<BadgeCodeResult> {
  return api.post('dingtalk/badge-codes', { json: payload }).json()
}

export type UpdateBadgeCodePayload = {
  code_id: string
  code_identity: string
  user_identity: string
  user_corp_relation_type?: string
  status?: string
  code_value?: string
  gmt_expired?: string
  available_times?: { gmtStart: string; gmtEnd: string }[]
  ext_info?: Record<string, string>
}

export async function updateBadgeCode(payload: UpdateBadgeCodePayload): Promise<{ codeId: string }> {
  return api.put('dingtalk/badge-codes', { json: payload }).json()
}

export type DecodeBadgeCodePayload = {
  pay_code: string
  code_identity?: string
}

export type DecodeBadgeCodeResult = {
  corpId?: string
  userId?: string
  codeIdentity?: string
  codeId?: string
  userIdentity?: string
  userCorpRelationType?: string
  extInfo?: Record<string, string>
}

export async function decodeBadgeCode(payload: DecodeBadgeCodePayload): Promise<DecodeBadgeCodeResult> {
  return api.post('dingtalk/badge-codes/decode', { json: payload }).json()
}

// ── DVI 设备 ──

export type DviTimestampedValue<T extends string | number> = {
  value?: T
  timestamp?: number
}

export type DviDevice = {
  sn: string
  name?: string
  teamCode?: string
  userId?: string
  hospitalCode?: string | null
  hospitalShortName?: string | null
  systemBinding?: DviSystemBinding | null
  remoteProvider?: 'dvi' | 'iot' | string
  iotAvailable?: boolean
  dviAvailable?: boolean
  status?: string | DviTimestampedValue<string>
  battery?: number | DviTimestampedValue<number>
  deviceType?: string
  [key: string]: unknown
}

export type DviSystemBinding = {
  deviceId: string
  staffId: string
  staffName: string
  externalAccount?: string | null
  hospitalCode?: string | null
  hospitalShortName?: string | null
  deviceHospitalCode?: string | null
  deviceHospitalShortName?: string | null
  positionName?: string | null
  isActive: boolean
  accountOpened: boolean
  accountUsername?: string | null
  accountIsActive?: boolean | null
}

export type DviDeviceListResult = {
  result?: DviDevice[]
  nextToken?: string
  hasMore?: boolean
}

export async function listDevices(params?: {
  maxResults?: number
  nextToken?: string
  sn?: string
  teamCode?: string
  userId?: string
  hospitalCode?: string
  syncStatus?: boolean
}): Promise<DviDeviceListResult> {
  const searchParams: Record<string, string> = {}
  if (params?.maxResults) searchParams.maxResults = String(params.maxResults)
  if (params?.nextToken) searchParams.nextToken = params.nextToken
  if (params?.sn) searchParams.sn = params.sn
  if (params?.teamCode) searchParams.teamCode = params.teamCode
  if (params?.userId) searchParams.userId = params.userId
  if (params?.hospitalCode) searchParams.hospitalCode = params.hospitalCode
  if (params?.syncStatus) searchParams.syncStatus = 'true'
  return api.get('dingtalk/devices', { searchParams }).json()
}

export type DviDeviceStatus = {
  sn: string
  online?: boolean
  battery?: number
  [key: string]: unknown
}

export type DviDeviceStatusResult = {
  result?: DviDeviceStatus[]
}

export async function queryDeviceStatus(snList: string[]): Promise<DviDeviceStatusResult> {
  return api.post('dingtalk/devices/status', { json: { snList } }).json()
}

export type DviDeviceDetail = {
  sn: string
  [key: string]: unknown
}

export type DviDeviceDetailResult = {
  result?: DviDeviceDetail[]
}

export async function queryDeviceDetail(snList: string[]): Promise<DviDeviceDetailResult> {
  return api.post('dingtalk/devices/details', { json: { snList } }).json()
}

// ── DVI 绑定/解绑 ──

export async function bindDevice(
  sn: string,
  teamCode: string,
  userId: string,
): Promise<unknown> {
  return api.post('dingtalk/devices/bind', { json: { sn, teamCode, userId } }).json()
}

export async function unbindDevice(
  sn: string,
  teamCode: string,
  userId: string,
): Promise<unknown> {
  return api.post('dingtalk/devices/unbind', { json: { sn, teamCode, userId } }).json()
}

export async function bindSystemDevice(
  sn: string,
  staffId: string,
  deviceName?: string,
  effectiveStart?: string | null,
  effectiveEnd?: string | null,
  overrideOverlap = false,
  effectiveAt?: string,
): Promise<unknown> {
  return api.post('dingtalk/devices/system-bind', {
    json: {
      sn,
      staffId,
      deviceName,
      effectiveStart,
      effectiveEnd,
      overrideOverlap,
      effectiveAt,
    },
  }).json()
}

export async function unbindSystemDevice(
  sn: string,
  clearHistory = true,
  clearRecordingOwners = false,
): Promise<unknown> {
  return api.post('dingtalk/devices/system-unbind', {
    json: { sn, clearHistory, clearRecordingOwners },
  }).json()
}

// ── DVI 录音控制 ──

export async function startRecording(teamCode: string, userId: string): Promise<unknown> {
  return api.post('dingtalk/devices/recording/start', { json: { teamCode, userId } }).json()
}

export async function stopRecording(teamCode: string, userId: string): Promise<unknown> {
  return api.post('dingtalk/devices/recording/stop', { json: { teamCode, userId } }).json()
}

// ── DVI 音频文件 ──

export type DviAudioFile = {
  fileId: string
  fileName?: string
  sn?: string
  duration?: number
  fileSize?: number
  createTime?: number
  [key: string]: unknown
}

export type DviAudioListResult = {
  result?: DviAudioFile[]
  nextToken?: string
  hasMore?: boolean
}

export async function listAudioFiles(payload: {
  sn: string
  maxResults?: number
  nextToken?: string
  startTimestamp?: number
  endTimestamp?: number
}): Promise<DviAudioListResult> {
  return api.post('dingtalk/audio-files/list', { json: payload }).json()
}

export async function getAudioFileInfo(fileId: string): Promise<DviAudioFile> {
  return api.get(`dingtalk/audio-files/${fileId}`).json()
}

export type DviAudioDownloadResult = {
  result?: { url?: string }
  downloadUrl?: string
  [key: string]: unknown
}

export async function getAudioDownloadUrl(fileId: string): Promise<DviAudioDownloadResult> {
  return api.get(`dingtalk/audio-files/${fileId}/download-url`).json()
}

export type DviAudioArchiveItem = {
  sn: string
  fileId: string
  status: string
  savedPath?: string | null
  message?: string | null
}

export type ArchiveAudioFilePayload = {
  sn: string
  fileId: string
  fileName?: string
  duration?: number
  fileSize?: number
  createTime?: number
  downloadUrl?: string
  source?: string
  overwrite?: boolean
}

export async function archiveAudioFile(payload: ArchiveAudioFilePayload): Promise<DviAudioArchiveItem> {
  return api.post('dingtalk/audio-files/archive-item', { json: payload }).json()
}

export type ArchiveAudioFilesPayload = {
  snList?: string[]
  overwrite?: boolean
}

export type DviAudioArchiveBatchResult = {
  archiveRoot: string
  downloaded: number
  skipped: number
  failed: number
  items: DviAudioArchiveItem[]
}

export async function archiveAudioFiles(payload: ArchiveAudioFilesPayload): Promise<DviAudioArchiveBatchResult> {
  return api.post('dingtalk/audio-files/archive', { json: payload }).json()
}

export type DingtalkArchiveRecording = {
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
  has_visit_link: boolean
  needs_visit_link: boolean
  utterance_count?: number | null
  full_text_length?: number | null
  has_transcript: boolean
  has_analysis: boolean
}

export type DingtalkArchiveRecordingDetail = DingtalkArchiveRecording & {
  manifest?: Record<string, unknown> | null
  archive_metadata?: Record<string, unknown> | null
  transcript?: Record<string, unknown> | null
  analysis_result?: Record<string, unknown> | null
  analysis_summary?: Record<string, unknown> | null
}

export type DingtalkArchiveEnsureRecordingResult = {
  item_id: string
  recording_id: string
  file_name: string
  created_new_recording: boolean
  visit_id?: string | null
  linked_visit_ids: string[]
  linked_visit_order_refs: string[]
}

export async function fetchArchiveRecordings(params?: {
  keyword?: string
  status?: string
  staffId?: string
  linkState?: 'linked' | 'unlinked' | 'needs_link'
  excludeFiltered?: boolean
  problemOnly?: boolean
  page?: number
  page_size?: number
}): Promise<PaginatedResponse<DingtalkArchiveRecording>> {
  const searchParams: Record<string, string> = {}
  if (params?.keyword) searchParams.keyword = params.keyword
  if (params?.status) searchParams.status = params.status
  if (params?.staffId) searchParams.staffId = params.staffId
  if (params?.linkState) searchParams.linkState = params.linkState
  if (params?.excludeFiltered !== undefined) searchParams.excludeFiltered = String(params.excludeFiltered)
  if (params?.problemOnly !== undefined) searchParams.problemOnly = String(params.problemOnly)
  if (params?.page) searchParams.page = String(params.page)
  if (params?.page_size) searchParams.page_size = String(params.page_size)
  return api.get('dingtalk/audio-archive/recordings', { searchParams }).json()
}

export async function fetchArchiveRecordingDetail(itemId: string): Promise<DingtalkArchiveRecordingDetail> {
  return api.get(`dingtalk/audio-archive/recordings/${itemId}`).json()
}

export async function fetchArchiveRecordingMediaBlob(itemId: string): Promise<Blob> {
  return api.get(`dingtalk/audio-archive/recordings/${itemId}/media`).blob()
}

export async function ensureArchiveRecording(itemId: string): Promise<DingtalkArchiveEnsureRecordingResult> {
  return api.post(`dingtalk/audio-archive/recordings/${itemId}/ensure-recording`).json()
}

// ── DVI 团队 ──

export type DviTeam = {
  teamCode: string
  teamName?: string
  [key: string]: unknown
}

export type DviTeamListResult = {
  result?: DviTeam[]
  nextToken?: string
  hasMore?: boolean
}

export async function listTeams(params?: {
  maxResults?: number
  nextToken?: string
}): Promise<DviTeamListResult> {
  const searchParams: Record<string, string> = {}
  if (params?.maxResults) searchParams.maxResults = String(params.maxResults)
  if (params?.nextToken) searchParams.nextToken = params.nextToken
  return api.get('dingtalk/teams', { searchParams }).json()
}
