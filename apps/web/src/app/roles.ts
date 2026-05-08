export type PermissionRole =
  | 'super_admin'
  | 'system_admin'
  | 'hospital_admin'
  | 'staff'

export const ROLE_LEVELS: Record<PermissionRole, number> = {
  staff: 10,
  hospital_admin: 30,
  system_admin: 90,
  super_admin: 100,
}

export const ROLE_LABELS: Record<PermissionRole, string> = {
  super_admin: '超级管理员',
  system_admin: '系统管理员',
  hospital_admin: '机构管理员',
  staff: '普通员工',
}

const LEGACY_ROLE_MAP: Record<string, PermissionRole> = {
  admin: 'system_admin',
  manager: 'hospital_admin',
  viewer: 'staff',
}

export function normalizeRole(role: string | null | undefined): PermissionRole {
  const normalized = (role ?? '').trim()
  if (!normalized) return 'staff'
  if (normalized in ROLE_LEVELS) return normalized as PermissionRole
  if (normalized in LEGACY_ROLE_MAP) return LEGACY_ROLE_MAP[normalized]
  return 'staff'
}

export function roleLevel(role: string | null | undefined): number {
  return ROLE_LEVELS[normalizeRole(role)]
}

export function roleLabel(role: string | null | undefined): string {
  return ROLE_LABELS[normalizeRole(role)]
}

export function canAccessRole(role: string | null | undefined, minRole: PermissionRole = 'staff'): boolean {
  return roleLevel(role) >= roleLevel(minRole)
}

export function isSystemAdminOrAbove(role: string | null | undefined): boolean {
  return canAccessRole(role, 'system_admin')
}

export function isHospitalAdminOrAbove(role: string | null | undefined): boolean {
  return canAccessRole(role, 'hospital_admin')
}
