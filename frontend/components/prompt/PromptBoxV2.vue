<template>
    <div class="flex-shrink-0 p-4 pb-8 bg-white">
        <!-- Query pills + Excel hint (above container) — hidden for now -->
        <div v-if="props.pendingTrainingBuild || (false && (props.queryList.length > 0 || props.scheduledPrompts.length > 0 || (isExcel && excelSelection && !excelSelectionDismissed)))" class="mb-2 flex items-center justify-between">
            <div v-if="props.queryList.length > 0 || props.scheduledPrompts.length > 0 || props.pendingTrainingBuild" class="flex items-center gap-2">
                <!-- Query pill with hover dropdown -->
                <div
                    v-if="props.queryList.length > 0"
                    class="relative"
                    @mouseenter="showQueryDropdown = true"
                    @mouseleave="showQueryDropdown = false"
                >
                    <button
                        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 bg-white text-xs text-gray-600 hover:bg-gray-50 transition-colors"
                    >
                        <Icon name="heroicons-circle-stack" class="w-3.5 h-3.5 text-gray-400" />
                        {{ props.queryList.length }} {{ props.queryList.length === 1 ? $t('prompt.query') : $t('prompt.queries') }}
                    </button>
                    <!-- Query dropdown on hover — pad-bridge eliminates the gap -->
                    <div
                        v-if="showQueryDropdown"
                        class="absolute start-0 bottom-full w-72 z-20"
                    >
                        <div class="bg-white border border-gray-200 rounded-lg shadow-lg py-1 mb-0">
                            <div
                                v-for="(q, i) in props.queryList"
                                :key="i"
                                class="px-3 py-2 hover:bg-gray-50 cursor-pointer"
                                @click="q.messageId && emit('scrollToMessage', q.messageId, q.stepId); showQueryDropdown = false"
                            >
                                <div class="text-xs text-gray-700 truncate">{{ q.label }}</div>
                                <div v-if="q.rowCount != null" class="text-[10px] text-gray-400 mt-0.5">{{ q.rowCount.toLocaleString() }} {{ $t('prompt.rows') }}</div>
                            </div>
                        </div>
                        <!-- Invisible bridge to cover gap between dropdown and pill -->
                        <div class="h-1"></div>
                    </div>
                </div>
                <!-- Scheduled prompts pill with hover dropdown -->
                <div
                    v-if="props.scheduledPrompts.length > 0"
                    class="relative"
                    @mouseenter="showScheduledDropdown = true"
                    @mouseleave="showScheduledDropdown = false"
                >
                    <button
                        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 bg-white text-xs text-gray-600 hover:bg-gray-50 transition-colors"
                    >
                        <Icon name="heroicons-clock" class="w-3.5 h-3.5 text-gray-400" />
                        {{ props.scheduledPrompts.length }} {{ $t('prompt.scheduled') }}
                    </button>
                    <div
                        v-if="showScheduledDropdown"
                        class="absolute start-0 bottom-full w-80 z-20"
                    >
                        <div class="bg-white border border-gray-200 rounded-lg shadow-lg py-1 mb-0">
                            <div
                                v-for="sp in props.scheduledPrompts"
                                :key="sp.id"
                                class="px-3 py-2 hover:bg-gray-50 cursor-pointer flex items-center gap-2"
                                @click.stop="emit('editScheduledPrompt', sp); showScheduledDropdown = false"
                            >
                                <div class="flex-shrink-0">
                                    <div
                                        class="w-2 h-2 rounded-full"
                                        :class="sp.is_active ? 'bg-green-400' : 'bg-gray-300'"
                                    />
                                </div>
                                <div class="flex-1 min-w-0">
                                    <div class="text-xs text-gray-700 truncate" :class="{ 'text-gray-400': !sp.is_active }">{{ sp.prompt?.content || $t('prompt.untitled') }}</div>
                                    <div class="text-[10px] text-gray-400 mt-0.5">{{ getCronLabel(sp.cron_schedule) }}</div>
                                </div>
                            </div>
                        </div>
                        <div class="h-1"></div>
                    </div>
                </div>
                <!-- Training instructions pill with hover dropdown -->
                <div
                    v-if="props.pendingTrainingBuild"
                    class="relative"
                    @mouseenter="showTrainingDropdown = true"
                    @mouseleave="showTrainingDropdown = false"
                >
                    <div class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 bg-white text-xs text-gray-600">
                        <Icon name="heroicons-academic-cap" class="w-3.5 h-3.5 text-gray-400" />
                        {{ props.trainingInstructions.length }} {{ props.trainingInstructions.length === 1 ? $t('prompt.instruction') : $t('prompt.instructionsPlural') }}
                        <span v-if="props.pendingTrainingBuildDiff?.added_lines" class="font-mono text-green-600 ms-1">+{{ props.pendingTrainingBuildDiff.added_lines }}</span>
                        <span v-if="props.pendingTrainingBuildDiff?.removed_lines" class="font-mono text-red-500">-{{ props.pendingTrainingBuildDiff.removed_lines }}</span>
                        <span v-if="props.pendingTrainingBuild" class="text-gray-200">|</span>
                        <button
                            v-if="props.pendingTrainingBuild"
                            class="inline-flex items-center gap-1 text-[11px] text-sky-600 hover:text-sky-700 transition-colors disabled:opacity-60"
                            :disabled="isApprovingBuild || selectedInstructionIds.size === 0"
                            @click.stop="handleApproveTrainingBuild"
                        >
                            <Spinner v-if="isApprovingBuild" class="w-3 h-3 text-sky-600" />
                            {{ isApprovingBuild ? $t('prompt.approving', 'Publishing…') : $t('prompt.saveChanges', 'Save changes') }}
                        </button>
                    </div>
                    <div
                        v-if="showTrainingDropdown"
                        class="absolute start-0 bottom-full w-96 z-20"
                    >
                        <div class="bg-white border border-gray-200 rounded-lg shadow-lg py-2 mb-0">
                            <div class="px-3 pb-1.5 flex items-center gap-1.5 text-[11px] text-gray-500">
                                <span class="font-medium text-gray-700">{{ $t('prompt.pendingChanges', 'Pending changes') }}</span>
                                <span class="text-gray-300">·</span>
                                <span>{{ props.trainingInstructions.length }} {{ props.trainingInstructions.length === 1 ? $t('prompt.changeSingular', 'change') : $t('prompt.changePlural', 'changes') }}</span>
                            </div>
                            <div class="max-h-64 overflow-y-auto">
                                <div
                                    v-for="inst in props.trainingInstructions"
                                    :key="inst.instructionId"
                                    :class="[
                                        'flex items-start gap-2 px-3 py-1.5 mx-1 rounded border border-gray-150 bg-gray-50 hover:bg-gray-100 cursor-pointer mb-1',
                                        !selectedInstructionIds.has(inst.instructionId) ? 'opacity-60' : ''
                                    ]"
                                    @click="emit('editTrainingInstruction', inst); showTrainingDropdown = false"
                                >
                                    <UCheckbox
                                        :model-value="selectedInstructionIds.has(inst.instructionId)"
                                        color="blue"
                                        @update:model-value="toggleInstructionSelection(inst.instructionId, $event)"
                                        @click.stop
                                        class="mt-0.5"
                                    />
                                    <div class="flex-1 min-w-0">
                                        <div class="flex items-center gap-1.5">
                                            <span
                                                :class="[
                                                    'text-[9px] font-mono font-semibold uppercase tracking-wide',
                                                    inst.isEdit ? 'text-blue-600' : 'text-green-600'
                                                ]"
                                            >
                                                {{ inst.isEdit ? $t('prompt.changeEdit', 'edit') : $t('prompt.changeNew', 'new') }}
                                            </span>
                                            <span class="text-[12px] text-gray-700 truncate hover:text-gray-900">{{ inst.title }}</span>
                                            <span v-if="inst.lineCount > 0" class="text-[10px] font-mono text-green-600 shrink-0">+{{ inst.lineCount }}</span>
                                        </div>
                                        <div v-if="inst.category" class="text-[10px] text-gray-400 mt-0.5">{{ inst.category }}</div>
                                    </div>
                                </div>
                            </div>
                            <div
                                v-if="props.pendingTrainingBuild"
                                class="flex items-center gap-2 px-3 pt-2 border-t border-gray-100 mt-1"
                            >
                                <button
                                    class="flex-1 inline-flex items-center justify-center gap-1 px-2 py-1 text-[11px] font-medium text-white bg-sky-600 hover:bg-sky-700 rounded transition-colors disabled:opacity-60"
                                    :disabled="isApprovingBuild || selectedInstructionIds.size === 0"
                                    @click.stop="handleApproveTrainingBuild"
                                >
                                    <Spinner v-if="isApprovingBuild" class="w-3 h-3 text-white me-1" />
                                    <Icon v-else name="heroicons-check" class="w-3 h-3" />
                                    {{ approveButtonText }}
                                </button>
                                <button
                                    class="inline-flex items-center justify-center gap-1 px-2 py-1 text-[11px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors disabled:opacity-60"
                                    :disabled="isDiscardingBuild"
                                    @click.stop="handleDiscardTrainingBuild"
                                >
                                    <Icon name="heroicons-x-mark" class="w-3 h-3" />
                                    {{ isDiscardingBuild ? $t('prompt.rejecting', 'Rejecting…') : $t('prompt.rejectBuild', 'Reject') }}
                                </button>
                            </div>
                        </div>
                        <div class="h-1"></div>
                    </div>
                </div>
                <!-- View dashboard pill (only if artifacts exist) -->
                <button
                    v-if="props.hasArtifacts"
                    class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 bg-white text-xs text-blue-600 hover:bg-blue-50 transition-colors"
                    @click="emit('viewDashboard')"
                >
                    {{ $t('prompt.viewDashboard') }}
                    <Icon name="heroicons-arrow-right" class="w-3.5 h-3.5 rtl-flip" />
                </button>
            </div>
            <div v-else></div>
            <button
                v-if="isExcel && excelSelection && !excelSelectionDismissed"
                class="text-gray-400 hover:text-gray-600 text-[11px] flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-gray-50 transition-colors"
                @click="addExcelSelectionToPrompt"
                :title="excelSelectionTooltip"
            >
                <span class="text-green-500">●</span>
                <span class="truncate max-w-[160px]">{{ excelSelectionLabel }}</span>
                <span class="text-gray-300 hover:text-gray-500 ms-0.5" @click.stop="excelSelectionDismissed = true">&times;</span>
            </button>
        </div>

        <!-- Minimalist prompt container -->
        <div
            class="border rounded-xl bg-white transition-colors relative"
            :class="[isDraggingFiles ? 'border-blue-400 border-2 bg-blue-50/30' : mode === 'training' ? 'border-sky-300 focus-within:border-sky-400' : 'border-gray-200 focus-within:border-gray-300', props.compact ? 'text-sm' : '']"
            @dragenter="handleDragEnter"
            @dragleave="handleDragLeave"
            @dragover="handleDragOver"
            @drop="handleDrop"
            @paste="handlePaste"
        >
            <!-- Drop overlay -->
            <div
                v-if="isDraggingFiles"
                class="absolute inset-0 bg-blue-50/80 rounded-xl flex items-center justify-center z-10 pointer-events-none"
            >
                <div class="flex flex-col items-center text-blue-600">
                    <Icon name="heroicons-cloud-arrow-up" class="w-8 h-8 mb-2" />
                    <span class="text-sm font-medium">{{ $t('prompt.dropFilesToUpload') }}</span>
                </div>
            </div>

            <!-- Input area -->
            <div :class="props.compact ? 'px-3 pt-2 pb-1' : 'px-3 pt-2.5 pb-3'">
                <!-- Instructions -->
                <button
                    :class="props.compact
                        ? 'text-gray-400 hover:text-gray-600 hover:bg-gray-50 rounded py-0.5 text-sm flex items-center transition-colors mb-1.5'
                        : 'text-gray-400 hover:text-gray-600 hover:bg-gray-50 rounded-md py-0.5 text-sm flex items-center transition-colors mb-2'"
                    @click="openInstructions"
                >
                    <Icon name="heroicons-cube" :class="props.compact ? 'w-4 h-4 me-1.5' : 'w-4 h-4 me-1.5'" />
                    {{ $t('prompt.instructions') }}
                </button>
                <div
                    v-if="isHydratingDataSources"
                    class="flex items-center justify-center py-6 space-x-2 text-xs text-gray-500"
                >
                    <Spinner class="w-4 h-4 text-gray-400" />
                    <span>{{ $t('prompt.loadingReportContext') }}</span>
                </div>
                <MentionInput
                    v-else
                    v-model="text"
                    @update:mentions="handleMentionsUpdate"
                    @submit="submit"
                    :placeholder="placeholder"
                    :rows="props.compact ? 1 : 2"
                    :compact="props.compact"
                    :selectedDataSourceIds="selectedDataSources.map(ds => ds.id)"
                />
            </div>

            <!-- Inline file chips -->
            <div v-if="uploadedFiles.length > 0" class="px-3 pb-2 flex flex-wrap gap-2">
                <!-- Image files - show thumbnail preview -->
                <div
                    v-for="file in uploadedFiles.filter(f => isImageFile(f))"
                    :key="file.id"
                    class="relative group"
                >
                    <div
                        class="w-12 h-12 rounded-lg overflow-hidden border border-gray-200 bg-gray-100"
                        :class="{ 'cursor-pointer hover:opacity-80': file.status === 'uploaded' }"
                        @click="file.status === 'uploaded' && openImagePreview(file)"
                    >
                        <!-- Show local preview while uploading, authenticated image when uploaded -->
                        <img
                            v-if="file.status === 'processing' && file.file"
                            :src="getLocalImageUrl(file)"
                            class="w-full h-full object-cover opacity-50"
                        />
                        <AuthenticatedImage
                            v-else-if="file.status === 'uploaded' && file.id"
                            :file-id="file.id"
                            :alt="file.filename"
                            img-class="w-full h-full object-cover"
                        />
                        <div v-else class="w-full h-full flex items-center justify-center">
                            <Icon name="heroicons-photo" class="w-5 h-5 text-gray-400" />
                        </div>
                        <!-- Processing overlay -->
                        <div v-if="file.status === 'processing'" class="absolute inset-0 flex items-center justify-center bg-white/60">
                            <Spinner class="w-4 h-4 text-blue-500" />
                        </div>
                        <!-- Error overlay -->
                        <div v-if="file.status === 'error'" class="absolute inset-0 flex items-center justify-center bg-red-50/80">
                            <Icon name="heroicons-exclamation-circle" class="w-5 h-5 text-red-500" />
                        </div>
                    </div>
                    <!-- Remove button -->
                    <button
                        @click="removeInlineFile(file)"
                        class="absolute -top-1.5 -end-1.5 w-5 h-5 rounded-full bg-gray-700 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity hover:bg-gray-900"
                        :disabled="file.status === 'processing'"
                    >
                        <Icon name="heroicons-x-mark" class="w-3 h-3" />
                    </button>
                </div>

                <!-- Non-image files - show chip style -->
                <div
                    v-for="file in uploadedFiles.filter(f => !isImageFile(f))"
                    :key="file.id"
                    class="inline-flex items-center gap-1.5 px-2 py-1 bg-gray-100 rounded-lg text-xs text-gray-700 group"
                >
                    <Spinner v-if="file.status === 'processing'" class="w-3 h-3 text-blue-500 flex-shrink-0" />
                    <Icon v-else-if="file.status === 'error'" name="heroicons-exclamation-circle" class="w-3.5 h-3.5 text-red-500 flex-shrink-0" />
                    <Icon v-else name="heroicons-document" class="w-3.5 h-3.5 text-gray-500 flex-shrink-0" />
                    <span class="truncate max-w-[150px]">{{ file.filename }}</span>
                    <button
                        @click="removeInlineFile(file)"
                        class="ms-0.5 p-0.5 rounded hover:bg-gray-200 text-gray-400 hover:text-gray-600 opacity-0 group-hover:opacity-100 transition-opacity"
                        :disabled="file.status === 'processing'"
                    >
                        <Icon name="heroicons-x-mark" class="w-3 h-3" />
                    </button>
                </div>
            </div>

            <!-- Bottom controls -->
            <div
                :class="[props.compact ? 'px-3 pb-2 pt-1' : 'px-3 pb-3', 'flex items-center justify-between', { 'opacity-50 pointer-events-none': isHydratingDataSources }]"
            >
                <div class="flex items-center space-x-1 relative">
                    <!-- Data source selector -->
                    <DataSourceSelector v-model:selectedDataSources="selectedDataSources" :reportId="report_id" />

                    <!-- Mode selector -->
                    <UPopover :key="'mode-' + (props.popoverOffset || 0)" :popper="popperLegacy">
                        <UTooltip :text="isCompactPrompt ? modeLabel : ''" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                            <button
                                class="rounded-md px-2 py-1 text-xs flex items-center"
                                :class="mode === 'training' ? 'text-sky-600 bg-sky-50 hover:bg-sky-100 border border-sky-200' : 'text-gray-500 hover:text-gray-900 hover:bg-gray-50'"
                            >
                                <Icon :name="modeIcon" class="w-4 h-4" />
                                <span v-if="!isCompactPrompt" class="ms-1">{{ modeLabel }}</span>
                            </button>
                        </UTooltip>
                        <template #panel="{ close }">
                            <div class="p-2 text-xs">
                                <div class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between w-[180px]" @click="() => { selectMode('chat'); close(); }">
                                    <div class="flex items-center">
                                        <Icon name="heroicons-chat-bubble-left-right" class="w-4 h-4 me-2" />
                                        {{ $t('prompt.chat') }}
                                    </div>
                                    <Icon v-if="mode === 'chat'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                                </div>
                                <div class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between" @click="() => { selectMode('deep'); close(); }">
                                    <div class="flex items-center">
                                        <Icon name="heroicons-light-bulb" class="w-4 h-4 me-2" />
                                        {{ $t('prompt.deepAnalytics') }}
                                    </div>
                                    <Icon v-if="mode === 'deep'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                                </div>
                                <div v-if="canUseTrainingMode" class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center justify-between" @click="() => { selectMode('training'); close(); }">
                                    <div class="flex items-center">
                                        <Icon name="heroicons-academic-cap" class="w-4 h-4 me-2" />
                                        {{ $t('prompt.training') }}
                                    </div>
                                    <Icon v-if="mode === 'training'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                                </div>
                            </div>
                        </template>
                    </UPopover>
                </div>

                <div class="flex items-center space-x-0.5">
                    <div v-if="props.showContextIndicator" class="flex items-center">
                        <UTooltip :text="contextEstimateTooltip || (isLoadingContextEstimate ? $t('prompt.estimating') : $t('prompt.estimateUnavailable'))" :popper="{ placement: 'top', strategy: 'fixed' }">
                            <button class="text-gray-400 hover:text-gray-900 rounded-md w-7 h-7 flex items-center justify-center transition-colors me-0.5"
                                :disabled="isLoadingContextEstimate">
                                <Spinner v-if="isLoadingContextEstimate" class="w-4 h-4 text-gray-400" />
                                <UIcon
                                    v-else
                                    :name="contextIndicatorIcon"
                                    class="w-4 h-4"
                                />
                            </button>
                        </UTooltip>
                    </div>

                    <!-- File attach (open files modal) -->
                    <FileUploadComponent ref="fileUploadRef" :report_id="report_id" @update:uploadedFiles="onFilesUploaded" />

                    <!-- Schedule a prompt -->
                    <UTooltip v-if="!props.hideScheduleButton" :text="$t('prompt.schedulePrompt')" :popper="{ strategy: 'fixed', placement: 'top' }">
                        <button
                            class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md px-2 py-1 text-xs flex items-center"
                            @click="openScheduleModal"
                        >
                            <Icon name="heroicons-clock" class="w-4 h-4" />
                        </button>
                    </UTooltip>

                    <!-- Model selector -->
                    <UPopover :key="'model-' + (props.popoverOffset || 0)" :popper="popperLegacy">
                        <UTooltip :text="selectedModelLabel" :popper="{ strategy: 'fixed', placement: 'top' }">
                            <button class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md px-2 py-1 text-xs flex items-center max-w-[180px]">
                                <Icon name="heroicons-cpu-chip" class="w-4 h-4 flex-shrink-0" />
                                <span v-if="!isCompactPrompt" class="ms-1 truncate">{{ selectedModelLabel }}</span>
                            </button>
                        </UTooltip>
                        <template #panel="{ close }">
                            <div class="p-2 text-xs max-h-64 overflow-y-auto w-[200px]">
                                <div v-for="m in models" :key="m.id" class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center" @click="() => { selectModel(m.id); close(); }">
                                    <div class="me-2">
                                        <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
                                    </div>
                                    <div class="flex flex-col flex-1 text-start min-w-0">
                                        <span class="font-medium truncate" :title="m.name">{{ m.name }}</span>
                                        <span class="text-gray-500 text-[10px] truncate">{{ m.provider?.name }}</span>
                                    </div>
                                    <Icon v-if="selectedModel === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
                                </div>
                            </div>
                        </template>
                    </UPopover>

                    <!-- Send / Stop -->
                    <button
                        v-if="latestInProgressCompletion"
                        class="text-white bg-gray-500 hover:bg-gray-600 w-7 h-7 rounded-full flex items-center justify-center transition-colors ms-1"
                        :disabled="isStopping"
                        @click="$emit('stopGeneration')"
                    >
                        <Icon name="heroicons-stop-solid" class="w-3.5 h-3.5" />
                    </button>
                    <UTooltip v-else-if="!props.hideSubmitButton" :text="submitTooltip" :popper="{ strategy: 'fixed', placement: 'top' }" :disabled="canSubmit">
                        <button
                            class="text-white w-7 h-7 rounded-full flex items-center justify-center transition-colors ms-1"
                            :class="canSubmit ? (mode === 'training' ? 'bg-sky-500 hover:cursor-pointer hover:bg-sky-600' : 'bg-gray-700 hover:cursor-pointer hover:bg-black') : 'bg-gray-300 cursor-not-allowed'"
                            :disabled="!canSubmit"
                            @click="submit"
                        >
                            <Icon name="heroicons-arrow-right" class="w-3.5 h-3.5 rtl-flip" />
                        </button>
                    </UTooltip>
                </div>
            </div>
        </div>

        <!-- Modals -->
        <InstructionsListModalComponent ref="instructionsListModalRef" />
        <ImagePreviewModal ref="imagePreviewModalRef" />
        <ScheduledPromptModal
            v-model="showScheduledPromptModal"
            :reportId="report_id || ''"
            :initialDataSources="selectedDataSources"
            :draftContent="scheduleDraftContent"
            :draftMode="scheduleDraftMode"
            :draftModel="scheduleDraftModel"
            @saved="emit('scheduledPromptSaved')"
        />
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, getCurrentInstance } from 'vue'
import { useRouter } from 'vue-router'

