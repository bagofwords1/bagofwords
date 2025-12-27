<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }">
        <UCard :ui="{ body: { padding: '' }, header: { padding: 'px-4 py-3' } }">
            <!-- Header -->
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-base font-semibold text-gray-900">
                        {{ suggestionsMode ? 'Pending Review' : 'Version Explorer' }}
                    </h3>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark-20-solid"
                        size="xs"
                        @click="close"
                    />
                </div>
            </template>

            <!-- Two-pane layout -->
            <div class="flex h-[600px]">
                <!-- Left Pane: Builds List -->
                <div class="w-56 border-r border-gray-200 flex flex-col bg-gray-50/50">
                    <div class="px-3 py-2 border-b border-gray-200 bg-white">
                        <span class="text-xs font-medium text-gray-600">{{ suggestionsMode ? 'Pending' : 'Builds' }}</span>
                    </div>
                    
                    <!-- Loading builds -->
                    <div v-if="loadingBuilds" class="flex-1 flex items-center justify-center">
                        <Spinner class="w-5 h-5 text-gray-400" />
                    </div>
                    
                    <!-- Builds list -->
                    <div v-else class="flex-1 overflow-y-auto">
                        <div v-if="!builds.length" class="p-3 text-xs text-gray-400 text-center">
                            {{ suggestionsMode ? 'No pending suggestions' : 'No builds found' }}
                        </div>
                        <button
                            v-for="build in builds"
                            :key="build.id"
                            @click="selectBuild(build)"
                            class="w-full px-3 py-2 text-left border-b border-gray-100 hover:bg-white transition-colors"
                            :class="{ 'bg-white border-l-2 border-l-blue-500': selectedBuild?.id === build.id }"
                        >
                            <div class="flex items-center justify-between">
                                <div class="flex items-center gap-1.5 min-w-0">
                                    <span class="text-xs font-medium text-gray-800 truncate">{{ build.title || `Build #${build.build_number}` }}</span>
                                    <!-- Git indicator with tooltip -->
                                    <UTooltip 
                                        v-if="build.git_pr_url" 
                                        :text="`PR: ${build.git_pr_url}`"
                                        :popper="{ placement: 'top' }"
                                    >
                                        <a 
                                            :href="build.git_pr_url" 
                                            target="_blank" 
                                            @click.stop
                                            class="text-purple-500 hover:text-purple-700"
                                        >
                                            <GitBranchIcon class="w-3 h-3" />
                                        </a>
                                    </UTooltip>
                                    <UTooltip 
                                        v-else-if="build.git_pushed_at || build.git_branch_name" 
                                        :text="`Branch: ${build.git_branch_name || 'pushed'}`"
                                        :popper="{ placement: 'top' }"
                                    >
                                        <GitBranchIcon class="w-3 h-3 text-gray-400" />
                                    </UTooltip>
                                </div>
                                <span v-if="build.is_main" class="text-[9px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded shrink-0">Active</span>
                                <span v-else-if="build.status === 'pending_approval'" class="text-[9px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded shrink-0">Pending</span>
                                <span v-else-if="build.status === 'rejected'" class="text-[9px] px-1.5 py-0.5 bg-red-100 text-red-700 rounded shrink-0">Rejected</span>
                            </div>
                            <div class="text-[10px] text-gray-500 mt-0.5 flex items-center gap-1.5">
                                <span>#{{ build.build_number }}</span>
                                <span>•</span>
                                <span>{{ formatDate(build.created_at) }}</span>
                                <span v-if="build.created_by_user_name" class="truncate">• {{ build.created_by_user_name }}</span>
                            </div>
                            <div class="flex items-center justify-between mt-0.5">
                                <span class="text-[10px] text-gray-400">
                                    {{ build.total_instructions || 0 }} instructions
                                </span>
                                <!-- Test results indicator -->
                                <div v-if="build.test_passed != null || build.test_failed != null" class="flex items-center gap-1 text-[9px]">
                                    <span class="flex items-center gap-0.5 text-green-600">
                                        <UIcon name="i-heroicons-check" class="w-2.5 h-2.5" />
                                        {{ build.test_passed ?? 0 }}
                                    </span>
                                    <span class="flex items-center gap-0.5 text-red-500">
                                        <UIcon name="i-heroicons-x-mark" class="w-2.5 h-2.5" />
                                        {{ build.test_failed ?? 0 }}
                                    </span>
                                </div>
                            </div>
                        </button>
                    </div>
                </div>

                <!-- Right Pane: Build Details -->
                <div class="flex-1 flex flex-col min-w-0">
                    <!-- No build selected -->
                    <div v-if="!selectedBuild" class="flex-1 flex items-center justify-center">
                        <div class="text-center text-gray-400">
                            <UIcon name="i-heroicons-document-magnifying-glass" class="w-8 h-8 mx-auto mb-2" />
                            <p class="text-sm">Select a build to view details</p>
                        </div>
                    </div>

                    <template v-else>
                        <!-- Build Header -->
                        <div class="px-4 py-3 border-b border-gray-200 bg-white shrink-0">
                            <div class="flex items-center justify-between">
                                <div>
                                    <h4 class="text-sm font-semibold text-gray-900">
                                        {{ selectedBuild.title || `Build #${selectedBuild.build_number}` }}
                                    </h4>
                                    <div class="flex items-center gap-2 text-[10px] text-gray-500 mt-0.5">
                                        <span>#{{ selectedBuild.build_number }} • {{ formatDateTime(selectedBuild.created_at) }}</span>
                                        <span v-if="selectedBuild.created_by_user_name" class="flex items-center gap-1">
                                            <UIcon name="i-heroicons-user" class="w-2.5 h-2.5" />
                                            {{ selectedBuild.created_by_user_name }}
                                        </span>
                                        <span v-if="selectedBuild.approved_by_user_name" class="flex items-center gap-1 text-green-600">
                                            <UIcon name="i-heroicons-check-badge" class="w-2.5 h-2.5" />
                                            Approved by {{ selectedBuild.approved_by_user_name }}
                                        </span>
                                    </div>
                                    <div class="flex items-center gap-1.5 text-[10px] text-gray-400 mt-0.5">
                                        <span>Build ID:</span>
                                        <button 
                                            @click="copyBuildId"
                                            class="font-mono text-gray-500 hover:text-blue-600 hover:underline cursor-pointer transition-colors"
                                            title="Click to copy"
                                        >
                                            {{ selectedBuild.id }}
                                        </button>
                                    </div>
                                </div>
                                <div class="flex items-center gap-2">
                                    <span v-if="selectedBuild.is_main" class="text-[10px] px-2 py-0.5 bg-green-100 text-green-700 rounded-full">
                                        Active
                                    </span>
                                    <span v-else-if="selectedBuild.status === 'pending_approval'" class="text-[10px] px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full">
                                        Pending Approval
                                    </span>
                                    <span v-else-if="selectedBuild.status === 'rejected'" class="text-[10px] px-2 py-0.5 bg-red-100 text-red-700 rounded-full">
                                        Rejected
                                    </span>
                                    
                                    <!-- Git Status Badge -->
                                    <a 
                                        v-if="selectedBuild.git_pr_url"
                                        :href="selectedBuild.git_pr_url"
                                        target="_blank"
                                        class="text-[10px] px-2 py-0.5 bg-purple-100 text-purple-700 rounded-full hover:bg-purple-200 transition-colors flex items-center gap-1"
                                    >
                                        <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-2.5 h-2.5" />
                                        PR
                                    </a>
                                    <span 
                                        v-else-if="selectedBuild.git_branch_name"
                                        class="text-[10px] px-2 py-0.5 bg-gray-100 text-gray-600 rounded-full flex items-center gap-1"
                                    >
                                        <UIcon name="i-heroicons-code-bracket" class="w-2.5 h-2.5" />
                                        {{ selectedBuild.git_branch_name }}
                                    </span>
                                    
                                    <!-- Push to Git button - only for non-git-sourced builds -->
                                    <UButton
                                        v-if="gitRepoId && canCreateBuilds && !selectedBuild.git_pushed_at && selectedBuild.source !== 'git'"
                                        color="gray"
                                        variant="soft"
                                        size="xs"
                                        :icon="pushingToGit ? undefined : 'i-heroicons-cloud-arrow-up'"
                                        :loading="pushingToGit"
                                        @click="pushToGit"
                                    >
                                        Push to Git
                                    </UButton>
                                    
                                    <!-- Publish button - only for unpublished builds (draft or pending_approval) -->
                                    <UButton
                                        v-if="!selectedBuild.is_main && (selectedBuild.status === 'draft' || selectedBuild.status === 'pending_approval') && canCreateBuilds"
                                        color="blue"
                                        variant="solid"
                                        size="xs"
                                        :icon="publishingBuild ? undefined : 'i-heroicons-rocket-launch'"
                                        :loading="publishingBuild"
                                        @click="publishBuild"
                                    >
                                        Publish
                                    </UButton>
                                    
                                    <!-- Reject button - only for pending_approval builds -->
                                    <UButton
                                        v-if="selectedBuild.status === 'pending_approval' && canCreateBuilds"
                                        color="red"
                                        variant="soft"
                                        size="xs"
                                        :icon="rejectingBuild ? undefined : 'i-heroicons-x-mark'"
                                        :loading="rejectingBuild"
                                        @click="rejectBuild"
                                    >
                                        Reject
                                    </UButton>
                                    
                                    <!-- Rollback button - only show for non-main approved builds with permission -->
                                    <UButton
                                        v-if="!selectedBuild.is_main && selectedBuild.status === 'approved' && canCreateBuilds"
                                        color="amber"
                                        variant="soft"
                                        size="xs"
                                        :icon="rollingBack ? undefined : 'i-heroicons-arrow-path'"
                                        :loading="rollingBack"
                                        @click="rollbackToBuild"
                                    >
                                        Rollback to this version
                                    </UButton>
                                </div>
                            </div>
                        </div>

                        <!-- Loading State -->
                        <div v-if="loadingBuildContent" class="flex-1 flex items-center justify-center">
                            <div class="text-center">
                                <Spinner class="w-6 h-6 text-gray-400 mx-auto mb-2" />
                                <p class="text-xs text-gray-500">Loading build content...</p>
                            </div>
                        </div>

                        <!-- Diff Section (if previous build exists) -->
                        <div v-if="!loadingBuildContent && diffData && hasDiffChanges" class="border-b border-gray-200 bg-gradient-to-r from-gray-50 to-white shrink-0">
                            <button
                                @click="diffExpanded = !diffExpanded"
                                class="w-full px-4 py-2 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
                            >
                                <div class="flex items-center gap-2">
                                    <UIcon 
                                        :name="diffExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                        class="w-3 h-3 text-gray-400"
                                    />
                                    <span class="text-xs font-medium text-gray-700">Changes (from Build #{{ diffData.build_a_number }})</span>
                                </div>
                                <div class="flex gap-1.5">
                                    <span v-if="diffData.added_count" class="text-[9px] px-1.5 py-0.5 bg-green-100 text-green-700 rounded">
                                        +{{ diffData.added_count }}
                                    </span>
                                    <span v-if="diffData.modified_count" class="text-[9px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded">
                                        ~{{ diffData.modified_count }}
                                    </span>
                                    <span v-if="diffData.removed_count" class="text-[9px] px-1.5 py-0.5 bg-red-100 text-red-700 rounded">
                                        −{{ diffData.removed_count }}
                                    </span>
                                </div>
                            </button>
                            
                            <!-- Expanded diff content -->
                            <div v-if="diffExpanded" class="px-4 pb-3 space-y-2">
                                <!-- Added -->
                                <div v-if="addedItems.length" class="space-y-1">
                                    <div class="text-[10px] font-medium text-green-700">Added</div>
                                    <div 
                                        v-for="item in addedItems.slice(0, 5)" 
                                        :key="item.instruction_id"
                                        class="group flex items-center justify-between border-l-2 border-l-green-400 bg-green-50/30 pl-2 pr-1 py-1 rounded-r"
                                    >
                                        <span class="text-[10px] text-gray-700 truncate flex-1 min-w-0">
                                            {{ truncateText(item.title || item.text, 70) }}
                                        </span>
                                        <div class="flex items-center gap-1 shrink-0">
                                            <button
                                                v-if="isBuildEditable"
                                                @click="openEditInstruction(item)"
                                                class="flex items-center gap-0.5 px-1.5 py-0.5 text-[9px] font-medium text-blue-600 hover:text-blue-700 hover:bg-blue-50 rounded transition-colors"
                                                title="Edit instruction"
                                            >
                                                <UIcon name="i-heroicons-pencil" class="w-3 h-3" />
                                                Edit
                                            </button>
                                            <button
                                                v-if="isBuildEditable"
                                                @click="removeChange(item)"
                                                class="flex items-center gap-0.5 px-1.5 py-0.5 text-[9px] font-medium text-red-600 hover:text-red-700 hover:bg-red-50 rounded transition-colors"
                                                title="Remove this addition"
                                            >
                                                <UIcon name="i-heroicons-x-mark" class="w-3 h-3" />
                                                Remove
                                            </button>
                                        </div>
                                    </div>
                                    <div v-if="addedItems.length > 5" class="text-[10px] text-gray-400 pl-2">
                                        +{{ addedItems.length - 5 }} more
                                    </div>
                                </div>
                                
                                <!-- Modified -->
                                <div v-if="modifiedItems.length" class="space-y-1">
                                    <div class="text-[10px] font-medium text-amber-700">Modified</div>
                                    <div 
                                        v-for="item in modifiedItems.slice(0, 5)" 
                                        :key="item.instruction_id"
                                        class="group border-l-2 border-l-amber-400 bg-amber-50/30 pl-2 pr-1 py-1.5 rounded-r"
                                    >
                                        <div class="flex items-center justify-between">
                                            <span class="text-[10px] text-gray-700 font-medium truncate flex-1 min-w-0">
                                                {{ truncateText(item.title || item.text, 70) }}
                                            </span>
                                            <div class="flex items-center gap-1 shrink-0">
                                                <button
                                                    v-if="isBuildEditable"
                                                    @click="openEditInstruction(item)"
                                                    class="flex items-center gap-0.5 px-1.5 py-0.5 text-[9px] font-medium text-blue-600 hover:text-blue-700 hover:bg-blue-50 rounded transition-colors"
                                                    title="Edit instruction"
                                                >
                                                    <UIcon name="i-heroicons-pencil" class="w-3 h-3" />
                                                    Edit
                                                </button>
                                                <button
                                                    v-if="isBuildEditable"
                                                    @click="removeChange(item)"
                                                    class="flex items-center gap-0.5 px-1.5 py-0.5 text-[9px] font-medium text-amber-600 hover:text-amber-700 hover:bg-amber-50 rounded transition-colors"
                                                    title="Revert this change"
                                                >
                                                    <UIcon name="i-heroicons-arrow-uturn-left" class="w-3 h-3" />
                                                    Revert
                                                </button>
                                            </div>
                                        </div>
                                        <!-- Show what changed -->
                                        <div class="flex flex-wrap gap-1 mt-1">
                                            <span 
                                                v-if="item.changed_fields?.includes('text')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-amber-100 text-amber-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-document-text" class="w-2.5 h-2.5" />
                                                text
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('status')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-blue-100 text-blue-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-flag" class="w-2.5 h-2.5" />
                                                {{ item.previous_status }} → {{ item.status }}
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('load_mode')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-purple-100 text-purple-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-bolt" class="w-2.5 h-2.5" />
                                                {{ formatLoadMode(item.previous_load_mode) }} → {{ formatLoadMode(item.load_mode) }}
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('category')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-gray-100 text-gray-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-tag" class="w-2.5 h-2.5" />
                                                category
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('references')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-teal-100 text-teal-700 rounded text-[9px]"
                                            >
                                                <UIcon name="i-heroicons-table-cells" class="w-2.5 h-2.5" />
                                                references
                                                <template v-if="item.references_added">+{{ item.references_added }}</template>
                                                <template v-if="item.references_removed">-{{ item.references_removed }}</template>
                                            </span>
                                            <span 
                                                v-if="item.changed_fields?.includes('title')"
                                                class="inline-flex items-center gap-0.5 px-1 py-0.5 bg-gray-100 text-gray-600 rounded text-[9px]"
                                            >
                                                title
                                            </span>
                                        </div>
                                    </div>
                                    <div v-if="modifiedItems.length > 5" class="text-[10px] text-gray-400 pl-2">
                                        +{{ modifiedItems.length - 5 }} more
                                    </div>
                                </div>
                                
                                <!-- Removed -->
                                <div v-if="removedItems.length" class="space-y-1">
                                    <div class="text-[10px] font-medium text-red-700">Removed</div>
                                    <div 
                                        v-for="item in removedItems.slice(0, 5)" 
                                        :key="item.instruction_id"
                                        class="group flex items-center justify-between border-l-2 border-l-red-400 bg-red-50/30 pl-2 pr-1 py-1 rounded-r"
                                    >
                                        <span class="text-[10px] text-gray-700 truncate">
                                            {{ truncateText(item.title || item.text, 70) }}
                                        </span>
                                        <button
                                            v-if="isBuildEditable"
                                            @click="removeChange(item)"
                                            class="flex items-center gap-0.5 px-1.5 py-0.5 text-[9px] font-medium text-green-600 hover:text-green-700 hover:bg-green-50 rounded transition-colors"
                                            title="Restore this instruction"
                                        >
                                            <UIcon name="i-heroicons-arrow-uturn-left" class="w-3 h-3" />
                                            Restore
                                        </button>
                                    </div>
                                    <div v-if="removedItems.length > 5" class="text-[10px] text-gray-400 pl-2">
                                        +{{ removedItems.length - 5 }} more
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Evals Section - only shown for users with manage_tests permission -->
                        <div v-if="!loadingBuildContent && canManageTests" class="border-b border-gray-100 shrink-0">
                            <!-- Evals Header -->
                            <button
                                @click="evalsExpanded = !evalsExpanded"
                                class="w-full px-4 py-2 flex items-center justify-between hover:bg-gray-50/50 transition-colors border-b border-gray-100 shrink-0 bg-gray-50/50"
                            >
                                <div class="flex items-center gap-2">
                                    <UIcon 
                                        :name="evalsExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                        class="w-3 h-3 text-gray-400"
                                    />
                                    <span class="text-xs font-medium text-gray-700">Evals</span>
                                    <!-- Show test status badge if available -->
                                    <span 
                                        v-if="selectedBuild?.test_status"
                                        class="text-[9px] px-1.5 py-0.5 rounded"
                                        :class="{
                                            'bg-green-100 text-green-700': selectedBuild.test_status === 'passed',
                                            'bg-red-100 text-red-700': selectedBuild.test_status === 'failed',
                                            'bg-amber-100 text-amber-700': selectedBuild.test_status === 'pending'
                                        }"
                                    >
                                        {{ selectedBuild.test_status }}
                                    </span>
                                </div>
                            </button>
                            
                            <!-- Evals Content -->
                            <div v-if="evalsExpanded" class="p-3 space-y-3">
                                <!-- No test suites at all -->
                                <div v-if="!loadingTestSuites && testSuites.length === 0" class="text-center py-3">
                                    <p class="text-xs text-gray-500">
                                        No test cases have been found, create in 
                                        <NuxtLink to="/evals" class="text-blue-600 hover:text-blue-700 hover:underline">/evals</NuxtLink>
                                    </p>
                                </div>

                                <!-- Test Suite Selector & Run Button (only show if suites exist) -->
                                <template v-else>
                                    <div class="flex items-center gap-2">
                                        <USelectMenu
                                            v-model="selectedSuiteId"
                                            :options="testSuiteOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            placeholder="Select test suite..."
                                            size="xs"
                                            class="flex-1"
                                            :loading="loadingTestSuites"
                                            :ui="{ width: 'w-full' }"
                                        >
                                            <template #leading>
                                                <UIcon name="i-heroicons-beaker" class="w-3.5 h-3.5 text-gray-400" />
                                            </template>
                                        </USelectMenu>
                                        
                                        <UButton
                                            color="blue"
                                            size="xs"
                                            :loading="runningEval"
                                            :disabled="!selectedSuiteId || runningEval || !hasTestCases"
                                            @click="runEval"
                                        >
                                            <template #leading>
                                                <Spinner v-if="runningEval" class="w-3 h-3" />
                                                <UIcon v-else name="i-heroicons-play" class="w-3 h-3" />
                                            </template>
                                            Run
                                        </UButton>
                                    </div>
                                    
                                    <!-- No test cases in selected suite -->
                                    <div v-if="selectedSuiteId && !hasTestCases && !loadingTestSuites" class="text-center py-2">
                                        <p class="text-xs text-gray-500">
                                            No test cases have been found, create in 
                                            <NuxtLink to="/evals" class="text-blue-600 hover:text-blue-700 hover:underline">/evals</NuxtLink>
                                        </p>
                                    </div>

                                    <!-- Current/Active Test Run -->
                                    <div v-if="activeTestRun" class="bg-white border border-gray-200 rounded-lg p-3 space-y-2">
                                        <div class="flex items-center justify-between">
                                            <div class="flex items-center gap-2">
                                                <Spinner v-if="activeTestRun.status === 'in_progress'" class="w-3.5 h-3.5 text-blue-500" />
                                                <UIcon 
                                                    v-else-if="activeTestRun.status === 'success'" 
                                                    name="i-heroicons-check-circle" 
                                                    class="w-3.5 h-3.5 text-green-500" 
                                                />
                                                <UIcon 
                                                    v-else 
                                                    name="i-heroicons-x-circle" 
                                                    class="w-3.5 h-3.5 text-red-500" 
                                                />
                                                <span class="text-xs font-medium text-gray-700">
                                                    {{ activeTestRun.title || 'Test Run' }}
                                                </span>
                                            </div>
                                            <span 
                                                class="text-[9px] px-1.5 py-0.5 rounded-full"
                                                :class="{
                                                    'bg-blue-100 text-blue-700': activeTestRun.status === 'in_progress',
                                                    'bg-green-100 text-green-700': activeTestRun.status === 'success',
                                                    'bg-red-100 text-red-700': activeTestRun.status === 'error' || activeTestRun.status === 'fail'
                                                }"
                                            >
                                                {{ prettyStatus(activeTestRun.status) }}
                                            </span>
                                        </div>

                                        <!-- Test Results Summary -->
                                        <div class="flex flex-wrap items-center gap-1.5 text-[10px]">
                                            <span class="inline-flex items-center px-1.5 py-0.5 rounded border bg-slate-50 text-slate-600 border-slate-200">
                                                Cases: {{ testResultsSummary.total }}
                                            </span>
                                            <span class="inline-flex items-center px-1.5 py-0.5 rounded border bg-green-50 text-green-700 border-green-200">
                                                Pass: {{ testResultsSummary.passed }}
                                            </span>
                                            <span class="inline-flex items-center px-1.5 py-0.5 rounded border bg-red-50 text-red-700 border-red-200">
                                                Fail: {{ testResultsSummary.failed }}
                                            </span>
                                            <span v-if="testResultsSummary.inProgress > 0" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-200">
                                                <Spinner class="w-2.5 h-2.5" />
                                                Running: {{ testResultsSummary.inProgress }}
                                            </span>
                                        </div>

                                        <!-- Progress bar -->
                                        <div v-if="activeTestRun.status === 'in_progress'" class="w-full bg-gray-100 rounded-full h-1.5">
                                            <div 
                                                class="bg-blue-500 h-1.5 rounded-full transition-all duration-300"
                                                :style="{ width: `${testResultsSummary.progressPercent}%` }"
                                            />
                                        </div>

                                        <!-- Link to full results -->
                                        <div class="flex items-center justify-between pt-1">
                                            <span class="text-[10px] text-gray-400">
                                                Started {{ formatTimeAgo(activeTestRun.started_at || activeTestRun.created_at) }}
                                            </span>
                                            <NuxtLink 
                                                :to="`/evals/runs/${activeTestRun.id}`" 
                                                class="text-[10px] text-blue-500 hover:underline flex items-center gap-0.5"
                                            >
                                                View details
                                                <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-2.5 h-2.5" />
                                            </NuxtLink>
                                        </div>
                                    </div>

                                    <!-- Previous test runs for this build -->
                                    <div v-if="buildTestRuns.length > 0 && (!activeTestRun || buildTestRuns.length > 1)">
                                        <div class="text-[10px] font-medium text-gray-500 mb-1.5">Previous Runs</div>
                                        <div class="space-y-1">
                                            <div 
                                                v-for="run in displayedTestRuns" 
                                                :key="run.id"
                                                class="flex items-center justify-between px-2 py-1.5 bg-gray-50 rounded text-[10px] hover:bg-gray-100 transition-colors"
                                            >
                                                <div class="flex items-center gap-1.5">
                                                    <UIcon 
                                                        :name="run.status === 'success' ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" 
                                                        :class="run.status === 'success' ? 'text-green-500' : 'text-red-500'"
                                                        class="w-3 h-3" 
                                                    />
                                                    <span class="text-gray-600">{{ run.title || 'Test Run' }}</span>
                                                </div>
                                                <div class="flex items-center gap-2">
                                                    <span class="text-gray-400">{{ formatTimeAgo(run.created_at) }}</span>
                                                    <NuxtLink 
                                                        :to="`/evals/runs/${run.id}`" 
                                                        class="text-blue-500 hover:underline"
                                                    >
                                                        View
                                                    </NuxtLink>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </template>
                            </div>
                        </div>

                        <!-- Instructions Section (Collapsible) -->
                        <div v-if="!loadingBuildContent" class="flex-1 overflow-hidden flex flex-col min-h-0">
                            <!-- Instructions Header -->
                            <button
                                @click="instructionsExpanded = !instructionsExpanded"
                                class="w-full px-4 py-2 flex items-center justify-between hover:bg-gray-50/50 transition-colors border-b border-gray-100 shrink-0 bg-gray-50/50"
                            >
                                <div class="flex items-center gap-2">
                                    <UIcon 
                                        :name="instructionsExpanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                        class="w-3 h-3 text-gray-400"
                                    />
                                    <span class="text-xs font-medium text-gray-700">Instructions</span>
                                    <span class="text-[10px] text-gray-400">({{ totalInstructions }})</span>
                                </div>
                            </button>
                            
                            <!-- Instructions Content -->
                            <div v-if="instructionsExpanded" class="flex-1 overflow-auto p-3">
                                <div class="bg-white border border-gray-200 rounded-lg overflow-hidden">
                                    <InstructionsTable
                                        :instructions="instructions"
                                        :loading="loadingInstructions"
                                        :compact="true"
                                        :show-source="true"
                                        :show-category="true"
                                        :show-data-source="false"
                                        :show-load-mode="true"
                                        :show-labels="false"
                                        :show-status="true"
                                        :show-pagination="totalInstructions > pageSize"
                                        :current-page="currentPage"
                                        :page-size="pageSize"
                                        :total-items="totalInstructions"
                                        :total-pages="totalPages"
                                        :visible-pages="visiblePages"
                                        empty-title="No instructions"
                                        empty-message="This build contains no instructions."
                                        @page-change="handlePageChange"
                                        @click="handleInstructionClick"
                                    />
                                </div>
                            </div>
                        </div>
                    </template>
                </div>
            </div>
        </UCard>
    </UModal>
    
    <!-- Edit Instruction Modal -->
    <UModal v-model="showEditInstruction" :ui="{ width: 'sm:max-w-2xl' }">
        <UCard :ui="{ body: { padding: '' }, header: { padding: 'px-4 py-3' } }">
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-sm font-semibold text-gray-900">Edit Instruction</h3>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark"
                        size="xs"
                        @click="closeEditInstruction"
                    />
                </div>
            </template>
            <InstructionGlobalCreateComponent
                v-if="editingInstruction"
                :instruction="editingInstruction"
                :target-build-id="editTargetBuildId"
                @instructionSaved="onInstructionSaved"
                @cancel="closeEditInstruction"
            />
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import InstructionsTable from '~/components/instructions/InstructionsTable.vue'
import GitBranchIcon from '~/components/icons/GitBranchIcon.vue'
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import type { Instruction } from '~/composables/useInstructionHelpers'
import { useCan } from '~/composables/usePermissions'

