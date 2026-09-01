<template>
  <div>
    <!-- Use existing connection (create mode only) -->
    <div v-if="!isEditMode && existingConnections.length > 0" class="mb-4">
      <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.useExistingLabel') }}</label>
      <USelectMenu
        v-model="selectedExisting"
        :options="existingConnectionOptions"
        option-attribute="name"
        :placeholder="$t('settings.mcpModal.selectExistingPlaceholder')"
        size="sm"
        class="w-full"
      />
      <div v-if="!selectedExistingId" class="relative my-4">
        <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-gray-200 dark:border-gray-800" /></div>
        <div class="relative flex justify-center"><span class="bg-white dark:bg-gray-900 px-2 text-xs text-gray-400 dark:text-gray-500">{{ $t('settings.mcpModal.orCreateNew') }}</span></div>
      </div>
    </div>

    <form @submit.prevent="handleSubmit">
      <template v-if="!selectedExistingId">
        <div class="md:grid md:grid-cols-5 md:gap-6 space-y-4 md:space-y-0">
          <!-- ═══ Left pane: connection details ═══ -->
          <div class="md:col-span-2 space-y-4">
            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.connectionName') }}</label>
              <div class="flex items-center gap-2">
                <!-- Icon picker: tap to cycle through the palette popover -->
                <div class="relative">
                  <button type="button" @click="iconPickerOpen = !iconPickerOpen" :title="$t('data.iconLabel')"
                    class="w-9 h-9 flex items-center justify-center border border-gray-300 dark:border-gray-700 rounded-md text-lg hover:bg-gray-50 dark:hover:bg-gray-800">
                    <span v-if="form.icon">{{ form.icon.replace('emoji:', '') }}</span>
                    <UIcon v-else name="heroicons-cog-6-tooth" class="w-4 h-4 text-gray-400" />
                  </button>
                  <div v-if="iconPickerOpen" class="absolute z-20 mt-1 p-2 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md shadow-lg grid grid-cols-6 gap-1 w-52">
                    <button v-for="e in iconPalette" :key="e" type="button" @click="pickIcon(e)"
                      :class="['w-7 h-7 flex items-center justify-center rounded text-base hover:bg-gray-100 dark:hover:bg-gray-800', form.icon === 'emoji:' + e ? 'ring-1 ring-blue-500 bg-blue-50 dark:bg-blue-950' : '']">
                      {{ e }}
                    </button>
                    <button type="button" @click="pickIcon(null)" :title="$t('data.iconClear')"
                      class="w-7 h-7 flex items-center justify-center rounded hover:bg-gray-100 dark:hover:bg-gray-800">
                      <UIcon name="heroicons-x-mark" class="w-4 h-4 text-gray-400" />
                    </button>
                  </div>
                </div>
                <input v-model="form.name" type="text" :placeholder="$t('data.capiNamePlaceholder')" class="flex-1 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
            </div>

            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.baseUrl') }}</label>
              <input v-model="form.base_url" type="text" placeholder="https://api.example.com/v1" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>

            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.authentication') }}</label>
              <select v-model="form.auth_type" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500">
                <option value="none">{{ $t('settings.mcpModal.authNone') }}</option>
                <option value="bearer">{{ $t('settings.mcpModal.authBearer') }}</option>
                <option value="basic">{{ $t('data.capiAuthBasic') }}</option>
                <option value="api_key">{{ $t('settings.mcpModal.authApiKey') }}</option>
                <option value="oauth_app">{{ $t('data.capiAuthOauth') }}</option>
              </select>
            </div>

            <!-- HTTP Basic auth: username + password sent as an Authorization:
                 Basic header on every request. -->
            <div v-if="form.auth_type === 'basic'" class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.capiUsername') }}</label>
                <input v-model="form.username" type="text" autocomplete="off" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : ''" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.capiPassword') }}</label>
                <input v-model="form.password" type="password" autocomplete="new-password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : ''" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
            </div>

            <!-- OAuth app: admin registers the client; each user signs in and their
                 access token is sent as Bearer on every call. -->
            <div v-if="form.auth_type === 'oauth_app'" class="space-y-3">
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.clientId') }}</label>
                  <input v-model="form.client_id" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.clientSecret') }}</label>
                  <input v-model="form.client_secret" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : ''" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
              <details class="text-xs">
                <summary class="cursor-pointer text-gray-500 dark:text-gray-400">{{ $t('data.advancedOauth') }}</summary>
                <div class="mt-2 space-y-2">
                  <div>
                    <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.authorizeUrl') }}</label>
                    <input v-model="form.authorize_url" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  </div>
                  <div>
                    <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.tokenUrl') }}</label>
                    <input v-model="form.token_url" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  </div>
                  <div>
                    <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.scopes') }}</label>
                    <input v-model="form.scopes" type="text" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  </div>
                </div>
              </details>
            </div>

            <div v-if="form.auth_type === 'bearer'">
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.bearerLabel') }}</label>
              <input v-model="form.token" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.bearerPlaceholder')" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>

            <div v-if="form.auth_type === 'api_key'" class="space-y-3">
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.apiKeyLabel') }}</label>
                <input v-model="form.api_key" type="password" :placeholder="isEditMode ? $t('settings.mcpModal.unchanged') : $t('settings.mcpModal.apiKeyPlaceholder')" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
              <div>
                <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.mcpModal.headerNameLabel') }}</label>
                <input v-model="form.api_key_header" type="text" placeholder="X-API-Key" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
              </div>
            </div>

            <div>
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                {{ $t('data.customHeaders') }}
                <span class="text-gray-400 dark:text-gray-500 font-normal">({{ $t('data.customHeadersHint') }})</span>
              </label>
              <div class="space-y-1.5">
                <div v-for="(header, idx) in customHeaders" :key="idx" class="flex items-center gap-2">
                  <input v-model="header.key" type="text" :placeholder="$t('data.headerName')" class="flex-1 min-w-0 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  <input v-model="header.value" type="text" :placeholder="$t('data.kvValue')" class="flex-1 min-w-0 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  <button type="button" @click="customHeaders.splice(idx, 1)" class="text-gray-400 dark:text-gray-500 hover:text-red-500 p-0.5">
                    <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
              <button type="button" @click="customHeaders.push({ key: '', value: '' })" class="mt-1.5 text-[11px] text-blue-600 hover:text-blue-800">
                {{ $t('data.addHeader') }}
              </button>
            </div>

            <!-- Advanced: CSRF token flow (SAP Gateway & friends) -->
            <details class="text-xs" :open="form.csrf_token_flow">
              <summary class="cursor-pointer text-gray-500 dark:text-gray-400">{{ $t('data.advancedSettings') }}</summary>
              <div class="mt-2 space-y-2">
                <label class="flex items-start gap-2 cursor-pointer">
                  <input type="checkbox" v-model="form.csrf_token_flow" class="mt-0.5" />
                  <span>
                    <span class="block text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('data.csrfFlowLabel') }}</span>
                    <span class="block text-[11px] text-gray-400 dark:text-gray-500">{{ $t('data.csrfFlowHint') }}</span>
                  </span>
                </label>
                <div v-if="form.csrf_token_flow">
                  <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.csrfFetchPath') }}</label>
                  <input v-model="form.csrf_fetch_path" type="text" placeholder="/" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500" />
                </div>
              </div>
            </details>

            <div>
              <button type="button" @click="testConnection" :disabled="testing || !form.base_url" class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50">
                <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
                {{ $t('data.testConnection') }}
              </button>
              <div v-if="testResult" :class="['mt-2 text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400' : 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400']">
                {{ testResult.message }}
              </div>
            </div>
          </div>

          <!-- ═══ Right pane: tools ═══ -->
          <div class="md:col-span-3 md:border-s md:ps-6 border-gray-100 dark:border-gray-800">
            <div class="flex items-center justify-between mb-2">
              <label class="block text-xs font-medium text-gray-700 dark:text-gray-300">
                {{ $t('data.toolsTitle') }}
                <span v-if="endpoints.length" class="text-gray-400 dark:text-gray-500 font-normal">({{ endpoints.length }})</span>
              </label>
              <div class="flex items-center gap-3">
                <button type="button" @click="toggleRawMode" class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
                  {{ rawMode ? $t('data.visualEditor') : $t('data.rawJson') }}
                </button>
                <button v-if="!rawMode" type="button" @click="addTool" data-testid="add-tool" class="text-[11px] font-medium text-blue-600 hover:text-blue-800 flex items-center gap-0.5">
                  <UIcon name="heroicons-plus" class="w-3 h-3" /> {{ $t('data.addTool') }}
                </button>
              </div>
            </div>

            <!-- Visual builder -->
            <div v-if="!rawMode" class="space-y-2">
              <div v-if="!endpoints.length" class="border border-dashed border-gray-200 dark:border-gray-800 rounded-md px-4 py-8 text-center">
                <p class="text-xs text-gray-400 dark:text-gray-500 mb-2">{{ $t('data.noToolsYet') }}</p>
                <button type="button" @click="addTool" class="text-xs font-medium text-blue-600 hover:text-blue-800">
                  {{ $t('data.addTool') }}
                </button>
              </div>

              <div v-for="(ep, epIdx) in endpoints" :key="epIdx" class="border border-gray-200 dark:border-gray-800 rounded-md overflow-hidden">
                <!-- Collapsed row -->
                <div class="w-full flex items-center gap-2 px-2.5 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 select-none" :data-testid="`tool-row-${epIdx}`" @click="toggleExpand(epIdx)">
                  <UIcon name="heroicons-chevron-right" :class="['w-3.5 h-3.5 text-gray-400 transition-transform flex-shrink-0', expanded === epIdx ? 'rotate-90' : '']" />
                  <span :class="['text-[10px] font-semibold px-1.5 py-0.5 rounded flex-shrink-0', methodChipClass(ep.method)]">{{ ep.method }}</span>
                  <span class="text-xs font-mono text-gray-800 dark:text-gray-200 truncate">{{ ep.name || $t('data.untitledTool') }}</span>
                  <span class="text-[11px] text-gray-400 dark:text-gray-500 font-mono truncate hidden sm:inline">{{ ep.path }}</span>
                  <span class="ms-auto flex items-center gap-1.5 flex-shrink-0">
                    <span :class="['text-[10px] px-1.5 py-0.5 rounded', resolvedPolicy(ep) === 'ask' ? 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400' : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400']">
                      {{ policyBadgeLabel(ep) }}
                    </span>
                    <button type="button" @click.stop="removeTool(epIdx)" class="text-gray-400 dark:text-gray-500 hover:text-red-500 p-0.5" :title="$t('data.removeEndpoint')" :data-testid="`remove-tool-${epIdx}`">
                      <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
                    </button>
                  </span>
                </div>

                <!-- Expanded editor -->
                <div v-if="expanded === epIdx" class="border-t border-gray-100 dark:border-gray-800 px-3 py-3 space-y-2.5">
                  <div class="flex items-center gap-2">
                    <select v-model="ep.method" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
                      <option v-for="m in methodOptions" :key="m" :value="m">{{ m }}</option>
                    </select>
                    <input v-model="ep.name" type="text" placeholder="tool_name" class="flex-1 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  </div>
                  <input v-model="ep.path" type="text" placeholder="/customers/{id}" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500" />
                  <input v-model="ep.description" type="text" :placeholder="$t('data.endpointDescriptionPh')" class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />

                  <!-- Parameters -->
                  <div>
                    <div class="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-1">{{ $t('data.parameters') }}</div>
                    <div v-if="ep.parameters.length" class="space-y-1.5">
                      <div v-for="(p, pIdx) in ep.parameters" :key="pIdx" class="space-y-1">
                        <div class="flex items-center gap-1.5">
                          <input v-model="p.name" type="text" placeholder="name" class="flex-1 min-w-0 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500" />
                          <select v-model="p.in" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-1.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
                            <option v-for="loc in inOptions" :key="loc" :value="loc">{{ loc }}</option>
                          </select>
                          <select v-model="p.type" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-1.5 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500">
                            <option v-for="ty in typeOptions" :key="ty" :value="ty">{{ ty }}</option>
                          </select>
                          <label v-if="!p.pinned" class="flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 whitespace-nowrap">
                            <input type="checkbox" v-model="p.required" /> {{ $t('data.reqShort') }}
                          </label>
                          <button type="button" @click="p.pinned = !p.pinned" :title="$t('data.pinnedHint')" :data-testid="`pin-param-${epIdx}-${pIdx}`"
                            :class="['p-0.5 rounded', p.pinned ? 'text-blue-600 bg-blue-50 dark:bg-blue-950' : 'text-gray-400 dark:text-gray-500 hover:text-gray-600']">
                            <UIcon name="heroicons-lock-closed" class="w-3.5 h-3.5" />
                          </button>
                          <button type="button" @click="ep.parameters.splice(pIdx, 1)" class="text-gray-400 dark:text-gray-500 hover:text-red-500 p-0.5" :title="$t('data.kvRemove')">
                            <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                          </button>
                        </div>
                        <!-- Pinned: fixed value replaces description; hidden from the AI. -->
                        <div v-if="p.pinned" class="flex items-center gap-1.5">
                          <input v-model="p.value" type="text" :placeholder="$t('data.pinnedValuePh')" class="flex-1 border border-blue-200 dark:border-blue-900 bg-blue-50/50 dark:bg-blue-950/30 dark:text-gray-100 rounded-md px-2 py-1 text-[11px] font-mono focus:outline-none focus:ring-1 focus:ring-blue-500" />
                          <span class="text-[10px] text-gray-400 dark:text-gray-500 whitespace-nowrap">{{ $t('data.pinnedHint') }}</span>
                        </div>
                        <input v-else v-model="p.description" type="text" :placeholder="$t('data.paramDescriptionPh')" class="w-full border border-gray-200 dark:border-gray-800 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-600 rounded-md px-2 py-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-blue-500" />
                      </div>
                    </div>
                    <button type="button" @click="ep.parameters.push(newParam())" class="mt-1 text-[11px] text-blue-600 hover:text-blue-800">
                      {{ $t('data.kvAdd') }}
                    </button>
                  </div>

                  <!-- Policy + Test row -->
                  <div class="flex items-center justify-between pt-1 border-t border-gray-100 dark:border-gray-800">
                    <label class="flex items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                      {{ $t('data.policyLabel') }}
                      <select v-model="ep.confirmMode" :data-testid="`policy-${epIdx}`" class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-1.5 py-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-blue-500">
                        <option value="auto">{{ $t('data.policyAuto') }} ({{ autoPolicyLabel(ep) }})</option>
                        <option value="allow">{{ $t('data.policyAllow') }}</option>
                        <option value="ask">{{ $t('data.policyAsk') }}</option>
                      </select>
                    </label>
                    <button type="button" @click="toggleTest(ep)" :data-testid="`test-tool-${epIdx}`" class="text-[11px] font-medium text-blue-600 hover:text-blue-800 flex items-center gap-1">
                      <UIcon name="heroicons-play" class="w-3 h-3" /> {{ $t('data.testTool') }}
                    </button>
                  </div>

                  <!-- Inline test runner -->
                  <div v-if="ep._test?.open" class="space-y-1.5 bg-gray-50 dark:bg-gray-800/50 rounded-md p-2">
                    <div v-if="hasModelParams(ep)">
                      <label class="block text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-1">{{ $t('data.testToolArgsLabel') }}</label>
                      <textarea v-model="ep._test.args" rows="3" spellcheck="false" class="w-full border border-gray-200 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100 rounded-md px-2 py-1.5 text-[11px] font-mono focus:outline-none focus:ring-1 focus:ring-blue-500" />
                    </div>
                    <div class="flex items-center gap-2">
                      <button type="button" @click="runToolTest(ep)" :disabled="ep._test.testing" :data-testid="`run-test-${epIdx}`" class="text-[11px] font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded px-2.5 py-1 flex items-center gap-1">
                        <Spinner v-if="ep._test.testing" class="w-3 h-3" />
                        {{ $t('data.testToolRun') }}
                      </button>
                      <span v-if="ep._test.error" class="text-[11px] text-red-600">{{ ep._test.error }}</span>
                    </div>
                    <div v-if="ep._test.result" :class="['rounded-md px-2 py-1.5', ep._test.result.success ? 'bg-green-50 dark:bg-green-950/50' : 'bg-red-50 dark:bg-red-950/50']">
                      <div :class="['text-[11px] font-medium mb-0.5', ep._test.result.success ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400']">
                        {{ ep._test.result.success ? $t('data.testToolSuccess') : (ep._test.result.error || $t('data.testFailed')) }}
                        <span v-if="ep._test.result.success && ep._test.result.content_type" class="font-normal text-gray-400">· {{ ep._test.result.content_type }}</span>
                      </div>
                      <pre v-if="ep._test.result.data_preview" class="text-[10px] text-gray-600 dark:text-gray-300 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto">{{ ep._test.result.data_preview }}{{ ep._test.result.truncated ? '…' : '' }}</pre>
                    </div>
                  </div>
                </div>
              </div>
              <p v-if="endpointsError" class="text-xs text-red-500 mt-1">{{ endpointsError }}</p>
            </div>

            <!-- Raw JSON (advanced) -->
            <div v-else>
              <textarea
                v-model="form.endpoints_json"
                rows="16"
                placeholder='[
  {
    "name": "get_customers",
    "method": "GET",
    "path": "/customers",
    "description": "List customers",
    "parameters": [
      {"name": "status", "in": "query", "type": "string", "description": "Filter by status"}
    ]
  }
]'
                class="w-full border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <p v-if="endpointsError" class="text-xs text-red-500 mt-1">{{ endpointsError }}</p>
            </div>
          </div>
        </div>

        <div v-if="submitError" class="mt-3 text-xs px-3 py-2 rounded bg-red-50 dark:bg-red-950 text-red-700">
          {{ submitError }}
        </div>
      </template>

      <div class="flex items-center justify-end gap-2 pt-4">
        <UButton color="gray" variant="ghost" size="sm" @click="emit('cancel')">{{ $t('data.cancel') }}</UButton>
        <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="selectedExistingId ? false : (!form.base_url || !form.name)">
          {{ isEditMode ? $t('data.save') : $t('data.connect') }}
        </UButton>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
  editConnection?: any
  existingConnections?: any[]
  // Prefill from a Custom API preset (e.g. X Write): base_url + endpoints +
  // OAuth defaults. The admin only supplies client id/secret.
  prefill?: {
    name?: string; base_url?: string; auth?: string
    headers?: Record<string, string>
    endpoints?: any[]
    oauth_defaults?: { authorize_url?: string; token_url?: string; scopes?: string; audience?: string; token_endpoint_auth_method?: string } | null
    description?: string
  } | null
}>()
const emit = defineEmits<{
  // meta.existing marks the "use existing connection" path: nothing was created
  // or updated here — the parent still has to link the connection and owns the
  // outcome feedback.
  (e: 'saved', connection: any, meta?: { existing?: boolean }): void
  (e: 'cancel'): void
}>()

