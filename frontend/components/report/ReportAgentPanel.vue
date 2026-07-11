<template>
  <div class="h-full flex flex-col overflow-hidden">
    <!-- Agent selector dropdown -->
    <div class="px-4 pt-4 pb-2 flex-shrink-0 bg-gradient-to-b from-indigo-50/40 to-transparent dark:from-gray-900 dark:to-transparent">
      <div v-if="visibleAgents.length === 0" class="text-xs text-gray-400 dark:text-gray-500 italic text-center py-4">
        {{ $t('reportAgent.noAgents') }}
      </div>
      <div v-else-if="visibleAgents.length === 1" class="flex items-center gap-2">
        <button v-if="showClose" @click="$emit('close')" class="hover:bg-gray-100 dark:hover:bg-gray-800/70 p-1 rounded">
          <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
        </button>
        <Icon v-if="(visibleAgents[0] as any).isGlobal" name="heroicons:globe-alt" class="w-5 h-5 text-gray-500 dark:text-gray-400 flex-shrink-0" />
        <DataSourceIcon v-else :type="visibleAgents[0].type || visibleAgents[0].connections?.[0]?.type" :connector-key="(visibleAgents[0] as any).connector_key || visibleAgents[0].connections?.[0]?.connector_key" :icon="visibleAgents[0].icon" class="h-5 flex-shrink-0" />
        <span class="text-sm font-semibold text-gray-900 dark:text-white truncate">{{ visibleAgents[0].name }}</span>
        <span
          v-if="stageBadge(visibleAgents[0])"
          :class="['flex-shrink-0 text-[10px] rounded border px-1 py-0.5', stageBadge(visibleAgents[0])?.badge]"
        >{{ stageBadge(visibleAgents[0])?.label }}</span>
      </div>
      <div v-else class="flex items-center gap-2">
        <button v-if="showClose" @click="$emit('close')" class="hover:bg-gray-100 dark:hover:bg-gray-800/70 p-1 rounded flex-shrink-0">
          <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
        </button>
        <div class="relative flex-1" ref="dropdownRef">
        <button
          @click="dropdownOpen = !dropdownOpen"
          class="w-full flex items-center gap-2 px-3 py-2 border border-gray-200 dark:border-gray-800 rounded-lg text-sm hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors bg-white/80 dark:bg-gray-900/80"
        >
          <Icon v-if="(selectedAgent as any)?.isGlobal" name="heroicons:globe-alt" class="w-5 h-5 text-gray-500 dark:text-gray-400 flex-shrink-0" />
          <DataSourceIcon v-else-if="selectedAgent" :type="selectedAgent.type || selectedAgent.connections?.[0]?.type" :connector-key="(selectedAgent as any).connector_key || selectedAgent.connections?.[0]?.connector_key" :icon="selectedAgent.icon" class="h-5 flex-shrink-0" />
          <span class="truncate flex-1 text-start font-medium text-gray-900 dark:text-white">
            {{ selectedAgent?.name || $t('reportAgent.selectAgent') }}
          </span>
          <Icon name="heroicons:chevron-down" class="w-4 h-4 text-gray-400 dark:text-gray-500 flex-shrink-0 transition-transform" :class="{ 'rotate-180': dropdownOpen }" />
        </button>
        <Transition
          enter-active-class="transition duration-100 ease-out"
          enter-from-class="opacity-0 scale-95"
          enter-to-class="opacity-100 scale-100"
          leave-active-class="transition duration-75 ease-in"
          leave-from-class="opacity-100 scale-100"
          leave-to-class="opacity-0 scale-95"
        >
          <div v-if="dropdownOpen" class="absolute z-10 mt-1 w-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg shadow-lg overflow-hidden">
            <button
              v-for="agent in visibleAgents"
              :key="agent.id"
              @click="selectAgent(agent.id)"
              class="w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
              :class="selectedAgentId === agent.id ? 'bg-indigo-50 text-indigo-700' : 'text-gray-700 dark:text-gray-300'"
            >
              <Icon v-if="(agent as any).isGlobal" name="heroicons:globe-alt" class="w-4 h-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
              <DataSourceIcon v-else :type="agent.type || agent.connections?.[0]?.type" :connector-key="(agent as any).connector_key || agent.connections?.[0]?.connector_key" :icon="agent.icon" class="h-4 flex-shrink-0" />
              <span class="truncate flex-1 text-start font-medium">{{ agent.name }}</span>
              <span
                v-if="stageBadge(agent)"
                :class="['flex-shrink-0 text-[10px] rounded border px-1 py-0.5', stageBadge(agent)?.badge]"
              >{{ stageBadge(agent)?.label }}</span>
              <Icon v-if="selectedAgentId === agent.id" name="heroicons:check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
            </button>
          </div>
        </Transition>
        </div>
      </div>
    </div>

    <!-- Connect prompt for a user_required agent the user hasn't authenticated.
         Driven by selectedAgentDetails (fetched from /data_sources/{id}) — NOT
         the report-sourced selectedAgent, whose connections carry no user_status
         (the report endpoint skips per-user status), which would false-positive
         for admins on the service-account fallback.
         OAuth-only (Entra/OBO) redirects straight to the provider; otherwise the
         credentials modal opens. -->
    <div v-if="selectedAgentDetails && needsUserConnection(selectedAgentDetails)" class="px-4 pb-2 flex-shrink-0">
      <button
        @click="onConnect(selectedAgentDetails)"
        :disabled="connectingId === selectedAgentDetails.id"
        class="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
      >
        <Spinner v-if="connectingId === selectedAgentDetails.id" class="w-3.5 h-3.5" />
        <Icon v-else name="heroicons-key" class="w-3.5 h-3.5" />
        {{ $t('data.connect') }}
      </button>
    </div>

    <!-- Tabs -->
    <div v-if="selectedAgent" class="border-b border-gray-200 dark:border-gray-800 px-4 flex-shrink-0">
      <nav class="-mb-px flex space-x-4">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          @click="activeTab = tab.key"
          :class="[
            activeTab === tab.key
              ? 'border-indigo-500 text-indigo-600'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-700 hover:text-gray-700 dark:hover:text-gray-300',
            'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
          ]"
        >
          {{ tab.label }}
          <span v-if="tab.count > 0" class="ms-1 text-[10px] text-gray-400 dark:text-gray-500">({{ tab.count }})</span>
        </button>
      </nav>
    </div>

    <!-- Tab content -->
    <div v-if="selectedAgent" class="flex-1 min-h-0 overflow-y-auto p-4 bg-white dark:bg-gray-900">
      <!-- Overview tab -->
      <div v-if="activeTab === 'overview'" class="space-y-5">
        <div v-if="!selectedAgentDetails" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
        </div>
        <template v-else>
          <!-- Primary Instruction -->
          <div>
            <div class="flex items-center gap-2 mb-2">
              <div class="text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-500 font-semibold">{{ $t('reportAgent.primaryInstruction') }}</div>
              <button
                v-if="canCreateInstructions && selectedAgentDetails.primary_instruction"
                @click="openInstruction(selectedAgentDetails.primary_instruction)"
                class="text-[10px] text-blue-600 hover:underline"
              >{{ $t('dataSource.edit') }}</button>
              <button
                v-else-if="canCreateInstructions"
                @click="activeTab = 'instructions'; creatingInstruction = true; creatingPrimaryInstruction = true"
                class="text-[10px] text-blue-600 hover:underline"
              >{{ $t('reportAgent.create') }}</button>
            </div>
            <div v-if="selectedAgentDetails.primary_instruction">
              <InstructionText
                :text="selectedAgentDetails.primary_instruction.text"
                :references="selectedAgentDetails.primary_instruction.references || []"
                :prose="true"
              />
            </div>
            <div v-else class="text-xs text-gray-400 dark:text-gray-500 italic">{{ $t('reportAgent.noInstruction') }}</div>
          </div>

          <!-- Conversation Starters -->
          <div v-if="starterPrompts.length || canUpdateDataSource">
            <div class="flex items-center gap-2 mb-2">
              <div class="text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-500 font-semibold">{{ $t('dataSource.conversationStarters') }}</div>
              <button v-if="canUpdateDataSource" @click="openEditStarters" class="text-[10px] text-blue-600 hover:underline">{{ $t('dataSource.edit') }}</button>
            </div>
            <div v-if="starterPrompts.length" class="space-y-1.5">
              <button
                v-for="(p, idx) in starterPrompts"
                :key="p.id || idx"
                @click="$emit('starter-click', p.text)"
                class="w-full text-start text-xs px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 text-gray-700 dark:text-gray-300 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-700 transition-colors"
              >
                {{ (p.text || '').split('\n')[0] }}
              </button>
            </div>
            <div v-else class="text-xs text-gray-400 dark:text-gray-500 italic">{{ $t('reportAgent.noStarters') }}</div>
          </div>
        </template>
      </div>

      <!-- Instructions tab -->
      <div v-else-if="activeTab === 'instructions'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
        </div>
        <template v-else>
          <!-- Loading instruction from external click -->
          <div v-if="instructionLoading" class="flex items-center justify-center py-10">
            <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
          </div>

          <!-- Instruction form view (edit or create) -->
          <div v-else-if="selectedInstruction || creatingInstruction" class="flex flex-col h-full -m-4">
            <button
              @click="closeInstructionForm"
              class="flex items-center gap-1 px-4 pt-3 pb-2 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 flex-shrink-0"
            >
              <Icon name="heroicons:chevron-left" class="w-3 h-3 rtl-flip" />
              {{ $t('reportAgent.allInstructions') }}
            </button>
            <!-- Per-hunk tracked-changes review: shown when this instruction has
                 pending suggestions and the user can approve. Same component as
                 the Knowledge Explorer. A toggle drops into the editor. -->
            <template v-if="showInstructionReview">
              <div class="px-4 pb-1 flex items-center justify-end shrink-0">
                <button class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 inline-flex items-center gap-1" @click="forceInstructionEdit = true">
                  <Icon name="heroicons:pencil-square" class="w-3 h-3" />Edit instead
                </button>
              </div>
              <InstructionTrackedChanges
                :key="'review-' + selectedInstruction.id"
                :instruction-id="selectedInstruction.id"
                :can-approve="canCreateInstructions"
                @changed="refreshInstructions()"
                @empty="instructionReviewEmpty = true"
              />
            </template>
            <template v-else>
              <!-- Unpublished-build warning banner (edit mode only) -->
              <div
                v-if="selectedInstruction && selectedInstruction.current_build_id && ['draft', 'pending_approval'].includes(selectedInstruction.current_build_status)"
                class="mx-4 mb-2 px-2.5 py-1.5 rounded-md border border-amber-200 bg-amber-50 flex items-start gap-2"
              >
                <Icon name="heroicons:exclamation-triangle" class="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
                <div class="flex-1 min-w-0">
                  <div class="text-[11px] text-amber-800">
                    {{ selectedInstruction.current_build_status === 'draft'
                      ? $t('reportAgent.unpublishedDraft')
                      : $t('reportAgent.pendingReview') }}
                  </div>
                  <button
                    v-if="canViewBuilds"
                    @click="openBuildExplorer(selectedInstruction.current_build_id)"
                    class="mt-0.5 text-[11px] text-amber-700 hover:text-amber-900 underline"
                  >
                    {{ $t('reportAgent.viewChanges') }}
                  </button>
                </div>
              </div>
              <InstructionGlobalCreateComponent
                :key="selectedInstruction?.id || 'new'"
                :instruction="selectedInstruction || undefined"
                :default-status="canCreateInstructions ? 'published' : 'draft'"
                :initial-version-number="initialVersionNumberForInstruction ?? undefined"
                :agent-id="isGlobalSelected ? undefined : (selectedAgentId || undefined)"
                @instruction-saved="onInstructionSaved"
                @cancel="closeInstructionForm"
              />
            </template>
          </div>

          <!-- Instructions list -->
          <template v-else>
            <div v-if="instructionsError" class="text-xs text-gray-500 dark:text-gray-400">{{ instructionsError }}</div>
            <template v-else>
              <!-- Filters -->
              <div class="flex flex-col gap-2 mb-3">
                <div class="relative">
                  <Icon name="heroicons:magnifying-glass" class="absolute start-2 top-1/2 -translate-y-1/2 w-3 h-3 text-gray-400 dark:text-gray-500" />
                  <input
                    v-model="instructionSearch"
                    type="text"
                    :placeholder="$t('reportAgent.searchPlaceholder')"
                    class="w-full ps-7 pe-2 py-1.5 text-[11px] border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-300 focus:border-indigo-300"
                  />
                </div>
                <div class="flex items-center gap-2">
                  <!-- Status multi-select -->
                  <div class="relative" ref="statusDropdownRef">
                    <button
                      @click.stop="statusDropdownOpen = !statusDropdownOpen"
                      class="flex items-center gap-1 text-[11px] border border-gray-200 dark:border-gray-800 rounded-md py-1 px-2 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50 bg-white dark:bg-gray-900"
                    >
                      <span v-if="instructionStatusFilter.length === 0">{{ $t('reportAgent.allStatuses') }}</span>
                      <span v-else class="truncate max-w-[100px]">{{ instructionStatusFilter.map(s => helpers.formatStatus(s)).join(', ') }}</span>
                      <Icon name="heroicons:chevron-down" class="w-3 h-3 text-gray-400 dark:text-gray-500 transition-transform" :class="{ 'rotate-180': statusDropdownOpen }" />
                    </button>
                    <div v-if="statusDropdownOpen" class="absolute z-20 mt-1 start-0 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg shadow-lg overflow-hidden min-w-[120px]">
                      <button
                        v-for="s in instructionStatuses"
                        :key="s"
                        @click.stop="toggleStatusFilter(s)"
                        class="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors text-start"
                      >
                        <span
                          class="w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0"
                          :class="instructionStatusFilter.includes(s) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-300 dark:border-gray-700'"
                        >
                          <Icon v-if="instructionStatusFilter.includes(s)" name="heroicons:check" class="w-2.5 h-2.5 text-white" />
                        </span>
                        <span class="text-gray-700 dark:text-gray-300">{{ helpers.formatStatus(s) }}</span>
                      </button>
                      <button
                        v-if="instructionStatusFilter.length > 0"
                        @click.stop="instructionStatusFilter = []"
                        class="w-full px-2.5 py-1.5 text-[11px] text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 dark:border-gray-800 text-start font-medium"
                      >
                        {{ $t('reportAgent.clearAll') }}
                      </button>
                    </div>
                  </div>
                  <!-- Category multi-select -->
                  <div class="relative" ref="categoryDropdownRef">
                    <button
                      @click.stop="categoryDropdownOpen = !categoryDropdownOpen"
                      class="flex items-center gap-1 text-[11px] border border-gray-200 dark:border-gray-800 rounded-md py-1 px-2 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50 bg-white dark:bg-gray-900"
                    >
                      <span v-if="instructionCategoryFilter.length === 0">{{ $t('reportAgent.allCategories') }}</span>
                      <span v-else class="truncate max-w-[100px]">{{ instructionCategoryFilter.map(c => helpers.formatCategory(c)).join(', ') }}</span>
                      <Icon name="heroicons:chevron-down" class="w-3 h-3 text-gray-400 dark:text-gray-500 transition-transform" :class="{ 'rotate-180': categoryDropdownOpen }" />
                    </button>
                    <div v-if="categoryDropdownOpen" class="absolute z-20 mt-1 start-0 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg shadow-lg overflow-hidden min-w-[140px]">
                      <button
                        v-for="cat in instructionCategories"
                        :key="cat"
                        @click.stop="toggleCategoryFilter(cat)"
                        class="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors text-start"
                      >
                        <span
                          class="w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0"
                          :class="instructionCategoryFilter.includes(cat) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-300 dark:border-gray-700'"
                        >
                          <Icon v-if="instructionCategoryFilter.includes(cat)" name="heroicons:check" class="w-2.5 h-2.5 text-white" />
                        </span>
                        <span class="text-gray-700 dark:text-gray-300">{{ helpers.formatCategory(cat) }}</span>
                      </button>
                      <button
                        v-if="instructionCategoryFilter.length > 0"
                        @click.stop="instructionCategoryFilter = []"
                        class="w-full px-2.5 py-1.5 text-[11px] text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 dark:border-gray-800 text-start font-medium"
                      >
                        {{ $t('reportAgent.clearAll') }}
                      </button>
                    </div>
                  </div>
                  <!-- Create / Suggest button -->
                  <UButton
                    size="xs"
                    color="blue"
                    icon="i-heroicons-plus"
                    class="ms-auto"
                    @click="creatingInstruction = true"
                  >
                    {{ canCreateInstructions ? $t('reportAgent.create') : $t('reportAgent.suggest') }}
                  </UButton>
                </div>
              </div>

              <!-- List -->
              <div v-if="instructions.length === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">{{ $t('reportAgent.noInstructions') }}</div>
              <div v-else-if="filteredInstructions.length === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">{{ $t('reportAgent.noMatching') }}</div>
              <div v-else class="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
                <button
                  v-for="inst in filteredInstructions"
                  :key="inst.id"
                  @click="openInstruction(inst)"
                  class="w-full px-3 py-2.5 text-start text-xs flex items-start gap-2.5 hover:bg-gray-50 dark:hover:bg-gray-800/50 border-b border-gray-100 dark:border-gray-800 last:border-b-0 transition-colors"
                >
                  <div class="flex-1 min-w-0">
                    <!-- Title or text preview -->
                    <div class="flex items-center gap-1.5">
                      <span class="truncate text-gray-800 dark:text-gray-200 font-medium text-xs">{{ inst.title || inst.text?.slice(0, 60) || $t('reportAgent.untitled') }}</span>
                    </div>
                    <!-- Text preview if title exists -->
                    <p v-if="inst.title && inst.text" class="text-[11px] text-gray-500 dark:text-gray-400 truncate mt-0.5 leading-snug">
                      <InstructionText :text="inst.text.slice(0, 80)" :references="inst.references?.map((r: any) => ({ id: r.object_id, type: r.object_type, name: r.display_text || r.object?.name, data_source_type: r.object?.data_source_type || r.object?.connection_type, data_source_icon: r.data_source_icon }))" />
                    </p>
                    <!-- Badges row -->
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                      <!-- Data source indicator -->
                      <template v-if="inst.data_sources?.length">
                        <span v-for="ds in inst.data_sources" :key="ds.id" class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-gray-50 dark:bg-gray-800 text-[9px] text-gray-600 dark:text-gray-400 font-medium border border-gray-100 dark:border-gray-800">
                          <DataSourceIcon :type="getInstructionDsType(ds)" :icon="getInstructionDsIcon(ds)" class="h-2.5 flex-shrink-0" />
                          <span class="truncate max-w-[80px]">{{ ds.name }}</span>
                        </span>
                      </template>
                      <span v-else class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-purple-50 text-[9px] text-purple-600 font-medium border border-purple-100">
                        <Icon name="heroicons:circle-stack" class="w-2.5 h-2.5 text-purple-400 flex-shrink-0" />
                        {{ $t('reportAgent.any') }}
                      </span>
                      <span
                        :class="helpers.getCategoryClass(inst.category)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.formatCategory(inst.category) }}</span>
                      <span
                        :class="helpers.getStatusClass(inst)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.getStatusLabel(inst) }}</span>
                      <span
                        :class="helpers.getLoadModeClass(inst.load_mode)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.getLoadModeLabel(inst.load_mode) }}</span>
                      <!-- User & date -->
                      <span v-if="inst.user" class="inline-flex items-center gap-0.5 text-[9px] text-gray-400 dark:text-gray-500">
                        <Icon name="heroicons:user" class="w-2.5 h-2.5 flex-shrink-0" />
                        <span class="truncate max-w-[60px]">{{ inst.user.name || inst.user.email }}</span>
                        <span>· {{ formatDate(inst.created_at) }}</span>
                      </span>
                    </div>
                    <!-- References -->
                    <div v-if="inst.references?.length" class="flex items-center gap-1 mt-1 flex-wrap">
                      <Icon name="heroicons:link" class="w-2.5 h-2.5 text-gray-300 dark:text-gray-600 flex-shrink-0" />
                      <span
                        v-for="ref in inst.references"
                        :key="ref.id"
                        class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-slate-50 border border-slate-100 text-[9px] text-slate-600 font-medium"
                      >
                        <DataSourceIcon
                          v-if="(ref.object_type === 'datasource_table' || ref.object_type === 'connection_tool') && ref.object?.data_source_type"
                          :type="ref.object?.data_source_type || ref.object?.connection_type"
                          class="h-2.5 flex-shrink-0"
                        />
                        <Icon
                          v-else
                          :name="ref.object_type === 'datasource_table' ? 'heroicons:table-cells' : ref.object_type === 'instruction' ? 'heroicons:document-text' : ref.object_type === 'connection_tool' ? 'heroicons:wrench-screwdriver' : 'heroicons:cube'"
                          class="w-2.5 h-2.5 flex-shrink-0"
                          :class="ref.object_type === 'instruction' ? 'text-indigo-400' : 'text-slate-400'"
                        />
                        <Icon v-if="ref.object_type === 'connection_tool'" name="heroicons:wrench-screwdriver" class="w-2 h-2 flex-shrink-0 text-slate-300" />
                        <span class="truncate max-w-[100px]">{{ ref.display_text || ref.object?.name || ref.object?.title || ref.object_id }}</span>
                      </span>
                    </div>
                  </div>
                </button>
              </div>
            </template>
          </template>
        </template>
      </div>

      <!-- Tables tab — uses TablesSelector component -->
      <div v-else-if="activeTab === 'tables'" class="h-full">
        <TablesSelector
          :key="selectedAgentId"
          :ds-id="selectedAgentId!"
          :schema="canUpdateDataSource ? 'full' : 'user'"
          :can-update="canUpdateDataSource"
          :show-refresh="false"
          :show-save="canUpdateDataSource"
          :save-label="t('reportAgent.save')"
          :show-stats="canUpdateDataSource"
          max-height="calc(100vh - 280px)"
        />
      </div>

      <!-- Queries tab -->
      <div v-else-if="activeTab === 'queries'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
        </div>
        <template v-else>
          <div v-if="queriesError" class="text-xs text-gray-500 dark:text-gray-400">{{ queriesError }}</div>
          <div v-else-if="queries.length === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">{{ $t('reportAgent.noQueries') }}</div>
          <div v-else class="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
            <NuxtLink
              v-for="entity in queries"
              :key="entity.id"
              :to="`/queries/${entity.id}`"
              class="w-full px-3 py-2 text-start text-xs flex items-start gap-2 hover:bg-gray-50 dark:hover:bg-gray-800/50 border-b border-gray-100 dark:border-gray-800 last:border-b-0 block"
            >
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span
                    class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0"
                    :class="entity.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
                  >{{ (entity.type || 'entity').toUpperCase() }}</span>
                  <span class="truncate text-gray-800 dark:text-gray-200 font-medium">{{ entity.title || entity.slug }}</span>
                </div>
                <div v-if="entity.description" class="text-[11px] text-gray-400 dark:text-gray-500 truncate mt-0.5">
                  {{ entity.description }}
                </div>
              </div>
            </NuxtLink>
          </div>
        </template>
      </div>

      <!-- Evals tab -->
      <div v-else-if="activeTab === 'evals'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
        </div>
        <template v-else>
          <div v-if="evalsError" class="text-xs text-gray-500 dark:text-gray-400">{{ evalsError }}</div>
          <div v-else-if="evals.length === 0" class="text-xs text-gray-400 dark:text-gray-500 italic py-6 text-center">{{ $t('reportAgent.noEvals') }}</div>
          <div v-else class="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
            <NuxtLink
              v-for="tc in evals"
              :key="tc.id"
              to="/evals"
              class="w-full px-3 py-2 text-start text-xs flex items-start gap-2 hover:bg-gray-50 dark:hover:bg-gray-800/50 border-b border-gray-100 dark:border-gray-800 last:border-b-0 block"
            >
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0 text-amber-700 border-amber-200 bg-amber-50">
                    {{ tc.suite_name || $t('reportAgent.evalBadge') }}
                  </span>
                  <span class="truncate text-gray-800 dark:text-gray-200 font-medium">{{ tc.name || promptPreview(tc.prompt_json) }}</span>
                </div>
                <div v-if="tc.expectations_json?.length" class="text-[11px] text-gray-400 dark:text-gray-500 truncate mt-0.5">
                  {{ tc.expectations_json.length === 1 ? $t('reportAgent.expectationOne', { n: 1 }) : $t('reportAgent.expectationMany', { n: tc.expectations_json.length }) }}
                </div>
              </div>
            </NuxtLink>
          </div>
        </template>
      </div>
    </div>

    <!-- Edit Conversation Starters Modal -->
    <UModal v-model="showEditStarters" :ui="{ width: 'sm:max-w-2xl' }">
      <div class="p-5">
        <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('dataSource.editStartersTitle') }}</div>
        <div class="text-xs text-gray-600 dark:text-gray-400 mt-1">{{ $t('dataSource.editStartersHint') }}</div>
        <div class="mt-4 space-y-2 max-h-[60vh] overflow-auto pe-1">
          <div v-for="(item, idx) in editStartersForm" :key="idx" class="rounded-md border border-gray-100 dark:border-gray-800 p-2">
            <div class="flex items-center justify-between mb-1">
              <span class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{{ $t('dataSource.starterN', { n: idx + 1 }) }}</span>
              <button @click="removeStarter(idx)" class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-red-600">{{ $t('dataSource.remove') }}</button>
            </div>
            <div class="space-y-1">
              <div>
                <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-0.5">{{ $t('dataSource.starterTitle') }}</label>
                <input v-model="item.title" type="text" class="w-full h-8 text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 focus:outline-none focus:ring-2 focus:ring-blue-200" :placeholder="$t('dataSource.starterTitlePlaceholder')" />
              </div>
              <div>
                <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-0.5">{{ $t('dataSource.starterPrompt') }}</label>
                <textarea v-model="item.prompt" rows="2" class="w-full text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200" :placeholder="$t('dataSource.starterPromptPlaceholder')"></textarea>
              </div>
            </div>
          </div>
          <button @click="addStarter" class="text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg px-2 py-1 hover:bg-gray-50 dark:hover:bg-gray-800/50">{{ $t('dataSource.addStarter') }}</button>
        </div>
        <div class="flex justify-end gap-2 mt-4">
          <button @click="showEditStarters = false" class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg">{{ $t('dataSource.cancel') }}</button>
          <button @click="saveStarters" :disabled="savingStarters" class="px-3 py-1.5 text-xs border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50">{{ savingStarters ? $t('dataSource.saving') : $t('dataSource.save') }}</button>
        </div>
      </div>
    </UModal>

    <BuildExplorerModal
      v-if="canViewBuilds"
      v-model="showBuildExplorer"
      :build-id="buildExplorerBuildId"
    />

    <!-- User credentials / OAuth modal for connecting user_required agents -->
    <UserDataSourceCredentialsModal
      v-model="showCredsModal"
      :data-source="selectedConnectDs"
      @saved="onCredentialsSaved"
    />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import TablesSelector from '~/components/datasources/TablesSelector.vue'
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import BuildExplorerModal from '~/components/instructions/BuildExplorerModal.vue'
import InstructionTrackedChanges from '~/components/instructions/InstructionTrackedChanges.vue'
import InstructionText from '~/components/instructions/InstructionText.vue'
import { useInstructionHelpers } from '~/composables/useInstructionHelpers'
import { deriveStage, stageMeta } from '~/composables/useDataSourcePublishStatus'

