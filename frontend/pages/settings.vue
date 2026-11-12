<template>
    <!-- Parent route for every /settings page. It replaces the old `settings`
         layout: switching layouts remounts layouts/default.vue (sidebar and
         all), which would make the sidebar's swap into the admin nav impossible
         to animate. As a nested route, the sidebar stays mounted and only this
         content area changes. Navigation itself lives in the sidebar
         (components/AdminSidebarNav.vue), so there is no tab strip here. -->
    <NuxtPage v-if="!inConsole" />
    <div v-else class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-7xl px-4 ps-0 py-4">
            <!-- Keyed on the page so each one fades up as it arrives. An enter
                 animation only (no <Transition>): several settings pages have
                 Fragment roots, which <Transition> cannot animate. -->
            <div :key="pageKey" class="settings-page-in">
                <h1 class="text-lg font-semibold text-ink">{{ pageTitle }}</h1>
                <NuxtPage />
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ADMIN_NAV_ITEMS, isAdminRoute } from '~/composables/useAdminNav'

const route = useRoute()
const { t } = useI18n()

const inConsole = computed(() => isAdminRoute(route.path))

const currentName = computed(() => route.path.replace(/^\/settings\/?/, '').split('/')[0] || '')

// The heading names where you are, since the tab strip that used to say it is
// gone: the page, or for a page with sub-pages (Access), the sub-page.
const pageTitle = computed(() => {
    const item = ADMIN_NAV_ITEMS.find(i => i.name === currentName.value)
    if (!item) return t('settings.title')
    if (item.children?.length) {
        const tab = String(route.query.tab || '')
        const child = item.children.find(c => c.key === tab) || item.children[0]
        return t(child.label)
    }
    return t(item.label)
})

const pageKey = computed(() => `${currentName.value}:${String(route.query.tab || '')}`)
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
