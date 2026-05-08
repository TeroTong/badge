import dayjs, { type ConfigType, type Dayjs } from 'dayjs'
import timezone from 'dayjs/plugin/timezone'
import utc from 'dayjs/plugin/utc'

dayjs.extend(utc)
dayjs.extend(timezone)

export const BEIJING_TIME_ZONE = 'Asia/Shanghai'

export type DateTimeInput = ConfigType

export function beijingNow(): Dayjs {
  return dayjs().tz(BEIJING_TIME_ZONE)
}

export function toBeijingTime(value: DateTimeInput): Dayjs {
  return dayjs.utc(value).tz(BEIJING_TIME_ZONE)
}

export function formatBeijingTime(
  value: DateTimeInput | null | undefined,
  format = 'YYYY-MM-DD HH:mm:ss',
  fallback = '-',
) {
  if (value == null || value === '') return fallback
  const parsed = toBeijingTime(value)
  return parsed.isValid() ? parsed.format(format) : String(value)
}

export function splitBeijingDateTime(value: DateTimeInput | null | undefined) {
  if (value == null || value === '') {
    return { date: '-', time: '' }
  }
  const parsed = toBeijingTime(value)
  if (!parsed.isValid()) {
    return { date: String(value), time: '' }
  }
  return {
    date: parsed.format('YYYY-MM-DD'),
    time: parsed.format('HH:mm:ss'),
  }
}
