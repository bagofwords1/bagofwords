<template>
    <div class="flex flex-col h-full">
        <!-- VIEW MODE: Read-only display for existing instructions -->
        <div v-if="isEditing && isViewMode" class="flex-1 flex flex-col min-h-0">
            <!-- Scrollable content area -->
            <div class="flex-1 overflow-y-auto px-6 py-5 space-y-5">

                <!-- Content Display -->
                <div class="border border-gray-200 rounded-xl overflow-hidden bg-white">
                    <!-- Header with file path and git sync status -->
                    <div class="flex items-center justify-between px-3 py-1.5 bg-gray-50 border-b border-gray-100">
                        <div class="flex items-center gap-2 min-w-0">
                            <Icon v-if="props.isGitSourced" name="heroicons:code-bracket" class="w-3 h-3 text-gray-400 shrink-0" />
                            <span v-if="filePath" class="text-xs font-mono text-gray-600 truncate">{{ filePath }}</span>
                            <span v-else class="text-xs font-medium text-gray-500">Content</span>
                        </div>
                        <div v-if="props.isGitSourced" class="flex items-center gap-2 shrink-0">
                            <span v-if="props.isGitSynced" class="flex items-center gap-1 text-[10px] text-green-600 bg-green-50 px-1.5 py-0.5 rounded">
                                <GitBranchIcon class="w-3 h-3" />
                                Synced
                            </span>
                            <span v-else class="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">Unlinked</span>
                        </div>
                    </div>
                    
                    <!-- Markdown rendered content (for .md files or non-git-linked) -->
                    <div v-if="shouldRenderAsMarkdown" class="p-4 markdown-wrapper">
                        <MDC :value="instructionForm.text || ''" class="markdown-content" />
                    </div>
                    
                    <!-- Code block for other file types -->
                    <div v-else class="p-4 bg-gray-50">
                        <pre class="text-xs leading-relaxed font-mono text-gray-800 whitespace-pre-wrap overflow-x-auto"><code>{{ instructionForm.text }}</code></pre>
                    </div>
                </div>

                <!-- Metadata Display (read-only) -->
                <div class="flex flex-wrap items-center gap-3 text-xs">
                    <!-- Status -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Status:</span>
                        <span :class="getStatusClass(instructionForm.status)" class="inline-flex px-2 py-0.5 text-[11px] font-medium rounded-full">
                            {{ getCurrentStatusDisplayText() }}
                        </span>
                    </div>
                    
                    <!-- Category -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Category:</span>
                        <div class="inline-flex items-center text-gray-700">
                            <Icon :name="getCategoryIcon(instructionForm.category)" class="w-3 h-3 mr-1" />
                            {{ formatCategory(instructionForm.category) }}
                        </div>
                    </div>

                    <!-- Load Mode -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Loading:</span>
                        <div class="inline-flex items-center text-gray-700">
                            <Icon :name="getLoadModeIcon(instructionForm.load_mode)" class="w-3 h-3 mr-1" />
                            {{ getLoadModeLabel(instructionForm.load_mode) }}
                        </div>
                    </div>

                    <!-- Visibility -->
                    <div class="flex items-center gap-1.5">
                        <Icon :name="instructionForm.is_seen ? 'heroicons:eye' : 'heroicons:eye-slash'" class="w-3 h-3 text-gray-400" />
                        <span class="text-gray-600">{{ instructionForm.is_seen ? 'Visible' : 'Hidden' }}</span>
                    </div>
                </div>

                <!-- Labels (read-only) -->
                <div v-if="selectedLabelObjects.length > 0" class="flex items-center gap-2">
                    <span class="text-[11px] text-gray-400">Labels:</span>
                    <div class="flex flex-wrap gap-1">
                        <span
                            v-for="label in selectedLabelObjects"
                            :key="label.id"
                            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px]"
                            :style="{ backgroundColor: (label.color || '#94a3b8') + '20', color: '#1F2937' }"
                        >
                            <span class="w-1.5 h-1.5 rounded-full" :style="{ backgroundColor: label.color || '#94a3b8' }"></span>
                            {{ label.name }}
                        </span>
                    </div>
                </div>

                <!-- Scope (read-only) -->
                <div class="flex flex-wrap items-center gap-4 text-xs">
                    <!-- Data Sources -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Sources:</span>
                        <span v-if="isAllDataSourcesSelected" class="text-gray-700">All sources</span>
                        <div v-else-if="getSelectedDataSourceObjects.length > 0" class="flex items-center gap-1">
                            <div class="flex -space-x-1">
                                <DataSourceIcon 
                                    v-for="ds in getSelectedDataSourceObjects.slice(0, 3)" 
                                    :key="ds.id" 
                                    :type="ds.type" 
                                    class="h-4 w-4 border border-white rounded" 
                                />
                            </div>
                            <span class="text-gray-700">{{ getSelectedDataSourceObjects.length }} source{{ getSelectedDataSourceObjects.length > 1 ? 's' : '' }}</span>
                        </div>
                        <span v-else class="text-gray-400">None</span>
                    </div>

                    <!-- Tables -->
                    <div class="flex items-center gap-1.5">
                        <span class="text-gray-400">Tables:</span>
                        <span v-if="selectedReferences.length === 0" class="text-gray-400">None</span>
                        <span v-else class="text-gray-700">{{ selectedReferences.length }} table{{ selectedReferences.length > 1 ? 's' : '' }}</span>
                    </div>
                </div>

            </div>
            
            <!-- View Mode Actions (fixed at bottom) -->
            <div class="shrink-0 bg-white border-t px-5 py-3">
                <div class="flex justify-between items-center">
                    <UButton 
                        size="xs"
                        color="red" 
                        variant="ghost" 
                        @click="confirmDelete"
                        :loading="isDeleting"
                    >
                        <Icon name="heroicons:trash" class="w-3.5 h-3.5 mr-1" />
                        Delete
                    </UButton>
                    
                    <div class="flex gap-2">
                        <UButton color="gray" variant="ghost" size="xs" @click="$emit('cancel')">
                            Close
                        </UButton>
                        <UButton 
                            size="xs" 
                            color="blue"
                            @click="isViewMode = false"
                        >
                            <Icon name="heroicons:pencil" class="w-3.5 h-3.5 mr-1" />
                            Edit
                        </UButton>
                    </div>
                </div>
            </div>
        </div>

        <!-- EDIT MODE: Form for creating/editing instructions -->
        <form v-else @submit.prevent="submitForm" class="flex-1 flex flex-col min-h-0">
            <!-- Scrollable content area -->
            <div class="flex-1 overflow-y-auto px-6 py-5 space-y-5">

                <!-- Hero Textarea / Code Editor -->
                <div class="border border-gray-200 rounded-xl overflow-hidden focus-within:ring-2 focus-within:ring-blue-100 focus-within:border-blue-400">
                    <!-- Header with file path, git sync status, and code view toggle -->
                    <div class="flex items-center justify-between px-3 py-1.5 bg-white border-b border-gray-100">
                        <div class="flex items-center gap-2 min-w-0">
                            <Icon v-if="props.isGitSourced" name="heroicons:code-bracket" class="w-3 h-3 text-gray-400 shrink-0" />
                            <span v-if="filePath" class="text-xs font-mono text-gray-600 truncate">{{ filePath }}</span>
                            <span v-else class="text-xs font-medium text-gray-500">Instruction</span>
                        </div>
                        <div class="flex items-center gap-2 shrink-0">
                            <!-- Git sync status and actions -->
                            <template v-if="props.isGitSourced">
                                <UTooltip 
                                    v-if="props.isGitSynced"
                                    text="Stop syncing from git. You'll be able to edit manually."
                                    :popper="{ placement: 'top' }"
                                >
                                    <button 
                                        type="button"
                                        class="flex items-center gap-1 text-[10px] text-green-600 bg-green-50 px-1.5 py-0.5 rounded hover:bg-green-100 transition-colors"
                                        @click="$emit('unlink-from-git')"
                                    >
                                        <GitBranchIcon class="w-3 h-3" />
                                        Synced
                                        <Icon name="heroicons:x-mark" class="w-3 h-3" />
                                    </button>
                                </UTooltip>
                                <template v-else>
                                    <span class="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">Unlinked</span>
                                    <UTooltip 
                                        text="Resume syncing from git"
                                        :popper="{ placement: 'top' }"
                                    >
                                        <button 
                                            type="button"
                                            class="text-[10px] text-blue-500 hover:text-blue-600 transition-colors"
                                            @click="$emit('relink-to-git')"
                                        >
                                            Relink
                                        </button>
                                    </UTooltip>
                                </template>
                            </template>
                            <!-- Code view toggle -->
                            <button 
                                type="button"
                                @click="codeView = !codeView"
                                class="text-gray-400 hover:text-gray-600 p-1 rounded transition-colors"
                                :title="codeView ? 'Switch to text editor' : 'Switch to code editor'"
                            >
                                <Icon :name="codeView ? 'heroicons:document-text' : 'heroicons:code-bracket'" class="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                    
                    <!-- Normal textarea -->
                    <textarea 
                        v-if="!codeView"
                        v-model="instructionForm.text"
                        placeholder="Describe the instruction for the AI agent...

