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
            <component v-if="item.iconComponent" :is="item.iconComponent" class="w-4 h-4 shrink-0" />
            <UIcon v-else :name="item.icon" class="w-4 h-4 shrink-0" />
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

            <!-- Personal default LLM model -->
            <div class="pt-2 border-t border-gray-100 dark:border-gray-800 space-y-3">
              <div>
                <div class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ $t('profile.general.defaultModel') }}</div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.general.defaultModelSubtitle') }}</p>
              </div>

              <div v-if="modelsLoading" class="py-2">
                <Spinner class="w-4 h-4 text-gray-400" />
              </div>
              <UPopover v-else :popper="{ strategy: 'absolute', placement: 'bottom-start', offset: [0, 8] }">
                <button
                  class="text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-md px-2.5 py-1.5 text-xs flex items-center max-w-[280px]"
                  :disabled="savingDefaultModel"
                >
                  <LLMProviderIcon
                    v-if="selectedDefaultModel"
                    :provider="selectedDefaultModel.provider?.provider_type || 'default'"
                    :icon="true"
                    class="w-4 h-4 flex-shrink-0"
                  />
                  <Icon v-else name="heroicons-cpu-chip" class="w-4 h-4 flex-shrink-0" />
                  <span class="ms-1.5 truncate">{{ selectedDefaultModelLabel }}</span>
                  <Icon name="heroicons-chevron-down" class="w-3.5 h-3.5 ms-1.5 flex-shrink-0 text-gray-400" />
                </button>
                <template #panel="{ close }">
                  <div class="p-2 text-xs max-h-64 overflow-y-auto w-[240px]">
                    <!-- Follow the org default (clears the personal preference) -->
                    <div
                      class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center"
                      @click="() => { saveDefaultModel(null); close(); }"
                    >
                      <div class="me-2">
                        <Icon name="heroicons-building-office" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
                      </div>
                      <div class="flex flex-col flex-1 text-start min-w-0">
                        <span class="font-medium truncate">{{ $t('profile.general.orgDefault') }}</span>
                        <span v-if="orgDefaultModel" class="text-gray-500 dark:text-gray-400 text-[10px] truncate">{{ orgDefaultModel.name }}</span>
                      </div>
                      <Icon v-if="!userDefaultModelId" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
                    </div>
                    <div
                      v-for="m in models"
                      :key="m.id"
                      class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center"
                      @click="() => { saveDefaultModel(m.id); close(); }"
                    >
                      <div class="me-2">
                        <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
                      </div>
                      <div class="flex flex-col flex-1 text-start min-w-0">
                        <span class="font-medium truncate" :title="m.name">{{ m.name }}</span>
                        <span class="text-gray-500 dark:text-gray-400 text-[10px] truncate">{{ m.provider?.name }}</span>
                      </div>
                      <Icon v-if="userDefaultModelId === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
                    </div>
                  </div>
                </template>
              </UPopover>
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

              <!-- Daily breakdown since the start of the month, one small
                   multiple per metric (single series each, so no legends). -->
              <div v-if="dailyCharts.length" class="space-y-3">
                <div
                  v-for="chart in dailyCharts"
                  :key="chart.key"
                  class="rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 px-4 pt-3 pb-1"
                >
                  <div class="flex items-baseline gap-2">
                    <span class="inline-block w-2 h-2 rounded-sm" :style="{ backgroundColor: chart.color }" />
                    <span class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ chart.title }}</span>
                    <span class="text-[11px] text-gray-400 dark:text-gray-500">· {{ $t('profile.usage.dailyNote') }}</span>
                  </div>
                  <ClientOnly>
                    <VChart class="!h-28 w-full" :option="chart.option" autoresize />
                  </ClientOnly>
                </div>
              </div>
            </template>

            <div v-else class="rounded-lg border border-dashed border-gray-200 dark:border-gray-700 bg-gray-50/60 dark:bg-gray-800/30 px-6 py-8 text-center">
              <UIcon name="i-heroicons-chart-bar" class="w-7 h-7 mx-auto text-gray-300 dark:text-gray-600" />
              <p class="mt-3 text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('profile.usage.disabledTitle') }}</p>
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-sm mx-auto">{{ $t('profile.usage.disabledNote') }}</p>
            </div>
          </div>

          <!-- API Keys -->
          <div v-else-if="activeTab === 'apiKeys'" class="space-y-4">
            <div class="flex items-start justify-between gap-4">
              <div>
                <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.apiKeys.title') }}</h3>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.apiKeys.subtitle') }}</p>
              </div>
              <UButton
                color="blue"
                size="sm"
                class="shrink-0"
                :loading="apiKeysCreating"
                @click="createApiKey"
              >
                <UIcon name="i-heroicons-plus" class="w-4 h-4 mr-1" />
                {{ $t('profile.apiKeys.generate') }}
              </UButton>
            </div>

            <!-- Freshly created key (shown once) -->
            <div
              v-if="newApiKey"
              class="rounded-lg border border-blue-200 dark:border-blue-900 bg-blue-50/60 dark:bg-blue-500/10 px-4 py-3 space-y-2"
            >
              <div class="text-xs font-medium text-gray-800 dark:text-gray-200">{{ $t('profile.apiKeys.newKeyTitle') }}</div>
              <div class="flex items-center gap-2">
                <code class="flex-1 min-w-0 font-mono text-xs text-gray-700 dark:text-gray-300 break-all">{{ newApiKey }}</code>
                <UButton size="2xs" color="gray" variant="solid" @click="copyApiKey(newApiKey)">
                  <UIcon name="i-heroicons-clipboard-document" class="w-3.5 h-3.5 mr-1" />
                  {{ $t('profile.apiKeys.copy') }}
                </UButton>
              </div>
              <p class="text-[11px] text-amber-600 dark:text-amber-400">{{ $t('profile.apiKeys.newKeyWarning') }}</p>
            </div>

            <div v-if="apiKeysLoading" class="py-6 flex justify-center">
              <Spinner class="w-5 h-5 text-gray-400" />
            </div>

            <template v-else>
              <p v-if="!apiKeys.length" class="text-xs text-gray-400 dark:text-gray-500 italic">{{ $t('profile.apiKeys.empty') }}</p>
              <div v-else class="border border-gray-200 dark:border-gray-800 rounded-lg divide-y divide-gray-200 dark:divide-gray-800">
                <div
                  v-for="key in apiKeys"
                  :key="key.id"
                  class="flex items-center justify-between gap-3 px-3 py-2.5 group"
                >
                  <div class="min-w-0">
                    <div class="flex items-center gap-2">
                      <span class="text-[13px] font-medium text-gray-800 dark:text-gray-200 truncate">{{ key.name }}</span>
                      <code class="font-mono text-[11px] text-gray-500 dark:text-gray-400">{{ key.key_prefix }}…</code>
                    </div>
                    <div class="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">
                      {{ $t('profile.apiKeys.created', { date: formatApiKeyDate(key.created_at) }) }}
                      ·
                      {{ key.last_used_at ? $t('profile.apiKeys.lastUsed', { date: formatApiKeyDate(key.last_used_at) }) : $t('profile.apiKeys.neverUsed') }}
                    </div>
                  </div>
                  <button
                    @click="deleteApiKey(key)"
                    class="text-gray-400 hover:text-red-500 p-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                    :title="$t('profile.apiKeys.deleteTitle')"
                  >
                    <UIcon name="i-heroicons-trash" class="w-4 h-4" />
                  </button>
                </div>
              </div>
            </template>
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

            <div class="space-y-2">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('profile.appearance.language') }}</label>
              <USelect
                v-model="selectedLocale"
                :options="localeOptions"
                option-attribute="label"
                value-attribute="value"
                class="max-w-xs"
                @change="applyLocale"
              />
              <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('profile.appearance.languageDescription') }}</p>
            </div>
          </div>

          <!-- MCP Server (mirrors McpModal's content) -->
          <div v-else-if="activeTab === 'mcp'" class="space-y-5">
            <div>
              <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('mcpServerModal.title') }}</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('mcpServerModal.subtitle') }}</p>
            </div>

            <div v-if="mcpLoading" class="py-12 flex items-center justify-center">
              <Spinner class="w-6 h-6 text-gray-400" />
            </div>

            <template v-else>
              <!-- Server status + generate/regenerate -->
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 min-w-0">
                  <div class="w-1.5 h-1.5 rounded-full bg-green-500 shrink-0"></div>
                  <code class="font-mono text-gray-700 dark:text-gray-300 truncate">{{ mcpServerUrl }}</code>
                </div>
                <UButton size="xs" color="blue" :loading="mcpCreating" class="shrink-0" @click="regenerateMcpToken">
                  <UIcon :name="apiKeys.length === 0 ? 'heroicons-plus' : 'heroicons-arrow-path'" class="w-3.5 h-3.5 mr-1" />
                  {{ apiKeys.length === 0 ? $t('mcpServerModal.generateToken') : $t('mcpServerModal.regenerateToken') }}
                </UButton>
              </div>

              <!-- Configuration -->
              <div>
                <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{{ $t('mcpServerModal.configuration') }}</div>
                <div class="relative bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
                  <pre class="px-3 py-2.5 pr-20 font-mono text-xs text-gray-700 dark:text-gray-300 overflow-x-auto">{{ mcpConfig }}</pre>
                  <div class="absolute top-2 right-2">
                    <UTooltip :text="mcpCurrentToken ? '' : (apiKeys.length === 0 ? $t('mcpServerModal.generateTokenToCopy') : $t('mcpServerModal.regenerateTokenToCopy'))" :popper="{ placement: 'top' }">
                      <button
                        @click="mcpCurrentToken && copyMcp(mcpConfig)"
                        :disabled="!mcpCurrentToken"
                        :class="['flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors', mcpCurrentToken ? 'text-gray-500 hover:text-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600' : 'text-gray-300 dark:text-gray-600 cursor-not-allowed']"
                      >
                        <UIcon name="heroicons-clipboard-document" class="w-3.5 h-3.5" />
                        {{ $t('mcpServerModal.copy') }}
                      </button>
                    </UTooltip>
                  </div>
                </div>
              </div>

              <!-- Access token -->
              <div>
                <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{{ $t('mcpServerModal.accessToken') }}</div>
                <div v-if="apiKeys.length === 0 && !mcpCurrentToken" class="bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 border-dashed px-4 py-6 text-center">
                  <p class="text-sm text-gray-500 dark:text-gray-400 mb-3">{{ $t('mcpServerModal.noTokenYet') }}</p>
                  <UButton size="sm" color="blue" :loading="mcpCreating" @click="regenerateMcpToken">
                    <UIcon name="heroicons-plus" class="w-4 h-4 mr-1" />
                    {{ $t('mcpServerModal.generateToken') }}
                  </UButton>
                </div>
                <div v-else class="relative bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
                  <div class="px-3 py-2 pr-20 flex items-center gap-3">
                    <code class="font-mono text-xs text-gray-700 dark:text-gray-300 truncate">{{ mcpCurrentToken || '••••••••••••••••••••••••••••••••' }}</code>
                    <span v-if="!mcpCurrentToken && apiKeys.length > 0" class="text-[10px] text-gray-400 shrink-0">{{ mcpFormatDate(apiKeys[0].created_at) }}</span>
                  </div>
                  <div class="absolute top-1/2 -translate-y-1/2 right-2">
                    <UTooltip :text="mcpCurrentToken ? '' : $t('mcpServerModal.regenerateTokenToCopy')" :popper="{ placement: 'top' }">
                      <button
                        @click="mcpCurrentToken && copyMcp(mcpCurrentToken)"
                        :disabled="!mcpCurrentToken"
                        :class="['flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors', mcpCurrentToken ? 'text-gray-500 hover:text-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600' : 'text-gray-300 dark:text-gray-600 cursor-not-allowed']"
                      >
                        <UIcon name="heroicons-clipboard-document" class="w-3.5 h-3.5" />
                        {{ $t('mcpServerModal.copy') }}
                      </button>
                    </UTooltip>
                  </div>
                </div>
              </div>

              <!-- Manage tokens -->
              <div v-if="apiKeys.length > 0" class="pt-2 border-t border-gray-100 dark:border-gray-800">
                <button @click="mcpShowTokens = !mcpShowTokens" class="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-600 transition-colors">
                  <UIcon :name="mcpShowTokens ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3" />
                  {{ $t('mcpServerModal.manageTokens', { n: apiKeys.length }) }}
                </button>
                <div v-if="mcpShowTokens" class="mt-3 border border-gray-200 dark:border-gray-700 rounded-lg divide-y divide-gray-200 dark:divide-gray-700">
                  <div v-for="key in apiKeys" :key="key.id" class="flex items-center justify-between px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors group">
                    <div class="flex items-center gap-3 min-w-0">
                      <code class="font-mono text-xs text-gray-700 dark:text-gray-300">{{ key.key_prefix }}•••••••••</code>
                      <span class="text-[10px] text-gray-400">{{ mcpFormatDate(key.created_at) }}</span>
                    </div>
                    <button @click="deleteApiKey(key)" class="text-gray-400 hover:text-red-500 p-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0" :title="$t('mcpServerModal.deleteTokenTitle')">
                      <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </section>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { markRaw } from 'vue'
