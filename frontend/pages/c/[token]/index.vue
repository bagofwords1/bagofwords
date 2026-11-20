<template>
    <div class="h-screen w-full bg-white dark:bg-gray-900 flex flex-col overflow-hidden">
        <!-- Loading state -->
        <div v-if="isLoading" class="flex-1 flex items-center justify-center text-gray-500 dark:text-gray-400">
            <Spinner class="w-5 h-5 me-2" />
            <span class="text-sm">{{ $t('sharedConversation.loading') }}</span>
        </div>

        <!-- Access error state -->
        <div v-else-if="accessError" class="flex-1 flex items-center justify-center">
            <div class="text-center max-w-md mx-auto px-6">
                <Icon :name="accessError === 'login' ? 'heroicons:lock-closed' : 'heroicons:shield-exclamation'" class="w-12 h-12 text-ink-muted mx-auto mb-4" />
                <h2 class="text-lg font-semibold text-gray-900 dark:text-white mb-2">
                    {{ accessError === 'login' ? $t('sharedConversation.signInTitle') : $t('sharedConversation.deniedTitle') }}
                </h2>
                <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
                    {{ accessError === 'login'
                        ? $t('sharedConversation.signInBody')
                        : $t('sharedConversation.deniedBody') }}
                </p>
                <a v-if="accessError === 'login'" :href="`/users/sign-in?redirect=/c/${$route.params.token}`"
                    class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                    {{ $t('sharedConversation.signIn') }}
                </a>
                <a v-else href="/"
                    class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800">
                    {{ $t('sharedConversation.goHome') }}
                </a>
            </div>
        </div>

        <!-- Error state -->
        <div v-else-if="error" class="flex-1 flex items-center justify-center">
            <div class="text-center">
                <Icon name="heroicons:exclamation-circle" class="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
                <h1 class="text-lg font-medium text-gray-700 dark:text-gray-300">{{ $t('sharedConversation.notFoundTitle') }}</h1>
                <p class="text-sm text-ink-muted mt-1">{{ $t('sharedConversation.notFoundBody') }}</p>
            </div>
        </div>

        <!-- Conversation content -->
        <template v-else>
            <!-- Minimal header -->
            <header class="flex-none border-b border-gray-100 dark:border-gray-800 py-5 px-4 bg-white dark:bg-gray-900 z-10">
                <div class="max-w-chat mx-auto flex items-start justify-between">
                    <div>
                        <h1 v-text-dir class="text-base font-medium text-gray-900 dark:text-white">{{ conversation.title || $t('reportView.untitled') }}</h1>
                        <p class="text-xs text-ink-muted mt-1">{{ $t('sharedConversation.byAuthor', { name: conversation.user_name }) }} · {{ formatDate(conversation.created_at) }}</p>
                    </div>
                    <!-- Fork button -->
                    <button
                        v-if="forkEligibility?.can_fork"
                        @click="handleFork"
                        :disabled="isForking"
                        class="flex items-center gap-1 text-xs text-ink-muted hover:text-ink-2 transition-colors disabled:opacity-50 mt-1"
                    >
                        <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5" />
                        <span>{{ isForking ? $t('sharedConversation.forking') : $t('sharedConversation.fork') }}</span>
                    </button>
                </div>
            </header>

            <!-- Messages -->
            <div
                ref="messagesContainer"
                class="flex-1 overflow-y-auto py-6 px-4"
                @scroll="handleScroll"
            >
                <!-- Loading more indicator -->
                <div v-if="isLoadingMore" class="flex justify-center py-3 mb-4">
                    <Spinner class="w-4 h-4 text-ink-muted" />
                    <span class="text-xs text-ink-muted ms-2">{{ $t('reportView.loadingOlderMessages') }}</span>
                </div>

                <!-- Load more button (shown when there's more to load) -->
                <div v-else-if="hasMore" class="flex justify-center py-3 mb-4">
                    <button
                        @click="loadPreviousCompletions"
                        class="text-xs text-ink-muted hover:text-ink-2 transition-colors"
                    >
                        ↑ {{ $t('sharedConversation.loadOlder') }}
                    </button>
                </div>

                <ul class="max-w-chat mx-auto space-y-4">
                    <ResponseLanguageScope v-for="m in visibleCompletions" :key="m.id" :locale="m.prompt?.response_locale" :question-dir="replyDirHints.get(String(m.id))" :blocks="m.completion_blocks" v-slot="{ t: responseT, dir: responseDir }" class="text-gray-700 dark:text-gray-300 text-chat">
                        <!-- Scheduled prompt indicator -->
                        <div v-if="m.scheduled_prompt_id && m.role === 'user'" class="mb-2">
                            <div class="flex items-center gap-1.5 px-3 py-2 text-xs text-ink-muted rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-900">
                                <Icon name="heroicons-clock" class="w-3.5 h-3.5" />
                                <span class="font-medium text-gray-500 dark:text-gray-400">{{ $t('reportView.scheduledRun') }}</span>
                                <span class="text-ink-muted">{{ formatDateTime(m.created_at) }}</span>
                            </div>
                        </div>
                        <div v-else class="flex rounded-lg p-1" :class="m.role === 'user' ? 'justify-end' : 'justify-start'" :dir="m.role === 'user' ? undefined : responseDir">
                            <!-- User message (right-aligned bubble) -->
                            <template v-if="m.role === 'user'">
                                <div class="flex items-start gap-2 max-w-xl w-full mb-4">
                                    <div class="flex-1 flex justify-end">
                                        <div class="inline-block rounded-xl px-3 py-2 bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-white text-start" :dir="promptDir(m.prompt?.content)">
                                            <div v-if="m.prompt?.content" class="pt-1 markdown-wrapper">
                                                <PolishRequestCard v-if="parsePolishPrompt(m.prompt.content)" :request="parsePolishPrompt(m.prompt.content)!" />
                                                <InstructionText
                                                    v-else
                                                    :text="m.prompt.content"
                                                    :references="promptMentionsToRefs(m.prompt.mentions, agentIconTokens)"
                                                    :prose="true"
                                                />
                                            </div>
                                        </div>
                                    </div>
                                    <!-- User avatar (hidden on mobile) -->
                                    <div class="hidden md:block w-[28px] flex-shrink-0">
                                        <div class="h-7 w-7 uppercase flex items-center justify-center text-xs border border-blue-200 bg-blue-100 dark:bg-blue-900/50 rounded-full">
                                            {{ conversation.user_name?.charAt(0) || '?' }}
                                        </div>
                                    </div>
                                </div>
                            </template>

                            <!-- System message (left-aligned) -->
                            <template v-else>
                                <div class="hidden md:block w-6 me-2 flex-shrink-0">
                                    <div class="h-6 w-6 flex items-center justify-center text-xs rounded bg-contain bg-center bg-no-repeat" style="background-image: url('/assets/logo-128.png')">
                                    </div>
                                </div>
                                <div class="flex-1 min-w-0 md:ms-4" @mouseover="citationChips.onOver($event, m)" @mouseout="citationChips.onOut($event)" @focusin="citationChips.onFocusIn($event, m)" @focusout="citationChips.onFocusOut($event)" @click="citationChips.onClick($event, m)" @keydown="citationChips.onKeydown($event)">
                                    <div>
                                        <!-- Run fold: a finished run's activity sits behind this one
                                             row above its final message (useRunActivity.ts). -->
                                        <RunActivityToggle class="mb-3"
                                            v-if="runFold(m) && (!runFold(m)!.live || !!runFold(m)?.steps.length || !timelineBlocks(m).some(blockHasMessage))"
                                            :expanded="isRunExpanded(m)"
                                            :duration="runFoldDuration(m)"
                                            :translate="t"
                                            :live="runFold(m)!.live"
                                            :has-steps="!!runFold(m)?.steps.length"
                                            @toggle="toggleRun(m)"
                                        />
                                        <!-- Render each completion block -->
                                        <template v-for="(block, blockIndex) in timelineBlocks(m)" :key="block.id">
                                            <!-- Steering messages ("Send now" while the agent ran) sit
                                                 right above the step that picked them up
                                                 (useSteerPlacement.ts, shared with the report view). -->
                                            <div v-for="s in steersBeforeBlock(m, blockIndex)" :key="'steer-' + s.id" class="flex justify-end my-2" :data-message-id="s.id">
                                                <div class="flex flex-col items-end max-w-[85%]">
                                                    <div class="flex items-center gap-1 mb-0.5 text-chat-3" :class="isSteerApplied(s) ? 'text-green-600 dark:text-green-400' : 'text-amber-500'" data-testid="steering-badge">
                                                        <Icon :name="isSteerApplied(s) ? 'heroicons-check-circle-solid' : 'heroicons-bolt-solid'" class="w-3 h-3" />
                                                        <span>{{ isSteerApplied(s) ? i18nT('reportView.steeringApplied') : i18nT('reportView.steered') }}</span>
                                                    </div>
                                                    <div class="inline-block rounded-xl px-3 py-2 bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-white text-start border border-amber-200 dark:border-amber-900/60" :dir="promptDir(s.prompt?.content)">
                                                        <div v-if="s.prompt?.content" class="pt-1 markdown-wrapper">
                                                            <InstructionText
                                                                :text="s.prompt.content"
                                                                :references="promptMentionsToRefs(s.prompt.mentions, agentIconTokens)"
                                                                :prose="true"
                                                            />
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        <div v-if="isRunBlockShown(m, block)" :class="{ 'run-activity': isRunActivityBlock(m, block), 'run-group-member': isGroupedTool(m, block) }" :data-run-final="isRunAnswerBlock(m, block) ? '' : undefined">
                                            <!-- Live ticker for a run of low-signal tool steps (policy: useBlockGrouping.ts) -->
                                            <Transition name="fade" appear>
                                                <BlockGroupTicker
                                                    v-if="groupHeaderFor(m, block)"
                                                    :group="groupHeaderFor(m, block)"
                                                    :expanded="isGroupExpanded(groupHeaderFor(m, block).id)"
                                                    @toggle="toggleGroup(groupHeaderFor(m, block).id)"
                                                />
                                            </Transition>
                                            <!-- Keep prose visible when only this verification step is collapsed. -->
                                            <div v-if="isBlockFolded(m, block) && (block.tool_execution?.arguments_json?._verification_group_id || block.tool_execution?.result_json?.verification_group_id || block.tool_execution?.arguments_json?.artifact_id) && (block.content || block.plan_decision?.final_answer || block.plan_decision?.assistant)" class="block-content markdown-wrapper text-ink dark:text-gray-100" :dir="responseDir" :data-response-dir="responseDir">
                                                <MarkdownRender :content="citedMarkdown(m, block.content || block.plan_decision?.final_answer || block.plan_decision?.assistant || '')" :custom-html-tags="FIGURE_TAGS" :final="true" :typewriter="false" :render-code-blocks-as-pre="true" class="markdown-content" />
                                            </div>
                                            <div v-show="!isBlockFolded(m, block)">
                                            <!-- 1. Thinking box (reasoning) -->
                                            <div v-if="(block.plan_decision?.reasoning || block.reasoning || block.status === 'stopped') && isRunReasoningShown(m, block)" class="thinking-box">
                                                <div class="thinking-header" @click="toggleReasoning(block.id)">
                                                    <Icon :name="isReasoningCollapsed(block.id) ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-4 h-4 text-ink-muted rtl-flip" />
                                                    <span v-if="hasCompletedContent(block) || block.tool_execution" class="ms-1">
                                                        {{ getThoughtProcessLabel(block, responseT) }}
                                                    </span>
                                                    <span v-else class="ms-1">
                                                        <div class="dots" />
                                                    </span>
                                                </div>
                                                <Transition name="fade">
                                                    <div v-if="!isReasoningCollapsed(block.id)" class="thinking-content">
                                                        <template v-if="block.plan_decision?.reasoning || block.reasoning">
                                                            <MarkdownRender
                                                                :content="block.plan_decision?.reasoning || block.reasoning || ''"
                                                                :final="true"
                                                                :typewriter="false"
                                                                :render-code-blocks-as-pre="true"
                                                                class="markdown-content"
                                                            />
                                                        </template>
                                                        <template v-else-if="block.status === 'stopped'">
                                                            <div class="text-ink-muted italic">{{ responseT('reportView.generationStoppedBefore') }}</div>
                                                        </template>
                                                    </div>
                                                </Transition>
                                            </div>

                                            <!-- 2. Block content - assistant message -->
                                            <div v-if="(block.content || block.plan_decision?.assistant) && !block.plan_decision?.final_answer && block.status !== 'error'" class="block-content markdown-wrapper text-ink dark:text-gray-100" :dir="responseDir" :data-response-dir="responseDir">
                                                <MarkdownRender
                                                    :content="citedMarkdown(m, block.content || block.plan_decision?.assistant || '')" :custom-html-tags="FIGURE_TAGS"
                                                    :final="true"
                                                    :typewriter="false"
                                                    :render-code-blocks-as-pre="true"
                                                    class="markdown-content"
                                                />
                                            </div>

                                            <RunToolGroupRow v-if="toolGroupHeaderFor(m, block)" :group="toolGroupHeaderFor(m, block)!"
                :expanded="isToolGroupExpanded(toolGroupHeaderFor(m, block)!.id)" :translate="t"
                @toggle="toggleToolGroup(toolGroupHeaderFor(m, block)!.id)" />
            <!-- 3. Tool execution -->
                                            <!-- 3. Tool execution -->
                                            <div v-if="block.tool_execution && isRunToolShown(m, block)" class="tool-execution-container text-start" :class="{ 'ps-4': isGroupedTool(m, block) }" :dir="responseDir">
                                                <component
                                                    v-if="shouldUseToolComponent(block.tool_execution)"
                                                    :is="getToolComponent(block.tool_execution.tool_name)"
                                                    :key="`${block.id}:${block.tool_execution.id || 'noid'}`"
                                                    :tool-execution="block.tool_execution"
                                                    :initially-collapsed="!!runFold(m)"
                                                    :readonly="true"
                                                />
                                                <!-- Fallback to generic expandable tool display -->
                                                <div v-else>
                                                    <!-- Every unmapped tool names itself. 'clarify' and
                                                         'suggest_instructions' used to be excluded here, which
                                                         (they had no component on this page either) left the
                                                         step rendering nothing at all — a bare avatar with an
                                                         empty body beside it. -->
                                                    <div class="text-xs text-gray-500 dark:text-gray-400 mb-1">
                                                        <span class="cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="toggleToolDetails(block.tool_execution.id)">
                                                            {{ block.tool_execution.tool_name }}{{ toolActionLabel(block.tool_execution) }} ({{ block.tool_execution.status }})
                                                        </span>
                                                        <div v-if="isToolDetailsExpanded(block.tool_execution.id)" class="ms-2 mt-1 text-xs text-ink-muted bg-gray-50 dark:bg-gray-900 p-2 rounded">
                                                            <div v-if="block.tool_execution.result_summary">{{ block.tool_execution.result_summary }}</div>
                                                            <div v-if="block.tool_execution.duration_ms">{{ responseT('reportView.duration', { ms: block.tool_execution.duration_ms }) }}</div>
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>

                                            <!-- Tool widget preview (readonly) -->
                                            <div class="tool-preview mt-1 text-start" :dir="responseDir" v-if="shouldShowToolWidgetPreview(block.tool_execution) && block.tool_execution && !runFold(m)">
                                                <ToolWidgetPreview :tool-execution="block.tool_execution" :readonly="true" />
                                            </div>

                                            <!-- 4. Final answer -->
                                            <div v-if="block.plan_decision?.analysis_complete && (block.plan_decision?.final_answer || (!block.content && !block.tool_execution))" class="mt-2 markdown-wrapper text-ink dark:text-gray-100" :dir="responseDir" :data-response-dir="responseDir">
                                                <MarkdownRender
                                                    :content="citedMarkdown(m, block.plan_decision?.final_answer || block.plan_decision?.assistant || block.content || '')" :custom-html-tags="FIGURE_TAGS"
                                                    :final="true"
                                                    :typewriter="false"
                                                    :render-code-blocks-as-pre="true"
                                                    class="markdown-content"
                                                />
                                            </div>
                                            </div>
                                        </div>
                                        </template>

                                        <!-- Steering messages whose step never ran (e.g. the run stopped): after the last step -->
                                        <div v-for="s in steersAfterLastBlock(m)" :key="'steer-tail-' + s.id" class="flex justify-end my-2" :data-message-id="s.id">
                                            <div class="flex flex-col items-end max-w-[85%]">
                                                <div class="flex items-center gap-1 mb-0.5 text-chat-3" :class="isSteerApplied(s) ? 'text-green-600 dark:text-green-400' : 'text-amber-500'" data-testid="steering-badge">
                                                    <Icon :name="isSteerApplied(s) ? 'heroicons-check-circle-solid' : 'heroicons-bolt-solid'" class="w-3 h-3" />
                                                    <span>{{ isSteerApplied(s) ? i18nT('reportView.steeringApplied') : i18nT('reportView.steered') }}</span>
                                                </div>
                                                <div class="inline-block rounded-xl px-3 py-2 bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-white text-start border border-amber-200 dark:border-amber-900/60" :dir="promptDir(s.prompt?.content)">
                                                    <div v-if="s.prompt?.content" class="pt-1 markdown-wrapper">
                                                        <InstructionText
                                                            :text="s.prompt.content"
                                                            :references="promptMentionsToRefs(s.prompt.mentions, agentIconTokens)"
                                                            :prose="true"
                                                        />
                                                    </div>
                                                </div>
                                            </div>
                                        </div>

                                        <!-- Outputs of a folded run stay reachable under its conclusion,
                                             whether or not the steps are open (read-only here). -->
                                        <div v-if="runDeliverables(m).length" class="mt-4 flex flex-col gap-3" data-testid="run-deliverables">
                                            <template v-for="d in runDeliverables(m)" :key="'deliverable-' + d.id">
                                                <ToolWidgetPreview
                                                    v-if="shouldShowToolWidgetPreview(d.tool_execution)"
                                                    :tool-execution="d.tool_execution"
                                                    :readonly="true"
                                                />
                                                <component
                                                    v-else-if="shouldUseToolComponent(d.tool_execution)"
                                                    :is="getToolComponent(d.tool_execution.tool_name)"
                                                    :tool-execution="d.tool_execution"
                                                    :readonly="true"
                                                />
                                            </template>
                                        </div>

                                        <!-- What the answer rests on: its cited results, numbered as in
                                             the text, plus any table the run made and did not cite. -->
                                        <RunSourcesRow
                                            :items="runSources(m)"
                                            :translate="t"
                                            @open="(item, el) => citationChips.pin(item.source, item.number, el)"
                                            @preview="(item, el) => citationChips.preview(item.source, item.number, el)"
                                            @leave="citationChips.hideSoon()"
                                        />
                                    </div>

                                    <!-- Knowledge harness: the post-analysis reflection sub-loop
                                         is a phase of its own, not part of the answer. Its blocks
                                         are lifted out of the stream above and rendered as the
                                         same single collapsible card the report view uses —
                                         read-only, since a shared link has no build to review. -->
                                    <KnowledgeGroup
                                        v-if="harnessBlocks(m).length"
                                        :blocks="harnessBlocks(m)"
                                        readonly
                                    />

                                    <!-- Status messages -->
                                    <div v-if="m.status === 'stopped'" class="text-xs text-gray-500 dark:text-gray-400 mt-2 italic">
                                        <Icon name="heroicons-stop-circle" class="w-4 h-4 inline me-1" />
                                        {{ responseT('reportView.generationStopped') }}
                                    </div>
                                    <div v-else-if="m.status === 'error'" class="text-xs text-gray-500 dark:text-gray-400">
                                        <Icon name="heroicons-x-mark" class="w-4 h-4 inline me-1 text-red-500" />
                                        <span class="italic">{{ responseT('reportView.anErrorOccurred') }}</span>
                                    </div>
                                </div>
                            </template>
                        </div>
                    </ResponseLanguageScope>
                </ul>
            </div>

            <!-- Footer -->
            <footer class="flex-none border-t border-gray-100 dark:border-gray-800 py-2 text-center bg-white dark:bg-gray-900 z-10">
                <a href="https://bagofwords.com" target="_blank" class="text-2xs text-ink-muted hover:text-ink-2 transition-colors">
                    {{ $t('sharedConversation.poweredBy', { brand: 'BOW' }) }}
                </a>
            </footer>
        </template>
        <!-- Preview of the cited result under the pointer or keyboard focus.
             A shared link has nowhere to open the full result. -->
        <CitationPreview
            :source="citationChips.active.value?.source || null"
            :anchor="citationChips.active.value?.anchor || null"
            :number="citationChips.active.value?.number"
            :translate="t"
            @enter="citationChips.keep()"
            @leave="citationChips.hideSoon()"
        />
    </div>
</template>

<script setup lang="ts">
import GenericTool from '~/components/tools/GenericTool.vue'
import RunToolGroupRow from '~/components/RunToolGroupRow.vue'
import { ref, computed, onMounted, onUnmounted, nextTick, provide } from 'vue'
import { useI18n } from 'vue-i18n'
import { computeBlockGroups } from '~/composables/useBlockGrouping'
import { placeSteers, isSteerApplied, groupSteersByParent } from '~/composables/useSteerPlacement'
import { blockHasMessage, describeDataOutput, type RunActivity } from '~/composables/useRunActivity'
import { createRunActivityMemo, useRunFold } from '~/composables/useRunFold'
import { citeText, collectSources, FIGURE_TAGS, type CitationSource } from '~/composables/useCitations'
import { FIGURE_CONTEXT } from '~/composables/figureModel'
import { useCitationChips } from '~/composables/useCitationChips'
import type { SourceItem } from '~/components/RunSourcesRow.vue'
import { CHAT_VIZ_TEXT_PX } from '~/utils/chatVizText'
import { useChatDisplay } from '~/composables/useChatDisplay'
import Spinner from '~/components/Spinner.vue'
import CreateWidgetTool from '~/components/tools/CreateWidgetTool.vue'
import CreateDemoDatasetTool from '~/components/tools/CreateDemoDatasetTool.vue'
import ListSchemaTool from '~/components/tools/ListSchemaTool.vue'
import DeleteListRowsTool from '~/components/tools/DeleteListRowsTool.vue'
import CreateDataTool from '~/components/tools/CreateDataTool.vue'
import DescribeTablesTool from '~/components/tools/DescribeTablesTool.vue'
import DescribeEntityTool from '~/components/tools/DescribeEntityTool.vue'
import ReadQueryTool from '~/components/tools/ReadQueryTool.vue'
import RunQueryTool from '~/components/tools/RunQueryTool.vue'
import AddParameterTool from '~/components/tools/AddParameterTool.vue'
import ReadResourcesTool from '~/components/tools/ReadResourcesTool.vue'
import InspectDataTool from '~/components/tools/InspectDataTool.vue'
import ExecuteCodeTool from '~/components/tools/ExecuteCodeTool.vue'
import WebFetchTool from '~/components/tools/WebFetchTool.vue'
import BrowserTool from '~/components/tools/BrowserTool.vue'
import BrowserVisionTool from '~/components/tools/BrowserVisionTool.vue'
import WebSearchTool from '~/components/tools/WebSearchTool.vue'
import SearchFilesTool from '~/components/tools/SearchFilesTool.vue'
import GrepFilesTool from '~/components/tools/GrepFilesTool.vue'
import ListFilesTool from '~/components/tools/ListFilesTool.vue'
import ReadFileTool from '~/components/tools/ReadFileTool.vue'
import GenerateImageTool from '~/components/tools/GenerateImageTool.vue'
import AttachFileTool from '~/components/tools/AttachFileTool.vue'
import CreateArtifactTool from '~/components/tools/CreateArtifactTool.vue'
import ManageArtifactResourcesTool from '~/components/tools/ManageArtifactResourcesTool.vue'
import EditArtifactTool from '~/components/tools/EditArtifactTool.vue'
import ReadArtifactTool from '~/components/tools/ReadArtifactTool.vue'
import ReadArtifactActivityTool from '~/components/tools/ReadArtifactActivityTool.vue'
import CreateDocTool from '~/components/tools/CreateDocTool.vue'
import EditDocTool from '~/components/tools/EditDocTool.vue'
import WriteCsvTool from '~/components/tools/WriteCsvTool.vue'
import WriteToExcelTool from '~/components/tools/WriteToExcelTool.vue'
import ReadExcelRangeTool from '~/components/tools/ReadExcelRangeTool.vue'
import ReadExcelAsCsvTool from '~/components/tools/ReadExcelAsCsvTool.vue'
import WriteOfficeJsCodeTool from '~/components/tools/WriteOfficeJsCodeTool.vue'
import MCPTool from '~/components/tools/MCPTool.vue'
import SearchReportsTool from '~/components/tools/SearchReportsTool.vue'
import ReadReportTool from '~/components/tools/ReadReportTool.vue'
import SearchInstructionsTool from '~/components/tools/SearchInstructionsTool.vue'
import SearchAgentsTool from '~/components/tools/SearchAgentsTool.vue'
import SetReportAgentsTool from '~/components/tools/SetReportAgentsTool.vue'
import ReadInstructionTool from '~/components/tools/ReadInstructionTool.vue'
import CreateNoteTool from '~/components/tools/CreateNoteTool.vue'
import SubmitListTool from '~/components/tools/SubmitListTool.vue'
import EditNoteTool from '~/components/tools/EditNoteTool.vue'
import RouteModelTool from '~/components/tools/RouteModelTool.vue'
import CreateInstructionTool from '~/components/tools/CreateInstructionTool.vue'
// Knowledge / training harness: instruction edits, eval suites and their runs.
// A training conversation is almost entirely these blocks, and every one of
// them used to fall through to the generic "tool_name (status)" debug line.
import EditInstructionTool from '~/components/tools/EditInstructionTool.vue'
import SearchEvalsTool from '~/components/tools/SearchEvalsTool.vue'
import CreateEvalTool from '~/components/tools/CreateEvalTool.vue'
import EditEvalTool from '~/components/tools/EditEvalTool.vue'
import GetEvalRunsTool from '~/components/tools/GetEvalRunsTool.vue'
import StopEvalRunTool from '~/components/tools/StopEvalRunTool.vue'
import RunEvalTool from '~/components/tools/RunEvalTool.vue'
import GetEvalRunTool from '~/components/tools/GetEvalRunTool.vue'
import ClarifyTool from '~/components/tools/ClarifyTool.vue'
import WaitTool from '~/components/tools/WaitTool.vue'
import DelegateTool from '~/components/tools/DelegateTool.vue'
import ReadSubAgentTool from '~/components/tools/ReadSubAgentTool.vue'
import MessageSubAgentTool from '~/components/tools/MessageSubAgentTool.vue'
import StopSubAgentTool from '~/components/tools/StopSubAgentTool.vue'
import MemoryTool from '~/components/tools/MemoryTool.vue'
// Agent actions and bookkeeping — same treatment, same reason.
import CreateDashboardTool from '~/components/tools/CreateDashboardTool.vue'
import SendEmailTool from '~/components/tools/SendEmailTool.vue'
import NotifyTool from '~/components/tools/NotifyTool.vue'
import CreateScheduledTaskTool from '~/components/tools/CreateScheduledTaskTool.vue'
import EditScheduledTaskTool from '~/components/tools/EditScheduledTaskTool.vue'
import CancelScheduledTaskTool from '~/components/tools/CancelScheduledTaskTool.vue'
import CancelWaitTool from '~/components/tools/CancelWaitTool.vue'
import CreatePromptTool from '~/components/tools/CreatePromptTool.vue'
import EditPromptTool from '~/components/tools/EditPromptTool.vue'
import SearchPromptsTool from '~/components/tools/SearchPromptsTool.vue'
import ListConnectionsTool from '~/components/tools/ListConnectionsTool.vue'
import GetConnectionTool from '~/components/tools/GetConnectionTool.vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import InstructionText from '~/components/instructions/InstructionText.vue'
import { dominantTextDir, localeTextDir, type TextDir } from '~/utils/textDirection'
import ResponseLanguageScope from '~/components/report/ResponseLanguageScope.vue'
import PolishRequestCard from '~/components/report/PolishRequestCard.vue'
import { parsePolishPrompt } from '~/utils/polishPrompt'
import { promptMentionsToRefs } from '~/utils/mentions'

// A shared conversation never names the org's agents, but the tool cards do
// carry each referenced agent's resolved icon. Reuse it so an @agent chip draws
// the same icon as the tool card beneath it, rather than the one snapshotted
// into the prompt when the message was sent.
const agentIconTokens = computed<Record<string, string | null | undefined>>(() => {
    const out: Record<string, string | null | undefined> = {}
    for (const c of (conversation.value?.completions || []) as any[]) {
        for (const b of c.completion_blocks || []) {
            for (const ds of b.tool_execution?.data_sources || []) {
                if (ds?.id && ds.icon_token) out[ds.id] = ds.icon_token
            }
        }
    }
    return out
})
// Same markdown pipeline as the report view. MDC (stock, no `mdc` config in
// nuxt.config) renders a ```mermaid / ```d2 / ```infographic fence as a plain
// code block and leaves $…$ math as literal text, so a shared answer degraded
// to raw diagram source while the same diagram written into an instruction
// card — which uses InstructionText — rendered fine on this very page.
// markstream dispatches on fence language before the render-code-blocks-as-pre
// flag, so diagrams and math survive it. useMarkdownAutoDir (started app-wide by
// plugins/markdown-auto-dir.client.ts) and the [dir=rtl] .markstream-vue rules in assets/css/rtl.css were
// both written against this renderer's DOM.
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'

// Charts and grids in this conversation follow the chat text size
// (utils/chatVizText.ts).
provide(CHAT_VIZ_TEXT_PX, useChatDisplay().textSizePx)

const route = useRoute()
const token = route.params.token as string

const isLoading = ref(true)
const error = ref(false)
const accessError = ref<'login' | 'denied' | null>(null)
const conversation = ref<any>({
    title: '',
    user_name: '',
    completions: [],
    created_at: null,
})

// ---------------------------------------------------------------------------
// What this page can actually render: a user prompt, or an assistant turn with
// blocks (or a stopped/error notice). Anything else — a silent session event
// (role='event'), a machine trigger entry (role='external'), an assistant row
// whose blocks were all sanitized away — has no body in the public payload and
// would otherwise draw a bare avatar with nothing beside it. The backend now
// filters these out of /api/c/{token} so they don't eat pagination slots
// either; this guard keeps a stale payload or a future role from putting empty
// bubbles back on the page.
// ---------------------------------------------------------------------------
const visibleCompletions = computed<any[]>(() => {
    const rows = (conversation.value?.completions || []).filter((m: any) => {
        // A steer renders inside its parent turn's step stream (see
        // steersBeforeBlock); it stays a standalone bubble only when that turn
        // has no steps to sit between or is not on this page.
        if (m?.role === 'user') return !inlineSteerIds.value.has(String(m.id))
        if (m?.role !== 'system') return false
        return (m.completion_blocks?.length || 0) > 0 || m.status === 'stopped' || m.status === 'error'
    })
    // A standalone steer whose turn is on the page goes right above that turn
    // (it was sent into it), as on the report view — not after it by send time.
    const shown = new Set(rows.map((m: any) => String(m.id)))
    const hoisted = new Map<string, any[]>()
    const isHoisted = (m: any) => m?.message_type === 'steering' && m.parent_id && shown.has(String(m.parent_id))
    for (const m of rows) {
        if (!isHoisted(m)) continue
        const list = hoisted.get(String(m.parent_id))
        list ? list.push(m) : hoisted.set(String(m.parent_id), [m])
    }
    const out: any[] = []
    for (const m of rows) {
        if (isHoisted(m)) continue
        if (m.role === 'system') out.push(...(hoisted.get(String(m.id)) || []))
        out.push(m)
    }
    return out
})

// ---------------------------------------------------------------------------
// Steering messages ("Send now" while the agent ran). Same placement as the
// report view (useSteerPlacement.ts): right above the step that picked the
// steer up, or by send time when it was never picked up.
// ---------------------------------------------------------------------------
const steersByParent = computed(() => groupSteersByParent(conversation.value?.completions || []))

const steerPlacements = computed(() => {
    const out = new Map<string, ReturnType<typeof placeSteers<any>>>()
    for (const c of conversation.value?.completions || []) {
        if (c?.role !== 'system') continue
        const steers = steersByParent.value.get(String(c.id))
        if (steers?.length) out.set(String(c.id), placeSteers(steers, timelineBlocks(c)))
    }
    return out
})

const inlineSteerIds = computed(() => {
    const ids = new Set<string>()
    for (const { before, tail } of steerPlacements.value.values()) {
        for (const list of before.values()) for (const s of list) ids.add(String(s.id))
        for (const s of tail) ids.add(String(s.id))
    }
    return ids
})

function steersBeforeBlock(m: any, blockIndex: number): any[] {
    return steerPlacements.value.get(String(m.id))?.before.get(blockIndex) || []
}

function steersAfterLastBlock(m: any): any[] {
    return steerPlacements.value.get(String(m.id))?.tail || []
}

// Prompt bubbles take the direction of their own words (a nested
// `dir="auto"` pair resolves LTR — see docs/design/i18n.md "Content
// direction"); each reply's question direction is the prior its message
// direction starts from (ResponseLanguageScope).
function promptDir(text?: string | null): TextDir | 'auto' {
    return dominantTextDir((text || '').replace(/@\S+/g, ' ')) || 'auto'
}
const { t, t: i18nT, locale: uiLocale } = useI18n({ useScope: 'global' })

// This page renders with `layout: false`, so layouts/default.vue — where the
// org locale is applied — never runs here, and an anonymous viewer has no
// session to ask for it. /api/c/{token} returns the sharing org's locale
// instead. A personal choice (`bow.locale`) still wins. Applied for this page
// view only: going through $setLocale would persist the org's language as the
// viewer's own choice.
async function applySharedLocale(code?: string | null) {
    if (!code || code === uiLocale.value) return
    let personal: string | null = null
    try { personal = localStorage.getItem('bow.locale') } catch {}
    if (personal) return
    const { $loadLocaleMessages } = useNuxtApp() as any
    if (typeof $loadLocaleMessages !== 'function' || !(await $loadLocaleMessages(code))) return
    uiLocale.value = code
    document.documentElement.setAttribute('lang', code)
    document.documentElement.setAttribute('dir', localeTextDir(code))
}
const replyDirHints = computed(() => {
    const hints = new Map<string, TextDir>()
    let last: TextDir | null = null
    for (const m of visibleCompletions.value) {
        if (m.role === 'user') {
            const d = dominantTextDir((m.prompt?.content || '').replace(/@\S+/g, ' '))
            if (d) last = d
        } else if (last) {
            hints.set(String(m.id), last)
        }
    }
    return hints
})

// One direction per reply comes from ResponseLanguageScope (the same
// component the report page wraps each message in): its saved response
// language, else its question's direction (replyDirHints), unless its prose is
// plainly the other script. The reply's row, markdown blocks, tool cards and
// widget previews all take it, so a reply split by tool calls never ends up
// half LTR; its labels follow the reply's language through `responseT`.

// ---------------------------------------------------------------------------
// The knowledge harness (the post-analysis reflection sub-loop) runs as its own
// phase and its blocks are tagged `phase === 'knowledge_harness'`. The report
// view lifts them out of the step stream into one Knowledge card; this page
// used to have neither the tag nor the split, so a harness run's
// search/create/edit steps were interleaved into the answer as loose rows.
// Same split here, from the same field.
// ---------------------------------------------------------------------------
const HARNESS_PHASE = 'knowledge_harness'

// Calls made in parallel share one planner decision, and this payload repeats
// that decision's progress message on every one of them. Said once, it is
// shown once: later copies are blanked so the line is not printed per call
// and so the calls can sit together under one step of a folded run.
const _timelineCache = new Map<string, { key: string; blocks: any[] }>()

function timelineBlocks(m: any): any[] {
    const raw = (m?.completion_blocks || []).filter((b: any) => b?.phase !== HARNESS_PHASE)
    const key = raw.map((b: any) => `${b.id}:${b.status}:${b.tool_execution?.status}:${(b.content || '').length}:${(b.plan_decision?.assistant || '').length}`).join(',')
    const hit = _timelineCache.get(String(m?.id))
    if (hit && hit.key === key) return hit.blocks
    let said = ''
    const blocks = raw.map((b: any) => {
        const pd = b.plan_decision
        const own = (b.content || '').trim()
        const shared = (pd?.assistant || '').trim()
        if (!own && shared && !pd?.final_answer && shared === said) {
            return { ...b, plan_decision: { ...pd, assistant: '' } }
        }
        said = own || shared
        return b
    })
    _timelineCache.set(String(m?.id), { key, blocks })
    return blocks
}

function harnessBlocks(m: any): any[] {
    return (m?.completion_blocks || []).filter((b: any) => b?.phase === HARNESS_PHASE)
}

// Pagination state
const hasMore = ref(false)
const nextBefore = ref<string | null>(null)
const isLoadingMore = ref(false)
const messagesContainer = ref<HTMLElement | null>(null)

// ---------------------------------------------------------------------------
// Grouping of consecutive low-signal tool blocks (policy in useBlockGrouping).
// The share view is a finished, read-only stream — grouping is computed per
// completion on demand and memoized by completion id + block count.
// ---------------------------------------------------------------------------
const expandedGroups = ref<Set<string>>(new Set())
const _groupingCache = new Map<string, { key: string; grouping: ReturnType<typeof computeBlockGroups> }>()

function toggleGroup(groupId: string) {
    const next = new Set(expandedGroups.value)
    next.has(groupId) ? next.delete(groupId) : next.add(groupId)
    expandedGroups.value = next
}

function isGroupExpanded(groupId: string): boolean {
    return expandedGroups.value.has(groupId)
}

function _groupingFor(m: any) {
    // Group over the blocks the timeline actually renders: a harness block
    // sitting between two chip-class steps would otherwise break a run that
    // reads as continuous on screen.
    const blocks = timelineBlocks(m)
    const steerSlots = steerPlacements.value.get(String(m.id))?.before
    const key = `${m.status}:${blocks.map(b => `${b.id}:${b.status}:${b.tool_execution?.status}`).join(",")}:${[...(steerSlots?.keys() || [])].join(",")}`
    const hit = _groupingCache.get(String(m.id))
    if (hit && hit.key === key) return hit.grouping
    // A steering bubble above a block forces a group boundary so it never
    // lands inside a folded run (same rule as the report view).
    // A run folded around its final message groups its calls into steps
    // instead (see runFold below) — one grammar per run, never both.
    const grouping = _runActivityFor(m)
        ? { groupOf: {}, headerAt: {} }
        : computeBlockGroups(blocks, {
            executionStatus: m.status,
            breakBefore: (b: any) => !!steerSlots?.has(blocks.indexOf(b)),
        })
    _groupingCache.set(String(m.id), { key, grouping })
    return grouping
}

function groupHeaderFor(m: any, block: any) {
    return _groupingFor(m)?.headerAt[String(block.id)]
}

function isBlockFolded(m: any, block: any): boolean {
    const g = _groupingFor(m)?.groupOf[String(block.id)]
    return !!g && !expandedGroups.value.has(g.id)
}

// Label/ticker rendering lives in BlockGroupTicker.vue.

// ---------------------------------------------------------------------------
// Run fold — the same policy as the report view (composables/useRunActivity):
// a finished run shows its conclusion, with the activity before it behind one
// "Worked for 10s · View steps" row. Memoized like the grouping above. A
// shared link always opens collapsed.
// ---------------------------------------------------------------------------
const runActivityMemo = createRunActivityMemo()
function _runActivityFor(m: any): RunActivity | null {
    return runActivityMemo(String(m.id), timelineBlocks(m), { runStatus: m.status })
}
const {
    toolGroupHeaderFor,
    isGroupedTool,
    isToolGroupExpanded,
    toggleToolGroup,
    runFold,
    isRunExpanded,
    toggleRun,
    isRunAnswerBlock,
    isRunBlockShown,
    isRunActivityBlock,
    isRunReasoningShown,
    isRunToolShown,
    runDeliverables,
} = useRunFold<any>({
    foldFor: m => _runActivityFor(m),
    blocksFor: m => timelineBlocks(m),
    keyFor: m => String(m.id),
})
// ---------------------------------------------------------------------------
// Citations — same grammar and chips as the report view (useCitations). A
// shared link has no panel to open a result in, so a chip previews on hover
// or focus and a click pins that preview.
// ---------------------------------------------------------------------------
const citationSources = computed<CitationSource[]>(() =>
    collectSources(
        visibleCompletions.value.flatMap((m: any) => (m.role === 'user' ? [] : timelineBlocks(m))),
        describeDataOutput,
    )
)
// Per message, the sources it cites in order of first mention across its blocks.
const messageCited = computed(() => {
    const out = new Map<string, CitationSource[]>()
    for (const m of visibleCompletions.value) {
        if (m.role === 'user') continue
        const cited: CitationSource[] = []
        for (const b of timelineBlocks(m)) {
            const raw = b.content || b.plan_decision?.final_answer || b.plan_decision?.assistant || ''
            if (raw.includes('{')) citeText(raw, citationSources.value, cited, { figures: true })
        }
        if (cited.length) out.set(String(m.id), cited)
    }
    return out
})
// Figures in a shared answer draw from the rows the link carries (a viewer
// here has no session to load more) and have no side panel to open.
provide(FIGURE_CONTEXT, {
    resolve: (key: string) => citationSources.value.find(s => s.key === key) || null,
    canHydrate: false,
})
function citedMarkdown(m: any, raw: string): string {
    if (!raw || !raw.includes('{')) return raw
    // The message's numbering is fixed above; this only looks numbers up.
    return citeText(raw, citationSources.value, [...(messageCited.value.get(String(m.id)) || [])], { figures: true }).markdown
}
function runSources(m: any): SourceItem[] {
    const cited = messageCited.value.get(String(m.id)) || []
    const items: SourceItem[] = cited.map((source, i) => ({ number: i + 1, source }))
    if (!runFold(m)) return items
    const own = new Set(timelineBlocks(m).map((b: any) => String(b.id)))
    for (const source of citationSources.value) {
        if (source.output.kind !== 'number' && own.has(source.key) && !cited.includes(source)) {
            items.push({ number: null, source })
        }
    }
    return items
}
const citationChips = useCitationChips<any>({
    resolve: (m, key) => {
        const source = citationSources.value.find(s => s.key === key)
        if (!source) return null
        const at = (messageCited.value.get(String(m.id)) || []).indexOf(source)
        return { source, number: at >= 0 ? at + 1 : null }
    },
})

// The server's run time when the payload carries it, else the span of the
// run's own blocks (first activity -> final message).
function runFoldDuration(m: any): string {
    const ms = (typeof m.total_duration_ms === 'number' && m.total_duration_ms > 0)
        ? m.total_duration_ms
        : runFold(m)?.spanMs
    if (typeof ms !== 'number' || ms < 0) return ''
    const s = Math.max(1, Math.round(ms / 1000))
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

// Fork state
const forkEligibility = ref<any>(null)
const isForking = ref(false)

async function handleFork() {
    if (isForking.value) return
    const reportId = conversation.value?.report_id
    if (!reportId) return
    isForking.value = true
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/reports/${reportId}/fork`, {
            method: 'POST',
            body: {},
        })
        if (data.value && !fetchError.value) {
            navigateTo(`/reports/${(data.value as any).id}`)
        }
    } catch (e) {
        console.error('Failed to fork report:', e)
    } finally {
        isForking.value = false
    }
}

// Collapsed reasoning tracking
const collapsedReasoning = ref<Set<string>>(new Set())
const expandedToolDetails = ref<Set<string>>(new Set())

definePageMeta({
    layout: false,
    auth: false,
})

// Render stored-UTC timestamps in the org timezone (falls back to the viewer's
// browser timezone when org settings aren't loaded, e.g. anonymous share view).
const _fd = useFormatDate()
function formatDate(dateString: string | null): string {
    if (!dateString) return ''
    return _fd.formatDate(dateString)
}
function formatDateTime(dateString: string | null): string {
    if (!dateString) return ''
    return _fd.formatDateTime(dateString)
}

function toggleReasoning(blockId: string) {
    if (collapsedReasoning.value.has(blockId)) {
        collapsedReasoning.value.delete(blockId)
    } else {
        collapsedReasoning.value.add(blockId)
    }
}

function isReasoningCollapsed(blockId: string): boolean {
    return collapsedReasoning.value.has(blockId)
}

function toggleToolDetails(toolId: string) {
    if (expandedToolDetails.value.has(toolId)) {
        expandedToolDetails.value.delete(toolId)
    } else {
        expandedToolDetails.value.add(toolId)
    }
}

function isToolDetailsExpanded(toolId: string): boolean {
    return expandedToolDetails.value.has(toolId)
}

function hasCompletedContent(block: any): boolean {
    return !!(block.content || block.tool_execution || block.status === 'completed' || block.status === 'stopped' || block.plan_decision?.analysis_complete || block.plan_decision?.final_answer)
}

// `t` is the reply's own translator (ResponseLanguageScope), so the label
// reads in the reply's language like the rest of its row.
function getThoughtProcessLabel(block: any, t: (key: string, values?: any) => string): string {
    if (block.status === 'stopped') return t('reportView.thoughtProcess')

    // Measured reasoning: the planner's plus the tool's code generation, which
    // streams into this same block.
    const pm = block.plan_decision?.metrics || block.plan_decision?.metrics_json
    const measured = [pm?.thinking_ms, block.tool_execution?.sub_timings_json?.codegen_reasoning_ms]
        .filter((ms: any) => typeof ms === 'number' && isFinite(ms) && ms >= 0)
    if (measured.length) {
        return t('reportView.thoughtForSeconds', { seconds: Math.round(measured.reduce((a: number, b: number) => a + b, 0) / 1000) })
    }

    if (block.started_at && block.completed_at) {
        const startTime = new Date(block.started_at).getTime()
        const endTime = new Date(block.completed_at).getTime()
        const durationMs = endTime - startTime
        const durationSeconds = Math.round(durationMs / 1000)
        if (durationSeconds > 1800) return t('reportView.stopped')
        return t('reportView.thoughtForSeconds', { seconds: durationSeconds })
    }

    // The tool's own duration is not reasoning time; it already shows on the tool row.
    return t('reportView.thoughtProcess')
}

function getToolComponent(toolName: string) {
    // Native per-list tools (submit_<list>) stream under their own name
    // before the gateway rewrite to submit_list.
    if (toolName?.startsWith('submit_')) return SubmitListTool
    // Native MCP tools (mcp__<connection>__<tool>) and call_tool stream under
    // their own name before the rewrite to execute_mcp.
    if (toolName?.startsWith('mcp__')) return MCPTool
    switch (toolName) {
        case 'create_widget':
            return CreateWidgetTool
        case 'create_data':
            return CreateDataTool
        case 'describe_tables':
            return DescribeTablesTool
        case 'describe_entity':
            return DescribeEntityTool
        case 'read_query':
            return ReadQueryTool
        case 'run_query':
            return RunQueryTool
        case 'add_parameter':
            return AddParameterTool
        case 'read_resources':
            return ReadResourcesTool
        case 'inspect_data':
            return InspectDataTool
        case 'generate_image':
            return GenerateImageTool
        case 'search_files':
        case 'search_email':
        case 'search_chat_messages':
        case 'search_notes':
            return SearchFilesTool
        case 'grep_files':
        case 'grep_notes':
            return GrepFilesTool
        case 'list_files':
        case 'list_emails':
        case 'list_chat_messages':
        case 'list_notes':
            return ListFilesTool
        case 'read_file':
        case 'read_email':
        case 'read_chat_thread':
        case 'read_note':
            return ReadFileTool
        case 'attach_file':
            return AttachFileTool
        case 'manage_artifact_resources':
            return ManageArtifactResourcesTool
        case 'create_artifact':
            return CreateArtifactTool
        case 'edit_artifact':
            return EditArtifactTool
        case 'read_artifact':
            return ReadArtifactTool
        case 'read_artifact_activity':
            return ReadArtifactActivityTool
        case 'create_doc':
            return CreateDocTool
        case 'edit_doc':
            return EditDocTool
        case 'write_csv':
            return WriteCsvTool
        case 'write_to_excel':
            return WriteToExcelTool
        case 'read_excel_range':
            return ReadExcelRangeTool
        case 'read_excel_as_csv':
            return ReadExcelAsCsvTool
        case 'write_officejs_code':
            return WriteOfficeJsCodeTool
        case 'search_mcps':
        case 'search_tools':
        case 'call_tool':
        case 'execute_mcp':
            return MCPTool
        case 'search_reports':
            return SearchReportsTool
        case 'read_report':
            return ReadReportTool
        case 'execute_code':
        case 'execute_sql':
        case 'create_and_execute_code':
            return ExecuteCodeTool
        case 'web_fetch':
            return WebFetchTool
        case 'web_search':
            return WebSearchTool
        case 'browser_navigate':
        case 'browser_snapshot':
        case 'browser_extract':
        case 'browser_act':
            return BrowserTool
        case 'browser_vision':
            return BrowserVisionTool
        case 'search_instructions':
            return SearchInstructionsTool
        case 'read_instruction':
            return ReadInstructionTool
        case 'search_agents':
            return SearchAgentsTool
        case 'set_report_agents':
            return SetReportAgentsTool
        case 'create_note':
            return CreateNoteTool
        case 'submit_list':
            return SubmitListTool
        case 'edit_note':
            return EditNoteTool
        case 'route_model':
            return RouteModelTool
        case 'create_instruction':
            return CreateInstructionTool
        case 'edit_instruction':
            return EditInstructionTool
        case 'search_evals':
            return SearchEvalsTool
        case 'create_eval':
            return CreateEvalTool
        case 'edit_eval':
            return EditEvalTool
        case 'get_eval_runs':
            return GetEvalRunsTool
        case 'stop_eval_run':
            return StopEvalRunTool
        case 'run_eval':
            return RunEvalTool
        case 'get_eval_run':
            return GetEvalRunTool
        // A clarifying question and the answer it got are conversation, not
        // machinery — both persist on the tool call, so the exchange reads in a
        // transcript exactly as it happened.
        case 'clarify':
            return ClarifyTool
        case 'delegate':
            return DelegateTool
        case 'message_sub_agent': return MessageSubAgentTool
        case 'read_sub_agent':
            return ReadSubAgentTool
        case 'stop_sub_agent':
            return StopSubAgentTool
        case 'wait':
            return WaitTool
        case 'create_memory':
        case 'edit_memory':
        case 'search_memory':
            return MemoryTool
        case 'create_dashboard':
            return CreateDashboardTool
        case 'send_email':
            return SendEmailTool
        case 'notify':
            return NotifyTool
        case 'create_scheduled_task':
            return CreateScheduledTaskTool
        case 'edit_scheduled_task':
            return EditScheduledTaskTool
        case 'cancel_scheduled_task':
            return CancelScheduledTaskTool
        case 'cancel_wait':
            return CancelWaitTool
        case 'create_prompt':
            return CreatePromptTool
        case 'edit_prompt':
            return EditPromptTool
        case 'search_prompts':
            return SearchPromptsTool
        case 'list_connections':
            return ListConnectionsTool
        case 'get_connection':
            return GetConnectionTool
        // Renders only what the run recorded (the approved spec, row counts and
        // the agents' names/icons) — nothing fetched live — and in readonly
        // mode drops the review controls and the links into the workspace.
        case 'create_demo_dataset':
            return CreateDemoDatasetTool
        // Same contract: the proposed/saved schema from the run's own record,
        // review controls and the workspace link dropped in readonly mode.
        case 'create_list':
        case 'edit_list':
            return ListSchemaTool
        case 'delete_list_rows':
            return DeleteListRowsTool
        // Deliberately NOT mapped here — these two stay owner-view-only, and
        // not merely for effort. suggest_instructions is a review queue over
        // UNREVIEWED draft instructions, and create_agent builds its body by
        // fetching the agent's live connections and files; rendering either on
        // a public link would publish workspace state the sharer never chose to
        // share. The fallback below names them instead of drawing nothing.
        default:
            return GenericTool
    }
}

function shouldUseToolComponent(toolExecution: any): boolean {
    return getToolComponent(toolExecution?.tool_name) !== null
}

// `tool_action` is a discriminator that most tools leave at the placeholder
// "tool_call", which rendered as the meaningless "run_eval → tool_call". Only
// show it when it actually says something the tool name doesn't.
function toolActionLabel(toolExecution: any): string {
    const action = toolExecution?.tool_action
    if (!action || action === 'tool_call' || action === toolExecution?.tool_name) return ''
    return ` → ${action}`
}

function shouldShowToolWidgetPreview(toolExecution: any): boolean {
    if (!toolExecution) return false
    const showForTools = ['create_and_execute_code', 'execute_code', 'execute_sql']
    return showForTools.includes(toolExecution.tool_name) &&
           toolExecution.status === 'success' &&
           (toolExecution.created_widget || toolExecution.created_step)
}

async function loadConversation() {
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/c/${token}?limit=10`)

        if (fetchError.value) {
            const status = (fetchError.value as any)?.statusCode || (fetchError.value as any)?.status
            if (status === 401) { accessError.value = 'login'; return }
            if (status === 403) { accessError.value = 'denied'; return }
            error.value = true
            return
        }
        if (!data.value) {
            error.value = true
            return
        }

        await applySharedLocale((data.value as any).locale)
        conversation.value = data.value
        hasMore.value = (data.value as any).has_more || false
        nextBefore.value = (data.value as any).next_before || null
        forkEligibility.value = (data.value as any).fork_eligibility || null

        // Auto-collapse reasoning for blocks that have content
        for (const completion of conversation.value.completions || []) {
            for (const block of completion.completion_blocks || []) {
                if (block.content || block.tool_execution || block.plan_decision?.final_answer) {
                    collapsedReasoning.value.add(block.id)
                }
            }
        }
    } catch (e) {
        console.error('Failed to load conversation:', e)
        error.value = true
    } finally {
        isLoading.value = false
        scheduleLiveRefresh()

        // Scroll to bottom after DOM renders (must be after isLoading = false)
        await nextTick()
        // Use a slightly longer timeout and requestAnimationFrame to ensure layout is stable
        setTimeout(() => {
            requestAnimationFrame(() => {
                scrollToBottom()
            })
        }, 100)
    }
}

