<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-5" data-testid="llm-access-modal">
            <div class="flex justify-between items-center mb-4">
                <div>
                    <h3 class="text-base font-semibold text-gray-900 dark:text-white">Manage model access</h3>
                    <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ model?.name }}</p>
                </div>
                <button @click="close" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200">
                    <UIcon name="i-heroicons-x-mark" class="w-5 h-5" />
                </button>
            </div>

            <!-- Restricted toggle -->
            <div class="flex items-start justify-between gap-4 p-3 rounded-md bg-gray-50 dark:bg-gray-800 mb-4">
                <div>
                    <div class="text-sm font-medium text-gray-900 dark:text-white">Restricted access</div>
                    <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                        <template v-if="isDefaultModel">Default models are available to all members and cannot be restricted.</template>
                        <template v-else-if="access.is_restricted">Only the users, groups and roles below can use this model.</template>
                        <template v-else>This model is available to all members.</template>
                    </div>
                </div>
                <UToggle
                    data-testid="llm-access-restricted-toggle"
                    v-model="access.is_restricted"
                    :disabled="isDefaultModel || savingRestricted"
                    @update:model-value="onToggleRestricted"
                />
            </div>

            <div v-if="access.is_restricted && !isDefaultModel">
                <!-- Members list -->
                <div class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Who can use this model</div>
                <div v-if="access.members.length === 0" class="text-sm text-gray-400 dark:text-gray-500 py-3 text-center border border-dashed border-gray-200 dark:border-gray-700 rounded-md mb-3">
                    No one has been granted access yet.
                </div>
                <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800 mb-3" data-testid="llm-access-member-list">
                    <li v-for="m in access.members" :key="m.grant_id" class="flex items-center justify-between py-2">
                        <div class="flex items-center gap-2">
                            <UIcon :name="principalIcon(m.principal_type)" class="w-4 h-4 text-gray-400" />
                            <span class="text-sm text-gray-800 dark:text-gray-200">{{ m.principal_name || m.principal_id }}</span>
                            <span class="text-[10px] uppercase text-gray-400 border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5">{{ m.principal_type }}</span>
                        </div>
                        <button @click="removeMember(m)" class="text-gray-400 hover:text-red-500" :title="'Remove'">
                            <UIcon name="i-heroicons-trash" class="w-4 h-4" />
                        </button>
                    </li>
                </ul>

                <!-- Add principal -->
                <div class="border-t border-gray-100 dark:border-gray-800 pt-3">
                    <div class="flex items-center gap-2">
                        <USelectMenu
                            v-model="addType"
                            :options="principalTypeOptions"
                            value-attribute="value"
                            option-attribute="label"
                            class="w-28"
                            data-testid="llm-access-add-type"
                        />
                        <USelectMenu
                            v-model="addPrincipalId"
                            :options="candidateOptions"
                            value-attribute="value"
                            option-attribute="label"
                            searchable
                            placeholder="Select…"
                            class="flex-1"
                            data-testid="llm-access-add-principal"
                        />
                        <UButton
                            size="sm"
                            :disabled="!addPrincipalId || adding"
                            :loading="adding"
                            @click="addMember"
                            data-testid="llm-access-add-btn"
                        >Add</UButton>
                    </div>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
const props = defineProps<{
    modelValue: boolean;
    model: any;
    organizationId: string;
}>();
const emit = defineEmits(['update:modelValue', 'updated']);

const toast = useToast();

const isOpen = computed({
    get: () => props.modelValue,
    set: (v: boolean) => emit('update:modelValue', v),
});

type Member = { grant_id: string; principal_type: string; principal_id: string; principal_name?: string };
const access = ref<{ is_restricted: boolean; is_default: boolean; is_small_default: boolean; members: Member[] }>({
    is_restricted: false, is_default: false, is_small_default: false, members: [],
});

const isDefaultModel = computed(() => !!props.model?.is_default || !!props.model?.is_small_default || access.value.is_default || access.value.is_small_default);

const savingRestricted = ref(false);
const adding = ref(false);

const addType = ref<'user' | 'group' | 'role'>('user');
const addPrincipalId = ref<string | null>(null);

const principalTypeOptions = [
    { value: 'user', label: 'User' },
    { value: 'group', label: 'Group' },
    { value: 'role', label: 'Role' },
];

const users = ref<any[]>([]);
const groups = ref<any[]>([]);
const roles = ref<any[]>([]);

const candidateOptions = computed(() => {
    const taken = new Set(access.value.members.filter(m => m.principal_type === addType.value).map(m => m.principal_id));
    if (addType.value === 'user') return users.value.filter(u => !taken.has(u.id)).map(u => ({ value: u.id, label: u.name || u.email }));
    if (addType.value === 'group') return groups.value.filter(g => !taken.has(g.id)).map(g => ({ value: g.id, label: g.name }));
    return roles.value.filter(r => !taken.has(r.id)).map(r => ({ value: r.id, label: r.name }));
});

watch(addType, () => { addPrincipalId.value = null; });

function principalIcon(t: string) {
    if (t === 'group') return 'i-heroicons-user-group';
    if (t === 'role') return 'i-heroicons-shield-check';
    return 'i-heroicons-user';
}

async function loadAccess() {
    const { data } = await useMyFetch(`/llm/models/${props.model.id}/access`, { method: 'GET' });
    if (data.value) access.value = data.value as any;
}

async function loadCandidates() {
    const [m, g, r] = await Promise.all([
        useMyFetch('/organization/members', { method: 'GET' }),
        useMyFetch(`/organizations/${props.organizationId}/groups`, { method: 'GET' }),
        useMyFetch(`/organizations/${props.organizationId}/roles`, { method: 'GET' }),
    ]);
    users.value = (m.data.value as any[]) || [];
    groups.value = (g.data.value as any[]) || [];
    roles.value = (r.data.value as any[]) || [];
}

watch(isOpen, async (open) => {
    if (open && props.model) {
        addPrincipalId.value = null;
        addType.value = 'user';
        await Promise.all([loadAccess(), loadCandidates()]);
    }
});

async function onToggleRestricted(val: boolean) {
    savingRestricted.value = true;
    const { error } = await useMyFetch(`/llm/models/${props.model.id}/restricted`, {
        method: 'PUT', body: { is_restricted: val },
    });
    savingRestricted.value = false;
    if (error.value) {
        access.value.is_restricted = !val; // revert
        toast.add({ title: 'Error', description: 'Could not update restriction', color: 'red' });
        return;
    }
    await loadAccess();
    emit('updated');
    toast.add({ title: val ? 'Model restricted' : 'Model opened to everyone', color: 'green' });
}

async function addMember() {
    if (!addPrincipalId.value) return;
    adding.value = true;
    const { error } = await useMyFetch(`/llm/models/${props.model.id}/access`, {
        method: 'POST', body: { principal_type: addType.value, principal_id: addPrincipalId.value },
    });
    adding.value = false;
    if (error.value) {
        toast.add({ title: 'Error', description: 'Could not add member', color: 'red' });
        return;
    }
    addPrincipalId.value = null;
    await loadAccess();
    emit('updated');
}

async function removeMember(m: Member) {
    const { error } = await useMyFetch(`/llm/models/${props.model.id}/access/${m.grant_id}`, { method: 'DELETE' });
    if (error.value) {
        toast.add({ title: 'Error', description: 'Could not remove member', color: 'red' });
        return;
    }
    await loadAccess();
    emit('updated');
}

function close() { isOpen.value = false; }
</script>
