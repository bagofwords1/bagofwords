<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-7xl'}">
        <UCard>
            <!-- Header: conversation identity + roll-up -->
            <template #header>
                <div class="flex items-start justify-between gap-4">
                    <div class="min-w-0">
                        <h3 class="text-base font-semibold text-gray-900 truncate">
                            {{ conversation?.report_title || $t('traceModal.title') }}
                        </h3>
                        <div class="flex items-center gap-2 mt-1 text-xs text-gray-500">
                            <span v-if="conversation?.user_name" class="inline-flex items-center gap-1">
                                <UIcon name="i-heroicons-user-circle" class="w-3.5 h-3.5" />
                                {{ conversation.user_name }}
                            </span>
                            <span v-if="conversation?.user_email" class="text-gray-400">{{ conversation.user_email }}</span>
                        </div>
                    </div>
                    <div class="flex items-center gap-3 flex-shrink-0">
                        <!-- Conversation roll-up: plain text -->
                        <div v-if="conversation" class="flex items-center gap-2 text-xs text-gray-500">
                            <span>{{ conversation.total_turns }} {{ conversation.total_turns === 1 ? 'turn' : 'turns' }}</span>
                            <span v-if="conversation.failed_turns" class="text-red-500">{{ conversation.failed_turns }} failed</span>
                            <span v-if="conversation.negative_feedback_turns" class="text-amber-600">{{ conversation.negative_feedback_turns }} negative</span>
                        </div>
                        <UButton
                            color="gray"
                            variant="ghost"
                            icon="i-heroicons-x-mark-20-solid"
                            @click="closeModal"
                        />
                    </div>
                </div>
            </template>

            <!-- Content: conversation rail + per-turn detail -->
            <div class="h-[620px] flex -mx-4 -mb-2">
                <!-- Pane A: whole conversation, rendered like the chat -->
                <div class="w-[40%] flex-shrink-0 border-e border-gray-200 flex flex-col min-h-0">
                    <div class="px-4 py-2.5 border-b border-gray-200 text-[11px] uppercase tracking-wide text-gray-500 flex items-center justify-between">
                        <span>Conversation</span>
                        <span v-if="conversation" class="text-gray-400 normal-case tracking-normal">{{ conversation.total_turns }}</span>
                    </div>
                    <div v-if="isConvLoading" class="flex-1 flex items-center justify-center">
                        <Spinner class="w-5 h-5 text-gray-400" />
                    </div>
                    <div v-else class="flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-5">
                        <div v-for="(turn, i) in turns" :key="turn.completion_id || i"
                             :class="['rounded-lg px-1.5 py-1.5 transition-colors', turn.completion_id === selectedCompletionId ? 'bg-blue-50/40 ring-1 ring-blue-100' : '']">
                            <!-- User bubble -->
                            <div class="flex justify-end mb-2">
                                <div class="max-w-[88%] rounded-xl px-3 py-2 bg-gray-100 text-[13px] text-gray-900 whitespace-pre-line break-words" dir="auto">{{ turn.user_prompt || '—' }}</div>
                            </div>
                            <!-- Assistant blocks -->
                            <div class="space-y-2">
                                <div v-for="block in chatBlocks(turn)" :key="block.id"
                                     @click="onChatBlockClick(turn, block)"
                                     :class="[
                                        'rounded-lg border px-3 py-2 cursor-pointer transition-colors',
                                        selectedItem?.id === block.id && turn.completion_id === selectedCompletionId
                                            ? 'border-blue-300 bg-blue-50/60'
                                            : 'border-gray-200 hover:border-gray-300'
                                     ]">
                                    <div class="flex items-center gap-1.5">
                                        <UIcon :name="getStatusIcon(block.status)" :class="getStatusIconClass(block.status)" />
                                        <span class="text-xs font-medium text-gray-800 truncate">{{ chatBlockTitle(block) }}</span>
                                        <span v-if="block.duration_ms != null" class="ms-auto text-[10px] text-gray-400 font-mono flex-shrink-0">{{ formatDuration(block.duration_ms) }}</span>
                                    </div>
                                    <div v-if="block.reasoning" class="text-[11px] text-gray-400 mt-1 line-clamp-3 leading-snug whitespace-pre-line">{{ block.reasoning }}</div>
                                    <div v-if="block.content" class="mt-1.5 text-xs text-gray-700 markdown-wrapper" dir="auto">
                                        <MarkdownRender :content="block.content" :final="true" :typewriter="false" :render-code-blocks-as-pre="true" class="markdown-content" />
                                    </div>
                                    <div v-if="block.tool_execution" class="mt-2" @click.stop="onChatBlockClick(turn, block)">
                                        <component
                                            v-if="shouldUseToolComponent(block.tool_execution)"
                                            :is="getToolComponent(block.tool_execution.tool_name)"
                                            :tool-execution="block.tool_execution"
                                        />
                                        <GenericTool v-else :tool-execution="block.tool_execution" />
                                    </div>
                                </div>
                                <!-- Turn meta -->
                                <div class="flex items-center gap-1.5 ps-1 text-[10px] text-gray-400">
                                    <span :class="statusTextClass(turn.status)">{{ statusLabel(turn.status) }}</span>
                                    <template v-if="turn.total_duration_ms != null"><span>·</span><span>{{ formatDuration(turn.total_duration_ms) }}</span></template>
                                    <template v-if="turn.feedback_status !== 'none'"><span>·</span><span :class="turn.feedback_status === 'positive' ? 'text-green-600' : 'text-red-500'">{{ turn.feedback_status }}</span></template>
                                </div>
                            </div>
                        </div>
                        <div v-if="!turns.length" class="text-xs text-gray-400 text-center py-8">No turns yet</div>
                    </div>
                </div>

                <!-- Pane B: timeline (focused turn) -->
                <div class="w-[260px] flex-shrink-0 border-e border-gray-200 flex flex-col min-h-0">
                    <div class="px-4 py-2.5 border-b border-gray-200 text-[11px] uppercase tracking-wide text-gray-500">Timeline</div>
                    <div v-if="isLoading" class="flex-1 flex items-center justify-center"><Spinner class="w-5 h-5 text-gray-400" /></div>
                    <div v-else-if="!visibleLeftItems.length" class="flex-1 flex items-center justify-center text-xs text-gray-400 px-4 text-center">Select a turn</div>
                    <div v-else class="flex-1 min-h-0 overflow-y-auto p-3 space-y-1">
                        <template v-for="item in visibleLeftItems" :key="item.id">
                            <div v-if="item.kind === 'section'"
                                 class="px-1 py-1 flex items-center gap-1 cursor-pointer text-[10px] text-gray-500 hover:text-gray-700 select-none"
                                 @click="toggleHarnessCollapsed()">
                                <UIcon :name="harnessCollapsed ? 'i-heroicons-chevron-right-20-solid' : 'i-heroicons-chevron-down-20-solid'" class="w-3 h-3 rtl-flip" />
                                <span>{{ item.title }}</span><span class="text-gray-400">· {{ harnessCount }}</span>
                            </div>
                            <button v-else type="button" @click="selectLeftItem(item)"
                                :class="[
                                    'w-full text-start rounded-md px-2 py-1.5 border',
                                    selectedItem?.id === item.id ? 'border-blue-400 bg-blue-50' : 'border-transparent hover:bg-gray-50',
                                    item.phase === 'knowledge_harness' ? 'ms-3' : ''
                                ]">
                                <div class="flex items-center gap-1.5">
                                    <UIcon :name="getLeftItemIcon(item)" :class="getLeftItemIconClass(item)" />
                                    <span class="text-[11px] text-gray-700 truncate flex-1">{{ item.title }}</span>
                                    <span v-if="getItemDurationMs(item) !== null" class="text-[10px] text-gray-400 font-mono flex-shrink-0">{{ formatDuration(getItemDurationMs(item) || 0) }}</span>
                                </div>
                                <div v-if="getItemDurationMs(item) !== null" class="mt-1 h-1.5 rounded bg-gray-100 overflow-hidden flex">
                                    <div class="h-full bg-purple-400" :style="{ width: barPct(itemLlmMs(item)) + '%' }"></div>
                                    <div class="h-full bg-amber-400" :style="{ width: barPct(itemExecMs(item)) + '%' }"></div>
                                </div>
                            </button>
                        </template>
                    </div>
                </div>

                <!-- Pane C: expanded block -->
                <div class="flex-1 flex flex-col min-h-0">
                    <!-- Per-turn summary strip -->
                    <div v-if="selectedTurn" class="px-5 py-2.5 border-b border-gray-200 flex items-center gap-2 flex-wrap">
                        <span :class="['inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium', statusChipClass(selectedTurn.status)]">
                            <UIcon :name="getStatusIcon(selectedTurn.status)" class="w-3.5 h-3.5" />
                            {{ selectedTurn.status }}
                        </span>
                        <span v-if="selectedTurn.total_tools" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-100 text-xs text-gray-600">
                            <UIcon name="i-heroicons-wrench" class="w-3.5 h-3.5" />
                            {{ selectedTurn.total_successful_tools }}/{{ selectedTurn.total_tools }} tools
                        </span>
                        <span v-if="traceData?.timing_breakdown?.total_duration_ms != null" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-100 text-xs text-gray-600">
                            <UIcon name="i-heroicons-clock" class="w-3.5 h-3.5" />
                            {{ formatDuration(traceData.timing_breakdown.total_duration_ms) }}
                        </span>
                        <span v-if="selectedTurn.feedback_status !== 'none'"
                              :class="['inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs', selectedTurn.feedback_status === 'positive' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']">
                            <UIcon :name="selectedTurn.feedback_status === 'positive' ? 'i-heroicons-hand-thumb-up' : 'i-heroicons-hand-thumb-down'" class="w-3.5 h-3.5" />
                            {{ selectedTurn.feedback_status }}
                        </span>
                        <!-- AI scoring -->
                        <div v-if="isJudgeEnabled && hasTurnScores(selectedTurn)" class="flex items-center gap-1.5 ms-auto">
                            <span v-if="selectedTurn.instructions_effectiveness != null" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs bg-blue-50 text-blue-700 border-blue-200">
                                <span>Inst</span><span class="font-semibold">{{ selectedTurn.instructions_effectiveness }}/5</span>
                            </span>
                            <span v-if="selectedTurn.context_effectiveness != null" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs bg-purple-50 text-purple-700 border-purple-200">
                                <span>Ctx</span><span class="font-semibold">{{ selectedTurn.context_effectiveness }}/5</span>
                            </span>
                            <span v-if="selectedTurn.response_score != null" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs bg-green-50 text-green-700 border-green-200">
                                <span>Resp</span><span class="font-semibold">{{ selectedTurn.response_score }}/5</span>
                            </span>
                        </div>
                    </div>

                    <div class="flex-1 min-h-0 overflow-y-auto p-5">
                        <!-- Loading -->
                        <div v-if="isLoading" class="h-full flex items-center justify-center">
                            <div class="text-center">
                                <Spinner class="w-8 h-8 mx-auto mb-4 text-gray-400" />
                                <p class="text-sm text-gray-500">{{ $t('traceModal.loading') }}</p>
                            </div>
                        </div>
                        <!-- Empty -->
                        <div v-else-if="!selectedItem" class="flex items-center justify-center h-full text-gray-500">
                            <div class="text-center">
                                <UIcon name="i-heroicons-cursor-arrow-rays" class="w-12 h-12 mx-auto mb-4 text-gray-400" />
                                <p class="text-xs">{{ $t('traceModal.selectItem') }}</p>
                            </div>
                        </div>

                        <div v-else>
                            <!-- Item Header -->
                            <div class="mb-4 flex-shrink-0">
                                <div class="flex items-center mb-2">
                                    <UIcon :name="getSelectedItemIcon()" class="w-4 h-4 me-2 text-gray-600" />
                                    <h4 class="text-sm font-medium text-gray-900">{{ getSelectedItemTitle() }}</h4>
                                    <span v-if="selectedItemDataSources.length" class="flex items-center gap-1.5 ms-2">
                                        <span v-for="ds in selectedItemDataSources" :key="ds.id" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-gray-100 text-[11px] text-gray-600">
                                            <DataSourceIcon :type="ds.type" class="w-3.5 h-3.5" />
                                            <span>{{ ds.name || ds.type }}</span>
                                        </span>
                                    </span>
                                </div>
                                <div class="text-xs text-gray-500">
                                    {{ formatDate(selectedItem.created_at) }}
                                </div>
                            </div>
                            <!-- Block Details (minimal) -->
                            <div class="space-y-4">

                                <!-- Overview: prompt + assessment (judge) + context -->
                                <template v-if="selectedItem.id === 'overview'">
                                    <!-- User prompt -->
                                    <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">{{ $t('traceModal.userPrompt') }}</div>
                                    <pre class="text-xs text-gray-900 font-sans whitespace-pre-wrap break-words">{{ traceData?.head_prompt_snippet || '—' }}</pre>

                                    <!-- Assessment (judge) -->
                                    <div v-if="isJudgeEnabled && assessmentRows.length" class="mt-4">
                                        <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">LLM Judge Assessment</div>
                                        <div class="space-y-2.5">
                                            <div v-for="row in assessmentRows" :key="row.key">
                                                <div class="flex items-center gap-2 text-xs">
                                                    <span class="w-28 text-gray-500 flex-shrink-0">{{ row.label }}</span>
                                                    <div class="flex-1 h-1.5 rounded bg-gray-100 overflow-hidden">
                                                        <div class="h-full" :class="row.bar" :style="{ width: (row.score / 5 * 100) + '%' }"></div>
                                                    </div>
                                                    <span class="font-semibold w-7 text-end" :class="row.text">{{ row.score }}/5</span>
                                                </div>
                                                <div v-if="row.reasoning" class="text-[11px] text-gray-500 mt-1 ps-[7.5rem] leading-snug">{{ row.reasoning }}</div>
                                            </div>
                                        </div>
                                    </div>

                                    <!-- Context (schemas / instructions / observations) -->
                                    <div v-if="traceData?.head_context_snapshot" class="mt-4">
                                        <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">{{ $t('traceModal.context') }}</div>
                                        <ContextBrowser
                                            :context-data="traceData.head_context_snapshot.context_view_json || {}"
                                            :build="traceData?.build"
                                        />
                                    </div>
                                </template>

                                <!-- Instructions summary detail -->
                                <template v-else-if="selectedItem.kind === 'instructions'">
                                    <div v-if="instructionsSummaryItems.length">
                                        <!-- Summary counts -->
                                        <div class="flex items-center gap-3 mb-3 text-xs text-gray-600">
                                            <span class="font-medium">{{ $t('traceModal.instructionsCount', { count: instructionsSummaryItems.length }) }}</span>
                                            <span v-if="instructionsAlwaysCount" class="text-[9px] px-1.5 py-0.5 rounded bg-green-100 text-green-700">{{ $t('traceModal.alwaysCount', { count: instructionsAlwaysCount }) }}</span>
                                            <span v-if="instructionsIntelligentCount" class="text-[9px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-700">{{ $t('traceModal.intelligentCount', { count: instructionsIntelligentCount }) }}</span>
                                        </div>
                                        <!-- Collapsible list -->
                                        <div class="space-y-1">
                                            <div v-for="ins in instructionsSummaryItems" :key="ins.id"
                                                 class="flex items-center gap-2 text-xs text-gray-700 px-2 py-1.5 rounded bg-gray-50 hover:bg-gray-100 cursor-pointer"
                                                 @click="emit('openInstruction', ins.id)">
                                                <UIcon name="i-heroicons-cube" class="w-3 h-3 text-indigo-500 flex-shrink-0" />
                                                <span class="font-medium flex-1 truncate">{{ ins.title || truncateText(ins.text || '', 60) }}</span>
                                                <span v-if="ins.category" class="text-[9px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-600 flex-shrink-0">{{ ins.category }}</span>
                                                <span class="text-[9px] px-1.5 py-0.5 rounded flex-shrink-0"
                                                      :class="ins.load_mode === 'always' ? 'bg-green-100 text-green-700' : ins.load_mode === 'intelligent' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'">
                                                    {{ ins.load_mode || 'always' }}
                                                </span>
                                            </div>
                                        </div>
                                    </div>
                                    <div v-else class="text-xs text-gray-500">{{ $t('traceModal.noInstructions') }}</div>
                                </template>

                                <!-- Decision details (minimal) -->
                                <template v-else>
                                    <!-- Feedback details -->
                                    <div v-if="selectedItem.kind === 'feedback'">
                                        <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">{{ $t('traceModal.feedback') }}</div>
                                        <div class="flex items-center space-x-2 mb-2">
                                            <span class="inline-flex px-2 py-1 text-xs font-medium rounded-full"
                                                  :class="(selectedItem.direction || 0) > 0 ? 'bg-green-100 text-green-800' : (selectedItem.direction || 0) < 0 ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-800'">
                                                {{ (selectedItem.direction || 0) > 0 ? $t('traceModal.positive') : (selectedItem.direction || 0) < 0 ? $t('traceModal.negative') : $t('traceModal.neutral') }}
                                            </span>
                                            <span class="text-xs text-gray-500">{{ formatDate(selectedItem.created_at) }}</span>
                                        </div>
                                        <div v-if="selectedItem.message">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">{{ $t('traceModal.message') }}</div>
                                            <pre class="text-xs text-gray-900 whitespace-pre-wrap font-sans leading-relaxed break-words">{{ selectedItem.message }}</pre>
                                        </div>
                                    </div>
                                    <!-- Non-feedback details -->
                                    <div v-else>
                                        <div v-if="selectedItem.reasoning || selectedItem.plan_decision?.reasoning">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">{{ $t('traceModal.reasoning') }}</div>
                                            <pre class="text-xs text-gray-900 whitespace-pre-wrap font-sans leading-relaxed break-words">{{ selectedItem.reasoning || selectedItem.plan_decision?.reasoning }}</pre>
                                        </div>
                                        <div>
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">{{ $t('traceModal.content') }}</div>
                                            <pre class="text-xs text-gray-900 whitespace-pre-wrap font-sans leading-relaxed break-words">{{ selectedItem.content || selectedItem.plan_decision?.assistant || $t('traceModal.noContent') }}</pre>
                                        </div>

                                        <!-- Tool execution with specialized rendering -->
                                        <div v-if="selectedItem.tool_execution" class="mt-4">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">{{ $t('traceModal.toolExecution') }}</div>
                                            <!-- Use specialized tool component if available -->
                                            <component
                                                v-if="shouldUseToolComponent(selectedItem.tool_execution)"
                                                :is="getToolComponent(selectedItem.tool_execution.tool_name)"
                                                :tool-execution="selectedItem.tool_execution"
                                            />
                                            <!-- Fallback to generic tool display -->
                                            <GenericTool
                                                v-else
                                                :tool-execution="selectedItem.tool_execution"
                                            />
                                            <!-- Error message fallback when result_json is empty -->
                                            <div v-if="selectedItem.tool_execution.status === 'error' && selectedItem.tool_execution.error_message && !selectedItem.tool_execution.result_json"
                                                 class="mt-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2 whitespace-pre-wrap break-words font-mono">
                                                {{ selectedItem.tool_execution.error_message }}
                                            </div>
                                        </div>

                                        <!-- Instructions loaded by this tool -->
                                        <div v-if="selectedItem.tool_execution?.result_json?.related_instructions?.length" class="mt-4">
                                            <div
                                                class="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-gray-500 mb-2 cursor-pointer hover:text-gray-700"
                                                @click="showToolInstructions = !showToolInstructions"
                                            >
                                                <UIcon :name="showToolInstructions ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
                                                <UIcon name="i-heroicons-cube" class="w-3 h-3" />
                                                {{ $t('traceModal.instructionsLoaded', { count: selectedItem.tool_execution.result_json.related_instructions.length }) }}
                                            </div>
                                            <Transition name="fade">
                                                <div v-if="showToolInstructions" class="space-y-1">
                                                    <div v-for="ins in selectedItem.tool_execution.result_json.related_instructions" :key="ins.id"
                                                         class="flex items-center gap-2 text-xs text-gray-700 px-2 py-1.5 rounded bg-gray-50 hover:bg-gray-100 cursor-pointer"
                                                         @click="emit('openInstruction', ins.id)">
                                                        <UIcon name="i-heroicons-cube" class="w-3 h-3 text-indigo-500 flex-shrink-0" />
                                                        <span class="font-medium">{{ ins.title || truncateText(ins.text || '', 60) }}</span>
                                                        <span v-if="ins.category" class="text-[9px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-600 ms-auto">{{ ins.category }}</span>
                                                    </div>
                                                </div>
                                            </Transition>
                                        </div>

                                        <!-- Sub-timings: per-query breakdown -->
                                        <div v-if="selectedItemSubTimings" class="mt-4">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">{{ $t('traceModal.queryTiming') }}</div>
                                            <div class="space-y-1 text-xs">
                                                <!-- Phase summary row -->
                                                <div class="flex items-center gap-3 text-gray-500 mb-2">
                                                    <span v-if="selectedItemSubTimings.codegen_ms != null">
                                                        {{ $t('traceModal.llmCodegen') }} <span class="font-medium text-gray-700">{{ formatDuration(selectedItemSubTimings.codegen_ms) }}</span>
                                                    </span>
                                                    <span v-if="selectedItemSubTimings.execution_ms != null">
                                                        {{ $t('traceModal.dataQueryExecution') }} <span class="font-medium text-gray-700">{{ formatDuration(selectedItemSubTimings.execution_ms) }}</span>
                                                    </span>
                                                    <span v-if="selectedItemSubTimings.retry_count">
                                                        {{ $t('traceModal.retries') }} <span class="font-medium text-red-600">{{ selectedItemSubTimings.retry_count }}</span>
                                                    </span>
                                                </div>
                                                <!-- Per-query table -->
                                                <div v-if="selectedItemSubTimings.queries?.length" class="border border-gray-200 rounded overflow-hidden">
                                                    <table class="w-full text-[11px]">
                                                        <thead class="bg-gray-50 text-gray-500">
                                                            <tr>
                                                                <th class="px-2 py-1 text-start font-medium">#</th>
                                                                <th class="px-2 py-1 text-end font-medium">{{ $t('traceModal.tableTime') }}</th>
                                                                <th class="px-2 py-1 text-end font-medium">{{ $t('traceModal.tableRows') }}</th>
                                                                <th class="px-2 py-1 text-start font-medium">{{ $t('traceModal.tableSql') }}</th>
                                                            </tr>
                                                        </thead>
                                                        <tbody>
                                                            <tr v-for="q in selectedItemSubTimings.queries" :key="q.index"
                                                                :class="q.error ? 'bg-red-50' : 'even:bg-gray-50'">
                                                                <td class="px-2 py-1 text-gray-500">{{ q.index + 1 }}</td>
                                                                <td class="px-2 py-1 text-end font-mono"
                                                                    :class="q.query_ms > 3000 ? 'text-red-600 font-semibold' : q.query_ms > 1000 ? 'text-orange-600' : 'text-gray-700'">
                                                                    {{ formatDuration(q.query_ms) }}
                                                                </td>
                                                                <td class="px-2 py-1 text-end text-gray-500">{{ q.rows ?? '—' }}</td>
                                                                <td class="px-2 py-1 text-gray-700 truncate max-w-[200px]" :title="q.sql ?? ''">
                                                                    <span v-if="q.error" class="text-red-600">{{ q.error }}</span>
                                                                    <span v-else>{{ q.sql }}</span>
                                                                </td>
                                                            </tr>
                                                        </tbody>
                                                    </table>
                                                </div>
                                            </div>
                                        </div>

                                        <!-- Stages waterfall -->
                                        <div v-if="filteredStages.length" class="mt-4">
                                            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">{{ $t('traceModal.stages') }}</div>
                                            <div class="space-y-1">
                                                <div v-for="s in filteredStages" :key="s.stage"
                                                     class="flex items-center gap-2 text-[11px]">
                                                    <span class="w-36 text-gray-600 truncate text-end" :title="s.stage">{{ humanizeStage(s.stage) }}</span>
                                                    <span class="w-16 text-end font-mono"
                                                          :class="s.ms > 5000 ? 'text-red-600 font-semibold' : s.ms > 1000 ? 'text-orange-600' : 'text-gray-700'">
                                                        {{ formatDuration(s.ms) }}
                                                    </span>
                                                    <div class="flex-1 h-2 bg-gray-100 rounded overflow-hidden">
                                                        <div class="h-full rounded"
                                                             :class="s.ms > 5000 ? 'bg-red-400' : s.ms > 1000 ? 'bg-orange-400' : 'bg-gray-300'"
                                                             :style="{ width: Math.max(2, (s.ms / Math.max(...filteredStages.map((x: any) => x.ms))) * 100) + '%' }">
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </template>
                            </div>

                            <!-- Step Details (unused in compact UI) -->
                            <div v-if="false" class="space-y-4">
                                <div class="grid grid-cols-2 gap-4">
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Title</label>
                                        <p class="text-xs text-gray-900">{{ selectedItem.title }}</p>
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Status</label>
                                        <span :class="[
                                            'inline-flex px-2 py-1 text-xs font-medium rounded-full',
                                            selectedItem.status === 'success' ? 'bg-green-100 text-green-800' :
                                            selectedItem.status === 'error' ? 'bg-red-100 text-red-800' :
                                            'bg-gray-100 text-gray-800'
                                        ]">
                                            {{ selectedItem.status }}
                                        </span>
                                    </div>
                                </div>

                                <div v-if="selectedItem.data_model">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Data Model</label>
                                    <div class="p-3 bg-gray-50 rounded-lg border max-h-32 overflow-y-auto">
                                        <pre class="text-xs text-gray-900">{{ JSON.stringify(selectedItem.data_model, null, 2) }}</pre>
                                    </div>
                                </div>

                                <div v-if="selectedItem.code">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Generated Code</label>
                                    <div class="p-3 bg-gray-900 rounded-lg max-h-40 overflow-y-auto">
                                        <pre class="text-xs text-green-400 font-mono">{{ selectedItem.code }}</pre>
                                    </div>
                                </div>

                                <div v-if="selectedItem.data">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Data Output</label>
                                    <div class="border rounded-lg bg-white h-48">
                                        <RenderTable 
                                            v-if="selectedItem.data?.columns" 
                                            :widget="{ id: 'trace-widget' }" 
                                            :step="selectedItem" 
                                        />
                                        <div v-else class="p-3 bg-gray-50 rounded-lg border h-full overflow-y-auto">
                                            <pre class="text-xs text-gray-900">{{ JSON.stringify(selectedItem.data, null, 2) }}</pre>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <!-- Feedback Details (unused in compact UI) -->
                            <div v-if="false" class="space-y-4">
                                <div class="grid grid-cols-2 gap-4">
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Direction</label>
                                        <span :class="[
                                            'inline-flex px-2 py-1 text-xs font-medium rounded-full',
                                            selectedItem.direction === 1 ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                                        ]">
                                            {{ selectedItem.direction === 1 ? 'Positive' : 'Negative' }}
                                        </span>
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-gray-700 mb-1">Feedback ID</label>
                                        <p class="text-xs text-gray-900">{{ selectedItem.feedback_id }}</p>
                                    </div>
                                </div>

                                <div v-if="selectedItem.message">
                                    <label class="block text-xs font-medium text-gray-700 mb-2">Message</label>
                                    <div class="p-3 bg-gray-50 rounded-lg border">
                                        <p class="text-xs text-gray-900">{{ selectedItem.message }}</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div><!-- /pane C scroll -->
                </div><!-- /pane C -->
            </div><!-- /content flex -->
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
import { MarkdownRender } from 'markstream-vue'
import RenderTable from '../RenderTable.vue'
import ContextBrowser from './ContextBrowser.vue'
import GenericTool from '../tools/GenericTool.vue'
import CreateWidgetTool from '../tools/CreateWidgetTool.vue'
import CreateDataTool from '../tools/CreateDataTool.vue'
import InspectDataTool from '../tools/InspectDataTool.vue'
import CreateInstructionTool from '../tools/CreateInstructionTool.vue'
import EditInstructionTool from '../tools/EditInstructionTool.vue'
import SendEmailTool from '../tools/SendEmailTool.vue'
import ListAgentExecutionsTool from '../tools/ListAgentExecutionsTool.vue'
import DataSourceIcon from '../DataSourceIcon.vue'
import Spinner from '../Spinner.vue'
const { isJudgeEnabled } = useOrgSettings()
const { t } = useI18n()

