<template>
    <UTooltip :text="buttonLabel">
        <button @click="openModal"
            :class="[
                'items-center flex gap-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400',
                compact ? 'p-1.5' : 'px-2 py-1 text-xs'
            ]">
            <div class="relative">
                <Icon :name="buttonIcon" :class="compact ? 'w-4 h-4' : 'w-3.5 h-3.5'" />
                <span v-if="isShared" class="absolute -top-0.5 -end-0.5 w-1.5 h-1.5 bg-green-500 rounded-full"></span>
            </div>
            <span v-if="!compact" class="text-xs whitespace-nowrap">{{ buttonLabel }}</span>
        </button>
    </UTooltip>

    <UModal v-model="modalOpen" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-6">
            <!-- Header -->
            <div class="flex items-center justify-between mb-1">
                <h2 class="text-base font-semibold text-gray-900 dark:text-white">{{ title }}</h2>
                <button @click="modalOpen = false"
                    class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>
            <p class="text-sm text-gray-400 mb-6">{{ shareDescription }}</p>

            <!-- Visibility dropdown -->
            <label class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2 block">{{ $t('share.access') }}</label>
            <USelectMenu
                v-model="currentVisibility"
                :options="visibilityOptions"
                value-attribute="value"
                option-attribute="label"
                size="xs"
                class="mb-5"
                :disabled="dashboardMode && (!dashboardPublished || !canShare)"
                :ui="{ rounded: 'rounded-lg', size: { xs: 'text-xs' }, padding: { xs: 'px-2.5 py-1.5' } }"
                @change="onVisibilityChange"
            >
                <template #label>
                    <div class="flex items-center gap-2 text-xs">
                        <Icon :name="selectedOption.icon" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400 flex-shrink-0" />
                        <span>{{ selectedOption.label }}</span>
                    </div>
                </template>
                <template #option="{ option }">
                    <!-- Public without make_public (collab01): listed, off, and says why. -->
                    <UTooltip v-if="option.disabledReason" :text="option.disabledReason" class="w-full"
                        data-testid="share-public-locked">
                        <div class="flex items-start gap-3 py-1 px-1 w-full">
                            <Icon :name="option.icon" class="w-4 h-4 text-gray-400 flex-shrink-0 mt-0.5" />
                            <div class="flex flex-col">
                                <span class="text-xs">{{ option.label }}</span>
                                <span class="text-[11px] text-gray-400">{{ option.disabledReason }}</span>
                            </div>
                        </div>
                    </UTooltip>
                    <div v-else class="flex items-start gap-3 py-1 px-1">
                        <Icon :name="option.icon" class="w-4 h-4 text-gray-400 flex-shrink-0 mt-0.5" />
                        <div class="flex flex-col">
                            <span class="text-xs">{{ option.label }}</span>
                            <span class="text-[11px] text-gray-400">{{ option.description }}</span>
                        </div>
                    </div>
                </template>
            </USelectMenu>

            <!-- A dashboard is shared with viewers once published (phase 8):
                 until then its visibility is off and says why. Editors can be
                 added before (collab01), in the list below. -->
            <p v-if="dashboardMode && !dashboardPublished" class="text-[11px] text-gray-400 -mt-3 mb-5"
                data-testid="share-requires-publish">
                {{ $t('dashboardCollab.share.publishToShareViewers') }}
            </p>

            <!-- Public link on a per-user-data dashboard: the link is open to
                 anyone, but data resolves per signed-in viewer — say so. -->
            <p v-if="shareType === 'artifact' && currentVisibility === 'public' && showRunIdentity"
                class="text-[11px] text-gray-400 -mt-3 mb-5">
                {{ $t('share.publicPerUserNote') }}
            </p>

            <!-- Share link -->
            <div v-if="isShared && shareUrl && editingName" class="mb-6" data-testid="link-name-editor">
                <div class="flex items-center gap-2">
                    <div class="flex-1 flex items-center h-[32px] px-2.5 border border-gray-200 dark:border-gray-700 rounded-lg text-xs bg-white dark:bg-gray-900 min-w-0 focus-within:ring-2 focus-within:ring-blue-500" dir="ltr">
                        <span class="text-gray-400 whitespace-nowrap">{{ linkPrefix }}</span>
                        <input ref="nameInputRef" v-model="nameDraft" type="text" maxlength="80"
                            class="flex-1 min-w-0 border-none outline-none bg-transparent p-0 text-xs text-gray-700 dark:text-gray-300"
                            :placeholder="dashboardId" @keydown.enter.prevent="saveName" @keydown.esc.prevent="editingName = false" />
                    </div>
                    <button @click="saveName" :disabled="isSaving || !nameValid"
                        class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400 disabled:opacity-40">
                        <Icon name="heroicons:check" class="w-3.5 h-3.5" />
                    </button>
                    <button @click="editingName = false"
                        class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400">
                        <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
                    </button>
                </div>
                <p v-if="!nameValid" class="text-[11px] text-red-500 mt-1">{{ $t("errors['dashboard.slug_invalid']") }}</p>
                <p v-else-if="!nameDraft && dashboardSlug" class="text-[11px] text-gray-400 mt-1">{{ $t('share.linkNameRemove') }}</p>
            </div>
            <div v-else-if="isShared && shareUrl" class="flex items-center gap-2 mb-6">
                <input :value="shareUrl" type="text"
                    class="flex-1 h-[32px] px-2.5 border border-gray-200 dark:border-gray-700 rounded-lg text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 min-w-0"
                    readonly />
                <!-- The dashboard's name in its link (phase 8) -->
                <UTooltip v-if="dashboardMode && canSettings" :text="$t('share.linkNameEdit')">
                    <button @click="startEditingName" data-testid="edit-link-name"
                        class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400">
                        <Icon name="heroicons:pencil" class="w-3.5 h-3.5" />
                    </button>
                </UTooltip>
                <button @click="copyLink"
                    class="flex-shrink-0 h-[32px] w-[32px] flex items-center justify-center border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400">
                    <Icon :name="copied ? 'heroicons:check' : 'heroicons:clipboard-document'" class="w-3.5 h-3.5" />
                </button>
            </div>

            <!-- Include Data Tab option (dashboards only): whether viewers of
                 the shared artifact page see the Data tab listing the queries
                 behind the report. The conversation share has no such tab. -->
            <div v-if="shareType === 'artifact' && isShared" class="flex items-start justify-between gap-3 mb-6 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                <div class="flex flex-col min-w-0 flex-1">
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.includeDataTab') }}</span>
                    <span class="text-[11px] text-gray-400">{{ $t('share.includeDataTabDesc') }}</span>
                </div>
                <UToggle v-model="includeDataTab" size="sm" :disabled="isSaving || !canSettings" class="flex-shrink-0 mt-0.5"
                    @update:model-value="onIncludeDataTabChange" />
            </div>

            <!-- Chat on the shared artifact page (dashboards only): the owner's
                 toggle plus the agent scope viewer chat may query. Viewer
                 threads are private per viewer and never touch this report's
                 own conversation. -->
            <div v-if="shareType === 'artifact' && isShared" class="mb-6 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg" data-testid="chat-settings">
                <div class="flex items-start justify-between gap-3">
                    <div class="flex flex-col min-w-0">
                        <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.allowChat') }}</span>
                        <span class="text-[11px] text-gray-400">{{ $t('share.allowChatDesc') }}</span>
                    </div>
                    <UToggle v-model="chatEnabled" size="sm" :disabled="isSaving || !canSettings" class="flex-shrink-0 mt-0.5"
                        data-testid="chat-enabled-toggle" @update:model-value="onChatEnabledChange" />
                </div>
                <div v-if="chatEnabled" class="mt-3 space-y-2">
                    <label class="text-[11px] font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider block">{{ $t('share.chatScope') }}</label>
                    <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-agents">
                        <input type="radio" value="agents" v-model="chatScope" :disabled="isSaving || !canSettings || reportAgents.length === 0"
                            class="mt-0.5" @change="onChatScopeChange" />
                        <span class="flex flex-col">
                            <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeAgents') }}</span>
                            <span class="text-[11px] text-gray-400">{{ $t('share.chatScopeAgentsDesc') }}</span>
                        </span>
                    </label>
                    <div v-if="chatScope === 'agents' && reportAgents.length > 0" class="ms-6 space-y-1">
                        <label v-for="agent in reportAgents" :key="agent.id" class="flex items-center gap-2 cursor-pointer">
                            <input type="checkbox" :value="agent.id" v-model="chatAgentIds" :disabled="isSaving || !canSettings"
                                @change="onChatAgentsChange" />
                            <span class="text-xs text-gray-600 dark:text-gray-400">{{ agent.name }}</span>
                        </label>
                    </div>
                    <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-data-only">
                        <input type="radio" value="data_only" v-model="chatScope" :disabled="isSaving || !canSettings"
                            class="mt-0.5" @change="onChatScopeChange" />
                        <span class="flex flex-col">
                            <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeDataOnly') }}</span>
                            <span class="text-[11px] text-gray-400">{{ $t('share.chatScopeDataOnlyDesc') }}</span>
                        </span>
                    </label>
                    <p v-if="reportAgents.length === 0" class="text-[11px] text-gray-400">{{ $t('share.chatNoAgents') }}</p>

                    <!-- Default model for viewer chat: the report's own
                         model, the organization default, or a specific model. -->
                    <div class="pt-2" data-testid="chat-model">
                        <label class="text-[11px] font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider block mb-1">{{ $t('share.chatModel') }}</label>
                        <USelectMenu
                            v-model="chatModelValue"
                            :options="chatModelOptions"
                            value-attribute="value"
                            option-attribute="label"
                            size="xs"
                            :disabled="isSaving || !canSettings"
                            :ui="{ rounded: 'rounded-lg', size: { xs: 'text-xs' }, padding: { xs: 'px-2.5 py-1.5' } }"
                            @change="onChatModelChange"
                        />
                        <span class="text-[11px] text-gray-400 block mt-1">{{ $t('share.chatModelDesc') }}</span>
                    </div>
                </div>
            </div>

            <!-- Viewer run identity (dashboards only): whose credentials a
                 viewer's "Run" uses. Results are always stored per viewer.
                 Only shown when the choice exists — user-scoped sources
                 (toggleable) or RLS (visible but disabled, to explain why
                 runs are always per-viewer). -->
            <div v-if="shareType === 'artifact' && isShared && showRunIdentity" class="flex items-start justify-between gap-3 mb-6">
                <div class="flex flex-col min-w-0">
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.runOnBehalf') }}</span>
                    <span class="text-[11px] text-gray-400">{{ hasRls ? $t('share.runOnBehalfRlsDisabled') : $t('share.runOnBehalfDesc') }}</span>
                </div>
                <UToggle v-model="runAsCreator" size="sm" :disabled="isSaving || hasRls || !canRunIdentity" class="flex-shrink-0 mt-0.5" @update:model-value="onRunIdentityChange" />
            </div>

            <!-- Share with people (only when 'shared' selected). A dashboard's
                 list is always there (collab01): its editors stand whatever
                 the visibility, before the first publish too. -->
            <div v-if="currentVisibility === 'shared' || dashboardMode" data-testid="share-people">
                <label class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2 block">{{ dashboardMode ? $t('dashboardCollab.share.peopleWithAccess') : $t('share.shareWith') }}</label>
                <div v-if="!dashboardMode || canShare" :class="dashboardMode ? 'flex flex-wrap items-start gap-2 mb-2' : 'flex items-start gap-2 mb-4'">
                    <div :class="dashboardMode ? 'min-w-[11rem]' : ''" class="flex-1 flex flex-wrap items-center gap-1.5 border border-gray-200 dark:border-gray-700 rounded-lg px-2.5 py-1.5 min-h-[32px] focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-blue-500 bg-white dark:bg-gray-900">
                        <span v-for="(principal, idx) in pendingPrincipals" :key="principal.kind + ':' + (principal.id || principal.email)"
                            class="inline-flex items-center gap-1 bg-blue-50 dark:bg-blue-950 text-blue-700 text-xs px-2 py-0.5 rounded-full whitespace-nowrap">
                            <Icon v-if="principal.kind === 'group'" name="heroicons:user-group" class="w-3 h-3" />
                            {{ principal.name || principal.email }}
                            <button @click="removePendingPrincipal(idx)" class="hover:text-red-500 outline-none">
                                <Icon name="heroicons:x-mark" class="w-3 h-3" />
                            </button>
                        </span>
                        <div class="relative flex-1 min-w-[120px]">
                            <input ref="inputRef" v-model="inputValue" type="text"
                                class="w-full border-none outline-none text-xs bg-transparent p-0"
                                :placeholder="groups.length > 0 ? $t('share.nameEmailOrGroup') : $t('share.nameOrEmail')"
                                @keydown.enter.prevent="handleEnter"
                                @keydown.,.prevent="handleComma"
                                @keydown.backspace="handleBackspace"
                                @input="onInput"
                                @focus="showDropdown = true"
                                @blur="onBlur" />
                            <div v-if="showDropdown && filteredOptions.length > 0"
                                class="absolute start-0 top-full mt-1 w-64 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 max-h-40 overflow-y-auto">
                                <button v-for="option in filteredOptions" :key="option.kind + ':' + option.id"
                                    class="w-full text-start px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-2.5"
                                    @mousedown.prevent="addPrincipal(option)">
                                    <div class="w-6 h-6 bg-gray-200 dark:bg-gray-700 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 dark:text-gray-400 flex-shrink-0">
                                        <Icon v-if="option.kind === 'group'" name="heroicons:user-group" class="w-3.5 h-3.5" />
                                        <template v-else>{{ (option.name || option.email).charAt(0).toUpperCase() }}</template>
                                    </div>
                                    <div class="flex flex-col min-w-0">
                                        <span class="text-gray-900 dark:text-white truncate">{{ option.name || option.email }}</span>
                                        <span v-if="option.kind === 'group'" class="text-xs text-gray-400 truncate">{{ $t('share.groupMembers', option.memberCount || 0) }}</span>
                                        <span v-else-if="option.name" class="text-xs text-gray-400 truncate">{{ option.email }}</span>
                                    </div>
                                </button>
                            </div>
                        </div>
                    </div>
                    <div :class="dashboardMode ? 'flex items-start gap-2 ms-auto' : 'contents'">
                        <!-- The role the picks get (collab01): editors before the
                             first publish, viewers once it is out. -->
                        <RoleSelect v-if="dashboardMode" v-model="newRole" :view-disabled-reason="viewDisabledReason"
                            :disabled="isSaving" data-testid="share-new-role" />
                        <button @click="invitePrincipals" :disabled="pendingPrincipals.length === 0 || isSaving"
                            class="flex-shrink-0 px-3 h-[32px] text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed">
                            {{ $t('share.share') }}
                        </button>
                    </div>
                </div>
                <!-- Why "Can view" is not offered right now (collab01). -->
                <p v-if="dashboardMode && canShare && viewDisabledReason" class="text-[11px] text-gray-400 mb-4"
                    data-testid="share-viewers-hint">
                    {{ viewDisabledReason }}
                </p>
                <!-- Someone who may see but not change the list (collab01). -->
                <p v-if="dashboardMode && !canShare" class="text-[11px] text-gray-400 mb-3 flex items-start gap-1.5"
                    data-testid="share-read-only">
                    <Icon name="heroicons:lock-closed" class="w-3.5 h-3.5 flex-shrink-0 mt-px" />
                    <span>{{ editorsCanShare ? $t('dashboardCollab.share.readOnly') : $t('dashboardCollab.share.ownerTurnedOffSharing') }}</span>
                </p>

                <!-- The owner, at the top, never removable (collab01). -->
                <div v-if="dashboardMode && ownerId" class="flex items-center justify-between gap-2 py-2 px-2 rounded-lg"
                    data-testid="share-owner-row">
                    <div class="flex items-center gap-2.5 min-w-0">
                        <div class="w-7 h-7 flex-shrink-0 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 dark:text-gray-400">
                            {{ (ownerMember?.name || ownerMember?.email || '?').charAt(0).toUpperCase() }}
                        </div>
                        <div class="flex flex-col min-w-0">
                            <span class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ ownerLabel }}</span>
                            <span v-if="ownerMember?.name && ownerMember?.email" class="text-xs text-gray-400 truncate">{{ ownerMember.email }}</span>
                        </div>
                    </div>
                    <span class="text-xs text-gray-500 dark:text-gray-400 flex-shrink-0 pe-1">{{ $t('dashboardCollab.share.owner') }}</span>
                </div>

                <!-- Everyone who can open the dashboard's project edits it. -->
                <div v-if="dashboardMode && projectId" class="flex items-center gap-2.5 py-2 px-2" data-testid="share-project-line">
                    <div class="w-7 h-7 flex-shrink-0 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center text-gray-500 dark:text-gray-400">
                        <Icon name="heroicons:folder" class="w-4 h-4" />
                    </div>
                    <span class="text-xs text-gray-600 dark:text-gray-400">
                        {{ projectName ? $t('dashboardCollab.share.projectCanEdit', { name: projectName }) : $t('dashboardCollab.share.projectCanEditUnnamed') }}
                    </span>
                </div>

                <!-- People and groups with access -->
                <div v-if="sharedEntries.length > 0" class="space-y-0.5">
                    <div v-for="entry in sharedEntries" :key="entry.id"
                        class="flex items-center justify-between gap-2 py-2 px-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 group"
                        :data-testid="dashboardMode ? `share-entry-${entry.role}` : undefined">
                        <div class="flex items-center gap-2.5 min-w-0">
                            <div class="w-7 h-7 flex-shrink-0 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 dark:text-gray-400">
                                <Icon v-if="entry.principal_type === 'group'" name="heroicons:user-group" class="w-4 h-4" />
                                <template v-else>{{ (entry.user_name || entry.user_email || '?').charAt(0).toUpperCase() }}</template>
                            </div>
                            <div class="flex flex-col min-w-0">
                                <span class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ entry.principal_type === 'group' ? (entry.group_name || (dashboardMode ? $t('dashboardCollab.share.unknownGroup') : '')) : (entry.user_name || entry.user_email || (dashboardMode ? $t('dashboardCollab.share.unknownPerson') : '')) }}</span>
                                <span v-if="entry.principal_type === 'group'" class="text-xs text-gray-400 truncate">{{ $t('share.groupMembers', entry.member_count || 0) }}</span>
                                <span v-else-if="entry.user_name && entry.user_email" class="text-xs text-gray-400 truncate">{{ entry.user_email }}</span>
                            </div>
                        </div>
                        <div v-if="dashboardMode" class="flex items-center gap-1 flex-shrink-0">
                            <RoleSelect v-if="canShare" :model-value="entry.role" :view-disabled-reason="viewDisabledReason"
                                :disabled="isSaving" @update:model-value="(role) => setEntryRole(entry, role)" />
                            <span v-else class="text-xs text-gray-500 dark:text-gray-400 pe-1">
                                {{ entry.role === 'edit' ? $t('dashboardCollab.share.canEdit') : $t('dashboardCollab.share.canView') }}
                            </span>
                            <UTooltip v-if="canShare" :text="$t('dashboardCollab.share.removeAccess')">
                                <button @click="removeSharedEntry(entry)" :disabled="isSaving" :aria-label="$t('dashboardCollab.share.removeAccess')"
                                    data-testid="share-remove"
                                    class="sm:opacity-0 sm:group-hover:opacity-100 focus:opacity-100 text-gray-400 hover:text-red-500 transition-opacity p-1 disabled:opacity-40">
                                    <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
                                </button>
                            </UTooltip>
                        </div>
                        <button v-else @click="removeSharedEntry(entry)"
                            class="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 transition-opacity p-1">
                            <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
                        </button>
                    </div>
                </div>

                <!-- The owner's switch (collab01): whether editors may change
                     who has access. -->
                <div v-if="showEditorsCanShare" class="flex items-start justify-between gap-3 mt-4 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg"
                    data-testid="editors-can-share">
                    <div class="flex flex-col min-w-0">
                        <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('dashboardCollab.share.editorsCanShare') }}</span>
                        <span class="text-[11px] text-gray-400">{{ $t('dashboardCollab.share.editorsCanShareDesc') }}</span>
                    </div>
                    <UToggle v-model="editorsCanShare" size="sm" :disabled="isSaving" class="flex-shrink-0 mt-0.5"
                        data-testid="editors-can-share-toggle" @update:model-value="onEditorsCanShareChange" />
                </div>
            </div>

            <!-- Notify recipients (optional email) -->
            <NotifyRecipientPicker
                v-if="smtpEnabled && isShared && (!dashboardMode || canShare)"
                :report-id="report.id"
                :notification-type="shareType === 'artifact' ? 'share_dashboard' : 'share_conversation'"
                :share-url="shareUrl"
                :notify-path="dashboardMode ? `/dashboards/${dashboardId}/notify` : undefined" />
        </div>
    </UModal>
