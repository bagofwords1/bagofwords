<template>
    <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-2/3 md:pt-10">
        <div class="w-full px-4 pl-0 py-4">

            <div v-if="integration">
                <h1 class="text-lg font-semibold text-center">
                    <GoBackChevron v-if="isExcel" />
                    <DataSourceIcon :type="integration.type" class="w-5 inline" />
                    {{ integrationName }}
                </h1>
                <p class="text-sm text-gray-500">
                    <div class="mt-4 text-center">
                        <span class="text-green-500 font-semibold" v-if="testConnectionStatus?.success">
                            <UIcon name="heroicons-check-circle"/>
                        </span>
                        <span class="text-red-500 font-semibold" v-else>
                            <UIcon name="heroicons-x-circle"/>
                        </span>
                        {{ testConnectionStatus.success ? 'Connected' : 'Failed to connect' }}
                    </div>
                </p>

                <div class="flex border-b border-gray-200 mt-7">
                    <button 
                        @click="activeTab = 'main'"
                        class="px-4 py-1 text-sm"
                        :class="{ 
                            'border-b-2 border-blue-500 text-blue-600': activeTab === 'main', 
                            'text-gray-500': activeTab !== 'main',
                            'opacity-50 cursor-not-allowed': !testConnectionStatus?.success 
                        }"
                        :disabled="!testConnectionStatus?.success"
                    >
                        Main
                    </button>
                    <button 
                        @click="activeTab = 'schema'"
                        class="px-4 py-1 text-sm"
                        :class="{ 
                            'border-b-2 border-blue-500 text-blue-600': activeTab === 'schema', 
                            'text-gray-500': activeTab !== 'schema',
                            'opacity-50 cursor-not-allowed': !testConnectionStatus?.success 
                        }"
                        :disabled="!testConnectionStatus?.success"
                    >
                        Context
                    </button>
                    <button 
                        @click="activeTab = 'context'"
                        class="px-4 py-1 text-sm"
                        :class="{ 
                            'border-b-2 border-blue-500 text-blue-600': activeTab === 'context', 
                            'text-gray-500': activeTab !== 'context',
                            'opacity-50 cursor-not-allowed': !testConnectionStatus?.success 
                        }"
                        :disabled="!testConnectionStatus?.success"
                    >
                        AI Rules
                    </button>
                    <button 
                        @click="activeTab = 'access'"
                        class="px-4 py-1 text-sm"
                        :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'access', 'text-gray-500': activeTab !== 'access' }"
                    >
                        Access
                    </button>
                    <button 
                        @click="activeTab = 'configuration'"
                        class="px-4 py-1 text-sm"
                        :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'configuration', 'text-gray-500': activeTab !== 'configuration' }"
                    >
                        Configuration
                    </button>
                </div>

                <div v-if="activeTab === 'main'" class="mt-4 w-full">
                    <div class="text-sm text-black">
                        <span class="font-medium">Description</span>
                        <button v-if="canUpdateDataSource" @click="generateItem('description')" class=" float-right mb-2 text-blue-500 bg-white hover:bg-gray-100 border border-gray-300 rounded-md px-2 py-1 text-xs hover:text-blue-600">
                            <UIcon v-if="generatingItem === 'description'" name="heroicons-arrow-path" class="w-4 h-4 animate-spin inline-block" />
                            <span v-else>✨ Generate</span>
                        </button>
                        <p class="mt-2 mb-4">
                            <UTextarea 
                                v-if="canUpdateDataSource"
                                v-model="integration.description" 
                                :rows="8" 
                                class="w-full outline-none whitespace-pre-wrap" 
                            />
                            <div v-else class="bg-gray-50 border border-gray-200 rounded-md p-3 text-sm text-gray-700 whitespace-pre-wrap min-h-[160px]">
                                {{ integration.description || 'No description provided.' }}
                            </div>
                        </p>
                    </div>
                    <div class="mt-4 text-sm">
                        <div class="flex justify-between">
                        <span class="font-medium">Conversation Starters</span>
                        <button v-if="canUpdateDataSource" @click="generateItem('conversation_starters')" class=" float-right text-blue-500 bg-white hover:bg-gray-100 border border-gray-300 rounded-md px-2 py-1 text-xs hover:text-blue-600">
                            <UIcon v-if="generatingItem === 'conversation_starters'" name="heroicons-arrow-path" class="w-4 h-4 animate-spin inline-block" />
                            <span v-else>✨ Generate</span>
                        </button>
                    </div>
                        <p class="mt-2 mb-4">
                            <ul v-if="canUpdateDataSource">
                                <li v-for="(starter, index) in integration.conversation_starters" :key="index" class="mb-2 flex items-center gap-2">
                                    <UTextarea 
                                        v-model="integration.conversation_starters[index]"
                                        :rows="4"
                                        size="sm"
                                        class="w-full leading-4" 
                                        placeholder="Show me list of customers
