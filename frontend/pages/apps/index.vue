<template>
  <!-- The app launcher, "Shared with you": every published artifact this
       member can open -- apps (a host application may show them), dashboards
       and docs -- filterable by kind. It is the whole of BOW for an app user
       (a member without access_workspace, see composables/useWorkspaceAccess);
       anyone else who opens it also gets a link back to the workspace. Its own
       shell on purpose: no sidebar, nothing to hide. -->
  <div class="min-h-dvh bg-surface-subtle text-ink" data-testid="app-launcher" :data-state="state">
    <UNotifications />

    <!-- Top bar: no border, it sits on the page. -->
    <header class="mx-auto flex w-full max-w-6xl items-center gap-3 px-5 pt-5 sm:px-8">
      <img :src="orgIconUrl || '/assets/logo-128.png'" alt="" class="h-7 max-w-[96px] object-contain" />
      <span v-if="orgName" class="truncate text-sm font-medium text-ink-2" data-testid="app-launcher-org">{{ orgName }}</span>

      <div class="ms-auto flex items-center gap-1.5">
        <UButton v-if="workspaceAccess" to="/" color="gray" variant="ghost" size="sm"
                 trailing-icon="i-heroicons-arrow-right" data-testid="app-launcher-open-workspace">
          {{ t('apps.openWorkspace') }}
        </UButton>

        <UDropdown v-if="localeItems[0]?.length > 1" :items="localeItems" :popper="{ placement: 'bottom-end' }"
                   :ui="{ width: 'w-44', height: 'max-h-72' }">
          <UButton color="gray" variant="ghost" size="sm" icon="i-heroicons-language"
                   :aria-label="t('apps.language')" data-testid="app-launcher-language" />
        </UDropdown>

        <UDropdown :items="accountItems" :popper="{ placement: 'bottom-end' }" :ui="{ width: 'w-60' }">
          <button type="button" class="flex h-8 w-8 items-center justify-center rounded-full bg-surface-muted text-xs font-medium text-ink-2 hover:bg-gray-200 dark:hover:bg-gray-700"
                  :aria-label="t('apps.account')" data-testid="app-launcher-account">
            {{ initials }}
          </button>
          <template #account>
            <div class="min-w-0 text-start">
              <p class="truncate text-sm font-medium text-ink">{{ userName }}</p>
              <p v-if="userEmail && userEmail !== userName" class="truncate text-xs text-ink-muted">{{ userEmail }}</p>
            </div>
          </template>
        </UDropdown>
      </div>
    </header>

    <main class="mx-auto w-full max-w-6xl px-5 pb-16 pt-10 sm:px-8 sm:pt-14">
      <div class="flex flex-wrap items-end gap-4">
        <div class="min-w-0">
          <h1 class="text-2xl font-semibold tracking-tight text-ink">{{ t('apps.title') }}</h1>
          <p v-if="state === 'ready' && apps.length" class="mt-1 text-sm text-ink-muted">{{ t('apps.subtitle') }}</p>
        </div>
        <UInput v-if="searchable" v-model="search" icon="i-heroicons-magnifying-glass" size="sm"
                class="ms-auto w-full sm:w-64" :placeholder="t('apps.search')" data-testid="app-launcher-search" />
      </div>

      <!-- What kind to show. Hidden only while nothing at all is shared. -->
      <div v-if="state === 'ready' && (apps.length || searched || kind)" role="group" :aria-label="t('apps.filter.label')"
           class="mt-6 flex flex-wrap gap-1.5" data-testid="app-launcher-kinds">
        <button v-for="option in kindOptions" :key="option.value" type="button" :aria-pressed="kind === option.value"
                class="rounded-full px-3 py-1 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                :class="kind === option.value ? 'bg-ink text-ink-inverted' : 'bg-surface text-ink-2 ring-1 ring-black/5 hover:bg-surface-muted dark:ring-white/10'"
                :data-testid="`app-launcher-kind-${option.value || 'all'}`" @click="kind = option.value">
          {{ option.label }}
        </button>
      </div>

      <!-- Loading -->
      <div v-if="state === 'loading'" class="mt-8 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="n in 3" :key="n" class="overflow-hidden rounded-xl bg-surface">
          <div class="aspect-[16/10] bow-shimmer" />
          <div class="space-y-2 p-4"><div class="h-3.5 w-2/3 rounded bow-shimmer" /><div class="h-3 w-1/2 rounded bow-shimmer" /></div>
        </div>
      </div>

      <!-- Could not load -->
      <div v-else-if="state === 'failed'" class="mt-16 text-center" data-testid="app-launcher-failed">
        <p class="text-sm text-ink-2">{{ t('apps.loadFailed') }}</p>
        <UButton class="mt-4" color="gray" size="sm" @click="load">{{ t('apps.retry') }}</UButton>
      </div>

      <!-- Nothing shared yet -->
      <div v-else-if="!apps.length && !searched && !kind" class="mt-16 flex flex-col items-center text-center" data-testid="app-launcher-empty">
        <div class="flex h-14 w-14 items-center justify-center rounded-2xl bg-surface-muted">
          <UIcon name="i-heroicons-squares-2x2" class="h-7 w-7 text-ink-muted" aria-hidden="true" />
        </div>
        <h2 class="mt-5 text-base font-medium text-ink">{{ t('apps.empty.title') }}</h2>
        <p class="mt-1 max-w-sm text-sm text-ink-muted">{{ t('apps.empty.body') }}</p>
      </div>

      <template v-else>
        <p v-if="!apps.length && searched" class="mt-16 text-center text-sm text-ink-muted" data-testid="app-launcher-no-matches">
          {{ t('apps.noMatches', { query: searched }) }}
        </p>
        <p v-else-if="!apps.length" class="mt-16 text-center text-sm text-ink-muted" data-testid="app-launcher-empty-kind">
          {{ emptyKindText }}
        </p>
        <ul v-else class="mt-8 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3" data-testid="app-launcher-grid">
          <li v-for="app in apps" :key="app.id">
            <NuxtLink :to="withBack(`/d/${app.ref}`, 'apps')" data-testid="app-card" :data-app-id="app.id" :data-kind="app.kind"
                      class="group block overflow-hidden rounded-xl bg-surface shadow-sm ring-1 ring-black/5 transition hover:-translate-y-0.5 hover:shadow-md dark:ring-white/10">
              <div class="relative aspect-[16/10] overflow-hidden" :class="thumbnailOf(app) ? 'bg-surface-muted' : tileOf(app).bg">
                <DashboardThumbnail v-if="thumbnailOf(app)" :url="app.thumbnail_url!" :alt="titleOf(app)"
                                    class="h-full w-full object-cover object-top" data-testid="app-card-thumbnail"
                                    @error="failed[app.id] = true" />
                <div v-else class="flex h-full w-full items-center justify-center" data-testid="app-card-tile">
                  <span class="flex h-16 w-16 items-center justify-center rounded-2xl bg-white/70 text-2xl font-semibold dark:bg-black/20"
                        :class="tileOf(app).text" aria-hidden="true">{{ initialOf(app) }}</span>
                </div>
              </div>
              <div class="p-4 text-start">
                <div class="flex items-center gap-2">
                  <h2 class="min-w-0 flex-1 truncate text-sm font-medium text-ink" data-testid="app-card-title">{{ titleOf(app) }}</h2>
                  <span v-if="app.kind" class="shrink-0 rounded-full bg-surface-muted px-2 py-0.5 text-[11px] font-medium text-ink-muted"
                        data-testid="app-card-kind">{{ kindLabel(app.kind) }}</span>
                </div>
                <p class="mt-0.5 truncate text-xs text-ink-muted">{{ app.description || applicationsOf(app) }}</p>
              </div>
            </NuxtLink>
          </li>
        </ul>
        <!-- The list is paged on the server: everything the reader may open is
             reachable, by this button or by searching, in each kind too. -->
        <div v-if="nextAfter !== null" class="mt-8 flex justify-center">
          <UButton color="white" size="sm" :loading="loadingMore" data-testid="app-launcher-more" @click="loadMore">
            {{ t('apps.showMore') }}
          </UButton>
        </div>
      </template>

      <!-- An app user's way to ask for the workspace. -->
      <div v-if="state === 'ready' && !workspaceAccess" class="mt-14 flex flex-col items-center gap-3 text-center"
           data-testid="app-launcher-access">
        <template v-if="accessRequested">
          <p class="flex items-center gap-1.5 text-sm text-ink-muted" data-testid="app-launcher-access-sent">
            <UIcon name="i-heroicons-check-circle" class="h-4 w-4 text-green-600" aria-hidden="true" />
            {{ t('apps.requestAccess.sent') }}
          </p>
        </template>
        <template v-else>
          <p class="text-sm text-ink-muted">{{ t('apps.requestAccess.prompt') }}</p>
          <UButton color="white" size="sm" :loading="requesting" data-testid="app-launcher-request-access"
                   @click="requestAccess">
            {{ t('apps.requestAccess.button') }}
          </UButton>
        </template>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import DashboardThumbnail from '~/components/dashboards/DashboardThumbnail.vue'