Examples:
• When querying revenue, always filter out cancelled orders
• Use the customers_v2 table instead of the deprecated customers table  
• Calculate MRR as sum of active subscription amounts"
                        class="w-full min-h-[210px] text-xs leading-relaxed p-4
                               border-0 resize-y
                               focus:ring-0 focus:outline-none
                               placeholder:text-gray-400"
                        required
                    />
                    
                    <!-- Code editor (Monaco with white background) -->
                    <ClientOnly v-else>
                        <MonacoEditor
                            v-model="instructionForm.text"
                            lang="sql"
                            :options="{ 
                                theme: 'vs', 
                                automaticLayout: true, 
                                minimap: { enabled: false }, 
                                wordWrap: 'on',
                                lineNumbers: 'on',
                                fontSize: 12,
                                scrollBeyondLastLine: false
                            }"
                            style="height: 210px"
                        />
                    </ClientOnly>
                    
                    <!-- Action buttons row -->
                    <div class="px-3 py-2 bg-gray-50 border-t border-gray-100 flex items-center gap-2">
                        <button 
                            type="button"
                            @click="enhanceInstruction"
                            :disabled="isEnhancing || !instructionForm.text?.trim()"
                            class="inline-flex items-center gap-1 px-2.5 py-1 
                                   bg-white border border-gray-200 rounded-full
                                   text-xs text-gray-600
                                   hover:bg-gray-50 hover:border-gray-300
                                   disabled:opacity-50 disabled:cursor-not-allowed
                                   transition-all"
                        >
                            <Spinner v-if="isEnhancing" class="w-3.5 h-3.5" />
                            <Icon v-else name="heroicons:sparkles" class="w-3.5 h-3.5 text-purple-500" />
                            {{ isEnhancing ? 'Enhancing...' : 'Enhance' }}
                        </button>
                        <button 
                            type="button"
                            @click="$emit('toggle-analyze')"
                            class="inline-flex items-center gap-1 px-2.5 py-1 
                                   bg-white border border-gray-200 rounded-full
                                   text-xs text-gray-500
                                   hover:bg-gray-50 hover:border-gray-300 hover:text-gray-700
                                   transition-all"
                        >
                            <Icon name="heroicons:chart-bar" class="w-3.5 h-3.5" />
                            Analyze
                        </button>
                    </div>
                </div>

                <!-- Horizontal Config Row -->
                <div class="flex flex-wrap items-center gap-2 p-2.5 bg-gray-50 rounded-lg">
                    <!-- Status -->
                    <USelectMenu 
                        v-model="instructionForm.status" 
                        :options="statusOptions" 
                        option-attribute="label" 
                        value-attribute="value" 
                        size="xs"
                        class="w-auto"
                    >
                        <template #label>
                            <span :class="getStatusClass(instructionForm.status)" class="inline-flex px-2 py-0.5 text-[11px] font-medium rounded-full">
                                {{ getCurrentStatusDisplayText() }}
                            </span>
                        </template>
                        <template #option="{ option }">
                            <span :class="getStatusClass(option.value)" class="inline-flex px-2 py-0.5 text-[11px] font-medium rounded-full">
                                {{ option.label }}
                            </span>
                        </template>
                    </USelectMenu>
                    
                    <!-- Category -->
                    <USelectMenu 
                        v-model="instructionForm.category" 
                        :options="categoryOptions" 
                        option-attribute="label" 
                        value-attribute="value" 
                        size="xs"
                        class="min-w-[120px]"
                    >
                        <template #label>
                            <div class="inline-flex items-center text-xs text-gray-700">
                                <Icon :name="getCategoryIcon(instructionForm.category)" class="w-3 h-3 mr-1" />
                                {{ formatCategory(instructionForm.category) }}
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center gap-1.5">
                                <Icon :name="getCategoryIcon(option.value)" class="w-3 h-3" />
                                <span class="text-xs">{{ option.label }}</span>
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- AI Context Loading -->
                    <USelectMenu 
                        v-model="instructionForm.load_mode" 
                        :options="loadModeOptions" 
                        option-attribute="label" 
                        value-attribute="value" 
                        size="xs"
                        class="w-auto"
                        :ui-menu="{ width: 'w-60' }"
                    >
                        <template #label>
                            <div class="inline-flex items-center text-xs text-gray-700">
                                <Icon :name="getLoadModeIcon(instructionForm.load_mode)" class="w-3 h-3 mr-1" />
                                {{ getLoadModeLabel(instructionForm.load_mode) }}
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex flex-col gap-0.5 py-0.5">
                                <div class="flex items-center gap-1.5">
                                    <Icon :name="getLoadModeIcon(option.value)" class="w-3 h-3" />
                                    <span class="text-xs font-medium">{{ option.label }}</span>
                                </div>
                                <span class="text-[10px] text-gray-500 ml-4">{{ option.description }}</span>
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- Labels -->
                    <USelectMenu
                        :model-value="selectedLabelIds"
                        @update:modelValue="handleLabelSelectionChange"
                        :options="labelSelectOptions"
                        option-attribute="name"
                        value-attribute="id"
                        multiple
                        size="xs"
                        class="flex-1 min-w-[120px]"
                        searchable
                        searchable-placeholder="Search labels..."
                    >
                        <template #label>
                            <div class="flex items-center flex-wrap gap-1">
                                <span v-if="selectedLabelObjects.length === 0" class="text-gray-500 text-xs">+ Labels</span>
                                <span
                                    v-for="label in selectedLabelObjects.slice(0, 2)"
                                    :key="label.id"
                                    class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px]"
                                    :style="{ backgroundColor: (label.color || '#94a3b8') + '20', color: '#1F2937' }"
                                >
                                    <span class="w-1.5 h-1.5 rounded-full" :style="{ backgroundColor: label.color || '#94a3b8' }"></span>
                                    {{ label.name }}
                                </span>
                                <span v-if="selectedLabelObjects.length > 2" class="text-[10px] text-gray-500">
                                    +{{ selectedLabelObjects.length - 2 }}
                                </span>
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div
                                v-if="option.__isAdd"
                                class="flex items-center w-full py-0.5 text-blue-600 hover:text-blue-800 cursor-pointer"
                                @mousedown.prevent
                                @click.stop="openAddLabelModal"
                            >
                                <Icon name="heroicons:plus" class="w-2.5 h-2.5 mr-1" />
                                <span class="text-[11px] font-medium">Add label</span>
                            </div>
                            <div v-else class="flex items-center w-full py-0.5 gap-1">
                                <span class="w-2 h-2 rounded-full flex-shrink-0" :style="{ backgroundColor: option.color || '#94a3b8' }"></span>
                                <div class="min-w-0 flex-1">
                                    <p class="text-[11px] font-medium text-gray-900 truncate">{{ option.name }}</p>
                                </div>
                                <button
                                    type="button"
                                    class="p-0.5 rounded hover:bg-gray-100 text-gray-400"
                                    @mousedown.prevent
                                    @click.stop="openEditLabelModal(option as InstructionLabel)"
                                >
                                    <Icon name="heroicons:pencil" class="w-2.5 h-2.5" />
                                </button>
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- Visibility Toggle -->
                    <UTooltip 
                        :text="instructionForm.is_seen ? 'Visible in instructions list' : 'Hidden from instructions list'"
                        :popper="{ placement: 'top' }"
                    >
                        <button
                            type="button"
                            @click="instructionForm.is_seen = !instructionForm.is_seen"
                            class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border transition-all"
                            :class="instructionForm.is_seen 
                                ? 'bg-white border-gray-200 text-gray-700 hover:bg-gray-50' 
                                : 'bg-gray-100 border-gray-300 text-gray-500 hover:bg-gray-200'"
                        >
                            <Icon :name="instructionForm.is_seen ? 'heroicons:eye' : 'heroicons:eye-slash'" class="w-3 h-3" />
                            <span>{{ instructionForm.is_seen ? 'Visible' : 'Hidden' }}</span>
                        </button>
                    </UTooltip>
                </div>

                <!-- Scope Row -->
                <div class="flex items-center gap-3">
                    <span class="text-[11px] text-gray-500 shrink-0">Scope:</span>
                    
                    <!-- Data Sources -->
                    <USelectMenu 
                        v-model="selectedDataSources" 
                        :options="dataSourceOptions" 
                        option-attribute="name"
                        value-attribute="id"
                        size="xs"
                        multiple
                        class="min-w-[200px]"
                    >
                        <template #label>
                            <span v-if="isAllDataSourcesSelected" class="text-xs text-gray-700">All sources</span>
                            <span v-else-if="selectedDataSources.length === 0" class="text-gray-400 text-xs">Sources</span>
                            <div v-else class="flex items-center gap-1 text-xs text-gray-700">
                                <DataSourceIcon :type="getSelectedDataSourceObjects[0]?.type" class="w-3 h-3" />
                                <span class="truncate max-w-[100px]">{{ getSelectedDataSourceObjects[0]?.name }}</span>
                                <span v-if="getSelectedDataSourceObjects.length > 1" class="text-gray-500">+{{ getSelectedDataSourceObjects.length - 1 }}</span>
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="flex items-center w-full py-0.5">
                                <div v-if="option.id === 'all'" class="flex -space-x-1 mr-1.5">
                                    <DataSourceIcon v-for="ds in availableDataSources.slice(0, 3)" :key="ds.id" :type="ds.type" class="h-3 w-3 border border-white rounded" />
                                </div>
                                <DataSourceIcon v-else :type="option.type" class="w-3 h-3 mr-1.5" />
                                <span class="text-xs">{{ option.name }}</span>
                            </div>
                        </template>
                    </USelectMenu>

                    <!-- Tables -->
                    <USelectMenu
                        :options="filteredMentionableOptions"
                        option-attribute="name"
                        value-attribute="id"
                        size="xs"
                        multiple
                        searchable
                        searchable-placeholder="Search tables..."
                        :model-value="selectedReferenceIds"
                        @update:model-value="handleReferencesChange"
                        class="min-w-[200px]"
                        :ui-menu="{ width: 'w-96', option: { base: 'py-1.5' } }"
                    >
                        <template #label>
                            <span v-if="selectedReferences.length === 0" class="text-gray-400 text-xs">Tables</span>
                            <div v-else class="flex items-center gap-1 text-xs text-gray-700">
                                <Icon name="heroicons:table-cells" class="w-3 h-3 text-gray-500" />
                                <span class="truncate max-w-[120px]">{{ selectedReferences[0].name }}</span>
                                <span v-if="selectedReferences.length > 1" class="text-gray-500">+{{ selectedReferences.length - 1 }}</span>
                            </div>
                        </template>
                        <template #option="{ option }">
                            <div class="w-full py-0.5">
                                <div class="flex items-center gap-1.5">
                                    <UIcon :name="getRefIcon(option.type)" class="w-3 h-3 text-gray-500 flex-shrink-0" />
                                    <span class="text-[11px] font-medium text-gray-900 break-all">{{ option.name }}</span>
                                </div>
                                <div class="flex items-center gap-1">
                                    <DataSourceIcon :type="option.data_source_type" class="w-2.5 h-2.5 flex-shrink-0" />
                                    <span class="text-[10px] text-gray-500 truncate">{{ option.data_source_name }}</span>
                                </div>
                            </div>
                        </template>
                    </USelectMenu>
                </div>

            </div>
            
            <!-- Form Actions (fixed at bottom) -->
            <div class="shrink-0 bg-white border-t px-5 py-3">
                <div class="flex justify-between items-center">
                    <!-- Delete button (only show when editing) -->
                    <UButton 
                        v-if="isEditing"
                        size="xs"
                        color="red" 
                        variant="ghost" 
                        @click="confirmDelete"
                        :loading="isDeleting"
                    >
                        <Icon name="heroicons:trash" class="w-3.5 h-3.5 mr-1" />
                        Delete
                    </UButton>
                    
                    <div class="flex gap-2" :class="{ 'ml-auto': !isEditing }">
                        <UButton v-if="isEditing" color="gray" variant="ghost" size="xs" @click="cancelEdit">
                            Cancel
                        </UButton>
                        <UButton v-else color="gray" variant="ghost" size="xs" @click="$emit('cancel')">
                            Cancel
                        </UButton>
                        <UButton 
                            type="submit" 
                            size="xs" 
                            color="blue"
                            :loading="isSubmitting"
                        >
                            {{ isEditing ? 'Update' : 'Create' }}
                        </UButton>
                    </div>
                </div>
            </div>
        </form>
    </div>

    <InstructionLabelFormModal
        v-model="showLabelModal"
        :label="editingLabel"
        @saved="handleLabelModalSaved"
        @deleted="handleLabelModalDeleted"
    />

    <!-- Unlink Confirmation Modal (for save) -->
    <UModal v-model="showUnlinkConfirm" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-4">
            <div class="flex items-start gap-3 mb-4">
                <div class="shrink-0 w-8 h-8 rounded-full bg-amber-100 flex items-center justify-center">
                    <Icon name="heroicons:link-slash" class="w-4 h-4 text-amber-600" />
                </div>
                <div>
                    <h3 class="text-sm font-semibold text-gray-900 mb-1">Unlink from Git?</h3>
                    <p class="text-sm text-gray-600">
                        This will stop automatic syncing from git. Your local changes will be preserved and can be pushed back to git later via <span class="font-medium">Push to Git</span>.
                    </p>
                </div>
            </div>
            <div class="flex justify-end gap-2">
                <UButton color="gray" variant="ghost" size="xs" @click="showUnlinkConfirm = false">
                    Cancel
                </UButton>
                <UButton color="blue" size="xs" @click="confirmUnlinkAndSave">
                    Unlink & Save
                </UButton>
            </div>
        </div>
    </UModal>

    <!-- Delete Confirmation Modal (non-git) -->
    <UModal v-model="showDeleteConfirm" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-4">
            <p class="text-sm text-gray-700 mb-3">
                Are you sure you want to delete this instruction?
            </p>
            <p class="text-xs text-gray-500 mb-4">
                This action cannot be undone.
            </p>
            <div class="flex justify-end gap-2">
                <UButton color="gray" variant="ghost" size="xs" @click="showDeleteConfirm = false">
                    Cancel
                </UButton>
                <UButton color="red" size="xs" @click="confirmDeleteNonGit">
                    Delete
                </UButton>
            </div>
        </div>
    </UModal>

    <!-- Delete Git-Synced Confirmation Modal -->
    <UModal v-model="showDeleteGitConfirm" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-4">
            <p class="text-sm text-gray-700 mb-3">
                This instruction is synced from git.
            </p>
            <div class="space-y-2 mb-4">
                <div class="flex items-start gap-2 text-xs text-gray-600">
                    <span class="text-red-500 font-medium shrink-0">Delete:</span>
                    <span>Will be recreated on next git sync</span>
                </div>
                <div class="flex items-start gap-2 text-xs text-gray-600">
                    <span class="text-orange-500 font-medium shrink-0">Unlink & Delete:</span>
                    <span>Will not be recreated (stops syncing)</span>
                </div>
            </div>
            <div class="flex justify-end gap-2">
                <UButton color="gray" variant="ghost" size="xs" @click="showDeleteGitConfirm = false">
                    Cancel
                </UButton>
                <UButton color="red" variant="soft" size="xs" @click="confirmDeleteGitSynced">
                    Delete
                </UButton>
                <UButton color="orange" size="xs" @click="confirmUnlinkAndDelete">
                    Unlink & Delete
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import InstructionLabelFormModal from '~/components/InstructionLabelFormModal.vue'
import GitBranchIcon from '~/components/icons/GitBranchIcon.vue'
import { useDomain } from '~/composables/useDomain'

