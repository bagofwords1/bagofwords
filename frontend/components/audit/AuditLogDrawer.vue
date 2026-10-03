<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition-opacity duration-150"
      leave-active-class="transition-opacity duration-150"
      enter-from-class="opacity-0"
      leave-to-class="opacity-0"
    >
      <div v-if="log" class="fixed inset-0 z-[1050] bg-black/20" @click="emit('close')" />
    </Transition>
    <Transition
      enter-active-class="transition-transform duration-200 ease-out"
      leave-active-class="transition-transform duration-150 ease-in"
      :enter-from-class="isRtl ? '-translate-x-full' : 'translate-x-full'"
      :leave-to-class="isRtl ? '-translate-x-full' : 'translate-x-full'"
    >
      <aside
        v-if="log"
        data-testid="audit-drawer"
        role="dialog"
        aria-modal="true"
        :aria-label="$t('settings.audit.drawer.title')"
        class="fixed inset-y-0 end-0 z-[1100] w-full sm:w-[460px] bg-white dark:bg-gray-900 border-s border-gray-200 dark:border-gray-700 shadow-xl flex flex-col"
      >
        <!-- Header -->
        <div class="px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex items-start gap-3">
          <div class="flex-1 min-w-0">
            <div class="text-[11px] text-gray-400 mb-1">{{ $t('settings.audit.drawer.title') }}</div>
            <div class="flex items-center gap-1.5 flex-wrap">
              <span v-for="(seg, i) in split.path" :key="i" class="text-sm text-gray-500 dark:text-gray-400">{{ seg }}<span class="mx-1 text-gray-300">·</span></span>
              <span class="inline-flex px-1.5 py-0.5 rounded text-xs font-medium" :class="verbClass(log.action)">{{ split.verb }}</span>
            </div>
          </div>
          <button
            type="button"
            data-testid="audit-drawer-close"
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
            :aria-label="$t('settings.audit.drawer.close')"
            @click="emit('close')"
          >
            <UIcon name="i-heroicons-x-mark" class="w-5 h-5" />
          </button>
        </div>

        <div class="flex-1 overflow-y-auto px-4 py-3 space-y-5 text-xs">
          <!-- Who -->
          <section>
            <h4 class="section-title">{{ $t('settings.audit.drawer.who') }}</h4>
            <dl class="fields">
              <dt>{{ $t('settings.audit.drawer.actor') }}</dt>
              <dd data-testid="audit-field-actor">{{ log.user_email || $t('settings.audit.system') }}</dd>
              <dt>{{ $t('settings.audit.drawer.actorType') }}</dt>
              <dd>
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">
                  <UIcon :name="kindIcon" class="w-3 h-3" />
                  {{ $t(`settings.audit.drawer.actorKinds.${kind}`) }}
                </span>
              </dd>
            </dl>
          </section>

          <!-- When -->
          <section>
            <h4 class="section-title">{{ $t('settings.audit.drawer.when') }}</h4>
            <dl class="fields">
              <dt>{{ $t('settings.audit.drawer.timeLocal') }}</dt>
              <dd>{{ localTime }}</dd>
              <dt>{{ $t('settings.audit.drawer.timeUtc') }}</dt>
              <dd class="font-mono" dir="ltr">{{ utcTime }}</dd>
              <dt>{{ $t('settings.audit.drawer.relative') }}</dt>
              <dd>{{ relativeTime }}</dd>
            </dl>
          </section>

          <!-- Where -->
          <section>
            <h4 class="section-title">{{ $t('settings.audit.drawer.where') }}</h4>
            <dl class="fields">
              <dt>{{ $t('settings.audit.drawer.ip') }}</dt>
              <dd class="font-mono" dir="ltr" data-testid="audit-field-ip">{{ log.ip_address || none }}</dd>
              <dt>{{ $t('settings.audit.drawer.userAgent') }}</dt>
              <dd class="break-all" dir="ltr" data-testid="audit-field-user_agent">{{ log.user_agent || none }}</dd>
            </dl>
          </section>

          <!-- What -->
          <section>
            <h4 class="section-title">{{ $t('settings.audit.drawer.what') }}</h4>
            <dl class="fields">
              <dt>{{ $t('settings.audit.drawer.action') }}</dt>
              <dd class="flex items-center gap-1.5">
                <code class="font-mono" dir="ltr">{{ log.action }}</code>
                <CopyChip :value="log.action" />
              </dd>
              <dt>{{ $t('settings.audit.drawer.resourceType') }}</dt>
              <dd>{{ log.resource_type || none }}</dd>
              <dt>{{ $t('settings.audit.drawer.resourceId') }}</dt>
              <dd class="flex items-center gap-1.5 min-w-0" data-testid="audit-field-resource_id">
                <template v-if="log.resource_id">
                  <code class="font-mono truncate" dir="ltr">{{ log.resource_id }}</code>
                  <CopyChip :value="log.resource_id" />
                  <NuxtLink v-if="link" :to="link" class="text-blue-600 hover:text-blue-700 inline-flex items-center gap-0.5" @click="emit('close')">
                    {{ $t('settings.audit.drawer.open') }}
                    <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3 h-3 rtl:-scale-x-100" />
                  </NuxtLink>
                </template>
                <span v-else>{{ none }}</span>
              </dd>
              <template v-if="details.agent_execution_id">
                <dt>{{ $t('settings.audit.drawer.agentRun') }}</dt>
                <dd class="flex items-center gap-1.5 min-w-0">
                  <code class="font-mono truncate" dir="ltr">{{ details.agent_execution_id }}</code>
                  <CopyChip :value="String(details.agent_execution_id)" />
                </dd>
              </template>
              <template v-if="details.execution_mode">
                <dt>{{ $t('settings.audit.drawer.executionMode') }}</dt>
                <dd>{{ details.execution_mode }}</dd>
              </template>
            </dl>
          </section>

          <!-- Details -->
          <section>
            <h4 class="section-title">{{ $t('settings.audit.drawer.details') }}</h4>
            <div v-if="knownFields.length === 0 && extraKeys.length === 0" class="text-gray-400">{{ $t('settings.audit.drawer.noDetails') }}</div>
            <dl v-if="knownFields.length" class="fields" data-testid="audit-details">
              <template v-for="f in knownFields" :key="f.key">
                <dt>{{ $t(`settings.audit.drawer.fields.${f.key}`) }}</dt>
                <dd>
                  <div v-if="f.key === 'queries'" class="space-y-1.5">
                    <pre
                      v-for="(q, i) in asList(f.value)"
                      :key="i"
                      dir="ltr"
                      class="font-mono text-[11px] whitespace-pre-wrap break-words bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 rounded px-2 py-1.5"
                    >{{ q }}</pre>
                  </div>
                  <div v-else-if="f.key === 'changed' && isObject(f.value)" class="space-y-0.5">
                    <div v-for="(v, k) in f.value" :key="k" class="font-mono text-[11px]" dir="ltr">
                      <span class="text-gray-500">{{ k }}:</span>
                      <template v-if="Array.isArray(v) && v.length === 2">
                        <span class="text-red-600 line-through">{{ fmt(v[0]) }}</span>
                        <span class="text-gray-400"> → </span>
                        <span class="text-green-700">{{ fmt(v[1]) }}</span>
                      </template>
                      <span v-else>{{ fmt(v) }}</span>
                    </div>
                  </div>
                  <span v-else class="break-words">{{ fmt(f.value) }}</span>
                </dd>
              </template>
            </dl>
            <details class="mt-3 group" :open="knownFields.length === 0 && extraKeys.length > 0">
              <summary class="cursor-pointer select-none text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 flex items-center gap-1">
                <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 transition-transform group-open:rotate-90 rtl:-scale-x-100" />
                {{ $t('settings.audit.drawer.rawJson') }}
                <CopyChip class="ms-auto" :value="rawJson" />
              </summary>
              <pre dir="ltr" data-testid="audit-raw-json" class="mt-2 font-mono text-[11px] whitespace-pre-wrap break-words bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 rounded px-2 py-1.5 max-h-80 overflow-auto">{{ rawJson }}</pre>
            </details>
          </section>
        </div>
      </aside>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { defineComponent, h, resolveComponent } from 'vue'
