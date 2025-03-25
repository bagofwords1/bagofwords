<template>
    <UModal v-model="gitModalOpen">
        <div class="p-4 relative">
            <button @click="gitModalOpen = false" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700">
                <UIcon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Connect Git Repository</h1>
            <p class="text-sm text-gray-500">Configure Git repository access for data models</p>
            <hr class="my-4" />

            <!-- Connected Repository Info -->
            <div v-if="connectedRepo" class="space-y-4">
                <div class="bg-gray-50 rounded-lg p-4 border border-gray-200">
                    <div class="space-y-3">
                        <!-- Repository Details -->
                        <div class="flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UIcon :name="getProviderIcon(connectedRepo.provider)" class="w-5 h-5" />
                                <span class="text-sm font-medium text-gray-700">{{ connectedRepo.repo_url }}</span>
                            </div>
                            <UButton
                                icon="heroicons:trash"
                                color="red"
                                variant="ghost"
                                size="xs"
                                @click="confirmDelete"
                            />
                        </div>

                        <!-- Additional Repository Info -->
                        <div class="grid grid-cols-2 gap-4 text-sm">
                            <div>
                                <p class="text-gray-500">Branch</p>
                                <p class="font-medium">{{ connectedRepo.branch }}</p>
                            </div>
                            <div v-if="connectedRepo.custom_host">
                                <p class="text-gray-500">Custom Host</p>
                                <p class="font-medium">{{ connectedRepo.custom_host }}</p>
                            </div>
                            <div v-if="metadata_resources.completed_at">
                                <p class="text-gray-500">Last Indexed</p>
                                <p class="font-medium">{{ new Date(metadata_resources.completed_at).toLocaleString() }}</p>
                            </div>
                            <div v-if="connectedRepo.last_commit">
                                <p class="text-gray-500">Last Commit</p>
                                <p class="font-medium">{{ connectedRepo.last_commit }}</p>
                            </div>
                            <div v-if="metadata_resources.status">
                                <p class="text-gray-500">Status</p>
                                <p class="font-medium">{{ metadata_resources.status }}</p>
                            </div>
                            <div v-if="metadata_resources.error_message">
                                <p class="text-gray-500">Error</p>
                                <p class="font-medium">{{ metadata_resources.error_message }}</p>
                            </div>
                            <div v-if="connectedRepo.is_active">
                                <p class="text-gray-500">Active</p>
                                <p class="font-medium">{{ connectedRepo.is_active ? 'Yes' : 'No' }}</p>
                            </div>
                        </div>
                        
                        <!-- Reindex Button -->
                        <div class="mt-4 flex">
                            <UButton
                                @click="reindexRepository"
                                :loading="isReindexing"
                                :disabled="isReindexing"
                                icon="heroicons:arrow-path"
                                color="blue"
                                class="bg-gray-50 text-sm text-blue-500  hover:bg-gray-100 border border-gray-300 p-2 text-xs rounded-lg flex items-center gap-2"
                                size="sm"
                            >
                                Reindex Repository
                            </UButton>
                        </div>
                    </div>
                </div>
            </div>

            <!-- New Repository Form -->
            <form v-else @submit.prevent="handleSubmit" class="space-y-4">
                <!-- Git Provider Selection -->
                <div class="space-y-2">
                    <label class="text-sm font-medium text-gray-700">Git Provider</label>
                    <div class="flex flex-row gap-3 mt-2">
                        <button 
                            v-for="provider in gitProviders" 
                            :key="provider.type"
                            @click="selectProvider(provider)" 
                            type="button"
                            class="bg-gray-50 text-sm hover:bg-gray-100 border border-gray-300 p-3 rounded-lg flex items-center gap-2"
                            :class="{ '!border-blue-500 border-2 bg-white': selectedProvider === provider.type }"
                        >
                            <UIcon :name="provider.icon" class="w-5 h-5" />
                            <span>{{ provider.name }}</span>
                        </button>
                    </div>
                </div>

                <div v-if="selectedProvider" class="space-y-4">
                    <!-- Custom Git Host (only for custom provider) -->
                    <div v-if="selectedProvider === 'custom'" class="space-y-2">
                        <label class="text-sm font-medium text-gray-700">Custom Git Host</label>
                        <input 
                            v-model="formData.customHost"
                            type="text"
                            placeholder="git.customdomain.com"
                            class="border border-gray-300 rounded-lg px-4 py-2 w-full text-sm focus:outline-none focus:border-blue-500"
                        />
                    </div>

                    <!-- Repository URL and Branch -->
                    <div class="space-y-2">
                        <label class="text-sm font-medium text-gray-700">Repository URL</label>
                        <div class="flex gap-2">
                            <input 
                                v-model="formData.repoUrl"
                                type="text"
                                placeholder="git@github.com:user/repo.git"
                                class="border border-gray-300 rounded-lg px-4 py-2 w-3/4 text-sm focus:outline-none focus:border-blue-500"
                                required
                            />
                            <div class="w-1/4">
                                <input 
                                    v-model="formData.branch"
                                    type="text"
                                    placeholder="Branch (default: main)"
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full text-sm focus:outline-none focus:border-blue-500"
                                />
                            </div>
                        </div>
                    </div>

                    <!-- SSH Private Key Input -->
                    <div v-if="!connectedRepo" class="space-y-2">
                        <label class="text-sm font-medium text-gray-700">SSH Private Key</label>
                        <div class="relative">
                            <UTextarea
                                v-model="formData.privateKey"
                                rows="6"
                                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----"
                                class="w-full text-sm font-mono bg-gray-50"
                            />
                        </div>
                        <p class="text-xs text-gray-500">
                            Paste your SSH private key here (usually found in ~/.ssh/id_rsa)
                        </p>
                    </div>

                    <!-- Test Connection -->
                    <div class="pt-4">
                        <button
                            type="button"
                            @click="testConnection"
                            class="bg-white border border-gray-300 rounded-lg px-4 py-2 text-sm hover:bg-gray-50 mr-2"
                            :disabled="isLoading"
                        >
                            <UIcon v-if="isLoading" name="heroicons:arrow-path" class="w-4 h-4 animate-spin inline mr-1" />
                            Test Connection
                        </button>
                        <span v-if="connectionStatus" :class="connectionStatus.success ? 'text-green-600' : 'text-red-600'" class="text-sm">
                            {{ connectionStatus.message }}
                        </span>
                    </div>
                </div>

                <!-- Action Buttons -->
                <div class="flex justify-end space-x-2 pt-4">
                    <UButton label="Cancel" color="gray" variant="soft" @click="gitModalOpen = false" />
                    <UButton
                        type="submit"
                        label="Save Integration"
                        class="!bg-blue-500 !text-white"
                        :disabled="!canSave || isLoading"
                    />
                </div>
            </form>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';