// Define interfaces
interface DataSource {
    id: string
    name: string
    type: string
}

interface InstructionForm {
    text: string
    status: 'draft' | 'published' | 'archived'
    category: 'code_gen' | 'data_modeling' | 'general' | 'system' | 'visualizations' | 'dashboard'
    is_seen: boolean
    can_user_toggle: boolean
    
    // Unified Instructions System fields
    load_mode: 'always' | 'intelligent' | 'disabled'
}

interface InstructionLabel {
    id: string
    name: string
    color?: string | null
    description?: string | null
}

interface MentionableItem {
    id: string
    type: 'metadata_resource' | 'datasource_table'
    name: string
    data_source_id?: string
    column_name?: string | null
}

// Props and Emits
const props = defineProps<{
    instruction?: any
    analyzing?: boolean
    isGitSourced?: boolean
    isGitSynced?: boolean
    targetBuildId?: string  // If set, update instruction within this existing build (no new build created)
}>()

const emit = defineEmits(['instructionSaved', 'cancel', 'toggle-analyze', 'update-form', 'unlink-from-git', 'relink-to-git', 'view-mode-changed'])

// Reactive state
const toast = useToast()
const { selectedDomains: domainSelectedDomains, isAllDomains: isDomainAllSelected } = useDomain()
const isSubmitting = ref(false)
const isDeleting = ref(false)
const isEnhancing = ref(false)
const availableDataSources = ref<DataSource[]>([])
const selectedDataSources = ref<string[]>([])
const mentionableOptions = ref<MentionableItem[]>([])
const selectedReferences = ref<MentionableItem[]>([])
const availableLabels = ref<InstructionLabel[]>([])
const selectedLabelIds = ref<string[]>([])
const isLoadingLabels = ref(false)
const showLabelModal = ref(false)
const editingLabel = ref<InstructionLabel | null>(null)
const showUnlinkConfirm = ref(false)
const showDeleteConfirm = ref(false)
const showDeleteGitConfirm = ref(false)
const originalText = ref('')
const codeView = ref(false)
const isViewMode = ref(true)  // Start in view mode for existing instructions