// A shared run that is still going (e.g. waiting on its owner's review, or
// generating) refreshes until it settles, so the viewer sees it finish instead
// of a frozen spinner. Only the newest page is refetched; older pages the
// viewer scrolled in stay as they are.
const LIVE_REFRESH_MS = 4000
let liveRefreshTimer: ReturnType<typeof setTimeout> | null = null

function isRunInProgress(): boolean {
    for (const c of conversation.value?.completions || []) {
        if (c.status === 'in_progress') return true
        for (const b of c.completion_blocks || []) {
            if (['in_progress', 'running'].includes(b.tool_execution?.status)) return true
        }
    }
    return false
}

function scheduleLiveRefresh() {
    if (liveRefreshTimer) clearTimeout(liveRefreshTimer)
    liveRefreshTimer = null
    if (!isRunInProgress()) return
    liveRefreshTimer = setTimeout(refreshLatest, LIVE_REFRESH_MS)
}

async function refreshLatest() {
    try {
        const { data, error: fetchError } = await useMyFetch(`/api/c/${token}?limit=10`)
        if (fetchError.value || !data.value) return
        const latest: any[] = (data.value as any).completions || []
        const latestIds = new Set(latest.map((c: any) => c.id))
        const older = (conversation.value?.completions || []).filter((c: any) => !latestIds.has(c.id))
        conversation.value = { ...conversation.value, completions: [...older, ...latest] }
        for (const completion of latest) {
            for (const block of completion.completion_blocks || []) {
                if (block.content || block.tool_execution || block.plan_decision?.final_answer) {
                    collapsedReasoning.value.add(block.id)
                }
            }
        }
    } catch (e) {
        console.error('Failed to refresh conversation:', e)
    } finally {
        scheduleLiveRefresh()
    }
}

