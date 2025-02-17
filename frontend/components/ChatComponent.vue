<template>
    <div class="h-full flex flex-col">
        <div class="flex-1 flex flex-col justify-end p-0 overflow-y-scroll" id="messages">
            <div v-for="message in messageChain" :key="message.time" class="w-full text-sm mb-2">
                <div class="bg-white p-1 rounded shadow text-gray-600">
                    <div class="flex justify-between">
                        <span v-if="message.writer == 'user'" class="font-bold text-xs text-gray-700">Me</span>
                        <span v-if="message.writer == 'system'" class="font-bold text-xs text-gray-700">AI</span>
                        <span class="text-xs text-gray-400">{{ message.time }}</span>
                    </div>
                    {{ message.message }}

                    <span class='block mt-4 font-bold' v-if="message.metrics.count">{{ message.metrics.count }}</span>
                    <highchart class="mt-4" v-if="message.metrics.chartOptions" :options="message.metrics.chartOptions" />
                    <UTooltip v-if="message.metrics.chartOptions" text="Add to dashboard">
                        <button class="text-2xl hover:text-blue-400"  @click="sendMetric(message.metrics)">
                            <i class="i-heroicons-chart-pie"></i>
                        </button>
                    </UTooltip>
                    <UTooltip text="Save as Metric" v-if="message.writer == 'system'">
                        <button class="text-2xl ml-2 hover:text-blue-400"  @click="">
                            <i class="i-heroicons-bookmark-square"></i>
                        </button>
                    </UTooltip>
                </div>
            </div>
        </div>
        <div class="p-1 flex items-center">
            <form @submit.prevent="submit" class="flex w-full border-t pt-3">
                <input type="text" placeholder="Type your message..." class="flex-1 p-2 border text-xs border-gray-200 rounded mr-0" v-model="newMessage" />
                <button type="submit" class="text-gray-400 px-1 py-1 text-xs rounded">
                    Send
                </button>
            </form>
        </div>
    </div>
</template>

<script setup lang="ts">
    import { ref } from 'vue';

    const newMessage = ref('');

    const messageChain = ref([
    ]);
    const emit = defineEmits(['metric-sent']);

    async function submit() {

        messageChain.value.push({ message: newMessage.value, writer: "user", time: new Date().toLocaleTimeString(), metrics: {} });

        const freezeNewMessage = newMessage.value

        newMessage.value = ''; // Clear the input

        try {
            const response = await useMyFetch('/completion', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    messages: messageChain.value,
                    new_message: freezeNewMessage
                }),
            });

            if (!response.ok) {
            }

            const data = await response.data;
            // Handle response data here

            // Optionally, update messageChain with new message
            messageChain.value.push({ message: data.value.completion, writer: "system", time: new Date().toLocaleTimeString(), metrics: data.value.metrics });
        } catch (error) {
            console.error('Error during sending message:', error);
        }
    }

    function sendMetric(metric: any) {
  emit('metric-sent', metric);
}
</script>

<style>
           .bg-pastel-gradient {
            background: linear-gradient(135deg, #FFD1DC, #FAE1DD, #C7EAE4, #D4D3E9);
        }
</style>