const { t } = useI18n()

const props = defineProps<{
  agents: Array<{ id: string; name: string; type?: string; connections?: any[]; publish_status?: string; reliability_status?: string }>
  showClose?: boolean
}>()

// Lifecycle filter + badge, mirroring DataSourceSelector: disabled agents are
// hidden (the home instructions modal feeds this panel from /data_sources,
// which returns managers their disabled agents too), and non-production stages
// (Development / Training) are flagged. The synthetic Global entry is exempt.
const visibleAgents = computed(() =>
  props.agents.filter((a: any) => a.isGlobal || deriveStage(a.publish_status, a.reliability_status) !== 'disabled')
)

function stageBadge(agent: any) {
  if (agent?.isGlobal) return null
  const stage = deriveStage(agent?.publish_status, agent?.reliability_status)
  return stage === 'production' ? null : stageMeta(stage)
}

const emit = defineEmits(['close', 'starter-click', 'connected'])

// Connect (user credentials / OAuth) affordance for user_required agents.
const { connectingId, needsUserConnection, startConnect, asCredentialsModalSource } = useDataSourceConnect()
const showCredsModal = ref(false)
const selectedConnectDs = ref<any>(null)

async function onConnect(agent: any) {
  const openModal = await startConnect(agent)
  if (!openModal) return // redirecting to the provider; the page will reload on return
  selectedConnectDs.value = asCredentialsModalSource(agent)
  showCredsModal.value = true
}

