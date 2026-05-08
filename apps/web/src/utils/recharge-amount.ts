const ZERO_EPSILON = 0.000001

export function formatRechargeAmount(value: number | null | undefined) {
  if (value == null) return '-'
  if (Math.abs(value) < ZERO_EPSILON) return '0'
  return `¥${value.toFixed(2)}`
}

export function hasPositiveRechargeAmount(value: number | null | undefined) {
  return value != null && value > ZERO_EPSILON
}