import type { AuditLog } from '~/ee/composables/useAuditLogs'
import { splitAction, verbClass, actorKind, resourceLink, KNOWN_DETAIL_KEYS } from '~/utils/auditActionFormat'

const props = defineProps<{ log: AuditLog | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const { t, locale } = useI18n({ useScope: 'global' })
const _df = useFormatDate()
const isRtl = computed(() => ['he', 'ar', 'fa', 'ur'].includes(String(locale.value)))
const none = computed(() => t('settings.audit.drawer.none'))

const split = computed(() => splitAction(props.log?.action || ''))
const kind = computed(() => (props.log ? actorKind(props.log) : 'system'))
const kindIcon = computed(() => ({ user: 'i-heroicons-user', agent: 'i-heroicons-sparkles', system: 'i-heroicons-cog-6-tooth' }[kind.value]))
const link = computed(() => (props.log ? resourceLink(props.log) : null))
const details = computed<Record<string, any>>(() => (props.log?.details && typeof props.log.details === 'object' ? props.log.details : {}))

const SKIP_IN_FIELDS = new Set(['agent_execution_id', 'execution_mode'])
const knownFields = computed(() =>
  KNOWN_DETAIL_KEYS.filter((k) => !SKIP_IN_FIELDS.has(k) && details.value[k] !== undefined && details.value[k] !== null && details.value[k] !== '')
    .map((k) => ({ key: k, value: details.value[k] })),
)
const extraKeys = computed(() => Object.keys(details.value).filter((k) => !(KNOWN_DETAIL_KEYS as readonly string[]).includes(k)))
const rawJson = computed(() => JSON.stringify(props.log?.details ?? {}, null, 2))

const toDate = (s: string) => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z')
const localTime = computed(() => (props.log ? _df.format(props.log.created_at, { dateStyle: 'medium', timeStyle: 'medium' }) : ''))
const utcTime = computed(() => (props.log ? toDate(props.log.created_at).toISOString() : ''))
const relativeTime = computed(() => {
  if (!props.log) return ''
  const diff = (toDate(props.log.created_at).getTime() - Date.now()) / 1000
  const rtf = new Intl.RelativeTimeFormat(String(locale.value), { numeric: 'auto' })
  const abs = Math.abs(diff)
  if (abs < 60) return rtf.format(Math.round(diff), 'second')
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour')
  return rtf.format(Math.round(diff / 86400), 'day')
})

const isObject = (v: any) => v && typeof v === 'object' && !Array.isArray(v)
const asList = (v: any) => (Array.isArray(v) ? v : [v])
const fmt = (v: any) => (v === null || v === undefined ? '∅' : typeof v === 'object' ? JSON.stringify(v) : String(v))

const onKey = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && props.log) emit('close')
}
onMounted(() => document.addEventListener('keydown', onKey))
onUnmounted(() => document.removeEventListener('keydown', onKey))