function onCredentialsSaved() {
  showCredsModal.value = false
  // The agent's user_status is owned by the parent (the report); ask it to
  // refresh so the Connect prompt clears. The OAuth redirect path reloads the
  // page on return and refreshes naturally.
  emit('connected')
}

// Permissions
const canViewBuilds = computed(() => useCan('view_builds'))
const canManageTests = computed(() => useCan('manage_tests'))

// Build explorer modal (for "View changes" affordance on unpublished-build banner)
const showBuildExplorer = ref(false)
const buildExplorerBuildId = ref<string>('')
const openBuildExplorer = (bid: string) => {
  buildExplorerBuildId.value = bid
  showBuildExplorer.value = true
}

// Dropdown state
const dropdownOpen = ref(false)
const dropdownRef = ref<HTMLElement | null>(null)
const selectedAgentId = ref<string | null>(null)
// Editing the selected agent (tables, starters, …) requires `manage` on that
// data source (full_admin bypasses; otherwise a per-resource `manage` grant).
const canUpdateDataSource = computed(() =>
  selectedAgentId.value
    ? useCan('manage', { type: 'data_source', id: selectedAgentId.value })
    : false
)
// Synthetic "Global" entry (instructions attached to no agent). Callers that
// want it — e.g. the home Instructions modal — append { id: GLOBAL_AGENT_ID,
// name: 'Global', isGlobal: true } to `agents`. The report never does, so its
// behaviour is unchanged.
const GLOBAL_AGENT_ID = '__global__'
const isGlobalSelected = computed(() => selectedAgentId.value === GLOBAL_AGENT_ID)

