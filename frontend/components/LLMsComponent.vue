<template>
    <div>
        <!-- Provider chips + add buttons -->
        <div v-if="models.length > 0" class="flex items-center flex-wrap gap-2 mb-3">
            <!-- Chip click filters the model list to that provider; click again to clear. -->
            <button
                v-for="provider in providers"
                :key="provider.id"
                type="button"
                data-testid="provider-chip"
                class="inline-flex items-center gap-1.5 ps-2.5 py-1 border rounded-full transition-colors"
                :class="[
                    useCan('manage_llm_settings') ? 'pe-1' : 'pe-2.5',
                    providerFilterId === provider.id
                        ? 'border-blue-500 bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300'
                        : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800'
                ]"
                @click="toggleProviderFilter(provider.id)"
            >
                <LLMProviderIcon :provider="provider.provider_type" :icon="true" class="h-4 w-4" />
                <span class="text-sm" :class="providerFilterId === provider.id ? '' : 'text-gray-700 dark:text-gray-200'">{{ provider.name }}</span>
                <UTooltip v-if="useCan('manage_llm_settings')" :text="$t('settings.llms.providerSettingsTooltip')">
                    <span
                        role="button"
                        data-testid="provider-chip-gear"
                        class="p-1 rounded-full text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                        @click.stop="openManageProvider(provider.id)"
                    >
                        <UIcon name="i-heroicons-cog-6-tooth" class="w-4 h-4 block" />
                    </span>
                </UTooltip>
            </button>
            <!-- Same button style as the Members tab header (UButton size=xs). -->
            <div v-if="useCan('manage_llm_settings')" class="ms-auto flex items-center gap-2">
                <UButton
                    color="gray"
                    variant="outline"
                    size="xs"
                    icon="i-heroicons-cube"
                    data-testid="add-model-button"
                    @click="addModelModalOpen = true"
                >
                    {{ $t('settings.llms.addModel') }}
                </UButton>
                <UButton
                    color="blue"
                    variant="solid"
                    size="xs"
                    icon="i-heroicons-plus"
                    data-testid="add-provider-button"
                    @click="openAddProvider"
                >
                    {{ $t('settings.llms.addProvider') }}
                </UButton>
            </div>
        </div>

        <!-- Search + org-level toggles -->
        <div v-if="models.length > 0" class="flex justify-between items-center mb-2">
            <div class="w-1/2">
                <input
                    type="text"
                    v-model="searchQuery"
                    :placeholder="$t('settings.llms.searchPlaceholder')"
                    class="border border-gray-300 dark:border-gray-600 rounded-md px-3 py-1.5 text-sm focus:ring-blue-500 focus:border-blue-500 w-full"
                >
            </div>
            <div class="flex items-center space-x-3">
                <div class="flex items-center gap-1.5" data-testid="auto-router-toggle">
                    <span class="text-sm text-gray-600 dark:text-gray-300">{{ $t('settings.llms.autoRouter') }}</span>
                    <!-- Enterprise-only feature: keep the control visible but locked without a license. -->
                    <UTooltip v-if="!modelRoutingLicensed" :text="$t('settings.llms.autoRouterEnterpriseTooltip')">
                        <span
                            class="inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-900 rounded px-1.5 py-0.5"
                            data-testid="auto-router-enterprise-badge"
                        >
                            <UIcon name="i-heroicons-lock-closed" class="w-3 h-3" />
                            {{ $t('settings.llms.autoRouterEnterprise') }}
                        </span>
                    </UTooltip>
                    <UPopover v-else mode="hover" :popper="{ placement: 'bottom' }">
                        <UIcon name="i-heroicons-question-mark-circle" class="w-4 h-4 text-gray-400 hover:text-gray-600 cursor-help" />
                        <template #panel>
                            <div class="p-3 max-w-xs text-xs text-gray-600 dark:text-gray-300 space-y-1.5">
                                <p class="font-semibold text-gray-900 dark:text-white">{{ $t('settings.llms.autoRouterHowTitle') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow1') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow2') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow3') }}</p>
                                <p>• {{ $t('settings.llms.autoRouterHow4') }}</p>
                            </div>
                        </template>
                    </UPopover>
                    <UToggle
                        v-model="autoRouterOn"
                        :disabled="!useCan('manage_llm_settings') || !modelRoutingLicensed"
                        @update:model-value="saveAutoRouter"
                    />
                </div>
                <div class="flex items-center gap-1.5" data-testid="llm-fallback-toggle-group">
                    <span class="text-sm text-gray-600 dark:text-gray-300">{{ $t('settings.llms.fallback') }}</span>
                    <UTooltip v-if="!llmFallbackLicensed" :text="$t('settings.llms.fallbackEnterpriseTooltip')">
                        <span
                            class="inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-900 rounded px-1.5 py-0.5"
                            data-testid="llm-fallback-enterprise-badge"
                        >
                            <UIcon name="i-heroicons-lock-closed" class="w-3 h-3" />
                            {{ $t('settings.llms.autoRouterEnterprise') }}
                        </span>
                    </UTooltip>
                    <UPopover v-else mode="hover" :popper="{ placement: 'bottom' }">
                        <UIcon name="i-heroicons-question-mark-circle" class="w-4 h-4 text-gray-400 hover:text-gray-600 cursor-help" />
                        <template #panel>
                            <div class="p-3 max-w-xs text-xs text-gray-600 dark:text-gray-300 space-y-1.5">
                                <p class="font-semibold text-gray-900 dark:text-white">{{ $t('settings.llms.fallbackHowTitle') }}</p>
                                <p>• {{ $t('settings.llms.fallbackHow1') }}</p>
                                <p>• {{ $t('settings.llms.fallbackHow2') }}</p>
                                <p>• {{ $t('settings.llms.fallbackHow3') }}</p>
                            </div>
                        </template>
                    </UPopover>
                    <UToggle
                        v-model="fallbackOn"
                        :disabled="!useCan('manage_llm_settings') || !llmFallbackLicensed"
                        data-testid="llm-fallback-toggle"
                        @update:model-value="saveFallbackToggle"
                    />
                </div>
            </div>
        </div>
        <!-- LLM fallback chain summary (Enterprise): the order is edited per-row
             in the Fallback column below; this line shows the whole policy at a
             glance since the table isn't sorted by priority. -->
        <div
            v-if="models.length > 0 && fallbackOn"
            class="mb-2 flex items-center flex-wrap gap-1.5 text-xs text-gray-600 dark:text-gray-300"
            data-testid="llm-fallback-chain"
        >
            <UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-400" />
            <span class="font-medium">{{ $t('settings.llms.fallback') }}:</span>
            <span class="inline-flex items-center gap-1">
                <LLMProviderIcon v-if="defaultModel" :provider="defaultModel.provider.provider_type" :model="`${defaultModel.name} ${defaultModel.model_id}`" :icon="true" class="h-3.5 w-3.5" />
                <span>{{ defaultModel?.name || '—' }}</span>
                <span class="text-[10px] uppercase tracking-wide text-gray-400">{{ $t('settings.llms.fallbackPrimary') }}</span>
            </span>
            <template v-for="entry in fallbackOrder" :key="entry.id">
                <UIcon name="i-heroicons-arrow-long-right" class="w-3.5 h-3.5 text-gray-400" />
                <span class="inline-flex items-center gap-1">
                    <LLMProviderIcon :provider="entry.provider_type" :model="`${entry.name} ${entry.model_id}`" :icon="true" class="h-3.5 w-3.5" />
                    <span>{{ entry.name }}</span>
                    <span v-if="!entry.is_active" class="text-[10px] uppercase tracking-wide text-amber-700 dark:text-amber-400">{{ $t('settings.llms.fallbackInactive') }}</span>
                </span>
            </template>
            <span v-if="fallbackOrder.length === 0" class="text-gray-400 italic">{{ $t('settings.llms.fallbackEmpty') }}</span>
        </div>
        <div v-if="models.length > 0" class="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-100 dark:divide-gray-800">
                <thead class="bg-gray-50/60 dark:bg-gray-900">
                    <tr>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('settings.llms.colModel') }}</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('settings.llms.colProvider') }}</th>
                        <th v-if="autoRouterOn" class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400 w-full">{{ $t('settings.llms.colRouting') }}</th>
                        <th v-if="fallbackOn" class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">
                            <UTooltip :text="$t('settings.llms.fallbackColTooltip')">{{ $t('settings.llms.colFallback') }}</UTooltip>
                        </th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('settings.llms.colCost') }}</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('settings.llms.colStatus') }}</th>
                        <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400" v-if="canManageAccess">Access</th>
                        <th class="sticky right-0 z-20 bg-gray-50 dark:bg-gray-900 border-s border-gray-200 dark:border-gray-800 px-4 py-2 text-end text-xs font-medium text-gray-500 dark:text-gray-400" v-if="useCan('manage_llm_settings')">{{ $t('settings.llms.colActions') }}</th>
                    </tr>
                </thead>
                <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-100 dark:divide-gray-800">
                    <!-- Row click opens the model card (per-model settings). Cells with
                         their own controls stop propagation so they don't also open it. -->
                    <tr
                        v-for="model in filteredModels"
                        :key="model.id"
                        class="group hover:bg-gray-50/70 dark:hover:bg-gray-800/50 transition-colors"
                        :class="useCan('manage_llm_settings') ? 'cursor-pointer' : ''"
                        data-testid="model-row"
                        @click="openModelCard(model)"
                    >
                        <td class="px-4 py-2 whitespace-nowrap">
                            <div class="flex items-center">
                                <div class="flex-shrink-0 h-7 w-7 flex items-center justify-center">
                                    <LLMProviderIcon :provider="model.provider.provider_type" :model="`${model.name} ${model.model_id}`" :icon="true" class="h-5 w-5" />
                                </div>
                                <div class="ms-2.5 leading-tight">
                                    <div class="text-sm font-medium text-gray-900 dark:text-white flex items-center gap-1.5">
                                        {{ model.name }}
                                        <span v-if="model.is_default" class="text-xs bg-blue-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeDefault') }}</span>
                                        <UTooltip v-if="model.is_small_default" :text="$t('settings.llms.smallDefaultTooltip')">
                                            <span class="text-xs bg-green-500 text-white px-1.5 py-0.5 rounded-md">{{ $t('settings.llms.badgeSmallDefault') }}</span>
                                        </UTooltip>
                                    </div>
                                    <div v-if="model.model_id !== model.name" class="text-xs text-gray-500 dark:text-gray-400">
                                        {{ $t('settings.llms.modelIdLabel') }}: {{ model.model_id }}
                                    </div>
                                </div>
                            </div>
                        </td>
                        <td class="px-4 py-2 whitespace-nowrap text-sm" data-testid="llm-provider-cell">
                            <div class="flex items-center gap-1.5 text-gray-700 dark:text-gray-300">
                                <LLMProviderIcon :provider="model.provider.provider_type" :icon="true" class="h-4 w-4" />
                                <span>{{ model.provider.name }}</span>
                            </div>
                        </td>
                        <td v-if="autoRouterOn" class="px-4 py-2 text-sm align-middle w-full" data-testid="llm-routing-cell" @click.stop>
                            <div class="min-w-[20rem] max-w-[32rem]">
                                <span v-if="!model.is_enabled" class="text-xs text-gray-400 italic">{{ $t('settings.llms.routingNotAvailable') }}</span>
                                <!-- Routing guidance (only editable target when enabled) -->
                                <div v-else>
                                    <div v-if="editingHintId === model.id" class="flex items-start gap-1">
                                        <textarea
                                            v-model="hintDraft"
                                            rows="2"
                                            :placeholder="$t('settings.llms.routingHintPlaceholder')"
                                            class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-full text-xs focus:outline-none focus:border-blue-500"
                                            @keyup.escape="editingHintId = null"
                                        />
                                        <button type="button" class="text-blue-500 hover:text-blue-700 mt-1" @click="saveHint(model)">
                                            <UIcon name="i-heroicons-check" class="w-4 h-4" />
                                        </button>
                                        <button type="button" class="text-gray-400 hover:text-gray-600 mt-1" @click="editingHintId = null">
                                            <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
                                        </button>
                                    </div>
                                    <UTooltip v-else :text="$t('settings.llms.routingHintTooltip')">
                                        <button
                                            v-if="useCan('manage_llm_settings')"
                                            type="button"
                                            class="group inline-flex items-start gap-1 text-xs text-start"
                                            :class="modelHint(model) ? 'text-gray-600 dark:text-gray-300 hover:text-blue-600' : 'text-gray-400 hover:text-blue-600 italic'"
                                            @click="startHintEdit(model)"
                                        >
                                            <span class="underline decoration-dotted underline-offset-2">{{ modelHint(model) || $t('settings.llms.routingHintPlaceholder') }}</span>
                                            <UIcon name="i-heroicons-pencil-square" class="w-3.5 h-3.5 flex-shrink-0 mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity" />
                                        </button>
                                        <span v-else class="text-xs text-gray-500">{{ modelHint(model) || '—' }}</span>
                                    </UTooltip>
                                </div>
                            </div>
                        </td>
                        <td v-if="fallbackOn" class="px-4 py-2 whitespace-nowrap text-sm align-middle" data-testid="llm-fallback-cell" @click.stop>
                            <!-- The default model is what fails INTO the chain — fixed primary marker. -->
                            <span v-if="model.is_default" class="text-[10px] uppercase tracking-wide text-gray-400">{{ $t('settings.llms.fallbackPrimary') }}</span>
                            <span v-else-if="!model.is_enabled" class="text-xs text-gray-300 dark:text-gray-600">—</span>
                            <select
                                v-else-if="useCan('manage_llm_settings')"
                                class="border border-gray-200 dark:border-gray-700 dark:bg-gray-800 rounded px-1.5 py-0.5 text-xs text-gray-700 dark:text-gray-200"
                                :value="fallbackPriority(model) ?? ''"
                                @change="setFallbackPriority(model, ($event.target as HTMLSelectElement).value)"
                            >
                                <option value="">—</option>
                                <option v-for="p in fallbackPriorityOptions(model)" :key="p" :value="p">{{ p }}</option>
                            </select>
                            <span v-else class="text-xs text-gray-500">{{ fallbackPriority(model) ?? '—' }}</span>
                        </td>
                        <td class="px-4 py-2 whitespace-nowrap text-sm text-gray-600 dark:text-gray-300 tabular-nums" data-testid="llm-cost-cell">
                            <span class="flex items-center gap-3">
                                <span class="flex flex-col items-start leading-tight"><span class="text-[10px] text-gray-400 uppercase tracking-wide">{{ $t('settings.llms.costInput') }}</span><span>{{ formatCostPart(model.input_cost_per_million_tokens_usd) }}</span></span>
                                <span class="flex flex-col items-start leading-tight"><span class="text-[10px] text-gray-400 uppercase tracking-wide">{{ $t('settings.llms.costOutput') }}</span><span>{{ formatCostPart(model.output_cost_per_million_tokens_usd) }}</span></span>
                            </span>
                        </td>
                        <td class="px-4 py-2 whitespace-nowrap text-sm" @click.stop>
                            <UToggle
                                v-model="model.is_enabled"
                                @change="toggleModel(model.id, $event)"
                                :disabled="!useCan('manage_llm_settings') || model.is_default || model.is_small_default"
                            />
                        </td>
                        <td class="px-4 py-2 whitespace-nowrap text-sm" v-if="canManageAccess" @click.stop>
                            <button
                                type="button"
                                class="inline-flex items-center gap-1 text-sm"
                                :class="accessIsRestricted(model) ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'"
                                @click="openAccess(model)"
                                data-testid="llm-access-cell"
                            >
                                <UIcon :name="accessIsRestricted(model) ? 'i-heroicons-lock-closed' : 'i-heroicons-user-group'" class="w-4 h-4" />
                                <span>{{ accessLabel(model) }}</span>
                            </button>
                        </td>
                        <td
                            class="sticky right-0 bg-white dark:bg-gray-900 group-hover:bg-gray-50/70 dark:group-hover:bg-gray-800/50 border-s border-gray-200 dark:border-gray-800 px-4 py-2 whitespace-nowrap text-sm text-end transition-colors"
                            :class="openMenuModelId === model.id ? 'z-30' : 'z-10'"
                            v-if="useCan('manage_llm_settings')"
                            @click.stop
                        >
                            <UDropdown
                                :items="dropdownItemsByModel[model.id]"
                                :popper="{ strategy: 'fixed' }"
                                :ui="{ item: { size: 'text-xs', padding: 'px-2 py-1.5' }, width: 'w-44' }"
                                @update:open="(open) => openMenuModelId = open ? model.id : null"
                            >
                                <UButton size="xs" class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white font-medium transition-colors duration-150" color="white" label="" trailing-icon="i-heroicons-ellipsis-vertical" />
                                <template #item="{ item }">
                                    <span class="truncate" :class="item.danger ? 'text-red-600 dark:text-red-400' : ''">{{ item.label }}</span>
                                    <UIcon v-if="item.icon" :name="item.icon" class="flex-shrink-0 h-3.5 w-3.5 ms-auto" :class="item.danger ? 'text-red-500' : 'text-gray-400 dark:text-gray-500'" />
                                </template>
                            </UDropdown>
                        </td>
                    </tr>
                </tbody>
            </table>
            </div>
        </div>

        <!-- Empty state -->
        <div v-else class="text-center py-12 bg-white dark:bg-gray-900 rounded-lg mt-20">
            <div class="w-48 mx-auto mb-4 flex items-center justify-center">
                <UIcon name="heroicons-cube-transparent" class="w-12 h-12 text-gray-400" />
            </div>
            <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">{{ $t('settings.llms.emptyTitle') }}</h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">{{ $t('settings.llms.emptyHint') }}</p>
            <button
                v-if="useCan('manage_llm_settings')"
                @click="openAddProvider"
                class="bg-blue-500 text-white text-sm px-4 py-2 rounded-md hover:bg-blue-600 transition-colors"
            >
                {{ $t('settings.llms.addProvider') }}
            </button>
        </div>

        <!-- Provider Modal (add provider = existing flow; gear = provider settings) -->
        <LLMProviderModalComponent
            v-model="providerModalOpen"
            :edit-provider-id="editProviderId"
            :start-with-new="startWithNewProvider"
            @update:modelValue="handleProviderModalClose"
        />

        <!-- Add Model Modal -->
        <LLMAddModelModal
            v-model="addModelModalOpen"
            :providers="providers"
            :models="models"
            @added="getModels"
        />

        <!-- Per-model settings card -->
        <LLMModelCardModal
            v-model="cardModalOpen"
            :model="cardModel"
            @updated="getModels"
            @deleted="getModels"
        />

        <!-- Delete confirmation (Actions menu) -->
        <UModal v-model="deleteConfirmOpen" :ui="{ width: 'sm:max-w-sm' }">
            <div class="p-5" data-testid="delete-model-confirm">
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('settings.llms.deleteModelTitle') }}</h3>
                <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
                    {{ $t('settings.llms.deleteModelConfirm', { name: deleteTarget?.name || '' }) }}
                </p>
                <div class="mt-4 flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" size="sm" @click="deleteTarget = null">{{ $t('settings.llms.cancel') }}</UButton>
                    <UButton color="red" size="sm" :loading="isDeletingModel" data-testid="delete-model-confirm-button" @click="confirmDeleteModel">
                        {{ $t('settings.llms.deleteModel') }}
                    </UButton>
                </div>
            </div>
        </UModal>

        <!-- Per-model Access Modal (Enterprise) -->
        <LLMModelAccessModal
            v-if="canManageAccess && accessModel"
            v-model="accessModalOpen"
            :model="accessModel"
            :organization-id="organization.id"
            @updated="getModels"
        />


    </div>