</template>

<script lang="ts" setup>
import { ref, computed, watch, nextTick } from 'vue'
import RoleSelect from '~/components/dashboard/share/RoleSelect.vue'

const props = withDefaults(defineProps<{
    report: any
    shareType: 'artifact' | 'conversation'
    title: string
    compact?: boolean
    // The dashboard open in the editor (phase 8): its own sharing and
    // settings, its /d link. Without it, the conversation's, as before.
    dashboardId?: string
}>(), {
    compact: false,
})
const emit = defineEmits<{ (e: 'slug-changed', slug: string | null): void }>()

const toast = useToast()
const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()
const dashboardMode = computed(() => props.shareType === 'artifact' && !!props.dashboardId)
const dashboardPublished = ref(true)
const dashboardSlug = ref<string | null>(null)
// The visibility as last saved (the conversation's lives on the report).
const savedDashboardVisibility = ref('none')
// Roles (collab01): what the reader may do here (null: not known, the
// server still decides), the editors, the owner, the dashboard's project.
const capabilities = ref<string[] | null>(null)
const hasCap = (cap: string, unknown = true) => (capabilities.value === null ? unknown : capabilities.value.includes(cap))
const canShare = computed(() => !dashboardMode.value || hasCap('share'))
const canMakePublic = computed(() => !dashboardMode.value || hasCap('make_public'))
const canSettings = computed(() => !dashboardMode.value || hasCap('settings'))
// The owner's: credentials and "Editors can share". While unknown, the
// credentials switch stays as it was and the editors' switch stays hidden.
const canRunIdentity = computed(() => !dashboardMode.value || hasCap('run_identity'))
const showEditorsCanShare = computed(() => dashboardMode.value && hasCap('run_identity', false))
const editorGrants = ref<{ user_ids: string[]; group_ids: string[] }>({ user_ids: [], group_ids: [] })
const editorsCanShare = ref(true)
const ownerId = ref<string | null>(null)
const projectId = ref<string | null>(null)
const projectName = ref<string | null>(null)
// The role new picks get: "Can edit" until viewers can be added.
const newRole = ref<'view' | 'edit'>('view')
const { data: authUser } = useAuth()
const currentUserId = computed(() => (authUser.value as any)?.id || null)
const { smtpEnabled } = useAppSettings()
const modalOpen = ref(false)
const isSaving = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)
const inputValue = ref('')
const showDropdown = ref(false)
// Pending picks not yet saved: individual users or whole groups
const pendingPrincipals = ref<{ kind: 'user' | 'group'; id?: string; name?: string; email?: string; memberCount?: number }[]>([])
const sharedEntries = ref<any[]>([])
const copied = ref(false)
// Include Data Tab when sharing (show historical execution data)
const includeDataTab = ref(true)