Create a table of all customers, show customer name, email, phone, address and country. Add aggregated payments and orders data"
                                    />
                                    <button @click="deleteConversationStarter(index)" class="text-red-500 w-7 hover:text-red-600">
                                        <UIcon name="heroicons-trash" class="w-4 h-4" />
                                    </button>
                                </li>
                                <li>
                                    <button @click="addConversationStarter" class="flex items-center text-blue-500 hover:text-blue-600">
                                        <UIcon name="heroicons-plus" class="w-4 h-4 inline mr-1" />
                                        Add conversation starter
                                    </button>
                                </li>
                            </ul>
                            <div v-else>
                                <div v-if="integration.conversation_starters && integration.conversation_starters.length > 0" class="space-y-2">
                                    <div v-for="(starter, index) in integration.conversation_starters" :key="index" class="bg-gray-50 border border-gray-200 rounded-md p-3 text-sm text-gray-700">
                                        {{ starter }}
                                    </div>
                                </div>
                                <div v-else class="bg-gray-50 border border-gray-200 rounded-md p-3 text-sm text-gray-500">
                                    No conversation starters configured.
                                </div>
                            </div>
                        </p>
                    </div>
                    <div v-if="canUpdateDataSource">
                        <button @click="updateIntegrationMain()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                            Save
                        </button>
                    </div>
                </div>

                <div v-if="activeTab === 'schema'" class="mt-4">
                    <div class="bg-blue-50 border border-blue-200 rounded-md p-4 mb-4">
                        <div class="flex items-start">
                            <UIcon name="heroicons-light-bulb" class="w-5 h-5 text-blue-500 mt-0.5 mr-2" />
                            <div>
                                <p class="text-sm text-blue-600">
                                    <span v-if="canUpdateDataSource">
                                        You can toggle tables and data models on/off to control which ones are included in AI queries. Inactive items will be excluded from the schema provided to AI agents.
                                    </span>
                                    <span v-else>
                                        This shows the database tables and data models available for AI queries. Only active tables are shown.
                                    </span>
                                </p>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Database Schema Section -->
                    <div class="border rounded-md p-4 mb-4">
                        <div class="flex justify-between items-center mb-2">
                            <div class="font-semibold cursor-pointer flex items-center" @click="schemaExpanded = !schemaExpanded">
                                <UIcon :name="schemaExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1 mr-2" />
                                Database Schema ({{ (canUpdateDataSource ? schema : simpleSchema) ? (canUpdateDataSource ? schema.length : simpleSchema.length) : 0 }} tables)
                            </div>
                            <button v-if="canUpdateDataSource" @click="refreshSchema()"
                                :disabled="isRefreshing"
                                class="bg-white border border-gray-300 text-gray-500 px-4 py-2 text-xs rounded-md hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                                <UIcon v-if="isRefreshing" name="heroicons-arrow-path" class="animate-spin" />
                                <UIcon v-else name="heroicons-arrow-path" />
                                Refresh Schema
                            </button>
                        </div>
                        
                        <div v-if="schemaExpanded">
                            <!-- Admin view with checkboxes and editing capabilities -->
                            <div v-if="canUpdateDataSource && schema" class="mt-2">
                                <ul class="py-2 list-none list-inside">
                                    <li class="py-1" v-for="table in schema" :key="table.name">
                                        <UCheckbox v-model="table.is_active" checked-icon="heroicons-check" unchecked-icon="heroicons-x" class="float-left mr-2"/>
                                        <div @click="toggleTable(table)" class="font-semibold text-gray-500 cursor-pointer">
                                            <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1" />
                                            {{ table.name }}
                                        </div>
                                        <ul v-if="expandedTables[table.name]" class="ml-4 mt-1 text-sm">
                                            <li v-for="column in table.columns" :key="column.name" class="flex py-0.5">
                                                <span class="text-gray-500 mr-2">{{ column.name }}</span>
                                                <span class="text-gray-400">{{ column.dtype }}</span>
                                            </li>
                                        </ul>
                                    </li>
                                </ul>
                                <button @click="updateTableStatus()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-4 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                                    Save Schema Tables
                                </button>
                            </div>
                            
                            <!-- Non-admin view without checkboxes -->
                            <div v-else-if="!canUpdateDataSource && simpleSchema" class="mt-2">
                                <ul class="py-2 list-none list-inside">
                                    <li class="py-1" v-for="table in simpleSchema" :key="table.name">
                                        <div @click="toggleTable(table)" class="font-semibold text-gray-500 cursor-pointer">
                                            <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1" />
                                            {{ table.name }}
                                        </div>
                                        <ul v-if="expandedTables[table.name]" class="ml-4 mt-1 text-sm">
                                            <li v-for="column in table.columns" :key="column.name" class="flex py-0.5">
                                                <span class="text-gray-500 mr-2">{{ column.name }}</span>
                                                <span class="text-gray-400">{{ column.dtype }}</span>
                                            </li>
                                        </ul>
                                    </li>
                                </ul>
                            </div>
                            
                            <!-- Loading state -->
                            <div v-else-if="testConnectionStatus?.success" class="flex justify-center items-center p-4">
                                <UIcon name="heroicons-arrow-path" class="animate-spin" />
                            </div>
                        </div>
                    </div>
                    
                    <!-- Data Models Section -->
                    <div class="border rounded-md p-4 mb-4">
                        <div class="flex justify-between items-center mb-2">
                            <div class="font-semibold cursor-pointer flex items-center" @click="dataModelsExpanded = !dataModelsExpanded">
                                <UIcon :name="dataModelsExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1 mr-2" />
                                Context ({{ metadata_resources?.resources ? metadata_resources?.resources?.length : 0 }} resources)
                            </div>
                            <UButton 
                                v-if="canUpdateDataSource"
                                @click="showGitModal = true"
                                icon="heroicons:code-bracket"
                                :label="integration.git_repository ? integration.git_repository.repo_url : 'Connect Git Repository'"
                                class="bg-white border border-gray-300 text-gray-500 px-4 py-2 text-xs rounded-md hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50"
                            />
                            <span v-else-if="integration.git_repository" class="text-sm text-gray-500">
                                Connected: {{ integration.git_repository.repo_url }}
                            </span>
                        </div>
                        
                        <div v-if="dataModelsExpanded">
                            <div>
                                <p class="text-sm">
                                    Load context from dbt, lookml, markdown files, and other pieces of code by connecting your git repo to your data source.
                                </p>
                                <div v-if="integration.git_repository">
                                    <p class="text-sm text-gray-500 mt-4" v-if="integration.git_repository.status === 'pending'">
                                        Indexing...
                                    </p>
                                    <p class="text-sm text-gray-500 mt-4" v-else-if="integration.git_repository.status === 'completed'">
                                        Last indexed: {{ new Date(metadata_resources.completed_at).toLocaleString() }}
                                    </p>
                                    <p>
                                        <ul class="py-2 list-none list-inside" v-if="metadata_resources?.resources">
                                            <li class="py-1" v-for="resource in metadata_resources.resources" :key="resource.id">
                                                <UCheckbox v-if="canUpdateDataSource" v-model="resource.is_active" class="float-left mr-2"/>
                                                <span v-else :class="['inline-block w-4 h-4 rounded border mr-2', resource.is_active ? 'bg-blue-500 border-blue-500' : 'bg-gray-200 border-gray-300']"></span>
                                                <div @click="toggleResource(resource)" class="font-semibold text-gray-500 cursor-pointer">
                                                    <UIcon :name="expandedResources[resource.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1" />
                                                    <UIcon name="heroicons:cube" class="w-4 h-4 inline-block" v-if="resource.resource_type === 'model' || resource.resource_type === 'model_config'"/>
                                                    <UIcon name="heroicons:hashtag" class="w-4 h-4 inline-block" v-if="resource.resource_type === 'metric'"/>
                                                    {{ resource.name }}
                                                </div>
                                                <ul v-if="expandedResources[resource.id]" class="ml-4 mt-1 text-sm">
                                                    <li>Description: {{ resource.description }}</li>
                                                    <li>
                                                        <pre class="whitespace-pre-wrap overflow-x-auto max-w-full">
                                                         {{ resource.raw_data }}
                                                        </pre>
                                                    </li>
                                                </ul>
                                            </li>
                                        </ul>
                                    </p>
                                    <button v-if="canUpdateDataSource" @click="updateResourceStatus()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-4 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                                        Save Resources
                                    </button>
                                </div>
                                <div v-else>
                                    <p class="text-sm text-gray-500 mt-4">
                                        No git repository connected.
                                        <span v-if="canUpdateDataSource">Click the button above to connect one.</span>
                                    </p>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <GitRepoModalComponent 
                        v-model="showGitModal"
                        :datasource-id="String(ds_id)"
                        :git-repository="integration.git_repository"
                        :metadata-resources="metadata_resources"
                        @update:modelValue="handleGitModalClose"
                    />
                    
            
                </div>

                <div v-if="activeTab === 'context'" class="mt-4">
                    <div class="mt-4 flex">
                        <div class="w-2/3 pr-4 space-y-4">
                            <!-- AI Context Section -->
                            <div>
                                <h3 class="text-sm font-semibold mb-2">AI Context</h3>
                                <UTextarea 
                                    v-if="canUpdateDataSource"
                                    v-model="integration.context" 
                                    :rows="15" 
                                    class="w-full"
                                    placeholder="Add context for AI agents about this data source:

