import { api, type PaginatedResponse } from './client'

export type Tag = {
  id: string
  category_id: string
  name: string
  sort_order: number
  is_active: boolean
}

export type TagCategory = {
  id: string
  name: string
  description: string
  group_name: string | null
  weight_level: number | null
  sort_order: number
  is_active: boolean
  tags: Tag[]
}

export type Hotword = {
  id: string
  group_id: string
  word: string
  weight: number
  is_active: boolean
  created_at: string
}

export type HotwordGroup = {
  id: string
  name: string
  group_type: string
  library_scope: 'personal' | 'public'
  source_label: string
  is_active: boolean
  created_at: string
  updated_at: string
  words: Hotword[]
}

export type HotwordBulkCreateResult = {
  created: Hotword[]
  skipped_existing: string[]
  skipped_duplicate: string[]
}

export type Staff = {
  id: string
  name: string
  phone: string | null
  external_account: string | null
  wecom_user_id: string | null
  wecom_corp_id: string | null
  gender: string | null
  hospital_code: string | null
  hospital_short_name: string | null
  position_id: string | null
  position_name: string | null
  role: string
  permission_role: string
  badge_id: string | null
  is_doctor: boolean
  is_nurse: boolean
  is_anesthetist: boolean
  is_cashier: boolean
  is_guide: boolean
  is_pre_advisor: boolean
  is_onsite_advisor: boolean
  is_advisor_assistant: boolean
  is_doctor_assistant: boolean
  is_vip_service: boolean
  is_active: boolean
  account_opened: boolean
  account_username: string | null
  account_is_active: boolean | null
  account_last_login_at: string | null
}

export type StaffAccountActionResult = {
  staff_id: string
  staff_name: string
  username: string
  is_active: boolean
  created: boolean
  source_field: string | null
  source_label: string | null
  temporary_password: string | null
  message: string
}

export type StaffImportRow = {
  name: string
  phone?: string | null
  external_account?: string | null
  wecom_user_id?: string | null
  gender?: string | null
  hospital_code?: string | null
  hospital_short_name?: string | null
  position_name?: string | null
  permission_role?: string | null
  is_active?: boolean
}

export type StaffImportResult = {
  created_count: number
}

export type StaffDirectorySyncStatus = {
  scheduler_enabled: boolean
  scheduler_running: boolean
  scheduler_started_at: string | null
  scheduler_note: string | null
  interval_seconds: number
  last_synced_at: string | null
  next_scheduled_at: string | null
  last_sync_status: 'not_started' | 'success' | 'failed'
  checked_count: number | null
  updated_count: number | null
  missing_count: number | null
  deactivated_count: number | null
  error_message: string | null
}

export type StaffHospitalOption = {
  hospital_code: string
  hospital_name: string
}

export type StaffIdentityLookup = {
  external_account: string
  name: string | null
  hospital_code: string | null
  hospital_short_name: string | null
  phone: string | null
  dingtalk_user_id: string | null
  source: string
}

export type StaffBadgeBindingCandidate = {
  id: string
  name: string
  external_account: string | null
  badge_id: string | null
  hospital_code: string | null
  hospital_short_name: string | null
  position_name: string | null
  is_active: boolean
  account_opened: boolean
  account_username: string | null
  account_is_active: boolean | null
}

export type OrganizationStaff = {
  id: string
  name: string
  external_account: string | null
  hospital_code: string | null
  hospital_short_name: string | null
  position_id: string | null
  position_name: string | null
  permission_role: string
  is_active: boolean
}