// Form data (simplified - approval workflow handled by builds)
const instructionForm = ref<InstructionForm>({
    text: '',
    status: 'draft',
    category: 'general',
    is_seen: true,
    can_user_toggle: true,
    load_mode: 'always'
})

// Computed properties
const isEditing = computed(() => !!props.instruction)

// Get file path from instruction (git path or title)
const filePath = computed(() => {
    return props.instruction?.structured_data?.path || props.instruction?.title || null
})

// Get file extension from git path or title
const fileExtension = computed(() => {
    const path = filePath.value || ''
    const match = path.match(/\.([^.]+)$/)
    return match ? match[1].toLowerCase() : null
})

// Determine if content should be rendered as markdown
const shouldRenderAsMarkdown = computed(() => {
    // Render as markdown if:
    // 1. It's a .md file
    // 2. OR it's not git-linked (manually created instruction)
    if (fileExtension.value === 'md') return true
    if (!props.isGitSourced) return true
    return false
})

const dataSourceOptions = computed(() => {
    const allOption = {
        id: 'all',
        name: 'All Data Sources',
        type: 'all'
    }
    return [allOption, ...availableDataSources.value]
})

const isAllDataSourcesSelected = computed(() => {
    return selectedDataSources.value.includes('all') || selectedDataSources.value.length === 0
})