const { t } = useI18n()
const selectedExisting = ref<any>(null)
const existingConnections = computed(() => props.existingConnections || [])
const existingConnectionOptions = computed(() =>
  existingConnections.value.map((c: any) => ({ id: c.id, name: c.name }))
)
const selectedExistingId = computed(() => selectedExisting.value?.id || '')
const isEditMode = computed(() => !!props.editConnection)

const form = reactive({
  name: '',
  base_url: '',
  auth_type: 'none',
  token: '',
  username: '',
  password: '',
  api_key: '',
  api_key_header: 'X-API-Key',
  endpoints_json: '[]',
  icon: '',
  csrf_token_flow: false,
  csrf_fetch_path: '',
  // OAuth app fields (auth_type === 'oauth_app')
  client_id: '',
  client_secret: '',
  authorize_url: '',
  token_url: '',
  scopes: '',
  token_endpoint_auth_method: '',
})

const customHeaders = reactive<{ key: string; value: string }[]>([])

// --- Icon picker ---
const iconPickerOpen = ref(false)
const iconPalette = ['🧩', '🔌', '🌐', '⚙️', '📡', '🛰️', '🏭', '📦', '💼', '🧭', '🗄️', '⚡']
function pickIcon(e: string | null) {
  form.icon = e ? `emoji:${e}` : ''
  iconPickerOpen.value = false
}