const props = defineProps<{
    modelValue: boolean;
    datasourceId: string;
    gitRepository?: {
        id: string;
        provider: string;
        repo_url: string;
        branch: string;
        custom_host?: string;
        last_indexed?: string;
        last_commit?: string;
    },
    metadataResources?: any;
}>();

const emit = defineEmits(['update:modelValue']);

const gitModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
});

const toast = useToast();
const isLoading = ref(false);
const connectionStatus = ref(null);
const isReindexing = ref(false);

const gitProviders = [
    { type: 'github', name: 'GitHub', icon: 'logos:github-icon' },
    { type: 'gitlab', name: 'GitLab', icon: 'logos:gitlab' },
    { type: 'bitbucket', name: 'Bitbucket', icon: 'logos:bitbucket' },
    { type: 'custom', name: 'Custom', icon: 'heroicons:server' },
];

const selectedProvider = ref(null);
const formData = ref({
    customHost: '',
    repoUrl: '',
    branch: 'main',
    privateKey: '',
    publicKey: '',
});

const canSave = computed(() => {
    return selectedProvider.value && 
           formData.value.repoUrl && 
           (formData.value.privateKey || (connectionStatus.value && connectionStatus.value.success));
});

function selectProvider(provider) {
    selectedProvider.value = provider.type;
}

