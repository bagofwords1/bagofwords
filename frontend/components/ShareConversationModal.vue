<template>
    <UTooltip text="Share Conversation">
        <button @click="openModal" class="p-1.5 rounded text-xl hover:bg-gray-100 flex items-center">
            <Icon name="heroicons:arrow-up-tray" class="inline-block mr-2" />
            <span class="text-sm">Share</span>
        </button>
    </UTooltip>

    <UModal v-model="modalOpen">
        <div class="p-4 relative">
            <button @click="modalOpen = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Share Conversation</h1>
            <p class="text-sm text-gray-500">Share this conversation with others</p>
            <hr class="my-4" />
            <div class="flex flex-row items-center text-sm">
                Enable conversation sharing
                <UToggle color="sky" :model-value="isShared" class="ml-2" @update:model-value="toggleShare" :loading="isLoading" />
            </div>
            <div class="flex flex-col mt-4 text-sm" v-if="isShared && shareToken">
                <div class="my-2 font-semibold">Share URL</div>
                <div class="flex">
                    <input :value="shareUrl" type="text" class="py-2 px-2 border border-gray-200 rounded-md w-[95%]"
                        disabled />
                    <button @click="copyShareUrl"
                        class="ml-2 bg-gray-50 border border-gray-200 rounded-md px-3 text-xs hover:bg-gray-100 relative">
                        Copy
                        <span v-if="showTooltip"
                            class="absolute top-full left-1/2 transform -translate-x-1/2 mt-1 bg-black text-white text-xs rounded py-1 px-2">
                            Copied!
                        </span>
                    </button>
                </div>
                <div class="mt-4 font-normal">
                    <a :href="shareUrl" target="_blank" class="text-blue-500 hover:underline">
                        <Icon name="heroicons:arrow-top-right-on-square" />
                        View shared conversation</a>
                </div>
            </div>
            <div class="border-t border-gray-200 pt-4 mt-8">
                <button @click="modalOpen = false"
                    class="bg-gray-50 border border-gray-200 rounded-md px-3 py-2 text-xs hover:bg-gray-100">Close</button>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue'

const modalOpen = ref(false);
const toast = useToast();
const isLoading = ref(false);
const showTooltip = ref(false);

const props = defineProps<{
    report: any
}>();

// Local state for sharing status
const isShared = ref(false);
const shareToken = ref<string | null>(null);

/**
 * Note: in some screens, `report` is a partial object and may not include
 * `conversation_share_enabled/token`. So props are a hint, but backend is source of truth.
 */
const syncFromProps = () => {
    const r = props.report;
    if (!r) return;
    if (typeof r.conversation_share_enabled === 'boolean') {
        isShared.value = r.conversation_share_enabled;
    }
    if (r.conversation_share_token !== undefined) {
        shareToken.value = r.conversation_share_token ?? null;
    }
};

const fetchShareStatus = async () => {
    if (!props.report?.id) return;
    const res = await useMyFetch(`/reports/${props.report.id}`, { method: 'GET' });
    if (res.error.value) throw res.error.value;
    const data = res.data.value as any;
    const enabled = !!data?.conversation_share_enabled;
    const token = enabled ? (data?.conversation_share_token ?? null) : null;

    isShared.value = enabled;
    shareToken.value = token;

    // Best-effort: keep parent object in sync too
    if (props.report) {
        props.report.conversation_share_enabled = enabled;
        props.report.conversation_share_token = token;
    }
};

// Sync when modal opens to ensure fresh state
const openModal = async () => {
    modalOpen.value = true;
    syncFromProps();
    try {
        await fetchShareStatus();
    } catch {
        // If status fetch fails, keep best-effort state from props.
    }
};

// If the report instance changes, re-sync.
watch(() => props.report?.id, () => {
    syncFromProps();
});

const shareUrl = computed(() => {
    if (!shareToken.value) return '';
    return `${window.location.origin}/c/${shareToken.value}`;
});

const toggleShare = async (newValue: boolean) => {
    isLoading.value = true;
    try {
        // Ensure we have the real current state before toggling (endpoint is a toggle, not a set).
        await fetchShareStatus();

        // If UI is already in the desired state, do nothing.
        if (newValue === isShared.value) return;

        const response = await useMyFetch(`/reports/${props.report.id}/conversation-share`, { method: 'POST' });
        
        if (response.error.value) {
            throw new Error('Failed to toggle sharing');
        }
        
        const data = response.data.value as { enabled: boolean; token: string | null };
        isShared.value = data.enabled;
        shareToken.value = data.token;
        
        // Update the parent report object
        if (props.report) {
            props.report.conversation_share_enabled = data.enabled;
            props.report.conversation_share_token = data.token;
        }
        
        toast.add({
            title: data.enabled ? 'Conversation sharing enabled' : 'Conversation sharing disabled',
            description: data.enabled ? 'Anyone with the link can view this conversation' : 'The share link is now inactive',
            color: 'green',
        });
    } catch (error) {
        toast.add({
            title: 'Error',
            description: 'Failed to toggle conversation sharing',
            color: 'red',
        });
    } finally {
        isLoading.value = false;
    }
};

const copyShareUrl = async () => {
    try {
        await navigator.clipboard.writeText(shareUrl.value);
        showTooltip.value = true;
        setTimeout(() => {
            showTooltip.value = false;
        }, 2000);
    } catch {
        // Fallback for browsers that don't support clipboard API
        const textArea = document.createElement('textarea');
        textArea.value = shareUrl.value;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        textArea.style.top = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            document.execCommand('copy');
            showTooltip.value = true;
            setTimeout(() => {
                showTooltip.value = false;
            }, 2000);
        } catch {
            toast.add({
                title: 'Error',
                description: 'Failed to copy to clipboard',
                color: 'red',
            });
        }
        document.body.removeChild(textArea);
    }
};
</script>

