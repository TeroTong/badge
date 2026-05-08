import dayjs from 'dayjs'

import { formatBeijingTime, type DateTimeInput } from '@/utils/time'

const CANONICAL_SPLIT_RECORDING_NAME_RE = /^(\d{4}_\d{6}_\d{6})(?:\.[A-Za-z0-9]+)?$/i
const CANONICAL_SPLIT_RECORDING_NAME_IN_TEXT_RE = /(\d{4}_\d{6}_\d{6})(?:\.[A-Za-z0-9]+)?/i
const CANONICAL_RECORDING_NAME_RE = /^(\d{4}_\d{6})(?:\.[A-Za-z0-9]+)?$/i
const CANONICAL_RECORDING_NAME_IN_TEXT_RE = /(\d{4}_\d{6})(?:\.[A-Za-z0-9]+)?/i
const FULL_DATETIME_IN_TEXT_RE = /\b(?:19|20)\d{2}[-_/]?\d{2}[-_/]?\d{2}[T _-]?\d{2}[:_-]?\d{2}[:_-]?\d{2}\b/
const DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

function normalizeLeafName(fileName: string) {
  const leaf = fileName.split(/[\\/]/).pop() || fileName
  return leaf.trim()
}

function toCanonicalDisplayName(baseName: string) {
  return `${baseName}.mp3`
}

function isValidTimeLabel(timeLabel: string) {
  const matched = /^(\d{2})(\d{2})(\d{2})$/.exec(timeLabel)
  if (!matched) return false

  const hour = Number(matched[1])
  const minute = Number(matched[2])
  const second = Number(matched[3])

  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return false
  if (!Number.isInteger(minute) || minute < 0 || minute > 59) return false
  if (!Number.isInteger(second) || second < 0 || second > 59) return false
  return true
}

function isValidCanonicalBaseName(baseName: string) {
  const matched = /^(\d{2})(\d{2})_(\d{6})$/.exec(baseName)
  if (!matched) return false

  const month = Number(matched[1])
  const day = Number(matched[2])

  if (!Number.isInteger(month) || month < 1 || month > 12) return false
  const maxDay = DAYS_IN_MONTH[month - 1] ?? 31
  if (!Number.isInteger(day) || day < 1 || day > maxDay) return false
  return isValidTimeLabel(matched[3])
}

function isValidCanonicalSplitBaseName(baseName: string) {
  const matched = /^(\d{4}_\d{6})_(\d{6})$/.exec(baseName)
  if (!matched) return false
  return isValidCanonicalBaseName(matched[1]) && isValidTimeLabel(matched[2])
}

function tryResolveCanonicalBaseName(fileName: string) {
  const normalized = normalizeLeafName(fileName)
  const exactSplitCanonical = normalized.match(CANONICAL_SPLIT_RECORDING_NAME_RE)
  if (exactSplitCanonical && isValidCanonicalSplitBaseName(exactSplitCanonical[1])) return exactSplitCanonical[1]

  const embeddedSplitCanonical = normalized.match(CANONICAL_SPLIT_RECORDING_NAME_IN_TEXT_RE)
  if (embeddedSplitCanonical && isValidCanonicalSplitBaseName(embeddedSplitCanonical[1])) return embeddedSplitCanonical[1]

  const exactCanonical = normalized.match(CANONICAL_RECORDING_NAME_RE)
  if (exactCanonical && isValidCanonicalBaseName(exactCanonical[1])) return exactCanonical[1]

  const embeddedCanonical = normalized.match(CANONICAL_RECORDING_NAME_IN_TEXT_RE)
  if (embeddedCanonical && isValidCanonicalBaseName(embeddedCanonical[1])) return embeddedCanonical[1]

  const fullDatetime = normalized.match(FULL_DATETIME_IN_TEXT_RE)?.[0]
  if (fullDatetime) {
    const parsed = dayjs(fullDatetime.replace(/_/g, ' '))
    if (parsed.isValid()) return parsed.format('MMDD_HHmmss')
  }

  return null
}

export function formatRecordingDisplayName(
  fileName: string | null | undefined,
  createdAt?: DateTimeInput | null,
) {
  const normalized = String(fileName || '').trim()
  if (!normalized) return '未命名录音'
  const canonicalBaseName = tryResolveCanonicalBaseName(normalized)
  if (canonicalBaseName) return toCanonicalDisplayName(canonicalBaseName)

  if (createdAt) {
    const formatted = formatBeijingTime(createdAt, 'MMDD_HHmmss', '')
    if (formatted) return toCanonicalDisplayName(formatted)
  }

  return normalizeLeafName(normalized)
}

export function formatRecordingDisplayBaseName(
  fileName: string | null | undefined,
  createdAt?: DateTimeInput | null,
) {
  return formatRecordingDisplayName(fileName, createdAt).replace(/\.[^.]+$/, '')
}
