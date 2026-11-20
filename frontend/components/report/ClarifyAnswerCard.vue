<template>
  <!-- The user's answers to a clarify form. The agent still gets the plain
       "Q: … A: …" prompt; chat shows the question as a quiet caption and the
       answer as the message. -->
  <div class="flex flex-col gap-3 min-w-0" data-testid="clarify-answer-card">
    <div v-for="(pair, i) in answers" :key="i" class="flex flex-col gap-0.5 min-w-0">
      <p class="text-chat-2 text-ink-muted break-words" dir="auto">{{ pair.question }}</p>
      <p v-if="pair.answers.length === 1" class="text-chat text-ink whitespace-pre-wrap break-words" dir="auto">{{ pair.answers[0] }}</p>
      <ul v-else class="flex flex-col gap-0.5">
        <li v-for="(a, j) in pair.answers" :key="j" class="flex items-start gap-1.5 text-chat text-ink min-w-0">
          <Icon name="heroicons-check" class="w-3.5 h-3.5 mt-1 flex-shrink-0 text-ink-muted" />
          <span class="break-words min-w-0" dir="auto">{{ a }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { ClarifyAnswer } from '~/utils/clarifyPrompt'

defineProps<{ answers: ClarifyAnswer[] }>()
</script>
