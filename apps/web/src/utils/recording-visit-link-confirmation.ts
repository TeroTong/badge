function uniqueNonEmpty(values: Array<string | null | undefined>) {
  return Array.from(new Set(values.map((value) => String(value ?? '').trim()).filter(Boolean)))
}

export type RecordingVisitLinkRiskInput = {
  nextLinkedVisitIds?: Array<string | null | undefined>
  currentRecordingOtherVisitLabel?: string | null
  targetLinkedRecordingNames?: Array<string | null | undefined>
  targetLinkedRecordingCount?: number | null
}

export function buildRecordingVisitLinkRiskLines({
  nextLinkedVisitIds = [],
  currentRecordingOtherVisitLabel,
  targetLinkedRecordingNames = [],
  targetLinkedRecordingCount,
}: RecordingVisitLinkRiskInput) {
  const lines: string[] = []
  const linkedVisitCount = uniqueNonEmpty(nextLinkedVisitIds).length
  const recordingNames = uniqueNonEmpty(targetLinkedRecordingNames)
  const recordingCount = Math.max(Number(targetLinkedRecordingCount ?? 0), recordingNames.length)

  if (linkedVisitCount > 1) {
    lines.push(`当前录音将同时关联 ${linkedVisitCount} 张到诊单。请确认这是同行客户、连续接待，或一段录音覆盖多张到诊单的情况。`)
  } else if (currentRecordingOtherVisitLabel) {
    lines.push(`当前录音已关联 ${currentRecordingOtherVisitLabel}，继续后会形成一条录音关联多张到诊单。`)
  }

  if (recordingCount > 0) {
    const recordingSummary = recordingNames.length > 0
      ? `：${recordingNames.join('、')}`
      : ''
    lines.push(`目标到诊单已关联 ${recordingCount} 条录音${recordingSummary}。继续后该到诊单会关联多条录音。`)
  }

  return lines
}

export function buildRecordingVisitLinkRiskText(input: RecordingVisitLinkRiskInput) {
  const lines = buildRecordingVisitLinkRiskLines(input)
  if (!lines.length) return ''
  return `${lines.join('\n')}\n\n请确认是否继续关联。`
}