interface ToolExecutionUI {
    tool_name: string
    tool_action?: string
    result_json?: any
    error_message?: string | null
    duration_ms?: number
    status?: string
    sub_timings_json?: {
        total_ms?: number
        setup_ms?: number | null
        retry_count?: number
        codegen_ms?: number | null
        execution_ms?: number | null
        queries?: Array<{
            index: number
            query_ms: number
            rows?: number | null
            sql?: string | null
            error?: string
        }>
        stages?: Array<{
            stage: string
            ms: number
        }>
    } | null
}

interface CompletionFeedbackUI {
    id: string
    direction: number
    message?: string
    created_at: string
}

interface InstructionBuild {
    id: string
    build_number: number
    title?: string
    is_main: boolean
    status: string
}

interface CompletionBlockV2 {
    id: string
    completion_id: string
    agent_execution_id?: string
    block_index: number
    title: string
    status: string
    content?: string
    reasoning?: string
    tool_execution?: ToolExecutionUI
    created_at: string
}

interface IterationTiming {
    loop_index?: number | null
    block_index?: number | null
    llm_ms?: number | null
    tool_name?: string | null
    tool_ms?: number | null
    sub_timings?: {
        total_ms?: number
        setup_ms?: number | null
        retry_count?: number
        codegen_ms?: number | null
        execution_ms?: number | null
        queries?: Array<{
            index: number
            query_ms: number
            rows?: number | null
            sql?: string | null
            error?: string
        }>
    } | null
}

