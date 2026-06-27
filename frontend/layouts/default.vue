<template>
  <div>
    <!-- Fixed global onboarding banner shown above everything -->
    <div v-if="showGlobalOnboardingBanner" class="fixed top-0 start-0 end-0 z-[1000]">
      <div
        @click="router.push(showGlobalOnboardingBannerLink)"
        class="text-center cursor-pointer text-white text-sm bg-blue-500/95 dark:bg-blue-700/90 hover:bg-blue-600/90 dark:hover:bg-blue-600/90 py-2 flex items-center justify-center shadow-md"
      >
        <UIcon name="i-heroicons-rocket-launch" class="h-5 me-2" />
        <span>{{ showGlobalOnboardingBannerText }}</span>
      </div>
    </div>

    <!-- License expiry countdown banner (shown in the last 30 days, and after expiry) -->
    <div v-if="showLicenseBanner" class="fixed top-0 start-0 end-0 z-[1000]">
      <div
        :class="[
          'text-center text-sm py-2 px-4 flex items-center justify-center gap-2 shadow-md',
          licenseExpired
            ? 'bg-red-600/95 text-white'
            : 'bg-amber-500/95 text-white',
          canModifySettings ? 'cursor-pointer hover:opacity-95' : ''
        ]"
        @click="canModifySettings ? router.push('/settings/license') : null"
      >
        <UIcon :name="licenseExpired ? 'i-heroicons-exclamation-circle' : 'i-heroicons-exclamation-triangle'" class="h-5 shrink-0" />
        <span>{{ licenseBannerText }}</span>
        <span v-if="canModifySettings" class="underline underline-offset-2 font-medium ms-1">
          {{ $t('settings.licensePage.banner.viewLicense') }}
        </span>
      </div>
    </div>
  <aside id="separator-sidebar"
    :class="[
      'fixed start-0 z-40 bg-gray-50 dark:bg-gray-950 transition-all duration-300 -translate-x-full rtl:translate-x-full sm:translate-x-0 sm:rtl:translate-x-0 border-e border-gray-200/80 dark:border-gray-800',
      isCollapsed ? 'w-14' : 'w-60',
      showTopBanner ? 'top-10 bottom-0' : 'top-0 bottom-0'
    ]"
    aria-label="Sidebar">
    <button v-if="isCollapsed" @click="toggleSidebar"
          class="flex items-center justify-center w-full px-2 py-2 -mb-4 rounded-lg bg-gray-50 dark:bg-gray-950 text-gray-700 dark:text-gray-300 hover:text-blue-500 transition-colors">
            <UTooltip :text="$t('nav.expandSidebar')" :popper="{ placement: tooltipPlacement }">
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <SidebarIcon class="w-4 h-4 rtl-flip" />
              </span>
            </UTooltip>
          </button>
    <div class="h-full px-3 py-4 bg-gray-50 dark:bg-gray-950 flex flex-col">

      <ul class="font-normal text-[13px] !ps-0 shrink-0">
        <li class="flex items-center mb-3" :class="isCollapsed ? 'flex-col gap-1' : 'justify-between'">
            <button @click="router.push('/')" :class="['flex items-center text-gray-700 group min-w-0 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors', isCollapsed ? 'justify-center p-1' : 'gap-2 px-2.5 py-1']">
              <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" :class="isCollapsed ? 'w-8 object-contain' : 'max-h-6 max-w-[84px] object-contain shrink-0'" />
              <span v-if="showText && organization?.name" class="text-[13px] font-semibold text-gray-700 dark:text-gray-200 truncate">{{ organization.name }}</span>
            </button>
            <div class="flex items-center gap-0.5" :class="isCollapsed ? 'flex-col' : ''">
              <!-- Search (opens the ⌘K command palette) -->
              <UTooltip :text="$t('commandPalette.placeholder')" :popper="{ placement: tooltipPlacement }">
                <button
                  @click="openCommandPalette"
                  class="flex items-center justify-center w-7 h-7 rounded-md text-gray-400 hover:text-gray-700 dark:text-gray-500 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
                  aria-label="Search"
                >
                  <UIcon name="i-heroicons-magnifying-glass" class="w-[18px] h-[18px]" />
                </button>
              </UTooltip>
              <!-- Collapse sidebar (expanded state only; collapsed uses the top expand button) -->
              <UTooltip v-if="!isCollapsed" :text="$t('nav.collapseSidebar')" :popper="{ placement: tooltipPlacement }">
                <button
                  @click="toggleSidebar"
                  class="flex items-center justify-center w-7 h-7 rounded-md text-gray-400 hover:text-gray-700 dark:text-gray-500 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
                  aria-label="Collapse sidebar"
                >
                  <SidebarIcon class="w-[18px] h-[18px] rtl-flip" />
                </button>
              </UTooltip>
            </div>
        </li>

        <li>
             <button
               name="create-report"
               @click="createNewReport"
               :disabled="creatingReport"
               :class="[
                 'flex items-center px-2.5 py-1.5 w-full rounded-md text-blue-500 hover:bg-gray-100 dark:hover:bg-gray-800/70 disabled:opacity-50 disabled:cursor-not-allowed',
                 isCollapsed ? 'justify-center' : 'gap-2.5'
               ]">
              <UTooltip v-if="isCollapsed" :text="creatingReport ? $t('common.loading') : $t('nav.newReport')" :popper="{ placement: tooltipPlacement }">
                <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
              </UTooltip>
              <template v-else>
                <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
                <span v-if="showText" class="font-medium">{{ creatingReport ? $t('common.loading') : $t('nav.newReport') }}</span>
              </template>
            </button>
        </li>

        <template v-for="item in mainNavItems" :key="item.href">
        <li v-if="item.section && !isCollapsed && (!item.adminOnly || isAdmin)" class="pt-3 pb-1 px-2.5">
          <span class="text-[11px] font-medium text-gray-400 uppercase tracking-wider">{{ $t(item.section) }}</span>
        </li>
        <li v-if="(!item.permission || useCan(item.permission)) && (!item.adminOnly || isAdmin)" :class="[{ hidden: item.hidden }, item.gapBefore ? 'mt-2' : '']">
          <NuxtLink :to="item.href" :class="[
            'flex items-center px-2.5 py-1.5 w-full rounded-md',
            isRouteActive(item.activePath || item.href) ? 'text-gray-900 dark:text-white bg-gray-200/70 dark:bg-gray-800 font-medium' : 'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                <UIcon v-if="item.icon" :name="item.icon" />
                <component v-else-if="item.component" :is="item.component" class="w-[18px] h-[18px]" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                <UIcon v-if="item.icon" :name="item.icon" />
                <component v-else-if="item.component" :is="item.component" class="w-[18px] h-[18px]" />
              </span>
              <span v-if="showText">{{ $t(item.label) }}</span>
            </template>
          </NuxtLink>
        </li>
        </template>
      </ul>

      <!-- Recent reports — pinned (starred) first; scrolls independently. -->
      <div v-if="!isCollapsed" class="flex-1 min-h-0 flex flex-col mt-4">
        <div class="px-2.5 pb-1 shrink-0">
          <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('nav.reports') }}</span>
        </div>
        <div class="flex-1 min-h-0 overflow-y-auto -mr-1 pr-1">
          <ul class="font-normal text-[13px] !ps-0 space-y-0.5">
            <li v-for="report in recentReports" :key="report.id" class="relative group/report">
              <NuxtLink :to="`/reports/${report.id}`" :class="[
                'flex items-center gap-2 px-2.5 py-1.5 pr-8 w-full rounded-md',
                isRouteActive(`/reports/${report.id}`) ? 'text-gray-900 dark:text-white bg-gray-200/70 dark:bg-gray-800 font-medium' : 'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70'
              ]">
                <UIcon :name="reportTypeIcon(report)" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                <span class="flex-1 truncate">{{ report.title || $t('reports.untitled') }}</span>
                <UIcon v-if="report.is_starred" name="i-heroicons-star-solid" class="w-3.5 h-3.5 shrink-0 text-amber-400 group-hover/report:opacity-0 transition-opacity" />
              </NuxtLink>
              <!-- Hover actions: ellipsis circle → teleported menu (see below) -->
              <button
                type="button"
                @click.stop.prevent="openReportMenu($event, report)"
                class="absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded-full text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200/80 dark:hover:bg-gray-700 transition-opacity"
                :class="reportMenuOpen && menuReport?.id === report.id ? 'opacity-100 bg-gray-200/80 dark:bg-gray-700' : 'opacity-0 group-hover/report:opacity-100'"
                aria-label="Report actions"
              >
                <UIcon name="i-heroicons-ellipsis-horizontal" class="w-4 h-4" />
              </button>
            </li>
            <li v-if="!recentReports.length" class="px-2.5 py-1.5 text-[12px] text-gray-400 dark:text-gray-500">
              {{ $t('reports.empty') }}
            </li>
          </ul>
        </div>
      </div>

      <ul class="font-normal text-[13px] !ps-0 shrink-0 mt-auto pt-2">
        <li v-for="item in bottomNavItems" :key="item.href">
          <a v-if="item.external" :href="item.href" target="_blank" rel="noopener noreferrer" :class="[
            'flex items-center px-2.5 py-1.5 w-full rounded-md',
            'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                <component v-if="item.component" :is="item.component" class="w-[18px] h-[18px]" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                <component v-if="item.component" :is="item.component" class="w-[18px] h-[18px]" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
              <span v-if="showText">{{ $t(item.label) }}</span>
            </template>
          </a>
          <NuxtLink v-else :to="item.href" :class="[
            'flex items-center px-2.5 py-1.5 w-full rounded-md',
            isRouteActive(item.activePath || item.href) ? 'text-gray-900 dark:text-white bg-gray-200/70 dark:bg-gray-800 font-medium' : 'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                <component v-if="item.component" :is="item.component" class="w-[18px] h-[18px]" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-5 h-5 text-[18px]']">
                <component v-if="item.component" :is="item.component" class="w-[18px] h-[18px]" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
              <span v-if="showText">{{ $t(item.label) }}</span>
            </template>
          </NuxtLink>
        </li>
        <li>
          <UDropdown :items="userDropdownItems" :popper="{ placement: 'top-start' }" class="block w-full"
            :ui="{ width: 'w-56', item: { size: 'text-[13px]', padding: 'px-2 py-1.5', icon: { base: 'flex-shrink-0 w-4 h-4' } } }">
            <template #item="{ item }">
              <component v-if="item.iconComponent" :is="item.iconComponent" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
              <UIcon v-else-if="item.icon" :name="item.icon" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
              <span v-else class="w-4 h-4 shrink-0"></span>
              <span class="truncate text-gray-700 dark:text-gray-200">{{ item.label }}</span>
            </template>
             <button :class="[
               'flex items-center px-2.5 py-1.5 w-full rounded-md text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70',
               isCollapsed ? 'justify-center' : 'gap-2.5'
             ]">
              <UTooltip v-if="isCollapsed" :text="$t('nav.loggedInAs', { name: currentUserName })" :popper="{ placement: tooltipPlacement }">
                <img v-if="userImageUrl" :src="userImageUrl" alt="" class="w-5 h-5 rounded-full object-cover bg-gray-100" />
                <div v-else class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-[10px] font-bold rounded-full">
                  {{ userInitial }}
                </div>
              </UTooltip>
              <template v-else>
                <img v-if="userImageUrl" :src="userImageUrl" alt="" class="w-5 h-5 rounded-full object-cover bg-gray-100" />
                <div v-else class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-[10px] font-bold rounded-full">
                  {{ userInitial }}
                </div>
                <span v-if="showText" class="truncate">{{ currentUserName }}</span>
                <UIcon v-if="showText" name="i-heroicons-chevron-up-down" class="ml-auto w-4 h-4 text-gray-400 shrink-0" />
              </template>
            </button>
          </UDropdown>
        </li>
        <li v-if="version && !isCollapsed">
          <UTooltip :text="$t('nav.version')" :popper="{ placement: tooltipPlacement }">
            <div class="text-[10px] text-gray-400 px-3 cursor-pointer hover:text-gray-900 dark:hover:text-white">
              {{ version }}
            </div>
          </UTooltip>
        </li>
      </ul>
    </div>

  </aside>

  <div :class="['min-h-screen transition-all duration-300', isCollapsed ? 'sm:ms-14' : 'sm:ms-60', showTopBanner ? 'pt-10' : 'pt-0']">
    <UNotifications />

    <slot />
  </div>

  <McpModal v-if="showMcpModal" v-model="showMcpModal" />

  <UserProfileModal v-if="showProfileModal" v-model="showProfileModal" />

  <!-- Sidebar report actions: share / rename / delete (singletons bound to menuReport) -->
  <!-- Teleported to body so it escapes the sidebar's transform/overflow clipping. -->
  <Teleport to="body">
    <div v-if="reportMenuOpen" class="fixed inset-0 z-[70]" @click="reportMenuOpen = false" @contextmenu.prevent="reportMenuOpen = false">
      <div
        class="absolute w-52 py-1 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg shadow-lg text-[13px]"
        :style="{ top: reportMenuPos.y + 'px', left: reportMenuPos.x + 'px' }"
        @click.stop
      >
        <button
          v-for="(action, i) in currentReportActions"
          :key="i"
          type="button"
          class="flex items-center gap-2 w-full px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
          :class="action.danger ? 'text-red-500 dark:text-red-400' : 'text-gray-700 dark:text-gray-200'"
          @click="reportMenuOpen = false; action.click()"
        >
          <UIcon :name="action.icon" class="w-4 h-4 shrink-0" :class="action.danger ? 'text-red-500 dark:text-red-400' : 'text-gray-400 dark:text-gray-500'" />
          <span class="truncate">{{ action.label }}</span>
        </button>
      </div>
    </div>
  </Teleport>

  <ShareConversationModal v-if="menuReport" v-model="shareOpen" :report="menuReport" no-trigger />

  <UModal v-model="renameOpen">
    <div class="p-4">
      <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('reports.renameTitle') }}</h3>
      <input
        v-model="renameTitle"
        type="text"
        :placeholder="$t('reports.renamePlaceholder')"
        class="mt-3 w-full h-9 px-3 text-[13px] bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400"
        @keyup.enter="doRename"
      />
      <div class="flex justify-end gap-2 mt-4">
        <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="renameOpen = false">{{ $t('common.cancel') }}</button>
        <button class="px-3 py-1.5 text-[13px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50" :disabled="!renameTitle.trim() || renaming" @click="doRename">{{ $t('common.save') }}</button>
      </div>
    </div>
  </UModal>

  <UModal v-model="deleteOpen">
    <div class="p-4">
      <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('reports.deleteTitle') }}</h3>
      <p class="mt-2 text-[13px] text-gray-500 dark:text-gray-400">{{ $t('reports.deleteBody') }}</p>
      <div class="flex justify-end gap-2 mt-4">
        <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="deleteOpen = false">{{ $t('common.cancel') }}</button>
        <button class="px-3 py-1.5 text-[13px] rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-50" :disabled="deleting" @click="doDelete">{{ $t('common.delete') }}</button>
      </div>
    </div>
  </UModal>

  <!-- Per-user notification inbox (bell in the sidebar) -->
  <NotificationModal />

  <!-- Global ⌘K / Ctrl+K command palette -->
  <CommandPalette />
  </div>
