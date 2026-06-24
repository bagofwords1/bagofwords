<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-4xl' }">
    <div class="grid grid-cols-[210px_1fr] min-h-[480px] bg-white dark:bg-gray-900 rounded-lg overflow-hidden">
      <!-- Left column (smaller): user header + section nav -->
      <aside class="border-e border-gray-200/80 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 flex flex-col">
        <div class="px-4 pt-5 pb-4 flex flex-col items-center text-center gap-2">
          <img
            v-if="avatarUrl"
            :src="avatarUrl"
            alt=""
            class="w-12 h-12 rounded-full object-cover bg-gray-100 dark:bg-gray-800"
          />
          <div v-else class="flex items-center justify-center w-12 h-12 rounded-full bg-blue-500 text-white text-lg font-bold">
            {{ userInitial }}
          </div>
          <div class="min-w-0 w-full">
            <div class="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{{ currentUserName }}</div>
            <div class="text-[11px] text-gray-500 dark:text-gray-400 truncate">{{ currentUserEmail }}</div>
          </div>
        </div>

        <nav class="px-2 pb-3 space-y-0.5">
          <button
            v-for="item in navItems"
            :key="item.key"
            @click="activeTab = item.key"
            :class="[
              'flex items-center gap-2.5 w-full px-3 py-1.5 rounded-md text-[13px] transition-colors',
              activeTab === item.key
                ? 'bg-gray-200/70 dark:bg-gray-800 text-gray-900 dark:text-gray-100 font-medium'
                : 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/60'
            ]"
          >
            <UIcon :name="item.icon" class="w-4 h-4 shrink-0" />
            <span class="whitespace-nowrap">{{ item.label }}</span>
          </button>
        </nav>
      </aside>

      <!-- Right column: content -->
      <section class="relative flex flex-col min-w-0">
        <UButton
          class="absolute top-3 end-3 z-10"
          color="gray"
          variant="ghost"
          icon="i-heroicons-x-mark-20-solid"
          size="xs"
          @click="isOpen = false"
        />

        <div class="flex-1 overflow-y-auto px-6 py-5">
          <!-- General -->
          <div v-if="activeTab === 'general'" class="space-y-6">
            <div>
              <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.general.title') }}</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.general.subtitle') }}</p>
            </div>

            <!-- Avatar + full name -->
            <div class="flex items-center gap-4">
              <div class="relative shrink-0">
                <img
                  v-if="avatarUrl"
                  :src="avatarUrl"
                  alt=""
                  class="w-16 h-16 rounded-full object-cover bg-gray-100 dark:bg-gray-800"
                />
                <div
                  v-else
                  class="flex items-center justify-center w-16 h-16 rounded-full bg-blue-500 text-white text-2xl font-bold"
                >
                  {{ userInitial }}
                </div>
                <button
                  type="button"
                  :disabled="avatarBusy"
                  class="absolute -bottom-1 -end-1 w-6 h-6 rounded-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-sm flex items-center justify-center text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white disabled:opacity-50"
                  @click="selectAvatar"
                >
                  <Spinner v-if="avatarBusy" class="w-3 h-3 animate-spin" />
                  <UIcon v-else name="i-heroicons-camera" class="w-3.5 h-3.5" />
                </button>
                <input ref="avatarInput" type="file" accept="image/*" class="hidden" @change="onAvatarSelected" />
              </div>
              <div class="flex-1 min-w-0 space-y-1.5">
                <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('profile.general.fullName') }}</label>
                <UInput v-model="nameInput" :maxlength="50" :placeholder="$t('profile.general.fullNamePlaceholder')" />
                <button
                  v-if="avatarUrl"
                  type="button"
                  :disabled="avatarBusy"
                  class="text-[11px] text-gray-400 hover:text-red-500 disabled:opacity-50"
                  @click="removeAvatar"
                >
                  {{ $t('profile.general.removePhoto') }}
                </button>
              </div>
            </div>

            <!-- Email (read-only) -->
            <div class="space-y-1.5">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('profile.general.email') }}</label>
              <UInput :model-value="currentUserEmail" disabled />
            </div>

            <div>
              <UButton
                color="blue"
                size="sm"
                :loading="savingName"
                :disabled="!nameDirty || !nameInput.trim()"
                @click="saveName"
              >
                {{ $t('common.saveChanges') }}
              </UButton>
            </div>

            <!-- External platforms summary -->
            <div class="pt-2 border-t border-gray-100 dark:border-gray-800 space-y-3">
              <div>
                <div class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ $t('profile.general.platforms') }}</div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.general.platformsSubtitle') }}</p>
              </div>

              <div v-if="externalPlatforms.length" class="space-y-2">
                <div
                  v-for="(p, i) in externalPlatforms"
                  :key="i"
                  class="flex items-center gap-3 px-3 py-2 rounded-md border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900"
                >
                  <UIcon :name="platformMeta(p.platform_type).icon" class="w-4 h-4 text-gray-500 dark:text-gray-400 shrink-0" />
                  <div class="min-w-0 flex-1">
                    <div class="text-[13px] text-gray-800 dark:text-gray-200 truncate">{{ platformMeta(p.platform_type).label }}</div>
                    <div v-if="p.external_name || p.external_email" class="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                      {{ p.external_name || p.external_email }}
                    </div>
                  </div>
                  <UBadge
                    :color="p.is_verified ? 'green' : 'gray'"
                    variant="subtle"
                    size="xs"
                  >
                    {{ p.is_verified ? $t('profile.general.connected') : $t('profile.general.pending') }}
                  </UBadge>
                </div>
              </div>
              <p v-else class="text-xs text-gray-400 dark:text-gray-500 italic">{{ $t('profile.general.noPlatforms') }}</p>
            </div>
          </div>

          <!-- Custom Instructions -->
          <div v-else-if="activeTab === 'instructions'" class="space-y-4">
            <div>
              <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.instructions.title') }}</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.instructions.subtitle') }}</p>
            </div>

            <div v-if="instructionsLoading" class="py-6 flex justify-center">
              <Spinner class="w-5 h-5 text-gray-400" />
            </div>
            <template v-else>
              <UTextarea
                v-model="noteInput"
                :rows="8"
                :maxlength="500"
                :placeholder="$t('profile.instructions.placeholder')"
                autoresize
              />
              <div class="flex items-center justify-between">
                <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ noteInput.length }}/500</span>
                <UButton
                  color="blue"
                  size="sm"
                  :loading="savingNote"
                  :disabled="!noteDirty"
                  @click="saveNote"
                >
                  {{ $t('common.saveChanges') }}
                </UButton>
              </div>
            </template>
          </div>

          <!-- Usage -->
          <div v-else-if="activeTab === 'usage'" class="space-y-4">
            <div>
              <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.usage.title') }}</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.usage.subtitle') }}</p>
            </div>

            <!-- Real per-user counters only exist when the Usage Limits feature
                 is enabled; otherwise the quota source is empty and showing
                 zeros would be misleading, so we show an explicit notice. -->
            <template v-if="usage.enabled">
              <div class="grid grid-cols-3 gap-3">
                <div class="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 px-4 py-3">
                  <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ $t('profile.usage.tokens') }}</div>
                  <div class="text-lg font-semibold text-gray-900 dark:text-gray-100 mt-1">{{ formatNumber(usage.tokens.used) }}</div>
                  <div v-if="usage.tokens.limit" class="text-[11px] text-gray-400 dark:text-gray-500">/ {{ formatNumber(usage.tokens.limit) }}</div>
                </div>
                <div class="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 px-4 py-3">
                  <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ $t('profile.usage.queries') }}</div>
                  <div class="text-lg font-semibold text-gray-900 dark:text-gray-100 mt-1">{{ formatNumber(usage.queries.used) }}</div>
                  <div v-if="usage.queries.limit" class="text-[11px] text-gray-400 dark:text-gray-500">/ {{ formatNumber(usage.queries.limit) }}</div>
                </div>
                <div class="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 px-4 py-3">
                  <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ $t('profile.usage.data') }}</div>
                  <div class="text-lg font-semibold text-gray-900 dark:text-gray-100 mt-1">{{ formatBytes(usage.data_bytes.used) }}</div>
                  <div v-if="usage.data_bytes.limit" class="text-[11px] text-gray-400 dark:text-gray-500">/ {{ formatBytes(usage.data_bytes.limit) }}</div>
                </div>
              </div>
              <p class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('profile.usage.windowNote') }}</p>
            </template>

            <div v-else class="rounded-lg border border-dashed border-gray-200 dark:border-gray-700 bg-gray-50/60 dark:bg-gray-800/30 px-6 py-8 text-center">
              <UIcon name="i-heroicons-chart-bar" class="w-7 h-7 mx-auto text-gray-300 dark:text-gray-600" />
              <p class="mt-3 text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('profile.usage.disabledTitle') }}</p>
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-sm mx-auto">{{ $t('profile.usage.disabledNote') }}</p>
            </div>
          </div>

          <!-- Appearance -->
          <div v-else-if="activeTab === 'appearance'" class="space-y-4">
            <div>
              <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.appearance.title') }}</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.appearance.subtitle') }}</p>
            </div>

            <div class="space-y-2">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('profile.appearance.theme') }}</label>
              <div class="grid grid-cols-3 gap-2 max-w-md">
                <button
                  v-for="opt in themeOptions"
                  :key="opt.value"
                  @click="setTheme(opt.value)"
                  :class="[
                    'flex flex-col items-center gap-1.5 px-3 py-3 rounded-lg border transition-colors',
                    colorPreference === opt.value
                      ? 'border-blue-500 ring-1 ring-blue-500 bg-blue-50/50 dark:bg-blue-500/10'
                      : 'border-gray-200 dark:border-gray-800 hover:border-gray-300 dark:hover:border-gray-700'
                  ]"
                >
                  <UIcon :name="opt.icon" class="w-5 h-5 text-gray-600 dark:text-gray-300" />
                  <span class="text-xs text-gray-700 dark:text-gray-300">{{ opt.label }}</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const { t } = useI18n()
