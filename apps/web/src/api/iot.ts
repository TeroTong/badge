import { api } from './client'

export type IotRiskLevel = 'medium' | 'high' | string

export type IotCapabilityDefinition = {
  key: string
  title: string
  group: string
  description: string
  risk_level: IotRiskLevel
}

export type IotCapabilityState = {
  definitions: IotCapabilityDefinition[]
  capabilities: Record<string, boolean>
}

export const fetchIotCapabilities = () =>
  api.get('iot/capabilities').json<IotCapabilityState>()

export const updateIotCapabilities = (capabilities: Record<string, boolean>) =>
  api.put('iot/capabilities', { json: { capabilities } }).json<IotCapabilityState>()