import Spinner from '~/components/Spinner.vue'
import McpIcon from '~/components/icons/McpIcon.vue'
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'

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
const { isMcpEnabled } = useOrgSettings()
const colorMode = useColorMode()

const activeTab = ref<'general' | 'instructions' | 'usage' | 'apiKeys' | 'mcp' | 'appearance'>('general')

const navItems = computed(() => {
  const items: any[] = [
    { key: 'general', label: t('profile.nav.general'), icon: 'i-heroicons-user-circle' },
    { key: 'instructions', label: t('profile.nav.instructions'), icon: 'i-heroicons-sparkles' },
    { key: 'usage', label: t('profile.nav.usage'), icon: 'i-heroicons-chart-bar' },
    { key: 'apiKeys', label: t('profile.nav.apiKeys'), icon: 'i-heroicons-key' },
  ]
  if (isMcpEnabled.value) {
    items.push({ key: 'mcp', label: t('nav.mcpServer'), iconComponent: markRaw(McpIcon) })
  }
  items.push({ key: 'appearance', label: t('profile.nav.appearance'), icon: 'i-heroicons-swatch' })
  return items
})

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

// --- General: personal default LLM model ---
// Same list the prompt box selector uses (already access-filtered per user);
// `is_user_default` marks the saved preference, no personal default = follow org.
const models = ref<any[]>([])
const modelsLoading = ref(false)
const modelsLoaded = ref(false)
const userDefaultModelId = ref<string | null>(null)
const savingDefaultModel = ref(false)

