type MatchEvidenceLike = {
  type?: string | null
  label?: string | null
  detail?: string | null
}

type MatchCandidateLike = {
  reasons?: string[] | null
  evidence?: MatchEvidenceLike[] | null
}

type DisplayLine = {
  key: string
  text: string
  priority: number
  order: number
}

function normalizeText(value: string | null | undefined) {
  return String(value || '').trim()
}

function normalizeKey(value: string | null | undefined) {
  return normalizeText(value).replace(/\s+/g, '').toLowerCase()
}

function inferTimeAnchorKey(text: string) {
  if (text.includes('分诊')) return 'time:triage'
  if (text.includes('接诊')) return 'time:consult'
  return 'time'
}

function classifyEvidence(item: MatchEvidenceLike) {
  const type = normalizeKey(item.type)
  const label = normalizeKey(item.label)
  const detail = normalizeKey(item.detail)

  if (type === 'shortlist') {
    if (label.includes('到诊单号')) return 'visit_order_no'
    if (label.includes('客户编码')) return 'customer_code'
    if (label.includes('录音者编码')) return 'role_code'
    if (label.includes('分诊') || label.includes('接诊') || label.includes('时间')) {
      return inferTimeAnchorKey(`${label}${detail}`)
    }
  }

  if (type === 'time') {
    return inferTimeAnchorKey(`${label}${detail}`)
  }

  if (type === 'customer_name') {
    if (label.includes('姓名')) return 'customer_name:full'
    if (label.includes('近音')) return 'customer_name:phonetic'
    return 'customer_name:address'
  }

  if (type === 'staff_identity') {
    return label.includes('姓名') ? 'staff_identity:name' : 'staff_identity:surname'
  }

  if (type === 'demographics') {
    if (label.includes('出生日期') || label.includes('年龄')) return 'demographics:age'
    if (label.includes('性别')) return 'demographics:gender'
    if (label.includes('男性客户特征')) return 'demographics:gender_male'
    return 'demographics'
  }

  if (
    type === 'visit_order_no' ||
    type === 'customer_code' ||
    type === 'advisor' ||
    type === 'role_code' ||
    type === 'role_code_mismatch' ||
    type === 'role_name' ||
    type === 'project' ||
    type === 'procedure_plan' ||
    type === 'structured_demand' ||
    type === 'project_conflict' ||
    type === 'stage' ||
    type === 'stage_conflict' ||
    type === 'doctor' ||
    type === 'advisor_name' ||
    type === 'advyq' ||
    type === 'advyq_name' ||
    type === 'department' ||
    type === 'duration' ||
    type === 'role_time'
  ) {
    return type
  }

  return `${type || 'evidence'}:${label || detail || 'default'}`
}

function classifyReason(reason: string) {
  const text = normalizeKey(reason)
  if (!text) return null

  if (text.includes('payload中的到诊单号')) return 'visit_order_no'
  if (text.includes('payload中的客户编码') || text.includes('payload的客户编码') || text.includes('客户编码一致')) {
    return 'customer_code'
  }
  if (text.includes('录音payload顾问与到诊单顾问一致')) return 'advisor'
  if (text.includes('录音员工角色与到诊单中的对应岗位编码一致') || text.includes('录音者编码与到诊单中的对应岗位编码一致')) {
    return 'role_code'
  }
  if (text.includes('角色编码不一致') || text.includes('录音员工编码与到诊单')) return 'role_code_mismatch'
  if (text.includes('咨询师自报姓名')) return 'staff_identity:name'
  if (text.includes('咨询师自报姓氏')) return 'staff_identity:surname'
  if (text.includes('客户称呼') || text.includes('匹配的称呼')) return text.includes('近音') ? 'customer_name:phonetic' : 'customer_name:address'
  if (text.includes('客户姓名')) return 'customer_name:full'
  if (text.includes('分诊时间候选过滤') || text.includes('接诊时间候选过滤')) return inferTimeAnchorKey(text)
  if (text.includes('录音创建时间') || text.includes('录音开始时间') || text.includes('录音时间')) return inferTimeAnchorKey(text)
  if (text.includes('顾问接待窗口') || text.includes('预期接待时间窗口')) return 'role_time'
  if (text.includes('咨询项目/备注存在关键词重合') || text.includes('咨询项目/关键词匹配')) return 'project'
  if (text.includes('具体术式组合')) return 'procedure_plan'
  if (text.includes('结构化诉求')) return 'structured_demand'
  if (text.includes('咨询主题') && text.includes('冲突')) return 'project_conflict'
  if (text.includes('接待阶段') || text.includes('业务阶段')) return text.includes('冲突') ? 'stage_conflict' : 'stage'
  if (text.includes('面诊医生')) return 'doctor'
  if (text.includes('现场顾问')) return 'advisor_name'
  if (text.includes('院前顾问') && text.includes('一致')) return 'advyq'
  if (text.includes('院前顾问')) return 'advyq_name'
  if (text.includes('接诊科室') || text.includes('科室')) return 'department'
  if (text.includes('客户年龄') || text.includes('客户出生日期')) return 'demographics:age'
  if (text.includes('客户性别特征与到诊单档案一致') || text.includes('客户性别特征与到诊单档案冲突')) {
    return 'demographics:gender'
  }
  if (text.includes('男性客户特征')) return 'demographics:gender_male'
  if (text.includes('录音时段覆盖接诊时间')) return 'duration'

  return `reason:${text}`
}