</template>

<script setup lang="ts">
const props = defineProps({
    organization: {
        type: Object,
        required: true,
    },
});

const { t } = useI18n();
const toast = useToast();
const searchQuery = ref('');

// Which row's Actions menu is open. The menu is rendered inline inside the
// sticky (z-10) Actions cell, so an open menu is trapped in that cell's
// stacking context and gets painted over by the sibling rows' sticky cells
// below it (equal z-index, later in DOM order) — the "ghost text" overlay.
// Lifting only the open row's cell above its siblings while the menu is open
// lets the menu paint cleanly over the table.
const openMenuModelId = ref<string | null>(null);

type Provider = { id: string; name: string; provider_type: string };
type Model = {
  id: string;
  name: string;
  model_id: string;
  is_default: boolean;
  is_small_default: boolean;
  is_enabled: boolean;
  supports_vision: boolean;
  supports_vision_override?: boolean | null;
  supports_image_generation: boolean;
  supports_image_generation_override?: boolean | null;
  context_window_tokens?: number | null;
  context_window_tokens_override?: number | null;
  is_restricted?: boolean;
  input_cost_per_million_tokens_usd?: number | null;
  output_cost_per_million_tokens_usd?: number | null;
  config?: Record<string, any> | null;
  provider: Provider;
};