export type OrganizationUnit = {
  id: string
  hospital_code: string
  hospital_name: string | null
  name: string
  parent_id: string | null
  path: string
  sort_order: number
  member_count: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export type OrganizationUnitMember = {
  unit_id: string
  staff_id: string
  staff_name: string
  external_account: string | null
  position_name: string | null
  hospital_code: string | null
  hospital_short_name: string | null
  is_primary: boolean
  is_active: boolean
  created_at: string
}

export type StaffManagementRelation = {
  id: string
  hospital_code: string
  manager_staff_id: string
  manager_name: string
  subordinate_staff_id: string
  subordinate_name: string
  created_at: string
}

export type OrganizationOverview = {
  hospital_code: string
  hospital_name: string | null
  staff: OrganizationStaff[]
  units: OrganizationUnit[]
  memberships: OrganizationUnitMember[]
  management_relations: StaffManagementRelation[]
}

export type PositionProfile = {
  id: string
  name: string
  position_type: string
  mapped_role: string
  is_super_admin: boolean
  note: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export type DepartmentAssistantDepartmentConfig = {
  department_code: string
  department_name?: string | null
  assistant_staff_ids: string[]
}

export type DepartmentAssistantMatchConfig = {
  enabled: boolean
  departments: DepartmentAssistantDepartmentConfig[]
}

export type WecomTenant = {
  id: string
  name: string
  host: string | null
  corp_id: string | null
  agent_id: string | null
  frontend_url: string | null
  callback_configured: boolean
  default_hospital_code: string | null
  default_hospital_name: string | null
  sap_summary_template_name: string | null
  sap_summary_template_version: string | null
  sap_summary_template: string | null
  sap_summary_prompt: string | null
  sap_summary_enabled: boolean
  sap_auto_update_existing_consultation: boolean
  department_assistant_match_config: DepartmentAssistantMatchConfig | null
  is_default: boolean
  is_active: boolean
  agent_secret_configured: boolean
  created_at: string
  updated_at: string
}

export type WecomTenantPayload = {
  name?: string
  host?: string | null
  corp_id?: string | null
  agent_id?: string | null
  agent_secret?: string | null
  callback_token?: string | null
  callback_aes_key?: string | null
  frontend_url?: string | null
  default_hospital_code?: string | null
  default_hospital_name?: string | null
  sap_summary_template_name?: string | null
  sap_summary_template_version?: string | null
  sap_summary_template?: string | null
  sap_summary_prompt?: string | null
  sap_summary_enabled?: boolean
  sap_auto_update_existing_consultation?: boolean
  department_assistant_match_config?: DepartmentAssistantMatchConfig | null
  is_default?: boolean
  is_active?: boolean
}

export type AuditLog = {
  id: string
  operator_name: string
  ip_address: string
  module_name: string
  action_name: string
  content: string
  created_at: string
}

export type AsrUsageRange = {
  label: string
  start_date: string
  end_date: string
  request_count: number
  duration_seconds: number
}

export type AsrInstitutionUsage = {
  hospital_code: string
  hospital_name: string
  today_request_count: number
  today_duration_seconds: number
  last_7_days_request_count: number
  last_7_days_duration_seconds: number
  last_30_days_request_count: number
  last_30_days_duration_seconds: number
  last_30_days_failed_count: number
  average_duration_seconds: number
  share_percent: number
  latest_transcribed_at: string | null
}

export type AsrQuotaPackage = {
  name: string
  fee_mode: boolean
  total_seconds: number
  remaining_seconds: number
  used_seconds: number
  effective_time: string | null
  expiry_time: string | null
  pid: number | null
  unit: string | null
  sub_product_code: string | null
  available_type: number
}

export type AsrMonitoringOverview = {
  provider: string
  has_tencent_credentials: boolean
  request_log_available: boolean
  cloud_audit_log_available: boolean
  quota_state: 'normal' | 'exhausted' | 'unknown'
  quota_message: string | null
  local_exact_count: number
  local_success_count: number
  local_failed_count: number
  local_submitted_duration_ms: number
  local_recognized_duration_ms: number
  cloud_total_count: number
  cloud_failed_count: number
  latest_event_at: string | null
  latest_error_message: string | null
  quota_total_seconds: number
  quota_remaining_seconds: number
  quota_used_seconds: number
  quota_package_count: number
  quota_active_package_count: number
  quota_exhausted_package_count: number
  quota_packages: AsrQuotaPackage[]
  quota_fetch_error_message: string | null
  usage_ranges: AsrUsageRange[]
  usage_error_message: string | null
  institution_usage: AsrInstitutionUsage[]
  institution_usage_error_message: string | null
}

export type AsrRequestEvent = {
  id: string
  source: 'local_audit' | 'cloud_audit'
  action: string
  occurred_at: string | null
  status: 'submitted' | 'completed' | 'submit_failed' | 'task_failed' | 'unknown'
  audio_name: string | null
  audio_path: string | null
  source_id: string | null
  source_ip: string | null
  chunk_index: number | null
  chunk_count: number | null
  submitted_duration_ms: number | null
  recognized_duration_ms: number | null
  file_size_bytes: number | null
  request_id: string | null
  task_id: number | null
  error_code: string | null
  error_message: string | null
}

export type SapPushMonitoringOverview = {
  total_count: number
  succeeded_count: number
  failed_count: number
  pending_count: number
  auto_count: number
  manual_count: number
  latest_sent_at: string | null
}

export type SapPushMonitoringLog = {
  id: string
  log_id: string
  target_index: number
  target_count: number
  is_primary_target: boolean
  recording_id: string
  recording_file_name: string | null
  recording_created_at: string | null
  visit_id: string | null
  visit_order_no: string | null
  visit_order_seg: string | null
  customer_name: string | null
  customer_code: string | null
  advisor_name: string | null
  trigger_mode: string | null
  status: string
  send_enabled: boolean
  initiated_by: string | null
  request_url: string | null
  trace_id: string | null
  request_payloads: Record<string, unknown>[]
  gateway_requests: Record<string, unknown>[]
  response_items: Record<string, unknown>[]
  http_status_code: number | null
  business_status: string | null
  business_message: string | null
  error_message: string | null
  effective_status: string | null
  effective_business_status: string | null
  effective_reason: string | null
  result_status: string
  result_reason: string | null
  sent_at: string | null
  created_at: string
  updated_at: string
}

export const fetchCategories = () => api.get('tags/categories').json<TagCategory[]>()
export const createCategory = (data: { name: string; description?: string; weight_level?: number; group_name?: string; sort_order?: number }) =>
  api.post('tags/categories', { json: data }).json<TagCategory>()
export const updateCategory = (id: string, data: Partial<TagCategory>) =>
  api.put(`tags/categories/${id}`, { json: data }).json<TagCategory>()
export const deleteCategory = (id: string) => api.delete(`tags/categories/${id}`)
export const createTag = (categoryId: string, data: { name: string; sort_order?: number }) =>
  api.post(`tags/categories/${categoryId}/tags`, { json: data }).json<Tag>()
export const updateTag = (id: string, data: Partial<Tag>) =>
  api.put(`tags/tags/${id}`, { json: data }).json<Tag>()
export const deleteTag = (id: string) => api.delete(`tags/tags/${id}`)

export type BulkImportItem = { name: string; group: string; weight: number; description: string; options: string[] }
export type BulkImportResult = { categories_created: number; tags_created: number }
export const bulkImportTags = (items: BulkImportItem[]) =>
  api.post('tags/import', { json: { items } }).json<BulkImportResult>()

export const fetchHotwordGroups = (params?: { library_scope?: 'personal' | 'public' }) => {
  const sp = new URLSearchParams()
  if (params?.library_scope) sp.set('library_scope', params.library_scope)
  const qs = sp.toString()
  return api.get(`hotwords/groups${qs ? `?${qs}` : ''}`).json<HotwordGroup[]>()
}
export const createHotwordGroup = (data: {
  name: string
  group_type: string
  library_scope?: 'personal' | 'public'
  source_label?: string
}) => api.post('hotwords/groups', { json: data }).json<HotwordGroup>()
export const updateHotwordGroup = (id: string, data: Partial<HotwordGroup>) =>
  api.put(`hotwords/groups/${id}`, { json: data }).json<HotwordGroup>()
export const deleteHotwordGroup = (id: string) => api.delete(`hotwords/groups/${id}`)
export const createHotword = (groupId: string, data: { word: string; weight?: number; is_active?: boolean }) =>
  api.post(`hotwords/groups/${groupId}/words`, { json: data }).json<Hotword>()
export const createHotwordsBulk = (
  groupId: string,
  data: { words: string[]; weight?: number; is_active?: boolean },
) => api.post(`hotwords/groups/${groupId}/words/bulk`, { json: data }).json<HotwordBulkCreateResult>()
export const updateHotword = (id: string, data: Partial<Hotword>) =>
  api.put(`hotwords/words/${id}`, { json: data }).json<Hotword>()
export const deleteHotword = (id: string) => api.delete(`hotwords/words/${id}`)

export const fetchStaff = (params?: {
  keyword?: string
  position_id?: string
  badge_id?: string
  hospital_code?: string
  account_status?: 'not_opened' | 'active' | 'disabled'
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.position_id) sp.set('position_id', params.position_id)
  if (params?.badge_id) sp.set('badge_id', params.badge_id)
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.account_status) sp.set('account_status', params.account_status)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`staff${qs ? `?${qs}` : ''}`).json<PaginatedResponse<Staff>>()
}
export const fetchStaffDetail = (id: string) => api.get(`staff/${id}`).json<Staff>()
export const fetchStaffHospitalOptions = () => api.get('staff/hospital-options').json<StaffHospitalOption[]>()
export const lookupStaffIdentity = (params: { external_account: string; hospital_code?: string | null }) => {
  const sp = new URLSearchParams()
  sp.set('external_account', params.external_account)
  if (params.hospital_code) sp.set('hospital_code', params.hospital_code)
  return api.get(`staff/identity-lookup?${sp.toString()}`).json<StaffIdentityLookup>()
}
export const createStaff = (data: {
  name?: string | null
  phone?: string | null
  external_account?: string | null
  wecom_user_id?: string | null
  gender?: string | null
  hospital_code?: string | null
  hospital_short_name?: string | null
  position_id?: string | null
  role?: string | null
  permission_role?: string | null
  is_active?: boolean
}) => api.post('staff', { json: data }).json<Staff>()
export const importStaff = (data: { rows: StaffImportRow[] }) =>
  api.post('staff/import', { json: data }).json<StaffImportResult>()