** Key tables and fields **
- table_a - represents users
- table_b - represents customers

** Example queries and code **
- SELECT * FROM table_a WHERE date > '2024-01-01'
- SELECT * FROM table_b order by date desc limit 10

** Data formatting requirements **
- Date is in EST time.
- Don't allow more than 5 columns in a query.

** Rules for using the data **
- Prioritize tables A and B.
- Use table C only if table A and B don't have the data you need.
- Always use left joins with table A.
- Always order by date desc.

"
                                />
                                <div v-else class="bg-gray-50 border border-gray-200 rounded-md p-4 text-sm text-gray-700 whitespace-pre-wrap min-h-[300px]">
                                    {{ integration.context || 'No AI context provided.' }}
                                </div>
                                <button v-if="canUpdateDataSource" @click="updateIntegrationContext()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                                    Save
                                </button>
                            </div>

                            <!-- Schema Section (for non-admin users) -->
                            <div v-if="!canUpdateDataSource && simpleSchema">
                                <h3 class="text-sm font-semibold mb-2">Available Tables</h3>
                                <div class="bg-white border border-gray-200 rounded-md">
                                    <div v-if="simpleSchema && simpleSchema.length > 0">
                                        <ul class="divide-y divide-gray-200">
                                            <li v-for="table in simpleSchema" :key="table.name" class="p-3">
                                                <div class="font-medium text-gray-900 text-sm mb-1">{{ table.name }}</div>
                                                <div class="text-xs text-gray-500">
                                                    {{ table.columns.length }} columns
                                                    <span v-if="table.columns.length > 0" class="ml-2">
                                                        ({{ table.columns.slice(0, 3).map(col => col.name).join(', ') }}<span v-if="table.columns.length > 3">...</span>)
                                                    </span>
                                                </div>
                                            </li>
                                        </ul>
                                    </div>
                                    <div v-else class="p-4 text-center text-gray-500 text-sm">
                                        No tables available
                                    </div>
                                </div>
                            </div>
                        </div>
                        <div class="w-1/3 bg-gray-100 p-4 rounded-lg">
                            <h1 class="text-sm font-semibold">
                                What is AI Context?
                            </h1>
                            <p class="text-sm text-gray-500 leading-6 mt-4">
                                The AI context for this data source is a place to add any information that you want to share with the AI agents that are using this data source.
                                
                                <br />
                                <br />
                                <b>Useful information to include:</b>
                                <ul class="list-disc list-inside mt-4">
                                    <li class="mb-3">
                                        Rules for how to use the data: <br />
                                        • Prioritize tables A and B. <br />
                                        • Use table C only if table A and B don't have the data you need. <br />
                                        • Date is EST time. <br />
                                        • Always use left joins with table A. <br />
                                        • Always order by date desc.
                                    </li>
                                    <li class="mb-3">Sample code queries
                                        Users: SELECT * FROM table_a WHERE date > '2024-01-01' <br />
                                        Customers: SELECT * FROM table_b order by date desc limit 10
                                    </li>
                                    <li class="mb-3">Any other information that you want to share with the AI agents</li>
                                </ul>
                            </p>
                        </div>
                    </div>
                </div>

                <div v-if="activeTab === 'access'" class="mt-4">
                    <!-- Visibility Section -->
                    <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden mb-6">
                        <div class="px-6 py-4 border-b border-gray-200 bg-gray-50">
                            <h3 class="text-lg font-semibold text-gray-900">Data Source Visibility</h3>
                            <p class="text-sm text-gray-500 mt-1">Control who can access this data source</p>
                        </div>
                        <div class="px-6 py-4">
                            <div class="flex items-center justify-between">
                                <div>
                                    <div class="flex items-center">
                                        <label class="text-sm font-medium text-gray-700">
                                           Public Access 
                                        </label>
                                        <UToggle 
                                            v-if="canUpdateDataSource"
                                            :model-value="integration.is_public" 
                                            @click="handleVisibilityToggleClick"
                                            class="ml-3"
                                        />
                                        <span v-else class="ml-3 text-sm text-gray-500">
                                            {{ integration.is_public ? 'Public' : 'Private' }}
                                        </span>
                                    </div>
                                    <p class="text-sm text-gray-500 mt-1">
                                        Public data sources are accessible to all organization members. Private data sources require individual user access management.
                                    </p>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Access Management Section -->
                    <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                        <div class="px-6 py-4 border-b border-gray-200 bg-gray-50">
                            <div class="flex items-center justify-between">
                                <div>
                                    <h3 class="text-lg font-semibold text-gray-900">Individual Access Management</h3>
                                    <p class="text-sm text-gray-500 mt-1">Manage user access to this private data source</p>
                                </div>
                                <button 
                                    v-if="canUpdateDataSource && !integration?.is_public"
                                    @click="showUserModal = true"
                                    class="px-4 py-2 text-sm font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50"
                                >
                                    Add Users
                                </button>
                            </div>
                        </div>
                        
                        <div v-if="integration?.is_public" class="px-6 py-4">
                            <div class="bg-blue-50 border border-blue-200 rounded-md p-4">
                                <div class="flex items-start">
                                    <UIcon name="heroicons-information-circle" class="w-5 h-5 text-blue-500 mt-0.5 mr-2" />
                                    <div>
                                        <p class="text-sm text-blue-600">
                                            This data source is public and accessible to all organization members. Individual user access management is disabled.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        <div v-else>
                            <div class="overflow-x-auto">
                                <table v-if="dataSourceMembers && dataSourceMembers.length > 0" class="min-w-full divide-y divide-gray-200">
                                    <thead class="bg-gray-50">
                                        <tr>
                                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User</th>
                                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Access Level</th>
                                            <th v-if="canUpdateDataSource" class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody class="bg-white divide-y divide-gray-200">
                                        <tr v-for="member in dataSourceMembers" :key="member.id" class="hover:bg-gray-50">
                                            <td class="px-6 py-4 whitespace-nowrap">
                                                <div class="flex items-center">
                                                    <UIcon name="heroicons-user" class="w-5 h-5 text-gray-400 mr-3" />
                                                    <span class="text-sm font-medium text-gray-900">{{ getUserDisplayName(member.principal_id) }}</span>
                                                </div>
                                            </td>
                                            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                                Member
                                            </td>
                                            <td v-if="canUpdateDataSource" class="px-6 py-4 whitespace-nowrap">
                                                <button 
                                                    @click="removeMember(member.principal_id)"
                                                    class="text-red-600 hover:text-red-900 font-medium transition-colors duration-150"
                                                >
                                                    <UIcon name="heroicons-trash" class="w-4 h-4" />
                                                </button>
                                            </td>
                                        </tr>
                                    </tbody>
                                </table>
                                
                                <!-- Empty state -->
                                <div v-else class="px-6 py-12 text-center">
                                    <UIcon name="heroicons-users" class="mx-auto h-12 w-12 text-gray-400" />
                                    <h3 class="mt-2 text-sm font-medium text-gray-900">No users have access</h3>
                                    <p class="mt-1 text-sm text-gray-500">
                                        Get started by adding users to this private data source.
                                    </p>
                                    <button 
                                        v-if="canUpdateDataSource"
                                        @click="showUserModal = true"
                                        class="mt-4 px-4 py-2 text-sm font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50"
                                    >
                                        Add Users
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Visibility Confirmation Modal -->
                    <UModal v-model="showVisibilityConfirm" :ui="{ width: 'sm:max-w-md' }">
                        <div class="p-6">
                            <div class="flex items-center justify-between mb-4">
                                <h3 class="text-lg font-semibold">Confirm Visibility Change</h3>
                                <button @click="showVisibilityConfirm = false" class="text-gray-400 hover:text-gray-500">
                                    <UIcon name="heroicons-x-mark" class="w-5 h-5" />
                                </button>
                            </div>
                            
                            <div class="mb-6">
                                <p class="text-sm text-gray-600">
                                    Are you sure you want to make this data source 
                                    <strong>{{ pendingVisibilityChange ? 'public' : 'private' }}</strong>?
                                </p>
                                <div v-if="pendingVisibilityChange" class="mt-3 p-3 bg-yellow-50 border border-yellow-200 rounded-md">
                                    <p class="text-sm text-yellow-700">
                                        Making this data source public will give all organization members access to it.
                                    </p>
                                </div>
                                <div v-else class="mt-3 p-3 bg-blue-50 border border-blue-200 rounded-md">
                                    <p class="text-sm text-blue-700">
                                        Making this data source private will require individual user access management.
                                    </p>
                                </div>
                            </div>
                            
                            <div class="flex justify-end space-x-2">
                                <button 
                                    @click="cancelVisibilityChange"
                                    class="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                                >
                                    Cancel
                                </button>
                                <button 
                                    @click="confirmVisibilityChange"
                                    class="px-4 py-2 text-sm font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600"
                                >
                                    Confirm
                                </button>
                            </div>
                        </div>
                    </UModal>
                </div>

                <div v-if="activeTab === 'configuration'" class="mt-4">
                    <div class="mt-4">
                        <div class="font-semibold mb-2">Data Source Settings</div>
                        
                        <!-- Read-only view for users without update permission -->
                        <div v-if="!canUpdateDataSource">
                            <div class="mb-4">
                                <label class="block text-sm font-medium text-gray-700 mb-2">Name</label>
                                <div class="bg-gray-50 border border-gray-200 rounded-md p-3 text-sm text-gray-700">
                                    {{ integration.name }}
                                </div>
                            </div>
                            
                            <div v-if="configFields.length > 0" class="mt-4 bg-gray-50 p-5 rounded border">
                                <h3 class="text-sm font-semibold mb-4">Configuration</h3>
                                <div v-for="field in configFields" :key="field.field_name" class="mb-4">
                                    <label class="block text-sm font-medium text-gray-700 mb-2">
                                        {{ field.title || field.field_name }}
                                    </label>
                                    <div class="bg-white border border-gray-200 rounded-md p-3 text-sm text-gray-700">
                                        <span v-if="field.type === 'boolean'">
                                            {{ configFormData[field.field_name] ? 'Yes' : 'No' }}
                                        </span>
                                        <span v-else>
                                            {{ configFormData[field.field_name] || 'Not configured' }}
                                        </span>
                                    </div>
                                </div>
                            </div>

                            <div v-if="credentialFields.length > 0" class="mt-4 bg-gray-50 p-5 rounded border">
                                <h3 class="text-sm font-semibold mb-4">Credentials</h3>
                                <div v-for="field in credentialFields" :key="field.field_name" class="mb-4">
                                    <label class="block text-sm font-medium text-gray-700 mb-2">
                                        {{ field.title || field.field_name }}
                                    </label>
                                    <div class="bg-white border border-gray-200 rounded-md p-3 text-sm text-gray-700">
                                        <span v-if="isPasswordField(field.field_name)">
                                            {{ credentialsFormData[field.field_name] ? '••••••••' : 'Not configured' }}
                                        </span>
                                        <span v-else>
                                            {{ credentialsFormData[field.field_name] || 'Not configured' }}
                                        </span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Editable form for users with update permission -->
                        <form v-else @submit.prevent="handleConfigUpdate" class="mb-8">
                            <div class="mb-4">
                                <input 
                                    v-model="integration.name"
                                    type="text" 
                                    placeholder="Name" 
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
                                />
                            </div>
                            
                            <div v-if="configFields.length > 0" class="mt-4 bg-gray-50 p-5 rounded border">
                                <h3 class="text-sm font-semibold mb-4">Configuration</h3>
                                <div v-for="field in configFields" :key="field.field_name" class="mb-4">
                                    <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                                        {{ field.title || field.field_name }}
                                    </label>
                                    <input
                                        v-if="field.type === 'string'"
                                        type="text"
                                        v-model="configFormData[field.field_name]"
                                        :id="field.field_name"
                                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                                        :placeholder="field.title || field.field_name"
                                    />
                                    <input
                                        v-else-if="field.type === 'integer'"
                                        type="number"
                                        v-model="configFormData[field.field_name]"
                                        :id="field.field_name"
                                        :min="field.minimum"
                                        :max="field.maximum"
                                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                                        :placeholder="field.title || field.field_name"
                                    />
                                    <input
                                        v-else-if="field.type === 'boolean'"
                                        type="checkbox"
                                        v-model="configFormData[field.field_name]"
                                        :id="field.field_name"
                                        class="mt-1 h-4 w-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                                    />
                                </div>
                            </div>

                            <div v-if="credentialFields.length > 0" class="mt-4 bg-gray-50 p-5 rounded border">
                                <h3 class="text-sm font-semibold mb-4">Credentials
                                    <button 
                                        type="button"
                                        @click="editingCredentials = !editingCredentials" 
                                        class="text-blue-500 hover:text-blue-600 float-right"
                                    >
                                        <UIcon v-if="editingCredentials" name="heroicons-x-mark" class="w-4 h-4" />
                                        <UIcon v-else name="heroicons-pencil" class="w-4 h-4" />
                                    </button>
                                </h3>
                                <div v-for="field in credentialFields" :key="field.field_name" class="mb-4">
                                    <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                                        {{ field.title || field.field_name }}
                                    </label>
                                    <input
                                        v-if="field.type === 'string'"
                                        :type="isPasswordField(field.field_name) ? 'password' : 'text'"
                                        v-model="credentialsFormData[field.field_name]"
                                        :id="field.field_name"
                                        :disabled="!editingCredentials"
                                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                                        :placeholder="field.title || field.field_name"
                                    />
                                </div>
                            </div>

                            <button 
                                type="submit"
                                class="bg-blue-500 mt-4 text-white px-4 py-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 mr-4"
                            >
                                Save Configuration
                            </button>
                        </form>
                        
                        <div v-if="canUpdateDataSource" class="mt-4 p-4 bg-gray-50 rounded-lg border border-red-200">
                            <div class="font-semibold text-red-500">Danger Zone</div>
                            <button @click="handleDelete()" class="mt-4 bg-red-500 text-xs text-white px-4 py-2 rounded-md hover:bg-red-600 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-opacity-50">
                                Delete Data Source
                            </button>
                        </div>
                    </div>
                </div>
            </div>
            <div v-else>
                <p>Loading...</p>
            </div>
        </div>

        <!-- User Selection Modal -->
                    <UModal v-model="showUserModal" :ui="{ width: 'sm:max-w-md' }">
                        <div class="p-6">
                            <div class="flex items-center justify-between mb-4">
                                <h3 class="text-lg font-semibold">Add Users to Data Source</h3>
                                <button @click="showUserModal = false" class="text-gray-400 hover:text-gray-500">
                                    <UIcon name="heroicons-x-mark" class="w-5 h-5" />
                                </button>
                            </div>
                            
                            <div class="mb-4">
                                <label class="block text-sm font-medium text-gray-700 mb-2">
                                    Select Users
                                </label>
                                <USelectMenu
                                    v-model="selectedUsers"
                                    :options="availableUsers"
                                    multiple
                                    option-attribute="display_name"
                                    value-attribute="user_id"
                                    placeholder="Choose users to grant access..."
                                    class="w-full"
                                />
                            </div>
                            
                            <div v-if="selectedUsers.length > 0" class="mb-4">
                                <label class="block text-sm font-medium text-gray-700 mb-2">
                                    Selected Users
                                </label>
                                <div class="space-y-2">
                                    <div 
                                        v-for="userId in selectedUsers" 
                                        :key="userId" 
                                        class="flex items-center justify-between bg-gray-50 p-2 rounded"
                                    >
                                        <div class="flex items-center">
                                            <UIcon name="heroicons-user" class="w-4 h-4 text-gray-400 mr-2" />
                                            <span class="text-sm">{{ getUserDisplayName(userId) }}</span>
                                        </div>
                                        <button 
                                            @click="removeSelectedUser(userId)"
                                            class="text-red-500 hover:text-red-600"
                                        >
                                            <UIcon name="heroicons-x-mark" class="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>
                            </div>
                            
                            <div class="flex justify-end space-x-2">
                                <button 
                                    @click="showUserModal = false"
                                    class="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                                >
                                    Cancel
                                </button>
                                <button 
                                    @click="addSelectedUsers"
                                    :disabled="selectedUsers.length === 0 || isAddingUsers"
                                    class="px-4 py-2 text-sm font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    <UIcon v-if="isAddingUsers" name="heroicons-arrow-path" class="w-4 h-4 animate-spin mr-1" />
                                    Add Users
                                </button>
                            </div>
                        </div>
                    </UModal>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue';