interface Build {
    id: string
    build_number: number
    title?: string  // Auto-generated or user-provided title
    is_main: boolean
    status: string
    source?: string  // 'user' | 'git' | 'ai'
    base_build_id?: string  // The build this was forked from (for diff comparison)
    created_at: string
    total_instructions?: number
    added_count?: number
    modified_count?: number
    removed_count?: number
    git_branch_name?: string
    git_pr_url?: string
    git_pushed_at?: string
    // Test integration
    test_run_id?: string
    test_status?: 'pending' | 'passed' | 'failed'
    // Test summary from linked test run
    test_passed?: number
    test_failed?: number
    // User info
    created_by_user_id?: string
    created_by_user_name?: string
    approved_by_user_id?: string
    approved_by_user_name?: string
}

interface TestSuite {
    id: string
    name: string
    description?: string
    tests_count?: number
}

interface TestRun {
    id: string
    suite_ids?: string
    title?: string
    status: 'in_progress' | 'success' | 'error' | 'fail'
    started_at?: string
    finished_at?: string
    created_at: string
    build_id?: string
    build_number?: number
    summary_json?: {
        total?: number
        passed?: number
        failed?: number
    }
}

interface TestResult {
    id: string
    run_id: string
    case_id: string
    status: 'in_progress' | 'pass' | 'fail' | 'error'
}