// Chat on the shared artifact page: toggle + agent scope. Scope maps onto the
// backend's artifact_chat_data_source_ids: 'agents' with everything checked =
// null (inherit the roster, sent as the ["*"] reset sentinel), a subset = that
// list, 'data_only' = [].
const chatEnabled = ref(false)
const chatScope = ref<'agents' | 'data_only'>('agents')
const chatAgentIds = ref<string[]>([])
const reportAgents = ref<{ id: string; name: string }[]>([])
// Default model for artifact-page chat. Stored values: null = inherit the
// report's own model, ORG_DEFAULT_MODEL = the organization default, else a
// model id. USelectMenu treats '' as "nothing selected" (blank label), so
// inherit carries its own sentinel and is sent as the backend's "" clear value.
const INHERIT_MODEL = '__inherit__'
const ORG_DEFAULT_MODEL = 'org_default'
const chatModelId = ref(INHERIT_MODEL)
const chatModels = ref<{ id: string; name: string; provider?: string; isDefault?: boolean }[]>([])
// The report's own model override (what INHERIT_MODEL resolves to).
const reportModelId = ref('')
const modelLabel = (m: { name: string; provider?: string }) => (m.provider ? `${m.name} · ${m.provider}` : m.name)
// Without a report model, inheriting IS the organization default: show one
// option for both stored values instead of two identical ones.
const chatModelValue = computed({
    get: () => (!reportModelId.value && chatModelId.value === INHERIT_MODEL ? ORG_DEFAULT_MODEL : chatModelId.value),
    set: (v: string) => { chatModelId.value = v },
})
const chatModelOptions = computed(() => {
    const orgDefault = chatModels.value.find(m => m.isDefault)
    const orgOption = {
        value: ORG_DEFAULT_MODEL,
        label: orgDefault ? t('share.chatModelOrgDefault', { name: orgDefault.name }) : t('share.chatModelOrgDefaultPlain'),
    }
    const options = reportModelId.value
        ? (() => {
            const inherited = chatModels.value.find(m => m.id === reportModelId.value)
            return [
                { value: INHERIT_MODEL, label: inherited ? t('share.chatModelReport', { name: inherited.name }) : t('share.chatModelReportPlain') },
                orgOption,
            ]
        })()
        : [orgOption]
    options.push(...chatModels.value.map(m => ({ value: m.id, label: modelLabel(m) })))
    // A stored pick that was since disabled/deleted: keep it visible (chat
    // falls back to the default at run time) instead of a blank select.
    if (![INHERIT_MODEL, ORG_DEFAULT_MODEL].includes(chatModelId.value) && !chatModels.value.some(m => m.id === chatModelId.value)) {
        options.push({ value: chatModelId.value, label: t('share.chatModelUnavailable') })
    }
    return options
})

