<template>
    <div class="mt-4">
        <div class="flex justify-between items-center mb-2">

            <div class="w-1/2">

                <input
                    type="text"
                    v-model="searchQuery"
                    placeholder="Search members..."
                    class="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:ring-blue-500 focus:border-blue-500 w-full"
                >
            </div>
            <button @click="inviteModalOpen = true" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
                v-if="useCan('add_organization_members')"
            >Add Member</button>
        </div>
        <div class="overflow-x-auto">
            <div class="inline-block min-w-full align-middle">
                <div class="overflow-hidden shadow ring-1 ring-black ring-opacity-5 rounded-lg">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50">
                            <tr>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Role</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Last Seen</th>
                                <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Date</th>
                                <th
                                    v-if="useCan('remove_organization_members')"
                                    class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200">
                            <tr v-for="member in filteredMembers" :key="member.id" class="hover:bg-gray-50">
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <div v-if="member.user" class="flex items-center">
                                        <div class="flex-shrink-0 h-10 w-10">
                                            <div class="h-10 w-10 rounded-full bg-gray-200 flex items-center justify-center">
                                                <span class="text-gray-500 font-medium">
                                                    {{ member.user.name?.[0]?.toUpperCase() || member.user.email[0].toUpperCase() }}
                                                </span>
                                            </div>
                                        </div>
                                        <div class="ml-4">
                                            <div class="text-sm font-medium text-gray-900">{{ member.user.name }}</div>
                                            <div class="text-sm text-gray-500">{{ member.user.email }}</div>
                                        </div>
                                    </div>
                                    <div v-else class="text-sm text-gray-900">{{ member.email }}</div>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <UDropdown 
                                        v-model="member.role" 
                                        :items="[
                                            [{ label: 'Member', value: 'member', click: () => updateMemberRole(member, 'member') }],
                                            [{ label: 'Admin', value: 'admin', click: () => updateMemberRole(member, 'admin')}]
                                        ]"
                                        :popper="{ placement: 'bottom-start' }"
                                        v-if="useCan('update_organization_members')"
                                    >
                                        <UButton 
                                            color="white" 
                                            :label="member.role.charAt(0).toUpperCase() + member.role.slice(1)" 
                                            trailing-icon="i-heroicons-chevron-down-20-solid" 
                                        />
                                    </UDropdown>
                                    <button v-else class="text-gray-500 cursor-default">
                                        {{ member.role.charAt(0).toUpperCase() + member.role.slice(1) }}
                                    </button>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap">
                                    <span v-if="member.user" 
                                          class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">
                                        Active
                                    </span>
                                    <span v-else 
                                          class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">
                                        Pending
                                    </span>
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                    {{ member.user?.last_seen || '-' }}
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                    {{ member.created_at }}
                                </td>
                                <td class="px-6 py-4 whitespace-nowrap text-sm"
                                    v-if="useCan('remove_organization_members')"
                                >
                                    <button 
                                        @click="removeMember(member)"
                                        class="text-red-600 hover:text-red-900 font-medium transition-colors duration-150"
                                    >
                                        Remove
                                    </button>

                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    <UModal v-model="inviteModalOpen">
        <div class="p-4 relative">
            <button @click="inviteModalOpen = false" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Invite Member</h1>
            <p class="text-sm text-gray-500">Add a new member to your organization</p>
            <hr class="my-4" />
            
            <form @submit.prevent="inviteMember" class="space-y-4">
                <div class="flex flex-col">
                    <label class="text-sm font-medium text-gray-700 mb-2">Email</label>
                    <input 
                        v-model="inviteForm.email"
                        type="email" 
                        required
                        class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"
                        placeholder="member@example.com"
                    />
                </div>
                
                <div class="flex flex-col">
                    <label class="text-sm font-medium text-gray-700 mb-2">Role</label>
                    <select 
                        v-model="inviteForm.role"
                        required
                        class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 sm:text-sm"
                    >
                        <option value="member">Member</option>
                        <option value="admin">Admin</option>
                    </select>
                </div>

                <div class="flex justify-end space-x-2 pt-4">
                    <button 
                        type="button"
                        @click="inviteModalOpen = false"
                        class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                    >
                        Cancel
                    </button>
                    <button 

                        type="submit"
                        class="px-3 py-1.5 text-xs font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600"
                    >
                        Send Invitation
                    </button>
                </div>
            </form>
        </div>
    </UModal>