interface TimingBreakdown {
    setup_ms?: number | null
    total_duration_ms?: number | null
    total_tool_ms?: number | null
    total_llm_ms?: number | null
    total_db_ms?: number | null
    iterations: IterationTiming[]
}

interface AgentExecutionTraceResponse {
    agent_execution: any
    completion_blocks: CompletionBlockV2[]
    head_prompt_snippet?: string
    head_context_snapshot?: any
    latest_feedback?: CompletionFeedbackUI | null
    build?: InstructionBuild
    timing_breakdown?: TimingBreakdown | null
}

interface TraceCompletionData {
    completion_id: string
    role: string
    content?: string
    reasoning?: string
    created_at: string
    status?: string
    has_issue: boolean
    issue_type?: string
    instructions_effectiveness?: number
    context_effectiveness?: number
    response_score?: number
}

interface TraceStepData {
    step_id: string
    title: string
    status: string
    code?: string
    data_model?: any
    data?: any
    created_at: string
    completion_id: string
    has_issue: boolean
}

interface TraceFeedbackData {
    feedback_id: string
    direction: number
    message?: string
    created_at: string
    completion_id: string
}

interface TraceData {
    report_id: string
    head_completion: TraceCompletionData
    completions: TraceCompletionData[]
    steps: TraceStepData[]
    feedbacks: TraceFeedbackData[]
    issue_completion_id: string
    issue_type: string
    user_name: string
    user_email?: string
}