const currentVisibility = ref('none')
const conversationShareToken = ref<string | null>(null)
// Whose credentials a shared-dashboard viewer's "Run" uses:
// off = the viewer's own ('viewer'), on = on behalf of the owner ('creator')
const runAsCreator = ref(false)
// RLS dashboards force per-viewer identity — creator mode is disabled.
const hasRls = ref(false)
// Only user-scoped (user_required) sources make the run-identity toggle
// meaningful; on system-only credentials both identities resolve to the same
// credentials, so the control is hidden. RLS still shows it (disabled) to
// explain why runs are always per-viewer.
const hasUserScoped = ref(false)
const showRunIdentity = computed(() => hasUserScoped.value || hasRls.value)

// Public is the owner's (collab01): listed but off for an editor, unless
// the dashboard already is public (they may leave it as it is).
const publicLocked = computed(() => dashboardMode.value && !canMakePublic.value && savedDashboardVisibility.value !== 'public')
const visibilityOptions = computed(() => [
    { value: 'none', label: t('share.visibilityPrivate'), description: t('share.visibilityPrivateDesc'), icon: 'heroicons:lock-closed' },
    { value: 'shared', label: t('share.visibilityShared'), description: t('share.visibilitySharedDesc'), icon: 'heroicons:user-group' },
    { value: 'internal', label: t('share.visibilityInternal'), description: t('share.visibilityInternalDesc'), icon: 'heroicons:building-office' },
    {
        value: 'public', label: t('share.visibilityPublic'), description: t('share.visibilityPublicDesc'), icon: 'heroicons:globe-alt',
        disabled: publicLocked.value,
        disabledReason: publicLocked.value ? t('dashboardCollab.share.publicOwnerOnly') : '',
    },
])

