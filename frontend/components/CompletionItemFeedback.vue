<template>
  <div class="flex items-center gap-2">
    <div class="flex items-center gap-1">
      <UButton
        :icon="userFeedback?.direction === 1 ? 'i-heroicons-hand-thumb-up-solid' : 'i-heroicons-hand-thumb-up'"
        :color="userFeedback?.direction === 1 ? 'blue' : 'gray'"
        variant="ghost"
        size="xs"
        @click="sendFeedback(1)"
        :loading="isLoading"
      />
             <span v-if="feedbackSummary?.total_upvotes && feedbackSummary.total_upvotes > 0" class="text-xs text-gray-500">
         {{ feedbackSummary.total_upvotes }}
       </span>
    </div>
    
    <div class="flex items-center gap-1">
      <UButton
        :icon="userFeedback?.direction === -1 ? 'i-heroicons-hand-thumb-down-solid' : 'i-heroicons-hand-thumb-down'"
        :color="userFeedback?.direction === -1 ? 'red' : 'gray'"
        variant="ghost"
        size="xs"
        @click="sendFeedback(-1)"
        :loading="isLoading"
      />
             <span v-if="feedbackSummary?.total_downvotes && feedbackSummary.total_downvotes > 0" class="text-xs text-gray-500">
         {{ feedbackSummary.total_downvotes }}
       </span>
    </div>
    
    <!-- Net score display (optional) -->
    <div v-if="feedbackSummary && feedbackSummary.net_score !== 0" class="text-xs text-gray-400 ml-1">
      ({{ feedbackSummary.net_score > 0 ? '+' : '' }}{{ feedbackSummary.net_score }})
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'

interface UserFeedback {
  id: string;
  direction: number;
  message?: string;
  user_id?: string;
  completion_id: string;
  organization_id: string;
  created_at: string;
  updated_at: string;
}

interface FeedbackSummary {
  completion_id: string;
  total_upvotes: number;
  total_downvotes: number;
  net_score: number;
  total_feedbacks: number;
  user_feedback?: UserFeedback;
}

const props = defineProps<{
  completion: {
    id: string;
    [key: string]: any;
  };
  feedbackScore: number;
}>();

const isLoading = ref(false);
const feedbackSummary = ref<FeedbackSummary | null>(null);
const userFeedback = computed(() => feedbackSummary.value?.user_feedback);

// Fetch feedback summary on component mount
onMounted(async () => {
  await fetchFeedbackSummary();
});

const fetchFeedbackSummary = async () => {
  try {
    const response = await useMyFetch(`/api/completions/${props.completion.id}/feedback/summary`);
    if (response.data.value) {
      feedbackSummary.value = response.data.value as FeedbackSummary;
    }
  } catch (err) {
    console.error('Failed to fetch feedback summary:', err);
  }
};

const sendFeedback = async (vote: number) => {
  if (isLoading.value) return;
  
  isLoading.value = true;
  
  try {
    const response = await useMyFetch(`/api/completions/${props.completion.id}/feedback`, {
      method: 'POST',
      body: {
        direction: vote,
        message: null
      }
    });

    if (response.status.value !== 'success') throw new Error('Failed to submit feedback');

    // Refresh feedback summary after successful submission
    await fetchFeedbackSummary();

    const toast = useToast();
    toast.add({
      title: 'Success',
      description: vote > 0 ? 'Successfully upvoted AI response' : 'Successfully downvoted AI response',
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
  } finally {
    isLoading.value = false;
  }
};
</script> 