// --- Endpoints: visual builder + synced raw-JSON view ---
// `pinned`/`value` model admin-fixed parameters (sent on every call, hidden
// from the AI); `confirmMode` is the tri-state approval policy (auto → infer
// from the HTTP method: writes ask, reads allow).
type EndpointParam = { name: string; in: string; type: string; description: string; required: boolean; pinned: boolean; value: string; extra?: Record<string, any> }
type Endpoint = { name: string; method: string; path: string; description: string; parameters: EndpointParam[]; confirmMode: 'auto' | 'ask' | 'allow'; extra?: Record<string, any>; _test?: { open: boolean; args: string; testing: boolean; result: any; error: string | null } }

const methodOptions = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const inOptions = ['query', 'path', 'body', 'header']
const typeOptions = ['string', 'number', 'integer', 'boolean', 'object', 'array']
const WRITE_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE']

const endpoints = reactive<Endpoint[]>([])
const rawMode = ref(false)
const expanded = ref<number | null>(null)

function newParam(): EndpointParam {
  return { name: '', in: 'query', type: 'string', description: '', required: false, pinned: false, value: '' }
}
function newEndpoint(): Endpoint {
  return { name: '', method: 'GET', path: '', description: '', parameters: [], confirmMode: 'auto' }
}

function addTool() {
  endpoints.push(newEndpoint())
  expanded.value = endpoints.length - 1
}