const toast = useToast()
const { data: currentUser, getSession } = useAuth()
const { organization } = useOrganization()
const colorMode = useColorMode()

const activeTab = ref<'general' | 'instructions' | 'usage' | 'appearance'>('general')

const navItems = computed(() => [
  { key: 'general', label: t('profile.nav.general'), icon: 'i-heroicons-user-circle' },
  { key: 'instructions', label: t('profile.nav.instructions'), icon: 'i-heroicons-sparkles' },
  { key: 'usage', label: t('profile.nav.usage'), icon: 'i-heroicons-chart-bar' },
  { key: 'appearance', label: t('profile.nav.appearance'), icon: 'i-heroicons-swatch' },
])

// --- User basics ---
const currentUserName = computed<string>(() => {
  const u = currentUser.value as any
  return u?.name || u?.email || 'User'
})
const currentUserEmail = computed<string>(() => (currentUser.value as any)?.email || '')
const userInitial = computed<string>(() => currentUserName.value.charAt(0).toUpperCase())

// --- General: name ---
const nameInput = ref('')
const nameDirty = computed(() => nameInput.value.trim() !== ((currentUser.value as any)?.name || '').trim())
const savingName = ref(false)

const syncNameInput = () => { nameInput.value = (currentUser.value as any)?.name || '' }