async function copyPublicKey() {
    try {
        await navigator.clipboard.writeText(formData.value.publicKey);
        toast.add({
            title: 'Success',
            description: 'Public key copied to clipboard',
            color: 'green'
        });
    } catch (error) {
        toast.add({
            title: 'Error',
            description: 'Failed to copy public key',
            color: 'red'
        });
    }
}

async function testConnection() {
    isLoading.value = true;
    connectionStatus.value = null;

    try {
        const response = await useMyFetch(`/data_sources/${props.datasourceId}/git_repository/test`, {
            method: 'POST',
            body: {
                provider: selectedProvider.value,
                custom_host: formData.value.customHost,
                repo_url: formData.value.repoUrl,
                branch: formData.value.branch,
                ssh_key: formData.value.privateKey,
            }
        });

        connectionStatus.value = {
            success: response.data.value?.success,
            message: response.data.value?.message || 'Connection successful'
        };
    } catch (error) {
        connectionStatus.value = {
            success: false,
            message: 'Failed to connect to repository'
        };
    } finally {
        isLoading.value = false;
    }
}

async function handleSubmit() {
    if (!canSave.value) return;

    isLoading.value = true;
    try {
        const response = await useMyFetch(`/data_sources/${props.datasourceId}/git_repository`, {
            method: 'POST',
            body: {
                provider: selectedProvider.value,
                custom_host: formData.value.customHost,
                repo_url: formData.value.repoUrl,
                branch: formData.value.branch,
                ssh_key: formData.value.privateKey,
            }
        });

        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: 'Git repository connected successfully',
                color: 'green'
            });
            gitModalOpen.value = false;
        }
    } catch (error) {
        toast.add({
            title: 'Error',
            description: 'Failed to save Git integration',
            color: 'red'
        });
    } finally {
        isLoading.value = false;
    }
}

// Update the connectedRepo ref to use the prop
const connectedRepo = computed(() => props.gitRepository);
const metadata_resources = computed(() => props.metadataResources || {});

// Helper function to get provider icon
function getProviderIcon(provider: string) {
    const found = gitProviders.find(p => p.type === provider);
    return found?.icon || 'heroicons:server';
}

// Delete confirmation and handling
async function confirmDelete() {
    if (!window.confirm('Are you sure you want to disconnect this Git repository?')) return;
    
    isLoading.value = true;
    try {
        await useMyFetch(`/data_sources/${props.datasourceId}/git_repository/${connectedRepo.value.id}`, {
            method: 'DELETE'
        });
        
        toast.add({
            title: 'Success',
            description: 'Git repository disconnected successfully',
            color: 'green'
        });
        gitModalOpen.value = false;
    } catch (error) {
        toast.add({
            title: 'Error',
            description: 'Failed to disconnect Git repository',
            color: 'red'
        });
    } finally {
        isLoading.value = false;
    }
}

async function reindexRepository() {
    if (!connectedRepo.value || !connectedRepo.value.id) return;
    
    isReindexing.value = true;
    try {
        const response = await useMyFetch(`/data_sources/${props.datasourceId}/git_repository/${connectedRepo.value.id}/index`, {
            method: 'POST'
        });
        
        if (response.status.value === 'success') {
            toast.add({
                title: 'Success',
                description: 'Repository reindexing started',
                color: 'green'
            });
        }
    } catch (error) {
        toast.add({
            title: 'Error',
            description: 'Failed to reindex repository',
            color: 'red'
        });
    } finally {
        isReindexing.value = false;
    }
}
</script>