import { withBack } from '~/utils/dashboardBack'

definePageMeta({ layout: false })

type Kind = 'app' | 'dashboard' | 'doc'
const KINDS: Kind[] = ['app', 'dashboard', 'doc']

interface AppCard {
  id: string
  ref: string
  title: string | null
  description: string | null
  mode: string | null
  kind: Kind | null
  thumbnail_url: string | null
  applications: string[]
}

const SEARCH_FROM = 8

const { t, locale } = useI18n()
const { locale: globalLocale } = useI18n({ useScope: 'global' })
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
const { data: session, signOut } = useAuth()
const { organization, ensureOrganization, setOrganization } = useOrganization()
const toast = useToast()
const nuxtApp = useNuxtApp() as any
const route = useRoute()
const router = useRouter()

useHead(() => ({ title: t('apps.title') }))

const state = ref<'loading' | 'ready' | 'failed'>('loading')
const apps = ref<AppCard[]>([])
const workspaceAccess = ref(false)
const accessRequested = ref(false)
const requesting = ref(false)
const search = ref('')
const failed = reactive<Record<string, boolean>>({})

const activeOrg = computed(() =>
  ((session.value as any)?.organizations || []).find((o: any) => o.id === organization.value?.id) || null)
const orgName = computed(() => activeOrg.value?.name || organization.value?.name || '')
const orgIconUrl = computed<string | null>(() => activeOrg.value?.icon_url || null)
const userName = computed(() => (session.value as any)?.name || (session.value as any)?.email || '')
const userEmail = computed(() => (session.value as any)?.email || '')
const initials = computed(() => {
  const parts = String(userName.value).trim().split(/[\s@._-]+/).filter(Boolean)
  return ((parts[0]?.[0] || '') + (parts.length > 1 ? parts[parts.length - 1]![0] : '')).toUpperCase() || '?'
})