interface DiffInstructionItem {
    instruction_id: string
    change_type: 'added' | 'removed' | 'modified'
    title?: string
    text: string
    category?: string
    source_type?: string
    status?: string
    load_mode?: string
    previous_text?: string
    previous_title?: string
    previous_status?: string
    previous_load_mode?: string
    previous_category?: string
    changed_fields?: string[]
    // References changes
    references_added?: number
    references_removed?: number
    // Version IDs for editing
    from_version_id?: string  // Version in parent build (for restore/revert)
    to_version_id?: string    // Version in current build (for added/modified)
}

interface BuildDiffDetailedResponse {
    build_a_id: string
    build_b_id: string
    build_a_number: number
    build_b_number: number
    items: DiffInstructionItem[]
    added_count: number
    modified_count: number
    removed_count: number
}

interface BuildContent {
    id: string
    build_id: string
    instruction_id: string
    instruction_version_id: string
    version_number?: number
    text?: string
    title?: string
    content_hash?: string
    load_mode?: string
    instruction_status?: string
    instruction_category?: string
}

interface Props {
    modelValue: boolean
    buildId?: string
    compareToBuildId?: string
    gitRepoId?: string  // Git repository ID for push operations
    suggestionsMode?: boolean  // Filter to pending_approval builds only
    userOnly?: boolean  // Only show builds created by current user
}

