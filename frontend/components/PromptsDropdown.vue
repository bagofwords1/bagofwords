<template>
<div class="">
    <USelectMenu searchable searchable-placeholder="Search a prompt.." class="w-full text-xs"
        placeholder="Prompts" :options="promptsSuggestions" 
        :ui-menu="{ width: 'min-w-max' }"
        value-attribute="prompt" option-attribute="title"
        v-model="selectedPrompt"
        @update:modelValue="handleSelect">

        <UButton
            class="text-black bg-transparent font-regular text-sm flex-1 border-none hover:bg-gray-50 ">
            <UIcon name="heroicons-cube" /> Prompts
        </UButton>

        <template #option="{ option: prompt }">
            <div class="text-sm">
            <span>{{  prompt.title }}</span>
        </div>
        </template>

    </USelectMenu>
</div>
</template>

<script setup lang="ts">
import { ref, defineEmits } from 'vue';
const selectedPrompt = ref({ title: '', prompt: '' });
const emit = defineEmits(['select']);

function handleSelect(selectedOption) {
    emit('select', selectedOption);
}

const promptsSuggestions = ref([
    {
        title: "Monthly Financial Summary",
        prompt: "Generate a detailed financial summary for the last month, including revenue, expenses, and net profit across all departments."
    },
    {
        title: "Budget vs. Actuals",
        prompt: "Compare the budgeted financial figures to the actuals for the current fiscal quarter, highlighting any significant variances."
    },
    {
        title: "Cash Flow Analysis",
        prompt: "Provide a cash flow analysis for the past six months, breaking down cash inflows and outflows by category."
    },
    {
        title: "Accounts Receivable Aging",
        prompt: "Show an aging report of accounts receivable, categorized by customer and payment status."
    },
    {
        title: "Inventory Turnover",
        prompt: "Calculate the inventory turnover ratio for the last year, and provide insights on the slow-moving and fast-moving inventory items."
    }
]);

</script>