import { useCan } from '~/composables/usePermissions';
const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth();
const { organization } = useOrganization();
const { isExcel } = useExcel();
definePageMeta({ auth: true });

const toast = useToast();

const route = useRoute();
const ds_id = route.params.id;

const integration = ref<any>(null);

const integrationName = ref('');

const testConnectionStatus = ref<any>(null);
const editingCredentials = ref(false);

const activeTab = ref('main');

const schema = ref<any>(null);
const simpleSchema = ref<any>(null);

const isRefreshing = ref(false);

watch(testConnectionStatus, (newStatus) => {
    if (!newStatus?.success) {
        activeTab.value = 'configuration';
    }
}, { immediate: true });

const editingStarterIndex = ref(-1);

const expandedTables = ref<Record<string, boolean>>({});

const configFields = ref<any[]>([]);
const credentialFields = ref<any[]>([]);
const configFormData = reactive<any>({});
const credentialsFormData = reactive<any>({});

const generatingItem = ref<string | null>(null);

const showGitModal = ref(false);
const showUserModal = ref(false);

const metadata_resources = ref<any>({});
const dataSourceMembers = ref<any[]>([]);
const organizationUsers = ref<any[]>([]);
const selectedUsers = ref<any[]>([]);
const isAddingUsers = ref(false);
const isLoading = ref(true);

