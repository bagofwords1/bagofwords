<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-sparkles" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ runningLabel }}</span>
        </span>
        <span v-else-if="hasErrors" class="text-red-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1" />
          {{ summaryLabel }}
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <Icon :name="statusIcon" class="w-3 h-3 me-1" :class="statusIconColor" />
          {{ summaryLabel }}
        </span>
      </div>
    </Transition>

    <!-- Success card -->
    <Transition name="fade" appear>
      <div
        v-if="status !== 'running' && !hasErrors"
        class="border border-gray-200 rounded-md overflow-hidden text-xs"
      >
        <div class="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center gap-2">
          <Icon name="heroicons-cube" class="w-3.5 h-3.5 text-gray-500" />
          <span class="font-medium text-gray-800">{{ agentName }}</span>
          <span class="text-[9px] px-1.5 py-0.5 rounded uppercase tracking-wide" :class="statusBadgeClass">
            {{ statusLabel }}
          </span>
          <span
            v-if="args.is_public === false"
            class="text-[9px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 ms-auto"
          >private</span>
        </div>
        <div class="px-3 py-2 space-y-1.5">
          <div v-if="args.description" class="text-gray-600 leading-snug">{{ args.description }}</div>

          <!-- Connections -->
          <div v-if="connectionNames.length" class="flex items-start gap-2">
            <Icon name="heroicons-link" class="w-3 h-3 text-gray-400 mt-0.5" />
            <div class="flex flex-wrap gap-1">
              <code
                v-for="c in connectionNames"
                :key="c"
                class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700"
              >{{ c }}</code>
            </div>
          </div>

          <!-- Conversation starters -->
          <div v-if="starters.length" class="flex items-start gap-2">
            <Icon name="heroicons-chat-bubble-left-right" class="w-3 h-3 text-gray-400 mt-0.5" />
            <div class="flex-1 min-w-0">
              <div v-for="(q, i) in starters" :key="i" class="text-gray-600 truncate">{{ q }}</div>
            </div>
          </div>

          <!-- Tool policies -->
          <div v-if="toolPolicies.length" class="flex items-start gap-2">
            <Icon name="heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-400 mt-0.5" />
            <div class="flex-1 space-y-0.5 min-w-0">
              <div v-for="tp in toolPolicies" :key="tp.connection_name" class="flex items-center gap-1 flex-wrap">
                <span class="text-[10px] text-gray-500">{{ tp.connection_name }}</span>
                <span v-if="tp.allow && tp.allow.length" class="text-[9px] px-1 py-0.5 rounded bg-green-50 text-green-700">allow: {{ tp.allow.join(', ') }}</span>
                <span v-if="tp.confirm && tp.confirm.length" class="text-[9px] px-1 py-0.5 rounded bg-amber-50 text-amber-700">confirm: {{ tp.confirm.join(', ') }}</span>
                <span v-if="tp.deny && tp.deny.length" class="text-[9px] px-1 py-0.5 rounded bg-red-50 text-red-600">deny: {{ tp.deny.join(', ') }}</span>
              </div>
            </div>
          </div>

          <!-- Members -->
          <div v-if="members.length" class="flex items-start gap-2">
            <Icon name="heroicons-user-group" class="w-3 h-3 text-gray-400 mt-0.5" />
            <div class="flex flex-wrap gap-1">
              <span
                v-for="(m, i) in members"
                :key="i"
                class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 inline-flex items-center gap-1"
              >
                <Icon
                  :name="m.group ? 'heroicons-user-group' : 'heroicons-user'"
                  class="w-2.5 h-2.5 text-gray-500"
                />
                {{ m.user || m.group }}
              </span>
            </div>
          </div>

          <!-- Diff summary -->
          <div v-if="diffSummary.length" class="flex items-start gap-2 pt-1 border-t border-gray-100 mt-1">
            <Icon name="heroicons-arrow-path" class="w-3 h-3 text-gray-400 mt-0.5" />
            <div class="flex-1 min-w-0">
              <div
                v-for="(line, i) in diffSummary"
                :key="i"
                class="text-[10px] text-gray-500 truncate"
              >{{ line }}</div>
            </div>
          </div>

          <!-- Warnings -->
          <div v-if="warnings.length" class="flex items-start gap-2 pt-1 border-t border-gray-100 mt-1">
            <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 text-amber-500 mt-0.5" />
            <div class="flex-1 min-w-0 space-y-0.5">
              <div
                v-for="(w, i) in warnings"
                :key="i"
                class="text-[10px] text-amber-700"
              >
                <code class="text-amber-600">{{ w.code }}</code> — {{ w.message }}
              </div>
            </div>
          </div>
        </div>
      </div>
    </Transition>

    <!-- Error envelope -->
    <Transition name="fade" appear>
      <div
        v-if="hasErrors"
        class="border border-red-200 rounded-md overflow-hidden text-xs"
      >
        <div class="px-3 py-1.5 bg-red-50 border-b border-red-200 text-red-700">
          {{ errors.length }} error{{ errors.length === 1 ? '' : 's' }} — {{ agentName }} not saved
        </div>
        <ul class="px-3 py-2 space-y-1">
          <li v-for="(e, i) in errors" :key="i" class="text-gray-700 leading-snug">
            <div class="flex items-baseline gap-2">
              <code class="text-[10px] text-red-600">{{ e.code }}</code>
              <span v-if="e.loc && e.loc.length" class="text-[10px] text-gray-400">{{ e.loc.join('.') }}</span>
            </div>
            <div class="text-gray-700">{{ e.message }}</div>
            <div v-if="e.suggestion" class="text-[10px] text-blue-600 mt-0.5">
              Did you mean <code class="bg-blue-50 px-1 rounded">{{ e.suggestion }}</code>?
            </div>
          </li>
        </ul>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ToolExecution {
  status: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})

