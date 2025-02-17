<template>
    <div>
        <span class="font-semibold block mb-1">{{ file.filename }}</span>
        <span class="text-gray-500 block mb-1">
            Uploaded
            {{ new Date(file.created_at).toLocaleDateString('en-US', { 
                year: 'numeric',
                month: 'short',
                day: 'numeric'
            }) }}
        </span>
        <span class="text-gray-500 block mb-1"
            v-if="file.content_type.includes('sheet')"
        >
            <Icon name="heroicons-document-text" /> Excel
        </span>
        <span v-else-if="file.content_type.includes('image')">
            <Icon name="heroicons-photo" /> Image
        </span>
        <span v-else>
            <Icon name="heroicons-document" /> Document
        </span>
    </div>

</template>

<script setup lang="ts">
const props = defineProps<{
    file: any
}>()
</script>