const expandedResources = ref<Record<string, boolean>>({});

const schemaExpanded = ref(true);
const dataModelsExpanded = ref(true);

// Permission checks
const canUpdateDataSource = computed(() => {
    return useCan('update_data_source');
});
const showVisibilityConfirm = ref(false);
const pendingVisibilityChange = ref(false);

const availableUsers = computed(() => {
    if (!organizationUsers.value) return [];
    
    // Filter out users who already have access
    const memberUserIds = dataSourceMembers.value.map(m => m.principal_id);
    
    return organizationUsers.value
        .filter(user => user.id && !memberUserIds.includes(user.id))
        .map(user => ({
            user_id: user.id,
            display_name: user.name || user.email || 'Unknown User'
        }));
});

async function testConnection() {
    const response = await useMyFetch(`/data_sources/${ds_id}/test_connection`, {
        method: 'GET',
    });
    testConnectionStatus.value = response.data.value;
    if (!testConnectionStatus.value?.success) {
        activeTab.value = 'configuration';
        toast.add({
            title: 'Failed to connect',
            description: testConnectionStatus.value.message,
            color: 'red'
        });
    }
    else {
        activeTab.value = 'main';
    }
}

async function refreshSchema() {
    isRefreshing.value = true;
    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/refresh_schema`, {
            method: 'GET',
        });
        schema.value = response.data.value;
    } finally {
        isRefreshing.value = false;
    }
}

async function fetchMetadataResources() {
    const response = await useMyFetch(`/data_sources/${ds_id}/metadata_resources`, {
        method: 'GET',
    });
    metadata_resources.value = response.data.value;
}

async function fetchSchema() {
    const response = await useMyFetch(`/data_sources/${ds_id}/full_schema`, {
        method: 'GET',
    });
    schema.value = response.data.value;
}

async function fetchSimpleSchema() {
    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/schema`, {
            method: 'GET',
        });
        simpleSchema.value = response.data.value;
    } catch (error) {
        console.error('Failed to fetch simple schema:', error);
        simpleSchema.value = null;
    }
}