const CopyChip = defineComponent({
  props: { value: { type: String, required: true } },
  setup(p) {
    const copied = ref(false)
    const copy = async (e: Event) => {
      e.preventDefault()
      e.stopPropagation()
      try {
        await navigator.clipboard.writeText(p.value)
        copied.value = true
        setTimeout(() => (copied.value = false), 1200)
      } catch {}
    }
    return () =>
      h('button', {
        type: 'button',
        class: 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 shrink-0',
        title: copied.value ? t('settings.audit.drawer.copied') : t('settings.audit.drawer.copy'),
        'aria-label': t('settings.audit.drawer.copy'),
        onClick: copy,
      }, [h(resolveComponent('UIcon') as any, { name: copied.value ? 'i-heroicons-check' : 'i-heroicons-clipboard-document', class: 'w-3.5 h-3.5' })])
  },
})
</script>

<style scoped>
.section-title {
  @apply text-[11px] font-medium uppercase tracking-wide text-gray-400 mb-1.5;
}
.fields {
  @apply grid gap-x-3 gap-y-1.5;
  grid-template-columns: 110px minmax(0, 1fr);
}
.fields dt {
  @apply text-gray-500 dark:text-gray-400;
}
.fields dd {
  @apply text-gray-800 dark:text-gray-200 min-w-0;
}
</style>