const props = defineProps<Props>()

// Computed for template access
const gitRepoId = computed(() => props.gitRepoId)
const suggestionsMode = computed(() => props.suggestionsMode)
const userOnly = computed(() => props.userOnly)
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    'rollback': [newBuildId: string]
}>()

// State
const loadingBuilds = ref(false)
const loadingInstructions = ref(false)
const loadingDiff = ref(false)
const rollingBack = ref(false)
const pushingToGit = ref(false)
const publishingBuild = ref(false)
const rejectingBuild = ref(false)
const builds = ref<Build[]>([])
const selectedBuild = ref<Build | null>(null)
const mainBuild = ref<Build | null>(null)  // Stored separately for diff comparison
const instructions = ref<Instruction[]>([])
const diffData = ref<BuildDiffDetailedResponse | null>(null)
const diffExpanded = ref(true)
const instructionsExpanded = ref(false)
const evalsExpanded = ref(true)

// Edit instruction state
const showEditInstruction = ref(false)
const editingInstruction = ref<any>(null)
const editTargetBuildId = ref<string | null>(null)  // Capture build ID at click time

// Evals state
const loadingTestSuites = ref(false)
const runningEval = ref(false)
const testSuites = ref<TestSuite[]>([])
const selectedSuiteId = ref<string | null>(null)
const selectedSuiteCaseCount = ref<number>(0)
const activeTestRun = ref<TestRun | null>(null)
const buildTestRuns = ref<TestRun[]>([])
const testResults = ref<TestResult[]>([])
const pollInterval = ref<ReturnType<typeof setInterval> | null>(null)

