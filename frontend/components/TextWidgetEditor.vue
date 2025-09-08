<template>
    <div  class="p-1 pb-10 pl-3 border h-full min-h-[140px] w-full rounded-lg relative" :style="wrapperStyle">
      <div v-if="editor" class="flex items-center gap-1 border-b border-gray-200 mb-2 text-xs">
        <button
          @click="editor.chain().focus().toggleBold().run()"
          :disabled="!editor.can().chain().focus().toggleBold().run()"
          :class="{ 'is-active': editor.isActive('bold') }"
        >
          <Icon name="heroicons:bold" />
        </button>
        <button
          @click="editor.chain().focus().toggleItalic().run()"
          :disabled="!editor.can().chain().focus().toggleItalic().run()"
          :class="{ 'is-active': editor.isActive('italic') }"
        >
          <Icon name="heroicons:italic" />
        </button>

        <button
          @click="editor.chain().focus().toggleHeading({ level: 1 }).run()"
          :class="{ 'is-active': editor.isActive('heading', { level: 1 }) }"
        >
          <Icon name="heroicons:h1" />
        </button>
        <button
          @click="editor.chain().focus().toggleHeading({ level: 2 }).run()"
          :class="{ 'is-active': editor.isActive('heading', { level: 2 }) }"
        >
          <Icon name="heroicons:h2" />
        </button>
        <button
          @click="editor.chain().focus().toggleLink().run()"
          :class="{ 'is-active': editor.isActive('link') }"
        >
          <Icon name="heroicons:link" />
        </button>


      </div>
      <div class="editor-container">
        <TiptapEditorContent :editor="editor" />
      </div>
    <div class="absolute bottom-2 left-2">
        <button 
          @click="$emit('save', editor.getHTML())"
          class="text-xs bg-blue-500 hover:bg-blue-600 text-white px-2 py-1 rounded"
        >
            Save
        </button>
        <button 
          @click="$emit('cancel')"
          class="text-xs bg-gray-100 hover:bg-gray-200 text-gray-500 px-2 py-1 rounded ml-2"
        >
            Cancel
        </button>
    </div>
    </div>
  </template>
  
  <script setup lang="ts">
  import { computed, unref } from 'vue'
  import { useEditor, EditorContent as TiptapEditorContent } from '@tiptap/vue-3'
  import StarterKit from '@tiptap/starter-kit'
  import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'

  const props = defineProps({
    textWidget: {
      type: Object,
      required: false
    }
  });

  const editor = useEditor({
    content: props.textWidget?.content || "<p>Insert text here...</p>",
    extensions: [StarterKit],
  });

  const { tokens } = useDashboardTheme(null, null, null)
  const wrapperStyle = computed(() => ({
    backgroundColor: tokens.value?.cardBackground || tokens.value?.background || '#ffffff',
    color: tokens.value?.textColor || '#232323',
    borderColor: tokens.value?.cardBorder || '#e5e7eb'
  }))
  
  onBeforeUnmount(() => {
    unref(editor).destroy();
  });
  defineEmits(['save']);
  </script>
  
  <style >
    .editor-content {
        border: 1px solid #ccc;
        padding: 10px;
        outline: none;
    }

    .ProseMirror {
        height: 100%;
        width: 100%;
        font-style: normal;
        font-size: 14px;
        color:#232323;

        h1 {
          font-size: 1.2rem;
          font-weight: 600;
          margin: 1rem 0;
        }

        h2 {
          font-size: 1rem;
          font-weight: 600;
          margin: 0.8rem 0;
        }

        p {
          margin: 0.5rem 0;
        }

        a {
          color: #3b82f6; /* blue-500 */
          text-decoration: underline;
        }

        strong {
          font-weight: 600;
        }

        em {
          font-style: italic;
        }
        button {
          @apply p-1 hover:bg-gray-100 rounded-md;
      }
    }

    .ProseMirror-focused {
        outline: none !important;
    }

  

    .editor-container {
      height: calc(100% - 60px); /* Adjust based on your toolbar height */
      overflow-y: auto;
      padding-right: 8px;
      margin-bottom: 40px; /* Space for save button */
    }

    .ProseMirror {
      height: 100%;
      width: 100%;
      font-style: normal;
      min-height: 100px; /* Ensures minimum content area */
    }
</style>
