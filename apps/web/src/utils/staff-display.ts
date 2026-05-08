type StaffIdentityLike = {
  name?: string | null
  external_account?: string | null
  badge_id?: string | null
}

type RecordingDeviceLike = {
  staff_badge_id?: string | null
  device_code?: string | null
}

function asText(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

export function getAdvisorCode(staff: Pick<StaffIdentityLike, 'external_account' | 'badge_id'> | null | undefined): string | null {
  if (!staff) return null

  const externalAccount = asText(staff.external_account)
  if (externalAccount) return externalAccount

  const badgeId = asText(staff.badge_id)
  if (badgeId && /^\d{6,12}$/.test(badgeId)) return badgeId

  return null
}

export function getDeviceBadgeId(staff: Pick<StaffIdentityLike, 'external_account' | 'badge_id'> | null | undefined): string | null {
  if (!staff) return null

  const externalAccount = asText(staff.external_account)
  const badgeId = asText(staff.badge_id)
  if (!badgeId) return null

  if (externalAccount) return badgeId === externalAccount ? null : badgeId
  if (/[A-Za-z]/.test(badgeId)) return badgeId

  return null
}

export function formatStaffDisplayLabel(staff: StaffIdentityLike | null | undefined): string {
  const name = asText(staff?.name) ?? '未命名人员'
  const parts = [name]

  const advisorCode = getAdvisorCode(staff)
  if (advisorCode) parts.push(`员工编号 ${advisorCode}`)

  const deviceBadgeId = getDeviceBadgeId(staff)
  if (deviceBadgeId) parts.push(`设备工牌 ${deviceBadgeId}`)

  return parts.join(' / ')
}

export function getRecordingDeviceBadge(recording: RecordingDeviceLike | null | undefined): string | null {
  if (!recording) return null

  const deviceCode = asText(recording.device_code)
  if (deviceCode) return deviceCode

  return asText(recording.staff_badge_id)
}
