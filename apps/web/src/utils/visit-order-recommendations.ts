import type { VisitOrderMatchCandidate } from '@/api/recordings'

const MAX_VISIBLE_RECOMMENDATIONS = 3
const WEAK_TOP1_CONFIDENCE = 0.45
const MIN_FALLBACK_CONFIDENCE = 0.35
const PRIMARY_THRESHOLD_FLOOR = 0.48
const PRIMARY_THRESHOLD_GAP = 0.14
const CLEAR_TOP1_GAP = 0.18
const CLEAR_TOP2_GAP = 0.12

export function getQuickRecommendSelection(candidates: VisitOrderMatchCandidate[]) {
  if (!candidates.length) {
    return {
      items: [] as VisitOrderMatchCandidate[],
      hiddenCount: 0,
    }
  }

  const sorted = [...candidates].sort((a, b) => b.confidence - a.confidence)
  const top1 = sorted[0]
  const top2 = sorted[1]
  const top3 = sorted[2]

  let items: VisitOrderMatchCandidate[]

  if (top1.confidence < WEAK_TOP1_CONFIDENCE) {
    items = sorted.slice(0, MAX_VISIBLE_RECOMMENDATIONS)
  } else if (
    top2
    && top1.confidence >= PRIMARY_THRESHOLD_FLOOR
    && top1.confidence - top2.confidence >= CLEAR_TOP1_GAP
  ) {
    items = [top1]
  } else if (
    top2
    && (
      !top3
      || (
        top2.confidence >= PRIMARY_THRESHOLD_FLOOR
        && top2.confidence - top3.confidence >= CLEAR_TOP2_GAP
      )
    )
  ) {
    items = sorted.slice(0, Math.min(2, sorted.length))
  } else {
    const primaryThreshold = Math.max(top1.confidence - PRIMARY_THRESHOLD_GAP, PRIMARY_THRESHOLD_FLOOR)
    const preferred = sorted.filter(
      (candidate) =>
        candidate.decision === 'auto'
        || candidate.decision === 'recommend'
        || candidate.confidence >= primaryThreshold,
    )

    if (preferred.length > 0) {
      items = preferred.slice(0, MAX_VISIBLE_RECOMMENDATIONS)
    } else {
      const fallback = sorted.filter((candidate) => candidate.confidence >= MIN_FALLBACK_CONFIDENCE)
      items = (fallback.length > 0 ? fallback : sorted).slice(0, MAX_VISIBLE_RECOMMENDATIONS)
    }
  }

  return {
    items,
    hiddenCount: Math.max(sorted.length - items.length, 0),
  }
}
