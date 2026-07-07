<template>
  <div>
    <!-- Fixed global onboarding banner shown above everything.
         Desktop-only: on mobile it clutters the top and steals height from
         full-height views (the report chat prompt box gets clipped). -->
    <div v-if="showGlobalOnboardingBanner" class="hidden sm:block fixed top-0 start-0 end-0 z-[1000]">
      <div
        @click="router.push(showGlobalOnboardingBannerLink)"
        class="text-center cursor-pointer text-white text-sm bg-blue-500/95 dark:bg-blue-700/90 hover:bg-blue-600/90 dark:hover:bg-blue-600/90 py-2 flex items-center justify-center shadow-md"
      >
        <UIcon name="i-heroicons-rocket-launch" class="h-5 me-2" />
        <span>{{ showGlobalOnboardingBannerText }}</span>
      </div>
    </div>

    <!-- License expiry countdown banner (shown in the last 30 days, and after expiry) -->
    <div v-if="showLicenseBanner" class="hidden sm:block fixed top-0 start-0 end-0 z-[1000]">
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
  <!-- Mobile top bar: the sidebar is off-canvas on phones, so this gives a
       hamburger to open it plus quick access to New Report. Hidden on sm+ and
       on the immersive report-detail page (which has its own header). -->
  <div v-if="!isExcel && showMobileBar"
    :class="[
      'sm:hidden fixed start-0 end-0 z-40 h-12 flex items-center justify-between px-3 bg-gray-50 dark:bg-gray-950 border-b border-gray-200/80 dark:border-gray-800',
      'top-0'
    ]">
    <button @click="openMobile" class="flex items-center justify-center w-9 h-9 -ms-1 rounded-md text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70" aria-label="Open menu">
      <UIcon name="i-heroicons-bars-3" class="w-6 h-6" />
    </button>
    <button @click="router.push('/')" class="flex items-center gap-2 min-w-0">
      <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" class="max-h-6 max-w-[84px] object-contain" />
    </button>
    <button @click="createNewReport" :disabled="creatingReport" class="flex items-center justify-center w-9 h-9 -me-1 rounded-md text-blue-500 hover:bg-gray-100 dark:hover:bg-gray-800/70 disabled:opacity-50" aria-label="New report">
      <Spinner v-if="creatingReport" class="animate-spin w-5 h-5" />
      <UIcon v-else name="heroicons-plus-circle" class="w-6 h-6" />
    </button>
  </div>

  <!-- Backdrop behind the mobile drawer -->
  <div v-if="mobileOpen" class="sm:hidden fixed inset-0 z-40 bg-black/40" @click="closeMobile" />

  <aside id="separator-sidebar"
    :class="[
      'fixed start-0 z-50 sm:z-40 bg-gray-50 dark:bg-gray-950 transition-transform duration-300 sm:transition-all sm:translate-x-0 sm:rtl:translate-x-0 border-e border-gray-200/80 dark:border-gray-800',
      mobileOpen ? 'translate-x-0 rtl:translate-x-0' : '-translate-x-full rtl:translate-x-full',
      isCollapsed ? 'sm:w-14' : 'sm:w-60',
      mobileOpen ? 'w-72' : 'w-60',
      showTopBanner ? 'top-0 sm:top-10 bottom-0' : 'top-0 bottom-0'
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
              <UTooltip v-if="!isCollapsed && !mobileOpen" :text="$t('nav.collapseSidebar')" :popper="{ placement: tooltipPlacement }">
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
          <!-- Action item (e.g. Notifications → opens the bell modal) -->
          <button v-if="item.action === 'notifications'" @click="notifOpen = true" :class="[
            'flex items-center px-2.5 py-1.5 w-full rounded-md text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span class="relative flex items-center justify-center w-5 h-5 text-[16px]">
                <UIcon :name="item.icon || 'i-heroicons-bell'" />
                <span v-if="notifUnread" class="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-1 rounded-full bg-red-500 text-white text-[8px] font-semibold leading-none flex items-center justify-center ring-2 ring-gray-50 dark:ring-gray-950">{{ notifUnread > 9 ? '9+' : notifUnread }}</span>
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-[18px]">
                <UIcon :name="item.icon || 'i-heroicons-bell'" />
              </span>
              <span v-if="showText" class="flex-1 text-start">{{ $t(item.label) }}</span>
              <span v-if="showText && notifUnread" class="min-w-[18px] h-[18px] px-1 rounded-full bg-red-500 text-white text-[10px] font-semibold leading-none flex items-center justify-center">{{ notifUnread > 9 ? '9+' : notifUnread }}</span>
            </template>
          </button>
          <NuxtLink v-else :to="item.href" :class="[
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
        <div class="px-2.5 pb-1 shrink-0 flex items-center justify-between group/hdr">
          <NuxtLink to="/reports" class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-200 transition-colors">{{ $t('nav.reports') }}</NuxtLink>
          <NuxtLink to="/reports" class="inline-flex items-center gap-0.5 text-[11px] font-medium text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 opacity-0 group-hover/hdr:opacity-100 focus:opacity-100 transition-opacity">
            {{ $t('reports.viewAll') }}<UIcon name="i-heroicons-arrow-right" class="w-3 h-3" />
          </NuxtLink>
        </div>
        <div class="flex-1 min-h-0 overflow-y-auto -me-1 pe-1">
          <ul class="font-normal text-[13px] !ps-0 space-y-0.5">
            <li v-for="report in recentReports" :key="report.id" class="relative group/report">
              <NuxtLink :to="`/reports/${report.id}`" :class="[
                'flex items-center gap-2 px-2.5 py-1.5 pe-8 w-full rounded-md',
                isRouteActive(`/reports/${report.id}`) ? 'text-gray-900 dark:text-white bg-gray-200/70 dark:bg-gray-800 font-medium' : 'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70'
              ]">
                <UIcon :name="reportTypeIcon(report)" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                <span
                  class="flex-1 truncate"
                  :class="{ 'report-title-fade': titledReportIds.has(report.id) }"
                >{{ report.title || $t('reports.untitled') }}</span>
                <UIcon v-if="report.is_starred" name="i-heroicons-star-solid" class="w-3.5 h-3.5 shrink-0 text-amber-400 group-hover/report:opacity-0 transition-opacity" />
              </NuxtLink>
              <!-- Hover actions: ellipsis circle → teleported menu (see below) -->
              <button
                type="button"
                @click.stop.prevent="openReportMenu($event, report)"
                class="absolute end-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded-full text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200/80 dark:hover:bg-gray-700 transition-opacity"
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
              <span v-if="item.isVersion" class="text-[11px] text-gray-400 dark:text-gray-500">{{ item.label }}</span>
              <template v-else>
                <component v-if="item.iconComponent" :is="item.iconComponent" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                <UIcon v-else-if="item.icon" :name="item.icon" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                <span v-else class="w-4 h-4 shrink-0"></span>
                <span class="truncate text-gray-700 dark:text-gray-200">{{ item.label }}</span>
              </template>
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
      </ul>
    </div>

  </aside>

  <div :class="['min-h-dvh transition-all duration-300', isCollapsed ? 'sm:ms-14' : 'sm:ms-60', contentPadClass]">
    <UNotifications />

    <slot />
  </div>

  <McpModal v-if="showMcpModal" v-model="showMcpModal" />

  <UserProfileModal v-if="showProfileModal" v-model="showProfileModal" />

  <ChangelogModal v-model="showChangelogModal" />

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
          class="flex items-center gap-2 w-full px-3 py-1.5 text-start hover:bg-gray-100 dark:hover:bg-gray-800"
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
  import ChangelogModal from '~/components/ChangelogModal.vue'
  import { useCan } from '~/composables/usePermissions'

  const { isMcpEnabled } = useOrgSettings()
  const showMcpModal = ref(false)
  const showProfileModal = ref(false)
  const showChangelogModal = ref(false)

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
    showChangelogModal.value = false
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
    action?: 'notifications'
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
    { href: '/automations', icon: 'heroicons-bolt', label: 'nav.automations' },
    { href: '/dashboards', icon: 'heroicons-chart-bar-square', label: 'nav.dashboards' },
    { href: 'notifications', action: 'notifications', icon: 'heroicons-bell', label: 'nav.notifications' },
    { href: '/agents', icon: 'heroicons-cube', label: 'nav.agents', gapBefore: true },
    { href: '/prompts', icon: 'heroicons-book-open', label: 'nav.prompts' },
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
  
  // Sidebar collapse state (shared via composable). The desktop sidebar and the
  // mobile off-canvas drawer are the SAME <aside>. The raw collapse state is a
  // desktop affordance; when the drawer is open on mobile we always want the
  // full expanded layout (labels, left-aligned) inside the wide w-72 drawer —
  // never the icon-only collapsed rendering. So the template binds to these
  // "effective" computeds, which force expanded while the mobile drawer is open.
  // On desktop mobileOpen is always false, so they equal the raw values and the
  // desktop rendering is unchanged.
  const { isCollapsed: rawCollapsed, showText: rawShowText, toggle: toggleSidebar, mobileOpen, openMobile, closeMobile } = useSidebar()
  const isCollapsed = computed(() => mobileOpen.value ? false : rawCollapsed.value)
  const showText = computed(() => mobileOpen.value ? true : rawShowText.value)
  const creatingReport = ref(false)

  // Mobile chrome. The report-detail page is full-height (h-dvh) and ships its
  // own ReportHeader, so we suppress the global mobile bar there to avoid a
  // double header and the extra top padding that would make it overflow.
  const isReportDetail = computed(() => /^\/reports\/[^/]+$/.test(route.path))
  const showMobileBar = computed(() => !isReportDetail.value)
  // Top padding for the content wrapper. Desktop only needs to clear the
  // banner; mobile also needs to clear the 48px mobile bar when it is shown.
  const contentPadClass = computed(() => {
    // The top banner is desktop-only, so mobile padding never accounts for it —
    // only the 48px mobile bar (when shown). Desktop still clears the banner.
    const sm = showTopBanner.value ? 'sm:pt-10' : 'sm:pt-0'
    const mobile = showMobileBar.value ? 'pt-12' : 'pt-0'
    return `${mobile} ${sm}`
  })
  // Close the drawer whenever the route changes (e.g. after tapping a nav item).
  watch(() => route.fullPath, () => { closeMobile() })

  // Recent reports list shown in the sidebar. The backend already orders
  // these `is_starred DESC, last_activity_at DESC`, so pinned reports come
  // first and the rest sort by most recent conversation activity.
  const recentReports = ref<any[]>([])
  const fetchRecentReports = async () => {
    try {
      const resp = await useMyFetch('/reports', { method: 'GET', query: { filter: 'my', limit: 50, view: 'minimal' } })
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

  // Live title updates: the open report page (pages/reports/[id]) dispatches
  // `report:updated` after it reloads, which is when the server-generated title
  // first becomes available. Patch the matching sidebar item in place — no route
  // change happens, so the route watcher above wouldn't catch it — and fade the
  // new title in. ids in `titledReportIds` get the `.report-title-fade` class.
  const titledReportIds = ref<Set<string>>(new Set())
  const onReportUpdated = (e: Event) => {
    const detail = (e as CustomEvent).detail || {}
    const id = detail.id
    const title = detail.title
    if (!id) return
    const item = recentReports.value.find((r: any) => r.id === id)
    if (!item) {
      // Report isn't in the recent list yet (e.g. freshly created) — pull it in.
      fetchRecentReports()
      return
    }
    // Only animate when the title actually changed (e.g. placeholder → real title).
    if (title && item.title !== title) {
      item.title = title
      const next = new Set(titledReportIds.value)
      next.add(id)
      titledReportIds.value = next
      // Clear after the animation so a later list re-render doesn't replay it.
      setTimeout(() => {
        const after = new Set(titledReportIds.value)
        after.delete(id)
        titledReportIds.value = after
      }, 800)
    }
  }
  onMounted(() => window.addEventListener('report:updated', onReportUpdated))
  onBeforeUnmount(() => window.removeEventListener('report:updated', onReportUpdated))

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
      label: t('changelog.menuItem'),
      icon: 'heroicons-document-text',
      click: () => { showChangelogModal.value = true }
    }, {
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
    if (version) {
      groups.push([{ label: `v${version}`, isVersion: true, disabled: true }])
    }
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

<style scoped>
/* Fade the report title in when it transitions from the "untitled report"
   placeholder to the server-generated title (see onReportUpdated). */
@keyframes report-title-fade {
  from { opacity: 0; transform: translateY(-2px); }
  to { opacity: 1; transform: translateY(0); }
}
.report-title-fade {
  animation: report-title-fade 0.45s ease-out;
}
@media (prefers-reduced-motion: reduce) {
  .report-title-fade { animation: none; }
}
</style>
