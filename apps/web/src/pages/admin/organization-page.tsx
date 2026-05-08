import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Select,
  Segmented,
  Space,
  Table,
  Tag,
  Tree,
  Typography,
  type TableProps,
} from 'antd'
import {
  ApartmentOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  SwapOutlined,
  TeamOutlined,
  UserSwitchOutlined,
} from '@ant-design/icons'

import type {
  OrganizationStaff,
  OrganizationUnit,
  OrganizationUnitMember,
  StaffManagementRelation,
} from '@/api/admin'
import * as adminApi from '@/api/admin'
import * as authApi from '@/api/auth'
import { getApiErrorMessage } from '@/api/errors'

type OrgTreeNode = {
  title: ReactNode
  key: string
  children?: OrgTreeNode[]
}

type RelationTreeNode = {
  title: ReactNode
  key: string
  disabled?: boolean
  children?: RelationTreeNode[]
}

type UnitFormValues = {
  name: string
  parent_id?: string | null
  sort_order?: number
  is_active?: boolean
}

type MemberFormValues = {
  staff_ids: string[]
}

type MoveMemberFormValues = {
  target_unit_id: string
}

type MemberScope = 'direct' | 'subtree'

type OrganizationMemberRow = OrganizationUnitMember & {
  row_key: string
  unit_path: string
}

const EMPTY_UNITS: OrganizationUnit[] = []
const EMPTY_STAFF: OrganizationStaff[] = []
const EMPTY_MEMBERSHIPS: OrganizationUnitMember[] = []
const EMPTY_RELATIONS: StaffManagementRelation[] = []
const ORG_TREE_ROOT_ID = '__organization_tree_root__'
const RELATION_ORG_PREFIX = 'relation-org:'
const RELATION_STAFF_PREFIX = 'relation-staff:'
const INSTITUTION_ROOT_ID = '__institution__'
const UNASSIGNED_UNIT_ID = '__unassigned__'