const models = ref<Model[]>([]);
const providers = ref<Provider[]>([]);

// ── Auto model router (org setting model_routing) ──────────────────────────
const autoRouterOn = ref(false);

const loadAutoRouter = async () => {
    const response = await useMyFetch<any>('/organization/settings', { method: 'GET' });
    const cfg = (response.data.value as any)?.config;
    autoRouterOn.value = !!cfg?.model_routing?.value;
};

const saveAutoRouter = async (val: boolean) => {
    const response = await useMyFetch('/organization/settings', {
        method: 'PUT',
        body: { config: { model_routing: { value: val } } },
    });
    if (response.status.value === 'success') {
        // On first enable, seed starter routing guidance on the default + small
        // models so the router has candidates to route between out of the box.
        // Only fills EMPTY hints — never overwrites an admin's own guidance.
        if (val) await seedDefaultRoutingHints();
        toast.add({
            title: val ? t('settings.llms.autoRouterOn') : t('settings.llms.autoRouterOff'),
            color: 'green',
        });
    } else {
        autoRouterOn.value = !val; // revert optimistic toggle
        toast.add({ title: 'Error', description: 'Could not update auto router', color: 'red' });
    }
};

const seedDefaultRoutingHints = async () => {
    const seeds: { find: (m: Model) => boolean; hint: string }[] = [
        { find: (m) => !!m.is_small_default, hint: t('settings.llms.routingHintSeedSmall') },
        { find: (m) => !!m.is_default, hint: t('settings.llms.routingHintSeedDefault') },
    ];
    let changed = false;
    for (const s of seeds) {
        const m = models.value.find((x) => s.find(x) && x.is_enabled);
        if (m && !modelHint(m)) {
            const r = await useMyFetch(`/llm/models/${m.id}/routing_hint`, {
                method: 'POST',
                body: { hint: s.hint },
            });
            if (r.status.value === 'success') changed = true;
        }
    }
    if (changed) await getModels();
};