import DataSourceSelector from '@/components/prompt/DataSourceSelector.vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'
import FileUploadComponent from '@/components/FileUploadComponent.vue'
import MentionInput from '@/components/prompt/MentionInput.vue'
import Spinner from '@/components/Spinner.vue'
import ImagePreviewModal from '@/components/ImagePreviewModal.vue'
import InstructionsListModalComponent from '@/components/InstructionsListModalComponent.vue'
import { useCan } from '@/composables/usePermissions'
import { useOrgSettings } from '@/composables/useOrgSettings'
import { useExcel } from '@/composables/useExcel'

const props = defineProps({
    report_id: String,
    latestInProgressCompletion: Object,
    isStopping: Boolean,
    // Allow fine-tuning alignment if needed later
    popoverOffset: { type: Number, default: 16 },
    // Landing page prefill support
    textareaContent: { type: String, default: '' },
    showContextIndicator: { type: Boolean, default: false },
    initialSelectedDataSources: {
        type: Array,
        default: () => []
    },
    initialMode: {
        type: String as () => 'chat' | 'deep' | 'training',
        default: 'chat'
    },
    // Query list for summary pills above input
    queryList: {
        type: Array as () => { id: string; label: string; rowCount?: number; messageId: string; stepId?: string }[],
        default: () => []
    },
    // Scheduled prompts for the pill above input
    scheduledPrompts: {
        type: Array as () => { id: string; prompt: any; cron_schedule: string; is_active: boolean }[],
        default: () => []
    },
    // Training instructions for the pill above input
    trainingInstructions: {
        type: Array as () => { instructionId: string; title: string; category: string; isEdit: boolean; lineCount: number }[],
        default: () => []
    },
    // Pending draft build (if any) to expose Approve / Discard actions in the pill
    pendingTrainingBuild: {
        type: Object as () => { id: string; status: string; total_instructions: number } | null,
        default: null
    },
    // Aggregate line diff for pendingTrainingBuild vs main build (loaded by parent)
    pendingTrainingBuildDiff: {
        type: Object as () => { added_lines: number; removed_lines: number } | null,
        default: null
    },
    // Whether the report has artifacts (for "View dashboard" pill)
    hasArtifacts: { type: Boolean, default: false },
    // Hide the schedule button (when embedded inside ScheduledPromptModal)
    hideScheduleButton: { type: Boolean, default: false },
    hideSubmitButton: { type: Boolean, default: false },
    compact: { type: Boolean, default: false },
    // Initial model to pre-select
    initialModel: { type: String, default: '' }
})

