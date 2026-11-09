<template>
  <div>
    <UTooltip :text="$t('reports.view.groupAndSort')" :popper="{ placement: 'top' }">
      <button
        ref="triggerEl"
        type="button"
        data-testid="reports-view-menu-trigger"
        @click.stop="toggle"
        :aria-label="$t('reports.view.groupAndSort')"
        :aria-haspopup="true"
        :aria-expanded="open"
        class="flex items-center justify-center w-5 h-5 rounded text-ink-muted hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
        :class="open || isCustomized ? 'text-gray-700 dark:text-gray-200' : ''"
      >
        <!-- Claude's "Group and sort" glyph: three centered, decreasing
             horizontal lines. Inline SVG so it matches the reference exactly
             rather than approximating with a heroicon. -->
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" class="w-3.5 h-3.5" aria-hidden="true">
          <line x1="4" y1="7" x2="20" y2="7" />
          <line x1="7" y1="12" x2="17" y2="12" />
          <line x1="10" y1="17" x2="14" y2="17" />
        </svg>
      </button>
    </UTooltip>

    <!-- Teleported to body so it escapes the sidebar's transform/overflow
         clipping, like the existing row/project menus in default.vue. -->
    <Teleport to="body">
      <div
        v-if="open"
        class="fixed inset-0 z-popover"
        @click="close"
        @contextmenu.prevent="close"
      >
        <!-- Main panel: a row per setting, current value + chevron on the end. -->
        <div
          class="absolute w-56 py-1 bg-white dark:bg-gray-900 border border-line rounded-lg shadow-lg text-sm"
          :style="panelStyle"
          @click.stop
        >
          <button
            v-for="row in rows"
            :key="row.key"
            type="button"
            class="flex items-center justify-between gap-3 w-full px-3 py-1.5 text-start"
            :class="openSub === row.key ? 'bg-gray-100 dark:bg-gray-800' : 'hover:bg-gray-100 dark:hover:bg-gray-800'"
            @mouseenter="openSubmenu(row.key, $event)"
            @focus="openSubmenu(row.key, $event)"
            @click.stop="openSubmenu(row.key, $event)"
          >
            <span class="text-ink shrink-0">{{ row.label }}</span>
            <span class="flex items-center gap-1 text-ink-muted min-w-0">
              <span class="truncate">{{ row.valueLabel }}</span>
              <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 shrink-0 rtl-flip" />
            </span>
          </button>
        </div>

        <!-- Submenu flyout: the options for the hovered/focused row. -->
        <div
          v-if="openSub"
          class="absolute w-48 py-1 bg-white dark:bg-gray-900 border border-line rounded-lg shadow-lg text-sm"
          :style="subStyle"
          @click.stop
          @mouseenter="keepSub = true"
        >
          <button
            v-for="opt in subOptions"
            :key="opt.value"
            type="button"
            class="flex items-center justify-between gap-3 w-full px-3 py-1.5 text-start text-ink hover:bg-gray-100 dark:hover:bg-gray-800"
            @click="selectOption(opt.value)"
          >
            <span class="truncate">{{ opt.label }}</span>
            <UIcon v-if="opt.active" name="i-heroicons-check" class="w-4 h-4 shrink-0 text-primary-500" />
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
/*
 * "Group and sort" control for the sidebar Reports list (reference: the Claude
 * desktop sidebar). A filter icon in the Reports section header opens a small
 * menu whose rows — Group by / Sort by / Direction — each show the current
 * value and open a submenu flyout of the options (checkmark on the active one).
 *
 * Positioning is hand-rolled against the trigger/row rects and teleported to
 * body, matching the row-action menu in layouts/default.vue, because the
 * sidebar clips its own overflow. Flyout side follows the writing direction.
 */
import type { SidebarGroupBy, SidebarSortBy, SidebarDirection } from '~/composables/useSidebarReportsView'

const { t } = useI18n()
const {
  groupBy, sortBy, direction,
  setGroupBy, setSortBy, setDirection,
  isCustomized, hydrate,
} = useSidebarReportsView()

const triggerEl = ref<HTMLElement | null>(null)
const open = ref(false)
type Row = 'group' | 'sort' | 'dir'
const openSub = ref<Row | null>(null)
const keepSub = ref(false)

const PANEL_W = 224 // w-56
const SUB_W = 192 // w-48
const panelPos = ref({ top: 0, left: 0, right: 0, rtl: false })
const subPos = ref({ top: 0, left: 0 })