async function updateTableStatus() {
    if (!schema.value) {
        toast.add({
            title: 'Error',
            description: 'No schema data available to update',
            color: 'red'
        });
        return;
    }

    try {
        // Add datasource_id and ensure pks/fks are arrays
        const tablesWithDatasourceId = schema.value.map(table => ({
            ...table,
            datasource_id: ds_id,
            pks: table.pks || [],
            fks: table.fks || []
        }));

        const response = await useMyFetch(`/data_sources/${ds_id}/update_schema`, {
            method: 'PUT',
            body: tablesWithDatasourceId
        });

        if (response.status.value === "success") {
            schema.value = response.data.value;
            toast.add({
                title: 'Success',
                description: 'Table status updated successfully',
                color: 'green'
            });
            
            // Refresh the schema and test connection after update
            await fetchSchema();
            await testConnection();
        } else {
            throw new Error('Failed to update table status');
        }
    } catch (error) {
        console.error('Failed to update table status:', error);
        toast.add({
            title: 'Error',
            description: 'Failed to update table status',
            color: 'red'
        });
    }
}

async function generateItem(item: string) {
    generatingItem.value = item;
    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/generate_items`, {
            method: 'GET',
            params: { item }
        });
        if (response.status.value == "success") {
            integration.value[item] = response.data.value[item];
        } else {
            console.error('Failed to generate item');
        }
    } finally {
        generatingItem.value = null;
    }
}

async function fetchIntegration() {
    console.log('Fetching integration for ds_id:', ds_id);
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'GET',
    });
    console.log('Response status:', response.status.value);
    console.log('Response data:', response.data.value);
    if (response.status.value == "success") {
        integration.value = response.data.value;
        integrationName.value = integration.value.name;
        console.log('Integration set successfully:', integration.value);
        isLoading.value = false;
    } else {
        console.error('Failed to fetch integration - status:', response.status.value);
        console.error('Error response:', response);
        isLoading.value = false;
    }
}

async function updateIntegrationMain() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            description: integration.value.description,
            conversation_starters: integration.value.conversation_starters
        }
    });
    if (response.status.value == "success") {
        integration.value = response.data.value;
        toast.add({
            title: 'Integration updated',
            description: 'Integration updated successfully',
            color: 'green'
        });
    } else {
        console.error('Failed to update integration context');
        toast.add({
            title: 'Failed to update integration',
            description: 'Failed to update integration',
            color: 'red'
        });
    }
}


async function updateIntegrationContext() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            context: integration.value.context
        }
    });
    if (response.status.value == "success") {
        integration.value = response.data.value;
        toast.add({
            title: 'AI Context updated',
            description: 'AI Context updated successfully',
            color: 'green'
        });
    } else {
        console.error('Failed to update integration context');
        toast.add({
            title: 'Failed to update AI Context',
            description: 'Failed to update AI Context',
            color: 'red'
        });
    }
}

function handleDelete() {
    if (confirm('Are you sure you want to delete this integration?')) {
        useMyFetch(`/data_sources/${ds_id}`, {
            method: 'DELETE',
        }).then(() => {
            navigateTo('/integrations');
        });
    }
}

function addConversationStarter() {
    if (!integration.value.conversation_starters) {
        integration.value.conversation_starters = [];
    }
    integration.value.conversation_starters.push('');
}

function editConversationStarter(index: number) {
    editingStarterIndex.value = index;
}

function saveConversationStarter(index: number) {
    editingStarterIndex.value = -1;
}

function deleteConversationStarter(index: number) {
    if (confirm('Are you sure you want to delete this conversation starter?')) {
        integration.value.conversation_starters.splice(index, 1);
    }
}

function toggleTable(table: any) {
    expandedTables.value[table.name] = !expandedTables.value[table.name];
}

function isPasswordField(fieldName: string) {
    return fieldName.toLowerCase().includes('password') || 
           fieldName.toLowerCase().includes('secret') || 
           fieldName.toLowerCase().includes('token');
}

async function getConfigFields() {
    if (!integration.value || !integration.value.type) {
        console.log('Skipping getConfigFields - integration not loaded yet');
        return;
    }
    
    const response = await useMyFetch(`/data_sources/${integration.value.type}/fields`, {
        method: 'GET',
    });
    
    const schema = response.data.value;
    
    if (schema.config?.properties) {
        configFields.value = Object.entries(schema.config.properties).map(([field_name, schema]) => ({
            field_name,
            ...schema
        }));
    }

    if (schema.credentials?.properties) {
        credentialFields.value = Object.entries(schema.credentials.properties).map(([field_name, schema]) => ({
            field_name,
            ...schema
        }));
    }

    if (integration.value?.config) {
        const config = typeof integration.value.config === 'string' 
            ? JSON.parse(integration.value.config) 
            : integration.value.config;
            
        Object.keys(config).forEach(key => {
            configFormData[key] = config[key];
        });
    }

    if (integration.value?.credentials) {
        const credentials = typeof integration.value.credentials === 'string'
            ? JSON.parse(integration.value.credentials)
            : integration.value.credentials;
            
        Object.keys(credentials).forEach(key => {
            credentialsFormData[key] = credentials[key];
        });
    }
}

async function handleConfigUpdate() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            name: integration.value.name,
            config: configFormData,
            credentials: credentialsFormData
        }
    });

    if (response.status.value === "success") {
        integration.value = response.data.value;
        toast.add({
            title: `${integration.value.name} configuration updated`,
            description: 'Configuration updated successfully',
            color: 'green'
        })

        await testConnection();
        
    } else {
        toast.add({
            title: 'Failed to update integration',
            description: 'Failed to update integration',
            color: 'red'
        });
    }
}

async function handleGitModalClose(value: boolean) {
    if (!value) {  // When modal is closed
        await fetchIntegration();
    }
}

function toggleResource(resource: any) {
    expandedResources.value[resource.id] = !expandedResources.value[resource.id];
}

async function updateResourceStatus() {
    if (!metadata_resources.value.resources || metadata_resources.value.resources.length === 0) {
        toast.add({
            title: 'Error',
            description: 'No resources available to update',
            color: 'red'
        });
        return;
    }

    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/update_metadata_resources`, {
            method: 'PUT',
            body: metadata_resources.value.resources  // Send the resources array directly
        });

        if (response.status.value === "success") {
            metadata_resources.value = response.data.value;
            toast.add({
                title: 'Success',
                description: 'Resources updated successfully',
                color: 'green'
            });
            
            // Refresh the resources after update
            await fetchMetadataResources();
        } else {
            throw new Error('Failed to update resources');
        }
    } catch (error) {
        console.error('Failed to update resources:', error);
        toast.add({
            title: 'Error',
            description: 'Failed to update resources',
            color: 'red'
        });
    }
}