const modelHint = (model: Model): string => (model.config?.routing_hint as string) || '';

// ── LLM fallback (org setting llm_fallback + /llm/fallback_order) ──────────
type FallbackEntry = {
    id: string;
    name: string;
    model_id: string;
    provider_type: string;
    provider_name: string;
    is_active: boolean;
};

const fallbackOn = ref(false);
const fallbackOrder = ref<FallbackEntry[]>([]);

const loadFallback = async () => {
    const response = await useMyFetch<any>('/llm/fallback_order', { method: 'GET' });
    const data = response.data.value as any;
    if (data) {
        fallbackOn.value = !!data.enabled;
        fallbackOrder.value = (data.order as FallbackEntry[]) || [];
    }
};

const saveFallbackToggle = async (val: boolean) => {
    const response = await useMyFetch('/organization/settings', {
        method: 'PUT',
        body: { config: { llm_fallback: { value: val } } },
    });
    if (response.status.value === 'success') {
        toast.add({
            title: val ? t('settings.llms.fallbackOn') : t('settings.llms.fallbackOff'),
            color: 'green',
        });
    } else {
        fallbackOn.value = !val; // revert optimistic toggle
        toast.add({ title: 'Error', description: 'Could not update LLM fallback', color: 'red' });
    }
};

const saveFallbackOrder = async () => {
    const response = await useMyFetch('/llm/fallback_order', {
        method: 'POST',
        body: { model_ids: fallbackOrder.value.map((e) => e.id) },
    });
    if (response.status.value === 'success') {
        const data = response.data.value as any;
        fallbackOrder.value = (data?.order as FallbackEntry[]) || fallbackOrder.value;
    } else {
        toast.add({ title: 'Error', description: 'Could not save fallback order', color: 'red' });
        await loadFallback(); // resync with server state
    }
};

