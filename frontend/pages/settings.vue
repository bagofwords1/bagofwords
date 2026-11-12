<template>
    <!-- Parent route for every /settings page. It replaces the old `settings`
         layout: switching layouts remounts layouts/default.vue (sidebar and
         all), which would make the sidebar's swap into the admin nav impossible
         to animate. As a nested route, the sidebar stays mounted and only this
         content area changes.

         The shell owns the page header (title, description, actions), so every
         page in the console has the same hierarchy and pages never render a
         heading of their own. Navigation lives in the sidebar
         (components/AdminSidebarNav.vue). -->
    <NuxtPage v-if="!inConsole" />
    <div v-else class="settings-shell px-5 sm:px-8 lg:px-10 pt-8 sm:pt-10 pb-16 text-sm">
        <!-- Keyed on the page so each one fades up as it arrives. An enter
             animation only (no <Transition>): several settings pages have
             Fragment roots, which <Transition> cannot animate. -->
        <div :key="pageKey" :class="['settings-page-in', widthClass]">
            <header class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-8">
                <div class="min-w-0">
                    <h1 class="text-xl font-semibold tracking-tight text-ink">{{ pageTitle }}</h1>
                    <p v-if="pageDescription" class="mt-1.5 max-w-2xl text-sm leading-relaxed text-ink-muted">
                        {{ pageDescription }}
                    </p>
                </div>
                <!-- Pages teleport their primary actions here
                     (<Teleport to="#settings-page-actions" defer>). -->
                <div id="settings-page-actions" class="flex items-center gap-2 shrink-0 empty:hidden" />
            </header>
            <NuxtPage />
        </div>
    </div>
</template>

<script setup lang="ts">
import { isAdminRoute, resolveAdminPage } from '~/composables/useAdminNav'

const route = useRoute()
const { t } = useI18n()

const inConsole = computed(() => isAdminRoute(route.path))

const page = computed(() => resolveAdminPage(route.path, String(route.query.tab || '')))

const pageTitle = computed(() => page.value ? t(page.value.label) : t('settings.title'))
const pageDescription = computed(() => page.value?.description ? t(page.value.description) : '')

// Fixed measures, never a percentage of the window (see AdminPageWidth).
const widthClass = computed(() => page.value?.width === 'narrow' ? 'max-w-3xl' : 'max-w-6xl')

const pageKey = computed(() => page.value?.key || route.path)
</script>

<style scoped>
@keyframes settings-page-in {
    from { opacity: 0; transform: translateY(3px); }
    to { opacity: 1; transform: none; }
}
.settings-page-in {
    animation: settings-page-in 160ms cubic-bezier(0.2, 0, 0, 1) both;
}
</style>
