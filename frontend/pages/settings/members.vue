<template>
    <div>
        <!-- Sub-page navigation (Members, Roles, Groups, …) lives in the admin
             sidebar under "Access"; the selected one is ?tab=<key>. -->
        <MembersComponent v-if="activeTab === 'members'" :organization="organization" />
        <RolesManager v-if="activeTab === 'roles'" :organization="organization" />
        <GroupsManager v-if="activeTab === 'groups'" :organization="organization" />
        <ServiceAccountsManager v-if="activeTab === 'service_accounts'" :organization="organization" />
        <QuotaPoliciesManager v-if="activeTab === 'quotas'" :organization="organization" />
        <SignupPolicyManager v-if="activeTab === 'signup'" :organization="organization" />
    </div>
</template>

<script setup lang="ts">
import { useAdminNav } from '~/composables/useAdminNav'

const route = useRoute()
const { organization } = useOrganization()
const { visibleItems } = useAdminNav()

// Permission + license gating is shared with the sidebar (useAdminNav). An
// unknown or forbidden ?tab falls back to the first sub-page the user may see.
const visibleTabs = computed(() =>
    visibleItems.value.find(item => item.name === 'members')?.children || []
)
const activeTab = computed(() => {
    const requested = String(route.query.tab || '')
    const tabs = visibleTabs.value
    return tabs.find(tab => tab.key === requested)?.key || tabs[0]?.key || 'members'
})

definePageMeta({
    permissions: ['view_members'],
})
</script>