function removeTool(idx: number) {
  endpoints.splice(idx, 1)
  if (expanded.value === idx) expanded.value = null
  else if (expanded.value !== null && expanded.value > idx) expanded.value -= 1
}

function toggleExpand(idx: number) {
  expanded.value = expanded.value === idx ? null : idx
}

function methodChipClass(method: string): string {
  switch (method) {
    case 'GET': return 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400'
    case 'POST': return 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-400'
    case 'PUT':
    case 'PATCH': return 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400'
    case 'DELETE': return 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400'
    default: return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
  }
}

function isWriteMethod(m: string): boolean {
  return WRITE_METHODS.includes(m)
}

// The policy the runtime will actually apply (mirrors the backend inference:
// explicit choice wins; auto = writes ask, reads allow).
function resolvedPolicy(ep: Endpoint): 'ask' | 'allow' {
  if (ep.confirmMode === 'ask') return 'ask'
  if (ep.confirmMode === 'allow') return 'allow'
  return isWriteMethod(ep.method) ? 'ask' : 'allow'
}

function autoPolicyLabel(ep: Endpoint): string {
  return isWriteMethod(ep.method) ? t('data.policyAsk') : t('data.policyAllow')
}

function policyBadgeLabel(ep: Endpoint): string {
  const label = resolvedPolicy(ep) === 'ask' ? t('data.policyAsk') : t('data.policyAllow')
  return ep.confirmMode === 'auto' ? `${t('data.policyAuto')} · ${label}` : label
}