const getSelectedDataSourceObjects = computed(() => {
    return availableDataSources.value.filter(ds => selectedDataSources.value.includes(ds.id))
})

const selectedReferenceIds = computed(() => selectedReferences.value.map(r => r.id))

const labelSelectOptions = computed(() => {
    const base = availableLabels.value.map(label => ({ ...label }))
    return [
        ...base,
        {
            id: '__add__',
            name: 'Add label',
            color: undefined,
            description: undefined,
            __isAdd: true
        } as InstructionLabel & { __isAdd?: boolean }
    ]
})

const selectedLabelObjects = computed(() => {
    const lookup = availableLabels.value.reduce<Record<string, InstructionLabel>>((acc, label) => {
        acc[label.id] = label
        return acc
    }, {})
    return selectedLabelIds.value.map(id => lookup[id]).filter(Boolean)
})

const labelModalTitle = computed(() => editingLabel.value?.id ? 'Edit Label' : 'Add Label')

// Load mode options for dropdown
const loadModeOptions = [
    { value: 'always' as const, label: 'Always', description: 'Always included in AI context' },
    { value: 'intelligent' as const, label: 'Smart', description: 'Included only when relevant to the query' }
]

const getLoadModeIcon = (mode: string) => {
    const icons: Record<string, string> = {
        always: 'heroicons:bolt',
        intelligent: 'heroicons:sparkles'
    }
    return icons[mode] || 'heroicons:bolt'
}

const getLoadModeLabel = (mode: string) => {
    const labels: Record<string, string> = {
        always: 'Always',
        intelligent: 'Smart'
    }
    return labels[mode] || mode
}

// Filter mentionable options based on selected data sources
const filteredMentionableOptions = computed(() => {
    // If all data sources are selected (or none selected), show all references
    if (isAllDataSourcesSelected.value) {
        return mentionableOptions.value
    }
    
    // Otherwise, filter by selected data sources
    return mentionableOptions.value.filter(option => {
        // For metadata_resource and datasource_table, check data_source_id
        if (option.data_source_id) {
            return selectedDataSources.value.includes(option.data_source_id)
        }
        
        // If no data_source_id, include it (fallback)
        return true
    })
})

