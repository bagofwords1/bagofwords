<template>
    <div class="mt-6">
        <!-- Inner tabs -->
        <div class="border-b border-gray-200 dark:border-gray-700 mb-4">
            <nav class="flex gap-4">
                <button
                    v-for="tab in visibleTabs"
                    :key="tab.key"
                    @click="activeTab = tab.key"
                    :class="[
                        activeTab === tab.key
                            ? 'border-primary-500 text-primary-500'
                            : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300',
                    ]"
                    class="pb-2 border-b-2 text-sm font-medium transition-colors"
                >
                    {{ tab.label }}
                </button>
            </nav>
        </div>

        <MembersComponent v-if="activeTab === 'members'" :organization="organization" />
        <RolesManager v-if="activeTab === 'roles'" :organization="organization" />
    </div>
</template>

<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'

const { organization } = useOrganization()
const activeTab = ref('members')

const tabs = [
    { key: 'members', label: 'Members' },
    { key: 'roles', label: 'Roles', permission: 'manage_roles' },
]

const visibleTabs = computed(() =>
    tabs.filter((tab) => !tab.permission || useCan(tab.permission))
)

definePageMeta({
    layout: 'settings',
})
</script>