// Viewers come from a list only while the dashboard is published and its
// visibility is private or "Specific people" (a pick then switches it to
// "Specific people"); otherwise "Can view" is off, with the reason.
const viewDisabledReason = computed<string | null>(() => {
    if (!dashboardMode.value) return null
    if (!dashboardPublished.value) return t('dashboardCollab.share.viewersAfterPublish')
    if (savedDashboardVisibility.value === 'internal') return t('dashboardCollab.share.viewersOrgWide')
    if (savedDashboardVisibility.value === 'public') return t('dashboardCollab.share.viewersPublic')
    return null
})
watch(viewDisabledReason, (reason) => { if (reason) newRole.value = 'edit' }, { immediate: true })

const visibilityField = computed(() =>
    props.shareType === 'artifact' ? 'artifact_visibility' : 'conversation_visibility'
)

const isShared = computed(() => currentVisibility.value !== 'none')

const shareDescription = computed(() =>
    dashboardMode.value
        ? t('dashboardCollab.share.description')
        : props.shareType === 'artifact'
            ? t('share.shareDashboardDesc')
            : t('share.shareConversationDesc')
)

const selectedOption = computed(() =>
    visibilityOptions.value.find(o => o.value === currentVisibility.value) || visibilityOptions.value[0]
)

const buttonLabel = computed(() => {
    if (!isShared.value) return props.shareType === 'artifact' ? t('share.shareDashboard') : t('share.share')
    return selectedOption.value.label
})

const buttonIcon = computed(() => selectedOption.value.icon)

const linkPrefix = computed(() => `${window.location.origin}/d/`)
const shareUrl = computed(() => {
    if (dashboardMode.value) {
        return `${linkPrefix.value}${dashboardSlug.value || props.dashboardId}`
    }
    if (props.shareType === 'artifact') {
        return `${window.location.origin}/r/${props.report.id}`
    }
    const token = conversationShareToken.value || props.report?.conversation_share_token
    return token ? `${window.location.origin}/c/${token}` : ''
})

// Org members and groups for autocomplete
const members = ref<{ id: string; name: string; email: string }[]>([])
const groups = ref<{ id: string; name: string; memberCount: number }[]>([])
const fetchMembers = async () => {
    try {
        const res = await useMyFetch('/organization/members')
        if (res.data.value) {
            members.value = (res.data.value as any[]).map((u: any) => ({
                id: u.id,
                name: u.name || '',
                email: u.email,
            }))
        }
    } catch { /* silent */ }
}

const fetchGroups = async () => {
    try {
        const res = await useMyFetch('/organization/groups')
        if (res.data.value) {
            groups.value = (res.data.value as any[]).map((g: any) => ({
                id: g.id,
                name: g.name || '',
                memberCount: g.member_count || 0,
            }))
        }
    } catch { /* silent */ }
}

const ownerMember = computed(() => members.value.find(m => m.id === ownerId.value) || null)
const ownerLabel = computed(() => {
    const name = ownerMember.value?.name || ownerMember.value?.email || t('dashboardCollab.share.unknownPerson')
    return ownerId.value && ownerId.value === currentUserId.value ? t('dashboardCollab.share.ownerYou', { name }) : name
})

const filteredOptions = computed(() => {
    const q = inputValue.value.toLowerCase().trim()
    if (!q) return []
    const existingUserIds = new Set([
        ...sharedEntries.value.filter(e => e.principal_type !== 'group').map(e => e.user_id),
        ...pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id),
    ])
    const existingGroupIds = new Set([
        ...sharedEntries.value.filter(e => e.principal_type === 'group').map(e => e.group_id),
        ...pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id),
    ])
    const groupMatches = groups.value.filter(
        g => !existingGroupIds.has(g.id) && g.name.toLowerCase().includes(q)
    ).map(g => ({ kind: 'group' as const, id: g.id, name: g.name, email: '', memberCount: g.memberCount }))
    const userMatches = members.value.filter(
        m => !existingUserIds.has(m.id) &&
            m.id !== props.report?.user?.id &&
            m.id !== ownerId.value &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).map(m => ({ kind: 'user' as const, id: m.id, name: m.name, email: m.email, memberCount: 0 }))
    return [...groupMatches, ...userMatches].slice(0, 6)
})

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

const addPrincipal = (option: { kind: 'user' | 'group'; id: string; name: string; email?: string; memberCount?: number }) => {
    if (!pendingPrincipals.value.find(p => p.kind === option.kind && p.id === option.id)) {
        pendingPrincipals.value.push({ kind: option.kind, id: option.id, name: option.name, email: option.email, memberCount: option.memberCount })
    }
    inputValue.value = ''
    showDropdown.value = false
}

const addEmailAsPending = (email: string) => {
    const clean = email.trim().toLowerCase()
    if (!clean) return
    if (!isValidEmail(clean)) {
        // Not an email — maybe an exact group name was typed.
        const group = groups.value.find(g => g.name.toLowerCase() === clean)
        if (group) addPrincipal({ kind: 'group', ...group })
        return
    }
    const member = members.value.find(m => m.email.toLowerCase() === clean)
    if (member) {
        addPrincipal({ kind: 'user', ...member })
    } else {
        toast.add({ title: t('share.userNotFound'), color: 'orange' })
    }
}

const removePendingPrincipal = (idx: number) => pendingPrincipals.value.splice(idx, 1)

const handleEnter = () => {
    if (filteredOptions.value.length > 0) {
        addPrincipal(filteredOptions.value[0])
    } else {
        addEmailAsPending(inputValue.value)
    }
}

const handleComma = () => addEmailAsPending(inputValue.value)

const handleBackspace = () => {
    if (!inputValue.value && pendingPrincipals.value.length > 0) pendingPrincipals.value.pop()
}

const onInput = () => { showDropdown.value = true }

const onBlur = () => {
    setTimeout(() => {
        showDropdown.value = false
        if (inputValue.value && isValidEmail(inputValue.value)) addEmailAsPending(inputValue.value)
    }, 200)
}

