<template>
    <div class="mt-4 ms-11" data-testid="kms-diagnostics">
        <div class="flex flex-wrap items-center gap-2">
            <UButton size="xs" color="gray" variant="soft" :loading="isRunning" data-testid="kms-diagnostics-run" @click="runDiagnostics">
                {{ $t('kmsDiagnostics.run') }}
            </UButton>
            <span class="text-xs text-ink-muted">
                {{ isRunning ? $t('kmsDiagnostics.running') : $t('kmsDiagnostics.description') }}
            </span>
        </div>

        <div v-if="errorMessage" class="mt-2 text-xs text-red-600" role="alert" data-testid="kms-diagnostics-error">
            {{ errorMessage }}
        </div>

        <div v-if="report" class="mt-3 border border-line rounded-lg p-3" data-testid="kms-diagnostics-report">
            <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="min-w-0">
                    <div class="text-sm font-medium text-ink" data-testid="kms-diagnostics-overall">{{ overallLine }}</div>
                    <div v-if="report.failed_stage" class="text-xs text-ink-muted" data-testid="kms-diagnostics-stage">
                        {{ $t('kmsDiagnostics.failedStage', { stage: $t(`kmsDiagnostics.stage.${report.failed_stage}`) }) }}
                    </div>
                    <div v-if="report.manual_checks?.length" class="text-xs text-ink-muted" data-testid="kms-diagnostics-manual">
                        {{ $t('kmsDiagnostics.manualCheck') }}
                    </div>
                    <div class="text-xs text-ink-muted">
                        {{ report.calls_unknown ? $t('kmsDiagnostics.callsUnknown', { seconds: Math.round(report.duration_ms / 1000) }) : $t('kmsDiagnostics.calls', { calls: report.calls_made, seconds: Math.round(report.duration_ms / 1000) }) }}
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <UButton size="xs" color="gray" variant="ghost" data-testid="kms-diagnostics-copy" @click="copyReport">
                        {{ $t('kmsDiagnostics.copy') }}
                    </UButton>
                    <UButton size="xs" color="gray" variant="ghost" data-testid="kms-diagnostics-download" @click="downloadReport">
                        {{ $t('kmsDiagnostics.download') }}
                    </UButton>
                </div>
            </div>

            <ul class="mt-3 space-y-1">
                <li v-for="check in report.checks" :key="check.id" class="flex items-start gap-2 text-xs" data-testid="kms-diagnostics-check">
                    <span :class="['shrink-0 px-1.5 py-0.5 rounded border', statusClass(check.status)]">
                        {{ $t(`kmsDiagnostics.status.${check.status}`) }}
                    </span>
                    <span dir="ltr" class="min-w-0 text-ink-2">
                        <span class="font-mono text-ink-muted">{{ check.id }}</span>
                        {{ check.summary }}
                    </span>
                </li>
            </ul>

            <div class="mt-3 text-2xs text-ink-muted">{{ $t('kmsDiagnostics.reportNote') }}</div>
        </div>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{ connectionId: string }>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const isRunning = ref(false)
const report = ref<any>(null)
const errorMessage = ref('')

const overallLine = computed(() => {
  const overall = report.value?.overall
  if (overall === 'pass') return t('kmsDiagnostics.overallPass')
  if (overall === 'fail') return t('kmsDiagnostics.overallFail')
  return t('kmsDiagnostics.overallWarn')
})

function statusClass(status: string) {
  if (status === 'pass') return 'bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-400 border-green-200'
  if (status === 'fail') return 'bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-400 border-red-200'
  if (status === 'warn') return 'bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 border-amber-200'
  return 'bg-surface-subtle text-ink-muted border-line'
}

async function runDiagnostics() {
  if (isRunning.value) return
  isRunning.value = true
  report.value = null
  errorMessage.value = ''
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connectionId}/kms-diagnostics`, { method: 'POST' })
    const answer = data.value as any
    if (error.value) errorMessage.value = getErrorMessage(error.value, t('kmsDiagnostics.failed'))
    else if (answer?.report_version && Array.isArray(answer.checks)) report.value = answer
    else errorMessage.value = t('kmsDiagnostics.failed')
  } catch (e: any) {
    errorMessage.value = getErrorMessage(e, t('kmsDiagnostics.failed'))
  } finally {
    isRunning.value = false
  }
}

const reportText = () => JSON.stringify(report.value, null, 2)

async function copyReport() {
  try {
    await navigator.clipboard.writeText(reportText())
    toast.add({ title: t('kmsDiagnostics.copied') })
  } catch {
    // No clipboard outside a secure context (a plain http install): Download still works.
    toast.add({ title: t('kmsDiagnostics.copyFailed'), color: 'red' })
  }
}

function downloadReport() {
  const url = URL.createObjectURL(new Blob([reportText()], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'kms-diagnostics.json'
  link.click()
  URL.revokeObjectURL(url)
}
</script>
