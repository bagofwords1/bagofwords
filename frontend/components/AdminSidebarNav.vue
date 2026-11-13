<template>
  <nav :class="['admin-nav flex-1 flex flex-col min-h-0', animate ? 'admin-nav--animate' : '']" :aria-label="$t('nav.admin')">
    <!-- Back to the app. A header row, not a list item: it leaves the console. -->
    <button
      type="button"
      @click="goBack"
      :class="[
        'admin-nav-in group flex items-center w-full rounded-md px-2.5 py-1.5 text-sm text-ink-muted hover:text-ink hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors',
        collapsed ? 'justify-center' : 'gap-2.5'
      ]"
      :style="{ '--i': 0 }"
      :aria-label="$t('nav.backToApp')"
    >
      <UTooltip v-if="collapsed" :text="$t('nav.backToApp')" :popper="{ placement: tooltipPlacement }">
        <span class="flex items-center justify-center w-5 h-5">
          <UIcon name="i-heroicons-arrow-left" class="w-4 h-4" />
        </span>
      </UTooltip>
      <template v-else>
        <!-- The icon itself is auto-mirrored under RTL (assets/css/rtl.css,
             rule 6); the hover nudge sits on the wrapper so the two
             transforms don't overwrite each other. -->
        <span class="flex items-center justify-center w-5 h-5 transition-transform duration-fast group-hover:-translate-x-0.5 rtl:group-hover:translate-x-0.5">
          <UIcon name="i-heroicons-arrow-left" class="w-4 h-4" />
        </span>
        <span v-if="showText">{{ $t('nav.backToApp') }}</span>
      </template>
    </button>

    <div
      v-if="showText && !collapsed"
      class="admin-nav-in px-2.5 pt-3 pb-2 mb-1 border-b border-line"
      :style="{ '--i': 1 }"
    >
      <span class="text-sm font-semibold text-ink">{{ $t('nav.admin') }}</span>
    </div>
    <div v-else class="my-2 mx-2 border-b border-line" />

    <ul ref="listEl" class="relative font-normal text-sm !ps-0 min-h-0 overflow-y-auto">
      <!-- The single active highlight. It glides between rows instead of
           each row painting its own background, which is what makes moving
           around the console feel continuous. -->
      <span
        aria-hidden="true"
        :class="[
          'admin-nav-pill absolute left-0 top-0 rounded-md bg-gray-200/70 dark:bg-gray-800 pointer-events-none',
          pillReady ? 'admin-nav-pill--animated' : ''
        ]"
        :style="pillStyle"
      />

      <li
        v-for="(item, idx) in visibleItems"
        :key="item.name"
        class="relative admin-nav-in"
        :style="{ '--i': idx + 2 }"
      >
        <NuxtLink
          :to="`/settings/${item.name}`"
          :data-admin-key="item.children?.length ? undefined : item.name"
          :class="rowClass(isItemActive(item) && !item.children?.length, isItemActive(item))"
          :aria-current="isItemActive(item) && !item.children?.length ? 'page' : undefined"
        >
          <UTooltip v-if="collapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
            <span class="flex items-center justify-center w-5 h-5 text-base">
              <UIcon :name="item.icon" />
            </span>
          </UTooltip>
          <template v-else>
            <span class="flex items-center justify-center w-5 h-5 text-base">
              <UIcon :name="item.icon" />
            </span>
            <span v-if="showText" class="flex-1 truncate">{{ $t(item.label) }}</span>
            <!-- Disclosure-chevron idiom (assets/css/rtl.css, rule 6): closed,
                 the global RTL mirror points it at the inline end; open, it
                 opts out with rtl-own-transform so rotate-90 points it down. -->
            <UIcon
              v-if="showText && item.children?.length"
              name="i-heroicons-chevron-right"
              :class="[
                'w-3.5 h-3.5 text-ink-muted transition-transform duration-fast',
                isItemActive(item) ? 'rotate-90 rtl-own-transform' : ''
              ]"
            />
          </template>
        </NuxtLink>

        <!-- Sub-pages expand under their parent while you are in it. -->
        <Transition
          @enter="onExpand"
          @after-enter="onExpandDone"
          @leave="onCollapse"
          @after-leave="onExpandDone"
          :css="false"
        >
          <ul
            v-if="item.children?.length && isItemActive(item)"
            data-admin-children
            :class="['!ps-0 overflow-hidden', collapsed ? '' : 'ms-[1.125rem] ps-2 border-s border-line']"
          >
            <li v-for="child in item.children" :key="child.key">
              <NuxtLink
                :to="{ path: `/settings/${item.name}`, query: child.key === item.children[0].key ? {} : { tab: child.key } }"
                :data-admin-key="`${item.name}:${child.key}`"
                :class="rowClass(activeKey === `${item.name}:${child.key}`, false, true)"
                :aria-current="activeKey === `${item.name}:${child.key}` ? 'page' : undefined"
              >
                <UTooltip v-if="collapsed" :text="$t(child.label)" :popper="{ placement: tooltipPlacement }">
                  <span class="flex items-center justify-center w-5 h-5 text-sm">
                    <UIcon :name="child.icon" />
                  </span>
                </UTooltip>
                <span v-else-if="showText" class="truncate">{{ $t(child.label) }}</span>
              </NuxtLink>
            </li>
          </ul>
        </Transition>
      </li>
    </ul>
  </nav>
