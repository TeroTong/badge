const SAP_FIELD_CONTINUATION_INDENT = ' '
const SAP_ITEM_MARKERS = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
const SAP_BULLET_FIELD_RE = /^●\s*([^：:\n]+?)\s*[：:]\s*(.*)$/
const SAP_MULTILINE_FIELD_LABELS = new Set(['顾客主诉', '顾客顾虑', '推荐方案', '未成交原因'])

function stripSapItemSeparator(value: string) {
  return String(value || '')
    .trim()
    .replace(/[；;]+$/g, '')
    .trim()
    .replace(new RegExp(`^\\s*(?:[${SAP_ITEM_MARKERS}]|\\d+\\s*[、.．])\\s*`), '')
    .trim()
}

function sapItemMarker(index: number) {
  if (index >= 1 && index <= SAP_ITEM_MARKERS.length) return SAP_ITEM_MARKERS[index - 1]
  return `${index}、`
}

function splitTopLevelSapItems(value: string) {
  const text = String(value || '').trim()
  if (!text) return []

  const items: string[] = []
  let current = ''
  let parenDepth = 0
  for (const char of text) {
    if (char === '（' || char === '(') {
      parenDepth += 1
    } else if ((char === '）' || char === ')') && parenDepth > 0) {
      parenDepth -= 1
    }

    if ((char === '；' || char === ';') && parenDepth === 0) {
      const item = stripSapItemSeparator(current)
      if (item) items.push(item)
      current = ''
      continue
    }
    current += char
  }

  const last = stripSapItemSeparator(current)
  if (last) items.push(last)
  return items
}

function dedupePreserveOrder(values: string[]) {
  const result: string[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const text = stripSapItemSeparator(value)
    if (!text || ['无', '暂无', '未明确', '-'].includes(text)) continue
    const key = text.replace(/\s+/g, '')
    if (seen.has(key)) continue
    seen.add(key)
    result.push(text)
  }
  return result
}

function formatSapMultilineField(title: string, values: string[]) {
  const items = dedupePreserveOrder(values)
  if (items.length === 0) return `●${title}：无`
  return items
    .map((item, index) => {
      const prefix = index === 0 ? `●${title}：` : SAP_FIELD_CONTINUATION_INDENT
      const suffix = index < items.length - 1 ? '；' : ''
      return `${prefix}${sapItemMarker(index + 1)}${item}${suffix}`
    })
    .join('\n')
}

export function formatSapPreviewText(text: string) {
  const lines = String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim().split('\n')
  if (lines.length === 0) return ''

  const blocks: string[] = []
  let index = 0
  while (index < lines.length) {
    const rawLine = lines[index].replace(/\s+$/g, '')
    const match = rawLine.trim().match(SAP_BULLET_FIELD_RE)
    if (!match) {
      if (rawLine.trim()) blocks.push(rawLine)
      index += 1
      continue
    }

    const title = match[1].trim()
    if (!SAP_MULTILINE_FIELD_LABELS.has(title)) {
      const blockLines = [rawLine]
      let nextIndex = index + 1
      while (nextIndex < lines.length) {
        const nextLine = lines[nextIndex].replace(/\s+$/g, '')
        if (nextLine.trim().match(SAP_BULLET_FIELD_RE)) break
        if (nextLine.trim()) blockLines.push(nextLine)
        nextIndex += 1
      }
      blocks.push(blockLines.join('\n').trim())
      index = nextIndex
      continue
    }

    const firstValue = stripSapItemSeparator(match[2] || '')
    const continuationValues: string[] = []
    let nextIndex = index + 1
    while (nextIndex < lines.length) {
      const nextLine = lines[nextIndex].replace(/\s+$/g, '')
      if (nextLine.trim().match(SAP_BULLET_FIELD_RE)) break
      if (nextLine.trim()) continuationValues.push(stripSapItemSeparator(nextLine))
      nextIndex += 1
    }

    const values = continuationValues.length > 0
      ? [firstValue, ...continuationValues]
      : splitTopLevelSapItems(firstValue)
    blocks.push(formatSapMultilineField(title, values))
    index = nextIndex
  }

  return blocks.filter((block) => block.trim()).join('\n\n').trim()
}
