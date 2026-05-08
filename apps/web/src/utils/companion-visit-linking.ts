export function buildLinkedVisitIds(primaryVisitId: string, extraVisitIds: Array<string | null | undefined> = []) {
  const seen = new Set<string>()
  const items: string[] = []
  for (const value of [primaryVisitId, ...extraVisitIds]) {
    const normalized = String(value || '').trim()
    if (!normalized || seen.has(normalized)) continue
    seen.add(normalized)
    items.push(normalized)
  }
  return items
}

export function hasCompanionVisitOptions(companionVisitIds: Array<string | null | undefined> = []) {
  return companionVisitIds.some((item) => Boolean(String(item || '').trim()))
}

export function buildCompanionVisitPromptMessage(
  companionVisitOrderRefs: string[] = [],
  companionCustomerCodes: string[] = [],
) {
  const refs = companionVisitOrderRefs.filter((item) => item.trim())
  const customerCodes = companionCustomerCodes.filter((item) => item.trim())
  const targetLabel = refs.length
    ? `同行到诊单：${refs.join(' / ')}`
    : customerCodes.length
      ? `同行客户编码：${customerCodes.join(' / ')}`
      : '检测到当前到诊存在同行辅单'
  return `${targetLabel}。\n是否在关联当前到诊单的同时，也一并关联这些同行到诊单？`
}