</template>

<script setup lang="ts">
  import { markRaw } from 'vue'
  import Spinner from '~/components/Spinner.vue'
  import McpIcon from '~/components/icons/McpIcon.vue'
  import GithubIcon from '~/components/icons/GithubIcon.vue'
  import LibraryIcon from '~/components/icons/LibraryIcon.vue'
  import ActivityIcon from '~/components/icons/ActivityIcon.vue'
  import SidebarIcon from '~/components/icons/SidebarIcon.vue'
  import McpModal from '~/components/McpModal.vue'
  import UserProfileModal from '~/components/UserProfileModal.vue'
  import NotificationModal from '~/components/NotificationModal.vue'
  import { useCan } from '~/composables/usePermissions'

  const { isMcpEnabled } = useOrgSettings()
  const showMcpModal = ref(false)
  const showProfileModal = ref(false)

  // Sidebar search button opens the global ⌘K command palette.
  const { open: openCommandPalette } = useCommandPalette()

  // Notification inbox (shared state with NotificationModal + the sidebar bell).
  const { isOpen: notifOpen, unread: notifUnread, fetchCount: fetchNotifCount } = useNotifications()
  let notifPollTimer: any = null
  onMounted(() => {
    fetchNotifCount()
    notifPollTimer = setInterval(fetchNotifCount, 60000)
  })
  onBeforeUnmount(() => { if (notifPollTimer) clearInterval(notifPollTimer) })
  // Resync the badge when the inbox closes (read/dismiss may have changed it).
  watch(notifOpen, (open) => { if (!open) fetchNotifCount() })

  const route = useRoute()
  const isRouteActive = (path: string) => {
    if (path === '/') return route.path === '/'
    return route.path === path || route.path.startsWith(path + '/')
  }
  watch(() => route.fullPath, () => {
    showMcpModal.value = false
    showProfileModal.value = false
    notifOpen.value = false
  })

  interface NavItem {
    href: string
    label: string
    icon?: string
    component?: any
    hidden?: boolean
    adminOnly?: boolean
    permission?: string
    section?: string
    gapBefore?: boolean
    external?: boolean
    activePath?: string
  }
  // Settings tabs and the permission each requires — must mirror the tab list
  // in layouts/settings.vue. The sidebar Settings link uses this to (a) hide
  // itself when no tab is reachable and (b) deep-link to the first reachable
  // tab, so clicking it never lands on a page the user gets redirected out of.
  const settingsTabPermissions: { name: string; permission: string }[] = [
    { name: 'members', permission: 'view_members' },
    { name: 'models', permission: 'manage_llm' },
    { name: 'ai_settings', permission: 'manage_settings' },
    { name: 'general', permission: 'manage_settings' },
    { name: 'integrations', permission: 'manage_settings' },
    { name: 'audit', permission: 'view_audit_logs' },
    { name: 'identity-provider', permission: 'manage_identity_providers' },
    { name: 'license', permission: 'manage_settings' },
  ]
  const firstAccessibleSettingsTab = computed(() =>
    settingsTabPermissions.find(tab => useCan(tab.permission)) || null
  )

  const mainNavItems: NavItem[] = [
    { href: '/dashboards', icon: 'heroicons-chart-bar-square', label: 'nav.dashboards' },
    { href: '/prompts', icon: 'heroicons-sparkles', label: 'nav.prompts' },
    { href: '/scheduled-tasks', icon: 'heroicons-clock', label: 'nav.scheduled' },
    { href: '/agents', icon: 'heroicons-cube', label: 'nav.agents', gapBefore: true },
    { href: '/files', icon: 'heroicons-document-duplicate', label: 'nav.files', hidden: true },
    { href: '/queries', component: LibraryIcon, label: 'nav.queries' },
    { href: '/monitoring', component: ActivityIcon, label: 'nav.monitoring', adminOnly: true },
  ]

  const bottomNavItems = computed<NavItem[]>(() => {
    const items: NavItem[] = []
    // The Settings entry was always shown but hard-linked to /settings/members,
    // which requires `view_members`. A user on a custom role without that perm
    // would click it and get silently bounced to '/' by permissions.global.ts —
    // i.e. "the Settings button does nothing". Only surface it when the user can
    // actually reach a settings tab, and point it at the first one they can open.
    const tab = firstAccessibleSettingsTab.value
    if (tab) {
      items.push({ href: `/settings/${tab.name}`, activePath: '/settings', icon: 'heroicons-cog-6-tooth', label: 'nav.admin' })
    }
    return items
  })
  
  // Agent management - use selectedAgentObjects for new report creation
  const { initAgent, selectedAgentObjects, agents, hasAgents } = useAgent()


  const workspaceIconUrl = computed<string | null>(() => {
    const orgId = organization.value?.id
    const orgs = (currentUser.value as any)?.organizations || []
    const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
    return org?.icon_url || null
  })
  const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
  const { organization, setOrganization } = useOrganization()
  const { onboarding, fetchOnboarding } = useOnboarding()
  const canModifySettings = computed(() => useCan('manage_settings'))
  // Banner visibility is shared via useTopBanner so full-height views (Agents)
  // can subtract the banner height from their own 100vh box.
  const { showGlobalOnboardingBanner, showLicenseBanner, showTopBanner } = useTopBanner()

  const showGlobalOnboardingBannerText = computed(() => {
    const ob = onboarding.value as any
    if (!ob) return 'Continue onboarding'
    return ob.current_step === 'llm_configured' ? 'Configure your LLM' : 'Connect your first data source'
  })

  const showGlobalOnboardingBannerLink = computed(() => {
    const ob = onboarding.value as any
    if (!ob) return '/onboarding'
    return ob.current_step === 'llm_configured' ? '/onboarding/llm' : '/onboarding/data'
  })

  // License expiry countdown banner. Shown to everyone (an expired license affects the
  // whole org), but only admins get the clickable link to the license settings page.
  const { isExpired: licenseExpired, isExpiringSoon, daysUntilExpiry } = useEnterprise()
  const licenseBannerText = computed<string>(() => {
    if (licenseExpired.value) return t('settings.licensePage.banner.expired')
    return t('settings.licensePage.banner.expiring', { days: daysUntilExpiry.value ?? 0 })
  })

  const { isExcel } = useExcel()
  const { isMobile } = useMobile()
  const router = useRouter()
  const { $intercom } = useNuxtApp()

  onMounted(async () => {
    try {
      const inOnboarding = route.path.startsWith('/onboarding')
      if (!inOnboarding) {
        // Fetch onboarding and agents in parallel for faster load
        await Promise.all([
          fetchOnboarding({ in_onboarding: false }),
          initAgent(),
          fetchRecentReports()
        ])
      }
    } catch {}

    // Hydrate locale from org config. Runs once per full page load —
    // the user's personal choice (stored under `bow.locale`) always
    // wins; we only apply the org override when they haven't picked
    // anything. Executes here rather than in the i18n plugin because
    // useMyFetch needs the session + org state that are only ready
    // after mount.
    try {
      const stored = typeof localStorage !== 'undefined' ? localStorage.getItem('bow.locale') : null
      if (!stored) {
        const resp = await useMyFetch('/api/organization/locale')
        const body = resp.data?.value as any
        const effective = body?.effective_locale
        const setLocale = (useNuxtApp() as any).$setLocale as ((c: string) => void) | undefined
        if (effective && typeof setLocale === 'function') setLocale(effective)
      }
    } catch {
      // non-fatal; user can still pick manually via the settings picker
    }
  })
  const { version, environment, app_url, intercom } = useRuntimeConfig().public
  
  // Sidebar collapse state (shared via composable)
  const { isCollapsed, showText, toggle: toggleSidebar } = useSidebar()
  const creatingReport = ref(false)

  // Recent reports list shown in the sidebar. The backend already orders
  // these `is_starred DESC, created_at DESC`, so pinned reports come first.
  const recentReports = ref<any[]>([])
  const fetchRecentReports = async () => {
    try {
      const resp = await useMyFetch('/reports', { method: 'GET', query: { filter: 'my', limit: 50 } })
      if ((resp as any).status?.value === 'success' && (resp as any).data?.value?.reports) {
        recentReports.value = (resp as any).data.value.reports
      }
    } catch {}
  }
  // Heroicon for a report based on its primary artifact type.
  const reportTypeIcon = (report: any): string => {
    const modes = report?.artifact_modes || []
    if (modes.includes('page')) return 'i-heroicons-chart-bar-square'
    if (modes.includes('slides')) return 'i-heroicons-presentation-chart-bar'
    return 'i-heroicons-chat-bubble-left-right'
  }
  // Keep the list fresh when the user moves between reports (titles/new reports).
  watch(() => route.path, (path) => {
    if (path === '/reports' || path.startsWith('/reports/')) fetchRecentReports()
  })

  // ── Per-report hover menu: share / rename / star / delete ──────────────
  // Rendered via Teleport (see template) so it escapes the sidebar's
  // transform + overflow clipping; positioned at the click coordinates.
  const reportToast = useToast()
  const menuReport = ref<any>(null)
  const reportMenuOpen = ref(false)
  const reportMenuPos = ref({ x: 0, y: 0 })
  const shareOpen = ref(false)
  const renameOpen = ref(false)
  const renameTitle = ref('')
  const renaming = ref(false)
  const deleteOpen = ref(false)
  const deleting = ref(false)

  const openReportMenu = (e: MouseEvent, report: any) => {
    menuReport.value = report
    // Clamp so the 208px-wide menu never runs off the right/bottom edge.
    const x = Math.min(e.clientX, (typeof window !== 'undefined' ? window.innerWidth : 9999) - 216)
    const y = Math.min(e.clientY, (typeof window !== 'undefined' ? window.innerHeight : 9999) - 180)
    reportMenuPos.value = { x: Math.max(8, x), y: Math.max(8, y) }
    reportMenuOpen.value = true
  }

  // Flat action list for the teleported menu, derived from the active report.
  const currentReportActions = computed(() => {
    const report = menuReport.value
    if (!report) return [] as any[]
    return [
      { label: t('reports.menu.share'), icon: 'i-heroicons-arrow-up-tray', click: () => openShare(report) },
      { label: t('reports.menu.rename'), icon: 'i-heroicons-pencil-square', click: () => openRename(report) },
      {
        label: report.is_starred ? t('reports.menu.unstar') : t('reports.menu.star'),
        icon: report.is_starred ? 'i-heroicons-star-solid' : 'i-heroicons-star',
        click: () => toggleStarReport(report),
      },
      { label: t('reports.menu.delete'), icon: 'i-heroicons-trash', danger: true, click: () => openDelete(report) },
    ]
  })

  // Close the menu on scroll / resize / route change so it never floats stale.
  watch(() => route.path, () => { reportMenuOpen.value = false })

  const openShare = (report: any) => { menuReport.value = report; shareOpen.value = true }

  const openRename = (report: any) => {
    menuReport.value = report
    renameTitle.value = report.title || ''
    renameOpen.value = true
  }
  const doRename = async () => {
    const r = menuReport.value
    const title = renameTitle.value.trim()
    if (!r || !title || renaming.value) return
    renaming.value = true
    try {
      const resp: any = await useMyFetch(`/reports/${r.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      })
      if (resp?.error?.value) throw resp.error.value
      r.title = title
      renameOpen.value = false
    } catch (e: any) {
      reportToast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
      renaming.value = false
    }
  }

  const toggleStarReport = async (report: any) => {
    const next = !report.is_starred
    report.is_starred = next // optimistic
    try {
      const resp: any = await useMyFetch(`/reports/${report.id}/star`, { method: next ? 'POST' : 'DELETE' })
      if (resp?.error?.value) throw resp.error.value
      await fetchRecentReports() // server orders starred-first
    } catch (e: any) {
      report.is_starred = !next // revert
      reportToast.add({ title: t('reports.toasts.starFailed'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    }
  }

  const openDelete = (report: any) => { menuReport.value = report; deleteOpen.value = true }
  const doDelete = async () => {
    const r = menuReport.value
    if (!r || deleting.value) return
    deleting.value = true
    try {
      const resp: any = await useMyFetch(`/reports/${r.id}`, { method: 'DELETE' })
      if (resp?.error?.value) throw resp.error.value
      recentReports.value = recentReports.value.filter((x: any) => x.id !== r.id)
      deleteOpen.value = false
      if (route.path === `/reports/${r.id}`) router.push('/')
    } catch (e: any) {
      reportToast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
      deleting.value = false
    }
  }

  // Collapsed sidebar tooltips need to pop INTO the viewport, not out of it.
  // In LTR the sidebar is on the left so tooltips go right; in RTL the
  // sidebar is on the right so tooltips go left.
  const { locale: i18nLocale } = useI18n({ useScope: 'global' })
  const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
  const isRtl = computed<boolean>(() => RTL_LOCALES.has(i18nLocale.value))
  const tooltipPlacement = computed<'left' | 'right'>(() =>
    isRtl.value ? 'left' : 'right'
  )
  // Intercom launcher should sit on the opposite side of the sidebar so it
  // doesn't collide with it. LTR: sidebar left → launcher right (default).
  // RTL: sidebar right → launcher left.
  const intercomAlignment = computed<'left' | 'right'>(() =>
    isRtl.value ? 'left' : 'right'
  )
  
  const currentUserName = computed<string>(() => {
    const user = currentUser.value as any
    return user?.name || user?.email || 'User'
  })

  const userInitial = computed<string>(() => {
    const name = currentUserName.value
    return name.charAt(0).toUpperCase()
  })

  const userImageUrl = computed<string | null>(() => (currentUser.value as any)?.image_url || null)

  const { t } = useI18n()
  const userOrganizations = computed<any[]>(() => {
    return ((currentUser.value as any)?.organizations || []) as any[]
  })

  const userDropdownItems = computed(() => {
    const groups: any[] = []
    groups.push([{
      label: t('profile.menuItem'),
      icon: 'heroicons-user-circle',
      click: () => { showProfileModal.value = true }
    }])

    // Documentation + MCP Server + GitHub moved out of the main sidebar into this menu.
    const resources: any[] = [{
      label: t('nav.documentation'),
      icon: 'heroicons-book-open',
      click: () => { window.open('https://docs.bagofwords.com', '_blank', 'noopener') }
    }]
    if (isMcpEnabled.value && useCan('manage_settings')) {
      resources.push({
        label: t('nav.mcpServer'),
        iconComponent: markRaw(McpIcon),
        click: () => { showMcpModal.value = true }
      })
    }
    resources.push({
      label: t('nav.starOnGithub'),
      iconComponent: markRaw(GithubIcon),
      click: () => { window.open('https://github.com/bagofwords1/bagofwords', '_blank', 'noopener') }
    })
    groups.push(resources)

    const orgs = userOrganizations.value
    if (orgs.length > 1) {
      groups.push(
        orgs.map((org: any) => ({
          label: org.name,
          icon: org.id === organization.value?.id ? 'heroicons-check' : undefined,
          disabled: org.id === organization.value?.id,
          click: () => setOrganization(org.id),
        }))
      )
    }
    groups.push([{
      label: t('auth.logout'),
      icon: 'heroicons-arrow-left',
      click: signOff
    }])
    return groups
  })

  const isAdmin = computed<boolean>(() => useCan('full_admin_access'))
 
  if (environment === 'production' && intercom) {
    const hideLauncher = computed<boolean>(() => isExcel.value || isMobile.value)
    $intercom.boot({
      hide_default_launcher: hideLauncher.value,
      alignment: intercomAlignment.value
    })
    watch([currentUser, organization], ([user, org]) => {
      if (user && org) {
        $intercom.update({
          user_id: (user as any).id,
          name: (user as any)?.name,
          email: (user as any)?.email,
          version: version,
          environment: environment,
          app_url: app_url,
          hide_default_launcher: hideLauncher.value,
          alignment: intercomAlignment.value,
          company: {
            company_id: org.id,
            name: org.name
          }
        })
      }
    }, { immediate: true })
    watch(intercomAlignment, (alignment) => {
      $intercom.update({ alignment })
    })
    watch(hideLauncher, (hide) => {
      $intercom.update({ hide_default_launcher: hide })
    })
  }

const createNewReport = async () => {
  if (creatingReport.value) return
  creatingReport.value = true
  
  try {
    // Use selected agents from AgentSelector, or all agents if none selected
    const dataSourceIds = selectedAgentObjects.value.map((a: any) => a.id)
    
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({
          title: 'untitled report',
          files: [],
          data_sources: dataSourceIds
        })
    });

    if ((response as any).error?.value) {
        throw new Error('Report creation failed');
    }

    const data = ((response as any).data?.value) as any;
    fetchRecentReports()
    await router.push({
        path: `/reports/${data.id}`
    })
  } finally {
    creatingReport.value = false
  }
}

  async function signOff() {
    await signOut({ 
      callbackUrl: '/' 
    })
    window.location.href = '/'
  }

  </script>