async function saveName() {
  const name = nameInput.value.trim()
  if (!name) return
  savingName.value = true
  try {
    const res = await useMyFetch('/users/me', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (res.status.value !== 'success') {
      throw new Error((res.error?.value as any)?.data?.detail || t('profile.general.saveFailed'))
    }
    await getSession({ force: true })
    syncNameInput()
    toast.add({ title: t('profile.general.saved'), color: 'green' })
  } catch (e: any) {
    toast.add({ title: e?.message || t('profile.general.saveFailed'), color: 'red' })
  } finally {
    savingName.value = false
  }
}

// --- General: avatar ---
const avatarInput = ref<HTMLInputElement | null>(null)
const avatarBusy = ref(false)
const avatarUrl = computed<string | null>(() => (currentUser.value as any)?.image_url || null)

function selectAvatar() {
  avatarInput.value?.click()
}

async function onAvatarSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 5 * 1024 * 1024) {
    toast.add({ title: t('profile.general.avatarTooLarge'), color: 'red' })
    input.value = ''
    return
  }
  avatarBusy.value = true
  try {
    const formData = new FormData()
    formData.append('avatar', file)
    const res = await useMyFetch('/users/me/avatar', { method: 'POST', body: formData })
    if (res.status.value !== 'success') {
      throw new Error((res.error?.value as any)?.data?.detail || t('profile.general.avatarFailed'))
    }
    await getSession({ force: true })
    toast.add({ title: t('profile.general.avatarUpdated'), color: 'green' })
  } catch (err: any) {
    toast.add({ title: err?.message || t('profile.general.avatarFailed'), color: 'red' })
  } finally {
    avatarBusy.value = false
    input.value = ''
  }
}