</template>

<script setup lang="ts">

const props = defineProps(['organization'])
const organizationId = props.organization.id
const members = ref([])
const searchQuery = ref('')
const toast = useToast()

async function updateMemberRole(member: any, role: string) {
    try {
        const response = await useMyFetch(`/organizations/${organizationId}/members/${member.id}`, {
            method: 'PUT',
            body: { role: role }
        })

        if (response.error.value) {
            const errorDetail = response.error.value.data?.detail
            toast.add({
                title: 'Error',
                description: errorDetail || 'Failed to update role',
                color: 'red'
            })
            throw new Error(errorDetail || 'Failed to update role')
        }

        member.role = role
        toast.add({
            title: 'Success',
            description: `Role updated to ${role}`,
            color: 'green'
        })
    } catch (error: any) {
    }
}

const filteredMembers = computed(() => {
    const query = searchQuery.value.toLowerCase()
    if (!query) return members.value
    
    return members.value.filter(member => {
        const name = member.user?.name?.toLowerCase() || ''
        const email = (member.user?.email || member.email).toLowerCase()
        return name.includes(query) || email.includes(query)
    })
})

onMounted(() => {
    nextTick(async () => {
        const response = await useMyFetch(`/organizations/${organizationId}/members`)
        members.value = response.data.value
    })
})

const inviteModalOpen = ref(false)
const inviteForm = ref({
    email: '',
    role: 'member',
    organization_id: organizationId
})

const removeMember = async (member: any) => {
    const confirmed = window.confirm(`Are you sure you want to remove ${member.user?.name || member.email} from this organization?`)
    if (!confirmed) return

    try {
        const response = await useMyFetch(`/organizations/${organizationId}/members/${member.id}`, {
            method: 'DELETE'
        })
        
        if (response.error.value) {
            const errorDetail = response.error.value.data?.detail
            toast.add({
                title: 'Error',
                description: errorDetail || 'Failed to remove member',
                color: 'red'
            })
            throw new Error(errorDetail || 'Failed to remove member')
        }
        
        const updatedMembers = await useMyFetch(`/organizations/${organizationId}/members`)
        members.value = updatedMembers.data.value

        toast.add({
            title: 'Success',
            description: `Successfully removed ${member.user?.name || member.email}`,
            color: 'green'
        })

    } catch (error: any) {
        const errorDetail = error.data?.detail || error.message
        toast.add({
            title: 'Error',
            description: errorDetail || 'Failed to remove member',
            color: 'red'
        })
    }
}

const inviteMember = async () => {
    try {
        const response = await useMyFetch(`/organizations/${organizationId}/members`, {
            method: 'POST',
            body: inviteForm.value
        })

        if (response.error.value) {
            const errorDetail = response.error.value.data?.detail
            toast.add({
                title: 'Error',
                description: errorDetail || 'Failed to invite member',
                color: 'red'
            })
            throw new Error(errorDetail || 'Failed to invite member')
        }
        
        const membersResponse = await useMyFetch(`/organizations/${organizationId}/members`)
        members.value = membersResponse.data.value
        
        toast.add({
            title: 'Success',
            description: `Invitation sent to ${inviteForm.value.email}`,
            color: 'green'
        })
        
        inviteForm.value = { email: '', role: 'member', organization_id: organizationId }
        inviteModalOpen.value = false
    } catch (error) {
        console.error('Failed to invite member:', error)
    }
}
</script>