// Status options (simplified - no more suggestion workflow)
const statusOptions = computed(() => {
    return [
        { label: 'Draft', value: 'draft' },
        { label: 'Published', value: 'published' },
        { label: 'Archived', value: 'archived' }
    ]
})

// Helper function to get the right display text for the currently selected status
const getCurrentStatusDisplayText = () => {
    const currentStatus = instructionForm.value.status
    const statusMap: Record<string, string> = {
        draft: 'Draft',
        published: 'Published', 
        archived: 'Archived'
    }
    return statusMap[currentStatus] || formatStatus(currentStatus)
}

const enhanceInstruction = async () => {
    if (isEnhancing.value || !instructionForm.value.text?.trim()) return
    
    isEnhancing.value = true
    const payload = buildInstructionPayload()
    
    try {
        const response = await useMyFetch('/instructions/enhance', {
            method: 'POST',
            body: payload
        })
        if (response.status.value === 'success') {
            instructionForm.value.text = response.data.value as string
        } else {
            throw new Error('Enhance failed')
        }
    } catch (error) {
        console.error('Error enhancing instruction:', error)
        toast.add({
            title: 'Error',
            description: 'Failed to enhance instruction',
            color: 'red'
        })
    } finally {
        isEnhancing.value = false
    }
}

// Options for dropdowns
const categoryOptions = [
    { label: 'General', value: 'general' },
    { label: 'Code Generation', value: 'code_gen' },
    { label: 'Data Modeling', value: 'data_modeling' },
    { label: 'System', value: 'system' },
    { label: 'Visualizations', value: 'visualizations' },
    { label: 'Dashboard', value: 'dashboard' }
]

// Data source methods
const fetchDataSources = async () => {
    try {
        const { data, error } = await useMyFetch<DataSource[]>('/data_sources/active', {
            method: 'GET'
        })
        
        if (error.value) {
            console.error('Failed to fetch data sources:', error.value)
        } else if (data.value) {
            availableDataSources.value = data.value
        }
    } catch (err) {
        console.error('Error fetching data sources:', err)
    }
}

const handleDataSourceToggle = (dataSourceId: string) => {
    if (dataSourceId === 'all') {
        if (isAllDataSourcesSelected.value) {
            selectedDataSources.value = selectedDataSources.value.filter(id => id !== 'all')
        } else {
            selectedDataSources.value = ['all']
        }
    } else {
        selectedDataSources.value = selectedDataSources.value.filter(id => id !== 'all')
        
        if (selectedDataSources.value.includes(dataSourceId)) {
            selectedDataSources.value = selectedDataSources.value.filter(id => id !== dataSourceId)
        } else {
            selectedDataSources.value.push(dataSourceId)
        }
    }
}

// Helper functions
const formatStatus = (status: string) => {
    const statusMap = {
        draft: 'Draft',
        published: 'Published',
        archived: 'Archived'
    }
    return statusMap[status as keyof typeof statusMap] || status
}

const formatCategory = (category: string) => {
    const categoryMap = {
        code_gen: 'Code Generation',
        data_modeling: 'Data Modeling',
        general: 'General',
        system: 'System',
        visualizations: 'Visualizations',
        dashboard: 'Dashboard'
    }
    return categoryMap[category as keyof typeof categoryMap] || category
}

const getStatusClass = (status: string) => {
    const statusClasses = {
        draft: 'bg-yellow-100 text-yellow-800',
        published: 'bg-green-100 text-green-800',
        archived: 'bg-gray-100 text-gray-800'
    }
    return statusClasses[status as keyof typeof statusClasses] || 'bg-gray-100 text-gray-800'
}

const getCategoryIcon = (category: string) => {
    const categoryIcons = {
        code_gen: 'heroicons:code-bracket',
        data_modeling: 'heroicons:cube',
        general: 'heroicons:document-text',
        system: 'heroicons:cog-6-tooth',
        visualizations: 'heroicons:chart-bar',
        dashboard: 'heroicons:squares-2x2'
    }
    return categoryIcons[category as keyof typeof categoryIcons] || 'heroicons:document-text'
}

const getRefIcon = (type: string) => {
    if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
    if (type === 'datasource_table') return 'i-heroicons-table-cells'
    return 'i-heroicons-circle'
}

const handleReferencesChange = (ids: string[]) => {
    const idSet = new Set(ids)
    selectedReferences.value = filteredMentionableOptions.value.filter(m => idSet.has(m.id))
}

// Toggle a single reference id from checkbox interaction
const toggleReference = (id: string) => {
    const currentIds = new Set(selectedReferenceIds.value.map(String))
    if (currentIds.has(id)) {
        currentIds.delete(id)
    } else {
        currentIds.add(id)
    }
    handleReferencesChange(Array.from(currentIds))
}

// Validate references when data sources change
const validateSelectedReferences = () => {
    const validReferenceIds = new Set(filteredMentionableOptions.value.map(m => m.id))
    selectedReferences.value = selectedReferences.value.filter(ref => validReferenceIds.has(ref.id))
}

const fetchLabels = async () => {
    isLoadingLabels.value = true
    try {
        const { data, error } = await useMyFetch<InstructionLabel[]>('/instructions/labels', {
            method: 'GET'
        })
        if (!error.value && Array.isArray(data.value)) {
            availableLabels.value = data.value
        }
    } catch (err) {
        console.error('Error fetching instruction labels:', err)
    } finally {
        isLoadingLabels.value = false
    }
}

const handleLabelSelectionChange = (ids: string[]) => {
    const normalized = Array.isArray(ids) ? ids : []
    if (normalized.includes('__add__')) {
        openAddLabelModal()
    }
    const filtered = normalized.filter(id => id && id !== '__add__')
    selectedLabelIds.value = filtered
    emit('update-form', { label_ids: filtered })
}

const openAddLabelModal = () => {
    editingLabel.value = null
    showLabelModal.value = true
}

const openEditLabelModal = (label: InstructionLabel) => {
    if (!label?.id) return
    editingLabel.value = label
    showLabelModal.value = true
}