const emit = defineEmits(['submitCompletion','stopGeneration','update:modelValue','viewDashboard','scrollToMessage','editScheduledPrompt','deleteScheduledPrompt','scheduledPromptSaved','toggleScheduledPrompt','editTrainingInstruction','approveTrainingBuild','discardTrainingBuild','openInstructions','update:selectedDataSources'])

const isApprovingBuild = ref(false)
const isDiscardingBuild = ref(false)
const selectedInstructionIds = ref<Set<string>>(new Set())

// Select all changes by default; preserve user selections across list updates.
watch(
    () => props.trainingInstructions,
    (next, prev) => {
        const prevIds = new Set((prev || []).map((i: any) => i.instructionId))
        const sel = new Set(selectedInstructionIds.value)
        for (const inst of next || []) {
            if (!prevIds.has(inst.instructionId)) sel.add(inst.instructionId)
        }
        const currentIds = new Set((next || []).map((i: any) => i.instructionId))
        for (const id of sel) {
            if (!currentIds.has(id)) sel.delete(id)
        }
        selectedInstructionIds.value = sel
    },
    { immediate: true, deep: true }
)

function toggleInstructionSelection(id: string, checked: boolean) {
    const next = new Set(selectedInstructionIds.value)
    if (checked) next.add(id)
    else next.delete(id)
    selectedInstructionIds.value = next
}