export const fetchStaffDirectorySyncStatus = () => api.get('staff/sync-status').json<StaffDirectorySyncStatus>()
export const fetchStaffBadgeBindingCandidates = (params?: { keyword?: string; hospital_code?: string; include_inactive?: boolean }) => {
  const sp = new URLSearchParams()
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.include_inactive !== undefined) sp.set('include_inactive', String(params.include_inactive))
  const qs = sp.toString()
  return api.get(`staff/badge-binding-candidates${qs ? `?${qs}` : ''}`).json<StaffBadgeBindingCandidate[]>()
}
export const updateStaff = (id: string, data: Partial<Staff>) =>
  api.put(`staff/${id}`, { json: data }).json<Staff>()
export const deleteStaff = (id: string) => api.delete(`staff/${id}`)
export const enableStaffAccount = (id: string) =>
  api.post(`staff/${id}/account/enable`).json<StaffAccountActionResult>()
export const resetStaffAccountPassword = (id: string) =>
  api.post(`staff/${id}/account/reset-password`).json<StaffAccountActionResult>()
export const disableStaffAccount = (id: string) =>
  api.post(`staff/${id}/account/disable`).json<StaffAccountActionResult>()
export const activateStaffAccount = (id: string) =>
  api.post(`staff/${id}/account/activate`).json<StaffAccountActionResult>()