const panelStyle = computed(() => ({ top: `${panelPos.value.top}px`, left: `${panelPos.value.left}px` }))
const subStyle = computed(() => ({ top: `${subPos.value.top}px`, left: `${subPos.value.left}px` }))

const groupLabel = computed(() => t(`reports.view.group.${groupBy.value}`))
const sortLabel = computed(() => t(`reports.view.sort.${sortBy.value}`))
const dirLabel = computed(() => t(`reports.view.dir.${direction.value}`))

const rows = computed(() => [
  { key: 'group' as Row, label: t('reports.view.groupBy'), valueLabel: groupLabel.value },
  { key: 'sort' as Row, label: t('reports.view.sortBy'), valueLabel: sortLabel.value },
  { key: 'dir' as Row, label: t('reports.view.direction'), valueLabel: dirLabel.value },
])

const subOptions = computed(() => {
  if (openSub.value === 'group') {
    return (['date', 'status', 'none'] as SidebarGroupBy[]).map(v => ({
      value: v, label: t(`reports.view.group.${v}`), active: groupBy.value === v,
    }))
  }
  if (openSub.value === 'sort') {
    return (['updated', 'created', 'name'] as SidebarSortBy[]).map(v => ({
      value: v, label: t(`reports.view.sort.${v}`), active: sortBy.value === v,
    }))
  }
  if (openSub.value === 'dir') {
    return (['desc', 'asc'] as SidebarDirection[]).map(v => ({
      value: v, label: t(`reports.view.dir.${v}`), active: direction.value === v,
    }))
  }
  return []
})

function selectOption(value: string) {
  if (openSub.value === 'group') setGroupBy(value as SidebarGroupBy)
  else if (openSub.value === 'sort') setSortBy(value as SidebarSortBy)
  else if (openSub.value === 'dir') setDirection(value as SidebarDirection)
  // Close on pick — one choice, menu dismisses (reopen to change another).
  close()
}

function isRtl(): boolean {
  return typeof document !== 'undefined' && document.documentElement.dir === 'rtl'
}

function openMenu() {
  const el = triggerEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const rtl = isRtl()
  // Open beside the sidebar (into the content area), not straight down, so the
  // report list below the header stays visible — like Claude. Flip to the other
  // side if it would overflow.
  let left = rtl ? r.left - PANEL_W - 6 : r.right + 6
  if (!rtl && left + PANEL_W > window.innerWidth - 8) left = r.left - PANEL_W - 6
  if (rtl && left < 8) left = r.right + 6
  left = Math.max(8, Math.min(left, window.innerWidth - PANEL_W - 8))
  const top = Math.max(8, Math.min(r.top - 4, window.innerHeight - 220))
  panelPos.value = { top, left, right: r.right, rtl }
  open.value = true
  openSub.value = null
}

function openSubmenu(row: Row, ev: Event) {
  openSub.value = row
  const rowEl = ev.currentTarget as HTMLElement
  const r = rowEl.getBoundingClientRect()
  const rtl = panelPos.value.rtl
  const panelLeft = panelPos.value.left
  const panelRight = panelLeft + PANEL_W
  // Flyout to the end side; flip to the other side if it would overflow.
  let left = rtl ? panelLeft - SUB_W - 2 : panelRight + 2
  if (!rtl && left + SUB_W > window.innerWidth - 8) left = panelLeft - SUB_W - 2
  if (rtl && left < 8) left = panelRight + 2
  left = Math.max(8, Math.min(left, window.innerWidth - SUB_W - 8))
  const top = Math.max(8, Math.min(r.top - 5, window.innerHeight - 180))
  subPos.value = { top, left }
}

function close() {
  open.value = false
  openSub.value = null
}
function toggle() {
  if (open.value) close()
  else openMenu()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') { close(); triggerEl.value?.focus() }
}

watch(open, (v) => {
  if (typeof window === 'undefined') return
  if (v) {
    window.addEventListener('keydown', onKeydown)
    // A detached, stale menu is worse than a closed one: resizing moves the
    // anchor, so close rather than try to re-anchor mid-interaction.
    window.addEventListener('resize', close)
  } else {
    window.removeEventListener('keydown', onKeydown)
    window.removeEventListener('resize', close)
  }
})

onMounted(() => { hydrate() })
onBeforeUnmount(() => {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', close)
})
</script>