// Tab state
const activeTab = ref<'overview' | 'instructions' | 'tables' | 'queries' | 'evals'>('overview')

// Data caches (keyed by agent id)
const agentDetailsCache = ref<Record<string, any>>({})
const instructionsCache = ref<Record<string, any[]>>({})
const queriesCache = ref<Record<string, any[]>>({})
const evalsCache = ref<Record<string, any[]>>({})

// Instruction detail state
const selectedInstruction = ref<any | null>(null)
// Per-hunk review (tracked changes) state for the instruction view.
const instructionReviewEmpty = ref(false)
const forceInstructionEdit = ref(false)
// Show the per-hunk review for any existing instruction — read-only when the
// user can't approve (`can-approve` gates the buttons), matching the Knowledge
// Explorer. The component reports `empty` (no live hunks) and we fall to the
// editor. Robust to current_build_status not being populated.
const showInstructionReview = computed(() =>
  !!selectedInstruction.value && !!selectedInstruction.value.id && !creatingInstruction.value
  && !instructionReviewEmpty.value && !forceInstructionEdit.value
)
const creatingInstruction = ref(false)
const creatingPrimaryInstruction = ref(false)
const instructionLoading = ref(false)
// Optional preselected version for the form (used by EditInstructionTool to
// open the pane already showing a diff against the current version).
const initialVersionNumberForInstruction = ref<number | null>(null)

