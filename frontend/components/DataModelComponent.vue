<template>
<div class="relative overflow-x-auto px-5 py-5">
    <table class="w-full text-sm text-left rtl:text-right text-gray-500 dark:text-gray-400 border border-gray-200 overflow-hidden">
        <thead class="text-xs text-gray-700 uppercase dark:text-gray-400">
            <tr>
                <th scope="col" class="px-2 py-2 bg-gray-50 dark:bg-gray-800 rounded-tl">
                    Column name
                </th>
                <th scope="col" class="px-2 py-2">
                    Type
                </th>
                <th scope="col" class="px-2 py-2">
                    Data Source 
                </th>
                <th scope="col" class="px-2 py-2">
                    Description
                </th>
            </tr>
        </thead>
        <tbody>
            <tr class="border-b border-gray-200 dark:border-gray-700" 
            v-for="(column, index) in data_model.columns"
            :key="index"
            >
                <th scope="row" class="text-xs px-2 py-2 font-medium text-gray-900 whitespace-nowrap bg-gray-50 dark:text-white dark:bg-gray-800" :class="{ 'rounded-bl': index === data_model.columns.length - 1 }">
                    {{ column.generated_column_name }}
                </th>
                <td class="text-xs px-2 py-2">
                    {{ column.data_type }}
                </td>
                <td class="text-xs px-2 py-2"> 
                    <DataSourceIcon :type="column.source.data_source.type" class="w-3 h-3 inline" />
                    {{ column.source.data_source.name }}<br />
                    {{ column.source.relationship }}
                </td>
                <td class="text-xs px-2 py-2" :class="{ 'rounded-br': index === data_model.columns.length - 1 }">
                    {{ column.description }}
                </td>
            </tr>
        </tbody>
    </table>
</div>

</template>

<script setup lang="ts">
const props = defineProps<{
  data_model: any
}>()
</script>