// The default model is implicit position 0 — it's what fails INTO the chain.
const defaultModel = computed<Model | undefined>(() => models.value.find((m) => m.is_default));

// 1-based position of a model in the fallback order, or null when not in it.
const fallbackPriority = (model: Model): number | null => {
    const idx = fallbackOrder.value.findIndex((e) => e.id === model.id);
    return idx === -1 ? null : idx + 1;
};

// Positions this model may take: every current slot, plus one at the end when
// it isn't in the chain yet.
const fallbackPriorityOptions = (model: Model): number[] => {
    const n = fallbackOrder.value.length + (fallbackPriority(model) === null ? 1 : 0);
    return Array.from({ length: n }, (_, i) => i + 1);
};

const setFallbackPriority = async (model: Model, raw: string) => {
    const next = fallbackOrder.value.filter((e) => e.id !== model.id);
    const p = raw === '' ? null : parseInt(raw, 10);
    if (p !== null && !Number.isNaN(p)) {
        next.splice(Math.min(Math.max(p - 1, 0), next.length), 0, {
            id: model.id,
            name: model.name,
            model_id: model.model_id,
            provider_type: model.provider.provider_type,
            provider_name: model.provider.name,
            is_active: true,
        });
    }
    fallbackOrder.value = next;
    await saveFallbackOrder();
};

