export type VisitOrderLineItemLike = {
  fzdh?: string | null
  dzseg?: string | null
  triage_staff_code?: string | null
  triage_staff_name?: string | null
  triage_time?: string | null
  consult_time?: string | null
  triage_status_text?: string | null
  deal_status_text?: string | null
  consult_project?: string | null
  note_summary?: string | null
}

export function formatVisitOrderClock(value: string | null | undefined) {
  if (!value) return null
  const digits = value.replace(/[^0-9]/g, '')
  if (digits.length < 4) return value
  const padded = digits.padStart(6, '0')
  return `${padded.slice(0, 2)}:${padded.slice(2, 4)}${padded.length >= 6 ? `:${padded.slice(4, 6)}` : ''}`
}

export function formatVisitOrderLineItemRef(item: VisitOrderLineItemLike) {
  if (item.fzdh) return item.fzdh
  if (item.dzseg) return `行项目 ${item.dzseg}`
  return '未编号分诊'
}

export function formatMergedVisitOrderTitle(
  dzdh: string | null | undefined,
  dzseg: string | null | undefined,
  mergedItemCount: number,
) {
  const normalizedDzdh = String(dzdh || '').trim()
  const normalizedDzseg = String(dzseg || '').trim()
  if (!normalizedDzdh) return '-'
  if (mergedItemCount > 1) return normalizedDzdh
  return normalizedDzseg ? `${normalizedDzdh}-${normalizedDzseg}` : normalizedDzdh
}

export function buildVisitOrderLineItemMeta(item: VisitOrderLineItemLike) {
  const lines: string[] = []
  const staffName = String(item.triage_staff_name || '').trim()
  const staffCode = String(item.triage_staff_code || '').trim()
  const triageTime = formatVisitOrderClock(item.triage_time)
  const consultTime = formatVisitOrderClock(item.consult_time)
  const triageStatus = String(item.triage_status_text || '').trim()
  const dealStatus = String(item.deal_status_text || '').trim()
  const consultProject = String(item.consult_project || '').trim()

  if (staffName || staffCode) {
    lines.push(`分诊人 ${staffName || staffCode}${staffName && staffCode ? ` / ${staffCode}` : ''}`)
  }
  if (triageTime || consultTime) {
    lines.push(
      [
        triageTime ? `分诊 ${triageTime}` : null,
        consultTime ? `接诊 ${consultTime}` : null,
      ].filter(Boolean).join(' / '),
    )
  }
  if (triageStatus || dealStatus) {
    lines.push(
      [
        triageStatus ? `分诊状态 ${triageStatus}` : null,
        dealStatus ? `业务状态 ${dealStatus}` : null,
      ].filter(Boolean).join(' / '),
    )
  }
  if (consultProject) {
    lines.push(`咨询项目 ${consultProject}`)
  }

  return lines
}