interface ConversationTurn {
    user_completion_id?: string | null
    user_prompt?: string
    role: string
    completion_id?: string | null
    agent_execution_id?: string | null
    assistant_content?: string
    status: string
    total_tools: number
    total_failed_tools: number
    total_successful_tools: number
    tool_names: string[]
    step_titles: string[]
    feedback_status: string
    feedback_direction: number
    feedback_message?: string | null
    instructions_effectiveness?: number | null
    context_effectiveness?: number | null
    response_score?: number | null
    judge?: Record<string, { score?: number | null; reasoning?: string | null }> | null
    total_duration_ms?: number | null
    created_at?: string | null
}

interface ConversationTraceResponse {
    report_id: string
    report_title?: string
    user_name?: string
    user_email?: string
    total_turns: number
    failed_turns: number
    negative_feedback_turns: number
    turns: ConversationTurn[]
}

interface Props {
    modelValue: boolean
    reportId: string
    completionId?: string
}

const props = defineProps<Props>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'openInstruction': [id: string]
}>()

// State
const isLoading = ref(false)
const isConvLoading = ref(false)
const traceData = ref<AgentExecutionTraceResponse | null>(null)
const conversation = ref<ConversationTraceResponse | null>(null)
const selectedCompletionId = ref<string | null>(null)
const activeTab = ref<'trace' | 'context'>('trace')
const selectedItem = ref<any>(null)
const selectedItemType = ref<'block'>('block')
const blocks = computed(() => traceData.value?.completion_blocks || [])
const turns = computed(() => conversation.value?.turns || [])
const selectedTurn = computed(() => turns.value.find(t => t.completion_id === selectedCompletionId.value) || null)

