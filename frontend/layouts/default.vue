<template>
    <!-- Fixed global onboarding banner shown above everything -->
    <div v-if="showGlobalOnboardingBanner" class="fixed top-0 left-0 right-0 z-[1000]">
      <div
        @click="router.push(showGlobalOnboardingBannerLink)"
        class="mx-auto max-w-screen-2xl text-center cursor-pointer text-white text-sm bg-blue-500/95 hover:bg-blue-600/90 py-2 flex items-center justify-center shadow-md"
      >
        <UIcon name="i-heroicons-rocket-launch" class="h-5 mr-2" />
        <span>{{ showGlobalOnboardingBannerText }}</span>
      </div>
    </div>
  <aside id="separator-sidebar"
    :class="[
      'fixed left-0 z-40 bg-gray-50 transition-all duration-300 -translate-x-full sm:translate-x-0 border-r-[3px] border-gray-100',
      isCollapsed ? 'w-14' : 'w-48',
      showGlobalOnboardingBanner ? 'top-10 bottom-0' : 'top-0 bottom-0'
    ]"
    aria-label="Sidebar">
    <button @click="toggleSidebar" :class="[
            'flex items-center gap-3 rounded-lg transition-all duration-200 bg-gray-50',
            isCollapsed 
              ? 'px-2 py-2 w-full text-center justify-center -mb-4 text-gray-700 hover:text-blue-500' 
              : 'px-1 py-1 mt-3 ml-auto -mb-5 text-gray-400 hover:text-gray-600'
          ]">
            <UTooltip v-if="isCollapsed" text="Expand sidebar" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <UIcon name="heroicons-chevron-right" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <UIcon name="heroicons-chevron-left" />
              </span>
              <span v-if="showText" class="text-xs opacity-75"></span>
            </template>
          </button>
    <div class="h-full px-3 py-4 bg-gray-50 flex flex-col justify-between">

      <ul class="font-normal text-sm">
        <li>
            <button @click="router.push('/')" class="flex items-center text-center p-1 text-gray-700 group">
              <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" class="w-10 " />
            </button>
        </li>

        <!-- Domain Selector - Context for all navigation below -->
        <li v-if="hasDomains" class="mt-6 mb-4">
          <UPopover 
            :popper="{ placement: 'bottom-start', offsetDistance: 4, strategy: 'fixed' }"
            :ui="{ 
              width: 'max-w-none',
              container: 'overflow-visible',
              inner: 'overflow-visible'
            }"
          >
            <button 
              :class="[
                'flex items-center w-full rounded-lg transition-all duration-200',
                'bg-white hover:bg-gray-50',
                'border border-gray-200 shadow-sm hover:shadow hover:border-gray-300',
                isCollapsed ? 'justify-center p-2' : 'gap-1.5 px-2.5 py-2'
              ]"
            >
              <UTooltip v-if="isCollapsed" :text="currentDomainName" :popper="{ placement: 'right' }">
                <span class="flex items-center justify-center w-5 h-5">
                  <UIcon name="heroicons-chevron-down" class="w-4 h-4 text-gray-500" />
                </span>
              </UTooltip>
              <template v-else>
                <span v-if="showText" class="flex-1 text-left min-w-0">
                  <span class="block text-[10px] uppercase tracking-wide text-gray-400 font-semibold leading-none">Domain</span>
                  <span class="block text-xs font-medium text-gray-700 truncate mt-0.5">
                    {{ currentDomainName }}
                    <span v-if="selectedCount > 0" class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full bg-indigo-100 text-indigo-600 text-[10px] font-semibold">
                      {{ selectedCount }}
                    </span>
                  </span>
                </span>
                <UIcon v-if="showText" name="heroicons-chevron-down" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
              </template>
            </button>

            <template #panel>
              <div class="overflow-visible">
                <!-- Domain list - compact -->
                <div class="w-44 bg-white rounded-lg shadow-lg border border-gray-200 overflow-hidden flex-shrink-0">
                  <div class="p-1">
                    <!-- All Domains option -->
                    <button 
                      @click="toggleDomain(null)"
                      @mouseenter="hoveredDomainId = null"
                      @mouseleave="onDomainHoverLeave()"
                      :class="[
                        'w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left transition-colors',
                        isAllDomains ? 'bg-indigo-50 text-indigo-700' : 'text-gray-600 hover:bg-gray-50'
                      ]"
                    >
                      <span class="text-xs font-medium">All Domains</span>
                      <UIcon v-if="isAllDomains" name="heroicons-check" class="w-3 h-3 ml-auto text-indigo-600" />
                    </button>

                    <!-- Divider -->
                    <div class="my-1 border-t border-gray-100" />

                    <!-- Domain list -->
                    <div class="max-h-48 overflow-y-auto">
                      <button 
                        v-for="d in domains" 
                        :key="d.id"
                        @click="toggleDomain(d.id)"
                        @mouseenter="onDomainHover(d.id, $event)"
                        @mouseleave="onDomainHoverLeave()"
                        :class="[
                          'w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left transition-colors',
                          isDomainSelected(d.id) ? 'bg-indigo-50 text-indigo-700' : 'text-gray-600 hover:bg-gray-50'
                        ]"
                      >
                        <span class="text-xs font-medium truncate flex-1">{{ d.name }}</span>
                        <UIcon v-if="isDomainSelected(d.id)" name="heroicons-check" class="w-3 h-3 text-indigo-600 flex-shrink-0" />
                      </button>
                    </div>

                    <!-- Divider -->
                    <div class="my-1 border-t border-gray-100" />

                    <!-- Manage link -->
                    <a 
                      href="/integrations"
                      class="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left text-gray-400 hover:bg-gray-50 hover:text-gray-600 transition-colors"
                    >
                      <UIcon name="heroicons-cog-6-tooth" class="w-3 h-3 flex-shrink-0" />
                      <span class="text-[11px]">Manage</span>
                    </a>
                  </div>
                </div>
              </div>
            </template>
          </UPopover>
        </li>

        <li class="">
          <div class="flex mt-2">
             <button
               name="create-report"
               @click="createNewReport"
               :disabled="creatingReport"
               :class="[
                 'flex items-center px-2 py-2 w-full rounded-lg text-blue-500 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed',
                 isCollapsed ? 'justify-center' : 'gap-3'
               ]">
              <UTooltip v-if="isCollapsed" :text="creatingReport ? 'Creating...' : 'New report'" :popper="{ placement: 'right' }">
                <span class="flex items-center justify-center w-5 h-5 text-lg">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
              </UTooltip>
              <template v-else>
                <span class="flex items-center justify-center w-5 h-5 text-lg">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
                <span v-if="showText" class="text-sm font-medium">{{ creatingReport ? 'Creating...' : 'New report' }}</span>
              </template>
            </button>
          </div>
        </li>

        <li class="">
           <a href="/reports" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Reports" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-chart-pie" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-chart-pie" />
              </span>
              <span v-if="showText" class="text-sm">Reports</span>
            </template>
          </a>
        </li>

        <li class="hidden">
           <NuxtLink to="/files" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Files" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-document-duplicate" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-document-duplicate" />
              </span>
              <span v-if="showText" class="text-sm">Files</span>
            </template>
          </NuxtLink>
        </li>

        <li class="">
           <a href="/instructions" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Instructions" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cube" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cube" />
              </span>
              <span v-if="showText" class="text-sm">Instructions</span>
            </template>
          </a>
        </li>
        <li>
          <a href="/catalog" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Catalog" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-library-icon lucide-library"><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></svg>
              </span>
              <span v-if="showText" class="text-sm">Catalog</span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-library-icon lucide-library"><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></svg>
              </span>
              <span v-if="showText" class="text-sm">Catalog</span>
            </template>
          </a>
        </li>
        <li class="" v-if="isAdmin">
           <a href="/monitoring" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Monitoring" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-activity-icon lucide-activity"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/></svg>
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-activity-icon lucide-activity"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/></svg>
              </span>
              <span v-if="showText" class="text-sm">Monitoring</span>
            </template>
          </a>
        </li>
        <li class="" v-if="isAdmin">
           <a href="/evals" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Evals" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-check-circle" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-check-circle" />
              </span>
              <span v-if="showText" class="text-sm">Evals</span>
            </template>
          </a>
        </li>
      </ul>
      <ul class="font-normal text-sm">
        <li v-if="isMcpEnabled">
          <button 
            @click="showMcpModal = true"
            :class="[
              'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
              isCollapsed ? 'justify-center' : 'gap-3'
            ]"
          >
            <UTooltip v-if="isCollapsed" text="MCP - Connect Claude/Cursor to your data" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <McpIcon class="w-5 h-5" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <McpIcon class="w-5 h-5" />
              </span>
              <span v-if="showText" class="text-sm">MCP</span>
            </template>
          </button>
        </li>
        <li>
           <a href="/settings" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Settings" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cog-6-tooth" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cog-6-tooth" />
              </span>
              <span v-if="showText" class="text-sm">Settings</span>
            </template>
          </a>
        </li>
        <li>
           <a href="https://docs.bagofwords.com" target="_blank" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Documentation" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-book-open" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-book-open" />
              </span>
              <span v-if="showText" class="text-sm">Documentation</span>
            </template>
          </a>
        </li>
        <li>
          <UDropdown :items="userDropdownItems" :popper="{ placement: 'top-start' }">
             <button :class="[
               'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
               isCollapsed ? 'justify-center' : 'gap-3'
             ]">
              <UTooltip v-if="isCollapsed" :text="`Logged in as ${currentUserName}`" :popper="{ placement: 'right' }">
                <div class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-xs font-bold rounded-full">
                  {{ userInitial }}
                </div>
              </UTooltip>
              <template v-else>
                <div class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-xs font-bold rounded-full">
                  {{ userInitial }}
                </div>
                <span v-if="showText" class="text-sm truncate">{{ currentUserName }}</span>
              </template>
            </button>
          </UDropdown>
        </li>
        <li v-if="version">
          <UTooltip text="Version" :popper="{ placement: 'right' }">
            <div class="text-[9px] text-gray-500 p-0 cursor-pointer hover:text-gray-900">
              {{ version }}
            </div>
          </UTooltip>
        </li>
      </ul>
    </div>

  </aside>

  <div :class="['min-h-screen transition-all duration-300', isCollapsed ? 'sm:ml-14' : 'sm:ml-48', showGlobalOnboardingBanner ? 'pt-10' : 'pt-0']">
    <UNotifications />

    <slot />
  </div>

  <McpModal v-model="showMcpModal" />

  <!-- Domain hover flyout (teleported so it never gets clipped by popovers) -->
  <Teleport to="body">
    <Transition
      enter-active-class="transition-all duration-150 ease-out"
      enter-from-class="opacity-0 translate-y-1"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition-all duration-100 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 translate-y-1"
    >
      <div
        v-if="flyout.visible && hoveredDomainId"
        class="fixed z-[2000]"
        :style="{ top: `${flyout.top}px`, left: `${flyout.left}px` }"
        @mouseenter="onFlyoutEnter"
        @mouseleave="onFlyoutLeave"
      >
        <div class="w-[400px] bg-white rounded-xl shadow-2xl border border-gray-200 overflow-hidden">
          <div class="px-4 py-3 border-b border-gray-100">
            <div class="text-sm font-semibold text-gray-900 truncate">
              {{ hoveredDomainDetails?.name || 'Loading…' }}
            </div>
            <div class="text-xs text-gray-400">Domain</div>
          </div>

          <!-- Tabs (underline / border-bottom style like Settings) -->
          <div class="border-b border-gray-200 px-4">
            <nav class="-mb-px flex space-x-6">
              <button
                @click="flyoutTab = 'overview'"
                :class="[
                  flyoutTab === 'overview'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Overview
              </button>
              <button
                @click="flyoutTab = 'tables'"
                :class="[
                  flyoutTab === 'tables'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                  'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
                ]"
              >
                Tables
              </button>
            </nav>
          </div>

          <div class="p-4">
            <div v-if="loadingDomainDetails" class="flex items-center justify-center py-8">
              <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
            </div>

            <template v-else>
              <!-- Overview tab -->
              <div v-if="flyoutTab === 'overview'" class="space-y-4">
                <!-- Description (no title) -->
                <div v-if="hoveredDomainDetails?.description" class="text-xs text-gray-600 leading-relaxed">
                  {{ hoveredDomainDetails.description }}
                </div>

                <!-- Sample Questions -->
                <div v-if="hoveredDomainDetails?.conversation_starters?.length">
                  <div class="text-[10px] uppercase tracking-wider text-gray-400 font-semibold mb-2">Sample Questions</div>
                  <div class="space-y-1.5">
                    <div
                      v-for="(starter, idx) in hoveredDomainDetails.conversation_starters.slice(0, 6)"
                      :key="idx"
                      class="bg-gray-50 border border-gray-100 text-gray-700 text-xs px-3 py-2 rounded-lg"
                    >
                      {{ starter.split('\n')[0] }}
                    </div>
                    <div
                      v-if="hoveredDomainDetails.conversation_starters.length > 6"
                      class="text-[11px] text-gray-400"
                    >
                      +{{ hoveredDomainDetails.conversation_starters.length - 6 }} more
                    </div>
                  </div>
                </div>

                <div
                  v-if="!hoveredDomainDetails?.description && !hoveredDomainDetails?.conversation_starters?.length"
                  class="text-xs text-gray-400 italic py-6 text-center"
                >
                  No details available
                </div>
              </div>

              <!-- Tables tab -->
              <div v-else-if="flyoutTab === 'tables'">
                <div v-if="tablesLoading" class="flex items-center justify-center py-10">
                  <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
                </div>

                <div v-else-if="tablesError" class="text-xs text-gray-500">
                  {{ tablesError }}
                </div>

                <div v-else>
                  <div class="flex items-center justify-between mb-2">
                    <div class="text-[10px] uppercase tracking-wider text-gray-400 font-semibold">Tables</div>
                    <div class="text-[11px] text-gray-400">{{ tablesCount }}</div>
                  </div>

                  <div v-if="tablesCount === 0" class="text-xs text-gray-400 italic py-6 text-center">
                    No tables found
                  </div>

                  <div v-else>
                    <!-- List view (like MentionInput) -->
                    <div v-if="!selectedTable" class="border border-gray-200 rounded-lg overflow-hidden">
                      <div class="max-h-[320px] overflow-auto">
                        <button
                          v-for="t in tablesResources"
                          :key="t.id || t.name"
                          @click="selectTable(t)"
                          class="w-full px-3 py-2 text-left text-xs flex items-center gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0"
                        >
                          <span class="truncate flex-1 text-gray-800 font-medium">{{ t.name }}</span>
                          <span v-if="t.columns?.length" class="text-[11px] text-gray-400 flex-shrink-0">{{ t.columns.length }} cols</span>
                        </button>
                      </div>
                      <div v-if="tablesResources.length === 0" class="px-3 py-3 text-xs text-gray-400">No tables.</div>
                    </div>

                    <!-- Detail view (columns) -->
                    <div v-else class="space-y-2">
                      <div class="flex items-center justify-between">
                        <button
                          @click="selectedTable = null"
                          class="text-[11px] text-gray-500 hover:text-gray-700"
                        >
                          ← Back
                        </button>
                        <div class="text-[11px] text-gray-400">Columns</div>
                      </div>

                      <div class="text-sm font-semibold text-gray-900 truncate">{{ selectedTable.name }}</div>

                      <div class="flex flex-wrap gap-1 max-h-[240px] overflow-auto border border-gray-200 rounded-lg p-2">
                        <span
                          v-for="(col, idx) in (selectedTable.columns || [])"
                          :key="idx"
                          class="px-1.5 py-0.5 bg-white rounded border text-[11px] text-gray-700"
                        >
                          {{ typeof col === 'string' ? col : (col as any).name }}
                          <span v-if="typeof col === 'object' && (col as any).dtype" class="text-gray-400 ml-1">({{ (col as any).dtype }})</span>
                        </span>
                        <span v-if="!(selectedTable.columns || []).length" class="text-[12px] text-gray-400">No columns.</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div class="mt-4 flex justify-end">
                <a
                  v-if="hoveredDomainId"
                  :href="`/integrations/${hoveredDomainId}`"
                  class="text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                >
                  Open data source →
                </a>
              </div>
            </template>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
  import Spinner from '~/components/Spinner.vue'
  import McpIcon from '~/components/icons/McpIcon.vue'
  import McpModal from '~/components/McpModal.vue'

  const { isMcpEnabled } = useOrgSettings()
  const showMcpModal = ref(false)
  
  // Domain management
  const { 
    selectedDomains,
    domains, 
    hasDomains, 
    selectedCount,
    isAllDomains,
    currentDomainName,
    selectedDomainObjects,
    toggleDomain,
    isDomainSelected,
    initDomain 
  } = useDomain()

  // Domain hover preview
  const hoveredDomainId = ref<string | null>(null)
  const hoveredDomainDetails = ref<any>(null)
  const loadingDomainDetails = ref(false)
  const domainDetailsCache = ref<Record<string, any>>({})
  const flyout = reactive({ visible: false, top: 0, left: 0 })
  let flyoutHideTimer: ReturnType<typeof setTimeout> | null = null
  const flyoutTab = ref<'overview' | 'tables'>('overview')

  // Tables tab state (data source schema)
  const tablesCache = ref<Record<string, any[]>>({})
  const tablesLoading = ref(false)
  const tablesError = ref<string | null>(null)
  const selectedTable = ref<any | null>(null)

  const tablesResources = computed<any[]>(() => {
    const id = hoveredDomainId.value
    if (!id) return []
    return tablesCache.value[id] || []
  })
  const tablesCount = computed(() => tablesResources.value.length)

  const showFlyoutAtEvent = (evt: MouseEvent) => {
    const el = evt.currentTarget as HTMLElement | null
    if (!el) return
    const rect = el.getBoundingClientRect()

    // Position to the right of the hovered row, with a small gap.
    // Clamp to viewport height to avoid going off-screen.
    const desiredLeft = rect.right + 12
    const desiredTop = rect.top - 8
    const maxTop = window.innerHeight - 520 // flyout approx height
    flyout.left = Math.max(12, desiredLeft)
    flyout.top = Math.max(12, Math.min(desiredTop, maxTop))
    flyout.visible = true
  }

  const onDomainHover = async (domainId: string, evt: MouseEvent) => {
    if (flyoutHideTimer) {
      clearTimeout(flyoutHideTimer)
      flyoutHideTimer = null
    }
    if (typeof window !== 'undefined') showFlyoutAtEvent(evt)
    hoveredDomainId.value = domainId
    flyoutTab.value = 'overview'
    tablesError.value = null
    selectedTable.value = null
    
    // Check cache first
    if (domainDetailsCache.value[domainId]) {
      hoveredDomainDetails.value = domainDetailsCache.value[domainId]
      return
    }

    hoveredDomainDetails.value = null
    loadingDomainDetails.value = true

    try {
      const { data, error } = await useMyFetch(`/data_sources/${domainId}`, { method: 'GET' })
      if (!error?.value && data?.value) {
        domainDetailsCache.value[domainId] = data.value
        // Only set if still hovering this domain
        if (hoveredDomainId.value === domainId) {
          hoveredDomainDetails.value = data.value
        }
      }
    } catch (e) {
      console.error('Failed to load domain details:', e)
    } finally {
      loadingDomainDetails.value = false
    }
  }

  const onDomainHoverLeave = () => {
    // Give the user time to move cursor from list → flyout
    if (flyoutHideTimer) clearTimeout(flyoutHideTimer)
    flyoutHideTimer = setTimeout(() => {
      flyout.visible = false
      hoveredDomainId.value = null
      hoveredDomainDetails.value = null
    }, 120)
  }

  const onFlyoutEnter = () => {
    if (flyoutHideTimer) {
      clearTimeout(flyoutHideTimer)
      flyoutHideTimer = null
    }
    flyout.visible = true
  }

  const onFlyoutLeave = () => {
    onDomainHoverLeave()
  }

  const fetchTablesForDomain = async (domainId: string) => {
    if (!domainId) return
    if (tablesCache.value[domainId]) return
    tablesLoading.value = true
    tablesError.value = null
    try {
      const { data, error } = await useMyFetch(`/data_sources/${domainId}/schema`, { method: 'GET' })
      if (error?.value) {
        tablesError.value = 'Failed to load tables'
        return
      }
      const payload: any = (data as any)?.value
      const tables = Array.isArray(payload) ? payload : []
      // Respect is_active if present; otherwise keep all
      const filtered = tables.filter((t: any) => t?.is_active !== false)
      tablesCache.value[domainId] = filtered
    } catch (e) {
      tablesError.value = 'Failed to load tables'
    } finally {
      tablesLoading.value = false
    }
  }

  watch(flyoutTab, async (tab) => {
    if (tab !== 'tables') return
    const id = hoveredDomainId.value
    if (!id) return
    await fetchTablesForDomain(id)
  })

  const selectTable = (t: any) => {
    selectedTable.value = t
  }

  
  const workspaceIconUrl = computed<string | null>(() => {
    const orgId = organization.value?.id
    const orgs = (currentUser.value as any)?.organizations || []
    const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
    return org?.icon_url || null
  })
  const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
  const { organization } = useOrganization()
  const { onboarding, fetchOnboarding } = useOnboarding()
  const { useCan } = await import('~/composables/usePermissions')
  const canModifySettings = computed(() => useCan('modify_settings'))
  const showGlobalOnboardingBanner = computed(() => {
    if (!canModifySettings.value) return false
    const ob = onboarding.value as any

    if (!ob) return false
    //if (ob.dismissed) return false
    const steps = ob.steps || {}
    const llmDone = steps.llm_configured?.status === 'done'
    const dataDone = steps.data_source_created?.status === 'done'
    return !(llmDone && dataDone)
  })

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

  const { isExcel } = useExcel()
  const router = useRouter()
  const selectedDataSources = ref<Array<{ id: string | number }>>([])
  const dataSourcesLoaded = ref(false)
  const { $intercom } = useNuxtApp()

  onMounted(async () => {
    try {
      const route = useRoute()
      const inOnboarding = route.path.startsWith('/onboarding')
      if (!inOnboarding) {
        // Fetch onboarding, data sources, and domains in parallel for faster load
        await Promise.all([
          fetchOnboarding({ in_onboarding: false }),
          getDataSourceOptions(),
          initDomain()
        ])
      }
    } catch {}
  })
  const { version, environment, app_url, intercom } = useRuntimeConfig().public
  
  // Sidebar collapse state
  const isCollapsed = ref(false)
  const showText = ref(true) // Controls text visibility during transitions
  const creatingReport = ref(false)
  
  const toggleSidebar = () => {
    if (!isCollapsed.value) {
      // Collapsing: hide text immediately
      showText.value = false
      isCollapsed.value = true
    } else {
      // Expanding: show sidebar first, then text after transition
      isCollapsed.value = false
      setTimeout(() => {
        showText.value = true
      }, 300) // Match the transition duration
    }
  }
  
  const currentUserName = computed<string>(() => {
    const user = currentUser.value as any
    return user?.name || user?.email || 'User'
  })

  const userInitial = computed<string>(() => {
    const name = currentUserName.value
    return name.charAt(0).toUpperCase()
  })

  const userDropdownItems = computed(() => [
    [{
      label: 'Sign out',
      icon: 'heroicons-arrow-left',
      click: signOff
    }]
  ])

  const isAdmin = computed<boolean>(() => {
    const user = currentUser.value as any
    const orgs = user?.organizations || []
    const role = orgs[0]?.role
    return role === 'admin'
  })
 
  if (environment === 'production' && intercom) {
    $intercom.boot()
    watch([currentUser, organization], ([user, org]) => {
      if (user && org) {
        $intercom.update({
          user_id: (user as any).id,
          name: (user as any)?.name,
          email: (user as any)?.email,
          version: version,
          environment: environment,
          app_url: app_url,
          company: {
            company_id: org.id,
            name: org.name
          }
        })
      }
    }, { immediate: true })
  }

  const getDataSourceOptions = async () => {
    const response = await useMyFetch('/data_sources', {
        method: 'GET',
    });

    if ((response as any).error?.value) {
        throw new Error('Could not fetch data sources');
    }

    const list = (((response as any).data?.value) as Array<{ id: string | number }> | undefined);
    selectedDataSources.value = list || [];
}


const createNewReport = async () => {
  if (creatingReport.value) return
  creatingReport.value = true
  
  try {
    // Only fetch if not already loaded
    if (!selectedDataSources.value.length) {
      await getDataSourceOptions()
    }
    
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({title: 'untitled report',
         files: [],
         data_sources: selectedDataSources.value.map((ds) => ds.id)})
    });

    if ((response as any).error?.value) {
        throw new Error('Report creation failed');
    }

    const data = ((response as any).data?.value) as any;
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