const toast = useToast()

// Permission check - use computed for reactivity
const canCreateBuilds = computed(() => useCan('create_builds'))
const canManageTests = computed(() => useCan('manage_tests'))

// Pagination
const currentPage = ref(1)
const pageSize = ref(25)
const totalInstructions = ref(0)

const totalPages = computed(() => Math.ceil(totalInstructions.value / pageSize.value) || 1)
const visiblePages = computed(() => {
    const pages: number[] = []
    const total = totalPages.value
    const current = currentPage.value
    
    let start = Math.max(1, current - 2)
    let end = Math.min(total, start + 4)
    
    if (end - start < 4) {
        start = Math.max(1, end - 4)
    }
    
    for (let i = start; i <= end; i++) {
        pages.push(i)
    }
    return pages
})

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

// Computed - filter diff items by type
const addedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'added') || []
)
const modifiedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'modified') || []
)
const removedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'removed') || []
)
const hasDiffChanges = computed(() => 
    (diffData.value?.added_count || 0) + 
    (diffData.value?.modified_count || 0) + 
    (diffData.value?.removed_count || 0) > 0
)

// Build is editable if it's in draft or pending_approval status (not yet published)
const isBuildEditable = computed(() => 
    (selectedBuild.value?.status === 'draft' || selectedBuild.value?.status === 'pending_approval') && canCreateBuilds.value
)

const loadingBuildContent = computed(() => loadingDiff.value || loadingInstructions.value)

// Evals computed properties
const testSuiteOptions = computed(() => 
    testSuites.value.map(s => ({
        value: s.id,
        label: `${s.name} (${s.tests_count || 0} tests)`
    }))
)

const testResultsSummary = computed(() => {
    const results = testResults.value
    const total = results.length
    const passed = results.filter(r => r.status === 'pass').length
    const failed = results.filter(r => r.status === 'fail' || r.status === 'error').length
    const inProgress = results.filter(r => r.status === 'in_progress').length
    const completed = passed + failed
    const progressPercent = total > 0 ? Math.round((completed / total) * 100) : 0
    
    return { total, passed, failed, inProgress, progressPercent }
})

