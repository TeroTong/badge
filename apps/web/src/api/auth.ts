import { api } from './client'

export type User = {
  id: string
  username: string
  display_name: string
  role: string
  is_active: boolean
  staff_id?: string | null
  staff_name?: string | null
  staff_external_account?: string | null
  staff_wecom_user_id?: string | null
  staff_wecom_corp_id?: string | null
  hospital_code?: string | null
  hospital_name?: string | null
}

export type AccountActivity = {
  id: string
  operator_name: string
  ip_address: string
  module_name: string
  action_name: string
  content: string
  created_at: string
}

export type AccountProfile = User & {
  created_at: string
  updated_at: string
  activity_count: number
  last_activity_at: string | null
  recent_activities: AccountActivity[]
}

export type MyBadge = {
  bound: boolean
  reason?: string | null
  device_id?: string | null
  device_code?: string | null
  device_name?: string | null
  staff_id?: string | null
  staff_name?: string | null
  external_account?: string | null
  hospital_short_name?: string | null
  position_name?: string | null
  status?: string | null
  online?: boolean | null
  battery_level?: number | null
  team_code?: string | null
  user_id?: string | null
  can_control_recording: boolean
  is_recording: boolean
  recording_started_at?: string | null
  remote_warning?: string | null
}

export type TokenResponse = {
  access_token: string
  refresh_token: string
  token_type: string
}

export type WecomAuthorizeUrlResponse = {
  authorize_url: string
}

export const login = (username: string, password: string) =>
  api.post('auth/login', { json: { username, password } }).json<TokenResponse>()

export const loginWithWecomCode = (code: string) =>
  api.post('auth/wecom/exchange', { json: { code } }).json<TokenResponse>()

export const getWecomAuthorizeUrl = (redirect?: string) => {
  const sp = new URLSearchParams()
  if (redirect) sp.set('redirect', redirect)
  const qs = sp.toString()
  return api.get(`auth/wecom/authorize-url${qs ? `?${qs}` : ''}`).json<WecomAuthorizeUrlResponse>()
}

export const getMe = () => api.get('auth/me').json<User>()

export const getAccountProfile = () => api.get('account/me').json<AccountProfile>()

export const getMyBadge = () => api.get('account/my-badge').json<MyBadge>()

export const getManagedBadges = () => api.get('account/managed-badges').json<MyBadge[]>()

export const updateAccountProfile = (display_name: string) =>
  api.put('account/me', { json: { display_name } }).json<AccountProfile>()

export const changeAccountPassword = (current_password: string, new_password: string) =>
  api.post('account/change-password', { json: { current_password, new_password } }).json<{ message: string }>()

export const startMyBadgeRecording = () =>
  api.post('account/my-badge/recording/start').json<{ message: string }>()

export const stopMyBadgeRecording = () =>
  api.post('account/my-badge/recording/stop').json<{ message: string }>()