function scrollToBottom() {
    const container = messagesContainer.value
    if (container) {
        container.scrollTop = container.scrollHeight
    }
}

async function loadPreviousCompletions() {
    if (isLoadingMore.value || !hasMore.value || !nextBefore.value) return

    isLoadingMore.value = true

    try {
        const { data, error: fetchError } = await useMyFetch(`/api/c/${token}?limit=10&before=${nextBefore.value}`)

        if (fetchError.value || !data.value) {
            return
        }

        // Get scroll height before prepending
        const container = messagesContainer.value
        const scrollHeightBefore = container?.scrollHeight || 0

        // Prepend older completions
        const olderCompletions = (data.value as any).completions || []
        conversation.value.completions = [...olderCompletions, ...conversation.value.completions]

        hasMore.value = (data.value as any).has_more || false
        nextBefore.value = (data.value as any).next_before || null

        // Auto-collapse reasoning for new blocks
        for (const completion of olderCompletions) {
            for (const block of completion.completion_blocks || []) {
                if (block.content || block.tool_execution || block.plan_decision?.final_answer) {
                    collapsedReasoning.value.add(block.id)
                }
            }
        }

        // Maintain scroll position after prepending
        await nextTick()
        if (container) {
            const scrollHeightAfter = container.scrollHeight
            container.scrollTop = scrollHeightAfter - scrollHeightBefore
        }
    } catch (e) {
        console.error('Failed to load more completions:', e)
    } finally {
        isLoadingMore.value = false
    }
}

