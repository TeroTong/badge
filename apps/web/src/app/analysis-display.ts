export const CUSTOMER_CHARACTERISTIC_LABELS: Record<string, string> = {
  gender: '性别',
  age: '年龄',
  occupation: '职业/人群',
  core_needs: '核心需求',
  needs_keywords: '需求关键词',
}

export function buildCustomerCharacteristics(
  characteristics: Record<string, unknown> | undefined,
): Array<[string, unknown]> {
  const orderedKeys = ['gender', 'age', 'occupation', 'core_needs', 'needs_keywords']
  return Object.entries(characteristics ?? {}).sort(([leftKey], [rightKey]) => {
    const leftIndex = orderedKeys.indexOf(leftKey)
    const rightIndex = orderedKeys.indexOf(rightKey)
    if (leftIndex === -1 && rightIndex === -1) return leftKey.localeCompare(rightKey)
    if (leftIndex === -1) return 1
    if (rightIndex === -1) return -1
    return leftIndex - rightIndex
  })
}