async function updateSchemaAndResources() {
    // First update table status
    if (schema.value && schema.value.length > 0) {
        try {
            // Add datasource_id and ensure pks/fks are arrays
            const tablesWithDatasourceId = schema.value.map(table => ({
                ...table,
                datasource_id: ds_id,
                pks: table.pks || [],
                fks: table.fks || []
            }));

            const response = await useMyFetch(`/data_sources/${ds_id}/update_schema`, {
                method: 'PUT',
                body: tablesWithDatasourceId
            });

            if (response.status.value === "success") {
                schema.value = response.data.value;
            } else {
                throw new Error('Failed to update table status');
            }
        } catch (error) {
            console.error('Failed to update table status:', error);
            toast.add({
                title: 'Error',
                description: 'Failed to update table status',
                color: 'red'
            });
            return;
        }
    }

    // Then update resources
    if (metadata_resources.value && metadata_resources.value.resources && metadata_resources.value.resources.length > 0) {
        try {
            const response = await useMyFetch(`/data_sources/${ds_id}/update_metadata_resources`, {
                method: 'PUT',
                body: metadata_resources.value.resources  // Send the resources array directly
            });

            if (response.status.value === "success") {
                metadata_resources.value.resources = response.data.value;
            } else {
                throw new Error('Failed to update resources');
            }
        } catch (error) {
            console.error('Failed to update resources:', error);
            toast.add({
                title: 'Error',
                description: 'Failed to update resources',
                color: 'red'
            });
            return;
        }
    }

    toast.add({
        title: 'Success',
        description: 'Schema and resources updated successfully',
        color: 'green'
    });
    
    // Refresh data after update
    await fetchSchema();
    await fetchMetadataResources();
}