</template>

<script setup lang="ts">
import { useAdminNav, useAdminReturnPath, type AdminNavItem } from '~/composables/useAdminNav'

const props = defineProps<{
  collapsed: boolean
  showText: boolean
  tooltipPlacement: 'left' | 'right'
  // Play the staggered entrance. Off on first paint (a deep link or refresh
  // into /settings), on when the user actually switches into the console.
  animate?: boolean
}>()

const route = useRoute()
const router = useRouter()
const { visibleItems } = useAdminNav()
const returnPath = useAdminReturnPath()

const goBack = () => router.push(returnPath.value || '/')

const currentName = computed(() => route.path.replace(/^\/settings\/?/, '').split('/')[0] || '')

const isItemActive = (item: AdminNavItem) => currentName.value === item.name

// The row the highlight sits on: "<page>" or "<page>:<sub-tab>".
const activeKey = computed(() => {
  const item = visibleItems.value.find(i => i.name === currentName.value)
  if (!item) return null
  if (!item.children?.length) return item.name
  const tab = String(route.query.tab || '')
  const child = item.children.find(c => c.key === tab) || item.children[0]
  return `${item.name}:${child.key}`
})

const rowClass = (isActive: boolean, isParentOfActive: boolean, isChild = false) => [
  'relative flex items-center w-full rounded-md transition-colors',
  isChild ? 'px-2.5 py-1' : 'px-2.5 py-1.5',
  props.collapsed ? 'justify-center' : 'gap-2.5',
  isActive
    ? 'text-ink font-medium'
    : isParentOfActive
      ? 'text-ink font-medium hover:bg-gray-100 dark:hover:bg-gray-800/70'
      : 'text-ink-2 hover:text-ink hover:bg-gray-100 dark:hover:bg-gray-800/70',
]

// ---- Gliding highlight ------------------------------------------------------
// We measure the active row and move one absolutely-positioned pill to it.
// While the sub-list is expanding or collapsing, rows below it are still
// moving, so the measurement is corrected by the height the sub-list is about
// to gain or lose: the pill heads straight for the row's final position
// instead of chasing it.
// shallowRef: a deep ref unwraps the element type and stops it matching `Element`.
const listEl = shallowRef<HTMLElement | null>(null)
const pillTop = ref(0)
const pillLeft = ref(0)
const pillWidth = ref(0)
const pillHeight = ref(0)
const pillVisible = ref(false)
const pillReady = ref(false)