function setEndpoints(arr: Endpoint[]) {
  endpoints.splice(0, endpoints.length, ...arr)
  expanded.value = null
}

// Coerce arbitrary parsed JSON into the builder shape so round-tripping is
// safe. Unknown keys (enum, schema, items, timeout, …) are kept in per-object
// "extra" bags so switching raw → visual → save never loses advanced fields
// the builder has no inputs for.
function normalizeEndpoints(arr: any[]): Endpoint[] {
  const KNOWN_EP = ['name', 'method', 'path', 'description', 'parameters', 'confirm']
  const KNOWN_PARAM = ['name', 'in', 'type', 'description', 'required', 'value']
  const extrasOf = (obj: any, known: string[]) => {
    const out: Record<string, any> = {}
    for (const [k, v] of Object.entries(obj || {})) {
      if (!known.includes(k)) out[k] = v
    }
    return out
  }
  return arr.map((ep: any) => ({
    name: ep?.name || '',
    method: String(ep?.method || 'GET').toUpperCase(),
    path: ep?.path || '',
    description: ep?.description || '',
    confirmMode: ep?.confirm === true ? 'ask' as const : ep?.confirm === false ? 'allow' as const : 'auto' as const,
    parameters: Array.isArray(ep?.parameters)
      ? ep.parameters.map((p: any) => ({
          name: p?.name || '',
          in: p?.in || 'query',
          type: p?.type || 'string',
          description: p?.description || '',
          required: !!p?.required,
          pinned: p != null && typeof p === 'object' && 'value' in p,
          value: p?.value == null ? '' : (typeof p.value === 'string' ? p.value : JSON.stringify(p.value)),
          extra: extrasOf(p, KNOWN_PARAM),
        }))
      : [],
    extra: extrasOf(ep, KNOWN_EP),
  }))
}

