const LEADING_SCORE_SUMMARY_RE = /^(?:(?:(?:六维(?:得分|总分)\s*\d+(?:\.\d+)?\s*\/\s*\d+(?:\.\d+)?|九点评价\s*\d+(?:\.\d+)?\s*\/\s*10(?:\.\d+)?))[。；\s]*)+/u
const DIMENSION_RULE_TEXT_PATTERNS = [
  /只要能从对话语义稳定映射到适应症，即视为获取成功，不要求咨询师或医生直接说出标准名称。?/gu,
  /该维度按\s*\d+\s*个必问\/重要标签的累计完成度计分，当前得分\s*\d+(?:\.\d+)?\/1。?/gu,
  /按评分规则记\s*0\s*分。?/gu,
]

export function sanitizeEvaluationSummary(value: string | null | undefined): string {
  if (!value) return ''
  const text = value.trim()
  if (!text) return ''
  return text.replace(LEADING_SCORE_SUMMARY_RE, '').trim()
}

export function sanitizeEvaluationDimensionSummary(value: string | null | undefined): string {
  if (!value) return ''
  let text = value.trim()
  if (!text) return ''
  for (const pattern of DIMENSION_RULE_TEXT_PATTERNS) {
    text = text.replace(pattern, '')
  }
  text = text.replace(/\s+/gu, ' ').replace(/([。；])(?:\s*[。；])+/gu, '$1').trim()
  return text
}
