<template>
    <!-- "Start a separate draft" (collab01): opt-in, e.g. a hotfix next to a
         redesign. Creates the draft with a conversation of the caller's on it
         and opens that conversation. -->
    <UModal v-model="open" :ui="{ width: 'sm:max-w-md' }">
        <form class="p-5 space-y-4" data-testid="new-draft-dialog" @submit.prevent="create">
            <div class="space-y-1">
                <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('dashboardCollab.page.newDraftTitle') }}</h3>
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('dashboardCollab.page.newDraftBody') }}</p>
            </div>
            <UFormGroup :label="$t('dashboardCollab.page.draftName')" :error="error || undefined">
                <UInput
                    v-model="name"
                    autofocus
                    maxlength="120"
                    data-testid="new-draft-name"
                    :placeholder="$t('dashboardCollab.page.draftNamePlaceholder')"
                />
            </UFormGroup>
            <UCheckbox
                v-if="fromVersionId && fromVersionNumber"
                v-model="fromThisVersion"
                data-testid="new-draft-from-version"
                :label="$t('dashboardCollab.page.fromThisVersion', { n: fromVersionNumber })"
            />
            <div class="flex justify-end gap-2 pt-1">
                <UButton color="gray" variant="ghost" size="xs" @click="open = false">{{ $t('dashboardCollab.page.cancel') }}</UButton>
                <UButton type="submit" size="xs" :loading="creating" :disabled="!name.trim()" data-testid="new-draft-create">
                    {{ $t('dashboardCollab.page.createDraft') }}
                </UButton>
            </div>
        </form>
    </UModal>
</template>

<script setup lang="ts">
// fromVersionId/Number: the version previewed (?version=N), offered as the
// draft's starting point; otherwise the draft starts from the newest.
const props = defineProps<{
    modelValue: boolean;
    dashboardId: string;
    fromVersionId?: string | null;
    fromVersionNumber?: number | null;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>();

const { getErrorMessage } = useErrorMessage();

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
});
const name = ref('');
const fromThisVersion = ref(true);
const creating = ref(false);
const error = ref<string | null>(null);

watch(open, (value) => {
    if (value) { name.value = ''; error.value = null; fromThisVersion.value = true; }
});

async function create() {
    const trimmed = name.value.trim();
    if (!trimmed || creating.value) return;
    creating.value = true;
    error.value = null;
    try {
        const body: Record<string, string> = { name: trimmed };
        if (props.fromVersionId && fromThisVersion.value) body.from_version_id = props.fromVersionId;
        const { data, error: err } = await useMyFetch(`/api/dashboards/${encodeURIComponent(props.dashboardId)}/drafts`, {
            method: 'POST',
            body,
        });
        if (err.value) {
            error.value = getErrorMessage(err.value);
            return;
        }
        const reportId = (data.value as any)?.report_id;
        if (reportId) {
            open.value = false;
            await navigateTo(`/reports/${reportId}?dashboard=${encodeURIComponent(props.dashboardId)}`);
        }
    } finally {
        creating.value = false;
    }
}
</script>