// API calls
// The dashboard's own sharing and settings (phase 8), in the shape the
// conversation's answer has, so the rest of the modal reads either.
const fetchDashboard = async () => {
    const [sharing, settings] = await Promise.all([
        useMyFetch(`/dashboards/${props.dashboardId}/sharing`),
        useMyFetch(`/dashboards/${props.dashboardId}/settings`),
    ])
    const sh = (sharing.data.value || {}) as any
    const st = (settings.data.value || {}) as any
    applyDashboardSharing(sh)
    dashboardSlug.value = st.slug || null
    return {
        artifact_visibility: sh.visibility || 'none',
        shared_run_identity: st.shared_run_identity,
        include_data_tab: st.include_data_tab,
        artifact_chat_enabled: st.chat_enabled,
        artifact_chat_model_id: st.chat_model_id,
        artifact_chat_data_source_ids: st.chat_data_source_ids,
    }
}
const dashboardGrants = ref<{ user_ids: string[]; group_ids: string[] }>({ user_ids: [], group_ids: [] })
// A sharing answer (GET or PUT) into the modal's state.
function applyDashboardSharing(sh: any) {
    if (!sh) return
    dashboardPublished.value = sh.published !== false
    savedDashboardVisibility.value = sh.visibility || 'none'
    dashboardGrants.value = { user_ids: sh.user_ids || [], group_ids: sh.group_ids || [] }
    editorGrants.value = { user_ids: sh.editor_user_ids || [], group_ids: sh.editor_group_ids || [] }
    editorsCanShare.value = sh.editors_can_share !== false
    ownerId.value = sh.owner_id || null
    if (projectId.value !== (sh.project_id || null)) {
        projectId.value = sh.project_id || null
        projectName.value = null
    }
}
// What the reader may do (collab01): the draft's meta answers editors; a
// dashboard with no draft for them answers on its published page.
const fetchCapabilities = async () => {
    const id = props.dashboardId
    try {
        let res = await useMyFetch(`/d/${id}/draft`)
        if (res.error.value || !res.data.value) res = await useMyFetch(`/d/${id}`)
        if (id !== props.dashboardId) return
        const caps = (res.data.value as any)?.capabilities
        capabilities.value = !res.error.value && Array.isArray(caps) ? caps : null
    } catch {
        capabilities.value = null
    }
}
// "Everyone in project <name> can edit": the name when the reader can open
// the project; else the line says "this dashboard's project".
const fetchProjectName = async () => {
    const pid = projectId.value
    if (!pid || projectName.value) return
    try {
        const res = await useMyFetch(`/projects/${pid}`)
        if (pid === projectId.value && !res.error.value) projectName.value = (res.data.value as any)?.name || null
    } catch { /* the unnamed line */ }
}
// Writes onto the report object only for the conversation's own settings.
const setOnReport = (key: string, value: any) => {
    if (props.report && !dashboardMode.value) props.report[key] = value
}

const fetchVisibility = async () => {
    try {
        const res = await useMyFetch(`/reports/${props.report.id}`, { method: 'GET' })
        if (res.data.value) {
            const data = dashboardMode.value
                ? { ...(res.data.value as any), ...(await fetchDashboard()) }
                : res.data.value as any
            currentVisibility.value = data[visibilityField.value] || 'none'
            hasRls.value = !!data.has_rls
            hasUserScoped.value = !!data.has_user_scoped
            if (data.shared_run_identity !== undefined) {
                runAsCreator.value = data.shared_run_identity === 'creator'
                setOnReport('shared_run_identity', data.shared_run_identity)
            }
            if (data.include_data_tab !== undefined) {
                includeDataTab.value = data.include_data_tab !== false
                setOnReport('include_data_tab', data.include_data_tab)
            }
            if (data.artifact_chat_enabled !== undefined) {
                chatEnabled.value = data.artifact_chat_enabled === true
                setOnReport('artifact_chat_enabled', data.artifact_chat_enabled)
            }
            // Candidate agents = what the report actually uses (attached
            // roster, or recovered from its runs for Auto reports) — not the
            // raw attachment list, which is empty under Auto.
            try {
                const agentsRes = await useMyFetch(`/reports/${props.report.id}/artifact_chat/agents`)
                const payload = agentsRes.data.value as any
                reportAgents.value = (payload?.agents || []).map((a: any) => ({ id: a.id, name: a.name }))
            } catch {
                reportAgents.value = (data.data_sources || []).map((ds: any) => ({ id: ds.id, name: ds.name }))
            }
            chatModelId.value = data.artifact_chat_model_id || INHERIT_MODEL
            setOnReport('artifact_chat_model_id', data.artifact_chat_model_id || null)
            reportModelId.value = data.model_id || ''
            try {
                const modelsRes = await useMyFetch('/llm/models?is_enabled=true')
                chatModels.value = ((modelsRes.data.value as any[]) || []).map((m: any) => ({
                    id: m.id, name: m.name || m.model_id, provider: m.provider?.name, isDefault: !!m.is_default,
                }))
            } catch { chatModels.value = [] }
            const storedIds = data.artifact_chat_data_source_ids
            if (storedIds === null || storedIds === undefined) {
                chatScope.value = reportAgents.value.length > 0 ? 'agents' : 'data_only'
                chatAgentIds.value = reportAgents.value.map(a => a.id)
            } else if (Array.isArray(storedIds) && storedIds.length === 0) {
                chatScope.value = 'data_only'
                chatAgentIds.value = reportAgents.value.map(a => a.id)
            } else {
                chatScope.value = 'agents'
                chatAgentIds.value = storedIds
            }
            if (data.conversation_share_token !== undefined) {
                conversationShareToken.value = data.conversation_share_token
                if (props.report) props.report.conversation_share_token = data.conversation_share_token
            }
        }
    } catch { /* silent */ }
}