const handleLabelModalSaved = async (payload: { label: InstructionLabel | null; isNew: boolean }) => {
    const savedLabel = payload.label
    await fetchLabels()

    // If a new label was created while creating/editing an instruction, auto-select it
    if (payload.isNew && savedLabel?.id) {
        selectedLabelIds.value = Array.from(new Set([...selectedLabelIds.value, savedLabel.id]))
        emit('update-form', { label_ids: selectedLabelIds.value })
    }
}

const handleLabelModalDeleted = async (labelId: string) => {
    await fetchLabels()
    selectedLabelIds.value = selectedLabelIds.value.filter(id => id !== labelId)
    emit('update-form', { label_ids: selectedLabelIds.value })
}

const buildInstructionPayload = () => {
    const dataSourceIds = isAllDataSourcesSelected.value ? [] : selectedDataSources.value.slice()
    return {
        text: instructionForm.value.text,
        status: instructionForm.value.status,
        category: instructionForm.value.category,
        is_seen: instructionForm.value.is_seen,
        can_user_toggle: instructionForm.value.can_user_toggle,
        load_mode: instructionForm.value.load_mode,
        data_source_ids: dataSourceIds,
        label_ids: selectedLabelIds.value.slice(),
        references: selectedReferences.value.map(r => ({
            object_type: r.type,
            object_id: r.id,
            column_name: r.column_name || null,
            relation_type: 'scope'
        }))
    }
}

// Event handlers
const resetForm = () => {
    instructionForm.value = {
        text: '',
        status: 'draft',
        category: 'general',
        is_seen: true,
        can_user_toggle: true,
        load_mode: 'always'
    }
    // Use domain selection as initial scope for new instructions
    if (!isDomainAllSelected.value && domainSelectedDomains.value.length > 0) {
        selectedDataSources.value = [...domainSelectedDomains.value]
    } else {
        selectedDataSources.value = []
    }
    selectedReferences.value = []
    selectedLabelIds.value = []
    isSubmitting.value = false
    originalText.value = ''
    isViewMode.value = false  // New instructions start in edit mode
    emit('update-form', { label_ids: [] })
}

const hasTextChanged = computed(() => {
    return instructionForm.value.text !== originalText.value
})

// Cancel edit and return to view mode (restore original values)
const cancelEdit = () => {
    // Restore form to original instruction values
    if (props.instruction) {
        instructionForm.value = {
            text: props.instruction.text || '',
            status: props.instruction.status || 'draft',
            category: props.instruction.category || 'general',
            is_seen: props.instruction.is_seen !== undefined ? props.instruction.is_seen : true,
            can_user_toggle: props.instruction.can_user_toggle !== undefined ? props.instruction.can_user_toggle : true,
            load_mode: props.instruction.load_mode || 'always'
        }
        selectedDataSources.value = props.instruction.data_sources?.map((ds: DataSource) => ds.id) || []
        selectedLabelIds.value = props.instruction.labels?.map((label: InstructionLabel) => label.id) || []
    }
    isViewMode.value = true
}

const submitForm = async () => {
    if (isSubmitting.value) return
    
    // Check if instruction is linked and text was modified
    if (isEditing.value && props.isGitSynced && hasTextChanged.value) {
        showUnlinkConfirm.value = true
        return
    }
    
    await doSubmit()
}

const doSubmit = async () => {
    if (isSubmitting.value) return
    
    isSubmitting.value = true
    
    try {
        const basePayload = buildInstructionPayload()
        const payload: Record<string, any> = {
            ...basePayload
            // Approval workflow is handled by builds, not instruction status
        }
        
        // If target_build_id is provided, update within that existing build (no new build created)
        if (props.targetBuildId) {
            payload.target_build_id = props.targetBuildId
            console.log('[InstructionGlobalCreateComponent] Using target_build_id:', props.targetBuildId)
        } else {
            console.log('[InstructionGlobalCreateComponent] No target_build_id, will create new build')
        }

        let response
        if (isEditing.value) {
            response = await useMyFetch(`/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: payload
            })
        } else {
            // Use the global endpoint for new instructions
            response = await useMyFetch('/instructions/global', {
                method: 'POST',
                body: payload
            })
        }

        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: `Instruction ${isEditing.value ? 'updated' : 'created'} successfully`,
                color: 'green'
            })
            
            emit('instructionSaved', response.data.value)
            resetForm()
        } else {
            throw new Error('Failed to save instruction')
        }
    } catch (error) {
        console.error('Error saving instruction:', error)
        toast.add({
            title: 'Error',
            description: `Failed to ${isEditing.value ? 'update' : 'create'} instruction`,
            color: 'red'
        })
    } finally {
        isSubmitting.value = false
    }
}

const confirmUnlinkAndSave = async () => {
    showUnlinkConfirm.value = false
    
    // Unlink from git first
    if (props.instruction?.id) {
        try {
            await useMyFetch(`/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: { source_sync_enabled: false }
            })
        } catch (err) {
            console.error('Error unlinking from git:', err)
        }
    }
    
    // Now submit the form with updated content
    await doSubmit()
}

const confirmDelete = async () => {
    if (!props.instruction?.id) return
    
    // If git-synced, show special confirmation modal
    if (props.isGitSynced) {
        showDeleteGitConfirm.value = true
        return
    }
    
    // Show regular confirmation modal for non-git items
    showDeleteConfirm.value = true
}

const confirmDeleteNonGit = async () => {
    showDeleteConfirm.value = false
    await doDelete()
}

const confirmDeleteGitSynced = async () => {
    showDeleteGitConfirm.value = false
    await doDelete()
}

const confirmUnlinkAndDelete = async () => {
    showDeleteGitConfirm.value = false
    
    // Unlink from git first
    if (props.instruction?.id) {
        try {
            await useMyFetch(`/instructions/${props.instruction.id}`, {
                method: 'PUT',
                body: { source_sync_enabled: false }
            })
        } catch (err) {
            console.error('Error unlinking from git:', err)
        }
    }
    
    // Now delete
    await doDelete()
}