function handleScroll(event: Event) {
    const target = event.target as HTMLElement
    // Load more when scrolled near the top (within 100px)
    if (target.scrollTop < 100 && hasMore.value && !isLoadingMore.value) {
        loadPreviousCompletions()
    }
}

onMounted(() => {
    loadConversation()
})

onUnmounted(() => {
    if (liveRefreshTimer) clearTimeout(liveRefreshTimer)
})
</script>

<style scoped>
.run-activity { padding-block: 3px; }
.run-activity.run-group-member:not(:has(.run-step)) { padding-block: 1px; }
.run-activity:has(+ :not(.run-activity)) { margin-block-end: 12px; }

/* Thinking box - collapsible reasoning */
.thinking-box {
    margin-bottom: 4px;
}

.thinking-header {
    display: flex;
    align-items: center;
    cursor: pointer;
    font-size: var(--bow-chat-font-size);
    line-height: var(--bow-chat-line-height);
    font-weight: 400;
    color: rgb(var(--bow-ink-muted-rgb));
    user-select: none;
}

.thinking-header:hover {
    color: rgb(var(--bow-ink-2-rgb));
}

/* Logical properties, not physical: the rule mirrors the report view, whose
   thinking box keeps its dashed gutter on the start edge under RTL. */
.thinking-content {
    padding-block: 4px;
    padding-inline-start: 10px;
    padding-inline-end: 0;
    margin-top: 2px;
    margin-bottom: 4px;
    border-inline-start: 1px dashed rgb(var(--bow-line-rgb));
    font-size: 12px !important;
    line-height: 1.4;
    color: rgb(var(--bow-ink-muted-rgb));
}