const agentName = computed<string>(() => result.value?.name || args.value?.name || 'agent')
const errors = computed<any[]>(() => Array.isArray(result.value?.errors) ? result.value.errors : [])
const warnings = computed<any[]>(() => Array.isArray(result.value?.warnings) ? result.value.warnings : [])
const hasErrors = computed<boolean>(() => errors.value.length > 0)
const applyStatus = computed<string>(() => result.value?.status || '')

const connectionNames = computed<string[]>(() => Array.isArray(args.value?.connection_names) ? args.value.connection_names : [])
const starters = computed<string[]>(() => Array.isArray(args.value?.conversation_starters) ? args.value.conversation_starters : [])
const toolPolicies = computed<any[]>(() => Array.isArray(args.value?.tool_policies) ? args.value.tool_policies : [])
const members = computed<any[]>(() => Array.isArray(args.value?.members) ? args.value.members : [])

const runningLabel = computed<string>(() => {
  const verb = args.value?.dry_run ? 'Validating' : 'Saving'
  return `${verb} agent ${agentName.value}…`
})

const statusLabel = computed<string>(() => {
  switch (applyStatus.value) {
    case 'created': return 'created'
    case 'updated': return 'updated'
    case 'unchanged': return 'unchanged'
    case 'dry_run': return 'dry run'
    default: return applyStatus.value || ''
  }
})

const statusBadgeClass = computed<string>(() => {
  switch (applyStatus.value) {
    case 'created': return 'bg-green-50 text-green-700'
    case 'updated': return 'bg-blue-50 text-blue-700'
    case 'unchanged': return 'bg-gray-100 text-gray-500'
    case 'dry_run': return 'bg-amber-50 text-amber-700'
    default: return 'bg-gray-100 text-gray-500'
  }
})

const statusIcon = computed<string>(() => {
  switch (applyStatus.value) {
    case 'created': return 'heroicons-check-circle'
    case 'updated': return 'heroicons-pencil-square'
    case 'unchanged': return 'heroicons-equals'
    case 'dry_run': return 'heroicons-beaker'
    default: return 'heroicons-cube'
  }
})

const statusIconColor = computed<string>(() => {
  switch (applyStatus.value) {
    case 'created': return 'text-green-500'
    case 'updated': return 'text-blue-500'
    case 'unchanged': return 'text-gray-400'
    case 'dry_run': return 'text-amber-500'
    default: return 'text-gray-400'
  }
})

const summaryLabel = computed<string>(() => {
  if (hasErrors.value) {
    return `Agent ${agentName.value} rejected (${errors.value.length} error${errors.value.length === 1 ? '' : 's'})`
  }
  const verb = statusLabel.value
  return verb ? `Agent ${agentName.value} ${verb}` : `Agent ${agentName.value}`
})

const diffSummary = computed<string[]>(() => {
  const d = result.value?.diff
  if (!d || typeof d !== 'object') return []
  const lines: string[] = []
  for (const [k, v] of Object.entries(d)) {
    if (k === 'action') continue
    if (v && typeof v === 'object' && 'from' in (v as any) && 'to' in (v as any)) {
      lines.push(`${k}: ${formatVal((v as any).from)} → ${formatVal((v as any).to)}`)
    } else if (Array.isArray(v)) {
      lines.push(`${k}: ${v.length} change${v.length === 1 ? '' : 's'}`)
    } else if (typeof v === 'object') {
      const keys = Object.keys(v as any)
      lines.push(`${k}: ${keys.join(', ')}`)
    } else {
      lines.push(`${k}: ${formatVal(v)}`)
    }
  }
  return lines.slice(0, 6)
})

function formatVal(v: any): string {
  if (v == null) return '∅'
  if (Array.isArray(v)) return `[${v.length}]`
  if (typeof v === 'object') return JSON.stringify(v).slice(0, 40)
  const s = String(v)
  return s.length > 40 ? s.slice(0, 40) + '…' : s
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
