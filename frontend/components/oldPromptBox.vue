<template>
    <div class="bg-white shadow-xl">
        <div class="flex justify-between">
            <div class="justify-start">
                <div class="">
                    <div class="h-22 rounded border px-2 py-3 border-gray-200 shadow-sm focus-within:ring-1 focus-within:ring-gray-300">
                        <div class="text-gray-300 px-2 text-xs hidden">
                            Previous Prompts<br />
                        </div>
                        <div>
                            <textarea v-model="prompt" @keyup.enter="handleFetchTable"
                                      placeholder="Follow up drill downs and charts.."
                                      class="w-[700px] rounded border-none outline-none focus:outline-none resize-none text-sm px-2 py-2 h-[48px]" />
                        </div>
                        <div class="text-xs">
                            <span class="bg-gray-100 hover:bg-gray-200 text-gray-500 text-xs font-medium me-2 px-2.5 py-2 rounded dark:bg-gray-700 dark:text-gray-300">
                                <img class="w-4 inline ml-2 mr-1" src="https://static-00.iconduck.com/assets.00/postgresql-icon-1987x2048-v2fkmdaw.png" />
                                <span class="underline">
                                    <NuxtLink to="/database">
                                        {{ db_title }}
                                    </NuxtLink>
                                </span>
                            </span>
                            <span class="bg-gray-100 hover:bg-gray-200 text-gray-500 text-xs font-medium me-2 px-2.5 py-2 rounded dark:bg-gray-700 dark:text-gray-300">
                                + Add Files
                            </span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';

const emit = defineEmits(['fetchTable']);
const prompt = ref('');

function handleFetchTable() {
    emit('fetchTable', prompt.value);
}
</script>