// A pinned value is authored as text but sent typed: numbers/booleans/JSON
// parse back to their declared type so `sap-client=400` (string) and
// `{"limit": 50}` (object) both round-trip correctly.
function coercePinnedValue(raw: string, type: string): any {
  const v = raw.trim()
  if (type === 'number' || type === 'integer') {
    const n = Number(v)
    return Number.isFinite(n) ? n : v
  }
  if (type === 'boolean') return v === 'true'
  if (type === 'object' || type === 'array') {
    try { return JSON.parse(v) } catch { return v }
  }
  return raw
}

// Serialize builder state into the backend endpoint shape, trimming empties.
function serializeEndpoints(): any[] {
  return endpoints.map((ep) => {
    const out: any = { ...(ep.extra || {}), name: ep.name.trim(), method: ep.method, path: ep.path.trim() }
    if (ep.description.trim()) out.description = ep.description.trim()
    if (ep.confirmMode === 'ask') out.confirm = true
    else if (ep.confirmMode === 'allow') out.confirm = false
    const params = ep.parameters
      .filter((p) => p.name.trim())
      .map((p) => {
        const po: any = { ...(p.extra || {}), name: p.name.trim(), in: p.in, type: p.type }
        if (p.pinned) {
          po.value = coercePinnedValue(p.value, p.type)
        } else {
          if (p.description.trim()) po.description = p.description.trim()
          if (p.required) po.required = true
        }
        return po
      })
    if (params.length) out.parameters = params
    return out
  })
}

function toggleRawMode() {
  if (rawMode.value) {
    // Raw -> visual: only switch if the JSON is a valid array.
    try {
      const parsed = JSON.parse(form.endpoints_json)
      if (!Array.isArray(parsed)) return
      setEndpoints(normalizeEndpoints(parsed))
      rawMode.value = false
    } catch {
      // Stay in raw; endpointsError surfaces the problem.
    }
  } else {
    // Visual -> raw: serialize current builder state.
    form.endpoints_json = JSON.stringify(serializeEndpoints(), null, 2)
    rawMode.value = true
  }
}

// --- Per-tool test runner ---
function hasModelParams(ep: Endpoint): boolean {
  return ep.parameters.some((p) => p.name.trim() && !p.pinned)
}

// Sample arguments prefill: example > default > type-shaped placeholder.
function sampleArgs(ep: Endpoint): Record<string, any> {
  const out: Record<string, any> = {}
  for (const p of ep.parameters) {
    if (!p.name.trim() || p.pinned) continue
    const extra = p.extra || {}
    if ('example' in extra) { out[p.name] = extra.example; continue }
    if ('default' in extra) { out[p.name] = extra.default; continue }
    switch (p.type) {
      case 'number': case 'integer': out[p.name] = 0; break
      case 'boolean': out[p.name] = false; break
      case 'object': out[p.name] = {}; break
      case 'array': out[p.name] = []; break
      default: out[p.name] = ''
    }
  }
  return out
}

function toggleTest(ep: Endpoint) {
  if (!ep._test) {
    ep._test = { open: true, args: JSON.stringify(sampleArgs(ep), null, 2), testing: false, result: null, error: null }
  } else {
    ep._test.open = !ep._test.open
  }
}