const approveButtonText = computed(() => {
    if (isApprovingBuild.value) return t('prompt.approving', 'Approving…')
    const n = selectedInstructionIds.value.size
    if (n === 0) return t('prompt.approveBuild', 'Approve & publish')
    if (n === 1) return t('prompt.approveOne', 'Publish 1 change')
    return t('prompt.approveMany', { n, default: `Publish ${n} changes` })
})

async function handleApproveTrainingBuild() {
    if (!props.pendingTrainingBuild || isApprovingBuild.value) return
    if (selectedInstructionIds.value.size === 0) return
    isApprovingBuild.value = true
    try {
        await Promise.resolve(emit('approveTrainingBuild', {
            buildId: props.pendingTrainingBuild.id,
            instructionIds: Array.from(selectedInstructionIds.value),
        }))
    } finally {
        isApprovingBuild.value = false
        showTrainingDropdown.value = false
    }
}
async function handleDiscardTrainingBuild() {
    if (!props.pendingTrainingBuild || isDiscardingBuild.value) return
    isDiscardingBuild.value = true
    try {
        await Promise.resolve(emit('discardTrainingBuild', props.pendingTrainingBuild.id))
    } finally {
        isDiscardingBuild.value = false
        showTrainingDropdown.value = false
    }
}

const { t } = useI18n()
const text = ref('')
const placeholder = computed(() => props.compact ? t('prompt.placeholderCompact') : t('prompt.placeholderDefault'))
const mode = ref<'chat' | 'deep' | 'training'>(props.initialMode || 'chat')
const selectedDataSources = ref<any[]>([...(props.initialSelectedDataSources || [])])