const displayedTestRuns = computed(() => {
    // Filter out the active run and show only completed runs
    return buildTestRuns.value
        .filter(r => r.id !== activeTestRun.value?.id && r.status !== 'in_progress')
        .slice(0, 3)
})

const hasTestCases = computed(() => selectedSuiteCaseCount.value > 0)

// Methods
const truncateText = (text: string, maxLength: number) => {
    if (!text) return ''
    return text.length > maxLength ? text.slice(0, maxLength) + '...' : text
}

const formatLoadMode = (mode?: string) => {
    if (!mode) return '?'
    const labels: Record<string, string> = {
        always: 'Always',
        intelligent: 'Smart',
        disabled: 'Disabled'
    }
    return labels[mode] || mode
}

const fetchBuilds = async () => {
    loadingBuilds.value = true
    try {
        // In suggestions mode, only fetch pending_approval builds
        // Otherwise fetch all builds for the Version Explorer
        const statusParam = props.suggestionsMode ? 'pending_approval' : 'all'
        const createdByParam = props.userOnly ? '&created_by=me' : ''
        const response = await useMyFetch<{ items: Build[], total: number }>(`/builds?limit=50&status=${statusParam}${createdByParam}`)
        if (response.data.value) {
            builds.value = response.data.value.items || []
        }
    } catch (e) {
        console.error('Failed to fetch builds:', e)
    } finally {
        loadingBuilds.value = false
    }
}

const selectBuild = async (build: Build) => {
    selectedBuild.value = build
    currentPage.value = 1
    
    // Fetch instructions and diff in parallel
    await Promise.all([
        fetchInstructions(),
        fetchDiff()
    ])
}

const fetchInstructions = async () => {
    if (!selectedBuild.value) return
    
    loadingInstructions.value = true
    try {
        const response = await useMyFetch<{ items: BuildContent[], total: number, build_id: string, build_number: number }>(
            `/builds/${selectedBuild.value.id}/contents`
        )
        if (response.data.value) {
            const data = response.data.value
            // Transform BuildContent to Instruction format for InstructionsTable
            instructions.value = (data.items || []).map((content: BuildContent) => ({
                id: content.instruction_id,
                text: content.text || '',
                title: content.title,
                status: content.instruction_status || 'published',
                category: content.instruction_category || 'general',
                load_mode: content.load_mode || 'always',
                source_type: 'user', // Not available in BuildContent, default to user
                version_number: content.version_number,
                // Required fields with defaults for InstructionsTable compatibility
                organization_id: '',
                data_sources: [],
                created_at: '',
                updated_at: '',
            })) as unknown as Instruction[]
            totalInstructions.value = data.total || instructions.value.length
        }
    } catch (e) {
        console.error('Failed to fetch instructions:', e)
        instructions.value = []
        totalInstructions.value = 0
    } finally {
        loadingInstructions.value = false
    }
}

const fetchMainBuild = async () => {
    try {
        const response = await useMyFetch<Build>('/builds/main')
        if (response.data.value) {
            mainBuild.value = response.data.value
        }
    } catch (e) {
        console.error('Failed to fetch main build:', e)
        mainBuild.value = null
    }
}

const fetchDiff = async () => {
    if (!selectedBuild.value) return
    
    // Compare against base_build (what the build was forked from) to show user's actual changes
    // This prevents showing "removed" items for things added to main after this build was created
    let compareToBuildId = selectedBuild.value.base_build_id
    
    // Fallback to main if no base_build_id (for older builds or main build itself)
    if (!compareToBuildId) {
        const mainBuildForDiff = mainBuild.value || builds.value.find(b => b.is_main)
        if (mainBuildForDiff && mainBuildForDiff.id !== selectedBuild.value.id) {
            compareToBuildId = mainBuildForDiff.id
        }
    }
    
    // Don't diff if this IS the main build or no comparison target exists
    if (!compareToBuildId || compareToBuildId === selectedBuild.value.id) {
        diffData.value = null
        return
    }
    
    loadingDiff.value = true
    try {
        const response = await useMyFetch<BuildDiffDetailedResponse>(
            `/builds/${selectedBuild.value.id}/diff/details?compare_to=${compareToBuildId}`
        )
        if (response.data.value) {
            diffData.value = response.data.value
        }
    } catch (e) {
        console.error('Failed to fetch diff:', e)
        diffData.value = null
    } finally {
        loadingDiff.value = false
    }
}

const close = () => {
    emit('update:modelValue', false)
}

