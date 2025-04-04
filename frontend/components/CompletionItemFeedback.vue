<template>
  <div class="flex items-center gap-1">
    <UButton
      icon="i-heroicons-hand-thumb-up"
      color="gray"
      variant="ghost"
      size="xs"
      @click="sendFeedback(completion.id, 1)"
    />
    <UButton
      icon="i-heroicons-hand-thumb-down"
      color="gray"
      variant="ghost"
      size="xs"
      @click="sendFeedback(completion.id, -1)"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  completion: {
    id: string;
    [key: string]: any;
  };
  feedbackScore: number;
}>();

const sendFeedback = async (completionId: string, vote: number) => {
  try {
    const response = await useMyFetch(`/api/completions/${completionId}/feedback?vote=${vote}`, {
      method: 'POST',
    });

    if (response.status.value !== 'success') throw new Error('Failed to submit feedback');

    const toast = useToast();
    toast.add({
      title: 'Success',
      description: vote > 0 ? 'Thanks for the positive feedback!' : 'Thanks for your feedback',
      color: 'green',
      timeout: 3000
    });
  } catch (err) {
    const toast = useToast();
    toast.add({
      title: 'Error',
      description: 'Failed to submit feedback',
      color: 'red',
      timeout: 5000,
      icon: 'i-heroicons-exclamation-circle'
    });
  }
};
</script> 