// Emit whenever selected data sources change (for parent sync, e.g. agent panel)
watch(selectedDataSources, (val) => {
    emit('update:selectedDataSources', val)
}, { deep: true })
const isHydratingDataSources = ref(!!props.report_id && selectedDataSources.value.length === 0)
const uploadedFiles = ref<any[]>([])
const isCompactPrompt = ref(false)
const inlineMentions = ref<any[]>([])
const hasBootstrappedFromInitial = ref(selectedDataSources.value.length > 0)
const isDraggingFiles = ref(false)
const showQueryDropdown = ref(false)
const showScheduledDropdown = ref(false)
const showTrainingDropdown = ref(false)
const showScheduledPromptModal = ref(false)
const scheduleDraftContent = ref('')
const scheduleDraftMode = ref<'chat' | 'deep'>('chat')
const scheduleDraftModel = ref('')

const openScheduleModal = () => {
    scheduleDraftContent.value = text.value
    scheduleDraftMode.value = mode.value === 'training' ? 'chat' : mode.value
    scheduleDraftModel.value = selectedModel.value
    showScheduledPromptModal.value = true
}

const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
function getCronLabel(cron: string): string {
    if (!cron) return ''
    const p = cron.split(' ')
    if (p.length < 5) return cron
    const [min, hour, dom, , dow] = p
    const fmtHour = (h: string) => {
        const n = parseInt(h)
        if (n === 0) return '12 AM'
        if (n < 12) return `${n} AM`
        if (n === 12) return '12 PM'
        return `${n - 12} PM`
    }
    if (min.startsWith('*/')) return `Every ${min.slice(2)} min`
    if (hour.startsWith('*/')) return `Every ${hour.slice(2)} hr`
    if (dow === '1-5') return `Weekdays at ${fmtHour(hour)}`
    if (dom !== '*' && dow === '*') return `Monthly on the ${dom}${ordSuffix(+dom)} at ${fmtHour(hour)}`
    if (dow !== '*') return `${dayNames[+dow] || dow}s at ${fmtHour(hour)}`
    if (hour !== '*') return `Daily at ${fmtHour(hour)}`
    return `Hourly`
}
function ordSuffix(n: number): string {
    if (n >= 11 && n <= 13) return 'th'
    const r = n % 10
    return r === 1 ? 'st' : r === 2 ? 'nd' : r === 3 ? 'rd' : 'th'
}
let dragCounter = 0 // Track enter/leave for nested elements

// Excel selection hint
const { isExcel, excelSelection } = useExcel()
const excelSelectionDismissed = ref(false)

const excelSelectionLabel = computed(() => {
    if (!excelSelection.value) return ''
    const addr = excelSelection.value.address.replace(/^.*!/, '') // strip sheet prefix from address
    const count = excelSelection.value.totalCellCount
    return `${addr} (${count} cell${count !== 1 ? 's' : ''})`
})

const excelSelectionTooltip = computed(() => {
    if (!excelSelection.value) return ''
    const s = excelSelection.value
    let tip = `${s.sheetName} ${s.address} — ${s.totalCellCount} cells`
    if (s.truncated) tip += ` (truncated to ${s.cellCount})`
    return tip + '\nClick to add to prompt'
})

// Re-show hint when selection changes
watch(excelSelection, () => {
    excelSelectionDismissed.value = false
})