const doDelete = async () => {
    if (!props.instruction?.id) return
    
    isDeleting.value = true
    
    try {
        const response = await useMyFetch(`/instructions/${props.instruction.id}`, {
            method: 'DELETE'
        })
        
        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: 'Instruction deleted successfully',
                color: 'green'
            })
            
            emit('instructionSaved', { deleted: true, id: props.instruction.id })
            resetForm()
        } else {
            throw new Error('Failed to delete instruction')
        }
    } catch (error) {
        console.error('Error deleting instruction:', error)
        toast.add({
            title: 'Error',
            description: 'Failed to delete instruction',
            color: 'red'
        })
    } finally {
        isDeleting.value = false
    }
}

const fetchAvailableReferences = async () => {
    try {
        const { data, error } = await useMyFetch<MentionableItem[]>('/instructions/available-references', { method: 'GET' })
        if (!error.value && data.value) {
            mentionableOptions.value = data.value
        }
    } catch (err) {
        console.error('Error fetching available references:', err)
    }
}

// Full instruction data (fetched separately to get references)
const fullInstruction = ref<any>(null)

const fetchFullInstruction = async () => {
    if (!props.instruction?.id) return
    
    try {
        const { data, error } = await useMyFetch<any>(`/instructions/${props.instruction.id}`, { method: 'GET' })
        if (!error.value && data.value) {
            fullInstruction.value = data.value
        }
    } catch (err) {
        console.error('Error fetching full instruction:', err)
    }
}

const initReferencesFromInstruction = () => {
    // Use fullInstruction if available (has references), fallback to props.instruction
    const instruction = fullInstruction.value || props.instruction
    
    if (instruction && Array.isArray(instruction.references)) {
        const map: Record<string, MentionableItem> = {}
        for (const m of mentionableOptions.value) map[m.id] = m
        
        // Use a Set to deduplicate by object_id
        const seenObjectIds = new Set<string>()
        const preselected: MentionableItem[] = []
        
        for (const r of instruction.references) {
            // Skip duplicates
            if (seenObjectIds.has(r.object_id)) continue
            seenObjectIds.add(r.object_id)
            
            const existing = map[r.object_id]
            if (existing) {
                preselected.push({ ...existing, column_name: r.column_name || null })
            } else {
                // Fallback if not in options yet
                preselected.push({ id: r.object_id, type: r.object_type, name: r.display_text || r.object_type, column_name: r.column_name || null })
            }
        }
        selectedReferences.value = preselected
    }
}

// Lifecycle
onMounted(async () => {
    fetchDataSources()
    fetchLabels()
    // Fetch full instruction first (to get references), then available references, then init
    await fetchFullInstruction()
    await fetchAvailableReferences()
    initReferencesFromInstruction()
})

// Emit text changes upward so parent modal has current text for analysis
watch(() => instructionForm.value.text, (val) => {
    emit('update-form', { text: val })
})

watch(() => props.instruction, async (newInstruction) => {
    if (newInstruction) {
        instructionForm.value = {
            text: newInstruction.text || '',
            status: newInstruction.status || 'draft',
            category: newInstruction.category || 'general',
            is_seen: newInstruction.is_seen !== undefined ? newInstruction.is_seen : true,
            can_user_toggle: newInstruction.can_user_toggle !== undefined ? newInstruction.can_user_toggle : true,
            load_mode: newInstruction.load_mode || 'always'
        }
        // Store original text for change detection
        originalText.value = newInstruction.text || ''
        selectedDataSources.value = newInstruction.data_sources?.map((ds: DataSource) => ds.id) || []
        selectedLabelIds.value = newInstruction.labels?.map((label: InstructionLabel) => label.id) || []
        emit('update-form', { label_ids: selectedLabelIds.value })
        
        // Start in view mode for existing instructions
        isViewMode.value = true
        
        // Fetch full instruction to get references, then init
        await fetchFullInstruction()
        initReferencesFromInstruction()
    } else {
        fullInstruction.value = null
        // Start in edit mode for new instructions
        isViewMode.value = false
        resetForm()
    }
}, { immediate: true })

// Validate references when data sources change
watch(() => selectedDataSources.value, () => {
    validateSelectedReferences()
}, { deep: true })

watch(showLabelModal, (isOpen) => {
    if (!isOpen) {
        editingLabel.value = null
    }
})

// Emit view mode changes so parent can update the modal title
watch(isViewMode, (newVal) => {
    emit('view-mode-changed', newVal)
}, { immediate: true })
</script>

<style scoped>
/* Markdown wrapper styles for instruction content */
.markdown-wrapper :deep(.markdown-content) {
    @apply leading-relaxed text-sm text-gray-800;

    p {
        margin-bottom: 1em;
    }
    p:last-child {
        margin-bottom: 0;
    }

    :where(h1, h2, h3, h4, h5, h6) {
        @apply font-semibold mb-3 mt-4 text-gray-900;
    }

    h1 { @apply text-xl; }
    h2 { @apply text-lg; }
    h3 { @apply text-base; }
    h4 { @apply text-sm; }

    /* Prevent anchor links inside headings from looking like links - needs high specificity */
    h1 a, h2 a, h3 a, h4 a, h5 a, h6 a {
        color: inherit !important;
        text-decoration: none !important;
    }

    ul, ol { @apply pl-5 mb-3; }
    ul { @apply list-disc; }
    ol { @apply list-decimal; }
    li { @apply mb-1; }

    /* Code blocks (fenced with ```) */
    pre {
        @apply bg-gray-50 p-3 rounded-lg mb-3 overflow-x-auto text-xs;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    pre code {
        background: none;
        padding: 0;
        border-radius: 0;
        font-size: 12px;
        line-height: 1.5;
        display: block;
        white-space: pre-wrap;
        word-wrap: break-word;
    }
    /* Inline code (single backticks) */
    code {
        @apply bg-gray-100 px-1.5 py-0.5 rounded font-mono text-xs;
        color: #374151;
    }
    
    /* Regular links - but not inside headings */
    a { 
        @apply text-blue-600 hover:text-blue-800 underline;
    }
    
    blockquote { 
        @apply border-l-4 border-gray-200 pl-4 italic my-3 text-gray-600; 
    }
    
    table { @apply w-full border-collapse mb-3; }
    table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
    
    hr {
        @apply my-4 border-gray-200;
    }

    strong {
        @apply font-semibold;
    }

    em {
        @apply italic;
    }
}
</style>