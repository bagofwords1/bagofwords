<template>
    <div class="">
        <USelectMenu 
            searchable 
            searchable-placeholder="Search a column.." 
            class="w-full" 
            placeholder="Columns"
            :options="columnsSuggestions" 
            :ui-menu="{ width: 'min-w-max' }" 
            value-attribute="title"
            option-attribute="title" 
            v-model="selectedColumn"
        >
            <UButton class="text-black bg-transparent font-regular text-sm flex-1 border-none hover:bg-gray-50">
                <UIcon name="heroicons-view-columns" /> Columns
            </UButton>

            <template #option="{ option: column }">
                <div class="text-sm">
                    <span>{{ column.title }}</span>
                </div>
            </template>
        </USelectMenu>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';

const selectedColumn = ref({ title: '', description: '', source: '' });

const columnsSuggestions = ref([
    {
        title: "Customer Name",
        description: "The name of the customer who made the purchase.",
        source: "Customers"
    },
    {
        title: "Product Name",
        description: "The name of the product purchased.",
        source: "Products"
    },
    {
        title: "Order Date",
        description: "The date when the order was placed.",
        source: "Orders"
    },
    {
        title: "Order Quantity",
        description: "The number of units of the product ordered.",
        source: "Orders"
    },
    {
        title: "Order Total",
        description: "The total amount of the order.",
        source: "Orders"
    }
]);
</script>