const titleOf = (app: AppCard) => app.title || t('apps.untitled')
const initialOf = (app: AppCard) => (Array.from(titleOf(app).trim())[0] || '?').toUpperCase()
const thumbnailOf = (app: AppCard) => !!app.thumbnail_url && !failed[app.id]
const applicationsOf = (app: AppCard) =>
  app.applications?.length ? t('apps.inApplication', { name: app.applications.join(', ') }) : ''

// A stable tint per app, for cards without a picture (or whose picture is
// withheld from this viewer because it shows the publisher's data).
const TILES = [
  { bg: 'bg-blue-50 dark:bg-blue-950', text: 'text-blue-600 dark:text-blue-300' },
  { bg: 'bg-violet-50 dark:bg-violet-950', text: 'text-violet-600 dark:text-violet-300' },
  { bg: 'bg-emerald-50 dark:bg-emerald-950', text: 'text-emerald-600 dark:text-emerald-300' },
  { bg: 'bg-amber-50 dark:bg-amber-950', text: 'text-amber-600 dark:text-amber-300' },
  { bg: 'bg-rose-50 dark:bg-rose-950', text: 'text-rose-600 dark:text-rose-300' },
  { bg: 'bg-cyan-50 dark:bg-cyan-950', text: 'text-cyan-600 dark:text-cyan-300' },
]
function tileOf(app: AppCard) {
  let hash = 0
  for (const ch of app.id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return TILES[hash % TILES.length]!
}

// The kind filter: in the URL (?kind=), so back from a dashboard and a
// reload return to the same view. '' is everything.
const kindOf = (raw: unknown): Kind | '' => (KINDS.includes(raw as Kind) ? raw as Kind : '')
const kind = ref<Kind | ''>(kindOf(route.query.kind))
const kindOptions = computed(() => [
  { value: '' as const, label: t('apps.filter.all') },
  { value: 'app' as const, label: t('apps.filter.apps') },
  { value: 'dashboard' as const, label: t('apps.filter.dashboards') },
  { value: 'doc' as const, label: t('apps.filter.docs') },
])
function kindLabel(value: Kind) {
  return { app: t('apps.kind.app'), dashboard: t('apps.kind.dashboard'), doc: t('apps.kind.doc') }[value]
}
const emptyKindText = computed(() => kind.value
  ? { app: t('apps.emptyKind.app'), dashboard: t('apps.emptyKind.dashboard'), doc: t('apps.emptyKind.doc') }[kind.value]
  : '')

// The search runs on the server (titles and descriptions), so it finds an
// app on a page not loaded yet. `searched` is the query the list answers.
const searched = ref('')
const nextAfter = ref<number | null>(null)
const loadingMore = ref(false)
const everSearchable = ref(false)
const searchable = computed(() => everSearchable.value || !!searched.value)
let request = 0

async function fetchPage(after: number) {
  const q = search.value.trim()
  const { data, error } = await useMyFetch('/apps', {
    query: { ...(q ? { q } : {}), ...(kind.value ? { kind: kind.value } : {}), ...(after ? { after } : {}) },
  })
  if (error.value || !data.value) throw error.value || new Error('empty')
  return { body: data.value as any, q }
}

async function load(initial = true) {
  const mine = ++request
  if (initial) state.value = 'loading'
  try {
    await ensureOrganization()
    const { body, q } = await fetchPage(0)
    if (mine !== request) return  // a newer search answered first
    apps.value = body.apps || []
    nextAfter.value = body.next ?? null
    searched.value = q
    if (!q && (apps.value.length > SEARCH_FROM || nextAfter.value !== null)) everSearchable.value = true
    workspaceAccess.value = !!body.workspace_access
    accessRequested.value = !!body.access_requested
    state.value = 'ready'
  } catch {
    if (mine === request) state.value = 'failed'
  }
}

async function loadMore() {
  if (nextAfter.value === null || loadingMore.value) return
  const mine = request
  loadingMore.value = true
  try {
    const { body } = await fetchPage(nextAfter.value)
    if (mine !== request) return
    const seen = new Set(apps.value.map(app => app.id))
    apps.value = [...apps.value, ...(body.apps || []).filter((app: AppCard) => !seen.has(app.id))]
    nextAfter.value = body.next ?? null
  } catch {
    toast.add({ title: t('apps.loadFailed'), color: 'red' })
  } finally {
    loadingMore.value = false
  }
}

let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(search, () => {
  // The input changed: whatever is in flight answers a query the box no
  // longer shows, so drop it now (not only once the debounce fires), then
  // always ask again for what the box holds.
  ++request
  loadingMore.value = false
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { load(false) }, 250)
})
onBeforeUnmount(() => { if (searchTimer) clearTimeout(searchTimer) })