// Conversation starters are sourced from agent-scoped starter Prompts
// (not the legacy data_source.conversation_starters JSON). Each prompt's
// `text` is the "title\nprompt" string.
const showEditStarters = ref(false)
const editStartersForm = ref<{ title: string; prompt: string }[]>([])
const savingStarters = ref(false)
const starterPrompts = ref<any[]>([])

async function loadStarterPrompts(agentId: string | null) {
  if (!agentId || agentId === GLOBAL_AGENT_ID) { starterPrompts.value = []; return }
  try {
    const { data } = await useMyFetch(`/prompts?data_source_id=${agentId}`)
    starterPrompts.value = (data.value as any)?.prompts || []
  } catch { starterPrompts.value = [] }
}

watch(selectedAgentId, (id) => loadStarterPrompts(id), { immediate: true })

function openEditStarters() {
  editStartersForm.value = (starterPrompts.value || []).map((p: any) => {
    const parts = String(p.text || '').split('\n')
    return { title: (parts[0] || '').trim(), prompt: parts.slice(1).join('\n').trim() }
  })
  if (editStartersForm.value.length === 0) editStartersForm.value = [{ title: '', prompt: '' }]
  showEditStarters.value = true
}

function addStarter() {
  editStartersForm.value.push({ title: '', prompt: '' })
}