const formatCostPart = (n?: number | null): string =>
    (n == null ? '—' : `$${parseFloat(Number(n).toFixed(2))}`);

const editingHintId = ref<string | null>(null);
const hintDraft = ref<string>('');

const startHintEdit = (model: Model) => {
    hintDraft.value = modelHint(model);
    editingHintId.value = model.id;
};

const saveHint = async (model: Model) => {
    const response = await useMyFetch(`/llm/models/${model.id}/routing_hint`, {
        method: 'POST',
        body: { hint: hintDraft.value },
    });
    if (response.status.value === 'success') {
        editingHintId.value = null;
        await getModels();
        toast.add({ title: 'Routing guidance updated', color: 'green' });
    } else {
        toast.add({ title: 'Error', description: 'Could not update routing guidance', color: 'red' });
    }
};

const providerModalOpen = ref(false);
const editProviderId = ref<string | null>(null);
const startWithNewProvider = ref(false);
const addModelModalOpen = ref(false);

const { hasFeature } = useEnterprise();
const canManageAccess = computed(() => hasFeature('llm_access_control') && useCan('manage_llm_settings'));
// Auto model router is an Enterprise feature — the toggle stays visible but
// locked (and the backend rejects enabling it) without the license.
const modelRoutingLicensed = computed(() => hasFeature('model_routing'));
// LLM fallback is likewise Enterprise: visible but locked without a license.
const llmFallbackLicensed = computed(() => hasFeature('llm_fallback'));