function addExcelSelectionToPrompt() {
    if (!excelSelection.value) return
    const s = excelSelection.value
    const rows = s.selectionValues
    if (!rows || rows.length === 0) return

    // Build a compact markdown table
    const header = rows[0].map((v: any) => v == null ? '' : String(v))
    const separator = header.map(() => '---')
    const dataRows = rows.slice(1).map((row: readonly any[]) =>
        row.map((v: any) => v == null ? '' : String(v)).join(' | ')
    )
    const lines = [
        `[Excel: ${s.sheetName} ${s.address}]`,
        header.join(' | '),
        separator.join(' | '),
        ...dataRows
    ]
    if (s.truncated) lines.push(`... truncated (${s.totalCellCount} total cells)`)

    const snippet = lines.join('\n')
    text.value = text.value ? text.value + '\n\n' + snippet : snippet
    excelSelectionDismissed.value = true
}

// Watch for changes in initialSelectedDataSources (from domain selector)
// On landing page (no report_id): always sync with domain selector
// On report page: only bootstrap once, then use report's data sources
watch(() => props.initialSelectedDataSources, (newVal) => {
    if (!Array.isArray(newVal)) return
    
    // On landing page (no report_id), always sync with domain selector
    if (!props.report_id) {
        selectedDataSources.value = [...newVal]
        isHydratingDataSources.value = false
        return
    }
    
    // On report page, only bootstrap once
    if (hasBootstrappedFromInitial.value) return
    if (newVal.length === 0) return
    selectedDataSources.value = [...newVal]
    hasBootstrappedFromInitial.value = selectedDataSources.value.length > 0
    isHydratingDataSources.value = false
}, { deep: true })

type CompletionContextEstimate = {
    model_id: string
    model_name?: string
    prompt_tokens: number
    model_limit?: number
    remaining_tokens?: number
    near_limit?: boolean
    context_usage_pct?: number
}

const contextEstimate = ref<CompletionContextEstimate | null>(null)
const isLoadingContextEstimate = ref(false)
const contextEstimateError = ref<string | null>(null)
const hasRequestedContextEstimate = ref(false)
const numberFormatter = new Intl.NumberFormat()

function formatTokenCountShort(value: number | null | undefined): string {
    if (value === null || value === undefined) return ''
    if (value >= 1_000_000) {
        return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
    }
    if (value >= 1_000) {
        return `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}K`
    }
    return `${value}`
}

const contextEstimateShort = computed(() => {
    return formatTokenCountShort(contextEstimate.value?.prompt_tokens)
})

const contextUsagePercent = computed(() => {
    const pct = contextEstimate.value?.context_usage_pct
    if (pct === null || pct === undefined) return ''
    return `${Math.round(pct)}%`
})

const contextEstimateTooltip = computed(() => {
    if (!props.showContextIndicator) return ''
    if (isLoadingContextEstimate.value) return t('prompt.estimatingContext')
    if (contextEstimateError.value) return contextEstimateError.value
    if (!contextEstimate.value) return ''
    const pct = contextUsagePercent.value
    const promptShort = contextEstimateShort.value
    if (pct && promptShort) {
        return t('prompt.contextSizeTokens', { pct, tokens: promptShort })
    }
    if (pct) {
        return t('prompt.contextSizePct', { pct })
    }
    if (promptShort) return t('prompt.contextSizeShort', { tokens: promptShort })
    return t('prompt.contextSizeUnavailable')
})

const contextIndicatorIcon = computed(() => {
    if (isLoadingContextEstimate.value) return 'i-heroicons-arrow-path'
    if (contextEstimateError.value) return 'i-heroicons-exclamation-triangle'
    return 'i-heroicons-information-circle'
})

// Popover state
const showModeMenu = ref(false)
const showModelMenu = ref(false)

// Mode computed properties
const modeLabel = computed(() => {
    switch (mode.value) {
        case 'chat': return t('prompt.chat')
        case 'deep': return t('prompt.deepAnalytics')
        case 'training': return t('prompt.training')
        default: return t('prompt.chat')
    }
})

const modeIcon = computed(() => {
    switch (mode.value) {
        case 'chat': return 'heroicons-chat-bubble-left-right'
        case 'deep': return 'heroicons-light-bulb'
        case 'training': return 'heroicons-academic-cap'
        default: return 'heroicons-chat-bubble-left-right'
    }
})

// Permission check for training mode - requires permission, allow_llm_see_data, and enable_training_mode enabled
const { allowLlmSeeData, isTrainingModeEnabled } = useOrgSettings()
const canUseTrainingMode = computed(() => useCan('train_mode') && isTrainingModeEnabled.value)

// Model selector state - fetch from backend
const models = ref<any[]>([])
const selectedModel = ref<string>('')
const selectedModelLabel = computed(() => {
    const model = models.value.find(m => m.id === selectedModel.value)
    return model?.name || t('prompt.selectModel')
})

// Legacy popper (for current Nuxt UI stable)
// Use a small fixed skid so content hugs the left edge of the chip
// Use absolute strategy so transforms from split-screen don't affect placement
const popperLegacy = computed(() => ({ strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [ 0, 8 ] }))


async function loadModels() {
    try {
        const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
        if (data.value && Array.isArray(data.value)) {
            models.value = data.value
            // Set the default model as selected, or fall back to first enabled model
            if (!selectedModel.value && models.value.length > 0) {
                if (props.initialModel && models.value.find(m => m.id === props.initialModel)) {
                    selectedModel.value = props.initialModel
                } else {
                // First try to find the model marked as default
                const defaultModel = models.value.find(m => m.is_default)
                if (defaultModel) {
                    selectedModel.value = defaultModel.id
                } else {
                    // Fall back to first enabled model if no default is set
                    selectedModel.value = models.value[0].id
                }
                }
            }
        }
    } catch (error) {
        console.error('Failed to load models:', error)
        // Fallback to hardcoded models
        models.value = [
            { id: 'default', name: 'Default Model', provider: { name: 'System' } }
        ]
        selectedModel.value = 'default'
    }
}

async function hydrateReportDataSources(reportId?: string, { showSpinner = true } = {}) {
    if (!reportId) {
        selectedDataSources.value = []
        if (showSpinner) isHydratingDataSources.value = false
        return
    }

    if (showSpinner) {
        isHydratingDataSources.value = true
    }
    try {
        const res = await useMyFetch(`/reports/${reportId}`, { method: 'GET' })
        const report = (res as any)?.data?.value as any
        if (report && Array.isArray(report.data_sources)) {
            selectedDataSources.value = report.data_sources
        } else {
            selectedDataSources.value = []
        }
        hasBootstrappedFromInitial.value = selectedDataSources.value.length > 0
    } catch (e) {
        console.error('Failed to hydrate data sources for report:', e)
    } finally {
        if (showSpinner) {
            isHydratingDataSources.value = false
        }
    }
}