function removeStarter(index: number) {
  editStartersForm.value.splice(index, 1)
}

async function saveStarters() {
  if (savingStarters.value || !selectedAgentId.value) return
  savingStarters.value = true
  const id = selectedAgentId.value
  const starters = editStartersForm.value
    .map(s => `${(s.title || '').trim()}${s.prompt?.trim() ? `\n${s.prompt.trim()}` : ''}`)
    .filter(s => s.trim().length > 0)
  try {
    // Replace-all of this agent's starter Prompts (no legacy JSON write).
    const { data: existing } = await useMyFetch(`/prompts?data_source_id=${id}`)
    for (const p of ((existing.value as any)?.prompts || [])) {
      await useMyFetch(`/prompts/${p.id}`, { method: 'DELETE' })
    }
    for (const text of starters) {
      await useMyFetch(`/prompts`, { method: 'POST', body: {
        text, title: (text.split('\n')[0] || '').slice(0, 60),
        scope: 'agent', is_starter: true, data_source_ids: [id],
      } })
    }
    await loadStarterPrompts(id)
    showEditStarters.value = false
  } finally {
    savingStarters.value = false
  }
}

const canCreateInstructions = computed(() => {
  if (useCan('manage_instructions')) return true
  // Global instructions: creation requires org-level manage_instructions.
  if (isGlobalSelected.value) return false
  if (!selectedAgentId.value) return false
  return useCan('manage_instructions', { type: 'data_source', id: selectedAgentId.value })
})

function closeInstructionForm() {
  selectedInstruction.value = null
  creatingInstruction.value = false
  creatingPrimaryInstruction.value = false
}

// Instruction helpers & filters
const helpers = useInstructionHelpers()
const instructionSearch = ref('')
const instructionCategoryFilter = ref<string[]>([])
const instructionStatusFilter = ref<string[]>(['published'])
const statusDropdownOpen = ref(false)
const statusDropdownRef = ref<HTMLElement | null>(null)
const categoryDropdownOpen = ref(false)
const categoryDropdownRef = ref<HTMLElement | null>(null)

const instructionCategories = computed(() => {
  const cats = new Set(instructions.value.map((i: any) => i.category).filter(Boolean))
  return Array.from(cats).sort()
})

const instructionStatuses = computed(() => {
  const statuses = new Set(instructions.value.map((i: any) => i.status).filter(Boolean))
  return Array.from(statuses).sort()
})

