import { useEffect, useMemo, useRef } from 'react'
import { Empty, Tag } from 'antd'

import { SPEAKER_MAP } from '@/api/segments'
import { keepElementInScrollContainerView } from '@/utils/scroll'

export type TranscriptUtteranceLite = {
  speaker?: string | null
  speaker_role?: string | null
  speaker_business_role?: string | null
  speaker_display_label?: string | null
  speaker_staff_name?: string | null
  speaker_identity_type?: string | null
  speaker_id?: string | null
  text?: string | null
  begin_ms?: number | null
  end_ms?: number | null
}

interface TranscriptPlaybackPanelProps {
  utterances: TranscriptUtteranceLite[]
  playbackMs: number | null
  onSeek: (ms: number) => void
  open: boolean
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '00:00'
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

type Role = 'staff' | 'customer' | 'unknown'

function normalizeSpeakerToken(value?: string | null): string {
  return String(value ?? '').trim().toLowerCase()
}

function isStaffToken(token: string): boolean {
  return (
    token === 'staff'
    || token === 'employee'
    || token === 'consultant'
    || token === 'advisor'
    || token === 'sales'
    || token === 'beauty_consultant'
    || token === 'service'
    || token === 'assistant'
    || token === 'doctor'
    || token === 'nurse'
    || token === 'badge_owner'
    || token === 'staff_peer'
    || token.includes('员工')
    || token.includes('咨询')
    || token.includes('顾问')
    || token.includes('客服')
    || token.includes('医生')
    || token.includes('护士')
    || token.includes('助理')
    || token.includes('工牌')
  )
}

function isCustomerToken(token: string): boolean {
  return (
    token === 'customer'
    || token === 'client'
    || token === 'patient'
    || token === 'visitor'
    || token === 'visitor_companion'
    || token === 'primary_customer'
    || token.includes('客户')
    || token.includes('顾客')
    || token.includes('患者')
    || token.includes('访客')
    || token.includes('同行')
    || token.includes('主客户')
  )
}

function resolveSpeakerMeta(value?: string | null) {
  const normalized = normalizeSpeakerToken(value)
  if (!normalized) return null
  return SPEAKER_MAP[value ?? ''] ?? SPEAKER_MAP[normalized] ?? null
}

function classifySpeaker(utterance: TranscriptUtteranceLite): Role {
  const directRoleCandidates = [
    utterance.speaker_role,
    utterance.speaker,
  ]
    .map((item) => normalizeSpeakerToken(item))
    .filter(Boolean)

  for (const token of directRoleCandidates) {
    if (isCustomerToken(token)) return 'customer'
    if (isStaffToken(token)) return 'staff'
  }

  const fallbackCandidates = [
    utterance.speaker_business_role,
    utterance.speaker_display_label,
    utterance.speaker_identity_type,
    utterance.speaker_staff_name,
    utterance.speaker_id,
  ]
    .map((item) => normalizeSpeakerToken(item))
    .filter(Boolean)

  for (const token of fallbackCandidates) {
    if (isStaffToken(token)) return 'staff'
    if (isCustomerToken(token)) return 'customer'
  }

  const fallback = [...directRoleCandidates, ...fallbackCandidates].find(
    (token) => token.startsWith('speaker_') || token.startsWith('spk'),
  )
  if (fallback) {
    if (fallback.includes('1')) return 'staff'
    if (fallback.includes('2')) return 'customer'
  }
  return 'unknown'
}

function speakerLabel(utterance: TranscriptUtteranceLite, role: Role): string {
  const directRoleToken = utterance.speaker_role ?? utterance.speaker
  const directRoleMeta = resolveSpeakerMeta(directRoleToken)
  const businessMeta = resolveSpeakerMeta(utterance.speaker_business_role)
  const displayMeta = resolveSpeakerMeta(utterance.speaker_display_label)
  const preferredLabel = utterance.speaker_display_label?.trim()
  const staffName = utterance.speaker_staff_name?.trim()
  let baseLabel = '未知'

  if (role === 'customer') {
    return (
      (directRoleMeta && isCustomerToken(normalizeSpeakerToken(directRoleToken)) ? directRoleMeta.label : null)
      || (businessMeta && isCustomerToken(normalizeSpeakerToken(utterance.speaker_business_role)) ? businessMeta.label : null)
      || (displayMeta && isCustomerToken(normalizeSpeakerToken(utterance.speaker_display_label)) ? displayMeta.label : null)
      || '客户'
    )
  }

  if (role === 'staff') {
    baseLabel = (
      (businessMeta && isStaffToken(normalizeSpeakerToken(utterance.speaker_business_role)) ? businessMeta.label : null)
      || (directRoleMeta && isStaffToken(normalizeSpeakerToken(directRoleToken)) ? directRoleMeta.label : null)
      || (displayMeta && isStaffToken(normalizeSpeakerToken(utterance.speaker_display_label)) ? displayMeta.label : null)
      || preferredLabel
      || '员工'
    )
  } else {
    baseLabel = preferredLabel || directRoleMeta?.label || businessMeta?.label || displayMeta?.label || '未知'
  }

  if (staffName && role === 'staff' && !baseLabel.includes(staffName)) {
    return `${baseLabel} · ${staffName}`
  }
  return baseLabel
}

function speakerColor(utterance: TranscriptUtteranceLite, role: Role): string {
  const directRoleToken = utterance.speaker_role ?? utterance.speaker
  const directRoleMeta = resolveSpeakerMeta(directRoleToken)
  const businessMeta = resolveSpeakerMeta(utterance.speaker_business_role)
  if (role === 'customer') {
    const customerMeta =
      (directRoleMeta && isCustomerToken(normalizeSpeakerToken(directRoleToken)) ? directRoleMeta : null)
      || (businessMeta && isCustomerToken(normalizeSpeakerToken(utterance.speaker_business_role)) ? businessMeta : null)
    if (customerMeta?.color && customerMeta.color !== 'default') return customerMeta.color
  }
  if (role === 'staff') {
    const staffMeta =
      (businessMeta && isStaffToken(normalizeSpeakerToken(utterance.speaker_business_role)) ? businessMeta : null)
      || (directRoleMeta && isStaffToken(normalizeSpeakerToken(directRoleToken)) ? directRoleMeta : null)
    if (staffMeta?.color && staffMeta.color !== 'default') return staffMeta.color
  }
  if (role === 'staff') return 'blue'
  if (role === 'customer') return 'green'
  return 'default'
}

export function TranscriptPlaybackPanel({
  utterances,
  playbackMs,
  onSeek,
  open,
}: TranscriptPlaybackPanelProps) {
  const listRef = useRef<HTMLDivElement | null>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])

  const activeIndex = useMemo(() => {
    if (playbackMs == null) return -1
    for (let i = 0; i < utterances.length; i += 1) {
      const u = utterances[i]
      const begin = u.begin_ms ?? 0
      const end = u.end_ms ?? begin
      if (playbackMs >= begin && playbackMs < end) return i
    }
    let latest = -1
    for (let i = 0; i < utterances.length; i += 1) {
      const begin = utterances[i].begin_ms ?? 0
      if (playbackMs >= begin) latest = i
      else break
    }
    return latest
  }, [utterances, playbackMs])

  useEffect(() => {
    if (!open || activeIndex < 0) return
    const el = itemRefs.current[activeIndex]
    const container = listRef.current
    keepElementInScrollContainerView(container, el, {
      topPadding: 56,
      bottomPadding: 80,
    })
  }, [activeIndex, open])

  if (!open) return null

  if (!utterances.length) {
    return (
      <div className="ad-transcript-panel">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无转写原文" />
      </div>
    )
  }

  return (
    <div className="ad-transcript-panel">
      <div className="ad-transcript-panel__hint">
        点击任一句可跳转音频；播放时会自动定位当前段落
      </div>
      <div className="ad-transcript-panel__list" ref={listRef}>
        {utterances.map((u, idx) => {
          const isActive = idx === activeIndex
          const beginMs = u.begin_ms ?? 0
          const role = classifySpeaker(u)
          const sideClass =
            role === 'staff'
              ? 'ad-transcript-utt--staff'
              : role === 'customer'
                ? 'ad-transcript-utt--customer'
                : 'ad-transcript-utt--unknown'
          const activeClass = isActive ? ' ad-transcript-utt--active' : ''
          return (
            <button
              key={`utt-${idx}-${beginMs}`}
              type="button"
              ref={(node) => { itemRefs.current[idx] = node }}
              className={`ad-transcript-utt ${sideClass}${activeClass}`}
              onClick={() => onSeek(beginMs)}
            >
              <div className="ad-transcript-utt__head">
                <Tag color={speakerColor(u, role)} style={{ marginRight: 0 }}>
                  {speakerLabel(u, role)}
                </Tag>
                <span className="ad-transcript-utt__time">
                  {formatMs(beginMs)} - {formatMs(u.end_ms ?? beginMs)}
                </span>
              </div>
              <div className="ad-transcript-utt__text">{u.text || '（无内容）'}</div>
            </button>
          )
        })}
      </div>
    </div>
  )
}