async function runToolTest(ep: Endpoint) {
  if (!ep._test) return
  ep._test.error = null
  ep._test.result = null
  if (!ep.name.trim() || !ep.path.trim()) {
    ep._test.error = t('data.endpointNeedsName')
    return
  }
  let args: any = {}
  if (hasModelParams(ep)) {
    try {
      args = JSON.parse(ep._test.args || '{}')
    } catch {
      ep._test.error = t('data.invalidArgsJson')
      return
    }
  }
  ep._test.testing = true
  try {
    const response = await useMyFetch('/connections/test-tool', {
      method: 'POST',
      body: {
        type: 'custom_api',
        config: buildConfig(),
        credentials: buildCredentials() || {},
        tool_name: ep.name.trim(),
        arguments: args,
        ...(props.editConnection?.id ? { connection_id: props.editConnection.id } : {}),
      },
    })
    if (response.error?.value) {
      ep._test.result = { success: false, error: errorMessage(response.error.value, t('data.testFailed')) }
    } else {
      ep._test.result = response.data.value
    }
  } catch (e: any) {
    ep._test.result = { success: false, error: errorMessage(e, t('data.testFailed')) }
  } finally {
    ep._test.testing = false
  }
}

watch(() => props.editConnection, async (conn) => {
  if (conn) {
    try {
      const response = await useMyFetch(`/connections/${conn.id}`, { method: 'GET' })
      const detail = response.data.value as any
      if (detail) {
        const config = detail.config || {}
        form.name = detail.name || ''
        form.base_url = config.base_url || ''
        form.auth_type = config.auth_type || (detail.has_credentials ? 'api_key' : 'none')
        form.token = ''
        form.username = ''
        form.password = ''
        form.api_key = ''
        form.api_key_header = config.api_key_header || 'X-API-Key'
        form.icon = config.icon || ''
        form.csrf_token_flow = !!config.csrf_token_flow
        form.csrf_fetch_path = config.csrf_fetch_path || ''
        setEndpoints(Array.isArray(config.endpoints) ? normalizeEndpoints(config.endpoints) : [])
        form.endpoints_json = config.endpoints ? JSON.stringify(config.endpoints, null, 2) : '[]'
        // OAuth app meta (non-secret) round-trips via credentials_meta.
        const meta = detail.credentials_meta || {}
        form.client_id = meta.client_id || ''
        form.client_secret = ''
        form.authorize_url = meta.authorize_url || ''
        form.token_url = meta.token_url || ''
        form.scopes = meta.scopes || ''
        form.token_endpoint_auth_method = meta.token_endpoint_auth_method || ''
        customHeaders.splice(0)
        const hdrs = config.headers || {}
        for (const [key, value] of Object.entries(hdrs)) {
          customHeaders.push({ key, value: String(value) })
        }
        return
      }
    } catch {}
    form.name = conn.name || ''
    form.auth_type = 'none'
  }
}, { immediate: true })

// Apply preset prefill in create mode (X Write etc). Runs immediately so the
// form opens populated; the admin only adds client id/secret.
watch(() => props.prefill, (p) => {
  if (!p || props.editConnection) return
  if (p.name && !form.name) form.name = p.name
  if (p.base_url) form.base_url = p.base_url
  if (p.auth) form.auth_type = p.auth
  if (Array.isArray(p.endpoints)) {
    setEndpoints(normalizeEndpoints(p.endpoints))
    form.endpoints_json = JSON.stringify(p.endpoints, null, 2)
  }
  if (p.headers) {
    customHeaders.splice(0)
    for (const [key, value] of Object.entries(p.headers)) customHeaders.push({ key, value: String(value) })
  }
  const d = p.oauth_defaults
  if (d) {
    if (d.authorize_url) form.authorize_url = d.authorize_url
    if (d.token_url) form.token_url = d.token_url
    if (d.scopes) form.scopes = d.scopes
    if (d.token_endpoint_auth_method) form.token_endpoint_auth_method = d.token_endpoint_auth_method
  }
}, { immediate: true })

const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const submitError = ref<string | null>(null)

// Extract a human-readable message from a useMyFetch error (FetchError) or a
// thrown exception. `detail` is what FastAPI returns for HTTPException.
function errorMessage(err: any, fallback: string): string {
  return err?.data?.detail || err?.message || fallback
}

// Clear the inline submit error — and any stale test result — as soon as the
// user edits any field: a green "Connected" next to a config that has since
// changed reads as a contradiction when the save then fails.
watch(() => ({ ...form }), () => { submitError.value = null; testResult.value = null })