async function refreshContextEstimate(force = false) {
    if (!props.showContextIndicator || !props.report_id) return
    if (!force && hasRequestedContextEstimate.value) return
    hasRequestedContextEstimate.value = true
    isLoadingContextEstimate.value = true
    contextEstimateError.value = null
    try {
        const response = await useMyFetch(`/reports/${props.report_id}/completions/estimate`, {
            method: 'POST',
            body: JSON.stringify({
                prompt: {
                    content: ' ',
                    mentions: [],
                    mode: mode.value,
                    model_id: selectedModel.value || undefined
                },
                stream: false
            })
        })
        const errorValue = (response as any)?.error?.value
        if (errorValue) {
            throw errorValue
        }
        const estimate = (response as any)?.data?.value as CompletionContextEstimate | null
        contextEstimate.value = estimate
    } catch (err) {
        console.error('Failed to fetch context estimate:', err)
        contextEstimateError.value = t('prompt.estimateUnavailable')
    } finally {
        isLoadingContextEstimate.value = false
    }
}

function selectModel(modelId: string) {
    selectedModel.value = modelId
}

async function persistMode() {
    // Only persist for reports, not landing page
    if (!props.report_id) return
    try {
        await useMyFetch(`/reports/${props.report_id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode.value })
        })
    } catch (e) {
        console.error('Failed to persist mode:', e)
    }
}

function selectMode(m: 'chat' | 'deep' | 'training') {
    mode.value = m
    persistMode()
}

// Functions to select and close popovers
function selectModeAndClose(m: 'chat' | 'deep' | 'training') {
    selectMode(m)
    showModeMenu.value = false
}

function selectModelAndClose(modelId: string) {
    selectModel(modelId)
    showModelMenu.value = false
}

function handleMentionsUpdate(mentions: any[]) {
    inlineMentions.value = mentions
}

function onInput() {
    emit('update:modelValue', text.value)
}

// Only count successfully uploaded files for submit eligibility
const successfullyUploadedFiles = computed(() => {
    return uploadedFiles.value.filter(f => f.status === 'uploaded')
})

const hasFilesUploading = computed(() => {
    return uploadedFiles.value.some(f => f.status === 'processing')
})

const hasDataSourceOrFile = computed(() => {
    return selectedDataSources.value.length > 0 || successfullyUploadedFiles.value.length > 0
})

const canSubmit = computed(() => {
    return text.value.trim().length > 0
        && !props.latestInProgressCompletion
        && !isHydratingDataSources.value
        && !hasFilesUploading.value  // Don't allow submit while files are uploading
        && !!selectedModel.value
        && hasDataSourceOrFile.value
})

const submitTooltip = computed(() => {
    if (!selectedModel.value && !hasDataSourceOrFile.value) {
        return t('prompt.connectLLMAndData')
    }
    if (!selectedModel.value) {
        return t('prompt.connectLLM')
    }
    if (!hasDataSourceOrFile.value) {
        return t('prompt.connectDataOrFile')
    }
    if (hasFilesUploading.value) {
        return t('prompt.waitingForFiles')
    }
    if (!text.value.trim()) {
        return t('prompt.enterMessage')
    }
    return ''
})

function submit() {
    if (!canSubmit.value) return

    // Excel selection is delivered via prompt.platform_context on the parent
    // submit path (see onSubmitCompletion). It is intentionally NOT prepended
    // to the user-visible text here.

    // Organize inline mentions by type
    const mentionsByType = {
        data_sources: inlineMentions.value.filter(m => m.type === 'data_source'),
        tables: inlineMentions.value.filter(m => m.type === 'datasource_table'),
        files: inlineMentions.value.filter(m => m.type === 'file'),
        entities: inlineMentions.value.filter(m => m.type === 'entity')
    }
    // Get image files that have been successfully uploaded (for immediate display in chat)
    const imageFiles = successfullyUploadedFiles.value
        .filter(f => isImageFile(f))
        .map(f => ({ id: f.id, filename: f.filename, content_type: f.content_type }))

    const payload = {
        text: text.value,
        mentions: [
            { name: 'DATA SOURCES', items: mentionsByType.data_sources },
            { name: 'TABLES', items: mentionsByType.tables },
            { name: 'FILES', items: mentionsByType.files },
            { name: 'ENTITIES', items: mentionsByType.entities }
        ],
        mode: mode.value,                 // 'chat' | 'deep'
        model_id: selectedModel.value,    // backend model id from selector
        files: imageFiles                 // image files for immediate display in chat
    }
    if (props.report_id) {
        // In-report behavior: emit to parent stream
        emit('submitCompletion', payload)
        text.value = ''
        // Clear images from prompt area - they're now part of the message
        // Backend will delete them after completion
        fileUploadRef.value?.clearImages?.()
    } else {
        // Landing page behavior: create a new report
        createReport()
    }
}

function onFilesUploaded(files: any[]) {
    uploadedFiles.value = files || []
}

// Helper to check if a file is an image
function isImageFile(file: any): boolean {
    const contentType = file.content_type || file.type || ''
    return contentType.startsWith('image/')
}

// Remove a file from the inline display
function removeInlineFile(file: any) {
    fileUploadRef.value?.removeFile?.(file)
}

// Get local blob URL for image preview while uploading
const localImageUrls = new Map<string, string>()
function getLocalImageUrl(file: any): string {
    if (!file.file) return ''
    const key = file.id || file.filename
    if (localImageUrls.has(key)) {
        return localImageUrls.get(key)!
    }
    const url = URL.createObjectURL(file.file)
    localImageUrls.set(key, url)
    return url
}

// Drag & drop handlers for file upload
function handleDragEnter(e: DragEvent) {
    e.preventDefault()
    dragCounter++
    if (e.dataTransfer?.types.includes('Files')) {
        isDraggingFiles.value = true
    }
}

function handleDragLeave(e: DragEvent) {
    e.preventDefault()
    dragCounter--
    if (dragCounter === 0) {
        isDraggingFiles.value = false
    }
}

function handleDragOver(e: DragEvent) {
    e.preventDefault()
}

function handleDrop(e: DragEvent) {
    e.preventDefault()
    dragCounter = 0
    isDraggingFiles.value = false

    const files = e.dataTransfer?.files
    if (files && files.length > 0) {
        fileUploadRef.value?.uploadFiles?.(files)
    }
}

// Paste handler for images (Cmd+V / Ctrl+V)
function handlePaste(e: ClipboardEvent) {
    const items = e.clipboardData?.items
    if (!items) return

    const imageFiles: File[] = []
    for (const item of items) {
        if (item.type.startsWith('image/')) {
            const file = item.getAsFile()
            if (file) imageFiles.push(file)
        }
    }

    if (imageFiles.length > 0) {
        e.preventDefault()  // Don't paste as text
        fileUploadRef.value?.uploadFiles?.(imageFiles)
    }
    // If no images, let normal text paste happen
}

const fileUploadRef = ref<any | null>(null)
const instructionsListModalRef = ref<any | null>(null)
const imagePreviewModalRef = ref<InstanceType<typeof ImagePreviewModal> | null>(null)

const attrs = useAttrs()

const instance = getCurrentInstance()

function openInstructions() {
    if (instance?.vnode.props?.onOpenInstructions) {
        emit('openInstructions')
    } else {
        const dataSourceIds = selectedDataSources.value.map((ds: any) => ds.id)
        instructionsListModalRef.value?.openModal?.(dataSourceIds)
    }
}

function openImagePreview(file: any) {
    if (file.id) {
        imagePreviewModalRef.value?.open(file)
    }
}

function handleEscKey(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    if (props.latestInProgressCompletion && !props.isStopping) {
        e.preventDefault()
        emit('stopGeneration')
    }
}

// Handle prompt prefill event from other components (e.g., ArtifactFrame)
function handlePromptPrefill(event: Event) {
    const detail = (event as CustomEvent).detail
    if (detail?.text) {
        text.value = detail.text
        // Auto-submit if requested (after a brief delay to ensure text is set)
        if (detail.autoSubmit) {
            setTimeout(() => {
                if (canSubmit.value) {
                    submit()
                }
            }, 50)
        }
    }
}

onMounted(async () => {
    // Listen for prompt prefill events
    window.addEventListener('prompt:prefill', handlePromptPrefill)
    window.addEventListener('keydown', handleEscKey)

    await loadModels()
    await refreshContextEstimate(false)
    if (props.report_id) {
        const shouldShowSpinner = selectedDataSources.value.length === 0
        await hydrateReportDataSources(props.report_id, { showSpinner: shouldShowSpinner })
        if (!shouldShowSpinner) {
            isHydratingDataSources.value = false
        }
    } else {
        isHydratingDataSources.value = false
    }
    // Compact mode: if container is narrow, hide labels
    const root = document.querySelector('.flex-shrink-0') as HTMLElement
    const ro = new ResizeObserver(() => {
        const w = root?.clientWidth || 0
        isCompactPrompt.value = w > 0 && w < 420
    })
    if (root) ro.observe(root)
})

onBeforeUnmount(() => {
    window.removeEventListener('prompt:prefill', handlePromptPrefill)
    window.removeEventListener('keydown', handleEscKey)
})

watch(() => props.report_id, async (newId, oldId) => {
    if (newId !== oldId) {
        selectedDataSources.value = [...(props.initialSelectedDataSources || [])]
        hasBootstrappedFromInitial.value = selectedDataSources.value.length > 0
        const shouldShowSpinner = selectedDataSources.value.length === 0
        await hydrateReportDataSources(newId, { showSpinner: shouldShowSpinner })
        if (!shouldShowSpinner) {
            isHydratingDataSources.value = false
        }
        if (props.showContextIndicator && newId) {
            hasRequestedContextEstimate.value = false
            await refreshContextEstimate(false)
        }
    }
})

watch(() => props.showContextIndicator, async (newVal, oldVal) => {
    if (!newVal) {
        hasRequestedContextEstimate.value = false
        return
    }
    await refreshContextEstimate(false)
})

watch(selectedModel, async (newModel, oldModel) => {
    if (!props.showContextIndicator) return
    hasRequestedContextEstimate.value = false
    await refreshContextEstimate(true)
})

defineExpose({
    refreshContextEstimate: () => refreshContextEstimate(true),
    // Refresh files list after completion (when backend deletes images)
    refreshFiles: () => fileUploadRef.value?.refresh?.(),
    // Expose current state for external save (e.g. ScheduledPromptModal)
    getText: () => text.value,
    getMode: () => mode.value,
    getModel: () => selectedModel.value,
    getMentions: () => inlineMentions.value,
})

// Keep local text in sync with parent-provided content (landing page)
watch(() => props.textareaContent, (newVal) => {
    if (typeof newVal === 'string' && newVal !== text.value) {
        text.value = newVal
    }
}, { immediate: true })

// Keep mode in sync with initialMode prop (from report data)
watch(() => props.initialMode, (newVal) => {
    if (newVal && newVal !== mode.value) {
        mode.value = newVal
    }
}, { immediate: true })

const router = useRouter()

async function createReport() {
    try {
        if (!text.value.trim()) return
        const response = await useMyFetch('/reports', {
            method: 'POST',
            body: JSON.stringify({
                title: 'untitled report',
                files: successfullyUploadedFiles.value?.map((file: any) => file.id) || [],
                new_message: text.value,
                data_sources: selectedDataSources.value?.map((ds: any) => ds.id) || []
            })
        })
        if ((response as any)?.error?.value) {
            throw new Error('Report creation failed')
        }
        const data = (response as any)?.data?.value as any
        if (data?.id) {
            // Build mentions from inlineMentions only (no automatic data sources)
            const mentionsByType = {
                data_sources: inlineMentions.value.filter((m: any) => m.type === 'data_source'),
                tables: inlineMentions.value.filter((m: any) => m.type === 'datasource_table'),
                files: inlineMentions.value.filter((m: any) => m.type === 'file'),
                entities: inlineMentions.value.filter((m: any) => m.type === 'entity')
            }
            const mentions = [
                { name: 'DATA SOURCES', items: mentionsByType.data_sources },
                { name: 'TABLES', items: mentionsByType.tables },
                { name: 'FILES', items: mentionsByType.files },
                { name: 'ENTITIES', items: mentionsByType.entities }
            ]

            router.push({ 
                path: `/reports/${data.id}`, 
                query: { 
                    new_message: text.value,
                    mode: mode.value,
                    model_id: selectedModel.value || '',
                    mentions: encodeURIComponent(JSON.stringify(mentions))
                }
            })
        }
        text.value = ''
    } catch (error) {
        console.error('Failed to create report:', error)
    }
}
</script>

<style scoped>
.placeholder-gray-400::placeholder { color: #9ca3af; }
</style>


