<template>
  <!-- The app launcher: the apps (published artifacts a host application may
       show) this member can open. It is the whole of BOW for an app user (a
       member without access_workspace, see composables/useWorkspaceAccess)
       and an "Apps" page for everyone else, who also get a link back to the
       workspace. Its own shell on purpose: no sidebar, nothing to hide. -->
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
        <UInput v-if="apps.length > SEARCH_FROM" v-model="search" icon="i-heroicons-magnifying-glass" size="sm"
                class="ms-auto w-full sm:w-64" :placeholder="t('apps.search')" data-testid="app-launcher-search" />
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
      <div v-else-if="!apps.length" class="mt-16 flex flex-col items-center text-center" data-testid="app-launcher-empty">
        <div class="flex h-14 w-14 items-center justify-center rounded-2xl bg-surface-muted">
          <UIcon name="i-heroicons-squares-2x2" class="h-7 w-7 text-ink-muted" aria-hidden="true" />
        </div>
        <h2 class="mt-5 text-base font-medium text-ink">{{ t('apps.empty.title') }}</h2>
        <p class="mt-1 max-w-sm text-sm text-ink-muted">{{ t('apps.empty.body') }}</p>
      </div>

      <template v-else>
        <p v-if="!shown.length" class="mt-16 text-center text-sm text-ink-muted" data-testid="app-launcher-no-matches">
          {{ t('apps.noMatches', { query: search.trim() }) }}
        </p>
        <ul v-else class="mt-8 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3" data-testid="app-launcher-grid">
          <li v-for="app in shown" :key="app.id">
            <NuxtLink :to="`/d/${app.ref}`" data-testid="app-card" :data-app-id="app.id"
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
                <h2 class="truncate text-sm font-medium text-ink" data-testid="app-card-title">{{ titleOf(app) }}</h2>
                <p class="mt-0.5 truncate text-xs text-ink-muted">{{ app.description || applicationsOf(app) }}</p>
              </div>
            </NuxtLink>
          </li>
        </ul>
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

definePageMeta({ layout: false })

interface AppCard {
  id: string
  ref: string
  title: string | null
  description: string | null
  mode: string | null
  thumbnail_url: string | null
  applications: string[]
}

const SEARCH_FROM = 8

const { t, locale } = useI18n()
const { locale: globalLocale } = useI18n({ useScope: 'global' })
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
const { data: session, signOut } = useAuth()
const { organization, ensureOrganization } = useOrganization()
const toast = useToast()
const nuxtApp = useNuxtApp() as any

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

const shown = computed(() => {
  const q = search.value.trim().toLocaleLowerCase(locale.value)
  if (!q) return apps.value
  return apps.value.filter(app =>
    [titleOf(app), app.description || '', ...(app.applications || [])]
      .some(text => text.toLocaleLowerCase(locale.value).includes(q)))
})

async function load() {
  state.value = 'loading'
  try {
    await ensureOrganization()
    const { data, error } = await useMyFetch('/apps')
    if (error.value || !data.value) throw error.value || new Error('empty')
    const body = data.value as any
    apps.value = body.apps || []
    workspaceAccess.value = !!body.workspace_access
    accessRequested.value = !!body.access_requested
    state.value = 'ready'
  } catch {
    state.value = 'failed'
  }
}

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

const accountItems = computed(() => [
  [{ label: userName.value, slot: 'account', disabled: true }],
  [{ label: t('apps.signOut'), icon: 'i-heroicons-arrow-right-on-rectangle', click: signOff }],
])

onMounted(async () => {
  await load()
  applyOrganizationLocale()
})
</script>
