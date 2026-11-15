<template>
  <section class="world-view flex min-h-0 flex-col bg-surface text-ink" :style="{ '--world-banner': showTopBanner ? bannerHeight : '0px' }" data-testid="world-view">
    <header class="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
      <div class="flex items-center gap-3">
        <NuxtLink to="/agents" class="text-sm text-ink-muted hover:text-ink">{{ t('agentsPage.title') }}</NuxtLink>
        <span class="text-ink-subtle">/</span>
        <span class="flex items-center gap-2 text-sm font-medium"><UIcon name="i-heroicons-globe-alt" class="h-4 w-4" />{{ t('world.title') }}</span>
      </div>
      <div class="flex items-center gap-2">
        <div class="hidden items-center gap-2 text-xs text-ink-muted sm:flex" role="status">
          <span class="h-1.5 w-1.5 rounded-full" :class="pending ? 'bg-amber-400' : 'bg-emerald-500'" />
          {{ pending ? t('world.loading') : t('world.personalScope') }}
        </div>
        <span class="mx-2 hidden h-4 border-s border-line sm:block" />
        <WorldViewSwitch active="world" />
        <UButton color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="!!pending" :aria-label="t('world.refresh')" @click="refreshWorld" />
      </div>
    </header>

    <div class="relative flex min-h-0 flex-1 flex-col lg:flex-row">
      <div class="world-stage relative min-h-0 min-w-0 flex-1 overflow-hidden">
        <WorldCanvas ref="map" :nodes="nodes" :edges="graphEdges" @inspect-usage="inspectUsage" :visible-ids="visibleIds" :selected="selectedId" :show-links="showLinks" @select="select" />
        <div class="pointer-events-none absolute start-6 top-6 max-w-sm">
          <div class="mt-5 flex items-center gap-5">
            <div><span class="nums text-xl font-medium">{{ number(visibleNodes.length) }}</span><span class="ms-2 text-xs text-ink-muted">{{ t('world.items') }}</span></div>
            <span class="h-5 border-s border-line-strong" />
            <div><span class="nums text-xl font-medium">{{ number(visibleEdges.length) }}</span><span class="ms-2 text-xs text-ink-muted">{{ t('world.connections') }}</span></div>
          </div>
        </div>
        <div v-if="!nodes.length && pending" class="absolute inset-0 flex items-center justify-center pt-28" aria-live="polite">
          <div class="flex items-center gap-3 rounded-lg border border-line bg-surface-elevated px-5 py-4 text-sm text-ink-muted shadow-sm">
            <UIcon name="i-heroicons-arrow-path" class="h-4 w-4 animate-spin motion-reduce:animate-none" />{{ t('world.discovering') }}
          </div>
        </div>
        <div v-else-if="!visibleNodes.length" class="pointer-events-none absolute inset-0 flex items-center justify-center pt-28">
          <div class="pointer-events-auto max-w-xs rounded-lg border border-line bg-surface-elevated/95 p-6 text-center shadow-sm">
            <UIcon name="i-heroicons-globe-alt" class="mb-3 h-8 w-8 text-ink-muted" />
            <p class="text-sm font-medium">{{ nodes.length ? t('world.noMatches') : t('world.empty') }}</p>
            <p class="mt-2 text-xs leading-relaxed text-ink-muted">{{ nodes.length ? t('world.resetHint') : t('world.emptyHint') }}</p>
            <UButton v-if="nodes.length" class="mt-4" color="gray" variant="soft" @click="resetFilters">{{ t('world.reset') }}</UButton>
            <UButton v-else class="mt-4" to="/agents" color="gray" variant="soft">{{ t('world.explorer') }}</UButton>
          </div>
        </div>
        <div class="pointer-events-none absolute bottom-6 end-6 hidden text-2xs text-ink-muted sm:block">{{ t('world.canvasHint') }}</div>
        <div v-if="memory" class="pointer-events-none absolute end-5 top-5 flex items-center gap-2 rounded-full border border-purple-200/60 bg-surface-elevated/90 px-3 py-1.5 text-xs text-ink-2 dark:border-purple-800">
          <UIcon name="i-heroicons-sparkles" class="h-3.5 w-3.5 text-purple-500" />{{ t('world.privateOverlay') }}
        </div>
      </div>

      <aside class="world-panel flex shrink-0 flex-col border-t border-line bg-surface-elevated lg:w-80 lg:border-s lg:border-t-0" :aria-label="t('world.details')">
        <div class="border-b border-line p-4">
          <UInput v-model="search" icon="i-heroicons-magnifying-glass" :placeholder="t('world.search')" :aria-label="t('world.search')" size="sm" @keydown.escape="search = ''" />
          <div class="mt-3 flex rounded-lg bg-surface-muted p-1" role="tablist" :aria-label="t('world.details')">
            <UButton v-for="tab in ['layers', 'browse'] as const" :key="tab" block color="gray" :variant="panel === tab ? 'solid' : 'ghost'"
              class="flex-1 justify-center" size="xs" role="tab" :aria-selected="panel === tab" @click="panel = tab">{{ t(`world.${tab}`) }}</UButton>
          </div>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto">
          <div v-if="selected" class="border-b border-line p-5" data-testid="world-detail">
            <div class="mb-4 flex items-center justify-between">
              <span class="flex items-center gap-2 text-xs text-ink-muted"><UIcon :name="worldIcons[selected.type]" class="h-4 w-4" :style="{ color: worldColors[selected.type] }" />{{ t(`world.types.${selected.type}`) }}</span>
              <UButton color="gray" variant="ghost" size="2xs" icon="i-heroicons-x-mark" :aria-label="t('world.close')" @click="select(null)" />
            </div>
            <h2 class="break-words text-lg font-semibold leading-snug">{{ selected.label }}</h2>
            <p v-if="selected.description" class="mt-3 whitespace-pre-wrap break-words text-xs leading-relaxed text-ink-muted">{{ selected.description }}</p>
            <dl v-if="detailFields.length" class="mt-4 space-y-2 rounded-lg bg-surface-subtle p-3">
              <div v-for="field in detailFields" :key="field.key" class="flex items-start justify-between gap-3 text-xs">
                <dt class="text-ink-muted">{{ t(`world.fields.${field.key}`) }}</dt><dd class="nums break-all text-end text-ink-2">{{ field.value }}</dd>
              </div>
            </dl>
            <div v-if="selected.meta.private" class="mt-3 flex items-center gap-1.5 text-xs text-ink-muted"><UIcon name="i-heroicons-lock-closed" class="h-3.5 w-3.5" />{{ t('world.onlyYou') }}</div>
            <div class="mt-4 flex flex-wrap gap-2">
              <UButton v-if="selected.url" :to="selected.url" color="gray" variant="soft" size="xs" trailing-icon="i-heroicons-arrow-up-right">{{ t('world.open') }}</UButton>
              <UButton v-if="selected.expandable && !expanded.has(selected.id)" color="gray" variant="outline" size="xs" icon="i-heroicons-plus" :loading="!!pending" @click="expand(selected)">{{ t('world.expand') }}</UButton>
            </div>
            <div v-if="neighbors.length" class="mt-5">
              <h3 class="mb-2 text-2xs font-semibold uppercase tracking-wider text-ink-muted">{{ t('world.related') }} <span class="nums">{{ neighbors.length }}</span></h3>
              <div v-for="item in neighbors.slice(0, neighborLimit)" :key="item.edge.id" class="mb-1 rounded-lg border border-line">
                <UButton color="gray" variant="ghost" block class="justify-start text-start" size="xs" @click="select(item.node.id)">
                  <span class="h-1.5 w-1.5 shrink-0 rounded-full" :style="{ backgroundColor: worldColors[item.node.type] }" /><span class="truncate">{{ item.node.label }}</span>
                </UButton>
                <details v-if="item.edge.report_ids" :open="inspectedEdge === item.edge.id" class="px-3 pb-2 text-xs">
                  <summary class="cursor-pointer text-ink-muted">{{ t('world.usedTogether', { count: item.edge.report_count }) }}</summary>
                  <NuxtLink v-for="id in item.edge.report_ids" :key="id" :to="reportsById.get(id)!.url!" class="block py-1 hover:underline">
                    {{ reportsById.get(id)?.label }}
                  </NuxtLink>
                </details>
                <p v-else class="px-3 pb-2 text-2xs text-ink-muted">{{ t(`world.relations.${item.edge.relation}`) }}</p>
              </div>
              <UButton v-if="neighbors.length > neighborLimit" color="gray" variant="ghost" size="xs" @click="neighborLimit += 30">{{ t('world.showMore') }}</UButton>
            </div>
            <p v-else class="mt-4 text-xs leading-relaxed text-ink-muted">{{ t('world.noLinks') }}</p>
          </div>

          <template v-if="panel === 'layers' && !search">
            <div class="space-y-3 px-5 pt-5">
              <label class="flex items-center justify-between text-xs">{{ t('world.usage') }}
                <UToggle v-model="showUsage" size="xs" :aria-label="t('world.usage')" />
              </label>
              <label class="flex items-center justify-between text-xs">{{ t('world.myReports') }}
                <UToggle v-model="mine" size="xs" :aria-label="t('world.myReports')" />
              </label>
            </div>
            <div class="mx-5 mt-5 rounded-lg border border-purple-200/60 bg-purple-50/40 p-4 dark:border-purple-800/40 dark:bg-purple-950/10">
              <div class="flex items-center gap-2">
                <UIcon name="i-heroicons-sparkles" class="h-4 w-4 text-purple-500" />
                <span class="flex-1 text-xs font-medium">{{ t('world.memory') }}</span>
                <UToggle v-model="memory" :aria-label="t('world.memory')" size="sm" />
              </div>
              <p class="mt-2 text-xs leading-relaxed text-ink-muted">{{ t('world.memoryHint') }}</p>
              <p v-if="memory" class="mt-2 text-2xs text-ink-muted">{{ t('world.memoryCount', { count: counts.memory || 0 }) }}</p>
            </div>
            <div class="px-5 pb-3 pt-5">
              <div class="mb-4 flex items-center justify-between">
                <h2 class="text-2xs font-semibold uppercase tracking-wider text-ink-muted">{{ t('world.entityTypes') }}</h2>
                <UButton color="gray" variant="link" size="2xs" @click="toggleAll">{{ enabled.size === worldTypes.length ? t('world.hideAll') : t('world.showAll') }}</UButton>
              </div>
              <div v-for="type in worldTypes.filter(type => type !== 'memory')" :key="type" class="mb-1 flex items-center gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-surface-subtle">
                <UCheckbox :model-value="enabled.has(type)" :aria-label="t(`world.types.${type}`)" @update:model-value="toggleType(type)" />
                <UIcon :name="worldIcons[type]" class="h-4 w-4 shrink-0" :style="{ color: worldColors[type] }" />
                <span class="flex-1 text-xs" :class="enabled.has(type) ? 'text-ink-2' : 'text-ink-muted'">{{ t(`world.types.${type}`) }}</span>
                <span class="nums text-xs text-ink-muted">{{ number(counts[type] || 0) }}</span>
              </div>
            </div>

          </template>
          <div v-else class="p-3">
            <p class="px-2 py-2 text-2xs text-ink-muted">{{ t('world.browseHint') }}</p>
            <UButton v-for="node in visibleNodes.slice(0, browseLimit)" :key="node.id" color="gray" :variant="selectedId === node.id ? 'soft' : 'ghost'" block class="mb-1 justify-start py-2 text-start" @click="select(node.id)">
              <UIcon :name="worldIcons[node.type]" class="h-4 w-4 shrink-0" :style="{ color: worldColors[node.type] }" />
              <span class="min-w-0"><span class="block truncate text-xs">{{ node.label }}</span><span class="block text-2xs text-ink-muted">{{ t(`world.types.${node.type}`) }}</span></span>
            </UButton>
            <UButton v-if="visibleNodes.length > browseLimit" color="gray" variant="ghost" block @click="browseLimit += 100">{{ t('world.showMore') }}</UButton>
          </div>
        </div>
        <footer class="space-y-3 border-t border-line p-4">
          <div v-if="failures.length" class="text-xs text-ink-muted" role="alert">{{ t('world.partialError') }} <UButton color="gray" variant="link" size="xs" @click="retry">{{ t('world.retry') }}</UButton></div>
          <div v-if="next.length || loadingAll" class="grid grid-cols-2 gap-2">
            <UButton v-if="!loadingAll" color="gray" variant="soft" block size="xs" :loading="!!pending" @click="loadMore">{{ t('world.loadMore') }}</UButton>
            <UButton color="gray" variant="outline" block size="xs" :disabled="!!pending && !loadingAll" @click="loadingAll ? stopLoading() : loadAll()">{{ t(loadingAll ? 'world.stopLoading' : 'world.loadAll') }}</UButton>
          </div>
          <div class="flex items-center justify-between text-xs text-ink-muted"><span>{{ t('world.showLinks') }}</span><UToggle v-model="showLinks" :aria-label="t('world.showLinks')" size="xs" /></div>
          <p class="text-2xs leading-relaxed text-ink-muted">{{ t('world.scopeHint') }}</p>
        </footer>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { worldTypes, type WorldType, type WorldEdge } from '~/composables/useWorldGraph'