const fetchShares = async () => {
    if (dashboardMode.value) {
        // The dashboard's grants, named from the organization's lists:
        // editors first, then viewers (someone in both is an editor).
        const user = (id: string, role: 'view' | 'edit') => {
            const m = members.value.find(x => x.id === id)
            return { id: `user:${id}`, role, principal_type: 'user', user_id: id, user_name: m?.name || '', user_email: m?.email || '' }
        }
        const group = (id: string, role: 'view' | 'edit') => {
            const g = groups.value.find(x => x.id === id)
            return { id: `group:${id}`, role, principal_type: 'group', group_id: id, group_name: g?.name || '', member_count: g?.memberCount || 0 }
        }
        const editorUsers = new Set(editorGrants.value.user_ids)
        const editorGroups = new Set(editorGrants.value.group_ids)
        sharedEntries.value = [
            ...editorGrants.value.user_ids.map(id => user(id, 'edit')),
            ...editorGrants.value.group_ids.map(id => group(id, 'edit')),
            ...dashboardGrants.value.user_ids.filter(id => !editorUsers.has(id)).map(id => user(id, 'view')),
            ...dashboardGrants.value.group_ids.filter(id => !editorGroups.has(id)).map(id => group(id, 'view')),
        ]
        return
    }
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/shares/${props.shareType}`)
        if (res.data.value) {
            sharedEntries.value = res.data.value as any[]
        }
    } catch { /* silent */ }
}

// Viewers only: a dashboard's editors (role 'edit') are a list of their own.
const sharedUserIds = () => sharedEntries.value
    .filter(e => e.principal_type !== 'group' && e.role !== 'edit')
    .map(e => e.user_id)
const sharedGroupIds = () => sharedEntries.value
    .filter(e => e.principal_type === 'group' && e.role !== 'edit')
    .map(e => e.group_id)

type DashboardSharingExtra = { editor_user_ids?: string[]; editor_group_ids?: string[]; editors_can_share?: boolean }
// One PUT of the dashboard's sharing; a list left out stays as it is.
const saveDashboardSharing = async (visibility: string, userIds?: string[], groupIds?: string[], extra: DashboardSharingExtra = {}) => {
    isSaving.value = true
    const before = savedDashboardVisibility.value
    try {
        const body: any = { visibility, ...extra }
        if (userIds) body.user_ids = userIds
        if (groupIds) body.group_ids = groupIds
        const res = await useMyFetch(`/dashboards/${props.dashboardId}/sharing`, { method: 'PUT', body })
        if (res.error.value) throw res.error.value
        const data = res.data.value as any
        if (data) applyDashboardSharing({ ...data, visibility: data.visibility || visibility })
        currentVisibility.value = savedDashboardVisibility.value
        await fetchShares()
        toast.add({
            title: visibility === 'none' && before !== 'none' ? t('share.sharingDisabled') : t('share.sharingUpdated'),
            color: 'green',
        })
        return true
    } catch (e) {
        currentVisibility.value = savedDashboardVisibility.value
        toast.add({ title: t('share.sharingFailed'), description: getErrorMessage(e), color: 'red' })
        return false
    } finally {
        isSaving.value = false
    }
}

// Roles on a dashboard (collab01). Someone in both lists is an editor, so a
// change of role moves the id from one list to the other in one PUT.
const principalId = (entry: any): string => (entry.principal_type === 'group' ? entry.group_id : entry.user_id)
const without = (ids: string[], id: string) => ids.filter(x => x !== id)

const inviteToDashboard = async () => {
    const users = pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id).filter(Boolean) as string[]
    const groupIds = pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id).filter(Boolean) as string[]
    let ok: boolean
    if (newRole.value === 'edit' || viewDisabledReason.value) {
        // Editors stand whatever the visibility, before the first publish too.
        ok = await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined, {
            editor_user_ids: [...editorGrants.value.user_ids, ...users],
            editor_group_ids: [...editorGrants.value.group_ids, ...groupIds],
        })
    } else {
        // A viewer pick on a private dashboard makes it "Specific people".
        currentVisibility.value = 'shared'
        ok = await saveDashboardSharing('shared', [...sharedUserIds(), ...users], [...sharedGroupIds(), ...groupIds])
    }
    if (ok) pendingPrincipals.value = []
}

const setEntryRole = async (entry: any, role: 'view' | 'edit') => {
    if (role === entry.role) return
    const id = principalId(entry)
    const isGroup = entry.principal_type === 'group'
    const editors = without(isGroup ? editorGrants.value.group_ids : editorGrants.value.user_ids, id)
    const viewers = without(isGroup ? sharedGroupIds() : sharedUserIds(), id)
    if (role === 'edit') editors.push(id)
    else viewers.push(id)
    const extra: DashboardSharingExtra = isGroup ? { editor_group_ids: editors } : { editor_user_ids: editors }
    if (role === 'edit') {
        await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined, extra)
        return
    }
    // A viewer counts only while the visibility is "Specific people".
    currentVisibility.value = 'shared'
    await saveDashboardSharing('shared', isGroup ? undefined : viewers, isGroup ? viewers : undefined, extra)
}

const removeFromDashboard = async (entry: any) => {
    const id = principalId(entry)
    const isGroup = entry.principal_type === 'group'
    if (entry.role === 'edit') {
        await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined,
            isGroup ? { editor_group_ids: without(editorGrants.value.group_ids, id) } : { editor_user_ids: without(editorGrants.value.user_ids, id) })
        return
    }
    const users = isGroup ? sharedUserIds() : without(sharedUserIds(), id)
    const groupIds = isGroup ? without(sharedGroupIds(), id) : sharedGroupIds()
    if (users.length === 0 && groupIds.length === 0 && savedDashboardVisibility.value === 'shared') {
        // The last viewer: private again, as for a conversation. Editors stay.
        currentVisibility.value = 'none'
        await saveDashboardSharing('none')
    } else {
        await saveDashboardSharing('shared', users, groupIds)
    }
}

const onEditorsCanShareChange = async (value: boolean) => {
    const ok = await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined, { editors_can_share: value })
    if (!ok) editorsCanShare.value = !value
}

// One PUT per change of a dashboard setting (phase 8).
const saveDashboardSettings = async (body: Record<string, any>, revert: () => void) => {
    isSaving.value = true
    try {
        const res = await useMyFetch(`/dashboards/${props.dashboardId}/settings`, { method: 'PUT', body })
        if (res.error.value) throw res.error.value
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
        return res.data.value as any
    } catch (e) {
        revert()
        toast.add({ title: t('share.sharingFailed'), description: getErrorMessage(e), color: 'red' })
        return null
    } finally {
        isSaving.value = false
    }
}

const saveVisibility = async (visibility: string, userIds?: string[], groupIds?: string[]) => {
    if (dashboardMode.value) return saveDashboardSharing(visibility, userIds, groupIds)
    isSaving.value = true
    try {
        const body: any = { visibility }
        if (userIds) body.shared_user_ids = userIds
        if (groupIds) body.shared_group_ids = groupIds
        // include_data_tab is deliberately NOT sent here. It has its own write
        // path (onIncludeDataTabChange), and omitting it means the backend
        // leaves the stored value alone — so a visibility change or an invite
        // can never overwrite the setting with a stale local ref.
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/${props.shareType}`, {
            method: 'PUT',
            body,
        })
        if (res.error.value) throw res.error.value

        if (props.report) {
            props.report[visibilityField.value] = visibility
        }
        // Surface the resulting report_shared / artifact_shared session-event strip.
        window.dispatchEvent(new CustomEvent('report:mutated', { detail: { reportId: props.report?.id, kind: 'share' } }))

        const data = res.data.value as any
        if (props.shareType === 'conversation' && data) {
            const token = data.conversation_share_token ?? null
            conversationShareToken.value = token
            if (props.report) props.report.conversation_share_token = token
        }

        toast.add({
            title: visibility === 'none' ? t('share.sharingDisabled') : t('share.sharingUpdated'),
            color: 'green',
        })
    } catch {
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const onRunIdentityChange = async (value: boolean) => {
    const identity = value ? 'creator' : 'viewer'
    if (dashboardMode.value) {
        await saveDashboardSettings({ shared_run_identity: identity }, () => { runAsCreator.value = identity !== 'creator' })
        return
    }
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            // Re-sends the current visibility unchanged; omitting
            // shared_user_ids leaves the recipient list untouched.
            body: { visibility: currentVisibility.value, run_identity: identity },
        })
        if (res.error.value) throw res.error.value
        if (props.report) props.report.shared_run_identity = identity
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
    } catch {
        runAsCreator.value = identity !== 'creator'
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const onIncludeDataTabChange = async (value: boolean) => {
    if (dashboardMode.value) {
        await saveDashboardSettings({ include_data_tab: value }, () => { includeDataTab.value = !value })
        return
    }
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            // Re-sends the current visibility unchanged; omitting
            // shared_user_ids leaves the recipient list untouched.
            body: { visibility: currentVisibility.value, include_data_tab: value },
        })
        if (res.error.value) throw res.error.value
        if (props.report) props.report.include_data_tab = value
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
    } catch {
        // Put the box back where it was — the setting did not change.
        includeDataTab.value = !value
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

// One PUT per chat-settings change, mirroring onIncludeDataTabChange: resend
// the current visibility unchanged and only the field being written.
const DASHBOARD_CHAT_KEYS: Record<string, string> = {
    artifact_chat_enabled: 'chat_enabled',
    artifact_chat_data_source_ids: 'chat_data_source_ids',
    artifact_chat_model_id: 'chat_model_id',
}
const saveChatSettings = async (body: Record<string, any>, revert: () => void) => {
    if (dashboardMode.value) {
        const mapped = Object.fromEntries(Object.entries(body).map(([k, v]) => [DASHBOARD_CHAT_KEYS[k] || k, v]))
        await saveDashboardSettings(mapped, revert)
        return
    }
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            body: { visibility: currentVisibility.value, ...body },
        })
        if (res.error.value) throw res.error.value
        toast.add({ title: t('share.sharingUpdated'), color: 'green' })
    } catch {
        revert()
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const chatScopeIdsPayload = (): string[] => {
    if (chatScope.value === 'data_only') return []
    const all = reportAgents.value.map(a => a.id)
    const picked = chatAgentIds.value.filter(id => all.includes(id))
    // Everything checked = inherit the roster (["*"] resets to null server-side).
    if (picked.length === all.length && all.length > 0) return ['*']
    return picked
}

const onChatEnabledChange = async (value: boolean) => {
    setOnReport('artifact_chat_enabled', value)
    await saveChatSettings(
        { artifact_chat_enabled: value },
        () => { chatEnabled.value = !value; setOnReport('artifact_chat_enabled', !value) },
    )
}

const onChatScopeChange = async () => {
    if (chatScope.value === 'agents' && chatAgentIds.value.length === 0) {
        chatAgentIds.value = reportAgents.value.map(a => a.id)
    }
    await saveChatSettings({ artifact_chat_data_source_ids: chatScopeIdsPayload() }, () => {})
}

const onChatModelChange = async (value: string) => {
    const next = value === INHERIT_MODEL ? '' : value
    const prev = (dashboardMode.value ? savedChatModelId.value : props.report?.artifact_chat_model_id) || ''
    if (next === prev) return
    setOnReport('artifact_chat_model_id', next || null)
    if (dashboardMode.value) savedChatModelId.value = next
    await saveChatSettings(
        { artifact_chat_model_id: next },
        () => {
            chatModelId.value = prev || INHERIT_MODEL
            setOnReport('artifact_chat_model_id', prev || null)
            if (dashboardMode.value) savedChatModelId.value = prev
        },
    )
}

const onChatAgentsChange = async () => {
    await saveChatSettings({ artifact_chat_data_source_ids: chatScopeIdsPayload() }, () => {})
}

const onVisibilityChange = async (value: string) => {
    const prev = dashboardMode.value ? savedDashboardVisibility.value : (props.report?.[visibilityField.value] || 'none')
    if (value === prev) return

    const userIds = value === 'shared' ? sharedUserIds() : undefined
    const groupIds = value === 'shared' ? sharedGroupIds() : undefined
    await saveVisibility(value, userIds, groupIds)
}

const invitePrincipals = async () => {
    if (pendingPrincipals.value.length === 0) return
    if (dashboardMode.value) return inviteToDashboard()

    if (currentVisibility.value === 'none') {
        currentVisibility.value = 'shared'
    }

    const allUserIds = [
        ...sharedUserIds(),
        ...pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id).filter(Boolean),
    ]
    const allGroupIds = [
        ...sharedGroupIds(),
        ...pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id).filter(Boolean),
    ]

    await saveVisibility(currentVisibility.value === 'shared' ? 'shared' : currentVisibility.value, allUserIds, allGroupIds)
    await fetchShares()
    pendingPrincipals.value = []
}

