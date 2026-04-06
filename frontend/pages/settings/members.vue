<template>
    <div class="mt-6">
        <!-- Inner tabs -->
        <div class="border-b border-gray-200 dark:border-gray-700 mb-4">
            <nav class="-mb-px flex space-x-6">
                <button
                    v-for="tab in visibleTabs"
                    :key="tab.key"
                    @click="activeTab = tab.key"
                    :class="[
                        activeTab === tab.key
                            ? 'border-blue-500 text-blue-600'
                            : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700 dark:hover:text-gray-300',
                    ]"
                    class="whitespace-nowrap border-b-2 py-2 px-1 text-sm font-medium transition-colors"
                >
                    {{ tab.label }}
                </button>
            </nav>
        </div>

        <MembersComponent v-if="activeTab === 'members'" :organization="organization" />
        <RolesManager v-if="activeTab === 'roles'" :organization="organization" />
        <GroupsManager v-if="activeTab === 'groups'" :organization="organization" />
    </div>
</template>

<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { organization } = useOrganization()
const activeTab = ref('members')
const { hasFeature } = useEnterprise()

const tabs = [
    { key: 'members', label: 'Members' },
    { key: 'roles', label: 'Roles', permission: 'manage_roles', feature: 'custom_roles' },
    { key: 'groups', label: 'Groups', permission: 'manage_groups', feature: 'custom_roles' },
]

const visibleTabs = computed(() =>
    tabs.filter((tab) => {
        if (tab.feature && !hasFeature(tab.feature)) return false
        if (tab.permission && !useCan(tab.permission)) return false
        return true
    })
)

definePageMeta({
    layout: 'settings',
})
</script>
