<template>
  <div class="flex items-center gap-1">
    <div class="flex items-center gap-1">
      <UButton
        :icon="userFeedback?.direction === 1 ? 'i-heroicons-hand-thumb-up-solid' : 'i-heroicons-hand-thumb-up'"
        :color="userFeedback?.direction === 1 ? 'black' : 'gray'"
        variant="ghost"
        size="xs"
        @click="sendFeedback(1)"
        :loading="isLoading"
      />
    </div>
    
    <div class="flex items-center gap-1">
      <UButton
        :icon="userFeedback?.direction === -1 ? 'i-heroicons-hand-thumb-down-solid' : 'i-heroicons-hand-thumb-down'"
        :color="userFeedback?.direction === -1 ? 'black' : 'gray'"
        variant="ghost"
        size="xs"
        @click="handleNegativeFeedback"
        :loading="isLoading"
      />
    </div>
  </div>

  <!-- Negative Feedback Modal -->
  <UModal v-model="showNegativeFeedbackModal" :ui="{ width: 'max-w-md' }">
    <div class="p-6">
      <div class="flex justify-between items-center mb-4">
        <h2 class="text-lg font-bold text-gray-900">
          What went wrong?
        </h2>
        <button @click="showNegativeFeedbackModal = false" class="text-gray-500 hover:text-gray-700">
          <Icon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>
      
      <p class="text-sm text-gray-600 mb-4">
        Help us improve by letting us know what went wrong with this response.
      </p>
      
      <UTextarea
        v-model="feedbackMessage"
        placeholder="Type more details here..."
        :rows="4"
        class="mb-4"
        :maxlength="500"
      />
      
      <div class="flex justify-end gap-2">
        <UButton
          color="gray"
          variant="ghost"
          size="xs"
          @click="showNegativeFeedbackModal = false"
        >
          Cancel
        </UButton>
        <UButton
          color="red"
          size="xs"
          @click="submitNegativeFeedback"
          :loading="isSubmittingNegativeFeedback"
        >
          Submit Feedback
        </UButton>
      </div>
    </div>
  </UModal>
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

// Negative feedback modal state
const showNegativeFeedbackModal = ref(false);
const feedbackMessage = ref('');
const isSubmittingNegativeFeedback = ref(false);

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

const handleNegativeFeedback = () => {
  // If user already has negative feedback, just toggle it off
  if (userFeedback.value?.direction === -1) {
    sendFeedback(-1);
  } else {
    // Show modal for new negative feedback
    feedbackMessage.value = '';
    showNegativeFeedbackModal.value = true;
  }
};

const submitNegativeFeedback = async () => {
  if (isSubmittingNegativeFeedback.value) return;
  
  isSubmittingNegativeFeedback.value = true;
  
  try {
    const response = await useMyFetch(`/api/completions/${props.completion.id}/feedback`, {
      method: 'POST',
      body: {
        direction: -1,
        message: feedbackMessage.value.trim() || null
      }
    });

    if (response.status.value !== 'success') throw new Error('Failed to submit feedback');

    // Close modal and refresh feedback summary
    showNegativeFeedbackModal.value = false;
    await fetchFeedbackSummary();

    const toast = useToast();
    toast.add({
      title: 'Success',
      description: 'Thank you for your feedback!',
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
    isSubmittingNegativeFeedback.value = false;
  }
};
</script> 