.thinking-content :deep(*) {
    font-size: 12px !important;
    line-height: 1.4 !important;
}

.thinking-content :deep(.markdown-content) {
    font-size: 12px !important;
    line-height: 1.4 !important;
}

.thinking-content :deep(p) {
    font-size: 12px !important;
    margin: 0;
}

/* Tool execution container */
.tool-execution-container {
    margin: 8px 0;
}

/* Block content - assistant messages */
.block-content {
    margin-bottom: 4px;
    /* The chat reading size, same token as the report view — a shared link
       that renders two sizes smaller than the conversation it came from is
       the divergence the comment below warns about. */
    font-size: var(--bow-chat-font-size);
    line-height: var(--bow-chat-line-height);
}

/* Markdown styling — kept in step with the same block in the report view, since
   both now render through markstream-vue and a divergence here shows up as the
   shared link laying out differently from the conversation it was shared from.
   The `unicode-bidi` declarations are what make a mixed Hebrew/English answer
   read correctly: each block resolves its own direction, and code stays LTR
   inside an RTL paragraph. (The report view additionally sets `contain` /
   `content-visibility` for streaming; omitted here because this page measures
   scrollHeight to anchor scroll position when prepending older pages, and
   estimated offscreen sizes would make that jump.) */
.markdown-wrapper :deep(.markdown-content) {
    font-size: var(--bow-chat-font-size);
    line-height: var(--bow-chat-line-height);
    --list-item-marker: currentColor;
    --list-item-counter-marker: currentColor;

    /* Code blocks (fenced with ```) — always LTR regardless of surrounding direction */
    pre {
        @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto;
        white-space: pre-wrap;
        word-wrap: break-word;
        direction: ltr;
        unicode-bidi: isolate;
        text-align: left;
    }
    pre code {
        background: none;
        padding: 0;
        border-radius: 0;
        font-size: var(--bow-chat-font-size-2);
        line-height: 1.5;
        display: block;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    /* Inline code (single backticks) */
    code {
        @apply bg-gray-100 px-1.5 py-0.5 rounded font-mono;
        font-size: var(--bow-chat-font-size-2);
        color: #374151;
        unicode-bidi: isolate;
        direction: ltr;
    }
    a {
        @apply text-gray-900 no-underline relative;
        transition: color 0.15s ease;
    }
    a:hover {
        @apply text-gray-700;
    }
    /* Physical l/pl, matching the report view verbatim. Both put a blockquote's
       rule on the left edge under RTL — one pre-existing nit, kept identical
       rather than fixed on one side only. */
    blockquote { @apply border-s-4 border-gray-200 ps-4 italic my-4; unicode-bidi: plaintext; }

    /* useMarkdownAutoDir stamps an explicit dir (dominant-direction estimate)
       on these blocks; once stamped, isolate replaces plaintext so the stamped
       dir governs alignment — plaintext keeps re-resolving from the first
       strong character, which is exactly what mis-aligns an RTL sentence that
       opens with a Latin token (see composables/useMarkdownAutoDir.ts). */
    :where(p, h1, h2, h3, h4, h5, h6, ul, ol, li, blockquote, table, th, td):is([dir="rtl"], [dir="ltr"]) {
        unicode-bidi: isolate;
    }
}

/* Fade transitions */
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}

/* Thinking dots animation */
@keyframes ellipsis {
    0% { content: 'Thinking.'; }
    33% { content: 'Thinking..'; }
    66% { content: 'Thinking...'; }
}

.dots::after {
    content: 'Thinking...';
    display: inline-block;
    background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    animation: shimmer 2s linear infinite, ellipsis 1s infinite;
    font-weight: 400;
    font-size: 12px;
    opacity: 1;
}

@keyframes shimmer {
    0% { background-position: -100% 0; }
    100% { background-position: 100% 0; }
}
</style>