const selectedItemSubTimings = computed(() => {
    const te = selectedItem.value?.tool_execution
    return te?.sub_timings_json ?? null
})

const filteredStages = computed(() => {
    const stages = selectedItemSubTimings.value?.stages
    if (!Array.isArray(stages) || !stages.length) return []
    return stages
})

const instructionsSummaryItems = computed(() => {
    return traceData.value?.head_context_snapshot?.context_view_json?.instructions_usage || []
})

const instructionsAlwaysCount = computed(() => instructionsSummaryItems.value.filter((i: any) => (i.load_mode || 'always') === 'always').length)
const instructionsIntelligentCount = computed(() => instructionsSummaryItems.value.filter((i: any) => i.load_mode === 'intelligent').length)

const showToolInstructions = ref(false)

const selectedItemDataSources = computed(() => {
    const item = selectedItem.value
    if (!item) return []
    // From tool_execution.data_sources on the selected block
    if (item.tool_execution?.data_sources) return item.tool_execution.data_sources
    if (item.data_sources) return item.data_sources
    return []
})

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const systemCompletions = computed(() => [])
const harnessCollapsed = ref(true)
const toggleHarnessCollapsed = () => { harnessCollapsed.value = !harnessCollapsed.value }
const harnessCount = computed(() => blocks.value.filter((b: any) => (b as any).phase === 'knowledge_harness').length)
const leftItems = computed(() => {
    const items: any[] = []
    // 1) Overview — prompt + assessment (judge scores) + context (incl. instructions)
    if (traceData.value) {
        items.push({ id: 'overview', kind: 'overview', title: 'Overview', subtitle: traceData.value.head_prompt_snippet })
    }
    // 2) Decisions (blocks) — main-loop first, then knowledge harness
    const mainBlocks = blocks.value.filter((b: any) => (b as any).phase !== 'knowledge_harness')
    const harnessBlocks = blocks.value.filter((b: any) => (b as any).phase === 'knowledge_harness')
    const pushBlock = (b: any, phase?: string) => {
        const te = (b as any).tool_execution
        const action = te?.tool_action ? te.tool_action : undefined
        const tool_call_name = action ? `${te.tool_name}.${action}` : te?.tool_name
        const data_sources = te?.data_sources || (b as any).tool_execution?.data_sources || []
        const title = tool_call_name ? t('traceModal.decision', { name: tool_call_name }) : (b.title || t('traceModal.decision', { name: '' }).replace(/:\s*$/, ''))
        items.push({ id: b.id, kind: 'decision', title, subtitle: undefined, ref: b, data_sources, phase })
    }
    for (const b of mainBlocks) pushBlock(b)
    if (harnessBlocks.length) {
        items.push({ id: 'knowledge_harness_header', kind: 'section', title: t('traceModal.knowledgeHarness') })
        for (const b of harnessBlocks) pushBlock(b, 'knowledge_harness')
    }
    // 2b) Latest feedback (if exists)
    if (traceData.value?.latest_feedback) {
        const fb = traceData.value.latest_feedback
        const label = fb.direction > 0 ? t('traceModal.positive') : (fb.direction < 0 ? t('traceModal.negative') : t('traceModal.neutral'))
        const subtitle = fb.message ? (fb.message.length > 140 ? fb.message.slice(0, 140) + '…' : fb.message) : undefined
        items.push({ id: 'latest_feedback', kind: 'feedback', title: t('traceModal.feedbackLabel', { label }), subtitle, ref: fb })
    }
    // 3) Analysis completed marker (if any block has analysis_complete)
    const hasFinal = blocks.value.some((b: any) => b?.plan_decision?.analysis_complete)
    if (hasFinal) {
        items.push({ id: 'analysis_completed', kind: 'final', title: t('traceModal.decisionAnalysisCompleted') })
    }
    return items
})
const visibleLeftItems = computed(() => {
    if (!harnessCollapsed.value) return leftItems.value
    return leftItems.value.filter((it: any) => it.phase !== 'knowledge_harness')
})