const orgDefaultModel = computed(() => models.value.find((m: any) => m.is_default) || null)
const selectedDefaultModel = computed(() => models.value.find((m: any) => m.id === userDefaultModelId.value) || null)
const selectedDefaultModelLabel = computed(() =>
  selectedDefaultModel.value?.name || t('profile.general.orgDefault')
)

async function loadModels() {
  if (modelsLoaded.value) return
  modelsLoading.value = true
  try {
    const res = await useMyFetch('/api/llm/models?is_enabled=true')
    const list = res.data?.value
    models.value = Array.isArray(list) ? list : []
    userDefaultModelId.value = models.value.find((m: any) => m.is_user_default)?.id || null
    modelsLoaded.value = true
  } catch {
    // non-fatal; section just shows the org-default chip
  } finally {
    modelsLoading.value = false
  }
}

async function saveDefaultModel(modelId: string | null) {
  if (savingDefaultModel.value || modelId === userDefaultModelId.value) return
  const previous = userDefaultModelId.value
  userDefaultModelId.value = modelId
  savingDefaultModel.value = true
  try {
    const res = await useMyFetch('/users/me/default_model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    })
    if (res.status.value !== 'success') {
      throw new Error((res.error?.value as any)?.data?.detail || t('profile.general.defaultModelFailed'))
    }
    toast.add({ title: t('profile.general.defaultModelSaved'), color: 'green' })
  } catch (e: any) {
    userDefaultModelId.value = previous
    toast.add({ title: e?.message || t('profile.general.defaultModelFailed'), color: 'red' })
  } finally {
    savingDefaultModel.value = false
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

// --- API keys (personal, per-user) ---
// Shares the /api/api_keys endpoints with McpModal. Keys are scoped to the user
// (not the org), so the same list shows here and in the MCP server modal.
interface ApiKey {
  id: string
  name: string
  key_prefix: string
  key?: string
  created_at: string
  last_used_at?: string | null
}
const apiKeys = ref<ApiKey[]>([])
const apiKeysLoading = ref(false)
const apiKeysCreating = ref(false)
const apiKeysLoaded = ref(false)
const newApiKey = ref<string | null>(null)

const _apiKeyDf = useFormatDate()
function formatApiKeyDate(dateStr: string) {
  return _apiKeyDf.format(dateStr, { year: 'numeric', month: 'short', day: 'numeric' })
}

async function loadApiKeys() {
  if (apiKeysLoaded.value) return
  apiKeysLoading.value = true
  try {
    const res = await useMyFetch('/api/api_keys')
    if (res.data?.value) apiKeys.value = res.data.value as ApiKey[]
    apiKeysLoaded.value = true
  } catch {
    // non-fatal
  } finally {
    apiKeysLoading.value = false
  }
}

async function createApiKey() {
  apiKeysCreating.value = true
  try {
    const res = await useMyFetch('/api/api_keys', { method: 'POST', body: { name: 'API Key' } })
    const created = res.data?.value as ApiKey | null
    if (res.status.value !== 'success' || !created) {
      throw new Error('create failed')
    }
    apiKeys.value = [created, ...apiKeys.value]
    if (created.key) newApiKey.value = created.key
    toast.add({ title: t('profile.apiKeys.toastGenerated'), color: 'green' })
  } catch {
    toast.add({ title: t('profile.apiKeys.toastGenerateFailed'), color: 'red' })
  } finally {
    apiKeysCreating.value = false
  }
}

async function copyApiKey(key: string) {
  await navigator.clipboard.writeText(key)
  toast.add({ title: t('profile.apiKeys.copied'), color: 'green' })
}

async function deleteApiKey(key: ApiKey) {
  if (!confirm(t('profile.apiKeys.confirmDelete'))) return
  try {
    const res = await useMyFetch(`/api/api_keys/${key.id}`, { method: 'DELETE' })
    if (res.status.value !== 'success') throw new Error('delete failed')
    apiKeys.value = apiKeys.value.filter(k => k.id !== key.id)
    if (newApiKey.value && key.key_prefix && newApiKey.value.startsWith(key.key_prefix)) {
      newApiKey.value = null
    }
    toast.add({ title: t('profile.apiKeys.toastDeleted'), color: 'green' })
  } catch {
    toast.add({ title: t('profile.apiKeys.toastDeleteFailed'), color: 'red' })
  }
}

// --- MCP server (mirrors McpModal; shares the same /api/api_keys list) ---
const mcpLoading = ref(false)
const mcpCreating = ref(false)
const mcpBaseUrl = ref('')
const mcpCurrentToken = ref<string | null>(null)
const mcpShowTokens = ref(false)
const mcpLoaded = ref(false)

const mcpServerUrl = computed(() => {
  const base = mcpBaseUrl.value || (typeof window !== 'undefined' ? window.location.origin : '')
  return `${base}/api/mcp`
})
const mcpConfig = computed(() => {
  const token = mcpCurrentToken.value || '<YOUR_API_KEY>'
  return JSON.stringify({
    mcpServers: { bagofwords: { url: mcpServerUrl.value, headers: { Authorization: `Bearer ${token}` } } },
  }, null, 2)
})
function mcpFormatDate(dateStr: string) {
  return _apiKeyDf.format(dateStr, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
async function copyMcp(text: string | undefined) {
  if (!text) return
  await navigator.clipboard.writeText(text)
  toast.add({ title: t('mcpServerModal.toastCopied'), icon: 'i-heroicons-check-circle', color: 'green' })
}
async function loadMcpSettings() {
  try {
    const res = await useMyFetch('/settings')
    if (res.data?.value) mcpBaseUrl.value = (res.data.value as any).base_url || ''
  } catch {}
}
async function regenerateMcpToken() {
  mcpCreating.value = true
  try {
    const res = await useMyFetch('/api/api_keys', { method: 'POST', body: { name: 'MCP' } })
    const created = res.data?.value as ApiKey | null
    if (res.status.value !== 'success' || !created) throw new Error('create failed')
    apiKeys.value = [created, ...apiKeys.value]
    if (created.key) {
      mcpCurrentToken.value = created.key
      toast.add({ title: t('mcpServerModal.toastTokenGenerated'), icon: 'i-heroicons-check-circle', color: 'green' })
    }
  } catch {
    toast.add({ title: t('mcpServerModal.toastTokenFailed'), icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    mcpCreating.value = false
  }
}
async function loadMcp() {
  if (mcpLoaded.value) return
  mcpLoading.value = true
  mcpCurrentToken.value = null
  mcpShowTokens.value = false
  apiKeysLoaded.value = false // ensure a fresh key list for the MCP view
  await Promise.all([loadMcpSettings(), loadApiKeys()])
  mcpLoaded.value = true
  mcpLoading.value = false
}

// --- Usage (per-user quota summary from whoami) ---
// The summary rides on the whoami session, which is only refetched on window
// focus — force a fresh snapshot whenever the tab is shown so the counters
// reflect activity from the current session instead of page-load state.
const { refreshQuotaIfStale } = useUsageQuota()
function loadUsage() {
  refreshQuotaIfStale({ force: true }).catch(() => {})
  loadDailyUsage()
}

// --- Usage: daily breakdown since the start of the month ---
type DailyPoint = { date: string; tokens: number; queries: number; data_bytes: number; spend_usd: number }
const dailyDays = ref<DailyPoint[]>([])

async function loadDailyUsage() {
  const orgId = organization.value?.id
  if (!orgId) return
  try {
    const res = await useMyFetch(`/organizations/${orgId}/usage/daily`)
    const data: any = res.data?.value
    dailyDays.value = data?.enabled ? (data.days || []) : []
  } catch {
    dailyDays.value = []
  }
}

// One small multiple per metric: single series each (no legend — the title
// names it), daily columns, y from zero, per-bar tooltip. Hues are fixed per
// metric and CVD-validated against both surfaces.
const DAILY_METRICS = [
  { key: 'tokens', color: '#2563eb', format: (v: number) => formatNumber(v) },
  { key: 'queries', color: '#0d9488', format: (v: number) => formatNumber(v) },
  { key: 'data_bytes', color: '#7c3aed', format: (v: number) => formatBytes(v) },
] as const

const dailyCharts = computed(() => {
  const days = dailyDays.value
  if (!days.length) return []
  const dark = colorMode.value === 'dark'
  const ink = dark ? '#9ca3af' : '#6b7280'
  const gridLine = dark ? '#1f2937' : '#e5e7eb'
  const labelEvery = Math.max(1, Math.ceil(days.length / 10))
  return DAILY_METRICS.map((metric) => {
    const values = days.map(d => Number((d as any)[metric.key]) || 0)
    return {
      key: metric.key,
      color: metric.color,
      title: t(`profile.usage.${metric.key === 'data_bytes' ? 'data' : metric.key}`),
      option: {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          formatter: (params: any) => {
            const p = params?.[0]
            if (!p) return ''
            return `${days[p.dataIndex].date}<br/><b>${metric.format(values[p.dataIndex])}</b>`
          },
        },
        grid: { left: 4, right: 4, top: 8, bottom: 2, containLabel: true },
        xAxis: {
          type: 'category',
          data: days.map(d => d.date.slice(8)),  // day of month
          axisTick: { show: false },
          axisLine: { lineStyle: { color: gridLine } },
          axisLabel: { color: ink, fontSize: 10, interval: labelEvery - 1 },
        },
        yAxis: {
          type: 'value',
          min: 0,
          splitNumber: 3,
          splitLine: { lineStyle: { color: gridLine } },
          axisLabel: { color: ink, fontSize: 10, formatter: (v: number) => metric.format(v) },
        },
        series: [{
          type: 'bar',
          data: values,
          barWidth: '60%',
          itemStyle: { color: metric.color, borderRadius: [3, 3, 0, 0] },
        }],
      },
    }
  })
})

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

// --- Appearance (per-user language override) ---
// Language is per-user: the org sets a default, but each user can override it,
// persisted per-browser under `bow.locale` (see plugins/i18n.ts). Picking
// "System default" clears the override so the user follows the org default.
const { locale: i18nLocale } = useI18n({ useScope: 'global' })
const { $setLocale } = useNuxtApp() as any

const LOCALE_NATIVE_LABELS: Record<string, string> = {
  en: 'English', es: 'Español', he: 'עברית', fr: 'Français', sv: 'Svenska',
  ar: 'العربية', ru: 'Русский', de: 'Deutsch', pt: 'Português (Brasil)', it: 'Italiano',
}
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])

const orgLocale = ref<{ default_locale: string; enabled_locales: string[]; effective_locale: string }>({
  default_locale: 'en', enabled_locales: ['en'], effective_locale: 'en',
})
const selectedLocale = ref('')

const localeOptions = computed(() => {
  const eff = orgLocale.value.effective_locale || 'en'
  const opts = [{ value: '', label: t('settings.language.systemDefault', { locale: LOCALE_NATIVE_LABELS[eff] || eff }) }]
  for (const code of (orgLocale.value.enabled_locales || [])) {
    opts.push({ value: code, label: LOCALE_NATIVE_LABELS[code] || code })
  }
  return opts
})

async function loadOrgLocale() {
  try {
    const res = await useMyFetch('/organization/locale')
    const body = res.data?.value as any
    if (body) orgLocale.value = body
  } catch {}
  try {
    const stored = localStorage.getItem('bow.locale')
    selectedLocale.value = stored && (orgLocale.value.enabled_locales || []).includes(stored) ? stored : ''
  } catch { selectedLocale.value = '' }
}

function applyDocumentLocale(code: string) {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('lang', code)
  document.documentElement.setAttribute('dir', RTL_LOCALES.has(code) ? 'rtl' : 'ltr')
}

function applyLocale(value: string) {
  if (value) {
    // Explicit per-user override (persists to bow.locale).
    if (typeof $setLocale === 'function') $setLocale(value)
    else { (i18nLocale as any).value = value; applyDocumentLocale(value) }
  } else {
    // System default: clear the override and follow the org's effective locale.
    try { localStorage.removeItem('bow.locale') } catch {}
    const eff = orgLocale.value.effective_locale || 'en'
    ;(i18nLocale as any).value = eff
    applyDocumentLocale(eff)
  }
}

// Load data when the modal opens. The component is v-if-mounted already-open,
// so the watcher must be immediate (a non-immediate watch never sees the
// initial true and the General/Appearance data would never load on first open).
watch(isOpen, (open) => {
  if (open) {
    syncNameInput()
    loadOrgLocale()
    loadModels()
    if (activeTab.value === 'instructions') loadInstructions()
    if (activeTab.value === 'apiKeys') loadApiKeys()
    if (activeTab.value === 'mcp') loadMcp()
    if (activeTab.value === 'usage') loadUsage()
  } else {
    // Reset one-time key reveal and force a fresh fetch on next open.
    newApiKey.value = null
    apiKeysLoaded.value = false
    modelsLoaded.value = false
    mcpLoaded.value = false
    mcpCurrentToken.value = null
  }
}, { immediate: true })
watch(activeTab, (tab) => {
  if (tab === 'instructions') loadInstructions()
  if (tab === 'apiKeys') loadApiKeys()
  if (tab === 'mcp') loadMcp()
  if (tab === 'usage') loadUsage()
})
</script>