export const fetchOrganizationOverview = (params?: { hospital_code?: string | null }) => {
  const sp = new URLSearchParams()
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  const qs = sp.toString()
  return api.get(`organization/overview${qs ? `?${qs}` : ''}`).json<OrganizationOverview>()
}
export const createOrganizationUnit = (data: {
  name: string
  hospital_code?: string | null
  parent_id?: string | null
  sort_order?: number
  is_active?: boolean
}) => api.post('organization/units', { json: data }).json<OrganizationUnit>()
export const updateOrganizationUnit = (
  id: string,
  data: {
    name?: string
    parent_id?: string | null
    sort_order?: number
    is_active?: boolean
  },
) => api.put(`organization/units/${id}`, { json: data }).json<OrganizationUnit>()
export const deleteOrganizationUnit = (id: string) => api.delete(`organization/units/${id}`)
export const replaceOrganizationUnitMembers = (id: string, staffIds: string[]) =>
  api.put(`organization/units/${id}/members`, { json: { staff_ids: staffIds } }).json<OrganizationUnitMember[]>()
export const moveOrganizationUnitMembers = (id: string, data: { staff_ids: string[]; target_unit_id: string }) =>
  api.post(`organization/units/${id}/members/move`, { json: data }).json<OrganizationUnitMember[]>()
export const createStaffManagementRelation = (data: { manager_staff_id: string; subordinate_staff_id: string }) =>
  api.post('organization/management-relations', { json: data }).json<StaffManagementRelation>()