// Methods
const fetchConversation = async () => {
    if (!props.reportId) return
    isConvLoading.value = true
    try {
        const response = await useMyFetch<ConversationTraceResponse>(`/api/console/reports/${props.reportId}/conversation`)
        if (response.error.value) {
            console.error('Error fetching conversation:', response.error.value)
        } else if (response.data.value) {
            conversation.value = response.data.value
            // Preselect: the completion the modal was opened on, else the last
            // turn that has a trace, else the last turn.
            const wanted = props.completionId
                ? turns.value.find(t => t.completion_id === props.completionId)
                : null
            const fallback = [...turns.value].reverse().find(t => t.completion_id) || turns.value[turns.value.length - 1]
            const initial = wanted || fallback
            if (initial?.completion_id) {
                await selectTurn(initial)
            }
        }
    } catch (error) {
        console.error('Failed to fetch conversation:', error)
    } finally {
        isConvLoading.value = false
    }
}

const selectTurn = async (turn: ConversationTurn) => {
    if (!turn?.completion_id) return
    selectedCompletionId.value = turn.completion_id
    await fetchTraceData()
}

// Pane A: chat rendering helpers
const chatBlocks = (turn: ConversationTurn) =>
    (turn.completion_blocks || []).filter((b: any) => b.phase !== 'knowledge_harness')