import { worldColors, worldIcons } from '~/utils/worldGraph'
import WorldViewSwitch from './WorldViewSwitch.vue'
import WorldCanvas from './WorldCanvas.vue'
const { t, locale } = useI18n()
const { showTopBanner, bannerHeight } = useTopBanner()
const { nodes, edges, usageLoaded, pending, initializing, loadingAll, failures, next, expanded, refresh, load, expand, loadMore, loadAll, stopLoading, retry, clearMemory } = useWorldGraph()
const enabled = ref(new Set<WorldType>(worldTypes))
const showUsage = ref(false)
const mine = ref(false)
const inspectedEdge = ref('')
const reportsById = computed(() => new Map(nodes.value.filter(n => n.type === 'report').map(n => [n.resource_id, n])))
const graphEdges = computed(() => edges.value.flatMap(edge => {
  if (!edge.report_ids) return [edge]
  if (!showUsage.value && edge.source !== selectedId.value && edge.target !== selectedId.value) return []
  if (!mine.value) return [edge]
  const report_ids = edge.report_ids.filter(id => reportsById.value.get(id)?.meta.mine)
  return report_ids.length ? [{ ...edge, report_ids, report_count: report_ids.length }] : []
}))
const memory = ref(false); const showLinks = ref(true); const search = ref('')
const panel = ref<'layers' | 'browse'>('layers'); const selectedId = ref<string | null>(null)
const map = ref<InstanceType<typeof WorldCanvas>>()
const browseLimit = ref(100); const neighborLimit = ref(30)
const number = (n: number) => new Intl.NumberFormat(locale.value).format(n)
const scopedNodes = computed(() => mine.value ? nodes.value.filter(n => n.type !== 'report' || n.meta.mine) : nodes.value)
const counts = computed(() => {
  const result: Partial<Record<WorldType, number>> = {}
  for (const n of scopedNodes.value) result[n.type] = (result[n.type] || 0) + 1
  return result
})
const visibleNodes = computed(() => scopedNodes.value.filter(n => enabled.value.has(n.type) && (n.type !== 'memory' || memory.value) && (!search.value || `${n.label} ${n.description}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase()))))
const visibleIds = computed(() => new Set(visibleNodes.value.map(n => n.id)))
const visibleEdges = computed(() => graphEdges.value.filter(e => visibleIds.value.has(e.source) && visibleIds.value.has(e.target)))
const selected = computed(() => nodes.value.find(n => n.id === selectedId.value) || null)
const detailFields = computed(() => Object.entries(selected.value?.meta || {})
  .filter(([key, value]) => ['source_type', 'columns', 'status', 'load_mode', 'policy', 'mode', 'handle'].includes(key) && value !== null && value !== undefined)
  .map(([key, value]) => ({ key, value: typeof value === 'number' ? number(value) : String(value) })))
const neighbors = computed(() => {
  const byId = new Map(nodes.value.map(n => [n.id, n]))
  return graphEdges.value.filter(e => e.source === selectedId.value || e.target === selectedId.value)
    .map(edge => ({ edge, node: byId.get(edge.source === selectedId.value ? edge.target : edge.source)! }))
    .filter(({node}) => !mine.value || node.type !== 'report' || node.meta.mine)
})
watch([showUsage, selectedId, initializing], () => {
  if (initializing.value || usageLoaded.value.has('all')) return
  if (showUsage.value) load({ section: 'usage' })
  else if (selectedId.value?.startsWith('agent:')) {
    const agent_id = selectedId.value.slice('agent:'.length)
    if (!usageLoaded.value.has(agent_id)) load({ section: 'usage', agent_id })
  }
})
function inspectUsage(edge: WorldEdge) {
  select(selectedId.value === edge.target ? edge.target : edge.source)
  neighborLimit.value = Math.max(neighborLimit.value, neighbors.value.findIndex(n => n.edge.id === edge.id) + 1)
  inspectedEdge.value = edge.id
}
function select(id: string | null) {
  inspectedEdge.value = ''
  if (id && !visibleIds.value.has(id)) {
    search.value = ''
    const node = nodes.value.find(n => n.id === id)
    if (node) enabled.value = new Set([...enabled.value, node.type])
  }
  selectedId.value = id
  neighborLimit.value = 30
}
function toggleType(type: WorldType) { const next = new Set(enabled.value); next.has(type) ? next.delete(type) : next.add(type); enabled.value = next }
function toggleAll() { enabled.value = enabled.value.size === worldTypes.length ? new Set() : new Set(worldTypes) }
function resetFilters() { enabled.value = new Set(worldTypes); search.value = '' }
async function refreshWorld() { select(null); await refresh(); if (memory.value) await load({ section: 'memory' }) }
watch(memory, async value => {
  if (value) {
    enabled.value = new Set([...enabled.value, 'memory'])
    await load({ section: 'memory' })
    if (!memory.value) clearMemory()
    else { await nextTick(); if (!selectedId.value) map.value?.fit() }
  } else clearMemory()
})
watch(visibleIds, ids => { if (selectedId.value && !ids.has(selectedId.value)) select(null) })
watch(search, value => { browseLimit.value = 100; if (value) panel.value = 'browse' })
watch(initializing, async value => { if (!value && !selectedId.value) { await nextTick(); map.value?.fit() } })
watch(loadingAll, async value => { if (!value && !selectedId.value) { await nextTick(); map.value?.fit() } })
</script>

<style scoped>
.world-view { height: calc(100dvh - var(--world-banner, 0px)); min-height: 560px; }
.world-stage { min-height: 420px; }
.world-panel { max-height: 46dvh; }
@media (max-width: 1023px) {
  .world-view { height: auto; }
  .world-stage { height: 500px; flex: none; }
  .world-panel { height: 460px; max-height: none; }
}
@media (min-width: 1024px) { .world-panel { max-height: none; } }
@media (prefers-reduced-motion: no-preference) {
  .world-stage { animation: world-reveal 600ms ease-out both; }
  @keyframes world-reveal { from { opacity: 0; } to { opacity: 1; } }
}
</style>
