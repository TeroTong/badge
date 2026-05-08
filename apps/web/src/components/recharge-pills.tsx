import { formatRechargeAmount, hasPositiveRechargeAmount } from '@/utils/recharge-amount'

type RechargePillsProps = {
  principal: number | null | undefined
  bonus: number | null | undefined
  compact?: boolean
  className?: string
}

function pillClassName(value: number | null | undefined) {
  return `recharge-pill${hasPositiveRechargeAmount(value) ? ' recharge-pill--active' : ''}`
}

export function RechargePills({
  principal,
  bonus,
  compact = false,
  className,
}: RechargePillsProps) {
  const rootClassName = [
    'recharge-pills',
    compact ? 'recharge-pills--compact' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={rootClassName}>
      <div className={pillClassName(principal)}>
        <span className="recharge-pill__label">本金</span>
        <strong className="recharge-pill__value">{formatRechargeAmount(principal)}</strong>
      </div>
      <div className={pillClassName(bonus)}>
        <span className="recharge-pill__label">赠金</span>
        <strong className="recharge-pill__value">{formatRechargeAmount(bonus)}</strong>
      </div>
    </div>
  )
}