const accessModalOpen = ref(false);
const accessModel = ref<Model | null>(null);

const accessIsRestricted = (model: Model) => !!model.is_restricted && !model.is_default && !model.is_small_default;
const accessLabel = (model: Model) => {
    if (model.is_default || model.is_small_default) return 'Everyone (default)';
    return model.is_restricted ? 'Restricted' : 'Everyone';
};
const openAccess = (model: Model) => {
    accessModel.value = model;
    accessModalOpen.value = true;
};

// ── Per-model settings card ────────────────────────────────────────────────
// Track the id (not the object) so the card always shows the freshest row
// after getModels() refreshes.
const cardModelId = ref<string | null>(null);
const cardModalOpen = ref(false);
const cardModel = computed<Model | null>(() => models.value.find((m) => m.id === cardModelId.value) || null);

const openModelCard = (model: Model) => {
    if (!useCan('manage_llm_settings')) return;
    cardModelId.value = model.id;
    cardModalOpen.value = true;
};

// Provider chip filter — click a chip to see only that provider's models.
const providerFilterId = ref<string | null>(null);
const toggleProviderFilter = (providerId: string) => {
    providerFilterId.value = providerFilterId.value === providerId ? null : providerId;
};

const filteredModels = computed<Model[]>(() => {
    let list = models.value;
    if (providerFilterId.value) {
        list = list.filter(model => model.provider.id === providerFilterId.value);
    }
    const query = searchQuery.value.toLowerCase();
    if (!query) return list;

    return list.filter(model => {
        return model.name.toLowerCase().includes(query) ||
               model.provider.name.toLowerCase().includes(query);
    });
});

const getModels = async () => {
  const response = await useMyFetch<Model[]>('/llm/models', {
      method: 'GET',
  });

  models.value = (response.data.value as unknown as Model[]) || [];
}

const getProviders = async () => {
    const response = await useMyFetch<Provider[]>('/llm/providers', {
        method: 'GET',
    });

    providers.value = (response.data.value as unknown as Provider[]) || [];
}

onMounted(async () => {
    await getModels();
    await getProviders();
    await loadAutoRouter();
    await loadFallback();
});

