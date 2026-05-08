import { api } from './client'

export type WecomMenuEntry = {
  label: string
  type: string
  level: number
  target_path: string | null
  target_url: string | null
}

export type WecomMenuState = {
  agent_id: string
  exists: boolean
  source: string
  menu: Record<string, unknown>
  entries: WecomMenuEntry[]
}

export type WecomMenuActionResult = {
  agent_id: string
  action: string
  menu: Record<string, unknown>
  entries: WecomMenuEntry[]
}

export type WecomJsSdkConfig = {
  corp_id: string
  agent_id: string | null
  timestamp: number
  nonceStr: string
  signature: string
}

export const fetchDefaultWecomMenu = () =>
  api.get('wecom/menu/default').json<WecomMenuState>()

export const fetchCurrentWecomMenu = () =>
  api.get('wecom/menu/current').json<WecomMenuState>()

export const publishDefaultWecomMenu = () =>
  api.post('wecom/menu/default/publish').json<WecomMenuActionResult>()

export const deleteCurrentWecomMenu = () =>
  api.delete('wecom/menu/current').json<WecomMenuActionResult>()

export const fetchWecomJsSdkConfig = (url: string) =>
  api.get('wecom/sdk/config', {
    searchParams: { url },
  }).json<WecomJsSdkConfig>()
