import { api } from './client'

export type MultiRecordingMode = 'many_to_many_visit_linking'

export type PreferenceSettings = {
  multi_recording_mode: MultiRecordingMode
  auto_match_recording: boolean
  iot_capabilities?: Record<string, boolean>
}

export type PreferenceProfile = {
  id: string
  scope_key: string
  name: string
  settings: PreferenceSettings
  created_at: string
  updated_at: string
}

export const fetchPreferenceProfile = () =>
  api.get('preferences/profile').json<PreferenceProfile>()

export const updatePreferenceProfile = (settings: PreferenceSettings) =>
  api.put('preferences/profile', { json: { settings } }).json<PreferenceProfile>()