const handleProviderModalClose = async (value: boolean) => {
    providerModalOpen.value = value;
    if (!value) {  // Modal is closing
        await getModels();
        await getProviders();
        editProviderId.value = null;
        startWithNewProvider.value = false;
    }
};

const openAddProvider = () => {
    editProviderId.value = null;
    startWithNewProvider.value = true;
    providerModalOpen.value = true;
};

const updateErrorDetail = (response: any): string => {
    const errAny = (response.error as any);
    const err = (errAny && (errAny.value || errAny)) || {};
    return String(err?.data?.detail || err?.data?.message || err?.message || 'Could not update model');
};

const setDefaultModel = async (modelId: string, small = false) => {
    const response = await useMyFetch(`/llm/models/${modelId}/set_default`, {
        method: 'POST',
        query: { small }
    });
    if (response.status.value === 'success') {
        await getModels();
        toast.add({
            title: 'Model updated',
            description: 'Model has been updated successfully',
            color: 'green'
        });
    }
    else {
        toast.add({
            title: 'Error',
            description: updateErrorDetail(response),
            color: 'red'
        });
    }
};

const toggleModel = async (modelId: string, enabled: boolean) => {
    const response = await useMyFetch(`/llm/models/${modelId}/toggle`, {
        method: 'POST',
        query: { enabled }
    });
    if (response.status.value === 'success') {
        await getModels();
        toast.add({
            title: 'Model updated',
            description: 'Model has been updated successfully',
            color: 'green'
        });
    }
    else {
        // Refresh so the toggle reflects the server's actual state instead of
        // staying flipped on a rejected change.
        await getModels();
        toast.add({
            title: 'Error',
            description: updateErrorDetail(response),
            color: 'red'
        });
    }
};

const openManageProvider = (providerId: string) => {
    startWithNewProvider.value = false;
    editProviderId.value = providerId;
    providerModalOpen.value = true;
};

// ── Delete model (Actions menu → confirmation) ─────────────────────────────
const deleteTarget = ref<Model | null>(null);
const isDeletingModel = ref(false);
const deleteConfirmOpen = computed({
    get: () => !!deleteTarget.value,
    set: (value: boolean) => { if (!value) deleteTarget.value = null; },
});

const confirmDeleteModel = async () => {
    if (!deleteTarget.value) return;
    isDeletingModel.value = true;
    try {
        const response = await useMyFetch(`/llm/models/${deleteTarget.value.id}`, { method: 'DELETE' });
        if (response.status.value === 'success') {
            toast.add({ title: t('settings.llms.modelDeleted'), color: 'green' });
            deleteTarget.value = null;
            await getModels();
        } else {
            const errAny = (response.error as any);
            const err = (errAny && (errAny.value || errAny)) || {};
            const detail = err?.data?.detail || err?.data?.message || err?.message || 'Could not delete model';
            toast.add({ title: 'Error', description: String(detail), color: 'red' });
        }
    } finally {
        isDeletingModel.value = false;
    }
};

const buildDropdownItems = (model: Model) => {
    const items: any[][] = [[
        {
            label: t('settings.llms.modelSettings'),
            icon: 'i-heroicons-adjustments-horizontal',
            click: () => {
                openModelCard(model);
            }
        },
        {
            label: t('settings.llms.makeDefault'),
            click: () => {
                setDefaultModel(model.id, false);
            }
        },
        {
            label: t('settings.llms.makeSmallDefault'),
            click: () => {
                setDefaultModel(model.id, true);
            }
        },
        {
            label: t('settings.llms.manageProvider'),
            click: () => {
                openManageProvider(model.provider.id);
            }
        }
    ]];
    items.push([
        {
            label: t('settings.llms.deleteModel'),
            icon: 'i-heroicons-trash',
            danger: true,
            disabled: model.is_default || model.is_small_default,
            click: () => {
                deleteTarget.value = model;
            }
        }
    ]);
    return items;
};

// Memoize dropdown items per model. Building them inline in the template
// (`:items="getDropdownItems(model)"`) returns a fresh array on every render —
// so each row hover (which toggles the hover background) re-created the items
// and thrashed the dropdown popper, making the Actions menu feel frozen.
const dropdownItemsByModel = computed<Record<string, any[][]>>(() => {
    const map: Record<string, any[][]> = {};
    for (const m of models.value) map[m.id] = buildDropdownItems(m);
    return map;
});
</script>