const rollbackToBuild = async () => {
    if (!selectedBuild.value || rollingBack.value) return
    
    rollingBack.value = true
    try {
        const response = await useMyFetch(`/builds/${selectedBuild.value.id}/rollback`, {
            method: 'POST'
        })
        
        if (response.error.value) {
            throw new Error((response.error.value as any)?.data?.detail || 'Failed to rollback')
        }
        
        toast.add({
            title: 'Rollback successful',
            description: `Created new build from Build #${selectedBuild.value.build_number}`,
            color: 'green',
            icon: 'i-heroicons-check-circle'
        })
        
        // Refresh builds list and select the new main build
        await fetchBuilds()
        const newMainBuild = builds.value.find(b => b.is_main)
        if (newMainBuild) {
            await selectBuild(newMainBuild)
            // Emit rollback event so parent can refresh its data
            emit('rollback', newMainBuild.id)
        }
    } catch (e: any) {
        console.error('Rollback failed:', e)
        toast.add({
            title: 'Rollback failed',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    } finally {
        rollingBack.value = false
    }
}

const publishBuild = async () => {
    if (!selectedBuild.value || publishingBuild.value) return
    
    publishingBuild.value = true
    try {
        const response = await useMyFetch(`/builds/${selectedBuild.value.id}/publish`, {
            method: 'POST'
        })
        
        if (response.error.value) {
            throw new Error((response.error.value as any)?.data?.detail || 'Failed to publish build')
        }
        
        toast.add({
            title: 'Build published',
            description: `Build #${selectedBuild.value.build_number} is now live`,
            color: 'green',
            icon: 'i-heroicons-rocket-launch'
        })
        
        // Refresh builds list
        await fetchBuilds()
        
        // Select the new main build
        const newMainBuild = builds.value.find(b => b.is_main)
        if (newMainBuild) {
            await selectBuild(newMainBuild)
        }
    } catch (e: any) {
        console.error('Publish failed:', e)
        toast.add({
            title: 'Publish failed',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    } finally {
        publishingBuild.value = false
    }
}

const rejectBuild = async () => {
    if (!selectedBuild.value || rejectingBuild.value) return
    
    rejectingBuild.value = true
    try {
        const response = await useMyFetch(`/builds/${selectedBuild.value.id}/reject`, {
            method: 'POST',
            body: {
                reason: 'Rejected via Version Explorer'
            }
        })
        
        if (response.error.value) {
            throw new Error((response.error.value as any)?.data?.detail || 'Failed to reject build')
        }
        
        toast.add({
            title: 'Build rejected',
            description: `Build #${selectedBuild.value.build_number} has been rejected`,
            color: 'amber',
            icon: 'i-heroicons-x-circle'
        })
        
        // Update local state
        if (selectedBuild.value) {
            selectedBuild.value.status = 'rejected'
        }
        
        // Refresh builds list
        await fetchBuilds()
        
        // Re-select the current build to update the UI
        const updatedBuild = builds.value.find(b => b.id === selectedBuild.value?.id)
        if (updatedBuild) {
            selectedBuild.value = updatedBuild
        }
    } catch (e: any) {
        console.error('Reject failed:', e)
        toast.add({
            title: 'Reject failed',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    } finally {
        rejectingBuild.value = false
    }
}

const removeChange = async (item: DiffInstructionItem) => {
    if (!selectedBuild.value || !isBuildEditable.value) return
    
    try {
        if (item.change_type === 'added') {
            // Remove the added instruction from the build
            const response = await useMyFetch(`/builds/${selectedBuild.value.id}/contents/${item.instruction_id}`, {
                method: 'DELETE'
            })
            
            if (response.error.value) {
                throw new Error((response.error.value as any)?.data?.detail || 'Failed to remove instruction')
            }
            
            toast.add({
                title: 'Change removed',
                description: 'Addition reverted',
                color: 'blue',
                icon: 'i-heroicons-arrow-uturn-left'
            })
        } else if (item.change_type === 'modified' && item.from_version_id) {
            // Revert to the previous version
            const response = await useMyFetch(`/builds/${selectedBuild.value.id}/contents/${item.instruction_id}`, {
                method: 'PUT',
                body: {
                    instruction_version_id: item.from_version_id
                }
            })
            
            if (response.error.value) {
                throw new Error((response.error.value as any)?.data?.detail || 'Failed to revert change')
            }
            
            toast.add({
                title: 'Change reverted',
                description: 'Modification undone',
                color: 'blue',
                icon: 'i-heroicons-arrow-uturn-left'
            })
        } else if (item.change_type === 'removed' && item.from_version_id) {
            // Restore the removed instruction
            const response = await useMyFetch(`/builds/${selectedBuild.value.id}/contents/${item.instruction_id}`, {
                method: 'PUT',
                body: {
                    instruction_version_id: item.from_version_id
                }
            })
            
            if (response.error.value) {
                throw new Error((response.error.value as any)?.data?.detail || 'Failed to restore instruction')
            }
            
            toast.add({
                title: 'Instruction restored',
                description: 'Removal undone',
                color: 'green',
                icon: 'i-heroicons-arrow-uturn-left'
            })
        }
        
        // Refresh the diff
        await fetchDiff()
    } catch (e: any) {
        console.error('Remove change failed:', e)
        toast.add({
            title: 'Failed to remove change',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    }
}

const openEditInstruction = async (item: DiffInstructionItem) => {
    try {
        // Capture build ID at click time (before any async operations)
        editTargetBuildId.value = selectedBuild.value?.id || null
        
        const { data, error } = await useMyFetch<any>(`/instructions/${item.instruction_id}`)
        if (error.value) {
            throw new Error((error.value as any)?.data?.detail || 'Failed to load instruction')
        }
        editingInstruction.value = data.value
        showEditInstruction.value = true
    } catch (e: any) {
        console.error('Failed to load instruction:', e)
        editTargetBuildId.value = null  // Clear on error
        toast.add({
            title: 'Failed to load instruction',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    }
}

const closeEditInstruction = () => {
    showEditInstruction.value = false
    editingInstruction.value = null
    editTargetBuildId.value = null
}

const onInstructionSaved = async () => {
    closeEditInstruction()
    
    // Refresh the diff and instructions to show updated content
    await Promise.all([
        fetchDiff(),
        fetchInstructions()
    ])
    
    toast.add({
        title: 'Instruction updated',
        description: 'Build has been updated with the new version',
        color: 'green',
        icon: 'i-heroicons-check-circle'
    })
}

const pushToGit = async () => {
    if (!selectedBuild.value || !props.gitRepoId || pushingToGit.value) return
    
    pushingToGit.value = true
    try {
        const response = await useMyFetch<{
            build_id: string
            branch_name: string
            pushed: boolean
            pr_url?: string
            message?: string
        }>(`/git/${props.gitRepoId}/push`, {
            method: 'POST',
            body: {
                build_id: selectedBuild.value.id,
                create_pr: true  // Attempt to create PR if PAT is configured
            }
        })
        
        if (response.error.value) {
            throw new Error((response.error.value as any)?.data?.detail || 'Failed to push to Git')
        }
        
        const result = response.data.value
        
        if (result?.pushed) {
            // Update the selected build with git info
            if (selectedBuild.value) {
                selectedBuild.value.git_branch_name = result.branch_name
                selectedBuild.value.git_pr_url = result.pr_url
                selectedBuild.value.git_pushed_at = new Date().toISOString()
            }
            
            toast.add({
                title: 'Pushed to Git',
                description: result.pr_url 
                    ? `Created branch ${result.branch_name} and opened PR` 
                    : `Created branch ${result.branch_name}`,
                color: 'green',
                icon: 'i-heroicons-check-circle'
            })
        } else {
            toast.add({
                title: 'No changes to push',
                description: result?.message || 'The build has no changes to push',
                color: 'amber',
                icon: 'i-heroicons-information-circle'
            })
        }
    } catch (e: any) {
        console.error('Push to Git failed:', e)
        toast.add({
            title: 'Push failed',
            description: e.message || 'Failed to push to Git',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    } finally {
        pushingToGit.value = false
    }
}

const handlePageChange = (page: number) => {
    currentPage.value = page
    // Note: The current /builds/{id}/contents endpoint doesn't support pagination
    // If pagination is needed, the API would need to be extended
}

const handleInstructionClick = (instruction: Instruction) => {
    // Could emit an event or open a detail view
    console.log('Clicked instruction:', instruction.id)
}

const formatDate = (dateStr: string) => {
    try {
        return new Date(dateStr).toLocaleDateString('en-US', { 
            month: 'short', 
            day: 'numeric' 
        })
    } catch {
        return dateStr
    }
}

const formatDateTime = (dateStr: string) => {
    try {
        return new Date(dateStr).toLocaleString('en-US', { 
            month: 'short', 
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        })
    } catch {
        return dateStr
    }
}

const copyBuildId = async () => {
    if (!selectedBuild.value?.id) return
    
    try {
        await navigator.clipboard.writeText(selectedBuild.value.id)
        toast.add({ title: 'Build ID copied', color: 'green' })
    } catch (error) {
        toast.add({ title: 'Failed to copy', color: 'red' })
    }
}

const formatTimeAgo = (dateStr?: string) => {
    if (!dateStr) return '—'
    try {
        const then = new Date(dateStr).getTime()
        const now = Date.now()
        const diffSec = Math.max(0, Math.floor((now - then) / 1000))
        if (diffSec < 60) return `${diffSec}s ago`
        const mins = Math.floor(diffSec / 60)
        if (mins < 60) return `${mins}m ago`
        const hours = Math.floor(mins / 60)
        if (hours < 24) return `${hours}h ago`
        const days = Math.floor(hours / 24)
        return `${days}d ago`
    } catch {
        return '—'
    }
}

const prettyStatus = (status?: string) => {
    if (!status) return '—'
    if (status === 'in_progress') return 'Running'
    if (status === 'success') return 'Passed'
    if (status === 'fail' || status === 'error') return 'Failed'
    return status.replace('_', ' ')
}

// Evals methods
const fetchTestSuites = async () => {
    loadingTestSuites.value = true
    try {
        // Use summary endpoint to get test counts
        const response = await useMyFetch<TestSuite[]>('/tests/suites/summary')
        if (response.data.value) {
            testSuites.value = response.data.value
            // Auto-select first suite if available
            if (testSuites.value.length > 0 && !selectedSuiteId.value) {
                selectedSuiteId.value = testSuites.value[0].id
                selectedSuiteCaseCount.value = testSuites.value[0].tests_count || 0
            }
        }
    } catch (e) {
        console.error('Failed to fetch test suites:', e)
    } finally {
        loadingTestSuites.value = false
    }
}

const fetchBuildTestRuns = async () => {
    if (!selectedBuild.value) return
    
    try {
        // Fetch test runs that were run against this build
        const response = await useMyFetch<TestRun[]>(`/tests/runs?limit=10`)
        if (response.data.value) {
            // Filter runs for this build
            buildTestRuns.value = response.data.value.filter(
                r => r.build_id === selectedBuild.value?.id
            )
            // Set active run if there's an in-progress one
            const inProgressRun = buildTestRuns.value.find(r => r.status === 'in_progress')
            if (inProgressRun) {
                activeTestRun.value = inProgressRun
                await fetchTestResults(inProgressRun.id)
                startPolling()
            } else if (buildTestRuns.value.length > 0) {
                // Show the most recent completed run
                activeTestRun.value = buildTestRuns.value[0]
                await fetchTestResults(buildTestRuns.value[0].id)
                // Update builds list with existing test results
                updateBuildTestResults()
            }
        }
    } catch (e) {
        console.error('Failed to fetch build test runs:', e)
    }
}

const fetchTestResults = async (runId: string) => {
    try {
        const response = await useMyFetch<TestResult[]>(`/tests/runs/${runId}/results`)
        if (response.data.value) {
            testResults.value = response.data.value
        }
    } catch (e) {
        console.error('Failed to fetch test results:', e)
    }
}

const runEval = async () => {
    if (!selectedBuild.value || !selectedSuiteId.value || runningEval.value) return
    
    runningEval.value = true
    try {
        const response = await useMyFetch<TestRun>('/tests/runs/batch', {
            method: 'POST',
            body: {
                suite_id: selectedSuiteId.value,
                build_id: selectedBuild.value.id,
                trigger_reason: 'manual'
            }
        })
        
        if (response.error.value) {
            throw new Error((response.error.value as any)?.data?.detail || 'Failed to start test run')
        }
        
        if (response.data.value) {
            activeTestRun.value = response.data.value
            buildTestRuns.value.unshift(response.data.value)
            await fetchTestResults(response.data.value.id)
            startPolling()
            
            toast.add({
                title: 'Eval started',
                description: `Running tests for Build #${selectedBuild.value.build_number}`,
                color: 'blue',
                icon: 'i-heroicons-play'
            })
        }
    } catch (e: any) {
        console.error('Failed to run eval:', e)
        toast.add({
            title: 'Failed to start eval',
            description: e.message || 'An error occurred',
            color: 'red',
            icon: 'i-heroicons-x-circle'
        })
    } finally {
        runningEval.value = false
    }
}

const pollTestRun = async () => {
    if (!activeTestRun.value) return
    
    try {
        const response = await useMyFetch<TestRun>(`/tests/runs/${activeTestRun.value.id}`)
        if (response.data.value) {
            activeTestRun.value = response.data.value
            await fetchTestResults(response.data.value.id)
            
            // Stop polling if run is complete
            if (response.data.value.status !== 'in_progress') {
                stopPolling()
                
                // Update build in the builds list with test results
                updateBuildTestResults()
            }
        }
    } catch (e) {
        console.error('Failed to poll test run:', e)
    }
}

const updateBuildTestResults = () => {
    if (!selectedBuild.value) return
    
    const summary = testResultsSummary.value
    const buildIndex = builds.value.findIndex(b => b.id === selectedBuild.value?.id)
    
    if (buildIndex !== -1) {
        // Update the build in the list with test results
        builds.value[buildIndex] = {
            ...builds.value[buildIndex],
            test_status: summary.failed > 0 ? 'failed' : (summary.passed > 0 ? 'passed' : 'pending'),
            test_passed: summary.passed,
            test_failed: summary.failed
        }
    }
    
    // Also update the selected build
    if (selectedBuild.value) {
        selectedBuild.value.test_status = summary.failed > 0 ? 'failed' : (summary.passed > 0 ? 'passed' : 'pending')
        selectedBuild.value.test_passed = summary.passed
        selectedBuild.value.test_failed = summary.failed
    }
}

const startPolling = () => {
    if (pollInterval.value) return
    pollInterval.value = setInterval(pollTestRun, 2000)
}

const stopPolling = () => {
    if (pollInterval.value) {
        clearInterval(pollInterval.value)
        pollInterval.value = null
    }
}

// Watch for modal opening
watch(() => props.modelValue, async (newValue) => {
    if (newValue) {
        // Fetch builds, test suites, and main build (for diff) in parallel
        const fetches: Promise<void>[] = [fetchBuilds()]
        
        // Fetch test suites only if user has manage_tests permission
        if (canManageTests.value) {
            fetches.push(fetchTestSuites())
        }
        
        // In suggestions mode, fetch main build separately for diff comparison
        if (props.suggestionsMode) {
            fetches.push(fetchMainBuild())
        }
        
        await Promise.all(fetches)
        
        // If a buildId was provided, select it
        if (props.buildId) {
            const build = builds.value.find(b => b.id === props.buildId)
            if (build) {
                await selectBuild(build)
            }
        } else if (builds.value.length) {
            // Select the first (most recent) build
            await selectBuild(builds.value[0])
        }
    } else {
        // Reset state on close
        stopPolling()
        selectedBuild.value = null
        mainBuild.value = null
        instructions.value = []
        diffData.value = null
        currentPage.value = 1
        activeTestRun.value = null
        buildTestRuns.value = []
        testResults.value = []
    }
})

// Watch for build selection change to fetch evals data
watch(selectedBuild, async (newBuild) => {
    if (newBuild) {
        stopPolling()
        activeTestRun.value = null
        buildTestRuns.value = []
        testResults.value = []
        await fetchBuildTestRuns()
    }
})

// Watch for suite selection change to update case count
watch(selectedSuiteId, (newSuiteId) => {
    if (newSuiteId) {
        const suite = testSuites.value.find(s => s.id === newSuiteId)
        selectedSuiteCaseCount.value = suite?.tests_count || 0
    } else {
        selectedSuiteCaseCount.value = 0
    }
})

// Cleanup on unmount
onUnmounted(() => {
    stopPolling()
})
</script>