function toggleCategoryFilter(cat: string) {
  const idx = instructionCategoryFilter.value.indexOf(cat)
  if (idx >= 0) {
    instructionCategoryFilter.value = instructionCategoryFilter.value.filter(c => c !== cat)
  } else {
    instructionCategoryFilter.value = [...instructionCategoryFilter.value, cat]
  }
}

function toggleStatusFilter(status: string) {
  const idx = instructionStatusFilter.value.indexOf(status)
  if (idx >= 0) {
    instructionStatusFilter.value = instructionStatusFilter.value.filter(s => s !== status)
  } else {
    instructionStatusFilter.value = [...instructionStatusFilter.value, status]
  }
}

function onCategoryDropdownOutsideClick(e: MouseEvent) {
  if (categoryDropdownRef.value && !categoryDropdownRef.value.contains(e.target as Node)) {
    categoryDropdownOpen.value = false
  }
  if (statusDropdownRef.value && !statusDropdownRef.value.contains(e.target as Node)) {
    statusDropdownOpen.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onCategoryDropdownOutsideClick)
})
onUnmounted(() => {
  document.removeEventListener('click', onCategoryDropdownOutsideClick)
})

const filteredInstructions = computed(() => {
  let list = instructions.value
  const q = instructionSearch.value.toLowerCase().trim()
  if (q) {
    list = list.filter((i: any) =>
      (i.title || '').toLowerCase().includes(q) ||
      (i.text || '').toLowerCase().includes(q)
    )
  }
  if (instructionCategoryFilter.value.length > 0) {
    list = list.filter((i: any) => instructionCategoryFilter.value.includes(i.category))
  }
  if (instructionStatusFilter.value.length > 0) {
    list = list.filter((i: any) => instructionStatusFilter.value.includes(i.status))
  }
  return list
})

// Loading & error state
const loading = ref(false)
const instructionsError = ref<string | null>(null)
const queriesError = ref<string | null>(null)
const evalsError = ref<string | null>(null)

// Auto-select first agent; also reselect when the current one drops out of
// the visible list (e.g. it was disabled while the panel was open).
watch(visibleAgents, (agents) => {
  if (agents.length === 0) return
  if (!selectedAgentId.value || !agents.some(a => a.id === selectedAgentId.value)) {
    selectedAgentId.value = agents[0].id
    if (agents[0].id === GLOBAL_AGENT_ID) activeTab.value = 'instructions'
  }
}, { immediate: true })

const selectedAgent = computed(() => {
  return visibleAgents.value.find(a => a.id === selectedAgentId.value) || null
})

// Tab definitions with counts (tables count managed by TablesSelector internally)
const tabs = computed(() => {
  // The Global entry only carries instructions (no overview/tables/queries/evals).
  if (isGlobalSelected.value) {
    return [{ key: 'instructions' as const, label: t('reportAgent.tabInstructions'), count: instructions.value.length }]
  }
  const out: Array<{ key: 'overview' | 'instructions' | 'tables' | 'queries' | 'evals'; label: string; count: number }> = [
    { key: 'overview', label: 'Overview', count: 0 },
    { key: 'instructions', label: t('reportAgent.tabInstructions'), count: instructions.value.length },
    { key: 'tables', label: t('reportAgent.tabTables'), count: 0 },
    { key: 'queries', label: t('reportAgent.tabQueries'), count: queries.value.length },
  ]
  if (canManageTests.value) {
    out.push({ key: 'evals', label: t('reportAgent.tabEvals'), count: evals.value.length })
  }
  return out
})

const selectedAgentDetails = computed(() => selectedAgentId.value ? (agentDetailsCache.value[selectedAgentId.value] || null) : null)
const instructions = computed(() => selectedAgentId.value ? (instructionsCache.value[selectedAgentId.value] || []) : [])
const queries = computed(() => selectedAgentId.value ? (queriesCache.value[selectedAgentId.value] || []) : [])
const evals = computed(() => selectedAgentId.value ? (evalsCache.value[selectedAgentId.value] || []) : [])

// Resolve DS type for instruction's embedded data_source (may lack connections)
function getInstructionDsType(ds: any): string | undefined {
  // Try the DS object itself
  if (ds.type) return ds.type
  if (ds.connections?.[0]?.type) return ds.connections[0].type
  // Look up from agents prop (which has full connection info)
  const match = props.agents.find(a => a.id === ds.id)
  return match?.type || match?.connections?.[0]?.type || undefined
}

function getInstructionDsIcon(ds: any): string | null | undefined {
  if (ds.icon) return ds.icon
  const match = props.agents.find(a => a.id === ds.id)
  return match?.icon
}

function selectAgent(agentId: string) {
  selectedAgentId.value = agentId
  dropdownOpen.value = false
  // Global has only the instructions tab; never leave activeTab on overview/etc.
  if (agentId === GLOBAL_AGENT_ID) activeTab.value = 'instructions'
}

async function onInstructionSaved(savedInstruction?: any) {
  const wasPrimary = creatingPrimaryInstruction.value
  selectedInstruction.value = null
  creatingInstruction.value = false
  creatingPrimaryInstruction.value = false

  if (selectedAgentId.value) {
    // If created from the overview "Create" link, pin it as primary instruction
    if (wasPrimary && savedInstruction?.id && !savedInstruction?.deleted) {
      await useMyFetch(`/data_sources/${selectedAgentId.value}`, {
        method: 'PUT',
        body: { primary_instruction_id: savedInstruction.id },
      })
      delete agentDetailsCache.value[selectedAgentId.value]
      fetchTabData(selectedAgentId.value, 'overview')
    }
    delete instructionsCache.value[selectedAgentId.value]
    fetchTabData(selectedAgentId.value, 'instructions')
  }
}

const _df = useFormatDate()
function formatDate(dateStr: string): string {
  if (!dateStr) return ''
  return _df.format(dateStr, { month: 'short', day: 'numeric', year: 'numeric' })
}