async function removeAvatar() {
  avatarBusy.value = true
  try {
    const res = await useMyFetch('/users/me/avatar', { method: 'DELETE' })
    if (res.status.value !== 'success') {
      throw new Error((res.error?.value as any)?.data?.detail || t('profile.general.avatarFailed'))
    }
    await getSession({ force: true })
  } catch (err: any) {
    toast.add({ title: err?.message || t('profile.general.avatarFailed'), color: 'red' })
  } finally {
    avatarBusy.value = false
  }
}

// --- General: external platforms ---
const externalPlatforms = computed<any[]>(() => (currentUser.value as any)?.external_user_mappings || [])

function platformMeta(type: string): { label: string; icon: string } {
  const map: Record<string, { label: string; icon: string }> = {
    slack: { label: 'Slack', icon: 'i-heroicons-chat-bubble-left-right' },
    teams: { label: 'Microsoft Teams', icon: 'i-heroicons-chat-bubble-left-right' },
    whatsapp: { label: 'WhatsApp', icon: 'i-heroicons-chat-bubble-oval-left' },
    email: { label: 'Email', icon: 'i-heroicons-envelope' },
    mcp: { label: 'MCP', icon: 'i-heroicons-command-line' },
    excel: { label: 'Excel', icon: 'i-heroicons-table-cells' },
  }
  return map[type] || { label: type, icon: 'i-heroicons-puzzle-piece' }
}

// --- Custom instructions (membership note) ---
const noteInput = ref('')
const noteOriginal = ref('')
const noteDirty = computed(() => noteInput.value.trim() !== noteOriginal.value.trim())
const instructionsLoading = ref(false)
const savingNote = ref(false)
const instructionsLoaded = ref(false)

async function loadInstructions() {
  if (instructionsLoaded.value) return
  instructionsLoading.value = true
  try {
    const res = await useMyFetch('/users/me/instructions')
    const note = (res.data?.value as any)?.note || ''
    noteInput.value = note
    noteOriginal.value = note
    instructionsLoaded.value = true
  } catch {
    // non-fatal; user can still type and save
  } finally {
    instructionsLoading.value = false
  }
}

async function saveNote() {
  savingNote.value = true
  try {
    const res = await useMyFetch('/users/me/instructions', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: noteInput.value.trim() || null }),
    })
    if (res.status.value !== 'success') {
      throw new Error((res.error?.value as any)?.data?.detail || t('profile.instructions.saveFailed'))
    }
    noteOriginal.value = (res.data?.value as any)?.note || ''
    noteInput.value = noteOriginal.value
    toast.add({ title: t('profile.instructions.saved'), color: 'green' })
  } catch (e: any) {
    toast.add({ title: e?.message || t('profile.instructions.saveFailed'), color: 'red' })
  } finally {
    savingNote.value = false
  }
}

// --- Usage (per-user quota summary from whoami) ---
const emptyMetric = { used: 0, limit: null as number | null }
const usage = computed(() => {
  const orgs = (currentUser.value as any)?.organizations || []
  const org = orgs.find((o: any) => o.id === organization.value?.id) || orgs[0]
  const q = org?.usage_quota
  return {
    enabled: !!q?.enabled,
    tokens: q?.tokens || emptyMetric,
    queries: q?.queries || emptyMetric,
    data_bytes: q?.data_bytes || emptyMetric,
  }
})

function formatNumber(n: number | null | undefined): string {
  if (n == null) return '0'
  return new Intl.NumberFormat().format(n)
}
function formatBytes(n: number | null | undefined): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`
}

// --- Appearance (color mode) ---
const colorPreference = computed(() => colorMode.preference)
const themeOptions = computed(() => [
  { value: 'light', label: t('profile.appearance.light'), icon: 'i-heroicons-sun' },
  { value: 'dark', label: t('profile.appearance.dark'), icon: 'i-heroicons-moon' },
  { value: 'system', label: t('profile.appearance.system'), icon: 'i-heroicons-computer-desktop' },
])
function setTheme(value: string) {
  colorMode.preference = value
}

// Load data lazily when the modal opens / tab changes
watch(isOpen, (open) => {
  if (open) {
    syncNameInput()
    if (activeTab.value === 'instructions') loadInstructions()
  }
})
watch(activeTab, (tab) => {
  if (tab === 'instructions') loadInstructions()
})
</script>
