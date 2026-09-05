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
    ref="sidebarEl"
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
    <div class="h-full px-3 py-4 bg-gray-50 dark:bg-gray-950 flex flex-col" :class="isShortSidebar ? 'overflow-y-auto' : ''">

      <ul class="font-normal text-[13px] !ps-0 shrink-0">
        <li class="flex items-center mb-3" :class="isCollapsed ? 'flex-col gap-1' : 'justify-between'">
            <!-- Workspace header. With a single org this is just a link home
                 (the only desktop path to '/'). With several, it doubles as the
                 org switcher and carries 'Home' as its first item so that path
                 isn't lost. Users with one org see no switcher affordance. -->
            <UDropdown v-if="hasMultipleOrgs" :items="organizationDropdownItems"
              :popper="{ placement: isCollapsed ? 'right-start' : 'bottom-start' }" class="min-w-0"
              :ui="{ width: 'w-56', item: { size: 'text-[13px]', padding: 'px-2 py-1.5' } }">
              <template #item="{ item }">
                <img v-if="item.iconUrl" :src="item.iconUrl" alt="" class="w-4 h-4 shrink-0 rounded object-contain" />
                <UIcon v-else-if="item.icon" :name="item.icon" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                <span v-else class="w-4 h-4 shrink-0"></span>
                <span class="truncate text-gray-700 dark:text-gray-200">{{ item.label }}</span>
                <UIcon v-if="item.isCurrent" name="i-heroicons-check" class="w-4 h-4 shrink-0 ms-auto text-gray-400 dark:text-gray-500" />
              </template>
              <button :class="workspaceButtonClass" :aria-label="$t('nav.switchOrganization')">
                <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" :class="isCollapsed ? 'w-8 object-contain' : 'max-h-6 max-w-[84px] object-contain shrink-0'" />
                <span v-if="showText && organization?.name" class="text-[13px] font-semibold text-gray-700 dark:text-gray-200 truncate">{{ organization.name }}</span>
                <UIcon v-if="showText" name="i-heroicons-chevron-up-down" class="w-3.5 h-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
              </button>
            </UDropdown>
            <button v-else @click="router.push('/')" :class="workspaceButtonClass">
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
        <li v-if="(!item.permission || useCan(item.permission)) && (!item.adminOnly || isAdmin) && (!item.canView || item.canView())" :class="[{ hidden: item.hidden }, item.gapBefore ? 'mt-2' : '']">
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

      <!-- Projects — shared folders for reports. -->
      <div v-if="!isCollapsed" class="flex flex-col min-h-0 mt-4" :style="projectsSectionStyle">
        <div class="px-2.5 pb-1 shrink-0 flex items-center justify-between group/phdr">
          <!-- The title stays a link to /projects; folding is its own control so
               neither gesture steals the other. -->
          <div class="flex items-center gap-1 min-w-0">
            <button
              type="button"
              @click="projectsOpen = !projectsOpen"
              :aria-expanded="projectsOpen"
              :aria-label="$t('projects.title')"
              class="flex items-center justify-center w-4 h-4 -ms-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70"
            >
              <UIcon :name="projectsOpen ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
            </button>
            <NuxtLink to="/projects" class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-200 transition-colors">{{ $t('projects.title') }}</NuxtLink>
          </div>
          <!-- While a report is in flight, the header explains where to drop it. -->
          <span v-if="draggingReport" class="text-[11px] font-medium text-blue-500 dark:text-blue-400 truncate ps-2">{{ $t('projects.dropHint') }}</span>
          <div v-else class="flex items-center gap-1 opacity-0 group-hover/phdr:opacity-100 focus-within:opacity-100 transition-opacity">
            <UTooltip :text="$t('projects.newProject')" :popper="{ placement: 'top' }">
              <button
                type="button"
                name="new-project"
                @click="openCreateProject"
                class="flex items-center justify-center w-5 h-5 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70"
                :aria-label="$t('projects.newProject')"
              >
                <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
              </button>
            </UTooltip>
            <NuxtLink to="/projects" class="inline-flex items-center gap-0.5 text-[11px] font-medium text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">
              {{ $t('reports.viewAll') }}<UIcon name="i-heroicons-arrow-right" class="w-3 h-3" />
            </NuxtLink>
          </div>
        </div>
        <ul v-show="projectsOpen" class="font-normal text-[13px] !ps-0 space-y-0.5 -me-1 pe-1" :class="isShortSidebar ? '' : 'flex-1 min-h-0 overflow-y-auto'">
          <!-- Each row is a drop target for a report dragged from the list
               below: dropping files that report into the project without
               leaving the sidebar (the row menu's "Move to project" and its
               modal are unchanged, and remain the touch path). -->
          <li
            v-for="project in projects"
            :key="project.id"
            class="relative group/project rounded-md"
            :class="dropProjectId === project.id ? 'ring-2 ring-blue-400 dark:ring-blue-500 ring-inset bg-blue-50/70 dark:bg-blue-900/20' : ''"
            @dragover="onProjectDragOver($event, project)"
            @dragenter.prevent="onProjectDragEnter(project)"
            @dragleave="onProjectDragLeave($event, project)"
            @drop.prevent="onProjectDrop($event, project)"
          >
            <NuxtLink :to="`/projects/${project.id}`" :class="[
              'flex items-center gap-2 px-2.5 py-1.5 pe-8 w-full rounded-md',
              isRouteActive(`/projects/${project.id}`) ? 'text-gray-900 dark:text-white bg-gray-200/70 dark:bg-gray-800 font-medium' : 'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70'
            ]">
              <!-- Drop cue: the folder opens while a report hovers this row. -->
              <UIcon :name="dropProjectId === project.id ? 'i-heroicons-folder-open' : 'i-heroicons-folder'" class="w-4 h-4 shrink-0" :style="project.color ? { color: project.color } : undefined" :class="!project.color ? 'text-gray-400 dark:text-gray-500' : ''" />
              <span class="flex-1 truncate">{{ project.name }}</span>
              <UIcon v-if="!project.is_owner || project.member_count > 0 || project.access === 'org'" name="i-heroicons-user-group" class="w-3.5 h-3.5 shrink-0 text-gray-300 dark:text-gray-600 group-hover/project:opacity-0 transition-opacity" />
            </NuxtLink>
            <button
              v-if="currentProjectActionsAvailable(project)"
              type="button"
              @click.stop.prevent="openProjectMenu($event, project)"
              class="absolute end-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded-full text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200/80 dark:hover:bg-gray-700 transition-opacity"
              :class="projectMenuOpen && menuProject?.id === project.id ? 'opacity-100 bg-gray-200/80 dark:bg-gray-700' : 'opacity-0 group-hover/project:opacity-100'"
              :aria-label="$t('projects.rowActions')"
            >
              <UIcon name="i-heroicons-ellipsis-horizontal" class="w-4 h-4" />
            </button>
          </li>
          <li v-if="!projects.length">
            <button
              type="button"
              @click="openCreateProject"
              class="flex items-center gap-2 px-2.5 py-1.5 w-full rounded-md text-[12px] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70"
            >
              <UIcon name="i-heroicons-folder-plus" class="w-4 h-4 shrink-0" />
              <span>{{ $t('projects.empty') }}</span>
            </button>
          </li>
        </ul>
      </div>

      <!-- Recent reports — pinned (starred) first; scrolls independently. -->
      <div v-if="!isCollapsed" class="min-h-0 flex flex-col mt-4" :style="reportsSectionStyle">
        <div class="px-2.5 pb-1 shrink-0 flex items-center justify-between group/hdr">
          <div class="flex items-center gap-1 min-w-0">
            <button
              type="button"
              @click="reportsOpen = !reportsOpen"
              :aria-expanded="reportsOpen"
              :aria-label="$t('nav.reports')"
              class="flex items-center justify-center w-4 h-4 -ms-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70"
            >
              <UIcon :name="reportsOpen ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
            </button>
            <NuxtLink to="/reports" class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-200 transition-colors">{{ $t('nav.reports') }}</NuxtLink>
          </div>
          <NuxtLink to="/reports" class="inline-flex items-center gap-0.5 text-[11px] font-medium text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 opacity-0 group-hover/hdr:opacity-100 focus:opacity-100 transition-opacity">
            {{ $t('reports.viewAll') }}<UIcon name="i-heroicons-arrow-right" class="w-3 h-3" />
          </NuxtLink>
        </div>
        <div v-show="reportsOpen" class="-me-1 pe-1" :class="isShortSidebar ? '' : 'flex-1 min-h-0 overflow-y-auto'">
          <ul class="font-normal text-[13px] !ps-0 space-y-0.5">
            <!-- Draggable onto a project row above. The row keeps its place in
                 this list after the move — a project is a label on the report,
                 not a folder it disappears into — and gains the accent strip. -->
            <li
              v-for="report in sortedRecentReports"
              :key="report.id"
              class="relative group/report rounded-md"
              :class="draggingReport?.id === report.id ? 'opacity-50' : ''"
              draggable="true"
              @dragstart="startReportDrag($event, report)"
              @dragend="endReportDrag"
            >
              <NuxtLink :to="`/reports/${report.id}`" :class="[
                'flex items-center gap-2 px-2.5 py-1.5 pe-8 w-full rounded-md',
                isRouteActive(`/reports/${report.id}`) ? 'text-gray-900 dark:text-white bg-gray-200/70 dark:bg-gray-800 font-medium' : 'text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-800/70'
              ]">
                <!-- Status dot in a fixed leading cell: keeps every row's dot —
                     and the title after it — column-aligned. Always rendered
                     (show-idle → hollow ring when idle). -->
                <span class="inline-flex items-center shrink-0">
                  <ReportStatusDot :report-id="report.id" show-idle />
                </span>
                <span
                  class="flex-1 truncate"
                  :class="{ 'report-title-fade': titledReportIds.has(report.id) }"
                >{{ report.title || $t('reports.untitled') }}</span>
              </NuxtLink>
              <!-- Project membership: a thin color rule at the leading edge
                   (replaces the old project-tinted icon). Outside the flex flow
                   so the dot column stays aligned on non-project rows. Rendered
                   for any project — pre-palette folders have no color of their
                   own, and an invisible strip would read as "the move failed". -->
              <span
                v-if="report.project"
                class="absolute start-0.5 top-1/2 -translate-y-1/2 w-0.5 h-4 rounded-full pointer-events-none transition-colors"
                :class="!report.project.color ? 'bg-gray-300 dark:bg-gray-600' : ''"
                :style="report.project.color ? { backgroundColor: report.project.color } : undefined"
              ></span>
              <!-- Trailing star at rest; fades on hover (or when the row menu is
                   open) so the actions ellipsis takes the same spot.
                   pointer-events-none so the underlying link stays clickable. -->
              <UIcon
                v-if="report.is_starred"
                name="i-heroicons-star-solid"
                class="absolute end-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-amber-400 pointer-events-none transition-opacity"
                :class="(reportMenuOpen && menuReport?.id === report.id) ? 'opacity-0' : 'opacity-100 group-hover/report:opacity-0'"
              />
              <!-- Hover actions: ellipsis circle → teleported menu (see below) -->
              <button
                type="button"
                @click.stop.prevent="openReportMenu($event, report)"
                class="absolute end-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded-full text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200/80 dark:hover:bg-gray-700 transition-opacity"
                :class="reportMenuOpen && menuReport?.id === report.id ? 'opacity-100 bg-gray-200/80 dark:bg-gray-700' : 'opacity-0 group-hover/report:opacity-100'"
                :aria-label="$t('reports.rowActions')"
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
        <!-- App version — bottom-left of the sidebar (centered when collapsed).
             Clicking it opens the changelog modal. -->
        <li v-if="version">
          <button
            type="button"
            name="app-version"
            @click="showChangelogModal = true"
            :class="[
              'flex items-center w-full py-1 text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-white transition-colors',
              isCollapsed ? 'justify-center px-0' : 'px-3'
            ]"
            :aria-label="$t('nav.version')"
          >
            <UTooltip :text="$t('changelog.title')" :popper="{ placement: tooltipPlacement }">
              <span>v{{ version }}</span>
            </UTooltip>
          </button>
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

  <!-- Sidebar project actions: rename / delete (teleported like the report menu) -->
  <Teleport to="body">
    <div v-if="projectMenuOpen" class="fixed inset-0 z-[70]" @click="projectMenuOpen = false" @contextmenu.prevent="projectMenuOpen = false">
      <div
        class="absolute w-52 py-1 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg shadow-lg text-[13px]"
        :style="{ top: projectMenuPos.y + 'px', left: projectMenuPos.x + 'px' }"
        @click.stop
      >
        <button
          v-for="(action, i) in currentProjectActions"
          :key="i"
          type="button"
          class="flex items-center gap-2 w-full px-3 py-1.5 text-start hover:bg-gray-100 dark:hover:bg-gray-800"
          :class="action.danger ? 'text-red-500 dark:text-red-400' : 'text-gray-700 dark:text-gray-200'"
          @click="projectMenuOpen = false; action.click()"
        >
          <UIcon :name="action.icon" class="w-4 h-4 shrink-0" :class="action.danger ? 'text-red-500 dark:text-red-400' : 'text-gray-400 dark:text-gray-500'" />
          <span class="truncate">{{ action.label }}</span>
        </button>
      </div>
    </div>
  </Teleport>

  <!-- Create / rename project -->
  <UModal v-model="projectDialogOpen">
    <div class="p-4">
      <h3 class="text-base font-semibold text-gray-900 dark:text-white">
        {{ projectDialogMode === 'create' ? $t('projects.createTitle') : $t('projects.renameTitle') }}
      </h3>
      <input
        v-model="projectDialogName"
        type="text"
        :placeholder="$t('projects.namePlaceholder')"
        class="mt-3 w-full h-9 px-3 text-[13px] bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400"
        @keyup.enter="submitProjectDialog"
      />
      <p v-if="projectDialogMode === 'create'" class="mt-2 text-[12px] text-gray-400 dark:text-gray-500">
        {{ $t('projects.createHint') }}
      </p>
      <div class="flex justify-end gap-2 mt-4">
        <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="projectDialogOpen = false">{{ $t('common.cancel') }}</button>
        <button class="px-3 py-1.5 text-[13px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50" :disabled="!projectDialogName.trim() || projectDialogBusy" @click="submitProjectDialog">
          {{ projectDialogMode === 'create' ? $t('projects.create') : $t('common.save') }}
        </button>
      </div>
    </div>
  </UModal>

  <!-- Delete project (reports survive, back to root) -->
  <UModal v-model="projectDeleteOpen">
    <div class="p-4">
      <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('projects.deleteTitle') }}</h3>
      <!-- Honest impact: reports/dashboards are archived (not destroyed),
           automations are stopped, files are only unlinked from the project. -->
      <p class="mt-2 text-[13px] text-gray-500 dark:text-gray-400">{{ $t('projects.deleteImpact', {
        reports: projectDialogTarget?.report_count ?? 0,
        dashboards: projectDialogTarget?.dashboard_count ?? 0,
        automations: projectDialogTarget?.automation_count ?? 0,
        files: projectDialogTarget?.files?.length ?? 0,
      }) }}</p>
      <div class="flex justify-end gap-2 mt-4">
        <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="projectDeleteOpen = false">{{ $t('common.cancel') }}</button>
        <button class="px-3 py-1.5 text-[13px] rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-50" :disabled="projectDeleteBusy" @click="doDeleteProject">{{ $t('common.delete') }}</button>
      </div>
    </div>
  </UModal>

  <!-- Move report to project -->
  <UModal v-model="moveOpen">
    <div class="p-4">
      <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('projects.moveToProject') }}</h3>
      <div class="mt-3 max-h-64 overflow-y-auto space-y-0.5">
        <button
          v-for="project in projects"
          :key="project.id"
          type="button"
          class="flex items-center gap-2 w-full px-2.5 py-2 rounded-md text-[13px] text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
          :disabled="moveBusy || moveTargetReport?.project_id === project.id"
          @click="doMoveReport(project.id)"
        >
          <UIcon name="i-heroicons-folder" class="w-4 h-4 shrink-0" :style="project.color ? { color: project.color } : undefined" :class="!project.color ? 'text-gray-400 dark:text-gray-500' : ''" />
          <span class="flex-1 truncate text-start">{{ project.name }}</span>
          <span v-if="isProjectShared(project)" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('projects.sharedBadge') }}</span>
          <UIcon v-if="moveTargetReport?.project_id === project.id" name="i-heroicons-check" class="w-4 h-4 text-blue-500 shrink-0" />
        </button>
        <button
          v-if="moveTargetReport?.project_id"
          type="button"
          class="flex items-center gap-2 w-full px-2.5 py-2 rounded-md text-[13px] text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
          :disabled="moveBusy"
          @click="doMoveReport(null)"
        >
          <UIcon name="i-heroicons-folder-minus" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
          <span class="flex-1 truncate text-start">{{ $t('projects.removeFromProject') }}</span>
        </button>
        <div v-if="!projects.length" class="px-2.5 py-3 text-[12px] text-gray-400 dark:text-gray-500">
          {{ $t('projects.moveNoProjects') }}
        </div>
      </div>
      <p v-if="projects.some(isProjectShared)" class="mt-2 text-[11px] text-gray-400 dark:text-gray-500">
        {{ $t('projects.moveShareDisclosure') }}
      </p>
      <div class="flex justify-between items-center gap-2 mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
        <button class="inline-flex items-center gap-1 text-[12px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200" @click="moveOpen = false; openCreateProject()">
          <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('projects.newProject') }}
        </button>
        <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="moveOpen = false">{{ $t('common.cancel') }}</button>
      </div>
    </div>
  </UModal>

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
  import ActivityIcon from '~/components/icons/ActivityIcon.vue'
  import SidebarIcon from '~/components/icons/SidebarIcon.vue'
  import McpModal from '~/components/McpModal.vue'
  import UserProfileModal from '~/components/UserProfileModal.vue'
  import NotificationModal from '~/components/NotificationModal.vue'
  import ChangelogModal from '~/components/ChangelogModal.vue'
  import { useCan, useCanAccessMonitoring } from '~/composables/usePermissions'

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
    // Visibility predicate for items whose gate isn't a single org permission
    // (e.g. Monitoring: org admin OR manager of any agent). Evaluated during
    // render, so it stays reactive to the permission map.
    canView?: () => boolean
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

  // Admin-level permissions that justify surfacing the Admin entry at all.
  // `view_members` is deliberately absent: it is baseline for every org member
  // (see BASELINE_PERMISSIONS in app/core/permissions_registry.py), so keying
  // visibility off the reachable-tab list alone put an Admin button in every
  // ordinary member's sidebar, linking to a read-only user list. A non-admin
  // gets no Admin entry.
  const adminAreaPermissions = [
    'manage_members',
    'manage_service_accounts',
    'manage_settings',
    'manage_llm',
    'view_audit_logs',
    'manage_identity_providers',
  ]
  const canAccessAdminArea = computed(() =>
    adminAreaPermissions.some(permission => useCan(permission))
  )

  const mainNavItems: NavItem[] = [
    { href: '/automations', icon: 'heroicons-bolt', label: 'nav.automations' },
    { href: '/dashboards', icon: 'heroicons-chart-bar-square', label: 'nav.dashboards' },
    { href: 'notifications', action: 'notifications', icon: 'heroicons-bell', label: 'nav.notifications' },
    { href: '/agents', icon: 'heroicons-cube', label: 'nav.agents', gapBefore: true },
    { href: '/prompts', icon: 'heroicons-book-open', label: 'nav.prompts' },
    { href: '/files', icon: 'heroicons-document-duplicate', label: 'nav.files', hidden: true },
    // Monitoring is no longer admin-only: an agent manager gets the same console
    // scoped to the agents they manage (see useCanAccessMonitoring).
    { href: '/monitoring', component: ActivityIcon, label: 'nav.monitoring', canView: useCanAccessMonitoring },
  ]

  const bottomNavItems = computed<NavItem[]>(() => {
    const items: NavItem[] = []
    // The Settings entry was always shown but hard-linked to /settings/members,
    // which requires `view_members`. A user on a custom role without that perm
    // would click it and get silently bounced to '/' by permissions.global.ts —
    // i.e. "the Settings button does nothing". Only surface it when the user
    // holds an actual admin permission AND can reach a settings tab, and point
    // it at the first one they can open.
    const tab = firstAccessibleSettingsTab.value
    if (tab && canAccessAdminArea.value) {
      items.push({ href: `/settings/${tab.name}`, activePath: '/settings', icon: 'heroicons-cog-6-tooth', label: 'nav.admin' })
    }
    return items
  })
  
  // Agent management - use selectedAgentObjects for new report creation
  const { initAgent, initAgentPreference, selectedAgentObjects, agents, hasAgents } = useAgent()

  // Projects (shared folders) shown above the recent reports list.
  const { projects, fetchProjects, createProject, updateProject, deleteProject, moveReport } = useProjects()
  const { activeProjectId, newReportPayload } = useNewReportProjectContext()
  const { fetchActivity, sortByActivity, openStream } = useReportActivity()


  const workspaceIconUrl = computed<string | null>(() => {
    const orgId = organization.value?.id
    const orgs = (currentUser.value as any)?.organizations || []
    const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
    return org?.icon_url || null
  })

  // Shared by both workspace-header variants (plain home link vs. org-switcher
  // trigger) so their styling can't drift apart.
  const workspaceButtonClass = computed(() => [
    'flex items-center text-gray-700 group min-w-0 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors',
    isCollapsed.value ? 'justify-center p-1' : 'gap-2 px-2.5 py-1',
  ])
  const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
  const { organization, setOrganization } = useOrganization()
  const { onboarding, fetchOnboarding } = useOnboarding()
  const canModifySettings = computed(() => useCan('manage_settings'))
  // Banner visibility is shared via useTopBanner so full-height views (Agents)
  // can subtract the banner height from their own 100vh box.
  const { showGlobalOnboardingBanner, showLicenseBanner, showTopBanner } = useTopBanner()

  const showGlobalOnboardingBannerText = computed(() => {
    const ob = onboarding.value as any
    if (!ob) return t('banner.continueOnboarding')
    return ob.current_step === 'llm_configured' ? t('banner.configureLlm') : t('banner.connectFirstDataSource')
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
    openStream() // live badge/sort updates for every list surface
    try {
      const inOnboarding = route.path.startsWith('/onboarding')
      if (!inOnboarding) {
        // Fetch onboarding and agents in parallel for faster load
        await Promise.all([
          fetchOnboarding({ in_onboarding: false }),
          initAgent(),
          // The user's saved agent scope (membership-backed). Loaded here, not
          // on the home page, because "New report" in this sidebar creates a
          // report from it too.
          initAgentPreference(),
          fetchRecentReports(),
          fetchProjects()
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
        // Awaited: $setLocale resolves once the catalogue chunk has loaded and
        // the locale is actually applied, so failures surface in the catch.
        const setLocale = (useNuxtApp() as any).$setLocale as ((c: string) => Promise<void>) | undefined
        if (effective && typeof setLocale === 'function') await setLocale(effective)
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
      // All of the user's reports, including ones inside projects — project
      // membership shows as a color-tinted icon (tooltip = project name).
      const resp = await useMyFetch('/reports', { method: 'GET', query: { filter: 'my', limit: 50, view: 'minimal' } })
      if ((resp as any).status?.value === 'success' && (resp as any).data?.value?.reports) {
        recentReports.value = (resp as any).data.value.reports
        fetchActivity(recentReports.value.map((r: any) => r.id))
      }
    } catch {}
  }
  // Live ordering: starred first, then latest activity — including activity
  // that arrived over the stream since the list was fetched, so a report
  // jumps to the top the moment its run produces something new.
  const sortedRecentReports = computed(() => sortByActivity(recentReports.value))

  // Sidebar height, split down the middle. PROJECTS used to be pinned at
  // max-h-44 (176px) and shrink-0, so it held that height whether it had two
  // rows or twenty, and REPORTS lived on whatever was left — which on a short
  // window was almost nothing.
  //
  // Now the space between the nav and the footer is halved, with the half as a
  // ceiling rather than an allotment: neither list may take more than half, and
  // neither reserves a half it isn't using. Two projects and thirty reports
  // gives PROJECTS its two rows and hands the other eight to REPORTS.
  //
  // `flex-basis: 50%` resolves against the whole sidebar rather than the gap
  // between nav and footer, but both sections carry the same basis and the same
  // shrink factor, so they absorb that overflow equally and land on exactly
  // half of what is actually free. The max-height does the rest: clamping a
  // section to its content is a max violation, which the flex algorithm
  // resolves by freezing that section and giving the remainder to the other.
  //
  // Deliberately no min-height. A floor would only bind on windows under about
  // 640px, and there it pushes the footer (Admin, the account row) out of the
  // sidebar entirely. Halving already guarantees fairness — measured in Chrome,
  // a 760px window lands on three rows each with or without a floor — so the
  // floor bought nothing above 640px and broke the layout below it.
  const SIDEBAR_ROW_PX = 31.5
  const SIDEBAR_ROW_GAP_PX = 2
  const SIDEBAR_SECTION_HEADER_PX = 20.5
  const SIDEBAR_SECTION_SLACK_PX = 2

  // A row is py-1.5 (12px) around text-[13px] at normal leading (~19.5px), rows
  // sit 2px apart (space-y-0.5), and the uppercase header is ~20.5px. The slack
  // keeps sub-pixel rounding from summoning a scrollbar on a list that fits.
  // An empty list still renders one row — its call to action — so the minimum
  // content is one row either way.
  const sectionContentHeight = (count: number) => {
    const rows = Math.max(1, count)
    return Math.ceil(
      SIDEBAR_SECTION_HEADER_PX + rows * SIDEBAR_ROW_PX + (rows - 1) * SIDEBAR_ROW_GAP_PX
    ) + SIDEBAR_SECTION_SLACK_PX
  }

  // Dividing height between two panes assumes there is height to divide. The
  // sidebar spends ~493px on chrome — logo, New report, seven nav items, Admin,
  // the account row, the version line — before either list gets a pixel, so on
  // a short viewport there is nothing left to halve: measured in Chrome, below
  // 597px each list renders zero rows and you are left with two bare headers.
  // (main is worse here, not better — it needs 742px before a single report is
  // visible, and drops the footer off-screen below 673px.)
  //
  // So below the threshold the sidebar stops dividing and simply scrolls as one
  // column: sections take their natural height, their inner scrollers are
  // switched off, and every project and report stays reachable. That is what a
  // phone drawer is expected to do anyway. Above it, nothing changes.
  //
  // Folding is worth noting as the thing that does NOT solve this: a folded
  // section keeps its header, and the two headers are most of what eats the
  // remaining space on a small phone.
  const SIDEBAR_SCROLL_BELOW_PX = 620
  const sidebarEl = ref<HTMLElement | null>(null)
  // Assume roomy until measured so SSR and the first client render agree.
  const sidebarHeight = ref(SIDEBAR_SCROLL_BELOW_PX + 1)
  const isShortSidebar = computed(() => sidebarHeight.value < SIDEBAR_SCROLL_BELOW_PX)

  // The element's own box, not window.innerHeight: the sidebar sits below the
  // top banner when there is one, and on iOS its height changes as the address
  // bar collapses and expands mid-scroll.
  let sidebarResizeObserver: ResizeObserver | null = null
  onMounted(() => {
    if (!sidebarEl.value || typeof ResizeObserver === 'undefined') return
    sidebarResizeObserver = new ResizeObserver((entries) => {
      const h = entries[0]?.contentRect.height
      if (h) sidebarHeight.value = h
    })
    sidebarResizeObserver.observe(sidebarEl.value)
  })
  onBeforeUnmount(() => sidebarResizeObserver?.disconnect())

  // Folding a section drops it to its header and gives up its share entirely,
  // which is the deliberate way to hand the whole sidebar to the other list.
  const projectsOpen = ref(true)
  const reportsOpen = ref(true)
  const sectionStyle = (count: number, open: boolean) => {
    // Short sidebar: natural height, no ceiling — the container scrolls instead.
    if (isShortSidebar.value) return { flex: '0 0 auto', maxHeight: 'none' }
    return open
      ? { flex: '1 1 50%', maxHeight: `${sectionContentHeight(count)}px` }
      : { flex: '0 0 auto', maxHeight: 'none' }
  }
  const projectsSectionStyle = computed(() => sectionStyle(projects.value.length, projectsOpen.value))
  const reportsSectionStyle = computed(() => sectionStyle(sortedRecentReports.value.length, reportsOpen.value))
  // Keep the list fresh when the user moves between reports (titles/new reports).
  watch(() => route.path, (path) => {
    if (path === '/reports' || path.startsWith('/reports/')) fetchRecentReports()
    if (path.startsWith('/projects')) fetchProjects()
  })

  // ── Projects: create / rename / delete dialogs + per-row menu ───────────
  const projectDialogOpen = ref(false)
  const projectDialogMode = ref<'create' | 'rename'>('create')
  const projectDialogName = ref('')
  const projectDialogTarget = ref<any>(null)
  const projectDialogBusy = ref(false)
  const projectDeleteOpen = ref(false)
  const projectDeleteBusy = ref(false)
  const projectMenuOpen = ref(false)
  const projectMenuPos = ref({ x: 0, y: 0 })
  const menuProject = ref<any>(null)

  const openCreateProject = () => {
    projectDialogMode.value = 'create'
    projectDialogName.value = ''
    projectDialogTarget.value = null
    projectDialogOpen.value = true
  }
  const openRenameProject = (project: any) => {
    projectDialogMode.value = 'rename'
    projectDialogName.value = project.name
    projectDialogTarget.value = project
    projectDialogOpen.value = true
  }
  const submitProjectDialog = async () => {
    const name = projectDialogName.value.trim()
    if (!name || projectDialogBusy.value) return
    projectDialogBusy.value = true
    try {
      if (projectDialogMode.value === 'create') {
        const created: any = await createProject({ name })
        projectDialogOpen.value = false
        if (created?.id) router.push(`/projects/${created.id}`)
      } else if (projectDialogTarget.value) {
        await updateProject(projectDialogTarget.value.id, { name })
        projectDialogOpen.value = false
      }
    } catch (e: any) {
      reportToast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
      projectDialogBusy.value = false
    }
  }
  const openProjectMenu = (e: MouseEvent, project: any) => {
    menuProject.value = project
    const x = Math.min(e.clientX, (typeof window !== 'undefined' ? window.innerWidth : 9999) - 216)
    const y = Math.min(e.clientY, (typeof window !== 'undefined' ? window.innerHeight : 9999) - 140)
    projectMenuPos.value = { x: Math.max(8, x), y: Math.max(8, y) }
    projectMenuOpen.value = true
  }
  const currentProjectActionsAvailable = (project: any) => !!(project?.can_manage || project?.is_owner)
  const currentProjectActions = computed(() => {
    const project = menuProject.value
    if (!project) return [] as any[]
    const actions: any[] = []
    if (project.can_manage) {
      actions.push({ label: t('projects.menu.rename'), icon: 'i-heroicons-pencil-square', click: () => openRenameProject(project) })
    }
    if (project.is_owner) {
      actions.push({ label: t('projects.menu.delete'), icon: 'i-heroicons-trash', danger: true, click: () => { projectDialogTarget.value = project; projectDeleteOpen.value = true } })
    }
    return actions
  })
  const doDeleteProject = async () => {
    const p = projectDialogTarget.value
    if (!p || projectDeleteBusy.value) return
    projectDeleteBusy.value = true
    try {
      await deleteProject(p.id)
      projectDeleteOpen.value = false
      // Contained reports were archived — refresh in case any list is stale.
      fetchRecentReports()
      if (route.path.startsWith(`/projects/${p.id}`)) router.push('/')
    } catch (e: any) {
      reportToast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
      projectDeleteBusy.value = false
    }
  }
  watch(() => route.path, () => { projectMenuOpen.value = false })

  // ── Move report → project modal ─────────────────────────────────────────
  const moveOpen = ref(false)
  const moveBusy = ref(false)
  const moveTargetReport = ref<any>(null)
  const openMoveToProject = (report: any) => {
    moveTargetReport.value = report
    moveOpen.value = true
    fetchProjects()
  }
  const isProjectShared = (project: any) =>
    project && (project.access === 'org' || (project.member_count || 0) > 0)
  const doMoveReport = async (projectId: string | null) => {
    const r = moveTargetReport.value
    if (!r || moveBusy.value) return
    moveBusy.value = true
    try {
      await moveReport(r.id, projectId)
      moveOpen.value = false
      // Patch in place instead of refetching: the report stays in the recent
      // list (a project doesn't remove it from "my reports") and just picks up
      // the accent strip — refetching would reorder the list under the cursor.
      const patched = applyProjectLocally(r.id, projectId ? projects.value.find((p: any) => p.id === projectId) : null)
      if (!patched) fetchRecentReports() // row not in the sidebar list yet
    } catch (e: any) {
      reportToast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
      moveBusy.value = false
    }
  }

  // ── Drag a report row onto a project row ────────────────────────────────
  // Same move as the menu item above, without the modal. The report keeps its
  // place in the REPORTS list; only its accent strip changes.
  const { draggingReport, startReportDrag, endReportDrag, droppedReportId } = useReportDrag()
  const dropProjectId = ref<string | null>(null)
  // A drag cancelled with Escape (or dropped anywhere else) never reaches the
  // row's drop handler, so clear the highlight off the drag ending instead.
  watch(draggingReport, (v) => { if (!v) dropProjectId.value = null })

  // Mirror a completed (or optimistic) move onto the sidebar row. `project` is
  // the target project mini, or null to drop the report back to the root.
  const applyProjectLocally = (reportId: string, project: any | null) => {
    const row = recentReports.value.find((r: any) => r.id === reportId)
    if (!row) return false
    row.project_id = project ? project.id : null
    row.project = project ? { id: project.id, name: project.name, color: project.color ?? null } : null
    return true
  }

  const onProjectDragOver = (e: DragEvent, project: any) => {
    if (!draggingReport.value) return // let file drags etc. fall through
    e.preventDefault() // required, or the browser refuses the drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
    dropProjectId.value = project.id
  }
  const onProjectDragEnter = (project: any) => {
    if (!draggingReport.value) return
    dropProjectId.value = project.id
  }
  const onProjectDragLeave = (e: DragEvent, project: any) => {
    // Moving between the row's own children fires dragleave too; only clear
    // when the pointer actually left the row.
    const to = e.relatedTarget as Node | null
    if (to && (e.currentTarget as HTMLElement)?.contains(to)) return
    if (dropProjectId.value === project.id) dropProjectId.value = null
  }
  const onProjectDrop = async (e: DragEvent, project: any) => {
    dropProjectId.value = null
    const reportId = droppedReportId(e)
    endReportDrag()
    if (!reportId || !project?.id) return
    const row = recentReports.value.find((r: any) => r.id === reportId)
    const previous = row ? { id: row.project_id, project: row.project } : null
    if (previous && previous.id === project.id) return // already there
    applyProjectLocally(reportId, project) // optimistic: strip appears at once
    try {
      await moveReport(reportId, project.id)
      // Dragged in from a page whose rows aren't the sidebar's (e.g. /reports):
      // pull the list so the strip shows up there too.
      if (!row) fetchRecentReports()
      reportToast.add({ title: t('projects.movedTo', { name: project.name }), color: 'green' })
    } catch (err: any) {
      if (row && previous) { row.project_id = previous.id; row.project = previous.project }
      reportToast.add({ title: t('common.error'), description: String(err?.data?.detail || err?.message || ''), color: 'red' })
    }
  }

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
      { label: t('projects.moveToProject'), icon: 'i-heroicons-folder-arrow-down', click: () => openMoveToProject(report) },
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

  // Only multi-org users get a switcher; with one org the header stays a plain
  // link home and nothing hints that switching exists.
  const hasMultipleOrgs = computed<boolean>(() => userOrganizations.value.length > 1)

  const organizationDropdownItems = computed(() => {
    // whoami returns the orgs unordered (no ORDER BY on the membership join), so
    // sort here — otherwise the rows of a primary header control can reshuffle
    // between page loads.
    const orgs = [...userOrganizations.value].sort((a: any, b: any) =>
      String(a?.name || '').localeCompare(String(b?.name || ''))
    )
    return [
      [{
        label: t('nav.home'),
        icon: 'i-heroicons-home',
        click: () => router.push('/'),
      }],
      orgs.map((org: any) => ({
        label: org.name,
        // Falls back to the generic glyph so rows without a custom org icon
        // still line up with the ones that have one.
        iconUrl: org.icon_url || null,
        icon: 'i-heroicons-building-office-2',
        // Not `active`: the dropdown's item slot scope already exposes an
        // `active` of its own (HeadlessUI's hover state).
        isCurrent: org.id === organization.value?.id,
        disabled: org.id === organization.value?.id,
        click: () => setOrganization(org.id),
      })),
    ]
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

    // The org switcher lives in the workspace header now — keeping a copy here
    // would give multi-org users two of them.
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
    // Inside a project, hand the choice of agents to the project's defaults
    // (see useNewReportProjectContext). Outside one, use the agents pinned in
    // AgentSelector — empty when the selection is Auto, which is exactly how
    // the backend encodes Auto (it resolves the scope per run instead of
    // freezing today's roster onto the report).
    const dataSourceIds = activeProjectId.value
      ? []
      : selectedAgentObjects.value.map((a: any) => a.id)

    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({
          title: 'untitled report',
          files: [],
          ...newReportPayload(dataSourceIds),
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