function promptPreview(promptJson: any): string {
  if (!promptJson) return t('reportAgent.untitled')
  if (typeof promptJson === 'string') return promptJson.slice(0, 60)
  if (Array.isArray(promptJson) && promptJson.length > 0) {
    const first = promptJson[0]
    const content = first?.content || first?.text || ''
    return typeof content === 'string' ? content.slice(0, 60) : t('reportAgent.untitled')
  }
  return t('reportAgent.untitled')
}

// Close dropdown on outside click
function onClickOutside(e: MouseEvent) {
  if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) {
    dropdownOpen.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onClickOutside)
})

onUnmounted(() => {
  document.removeEventListener('click', onClickOutside)
})

// Fetch data for active tab when agent or tab changes
async function fetchTabData(agentId: string, tab: string) {
  // Global entry: only the instructions tab, listing instructions attached to no
  // agent. No /data_sources/{id} fetch (there is no data source).
  if (agentId === GLOBAL_AGENT_ID) {
    if (tab === 'instructions' && !instructionsCache.value[agentId]) {
      loading.value = true
      instructionsError.value = null
      try {
        const { data, error } = await useMyFetch('/api/instructions', {
          method: 'GET',
          query: { include_own: true, include_drafts: true, limit: 200 }
        })
        if (error?.value) { instructionsError.value = t('reportAgent.loadFailInstructions'); return }
        const payload: any = (data as any)?.value
        const all = payload?.items || payload || []
        instructionsCache.value[agentId] = all.filter((i: any) => !(i.data_sources?.length))
      } catch { instructionsError.value = t('reportAgent.loadFailInstructions') }
      finally { loading.value = false }
    }
    return
  }

  // Tables tab is handled by TablesSelector component — no manual fetch needed

  if (tab === 'overview' && !agentDetailsCache.value[agentId]) {
    try {
      const { data, error } = await useMyFetch(`/data_sources/${agentId}`, { method: 'GET' })
      if (!error?.value) {
        agentDetailsCache.value[agentId] = (data as any)?.value || null
      }
    } catch {}
  }

  if (tab === 'instructions' && !instructionsCache.value[agentId]) {
    loading.value = true
    instructionsError.value = null
    try {
      // Fetch instructions scoped to the selected agent AND global (any data source) instructions
      const { data, error } = await useMyFetch('/api/instructions', {
        method: 'GET',
        query: { data_source_ids: agentId, include_global: true, limit: 200, include_own: true, include_drafts: true }
      })
      if (error?.value) { instructionsError.value = t('reportAgent.loadFailInstructions'); return }
      const payload: any = (data as any)?.value
      const agentInstructions = payload?.items || payload || []
      instructionsCache.value[agentId] = agentInstructions
    } catch { instructionsError.value = t('reportAgent.loadFailInstructions') }
    finally { loading.value = false }
  }

  if (tab === 'queries' && !queriesCache.value[agentId]) {
    loading.value = true
    queriesError.value = null
    try {
      const { data, error } = await useMyFetch('/api/entities', {
        method: 'GET',
        query: { data_source_ids: agentId }
      })
      if (error?.value) { queriesError.value = t('reportAgent.loadFailQueries'); return }
      const payload: any = (data as any)?.value
      const entities = Array.isArray(payload) ? payload : []
      queriesCache.value[agentId] = entities.filter((e: any) => e.status === 'published' && e.global_status === 'approved')
    } catch { queriesError.value = t('reportAgent.loadFailQueries') }
    finally { loading.value = false }
  }

  if (tab === 'evals' && !evalsCache.value[agentId]) {
    loading.value = true
    evalsError.value = null
    try {
      const { data, error } = await useMyFetch('/api/tests/cases', {
        method: 'GET',
        query: { data_source_id: agentId, limit: 50 }
      })
      if (error?.value) { evalsError.value = t('reportAgent.loadFailEvals'); return }
      const payload: any = (data as any)?.value
      const cases = Array.isArray(payload) ? payload : []
      evalsCache.value[agentId] = cases.filter((c: any) => {
        const dsIds = c.data_source_ids_json || []
        return dsIds.includes(agentId)
      })
    } catch { evalsError.value = t('reportAgent.loadFailEvals') }
    finally { loading.value = false }
  }
}

watch([selectedAgentId, activeTab], ([agentId, tab]) => {
  if (agentId && tab) {
    fetchTabData(agentId, tab)
  }
}, { immediate: true })

// Reset when agent changes
watch(selectedAgentId, () => {
  activeTab.value = 'overview'
  selectedInstruction.value = null
  creatingInstruction.value = false
  creatingPrimaryInstruction.value = false
  initialVersionNumberForInstruction.value = null
  instructionsError.value = null
  queriesError.value = null
  evalsError.value = null
  instructionSearch.value = ''
  instructionCategoryFilter.value = []
  instructionStatusFilter.value = ['published']
})

// Any path that swaps the open instruction must restart the per-hunk review
// from scratch — otherwise a prior instruction's `empty` (or an Edit-instead
// toggle) leaks across and forces the legacy editor for the next one.
watch(selectedInstruction, (next, prev) => {
  if (next?.id === prev?.id) return
  instructionReviewEmpty.value = false
  forceInstructionEdit.value = false
})

// Expose methods for external callers
function openInstruction(instruction: any, opts?: { initialVersionNumber?: number | null }) {
  activeTab.value = 'instructions'
  instructionLoading.value = false
  creatingInstruction.value = false
  initialVersionNumberForInstruction.value = opts?.initialVersionNumber ?? null
  instructionReviewEmpty.value = false
  forceInstructionEdit.value = false
  selectedInstruction.value = instruction
}

function setInstructionLoading(value: boolean) {
  activeTab.value = 'instructions'
  if (value) {
    selectedInstruction.value = null
    creatingInstruction.value = false
    initialVersionNumberForInstruction.value = null
  }
  instructionLoading.value = value
}

function refreshInstructions() {
  if (!selectedAgentId.value) return
  delete instructionsCache.value[selectedAgentId.value]
  fetchTabData(selectedAgentId.value, 'instructions')
}

defineExpose({ openInstruction, setInstructionLoading, refreshInstructions })
</script>