const chatBlockTitle = (block: any) => {
    if (block.tool_execution) {
        const te = block.tool_execution
        return `${te.tool_name}${te.tool_action ? ' → ' + te.tool_action : ''}`
    }
    return block.title || 'Step'
}

const onChatBlockClick = async (turn: ConversationTurn, block: any) => {
    if (selectedCompletionId.value !== turn.completion_id) {
        selectedCompletionId.value = turn.completion_id
        await fetchTraceData()
    }
    const match = (traceData.value?.completion_blocks || []).find((b: any) => b.id === block.id) || block
    selectBlock(match)
}

// Pane B: timeline bar helpers
const itemLlmMs = (item: any): number => {
    const st = item?.ref?.tool_execution?.sub_timings_json
    if (st?.codegen_ms != null) return st.codegen_ms
    const pm = item?.ref?.plan_decision?.metrics_json
    if (pm?.total_duration_ms != null) return pm.total_duration_ms
    return 0
}
const itemExecMs = (item: any): number => {
    const st = item?.ref?.tool_execution?.sub_timings_json
    if (st?.execution_ms != null) return st.execution_ms
    if (item?.ref?.tool_execution) {
        const total = getItemDurationMs(item) || 0
        return Math.max(total - (st?.codegen_ms || 0), 0)
    }
    return 0
}
const maxItemMs = computed(() => {
    const ds = visibleLeftItems.value
        .map((it: any) => getItemDurationMs(it))
        .filter((x: any) => x != null) as number[]
    return ds.length ? Math.max(...ds, 1) : 1
})
const barPct = (ms: number) => (ms ? Math.max((ms / maxItemMs.value) * 100, 1) : 0)

const fetchTraceData = async () => {
    if (!props.reportId || !selectedCompletionId.value) return

    isLoading.value = true
    traceData.value = null
    selectedItem.value = null
    try {
        const response = await useMyFetch<AgentExecutionTraceResponse>(`/api/console/agent_executions/by-completion/${selectedCompletionId.value}`)

        if (response.error.value) {
            console.error('Error fetching trace data:', response.error.value)
        } else if (response.data.value) {
            traceData.value = response.data.value
            // Always open on the Overview
            selectedItem.value = { id: 'overview', title: 'Overview', created_at: traceData.value?.agent_execution?.started_at }
            selectedItemType.value = 'block'
        }
    } catch (error) {
        console.error('Failed to fetch trace data:', error)
    } finally {
        isLoading.value = false
    }
}

const closeModal = () => {
    emit('update:modelValue', false)
    selectedItem.value = null
    traceData.value = null
    conversation.value = null
    selectedCompletionId.value = null
    activeTab.value = 'trace'
}

// Conversation rail helpers
const statusLabel = (status: string) => {
    if (status === 'in_progress') return 'running'
    return status
}

const statusTextClass = (status: string) => {
    if (status === 'error') return 'text-red-500'
    if (status === 'in_progress') return 'text-amber-600'
    return 'text-green-600'
}

const statusChipClass = (status: string) => {
    if (status === 'error') return 'bg-red-50 text-red-700'
    if (status === 'in_progress') return 'bg-amber-50 text-amber-700'
    return 'bg-green-50 text-green-700'
}