export const createStaffManagementRelationsBulk = (data: {
  manager_staff_id: string
  subordinate_staff_ids: string[]
}) => api.post('organization/management-relations/bulk', { json: data }).json<StaffManagementRelation[]>()
export const createStaffManagementRelationsByUnit = (data: {
  manager_staff_id: string
  unit_id: string
  include_descendants: boolean
}) => api.post('organization/management-relations/by-unit', { json: data }).json<StaffManagementRelation[]>()
export const syncStaffManagementRelations = (managerStaffId: string, subordinateStaffIds: string[]) =>
  api
    .put(`organization/management-relations/managers/${managerStaffId}`, { json: { subordinate_staff_ids: subordinateStaffIds } })
    .json<StaffManagementRelation[]>()
export const deleteStaffManagementRelation = (id: string) => api.delete(`organization/management-relations/${id}`)

export const fetchPositions = (params?: { keyword?: string; position_type?: string; is_super_admin?: boolean }) => {
  const sp = new URLSearchParams()
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.position_type) sp.set('position_type', params.position_type)
  if (params?.is_super_admin !== undefined) sp.set('is_super_admin', String(params.is_super_admin))
  const qs = sp.toString()
  return api.get(`positions${qs ? `?${qs}` : ''}`).json<PositionProfile[]>()
}
export const createPosition = (data: {
  name: string
  position_type?: string
  mapped_role?: string
  is_super_admin?: boolean
  note?: string
  is_active?: boolean
}) => api.post('positions', { json: data }).json<PositionProfile>()
export const updatePosition = (id: string, data: Partial<PositionProfile>) =>
  api.put(`positions/${id}`, { json: data }).json<PositionProfile>()
export const deletePosition = (id: string) => api.delete(`positions/${id}`)

export const fetchWecomTenants = (params?: {
  keyword?: string
  is_active?: boolean
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.is_active !== undefined) sp.set('is_active', String(params.is_active))
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`wecom/tenants${qs ? `?${qs}` : ''}`).json<PaginatedResponse<WecomTenant>>()
}
export const createWecomTenant = (data: WecomTenantPayload) =>
  api.post('wecom/tenants', { json: data }).json<WecomTenant>()
export const updateWecomTenant = (id: string, data: WecomTenantPayload) =>
  api.put(`wecom/tenants/${id}`, { json: data }).json<WecomTenant>()
export const deleteWecomTenant = (id: string) => api.delete(`wecom/tenants/${id}`)

export const fetchAuditLogs = (params?: {
  date_from?: string
  date_to?: string
  ip_address?: string
  module_name?: string
  content?: string
  operator_name?: string
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.ip_address) sp.set('ip_address', params.ip_address)
  if (params?.module_name) sp.set('module_name', params.module_name)
  if (params?.content) sp.set('content', params.content)
  if (params?.operator_name) sp.set('operator_name', params.operator_name)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`audit-logs${qs ? `?${qs}` : ''}`).json<PaginatedResponse<AuditLog>>()
}

export const fetchAsrMonitoringOverview = () =>
  api.get('asr-monitoring/overview').json<AsrMonitoringOverview>()

export const fetchAsrMonitoringRequests = (params?: {
  source?: 'all' | 'local_audit' | 'cloud_audit'
  status?: 'submitted' | 'completed' | 'submit_failed' | 'task_failed' | 'unknown'
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.source) sp.set('source', params.source)
  if (params?.status) sp.set('status', params.status)
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`asr-monitoring/requests${qs ? `?${qs}` : ''}`).json<PaginatedResponse<AsrRequestEvent>>()
}

export const fetchSapPushMonitoringOverview = (params?: { hospital_code?: string }) => {
  const sp = new URLSearchParams()
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  const qs = sp.toString()
  return api.get(`sap-push-monitoring/overview${qs ? `?${qs}` : ''}`).json<SapPushMonitoringOverview>()
}

export const fetchSapPushMonitoringLogs = (params?: {
  hospital_code?: string
  status?: string
  trigger_mode?: string
  keyword?: string
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}) => {
  const sp = new URLSearchParams()
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.status) sp.set('status', params.status)
  if (params?.trigger_mode) sp.set('trigger_mode', params.trigger_mode)
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.date_from) sp.set('date_from', params.date_from)
  if (params?.date_to) sp.set('date_to', params.date_to)
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  const qs = sp.toString()
  return api.get(`sap-push-monitoring/logs${qs ? `?${qs}` : ''}`).json<PaginatedResponse<SapPushMonitoringLog>>()
}

// ── 到诊单 ──