const removeSharedEntry = async (entry: any) => {
    if (dashboardMode.value) return removeFromDashboard(entry)
    const remainingUsers = sharedEntries.value
        .filter(e => e.id !== entry.id && e.principal_type !== 'group')
        .map(e => e.user_id)
    const remainingGroups = sharedEntries.value
        .filter(e => e.id !== entry.id && e.principal_type === 'group')
        .map(e => e.group_id)

    if (remainingUsers.length === 0 && remainingGroups.length === 0 && currentVisibility.value === 'shared') {
        currentVisibility.value = 'none'
        await saveVisibility('none')
    } else {
        await saveVisibility('shared', remainingUsers, remainingGroups)
    }

    await fetchShares()
}

// The dashboard's name in its link (phase 8): owner-only, server-checked;
// the same rule here so a bad name is caught while typing.
const editingName = ref(false)
const nameDraft = ref('')
const nameInputRef = ref<HTMLInputElement | null>(null)
const NAME_RULE = /^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$/
const nameValid = computed(() => !nameDraft.value || NAME_RULE.test(nameDraft.value))
watch(nameDraft, (v) => { if (v !== v.toLowerCase()) nameDraft.value = v.toLowerCase() })
const startEditingName = async () => {
    nameDraft.value = dashboardSlug.value || ''
    editingName.value = true
    await nextTick()
    nameInputRef.value?.focus()
}
const saveName = async () => {
    if (!nameValid.value) return
    const next = nameDraft.value.trim()
    if (next === (dashboardSlug.value || '')) { editingName.value = false; return }
    const saved = await saveDashboardSettings({ slug: next }, () => {})
    if (saved) {
        dashboardSlug.value = saved.slug || null
        editingName.value = false
        emit('slug-changed', dashboardSlug.value)
    }
}

const copyLink = async () => {
    try {
        await navigator.clipboard.writeText(shareUrl.value)
        copied.value = true
        setTimeout(() => { copied.value = false }, 2000)
    } catch {
        toast.add({ title: t('share.copyFailed'), color: 'red' })
    }
}

const openModal = async () => {
    modalOpen.value = true
    editingName.value = false
    if (dashboardMode.value) {
        currentVisibility.value = savedDashboardVisibility.value
        pendingPrincipals.value = []
        // The grants are named from these lists, so they come first.
        await Promise.all([fetchMembers(), fetchGroups(), fetchCapabilities()])
        await fetchVisibility()
        currentVisibility.value = savedDashboardVisibility.value
        savedChatModelId.value = chatModelId.value === INHERIT_MODEL ? '' : chatModelId.value
        newRole.value = viewDisabledReason.value ? 'edit' : 'view'
        await Promise.all([fetchShares(), fetchProjectName()])
        return
    }
    currentVisibility.value = props.report?.[visibilityField.value] || 'none'
    conversationShareToken.value = props.report?.conversation_share_token ?? null
    runAsCreator.value = props.report?.shared_run_identity === 'creator'
    includeDataTab.value = props.report?.include_data_tab !== false
    chatEnabled.value = props.report?.artifact_chat_enabled === true
    await Promise.all([fetchMembers(), fetchGroups(), fetchVisibility(), fetchShares()])
}
const savedChatModelId = ref('')

// The button shows the dashboard's own sharing (phase 8): read it whenever
// another dashboard is opened in the editor. Silent for whoever may not
// manage it (the modal then shows what the server refuses).
watch(() => props.dashboardId, async (id) => {
    if (!id || props.shareType !== 'artifact') return
    // Another dashboard: its own capabilities, read again on open.
    capabilities.value = null
    const { data, error } = await useMyFetch(`/dashboards/${id}/sharing`)
    if (id !== props.dashboardId || error.value || !data.value) return
    applyDashboardSharing(data.value)
    if (!modalOpen.value) currentVisibility.value = savedDashboardVisibility.value
}, { immediate: true })

// Keep button in sync when report data loads/changes (e.g. after page reload)
watch(
    () => props.report?.[visibilityField.value],
    (val) => {
        if (val && !modalOpen.value && !dashboardMode.value) {
            currentVisibility.value = val
        }
    },
    { immediate: true }
)
</script>