async function fetchDataSourceMembers() {
    if (!integration.value || integration.value.is_public) return;
    
    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/members`, {
            method: 'GET',
        });
        if (response.status.value === "success") {
            dataSourceMembers.value = response.data.value;
        }
    } catch (error) {
        console.error('Failed to fetch data source members:', error);
    }
}

async function fetchOrganizationUsers() {
    try {
        const response = await useMyFetch(`/organization/members`, {
            method: 'GET',
        });
        if (response.status.value === "success") {
            organizationUsers.value = response.data.value;
        }
    } catch (error) {
        console.error('Failed to fetch organization users:', error);
    }
}

function getUserDisplayName(userId: string) {
    const user = organizationUsers.value.find(u => u.id === userId);
    return user ? user.name || user.email || 'Unknown User' : 'Unknown User';
}

async function removeMember(userId: string) {
    if (!confirm('Are you sure you want to remove this user\'s access?')) return;
    
    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/members/${userId}`, {
            method: 'DELETE',
        });
        
        if (response.status.value === "success") {
            toast.add({
                title: 'Success',
                description: 'User access removed successfully',
                color: 'green'
            });
            await fetchDataSourceMembers();
        } else {
            throw new Error('Failed to remove user access');
        }
    } catch (error) {
        console.error('Failed to remove member:', error);
        toast.add({
            title: 'Error',
            description: 'Failed to remove user access',
            color: 'red'
        });
    }
}

function removeSelectedUser(userId: string) {
    selectedUsers.value = selectedUsers.value.filter(id => id !== userId);
}

async function addSelectedUsers() {
    if (selectedUsers.value.length === 0) return;
    
    isAddingUsers.value = true;
    
    try {
        const promises = selectedUsers.value.map(userId => 
            useMyFetch(`/data_sources/${ds_id}/members`, {
                method: 'POST',
                body: {
                    principal_type: 'user',
                    principal_id: userId
                }
            })
        );
        
        await Promise.all(promises);
        
        toast.add({
            title: 'Success',
            description: `Added ${selectedUsers.value.length} user(s) successfully`,
            color: 'green'
        });
        
        // Reset and refresh
        selectedUsers.value = [];
        showUserModal.value = false;
        await fetchDataSourceMembers();
        
    } catch (error) {
        console.error('Failed to add users:', error);
        toast.add({
            title: 'Error',
            description: 'Failed to add some users',
            color: 'red'
        });
    } finally {
        isAddingUsers.value = false;
    }
}

function debugAddUsers() {
    console.log('Add Users button clicked!');
    console.log('integration.is_public:', integration.value?.is_public);
    console.log('showUserModal before:', showUserModal.value);
    showUserModal.value = true;
    console.log('showUserModal after:', showUserModal.value);
}

function handleVisibilityToggleClick() {
    // Don't change the value immediately, just show confirmation
    pendingVisibilityChange.value = !integration.value.is_public;
    showVisibilityConfirm.value = true;
}

async function confirmVisibilityChange() {
    showVisibilityConfirm.value = false;
    
    // Save the visibility change with the pending value
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            is_public: pendingVisibilityChange.value
        }
    });

    if (response.status.value === "success") {
        integration.value = response.data.value;
        toast.add({
            title: 'Visibility updated',
            description: `Data source is now ${integration.value.is_public ? 'public' : 'private'}`,
            color: 'green'
        });
        
        // Refresh membership data since visibility affects it
        await fetchDataSourceMembers();
    } else {
        toast.add({
            title: 'Failed to update visibility',
            description: 'Please try again',
            color: 'red'
        });
    }
    
    // Reset pending change
    pendingVisibilityChange.value = false;
}

function cancelVisibilityChange() {
    showVisibilityConfirm.value = false;
    pendingVisibilityChange.value = false;
}

onMounted(async () => {
    nextTick(async () => {
        try {
            console.log('Starting onMounted...');
            await fetchIntegration();
            console.log('fetchIntegration completed');
            await testConnection();
            console.log('testConnection completed');
            
            // Fetch appropriate schema based on permissions
            if (canUpdateDataSource.value) {
                await fetchSchema();
                console.log('fetchSchema completed');
            } else {
                await fetchSimpleSchema();
                console.log('fetchSimpleSchema completed');
            }
            
            await fetchMetadataResources();
            console.log('fetchMetadataResources completed');
            await getConfigFields();
            console.log('getConfigFields completed');
            await fetchOrganizationUsers();
            console.log('fetchOrganizationUsers completed');
            await fetchDataSourceMembers();
            console.log('fetchDataSourceMembers completed');
            console.log('All onMounted functions completed');
            isLoading.value = false;
        } catch (error) {
            console.error('Error in onMounted:', error);
            isLoading.value = false;
        }
    });
});
</script>