const endpointsError = computed(() => {
  if (rawMode.value) {
    try {
      const parsed = JSON.parse(form.endpoints_json)
      if (!Array.isArray(parsed)) return t('data.mustBeJsonArray')
      return null
    } catch {
      return t('data.invalidJsonShort')
    }
  }
  // Visual builder validation.
  for (const ep of endpoints) {
    if (!ep.name.trim()) return t('data.endpointNeedsName')
    if (!ep.path.trim()) return t('data.endpointNeedsPath', { name: ep.name.trim() })
  }
  const names = endpoints.map((e) => e.name.trim())
  if (new Set(names).size !== names.length) return t('data.endpointNamesUnique')
  return null
})

function buildCredentials(): Record<string, any> | undefined {
  if (form.auth_type === 'bearer' && form.token) return { token: form.token }
  // On edit, blank fields keep the saved values (credentials omitted).
  if (form.auth_type === 'basic' && (form.username || form.password)) {
    return { username: form.username, password: form.password }
  }
  if (form.auth_type === 'api_key' && form.api_key) return { api_key: form.api_key, api_key_header: form.api_key_header }
  if (form.auth_type === 'oauth_app') {
    // Send only non-empty fields; on edit, a blank client_secret is preserved
    // server-side (write-only placeholder).
    const c: Record<string, any> = {}
    if (form.authorize_url) c.authorize_url = form.authorize_url
    if (form.token_url) c.token_url = form.token_url
    if (form.client_id) c.client_id = form.client_id
    if (form.client_secret) c.client_secret = form.client_secret
    if (form.scopes) c.scopes = form.scopes
    if (form.token_endpoint_auth_method) c.token_endpoint_auth_method = form.token_endpoint_auth_method
    return Object.keys(c).length ? c : undefined
  }
  return undefined
}

function buildHeaders(): Record<string, string> {
  const h: Record<string, string> = {}
  for (const { key, value } of customHeaders) {
    if (key.trim()) h[key.trim()] = value
  }
  return h
}

function buildEndpoints(): any[] {
  if (rawMode.value) {
    try { return JSON.parse(form.endpoints_json) } catch { return [] }
  }
  return serializeEndpoints()
}

function buildConfig(): Record<string, any> {
  const cfg: Record<string, any> = {
    base_url: form.base_url,
    auth_type: form.auth_type,
    headers: buildHeaders(),
    endpoints: buildEndpoints(),
  }
  if (form.icon) cfg.icon = form.icon
  if (form.csrf_token_flow) {
    cfg.csrf_token_flow = true
    if (form.csrf_fetch_path.trim()) cfg.csrf_fetch_path = form.csrf_fetch_path.trim()
  }
  return cfg
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: { name: 'test', type: 'custom_api', config: buildConfig(), credentials: buildCredentials() || {} },
    })
    // useMyFetch resolves (never throws) on HTTP errors — surface them as a
    // failed test instead of showing nothing.
    if (response.error?.value) {
      testResult.value = { success: false, message: errorMessage(response.error.value, t('data.testFailed')) }
    } else {
      testResult.value = response.data.value as any
    }
  } catch (e: any) {
    testResult.value = { success: false, message: e?.data?.detail || t('data.testFailed') }
  } finally {
    testing.value = false
  }
}

async function handleSubmit() {
  if (selectedExisting.value) {
    const conn = existingConnections.value.find((c: any) => c.id === selectedExisting.value.id)
    if (conn) emit('saved', conn, { existing: true })
    return
  }

  if (endpointsError.value) return
  submitting.value = true
  submitError.value = null
  const fallback = isEditMode.value ? t('data.updateConnectionFailed') : t('data.createConnectionFailed')
  try {
    const config = buildConfig()
    const credentials = buildCredentials()

    let response: any
    if (isEditMode.value && props.editConnection) {
      response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body: { name: form.name, config, credentials },
      })
    } else {
      // OAuth-app connections authenticate per user: each user signs in and
      // their token runs the calls. Everything else uses the shared system creds.
      const isOAuth = form.auth_type === 'oauth_app'
      response = await useMyFetch('/connections', {
        method: 'POST',
        body: {
          name: form.name, type: 'custom_api', config, credentials,
          auth_policy: isOAuth ? 'user_required' : 'system_only',
          ...(isOAuth ? { allowed_user_auth_modes: ['oauth'] } : {}),
        },
      })
    }

    // useMyFetch resolves (never throws) on HTTP errors — a non-2xx response
    // surfaces via response.error, so surface it inline instead of silently
    // doing nothing (e.g. a duplicate connection name → 409).
    if (response.error?.value) {
      submitError.value = errorMessage(response.error.value, fallback)
      return
    }
    if (response.data.value) emit('saved', response.data.value)
  } catch (e: any) {
    submitError.value = errorMessage(e, fallback)
  } finally {
    submitting.value = false
  }
}
</script>
