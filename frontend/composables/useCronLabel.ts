/**
 * Shared human-readable cron label.
 *
 * Single source of truth for turning a 5-field cron expression into a short
 * label (e.g. "0 9 * * 1" -> "Mon at 9:00 AM"). Used by the right-panel
 * scheduled-tasks list, the scheduled-tasks management page, and the
 * create_scheduled_task chat tool result, so the wording stays consistent.
 *
 * i18n-aware: relies on the existing `scheduled.*` message keys.
 */
export function useCronLabel() {
  const { t } = useI18n()

  function getCronLabel(cron?: string): string {
    if (!cron) return ''
    const parts = cron.split(' ')
    if (parts.length < 5) return cron
    const [min, hour, dom, , dow] = parts

    const isStep = (v: string) => v.startsWith('*/')
    const stepVal = (v: string) => parseInt(v.slice(2))

    if (isStep(min) && hour === '*') return t('scheduled.everyMinutes', { n: stepVal(min) })
    if (min !== '*' && isStep(hour)) return t('scheduled.everyHours', { n: stepVal(hour) })

    if (min !== '*' && hour !== '*' && !isStep(hour)) {
      const h = parseInt(hour)
      const ampm = h >= 12 ? 'PM' : 'AM'
      const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h
      const time = `${h12}:${min.padStart(2, '0')} ${ampm}`

      if (dow === '*' && dom === '*') return t('scheduled.dailyAt', { time })
      if (dow !== '*') {
        const dayKeys: Record<string, string> = {
          '0': 'scheduled.daySun', '1': 'scheduled.dayMon', '2': 'scheduled.dayTue',
          '3': 'scheduled.dayWed', '4': 'scheduled.dayThu', '5': 'scheduled.dayFri',
          '6': 'scheduled.daySat',
        }
        const days = dow.split(',').map((d: string) => (dayKeys[d] ? t(dayKeys[d]) : d)).join(', ')
        return t('scheduled.daysAt', { days, time })
      }
      return t('scheduled.monthlyOn', { day: dom, time })
    }

    return cron
  }

  return { getCronLabel }
}