function asText(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

function normalizeKeyword(value: string): string {
  return value.trim().toLowerCase()
}

function textIncludes(value: unknown, keyword: string): boolean {
  if (!keyword) return true
  return String(value ?? '').toLowerCase().includes(keyword)
}

function normalizePermissionRole(role: string | null | undefined): string {
  const normalized = String(role || '').trim()
  if (['staff', 'hospital_admin', 'system_admin', 'super_admin'].includes(normalized)) return normalized
  if (normalized === 'admin') return 'system_admin'
  if (normalized === 'manager') return 'hospital_admin'
  if (normalized === 'viewer' || normalized === 'consultant') return 'staff'
  return 'staff'
}

function permissionRoleLevel(role: string | null | undefined): number {
  const normalized = normalizePermissionRole(role)
  if (normalized === 'super_admin') return 100
  if (normalized === 'system_admin') return 90
  if (normalized === 'hospital_admin') return 30
  return 10
}

function isGlobalPermissionRole(role: string | null | undefined): boolean {
  return ['super_admin', 'system_admin'].includes(normalizePermissionRole(role))
}

function filterUnitsByKeyword(units: OrganizationUnit[], keyword: string): OrganizationUnit[] {
  if (!keyword) return units
  const unitMap = new Map(units.map((unit) => [unit.id, unit]))
  const includedIds = new Set<string>()
  units.forEach((unit) => {
    if (!textIncludes(unit.name, keyword) && !textIncludes(unit.path, keyword)) return
    let current: OrganizationUnit | undefined = unit
    while (current) {
      includedIds.add(current.id)
      current = current.parent_id ? unitMap.get(current.parent_id) : undefined
    }
  })
  return units.filter((unit) => includedIds.has(unit.id))
}

function buildTree(units: OrganizationUnit[], rootTitle: ReactNode): OrgTreeNode[] {
  const childrenMap = new Map<string | null, OrganizationUnit[]>()
  units.forEach((unit) => {
    const parentId = unit.parent_id ?? null
    childrenMap.set(parentId, [...(childrenMap.get(parentId) ?? []), unit])
  })

  const makeNode = (unit: OrganizationUnit): OrgTreeNode => ({
    key: unit.id,
    title: (
      <span className="organization-page__tree-title">
        <span>{unit.name}</span>
        <Tag>{unit.member_count} 人</Tag>
      </span>
    ),
    children: (childrenMap.get(unit.id) ?? []).map(makeNode),
  })

  return [
    {
      key: ORG_TREE_ROOT_ID,
      title: rootTitle,
      children: (childrenMap.get(null) ?? []).map(makeNode),
    },
  ]
}

function getDescendantIds(units: OrganizationUnit[], unitId: string): Set<string> {
  const childrenMap = new Map<string, string[]>()
  units.forEach((unit) => {
    if (!unit.parent_id) return
    childrenMap.set(unit.parent_id, [...(childrenMap.get(unit.parent_id) ?? []), unit.id])
  })
  const result = new Set<string>()
  const stack = [...(childrenMap.get(unitId) ?? [])]
  while (stack.length) {
    const current = stack.pop()
    if (!current || result.has(current)) continue
    result.add(current)
    stack.push(...(childrenMap.get(current) ?? []))
  }
  return result
}

function staffLabel(staff: OrganizationStaff): string {
  const code = asText(staff.external_account)
  const position = asText(staff.position_name)
  return [staff.name, code, position].filter(Boolean).join(' / ')
}

function relationOrgKey(unitId: string): string {
  return `${RELATION_ORG_PREFIX}${unitId}`
}

function relationStaffKey(unitId: string, staffId: string): string {
  return `${RELATION_STAFF_PREFIX}${unitId}:${staffId}`
}

function staffIdFromRelationStaffKey(key: string): string | null {
  if (!key.startsWith(RELATION_STAFF_PREFIX)) return null
  const parts = key.slice(RELATION_STAFF_PREFIX.length).split(':')
  return parts[1] || null
}

function unitIdFromRelationOrgKey(key: string): string | null {
  if (!key.startsWith(RELATION_ORG_PREFIX)) return null
  return key.slice(RELATION_ORG_PREFIX.length) || null
}

export function OrganizationPage() {
  const qc = useQueryClient()
  const [selectedHospitalCode, setSelectedHospitalCode] = useState<string | null>(null)
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null)
  const [unitModalOpen, setUnitModalOpen] = useState(false)
  const [editingUnit, setEditingUnit] = useState<OrganizationUnit | null>(null)
  const [memberModalOpen, setMemberModalOpen] = useState(false)
  const [moveMemberModalOpen, setMoveMemberModalOpen] = useState(false)
  const [memberScope, setMemberScope] = useState<MemberScope>('direct')
  const [memberKeyword, setMemberKeyword] = useState('')
  const [memberAddKeyword, setMemberAddKeyword] = useState('')
  const [memberAddSearchKeyword, setMemberAddSearchKeyword] = useState('')
  const [memberAddSelectedStaffIds, setMemberAddSelectedStaffIds] = useState<string[]>([])
  const [selectedMemberIds, setSelectedMemberIds] = useState<string[]>([])
  const [relationManagerId, setRelationManagerId] = useState<string | null>(null)
  const [relationManagerKeyword, setRelationManagerKeyword] = useState('')
  const [relationTreeCheckedKeys, setRelationTreeCheckedKeys] = useState<string[] | null>(null)
  const [organizationKeyword, setOrganizationKeyword] = useState('')
  const [unitForm] = Form.useForm<UnitFormValues>()
  const [memberForm] = Form.useForm<MemberFormValues>()
  const [moveMemberForm] = Form.useForm<MoveMemberFormValues>()

  const { data: currentUser } = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: authApi.getMe,
  })

  const { data: hospitalOptions = [] } = useQuery({
    queryKey: ['staff', 'hospital-options'],
    queryFn: () => adminApi.fetchStaffHospitalOptions(),
  })

  const { data: overview, isLoading } = useQuery({
    queryKey: ['organization-overview', selectedHospitalCode],
    queryFn: () => adminApi.fetchOrganizationOverview({ hospital_code: selectedHospitalCode }),
  })

  const units = overview?.units ?? EMPTY_UNITS
  const staffItems = overview?.staff ?? EMPTY_STAFF
  const memberships = overview?.memberships ?? EMPTY_MEMBERSHIPS
  const managementRelations = overview?.management_relations ?? EMPTY_RELATIONS
  const activeHospitalCode = selectedHospitalCode || overview?.hospital_code || hospitalOptions[0]?.hospital_code || null
  const institutionName = overview?.hospital_name || activeHospitalCode || '整个机构'
  const institutionMemberCount = staffItems.filter((staff) => staff.is_active).length
  const normalizedOrganizationKeyword = normalizeKeyword(organizationKeyword)
  const filteredTreeUnits = useMemo(
    () => filterUnitsByKeyword(units, normalizedOrganizationKeyword),
    [units, normalizedOrganizationKeyword],
  )
  const treeData = useMemo(
    () =>
      buildTree(
        filteredTreeUnits,
        <span className="organization-page__tree-title">
          <ApartmentOutlined />
          <span>{institutionName}</span>
          <Tag>{institutionMemberCount} 人</Tag>
        </span>,
      ),
    [filteredTreeUnits, institutionMemberCount, institutionName],
  )
  const unitById = useMemo(() => new Map(units.map((unit) => [unit.id, unit])), [units])
  const staffById = useMemo(() => new Map(staffItems.map((staff) => [staff.id, staff])), [staffItems])
  const currentUserRole = normalizePermissionRole(currentUser?.role)
  const currentUserRoleLevel = permissionRoleLevel(currentUserRole)
  const currentUserIsGlobal = isGlobalPermissionRole(currentUserRole)
  const canConfigureRelationManager = (staff: OrganizationStaff) => {
    if (!currentUser) return false
    if (currentUserRole === 'super_admin') return true
    if (currentUserIsGlobal) return permissionRoleLevel(staff.permission_role) <= currentUserRoleLevel
    if (currentUserRole === 'hospital_admin') {
      return staff.hospital_code === currentUser.hospital_code && permissionRoleLevel(staff.permission_role) <= currentUserRoleLevel
    }
    return Boolean(currentUser.staff_id && staff.id === currentUser.staff_id)
  }
  const canUseAsRelationTarget = (staff: OrganizationStaff) => {
    if (!currentUser) return false
    if (currentUserRole === 'super_admin') return true
    if (currentUserIsGlobal) return permissionRoleLevel(staff.permission_role) <= currentUserRoleLevel
    if (currentUser.staff_id && staff.id === currentUser.staff_id) return true
    return staff.hospital_code === currentUser.hospital_code && permissionRoleLevel(staff.permission_role) <= currentUserRoleLevel
  }
  const relationManagerStaffItems = staffItems.filter(canConfigureRelationManager)
  const relationScopeStaffItems = staffItems.filter(canUseAsRelationTarget)
  const relationScopeStaffIds = new Set(relationScopeStaffItems.map((staff) => staff.id))
  const relationScopeMemberships = memberships.filter((member) => relationScopeStaffIds.has(member.staff_id))
  const relationManagerStaffById = new Map(relationManagerStaffItems.map((staff) => [staff.id, staff]))
  const selectedUnit = selectedUnitId && selectedUnitId !== ORG_TREE_ROOT_ID ? units.find((unit) => unit.id === selectedUnitId) ?? null : null
  const selectedTreeKey = selectedUnit?.id ?? ORG_TREE_ROOT_ID
  const activeUnitId = selectedUnit?.id ?? null
  const selectedUnitMembers = memberships.filter((item) => item.unit_id === activeUnitId)
  const selectedUnitMemberIds = new Set(selectedUnitMembers.map((item) => item.staff_id))
  const descendantIds = selectedUnit ? getDescendantIds(overview?.units ?? [], selectedUnit.id) : new Set<string>()
  const selectedSubtreeUnitIds = selectedUnit ? new Set([selectedUnit.id, ...descendantIds]) : new Set<string>()
  const selectedUnitMemberRows: OrganizationMemberRow[] = selectedUnitMembers.map((item) => ({
    ...item,
    row_key: item.staff_id,
    unit_path: selectedUnit?.path ?? '-',
  }))
  const selectedSubtreeMemberRows: OrganizationMemberRow[] = memberships
    .filter((item) => selectedSubtreeUnitIds.has(item.unit_id))
    .map((item) => ({
      ...item,
      row_key: `${item.unit_id}:${item.staff_id}`,
      unit_path: unitById.get(item.unit_id)?.path ?? '-',
    }))
  const normalizedMemberKeyword = normalizeKeyword(memberKeyword)
  const currentMemberRows = memberScope === 'direct' ? selectedUnitMemberRows : selectedSubtreeMemberRows
  const selectedMemberRows = currentMemberRows.filter((row) => selectedMemberIds.includes(row.row_key))
  const selectedMemberCount = selectedMemberRows.length
  const selectedMemberSourceUnitIds = new Set(selectedMemberRows.map((row) => row.unit_id))
  const visibleMemberRows = currentMemberRows.filter(
    (item) =>
      textIncludes(item.staff_name, normalizedMemberKeyword) ||
      textIncludes(item.external_account, normalizedMemberKeyword) ||
      textIncludes(item.position_name, normalizedMemberKeyword) ||
      textIncludes(item.unit_path, normalizedMemberKeyword),
  )
  const activeRelationManagerId =
    relationManagerId && relationManagerStaffById.has(relationManagerId)
      ? relationManagerId
      : relationManagerStaffItems.find((staff) => staff.is_active)?.id ?? relationManagerStaffItems[0]?.id ?? null
  const activeRelationManager = activeRelationManagerId ? relationManagerStaffById.get(activeRelationManagerId) ?? null : null
  const activeManagerRelations = managementRelations.filter((item) => item.manager_staff_id === activeRelationManagerId)
  const activeManagedStaffIds = new Set(activeManagerRelations.map((item) => item.subordinate_staff_id))
  const relationSelfStaffKeys = (() => {
    if (!activeRelationManagerId) return []
    const keys = relationScopeMemberships
      .filter((member) => member.staff_id === activeRelationManagerId)
      .map((member) => relationStaffKey(member.unit_id, member.staff_id))
    if (!keys.length && staffById.has(activeRelationManagerId)) {
      keys.push(relationStaffKey(UNASSIGNED_UNIT_ID, activeRelationManagerId))
    }
    return Array.from(new Set(keys))
  })()

  const blockedParentUnitIds = editingUnit
    ? new Set([editingUnit.id, ...getDescendantIds(overview?.units ?? [], editingUnit.id)])
    : new Set<string>()
  const unitOptions = (overview?.units ?? [])
    .filter((unit) => !blockedParentUnitIds.has(unit.id))
    .map((unit) => ({ label: unit.path, value: unit.id }))

  const relationManagerStaffOptions = relationManagerStaffItems.map((staff) => ({
    label: staffLabel(staff),
    searchText: [staff.name, staff.external_account, staff.position_name].filter(Boolean).join(' '),
    value: staff.id,
    disabled: !staff.is_active,
  }))
  const normalizedRelationManagerKeyword = normalizeKeyword(relationManagerKeyword)
  const relationManagerOptions = relationManagerStaffOptions.filter(
    (option) =>
      textIncludes(option.label, normalizedRelationManagerKeyword) ||
      textIncludes(option.searchText, normalizedRelationManagerKeyword),
  )
  const activeRelationManagerOption = activeRelationManagerId
    ? relationManagerStaffOptions.find((option) => option.value === activeRelationManagerId)
    : undefined
  const visibleRelationManagerOptions =
    activeRelationManagerOption && !relationManagerOptions.some((option) => option.value === activeRelationManagerOption.value)
      ? [activeRelationManagerOption, ...relationManagerOptions]
      : relationManagerOptions
  const normalizedMemberAddSearchKeyword = normalizeKeyword(memberAddSearchKeyword)
  const addableStaffRows = (overview?.staff ?? [])
    .filter(
      (staff) =>
        !selectedUnitMemberIds.has(staff.id) &&
        (!normalizedMemberAddSearchKeyword ||
          textIncludes(staff.name, normalizedMemberAddSearchKeyword) ||
          textIncludes(staff.external_account, normalizedMemberAddSearchKeyword) ||
          textIncludes(staff.position_name, normalizedMemberAddSearchKeyword)),
    )
  const activeAddableStaffIds = addableStaffRows.filter((staff) => staff.is_active).map((staff) => staff.id)
  const moveTargetUnitOptions = (overview?.units ?? [])
    .filter((unit) => (selectedMemberSourceUnitIds.size ? !selectedMemberSourceUnitIds.has(unit.id) : unit.id !== activeUnitId))
    .map((unit) => ({
      label: unit.path,
      value: unit.id,
      disabled: !unit.is_active,
    }))
  const relationTreeData: RelationTreeNode[] = (() => {
    const childrenMap = new Map<string | null, OrganizationUnit[]>()
    const membersByUnit = new Map<string, OrganizationUnitMember[]>()
    const assignedStaffIds = new Set<string>()
    units.forEach((unit) => {
      const parentId = unit.parent_id ?? null
      childrenMap.set(parentId, [...(childrenMap.get(parentId) ?? []), unit])
    })
    relationScopeMemberships.forEach((member) => {
      membersByUnit.set(member.unit_id, [...(membersByUnit.get(member.unit_id) ?? []), member])
      assignedStaffIds.add(member.staff_id)
    })

    const makeStaffNode = (member: OrganizationUnitMember, unitId: string): RelationTreeNode => {
      const isManagerSelf = activeRelationManagerId === member.staff_id
      return {
        key: relationStaffKey(unitId, member.staff_id),
        disabled: isManagerSelf,
        title: (
          <span className="organization-page__relation-staff">
            <span>{member.staff_name}</span>
            {isManagerSelf ? <Tag color="blue">本人</Tag> : null}
            <span className="organization-page__muted">
              {[member.external_account, member.position_name].filter(Boolean).join(' / ') || (isManagerSelf ? '当前管理人' : '-')}
            </span>
          </span>
        ),
      }
    }

    const makeUnitNode = (unit: OrganizationUnit): RelationTreeNode => {
      const memberNodes = (membersByUnit.get(unit.id) ?? []).map((member) => makeStaffNode(member, unit.id))
      const childNodes = (childrenMap.get(unit.id) ?? []).map(makeUnitNode)
      return {
        key: relationOrgKey(unit.id),
        title: (
          <span className="organization-page__relation-unit-title">
            <ApartmentOutlined />
            <span>{unit.name}</span>
            <Tag>{unit.member_count} 人</Tag>
          </span>
        ),
        children: [...memberNodes, ...childNodes],
      }
    }

    const roots = (childrenMap.get(null) ?? []).map(makeUnitNode)
    const unassignedNodes = relationScopeStaffItems
      .filter((staff) => !assignedStaffIds.has(staff.id))
      .map((staff) => ({
        key: relationStaffKey(UNASSIGNED_UNIT_ID, staff.id),
        disabled: activeRelationManagerId === staff.id,
        title: (
          <span className="organization-page__relation-staff">
            <span>{staff.name}</span>
            {activeRelationManagerId === staff.id ? <Tag color="blue">本人</Tag> : null}
            <span className="organization-page__muted">
              {[staff.external_account, staff.position_name].filter(Boolean).join(' / ') || '-'}
            </span>
          </span>
        ),
      }))
    if (unassignedNodes.length) {
      roots.push({
        key: relationOrgKey(UNASSIGNED_UNIT_ID),
        title: (
          <span className="organization-page__relation-unit-title">
            <ApartmentOutlined />
            <span>未分配组织</span>
            <Tag>{unassignedNodes.length} 人</Tag>
          </span>
        ),
        children: unassignedNodes,
      })
    }
    return [
      {
        key: relationOrgKey(INSTITUTION_ROOT_ID),
        title: (
          <span className="organization-page__relation-unit-title">
            <ApartmentOutlined />
            <span>{overview?.hospital_name || activeHospitalCode || '整个机构'}</span>
            <Tag>{relationScopeStaffItems.filter((staff) => staff.is_active).length} 人</Tag>
          </span>
        ),
        children: roots,
      },
    ]
  })()

  const assignedRelationStaffIds = new Set(relationScopeMemberships.map((member) => member.staff_id))
  const relationInitialCheckedKeys = (() => {
    const keys = new Set<string>()
    relationScopeMemberships
      .filter((member) => activeManagedStaffIds.has(member.staff_id))
      .forEach((member) => keys.add(relationStaffKey(member.unit_id, member.staff_id)))
    relationScopeStaffItems.forEach((staff) => {
      if (!assignedRelationStaffIds.has(staff.id) && activeManagedStaffIds.has(staff.id)) {
        keys.add(relationStaffKey(UNASSIGNED_UNIT_ID, staff.id))
      }
    })
    relationSelfStaffKeys.forEach((key) => keys.add(key))
    return Array.from(keys)
  })()
  const checkedRelationTreeKeys = Array.from(new Set([...(relationTreeCheckedKeys ?? relationInitialCheckedKeys), ...relationSelfStaffKeys]))
  const checkedRelationStaffIds = (() => {
    const checkedKeySet = new Set(checkedRelationTreeKeys)
    const checkedUnitIds = new Set<string>()
    const staffIds = new Set<string>()
    checkedRelationTreeKeys.forEach((key) => {
      const staffId = staffIdFromRelationStaffKey(key)
      if (staffId) {
        staffIds.add(staffId)
        return
      }
      const unitId = unitIdFromRelationOrgKey(key)
      if (unitId) checkedUnitIds.add(unitId)
    })
    checkedUnitIds.forEach((unitId) => {
      if (unitId === INSTITUTION_ROOT_ID) {
        relationScopeStaffItems.forEach((staff) => staffIds.add(staff.id))
        return
      }
      if (unitId === UNASSIGNED_UNIT_ID) {
        relationScopeStaffItems.forEach((staff) => {
          if (!assignedRelationStaffIds.has(staff.id)) staffIds.add(staff.id)
        })
        return
      }
      const unitIds = new Set([unitId, ...getDescendantIds(units, unitId)])
      relationScopeMemberships.forEach((member) => {
        if (unitIds.has(member.unit_id)) staffIds.add(member.staff_id)
      })
    })
    checkedKeySet.forEach((key) => {
      const staffId = staffIdFromRelationStaffKey(key)
      if (staffId) staffIds.add(staffId)
    })
    if (activeRelationManagerId) staffIds.add(activeRelationManagerId)
    return Array.from(staffIds)
      .filter((staffId) => {
        const staff = staffById.get(staffId)
        return staff?.is_active && canUseAsRelationTarget(staff)
      })
      .sort()
  })()
  const savedRelationStaffIds = (() => {
    const staffIds = new Set(activeManagedStaffIds)
    if (activeRelationManagerId && staffById.get(activeRelationManagerId)?.is_active) staffIds.add(activeRelationManagerId)
    return Array.from(staffIds)
      .filter((staffId) => {
        const staff = staffById.get(staffId)
        return staff?.is_active && canUseAsRelationTarget(staff)
      })
      .sort()
  })()
  const relationTreeDirty = checkedRelationStaffIds.join('|') !== savedRelationStaffIds.join('|')

  const refreshOverview = () => qc.invalidateQueries({ queryKey: ['organization-overview'] })

  const clearRelationTreeDraft = () => {
    setRelationTreeCheckedKeys(null)
  }

  const clearMemberSelection = () => {
    setSelectedMemberIds([])
    moveMemberForm.resetFields()
  }

  const groupMemberRowsByUnit = (rows: OrganizationMemberRow[]) => {
    const grouped = new Map<string, string[]>()
    rows.forEach((row) => {
      grouped.set(row.unit_id, [...(grouped.get(row.unit_id) ?? []), row.staff_id])
    })
    return grouped
  }

  const createUnitMutation = useMutation({
    mutationFn: adminApi.createOrganizationUnit,
    onSuccess: () => {
      message.success('组织已创建')
      setUnitModalOpen(false)
      unitForm.resetFields()
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '组织创建失败')),
  })

  const updateUnitMutation = useMutation({
    mutationFn: ({ id, values }: { id: string; values: UnitFormValues }) =>
      adminApi.updateOrganizationUnit(id, values),
    onSuccess: () => {
      message.success('组织已更新')
      setUnitModalOpen(false)
      setEditingUnit(null)
      unitForm.resetFields()
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '组织更新失败')),
  })

  const deleteUnitMutation = useMutation({
    mutationFn: adminApi.deleteOrganizationUnit,
    onSuccess: () => {
      message.success('组织已删除')
      setSelectedUnitId(null)
      clearMemberSelection()
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '组织删除失败')),
  })

  const replaceMembersMutation = useMutation({
    mutationFn: ({ unitId, staffIds }: { unitId: string; staffIds: string[] }) =>
      adminApi.replaceOrganizationUnitMembers(unitId, staffIds),
    onSuccess: () => {
      message.success('组织成员已更新')
      setMemberModalOpen(false)
      memberForm.resetFields()
      setMemberAddKeyword('')
      setMemberAddSearchKeyword('')
      setMemberAddSelectedStaffIds([])
      setSelectedMemberIds([])
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '组织成员更新失败')),
  })

  const moveMembersMutation = useMutation({
    mutationFn: async ({ rows, targetUnitId }: { rows: OrganizationMemberRow[]; targetUnitId: string }) => {
      const groupedRows = groupMemberRowsByUnit(rows)
      const movedStaffIds = Array.from(new Set(rows.map((row) => row.staff_id)))
      const targetStaffIds = new Set(
        memberships.filter((item) => item.unit_id === targetUnitId).map((item) => item.staff_id),
      )
      movedStaffIds.forEach((staffId) => targetStaffIds.add(staffId))
      await Promise.all(
        [
          ...Array.from(groupedRows.entries()).map(([unitId, staffIds]) => {
            const staffIdSet = new Set(staffIds)
            const nextStaffIds = memberships
              .filter((item) => item.unit_id === unitId && !staffIdSet.has(item.staff_id))
              .map((item) => item.staff_id)
            return adminApi.replaceOrganizationUnitMembers(unitId, nextStaffIds)
          }),
          adminApi.replaceOrganizationUnitMembers(targetUnitId, Array.from(targetStaffIds)),
        ],
      )
      return rows.length
    },
    onSuccess: (count) => {
      message.success(`组织成员已移动：${count} 人`)
      setMoveMemberModalOpen(false)
      moveMemberForm.resetFields()
      setSelectedMemberIds([])
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '组织成员移动失败')),
  })

  const removeMembersMutation = useMutation({
    mutationFn: async (rows: OrganizationMemberRow[]) => {
      const groupedRows = groupMemberRowsByUnit(rows)
      await Promise.all(
        Array.from(groupedRows.entries()).map(([unitId, staffIds]) => {
          const staffIdSet = new Set(staffIds)
          const nextStaffIds = memberships
            .filter((item) => item.unit_id === unitId && !staffIdSet.has(item.staff_id))
            .map((item) => item.staff_id)
          return adminApi.replaceOrganizationUnitMembers(unitId, nextStaffIds)
        }),
      )
      return rows.length
    },
    onSuccess: (count) => {
      message.success(`组织成员已移出：${count} 人`)
      setSelectedMemberIds([])
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '组织成员移出失败')),
  })

  const syncRelationsMutation = useMutation({
    mutationFn: ({ managerStaffId, subordinateStaffIds }: { managerStaffId: string; subordinateStaffIds: string[] }) =>
      adminApi.syncStaffManagementRelations(managerStaffId, subordinateStaffIds),
    onSuccess: (items) => {
      message.success(`管理范围已保存：${items.length} 人`)
      setRelationTreeCheckedKeys(null)
      void refreshOverview()
    },
    onError: (error) => message.error(getApiErrorMessage(error, '管理范围保存失败')),
  })

  const openCreateUnitModal = (parentId?: string | null) => {
    setEditingUnit(null)
    unitForm.setFieldsValue({
      name: '',
      parent_id: parentId ?? null,
      sort_order: 0,
      is_active: true,
    })
    setUnitModalOpen(true)
  }

  const openEditUnitModal = (unit: OrganizationUnit) => {
    setEditingUnit(unit)
    unitForm.setFieldsValue({
      name: unit.name,
      parent_id: unit.parent_id,
      sort_order: unit.sort_order,
      is_active: unit.is_active,
    })
    setUnitModalOpen(true)
  }

  const openMemberModal = () => {
    if (!selectedUnit) return
    memberForm.setFieldsValue({
      staff_ids: [],
    })
    setMemberAddKeyword('')
    setMemberAddSearchKeyword('')
    setMemberAddSelectedStaffIds([])
    setMemberModalOpen(true)
  }

  const handleSaveUnit = async () => {
    const values = await unitForm.validateFields()
    const payload = {
      ...values,
      parent_id: values.parent_id || null,
      hospital_code: activeHospitalCode,
      sort_order: values.sort_order ?? 0,
      is_active: values.is_active ?? true,
    }
    if (editingUnit) {
      updateUnitMutation.mutate({ id: editingUnit.id, values: payload })
    } else {
      createUnitMutation.mutate(payload)
    }
  }

  const handleSaveMembers = async () => {
    if (!selectedUnit) return
    if (!memberAddSelectedStaffIds.length) {
      message.warning('请选择要新增的员工')
      return
    }
    const nextStaffIds = Array.from(
      new Set([...selectedUnitMembers.map((item) => item.staff_id), ...memberAddSelectedStaffIds]),
    )
    replaceMembersMutation.mutate({
      unitId: selectedUnit.id,
      staffIds: nextStaffIds,
    })
  }

  const handleRemoveMembers = () => {
    if (!selectedMemberRows.length) return
    removeMembersMutation.mutate(selectedMemberRows)
  }

  const handleRemoveMember = (member: OrganizationMemberRow) => {
    removeMembersMutation.mutate([member])
  }

  const openMoveMemberModal = () => {
    if (!selectedUnit || !selectedMemberCount) return
    moveMemberForm.resetFields()
    setMoveMemberModalOpen(true)
  }

  const handleMoveMembers = async () => {
    if (!selectedUnit || !selectedMemberCount) return
    const values = await moveMemberForm.validateFields()
    moveMembersMutation.mutate({
      rows: selectedMemberRows,
      targetUnitId: values.target_unit_id,
    })
  }

  const handleSaveRelationTree = () => {
    if (!activeRelationManagerId) return
    syncRelationsMutation.mutate({
      managerStaffId: activeRelationManagerId,
      subordinateStaffIds: checkedRelationStaffIds,
    })
  }

  const memberColumns: TableProps<OrganizationMemberRow>['columns'] = [
    {
      title: '员工',
      dataIndex: 'staff_name',
      key: 'staff_name',
      width: 170,
      render: (_value, row) => (
        <div className="organization-page__member-person">
          <span className="organization-page__member-name">{row.staff_name}</span>
          <span className="organization-page__member-code">{row.external_account || '-'}</span>
        </div>
      ),
    },
    {
      title: '岗位 / 组织',
      dataIndex: 'position_name',
      key: 'position_name',
      render: (_value, row) => (
        <div className="organization-page__member-meta">
          <span>{row.position_name || '-'}</span>
          <span>{row.unit_path || '-'}</span>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 64,
      align: 'center',
      render: (value) => (
        <Tag className="organization-page__member-status" color={value ? 'success' : 'default'}>
          {value ? '在职' : '停用'}
        </Tag>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 76,
      align: 'center',
      render: (_value, row) => (
        <Popconfirm
          title="移出组织成员"
          description={`确认将 ${row.staff_name} 移出 ${row.unit_path || selectedUnit?.name || '当前组织'}？`}
          onConfirm={() => handleRemoveMember(row)}
        >
          <Button
            danger
            type="text"
            size="small"
            icon={<DeleteOutlined />}
            loading={replaceMembersMutation.isPending || moveMembersMutation.isPending || removeMembersMutation.isPending}
          >
            移出
          </Button>
        </Popconfirm>
      ),
    },
  ]

  const memberRowSelection: TableProps<OrganizationMemberRow>['rowSelection'] = {
    selectedRowKeys: selectedMemberIds,
    onChange: (keys) => setSelectedMemberIds(keys.map((key) => String(key))),
    preserveSelectedRowKeys: true,
    getCheckboxProps: () => ({
      disabled: replaceMembersMutation.isPending || moveMembersMutation.isPending || removeMembersMutation.isPending,
    }),
  }

  const memberAddColumns: TableProps<OrganizationStaff>['columns'] = [
    {
      title: '员工',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      render: (_value, row) => (
        <div className="organization-page__member-person">
          <span className="organization-page__member-name">{row.name}</span>
          <span className="organization-page__member-code">{row.external_account || '-'}</span>
        </div>
      ),
    },
    {
      title: '岗位',
      dataIndex: 'position_name',
      key: 'position_name',
      render: (value) => <span className="organization-page__member-add-meta">{value || '-'}</span>,
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 72,
      align: 'center',
      render: (value) => (
        <Tag className="organization-page__member-status" color={value ? 'success' : 'default'}>
          {value ? '在职' : '停用'}
        </Tag>
      ),
    },
  ]

  const memberAddRowSelection: TableProps<OrganizationStaff>['rowSelection'] = {
    selectedRowKeys: memberAddSelectedStaffIds,
    onChange: (keys) => setMemberAddSelectedStaffIds(keys.map((key) => String(key))),
    preserveSelectedRowKeys: true,
    getCheckboxProps: (row) => ({
      disabled: !row.is_active,
    }),
  }

  return (
    <div className="operation-page organization-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>组织架构</h1>
            <p>配置机构内多层组织、员工归属和人员管理关系。管理关系会用于员工可见数据范围。</p>
          </div>
        </div>
      </div>

      <div className="operation-card organization-page__toolbar-card">
        <div className="operation-toolbar">
          <Space wrap>
            <Select
              className="operation-page__status-select"
              showSearch
              placeholder="请选择机构"
              optionFilterProp="label"
              value={activeHospitalCode ?? undefined}
              options={hospitalOptions.map((item) => ({
                label: `${item.hospital_name}（${item.hospital_code}）`,
                value: item.hospital_code,
              }))}
              onChange={(value) => {
                setSelectedHospitalCode(value)
                setSelectedUnitId(null)
                clearMemberSelection()
                setRelationManagerId(null)
                setRelationManagerKeyword('')
                clearRelationTreeDraft()
              }}
            />
            <Tag color="blue">{overview?.hospital_name || activeHospitalCode || '未选择机构'}</Tag>
          </Space>
          <Space wrap>
            <Button type="primary" icon={<PlusOutlined />} disabled={!activeHospitalCode} onClick={() => openCreateUnitModal(null)}>
              新增一级组织
            </Button>
          </Space>
        </div>
      </div>

      <Alert
        showIcon
        type="info"
        message="组织层级和管理关系分开维护"
        description="一个员工可以属于多个组织；管理关系用于明确某个员工可以管理哪些员工，系统会据此扩展该员工可见的数据范围。"
      />

      <div className="organization-page__layout">
        <section className="operation-card organization-page__tree-panel">
          <div className="organization-page__section-head">
            <div>
              <Typography.Title level={4}>组织树</Typography.Title>
              <span className="organization-page__muted">支持多层级组织</span>
            </div>
          </div>
          <Input.Search
            allowClear
            placeholder="搜索组织"
            value={organizationKeyword}
            onChange={(event) => setOrganizationKeyword(event.target.value)}
            style={{ marginBottom: 12 }}
          />
          {treeData.length ? (
            <Tree
              key={normalizedOrganizationKeyword || 'all'}
              blockNode
              showLine
              defaultExpandAll
              selectedKeys={[selectedTreeKey]}
              treeData={treeData}
              onSelect={(keys) => {
                const nextKey = asText(keys[0])
                setSelectedUnitId(nextKey && nextKey !== ORG_TREE_ROOT_ID ? nextKey : null)
                clearMemberSelection()
              }}
            />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无组织">
              <Button type="primary" icon={<PlusOutlined />} disabled={!activeHospitalCode} onClick={() => openCreateUnitModal(null)}>
                新增一级组织
              </Button>
            </Empty>
          )}
        </section>

        <section className="operation-card organization-page__detail-panel">
          {selectedUnit ? (
            <>
              <div className="organization-page__section-head">
                <div>
                  <Typography.Title level={4}>{selectedUnit.name}</Typography.Title>
                  <span className="organization-page__muted">{selectedUnit.path}</span>
                </div>
                <Tag color={selectedUnit.is_active ? 'success' : 'default'}>{selectedUnit.is_active ? '启用' : '停用'}</Tag>
              </div>

              <div className="organization-page__metric-grid">
                <div className="organization-page__metric">
                  <span>直属成员</span>
                  <strong>{selectedUnitMembers.length}</strong>
                </div>
                <div className="organization-page__metric">
                  <span>含下级成员</span>
                  <strong>{selectedUnit.member_count}</strong>
                </div>
                <div className="organization-page__metric">
                  <span>下级组织</span>
                  <strong>{descendantIds.size}</strong>
                </div>
                <div className="organization-page__metric">
                  <span>同级排序</span>
                  <strong>{selectedUnit.sort_order}</strong>
                </div>
              </div>

              <div className="operation-toolbar organization-page__detail-actions">
                <Space wrap size={6}>
                  <Button size="small" icon={<PlusOutlined />} onClick={() => openCreateUnitModal(selectedUnit.id)}>
                    新增下级
                  </Button>
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEditUnitModal(selectedUnit)}>
                    编辑组织
                  </Button>
                  <Button size="small" icon={<TeamOutlined />} onClick={openMemberModal}>
                    新增成员
                  </Button>
                </Space>
                <Space wrap size={6}>
                  <Button
                    size="small"
                    icon={<SwapOutlined />}
                    disabled={!selectedMemberCount || moveTargetUnitOptions.length === 0}
                    loading={moveMembersMutation.isPending}
                    onClick={openMoveMemberModal}
                  >
                    移动成员{selectedMemberCount ? `（${selectedMemberCount}）` : ''}
                  </Button>
                  <Popconfirm
                    title="批量移出组织成员"
                    description={`确认将已选择的 ${selectedMemberCount} 名成员从各自所在组织移出？`}
                    disabled={!selectedMemberCount}
                    onConfirm={handleRemoveMembers}
                  >
                    <Button
                      danger
                      size="small"
                      icon={<DeleteOutlined />}
                      disabled={!selectedMemberCount}
                      loading={removeMembersMutation.isPending}
                    >
                      批量移出{selectedMemberCount ? `（${selectedMemberCount}）` : ''}
                    </Button>
                  </Popconfirm>
                  <Popconfirm
                    title="删除组织"
                    description="删除前需要先移出成员并处理下级组织。"
                    onConfirm={() => deleteUnitMutation.mutate(selectedUnit.id)}
                  >
                    <Button danger size="small" icon={<DeleteOutlined />}>
                      删除组织
                    </Button>
                  </Popconfirm>
                </Space>
              </div>

              <div className="operation-toolbar organization-page__table-tools">
                <Segmented
                  size="small"
                  value={memberScope}
                  options={[
                    { label: '直属成员', value: 'direct' },
                    { label: '含下级成员', value: 'subtree' },
                  ]}
                  onChange={(value) => {
                    setMemberScope(value as MemberScope)
                    clearMemberSelection()
                  }}
                />
                <Input.Search
                  allowClear
                  size="small"
                  placeholder="搜索成员 / 工号 / 岗位 / 组织"
                  value={memberKeyword}
                  onChange={(event) => setMemberKeyword(event.target.value)}
                  style={{ maxWidth: 260 }}
                />
              </div>

              <Table
                className="organization-page__member-table"
                rowKey="row_key"
                dataSource={visibleMemberRows}
                columns={memberColumns}
                rowSelection={memberRowSelection}
                loading={isLoading}
                size="small"
                tableLayout="fixed"
                scroll={{ y: 320 }}
                pagination={
                  visibleMemberRows.length > 30
                    ? { pageSize: 30, showSizeChanger: true, showLessItems: true, size: 'small', showTotal: (total) => `共 ${total} 人` }
                    : false
                }
              />
            </>
          ) : (
            <div className="organization-page__institution-panel">
              <div className="organization-page__section-head">
                <div>
                  <Typography.Title level={4}>{institutionName}</Typography.Title>
                  <span className="organization-page__muted">机构根组织</span>
                </div>
                <Button type="primary" icon={<PlusOutlined />} disabled={!activeHospitalCode} onClick={() => openCreateUnitModal(null)}>
                  新增一级组织
                </Button>
              </div>
              <div className="organization-page__metric-grid">
                <div className="organization-page__metric">
                  <span>一级组织</span>
                  <strong>{units.filter((unit) => !unit.parent_id).length}</strong>
                </div>
                <div className="organization-page__metric">
                  <span>全部组织</span>
                  <strong>{units.length}</strong>
                </div>
                <div className="organization-page__metric">
                  <span>在职员工</span>
                  <strong>{institutionMemberCount}</strong>
                </div>
                <div className="organization-page__metric">
                  <span>管理关系</span>
                  <strong>{managementRelations.length}</strong>
                </div>
              </div>
              <Alert
                showIcon
                type="info"
                message="机构根组织用于承载整个机构"
                description="在左侧选择具体下级组织后，可以维护组织成员、移动成员和配置该组织对应的管理关系。"
              />
            </div>
          )}
        </section>
      </div>

      <section className="operation-card">
        <div className="organization-page__section-head">
          <div>
            <Typography.Title level={4}>人员管理关系</Typography.Title>
            <span className="organization-page__muted">左侧选择一个管理人，右侧勾选他可以管理的组织或人员</span>
          </div>
          <Space wrap>
            <Tag color={relationTreeDirty ? 'orange' : 'blue'}>{relationTreeDirty ? '有未保存修改' : '已同步'}</Tag>
            <Button disabled={!relationTreeDirty || !activeRelationManagerId} onClick={clearRelationTreeDraft}>
              还原
            </Button>
            <Button
              type="primary"
              icon={<UserSwitchOutlined />}
              disabled={!activeRelationManagerId || !relationTreeDirty}
              loading={syncRelationsMutation.isPending}
              onClick={handleSaveRelationTree}
            >
              保存管理范围
            </Button>
          </Space>
        </div>
        <div className="organization-page__relation-editor">
          <aside className="organization-page__relation-manager">
            <Typography.Text strong>管理人</Typography.Text>
            <Input.Search
              allowClear
              placeholder="筛选姓名 / 工号 / 岗位"
              value={relationManagerKeyword}
              onChange={(event) => setRelationManagerKeyword(event.target.value)}
            />
            <Select
              showSearch
              optionFilterProp="searchText"
              placeholder="可按姓名或工号搜索管理人"
              value={activeRelationManagerId ?? undefined}
              options={visibleRelationManagerOptions}
              onChange={(value) => {
                setRelationManagerId(value)
                clearRelationTreeDraft()
              }}
            />
            <div className="organization-page__relation-manager-summary">
              <span className="organization-page__muted">当前管理人</span>
              <strong>{activeRelationManager?.name ?? '-'}</strong>
              <span>{activeRelationManager ? staffLabel(activeRelationManager) || '-' : '-'}</span>
            </div>
          </aside>
          <div className="organization-page__relation-scope">
            <div className="organization-page__relation-scope-head">
              <div>
                <Typography.Text strong>可管理范围</Typography.Text>
                <div className="organization-page__muted">
                  已勾选 {checkedRelationStaffIds.length} 人，当前已保存 {savedRelationStaffIds.length} 人
                </div>
              </div>
              <Space wrap>
                <Button size="small" onClick={() => setRelationTreeCheckedKeys(relationSelfStaffKeys)} disabled={!activeRelationManagerId}>
                  清空
                </Button>
                <Button size="small" onClick={clearRelationTreeDraft} disabled={!relationTreeDirty}>
                  恢复已保存
                </Button>
              </Space>
            </div>
            {relationTreeData.length ? (
              <Tree
                key={activeRelationManagerId ?? 'relation-tree'}
                checkable
                blockNode
                showLine
                defaultExpandAll
                selectable={false}
                checkedKeys={checkedRelationTreeKeys}
                treeData={relationTreeData}
                onCheck={(checked) => {
                  const keys = Array.isArray(checked) ? checked : checked.checked
                  setRelationTreeCheckedKeys(Array.from(new Set([...keys.map((key) => String(key)), ...relationSelfStaffKeys])))
                }}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无组织或人员" />
            )}
          </div>
        </div>
      </section>

      <Modal
        title={editingUnit ? '编辑组织' : '新增组织'}
        open={unitModalOpen}
        onOk={() => void handleSaveUnit()}
        onCancel={() => {
          setUnitModalOpen(false)
          setEditingUnit(null)
          unitForm.resetFields()
        }}
        confirmLoading={createUnitMutation.isPending || updateUnitMutation.isPending}
        destroyOnClose
      >
        <Form form={unitForm} layout="vertical">
          <Form.Item name="name" label="组织名称" rules={[{ required: true, whitespace: true, message: '请输入组织名称' }]}>
            <Input prefix={<ApartmentOutlined />} placeholder="例如 咨询中心 / 现场咨询一组" />
          </Form.Item>
          <Form.Item name="parent_id" label="上级组织">
            <Select allowClear showSearch optionFilterProp="label" placeholder="不选择则为根组织" options={unitOptions} />
          </Form.Item>
          <Form.Item name="sort_order" label="同级排序" extra="用于控制同一上级组织下的显示顺序，数字越小越靠前。">
            <InputNumber min={0} precision={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="is_active" label="状态">
            <Select
              options={[
                { label: '启用', value: true },
                { label: '停用', value: false },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={selectedUnit ? `新增成员：${selectedUnit.name}` : '新增成员'}
        open={memberModalOpen}
        onOk={() => void handleSaveMembers()}
        onCancel={() => {
          setMemberModalOpen(false)
          memberForm.resetFields()
          setMemberAddKeyword('')
          setMemberAddSearchKeyword('')
          setMemberAddSelectedStaffIds([])
        }}
        confirmLoading={replaceMembersMutation.isPending}
        destroyOnClose
        width={760}
      >
        <Form form={memberForm} layout="vertical">
          <Form.Item label="搜索员工">
            <Input.Search
              allowClear
              enterButton="搜索"
              placeholder="输入姓名 / 工号 / 岗位后点击搜索"
              value={memberAddKeyword}
              onChange={(event) => {
                const nextKeyword = event.target.value
                setMemberAddKeyword(nextKeyword)
                if (!nextKeyword.trim()) setMemberAddSearchKeyword('')
              }}
              onSearch={(value) => setMemberAddSearchKeyword(value)}
            />
          </Form.Item>
          <div className="organization-page__member-add-tools">
            <Typography.Text type="secondary">
              当前结果 {addableStaffRows.length} 人，已选择 {memberAddSelectedStaffIds.length} 人
            </Typography.Text>
            <Space wrap size={6}>
              <Button
                size="small"
                disabled={!activeAddableStaffIds.length}
                onClick={() =>
                  setMemberAddSelectedStaffIds(Array.from(new Set([...memberAddSelectedStaffIds, ...activeAddableStaffIds])))
                }
              >
                全选当前结果
              </Button>
              <Button
                size="small"
                disabled={!memberAddSelectedStaffIds.length || !addableStaffRows.length}
                onClick={() => {
                  const visibleStaffIds = new Set(addableStaffRows.map((staff) => staff.id))
                  setMemberAddSelectedStaffIds((prev) => prev.filter((staffId) => !visibleStaffIds.has(staffId)))
                }}
              >
                取消当前结果
              </Button>
            </Space>
          </div>
          <Table<OrganizationStaff>
            className="organization-page__member-add-table"
            rowKey="id"
            dataSource={addableStaffRows}
            columns={memberAddColumns}
            rowSelection={memberAddRowSelection}
            size="small"
            tableLayout="fixed"
            scroll={{ y: 280 }}
            pagination={
              addableStaffRows.length > 20
                ? { pageSize: 20, showSizeChanger: false, showLessItems: true, size: 'small', showTotal: (total) => `共 ${total} 人` }
                : false
            }
            locale={{
              emptyText: memberAddSearchKeyword ? '没有匹配的可新增员工' : '暂无可新增员工',
            }}
          />
        </Form>
      </Modal>

      <Modal
        title={selectedUnit ? `移动成员：${selectedUnit.name}` : '移动成员'}
        open={moveMemberModalOpen}
        onOk={() => void handleMoveMembers()}
        onCancel={() => {
          setMoveMemberModalOpen(false)
          moveMemberForm.resetFields()
        }}
        confirmLoading={moveMembersMutation.isPending}
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Typography.Text type="secondary">已选择 {selectedMemberCount} 名成员</Typography.Text>
          <Form form={moveMemberForm} layout="vertical">
            <Form.Item name="target_unit_id" label="目标组织" rules={[{ required: true, message: '请选择目标组织' }]}>
              <Select
                showSearch
                optionFilterProp="label"
                placeholder="请选择要移动到的组织"
                options={moveTargetUnitOptions}
              />
            </Form.Item>
          </Form>
        </Space>
      </Modal>
    </div>
  )
}

export default OrganizationPage