watch(kind, (value) => {
  router.replace({ query: { ...route.query, kind: value || undefined } })
  // As for the search: what is in flight answers another filter.
  ++request
  loadingMore.value = false
  load(false)
})

async function requestAccess() {
  requesting.value = true
  try {
    const { data, error } = await useMyFetch('/apps/access-request', { method: 'POST' })
    if (error.value) throw error.value
    const body = data.value as any
    if (body?.workspace_access) {
      // Granted in the meantime: reload into the workspace.
      window.location.href = '/'
      return
    }
    accessRequested.value = true
  } catch {
    toast.add({ title: t('apps.requestAccess.failed'), color: 'red' })
  } finally {
    requesting.value = false
  }
}

// ── language (per browser, as everywhere else) and the organization's default ──
const LOCALE_NATIVE_LABELS: Record<string, string> = {
  en: 'English', es: 'Español', he: 'עברית', fr: 'Français', sv: 'Svenska',
  ar: 'العربية', ru: 'Русский', de: 'Deutsch', pt: 'Português (Brasil)', it: 'Italiano',
}
const enabledLocales = ref<string[]>([])

async function setLocale(code: string) {
  if (typeof nuxtApp.$setLocale === 'function') await nuxtApp.$setLocale(code)
}

const localeItems = computed(() => [enabledLocales.value.map(code => ({
  label: LOCALE_NATIVE_LABELS[code] || code,
  icon: code === locale.value ? 'i-heroicons-check' : undefined,
  click: () => setLocale(code),
}))])

async function applyOrganizationLocale() {
  try {
    const { data } = await useMyFetch('/organization/locale')
    const body = data.value as any
    enabledLocales.value = body?.enabled_locales || []
    let stored: string | null = null
    try { stored = localStorage.getItem('bow.locale') } catch {}
    // A personal choice wins; otherwise follow the organization.
    // Not through $setLocale: that would store the organization's default
    // as this person's own choice.
    const effective = body?.effective_locale
    if (!stored && effective && effective !== locale.value
        && typeof nuxtApp.$loadLocaleMessages === 'function' && await nuxtApp.$loadLocaleMessages(effective)) {
      ;(globalLocale as any).value = effective
      document.documentElement.setAttribute('lang', effective)
      document.documentElement.setAttribute('dir', RTL_LOCALES.has(effective) ? 'rtl' : 'ltr')
    }
  } catch {
    // non-fatal: the page stays in the current language
  }
}

async function signOff() {
  await signOut({ callbackUrl: '/' })
  window.location.href = '/users/sign-in'
}

// Someone who belongs to several organizations may have the workspace in
// another one: without the switcher here they could never reach it.
const organizations = computed<any[]>(() => (session.value as any)?.organizations || [])
const accountItems = computed(() => [
  [{ label: userName.value, slot: 'account', disabled: true }],
  ...(organizations.value.length > 1 ? [organizations.value.map(org => ({
    label: org.name,
    icon: org.id === organization.value?.id ? 'i-heroicons-check' : 'i-heroicons-building-office',
    click: () => { if (org.id !== organization.value?.id) setOrganization(org.id) },
  }))] : []),
  [{ label: t('apps.signOut'), icon: 'i-heroicons-arrow-right-on-rectangle', click: signOff }],
])

onMounted(async () => {
  await load()
  applyOrganizationLocale()
})
</script>