function formatEvidenceLine(item: MatchEvidenceLike) {
  const label = normalizeText(item.label)
  const detail = normalizeText(item.detail)
  if (label && detail) return `${label}：${detail}`
  return label || detail
}

function getEvidencePriority(item: MatchEvidenceLike) {
  return normalizeKey(item.type) === 'shortlist' ? 1 : 3
}

function shouldReplaceLine(next: DisplayLine, current: DisplayLine) {
  if (next.priority !== current.priority) return next.priority > current.priority
  return next.text.length > current.text.length
}

function shouldKeepStageLine(line: DisplayLine, visibleKeys: Set<string>) {
  const text = line.text
  if (text.includes('分诊/初诊') && (visibleKeys.has('time:triage') || visibleKeys.has('role_time'))) {
    return false
  }
  if (text.includes('面诊/方案') && (visibleKeys.has('time:consult') || visibleKeys.has('role_time'))) {
    return false
  }
  return true
}

function postProcessLines(lines: DisplayLine[]) {
  const visibleKeys = new Set(lines.map((item) => item.key))

  let filtered = lines

  if ((visibleKeys.has('time:triage') || visibleKeys.has('time:consult') || visibleKeys.has('time')) && visibleKeys.has('role_time')) {
    filtered = filtered.filter((item) => item.key !== 'role_time')
  }

  const recomputedKeys = new Set(filtered.map((item) => item.key))
  filtered = filtered.filter((item) => {
    if (item.key === 'stage') {
      return shouldKeepStageLine(item, recomputedKeys)
    }
    return true
  })

  return filtered
}

export function getDisplayMatchEvidenceLines(candidate: MatchCandidateLike) {
  const buckets = new Map<string, DisplayLine>()
  const linesInOrder: DisplayLine[] = []
  let order = 0

  const upsert = (key: string | null, text: string, priority: number) => {
    const normalizedText = normalizeText(text)
    if (!key || !normalizedText) return
    const existing = buckets.get(key)
    const next: DisplayLine = {
      key,
      text: normalizedText,
      priority,
      order: existing?.order ?? order++,
    }
    if (!existing) {
      buckets.set(key, next)
      linesInOrder.push(next)
      return
    }
    if (shouldReplaceLine(next, existing)) {
      existing.text = next.text
      existing.priority = next.priority
    }
  }

  for (const item of candidate.evidence ?? []) {
    upsert(classifyEvidence(item), formatEvidenceLine(item), getEvidencePriority(item))
  }

  for (const reason of candidate.reasons ?? []) {
    upsert(classifyReason(reason), reason, 2)
  }

  const seenText = new Set<string>()
  return postProcessLines(
    linesInOrder
    .map((item) => buckets.get(item.key)!)
    .filter((item) => {
      const normalized = normalizeKey(item.text)
      if (!normalized || seenText.has(normalized)) return false
      seenText.add(normalized)
      return true
    })
  ).map((item) => item.text)
}