const pillStyle = computed(() => ({
  // Physical left/translate on purpose: the values are measured rects, which
  // are physical in both LTR and RTL.
  transform: `translate(${pillLeft.value}px, ${pillTop.value}px)`,
  width: `${pillWidth.value}px`,
  height: `${pillHeight.value}px`,
  opacity: pillVisible.value ? 1 : 0,
}))

type Pending = { el: HTMLElement; delta: number } | null
let pending: Pending = null

const measurePill = () => {
  const list = listEl.value
  if (!list || !activeKey.value) { pillVisible.value = false; return }
  const target = list.querySelector<HTMLElement>(`[data-admin-key="${activeKey.value}"]`)
  if (!target) { pillVisible.value = false; return }
  const rect = target.getBoundingClientRect()
  const listRect = list.getBoundingClientRect()
  let top = rect.top - listRect.top + list.scrollTop
  if (pending && !pending.el.contains(target)
      && (pending.el.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING)) {
    top += pending.delta
  }
  pillTop.value = top
  pillLeft.value = rect.left - listRect.left
  pillWidth.value = rect.width
  pillHeight.value = target.offsetHeight
  pillVisible.value = true
}

const EXPAND_MS = 160

const onExpand = (el: Element, done: () => void) => {
  const node = el as HTMLElement
  const full = node.scrollHeight
  node.style.height = '0px'
  pending = { el: node, delta: full }
  measurePill()
  // Force a reflow so the browser commits height 0 before animating.
  void node.offsetHeight
  node.style.transition = `height ${EXPAND_MS}ms cubic-bezier(0.2, 0, 0, 1)`
  node.style.height = `${full}px`
  setTimeout(done, EXPAND_MS)
}

const onCollapse = (el: Element, done: () => void) => {
  const node = el as HTMLElement
  const full = node.offsetHeight
  node.style.height = `${full}px`
  pending = { el: node, delta: -full }
  measurePill()
  void node.offsetHeight
  node.style.transition = `height ${EXPAND_MS}ms cubic-bezier(0.2, 0, 0, 1)`
  node.style.height = '0px'
  setTimeout(done, EXPAND_MS)
}

const onExpandDone = (el: Element) => {
  const node = el as HTMLElement
  node.style.height = ''
  node.style.transition = ''
  pending = null
  measurePill()
}

watch([activeKey, () => props.collapsed, () => props.showText], () => measurePill(), { flush: 'post' })

let resizeObserver: ResizeObserver | null = null
onMounted(() => {
  measurePill()
  // Turn the glide on only after the first placement, so the pill never
  // slides in from the top on page load.
  requestAnimationFrame(() => { pillReady.value = true })
  if (listEl.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => { if (!pending) measurePill() })
    resizeObserver.observe(listEl.value)
  }
})
onBeforeUnmount(() => resizeObserver?.disconnect())
</script>

<style scoped>
/* Rows rise into place one after another — 18ms apart so the whole list reads
   as a single gesture rather than a cascade you wait for. */
@keyframes admin-nav-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}
.admin-nav--animate .admin-nav-in {
  animation: admin-nav-in 180ms cubic-bezier(0.2, 0, 0, 1) both;
  animation-delay: calc(60ms + var(--i, 0) * 18ms);
}
/* The pill arrives with the rows, not ahead of them. Opacity only: its
   transform is its position, which an animated transform would override. */
@keyframes admin-nav-pill-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.admin-nav--animate .admin-nav-pill {
  animation: admin-nav-pill-in 180ms ease-out 140ms both;
}
.admin-nav-pill {
  will-change: transform;
}
.admin-nav-pill--animated {
  transition:
    transform 220ms cubic-bezier(0.3, 1.25, 0.5, 1),
    width 220ms cubic-bezier(0.2, 0, 0, 1),
    height 160ms cubic-bezier(0.2, 0, 0, 1),
    opacity 120ms ease-out;
}
</style>