export type VisitOrder = {
  id: string
  dzdh: string
  dzseg: string | null
  sjrq: string | null
  jgbm: string | null
  fzuer: string | null
  fzuer_long: string | null
  advxc: string | null
  advxc_long: string | null
  ksgw: string | null
  ksgw_long: string | null
  advyq: string | null
  kunr: string | null
  ninam: string | null
  kusex: string | null
  kusex_txt: string | null
  yydh: string | null
  yyuer: string | null
  kutyp_dq: string | null
  kutyp_dq_txt: string | null
  kut30_dq: string | null
  kut30_dq_txt: string | null
  kusta_dq: string | null
  kusta_dq_txt: string | null
  kulvl_dq: string | null
  vipkf: string | null
  d_fzuer: string | null
  d_vipkf: string | null
  fzdh: string | null
  fzsj: string | null
  fzsta: string | null
  fzsta_txt: string | null
  ddsc: string | null
  bhkx: string | null
  assxc: string | null
  jgks: string | null
  jgks_txt: string | null
  dztyp: string | null
  dztyp_txt: string | null
  dzsta: string | null
  dzsta_txt: string | null
  dzly: string | null
  dymd: string | null
  jcsta: string | null
  jcsta_txt: string | null
  kusrc: string | null
  kusrc2: string | null
  remark_dz: string | null
  bjzx: string | null
  dymd_txt: string | null
  dzly_txt: string | null
  crtdt: string | null
  crttm: string | null
}

export type MatchEvidence = {
  type: string
  label: string
  detail: string
  strength: string
}

export type RecordingMatchCandidate = {
  recording_id: string
  local_visit_id: string | null
  file_name: string
  created_at: string
  staff_name: string | null
  advisor_code: string | null
  customer_name: string | null
  current_visit_id: string | null
  current_visit_order_no: string | null
  current_visit_order_seg: string | null
  confidence: number
  decision: string
  method: string
  reasons: string[]
  excluded_reasons: string[]
  identity_conflicts: string[]
  manual_review_required: boolean
  manual_review_reason: string | null
  evidence: MatchEvidence[]
}

export type VisitOrderRecordingMatch = {
  visit_order_id: string
  local_visit_id: string | null
  dzdh: string
  dzseg: string | null
  visit_date: string | null
  advisor_code: string | null
  customer_code: string | null
  customer_name: string | null
  customer_type_code: string | null
  customer_type_label: string | null
  linked_recording_ids: string[]
  identity_conflicts: string[]
  manual_review_required: boolean
  manual_review_reason: string | null
  summary: string
  analyzed_at: string
  candidates: RecordingMatchCandidate[]
}

export type VisitOrderSyncResult = {
  synced_count: number
  new_count: number
  updated_count: number
  date_range: string
}

export const fetchVisitOrders = (params?: {
  page?: number
  page_size?: number
  hospital_code?: string
  keyword?: string
  fzuer?: string
  sjrq_start?: string
  sjrq_end?: string
  jcsta_txt?: string
  fast_page?: boolean
}) => {
  const sp = new URLSearchParams()
  if (params?.page) sp.set('page', String(params.page))
  if (params?.page_size) sp.set('page_size', String(params.page_size))
  if (params?.hospital_code) sp.set('hospital_code', params.hospital_code)
  if (params?.keyword) sp.set('keyword', params.keyword)
  if (params?.fzuer) sp.set('fzuer', params.fzuer)
  if (params?.sjrq_start) sp.set('sjrq_start', params.sjrq_start)
  if (params?.sjrq_end) sp.set('sjrq_end', params.sjrq_end)
  if (params?.jcsta_txt) sp.set('jcsta_txt', params.jcsta_txt)
  if (params?.fast_page !== undefined) sp.set('fast_page', String(params.fast_page))
  const qs = sp.toString()
  return api.get(`visit-orders${qs ? `?${qs}` : ''}`).json<PaginatedResponse<VisitOrder>>()
}

export const fetchVisitOrder = (id: string) => api.get(`visit-orders/${id}`).json<VisitOrder>()

export const syncVisitOrders = () => api.post('visit-orders/sync').json<VisitOrderSyncResult>()

export const fetchVisitOrderRecordingMatch = (id: string) =>
  api.get(`visit-orders/${id}/recording-match`).json<VisitOrderRecordingMatch>()
