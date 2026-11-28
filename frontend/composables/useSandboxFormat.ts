// Dates, durations and sizes as the Sandboxes list and its dialogs show them,
// in the active locale.
import type { Ref } from 'vue'
import { parseSandboxDate } from '~/composables/useSandboxApi'

// `inLocale`: format in that locale instead of the app's (a tool block in a
// report speaks the response's language, see useResponseI18n).
export function useSandboxFormat(inLocale?: Ref<string>) {
  const locale = inLocale || useI18n().locale

  function formatDate(value?: string | null) {
    if (!value) return ''
    return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(parseSandboxDate(value))
  }

  // "3 minutes ago" / "in 29 days", relative to `now` (ms).
  function relative(value: string, now: number = Date.now()) {
    const seconds = Math.round((parseSandboxDate(value).getTime() - now) / 1000)
    const format = new Intl.RelativeTimeFormat(locale.value, { numeric: 'auto' })
    const abs = Math.abs(seconds)
    // Within a few seconds either way is "now" (the server's clock and this one differ a little).
    if (abs < 3) return format.format(0, 'second')
    if (abs < 60) return format.format(seconds, 'second')
    if (abs < 3600) return format.format(Math.round(seconds / 60), 'minute')
    if (abs < 86400) return format.format(Math.round(seconds / 3600), 'hour')
    return format.format(Math.round(seconds / 86400), 'day')
  }

  // A length of time in the two largest units: "0.4 s", "42 s", "3 min 05 s", "2 h 10 min".
  function duration(ms: number) {
    const unit = (value: number, name: 'second' | 'minute' | 'hour', digits = 0) =>
      new Intl.NumberFormat(locale.value, {
        style: 'unit', unit: name, unitDisplay: 'narrow', maximumFractionDigits: digits,
      }).format(value)
    const total = Math.max(0, ms) / 1000
    if (total < 10) return unit(total, 'second', 1)
    const seconds = Math.floor(total)
    if (seconds < 60) return unit(seconds, 'second')
    if (seconds < 3600) return `${unit(Math.floor(seconds / 60), 'minute')} ${unit(seconds % 60, 'second')}`
    return `${unit(Math.floor(seconds / 3600), 'hour')} ${unit(Math.floor((seconds % 3600) / 60), 'minute')}`
  }

  function between(start?: string | null, end?: string | null, now: number = Date.now()) {
    if (!start) return ''
    const from = parseSandboxDate(start).getTime()
    return duration((end ? parseSandboxDate(end).getTime() : now) - from)
  }

  // Binary sizes, as memory and disk limits are set in MB/GB of 1024.
  function bytes(value: number) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB']
    let size = Math.max(0, value)
    let index = 0
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1 }
    const digits = size >= 100 || index === 0 ? 0 : 1
    return `${new Intl.NumberFormat(locale.value, { maximumFractionDigits: digits }).format(size)} ${units[index]}`
  }

  function percent(value: number) {
    return new Intl.NumberFormat(locale.value, { style: 'percent', maximumFractionDigits: value < 10 ? 1 : 0 }).format(value / 100)
  }

  return { formatDate, relative, duration, between, bytes, percent }
}

export const SANDBOX_STATES = ['provisioning', 'ready', 'busy', 'idle', 'stopped', 'failed']

export function sandboxStateColor(state: string) {
  switch (state) {
    case 'ready': return 'green'
    case 'busy': return 'blue'
    case 'failed': return 'red'
    case 'provisioning': return 'amber'
    default: return 'gray'
  }
}