const assistantSnippet = (turn: ConversationTurn) => {
    const raw = (turn.assistant_content || '').replace(/[*#`>_]/g, '').replace(/\s+/g, ' ').trim()
    if (raw) return raw
    if (turn.status === 'in_progress') return 'Running…'
    return '—'
}

const hasTurnScores = (turn: ConversationTurn) => {
    return turn.instructions_effectiveness != null || turn.context_effectiveness != null || turn.response_score != null
}

// LLM Judge assessment rows for the Overview. Prefer the per-dimension score
// from judge_json; fall back to the scalar column. Reasoning shows only when
// present, and the instructions/context rationale (a single combined judge
// explanation) is shown once to avoid duplication.
const assessmentRows = computed(() => {
    const turn = selectedTurn.value
    if (!turn) return [] as any[]
    const j = turn.judge || {}
    const dims = [
        { key: 'instructions', label: 'Instructions', scalar: turn.instructions_effectiveness, bar: 'bg-blue-400', text: 'text-blue-700' },
        { key: 'context', label: 'Context', scalar: turn.context_effectiveness, bar: 'bg-purple-400', text: 'text-purple-700' },
        { key: 'response', label: 'Response', scalar: turn.response_score, bar: 'bg-green-400', text: 'text-green-700' },
    ]
    const rows: any[] = []
    const seenReasoning = new Set<string>()
    for (const d of dims) {
        const score = (j as any)[d.key]?.score ?? d.scalar
        if (score == null) continue
        let reasoning = ((j as any)[d.key]?.reasoning || '').trim()
        if (reasoning && seenReasoning.has(reasoning)) reasoning = ''  // de-dupe combined instructions/context text
        else if (reasoning) seenReasoning.add(reasoning)
        rows.push({ key: d.key, label: d.label, score, bar: d.bar, text: d.text, reasoning })
    }
    return rows
})

const selectItem = (item: any) => {
    selectedItem.value = { ...item, id: item.completion_id || item.step_id || item.feedback_id }
}

const selectBlock = (block: any) => {
    selectedItem.value = { ...block, id: block.id }
    selectedItemType.value = 'block'
}

const selectLeftItem = (item: any) => {
    if (item.kind === 'decision' && item.ref) {
        selectBlock(item.ref)
    } else if (item.kind === 'overview') {
        selectedItem.value = { id: 'overview', title: 'Overview', created_at: traceData.value?.agent_execution?.started_at }
        selectedItemType.value = 'block'
    } else if (item.kind === 'feedback' && item.ref) {
        const fb = item.ref as CompletionFeedbackUI
        selectedItem.value = { id: 'latest_feedback', kind: 'feedback', title: t('traceModal.feedback'), direction: fb.direction, message: fb.message, created_at: fb.created_at }
        selectedItemType.value = 'block'
    } else if (item.kind === 'final') {
        selectedItem.value = { id: 'analysis_completed', title: t('traceModal.analysisCompleted'), content: t('traceModal.analysisMarkedComplete'), created_at: traceData.value?.agent_execution?.completed_at }
        selectedItemType.value = 'block'
    }
}


function getItemDurationMs(item: any): number | null {
    const block = item?.ref || item
    if (!block) return null
    const te = block.tool_execution
    if (te && typeof te.duration_ms === 'number') return te.duration_ms
    if (typeof block.duration_ms === 'number') return block.duration_ms
    // Planner decision timing
    const pm = block.plan_decision?.metrics_json
    if (pm?.total_duration_ms != null) return pm.total_duration_ms
    return null
}

function getTopStages(subTimings: any): Array<{ stage: string; ms: number }> {
    const stages = subTimings?.stages
    if (!Array.isArray(stages) || !stages.length) return []
    return [...stages].sort((a, b) => b.ms - a.ms).slice(0, 2)
}

function humanizeStage(stage: string): string {
    return stage.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

function formatDuration(ms: number): string {
    if (ms < 1000) return `${Math.round(ms)} ms`
    const seconds = ms / 1000
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`
    const minutes = seconds / 60
    return `${minutes.toFixed(1)} m`
}

const getStepsForCompletion = (_completionId: string) => []
const getFeedbackForCompletion = (_completionId: string) => []

const getCompletionIcon = (completion: TraceCompletionData) => {
    if (completion.has_issue) return 'i-heroicons-exclamation-triangle'
    return completion.role === 'user' ? 'i-heroicons-user' : 'i-heroicons-cpu-chip'
}

const getCompletionIconClass = (completion: TraceCompletionData) => {
    if (completion.has_issue) return 'w-4 h-4 text-red-600 mr-2'
    return completion.role === 'user' ? 'w-4 h-4 text-blue-600 mr-2' : 'w-4 h-4 text-gray-600 mr-2'
}

const getCompletionLabel = (completion: TraceCompletionData) => {
    if (completion.role === 'user') return 'User Input'
    return 'System Response'
}

const getStepIcon = (step: TraceStepData) => {
    if (step.has_issue) return 'i-heroicons-x-circle'
    return step.status === 'success' ? 'i-heroicons-check-circle' : 'i-heroicons-clock'
}

const getStepIconClass = (step: TraceStepData) => {
    if (step.has_issue) return 'w-3 h-3 text-red-600'
    return step.status === 'success' ? 'w-3 h-3 text-green-600' : 'w-3 h-3 text-yellow-600'
}

const getIssueLabel = (issueType?: string) => {
    switch (issueType) {
        case 'failed_step': return 'Failed Step'
        case 'negative_feedback': return 'Negative Feedback'
        case 'both': return 'Multiple Issues'
        default: return 'Issue'
    }
}

const getSelectedItemIcon = () => 'i-heroicons-cog-6-tooth'

const getSelectedItemTitle = () => selectedItem.value?.title || t('traceModal.block')

const getStatusIcon = (status: string) => {
    if (status === 'error') return 'i-heroicons-x-circle'
    if (status === 'success' || status === 'completed') return 'i-heroicons-check-circle'
    return 'i-heroicons-clock'
}

const getStatusIconClass = (status: string) => {
    if (status === 'error') return 'w-3 h-3 text-red-600'
    if (status === 'success' || status === 'completed') return 'w-3 h-3 text-green-600'
    return 'w-3 h-3 text-gray-500'
}

const getBlockTitle = (block: CompletionBlockV2) => {
    if (block.title) return block.title
    if ((block as any)?.tool_execution) {
        const te = (block as any).tool_execution
        return `${te.tool_name}${te.tool_action ? ' → ' + te.tool_action : ''}`
    }
    return 'Block'
}

const getLeftItemIcon = (item: any) => {
    if (item.kind === 'overview') return 'i-heroicons-clipboard-document-list'
    if (item.kind === 'prompt') return 'i-heroicons-user'
    if (item.kind === 'instructions') return 'i-heroicons-cube'
    if (item.kind === 'final') return 'i-heroicons-check-circle'
    if (item.kind === 'feedback') return (item?.ref?.direction || 0) > 0 ? 'i-heroicons-hand-thumb-up' : 'i-heroicons-hand-thumb-down'
    const status = item?.ref?.status
    return getStatusIcon(status || '')
}

const getLeftItemIconClass = (item: any) => {
    if (item.kind === 'overview') return 'w-3 h-3 text-gray-500'
    if (item.kind === 'prompt') return 'w-3 h-3 text-blue-600'
    if (item.kind === 'instructions') return 'w-3 h-3 text-indigo-600'
    if (item.kind === 'final') return 'w-3 h-3 text-green-600'
    if (item.kind === 'feedback') return (item?.ref?.direction || 0) > 0 ? 'w-3 h-3 text-green-600' : 'w-3 h-3 text-red-600'
    const status = item?.ref?.status
    return getStatusIconClass(status || '')
}

const truncateText = (text: string, maxLength: number) => {
    if (!text) return ''
    if (text.length <= maxLength) return text
    return text.slice(0, maxLength) + '…'
}

const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString()
}

const hasAnyScores = (item: any) => {
    return item.instructions_effectiveness || item.context_effectiveness || item.response_score
}

const hasAnyCompletionScores = (completion: any) => {
    return completion.instructions_effectiveness !== null || 
           completion.context_effectiveness !== null || 
           completion.response_score !== null
}

// Tool component helpers (matching index.vue)
function getToolComponent(toolName: string) {
    switch (toolName) {
        case 'create_widget':
            return CreateWidgetTool
        case 'create_data':
            return CreateDataTool
        case 'inspect_data':
            return InspectDataTool
        case 'create_instruction':
            return CreateInstructionTool
        case 'edit_instruction':
            return EditInstructionTool
        case 'send_email':
            return SendEmailTool
        case 'list_agent_executions':
            return ListAgentExecutionsTool
        default:
            return null
    }
}

function shouldUseToolComponent(toolExecution: any): boolean {
    return getToolComponent(toolExecution.tool_name) !== null
}

// Watch for modal opening
watch(() => props.modelValue, (newValue) => {
    if (newValue) {
        fetchConversation()
    }
})
</script> 