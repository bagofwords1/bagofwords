<template>
    <div class="relative flex-grow">
      <div
        ref="inputRef"
        contenteditable="true"
        class="w-full text-sm border-none border-gray-200 outline-none rounded-md p-2 h-20 overflow-y-auto"
        :placeholder="props.placeholder"
        @input="handleInput"
        @keydown="handleKeydown"
        @paste.prevent="handlePaste"
      ></div>
      <div v-if="showDropdown" 
           :class="[
             'absolute z-10 w-72 max-h-96 overflow-y-auto text-xs bg-white border border-gray-200 rounded-lg shadow-lg pb-2 bottom-full mb-2'
           ]"
           :style="{ left: '0px' }">
        <div v-if="isItemCardView" class="p-3">
          <div class="flex items-center justify-between mb-3">
            <div class="flex items-center">
              <button @click="closeItemCard" class="mr-2 text-gray-500 hover:bg-gray-200 rounded-md p-1">
                <Icon name="heroicons-chevron-left" class="w-4 h-4" />
              </button>
              <span @click="selectItem(expandedItem, expandedItem.category)" class=" hover font-semibold">
                {{ expandedItem.name || expandedItem.title || expandedItem.filename }}
              </span>
            </div>
          </div>
          
          <div class="mb-3">
            <DataSourceCardComponent v-if="expandedItem.category === 'DATA SOURCES'" :data_source="expandedItem" />
            <MemoryCardComponent v-else-if="expandedItem.category === 'MEMORY'" :memory="expandedItem" />
            <FileCardComponent v-else-if="expandedItem.category === 'FILES'" :file="expandedItem" />
          </div>

          <div v-if="expandedItem.items?.length" class="border-t pt-2">
            <div class="font-semibold mb-2">Available Fields</div>
            <div v-for="subItem in expandedItem.items" 
                 :key="subItem.name"
                 class="px-2 py-1 hover:bg-gray-100 cursor-pointer"
                 @click="selectItem(subItem, 'DATA SOURCES')">
              <div class="flex items-center">
                <Icon :name="subItem.icon || 'heroicons-table-cells'" class="w-4 h-4 mr-2" />
                <span>{{ subItem.name }}</span>
              </div>
            </div>
          </div>
        </div>

        <div v-else>
          <div v-for="(category, categoryIndex) in filteredCategories" :key="category.name">
            <div class="px-3 py-3 font-semibold text-xs text-gray-700">{{ category.name }}</div>
            <div v-for="(item, itemIndex) in category.items" 
                 :key="item.name" 
                 :class="[
                   'px-3 py-1 hover:bg-gray-100 cursor-pointer',
                   { 'bg-blue-100': selectedIndex === getCumulativeIndex(categoryIndex, itemIndex) }
                 ]">
              <div class="flex items-center justify-between w-full">
                <div class="flex items-center w-full" @click="selectItem(item, category.name)">
                  <span v-if="category.name === 'DATA SOURCES' || category.name === 'FILES'">
                    <DataSourceIcon :type="item.type" class="w-3 mr-2" v-if="item.type" />
                    <span v-else-if="category.name === 'FILES'">
                      <DataSourceIcon type="excel" class="w-3 mr-2" v-if="item.filename.endsWith('.xlsx') || item.filename.endsWith('.xls')" />
                      <DataSourceIcon type="document" class="w-3 mr-2" v-else-if="item.filename.endsWith('.pdf')" />
                    </span>
                  </span>
                  <span>
                    {{ item.name || item.title || item.filename }}
                  </span>
                </div>
                <div v-if="['DATA SOURCES', 'MEMORY', 'FILES'].includes(category.name)" 
                     @click="expandItem(item, category.name)" 
                     class="text-gray-500 hover:bg-gray-200 rounded-md p-1">
                  <Icon name="heroicons-chevron-right" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </template>
  
  <script setup lang="ts">
  import { ref, computed, onMounted, onUnmounted } from 'vue';
  import DataSourceCardComponent from '~/components/DataSourceCardComponent.vue';
  import MemoryCardComponent from '~/components/MemoryCardComponent.vue';
  import FileCardComponent from '~/components/FileCardComponent.vue';
  import DataSourceIcon from '~/components/DataSourceIcon.vue';

  const props = defineProps({
    categories: {
      type: Array,
      required: true
    },
    modelValue: {
      type: String,
      default: ''
    },
    placeholder: {
      type: String,
      default: 'Enter text'
    }
  });
  
  const emit = defineEmits(['update:modelValue', 'mentionsUpdated', 'submit-content']);
  
  const inputRef = ref<HTMLDivElement | null>(null);
  const textContent = ref('');
  const showDropdown = ref(false);
  const selectedIndex = ref(-1);
  const currentMentionStartIndex = ref(-1);
  const expandedItem = ref(null);
  const isItemCardView = ref(false);
  
  const filteredCategories = computed(() => {
    if (currentMentionStartIndex.value === -1) return [];
    const mentionText = textContent.value.slice(currentMentionStartIndex.value + 1).toLowerCase();
    return props.categories.map(category => ({
      ...category,
      items: category.items.filter(item => {
        const searchField = category.name === 'MEMORY' ? item.title : item.name || item.filename;
        return searchField && searchField.toLowerCase().includes(mentionText);
      })
    })).filter(category => category.items.length > 0);
  });
  
  const getCumulativeIndex = computed(() => {
    return (categoryIndex: number, itemIndex: number): number => {
      let index = 0;
      for (let i = 0; i < categoryIndex; i++) {
        index += filteredCategories.value[i].items.length;
      }
      return index + itemIndex;
    };
  });
  
  const mentions = ref([
    {
      name: 'MEMORY',
      items: []
    },
    {
      name: 'FILES',
      items: []
    },
    {
      name: 'DATA SOURCES',
      items: []
    },
  ]);
  
  function getCaretPosition(element: HTMLElement): number {
    const selection = window.getSelection();
    if (selection && selection.rangeCount > 0) {
      const range = selection.getRangeAt(0);
      const preCaretRange = range.cloneRange();
      preCaretRange.selectNodeContents(element);
      preCaretRange.setEnd(range.endContainer, range.endOffset);
      return preCaretRange.toString().length;
    }
    return 0;
  }
  
  function handleInput(event: Event) {
    const target = event.target as HTMLDivElement;
    textContent.value = target.innerText;
  
    // Remove any direct text modifications inside mention spans
    const mentionSpans = target.querySelectorAll('span[data-mention]');
    mentionSpans.forEach(span => {
      if (span.childNodes.length > 1 || (span.childNodes[0] && span.childNodes[0].nodeType !== Node.TEXT_NODE)) {
        span.innerHTML = span.getAttribute('data-mention') || '';
      }
    });
  
    const cursorPosition = getCaretPosition(target);
    const textBeforeCursor = textContent.value.slice(0, cursorPosition);
    const lastAtIndex = textBeforeCursor.lastIndexOf('@');
  
    if (lastAtIndex !== -1 && !textBeforeCursor.slice(lastAtIndex + 1).includes(' ')) {
      currentMentionStartIndex.value = lastAtIndex;
      showDropdown.value = true;
    } else {
      showDropdown.value = false;
      currentMentionStartIndex.value = -1;
    }
  
    emit('update:modelValue', textContent.value);
  }

  
  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'ArrowLeft' && isItemCardView.value) {
      event.preventDefault();
      closeItemCard();
      return;
    }

    // Update the Enter key handling
    if (event.key === 'Enter' && !event.shiftKey && !showDropdown.value) {
        event.preventDefault();
        // Clear the content
        clearContent();
        // Emit the submit event with the current text content
        emit('submit-content', textContent.value);
        return;
    }

    if (event.key === '@') {
      const target = event.target as HTMLDivElement;
      currentMentionStartIndex.value = getCaretPosition(target);
      showDropdown.value = true;
      selectedIndex.value = -1;
    } else if (showDropdown.value) {
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          selectedIndex.value = (selectedIndex.value + 1) % getTotalItems();
          break;
        case 'ArrowUp':
          event.preventDefault();
          selectedIndex.value = (selectedIndex.value - 1 + getTotalItems()) % getTotalItems();
          break;
        case 'Enter':
          event.preventDefault();
          if (selectedIndex.value !== -1) {
            const selectedItem = getItemAtIndex(selectedIndex.value);
            const categoryName = getCategoryName(selectedIndex.value);

            if (selectedItem) {
              selectItem(selectedItem, categoryName);
            }
          }
          showDropdown.value = false;
          break;
        case 'ArrowRight':
          event.preventDefault();
          if (selectedIndex.value !== -1) {
            const selectedItem = getItemAtIndex(selectedIndex.value);
            const categoryName = getCategoryName(selectedIndex.value);
            if (selectedItem && ['DATA SOURCES', 'MEMORY', 'FILES'].includes(categoryName)) {
              expandItem(selectedItem, categoryName);
            }
          }
          break;
        case 'Escape':
          event.preventDefault();
          if (isItemCardView.value) {
            closeItemCard();
          } else {
            showDropdown.value = false;
          }
          break;
      }
    }
  
    if (event.key === 'Backspace' || event.key === 'Delete') {
      const mentionToDelete = findAdjacentMention(event);
      if (mentionToDelete) {
        event.preventDefault();
        deleteMention(mentionToDelete);
      }
    }
  
    // Prevent editing inside mention spans
    if (isCaretInsideMention(event)) {
      if (event.key.length === 1 || event.key === 'Backspace' || event.key === 'Delete') {
        event.preventDefault();
        if (event.key === 'Backspace' || event.key === 'Delete') {
          deleteMention(event);
        }
      }
    }
  
    // Handle left arrow key to skip over mentions
    if (event.key === 'ArrowLeft') {
      const selection = window.getSelection();
      if (selection && selection.rangeCount > 0) {
        const range = selection.getRangeAt(0);
        const startContainer = range.startContainer;
        if (startContainer.nodeType === Node.TEXT_NODE && startContainer.textContent === ' ' && startContainer.previousSibling?.classList.contains('mention')) {
          event.preventDefault();
          const newRange = document.createRange();
          newRange.setStartBefore(startContainer.previousSibling);
          newRange.collapse(true);
          selection.removeAllRanges();
          selection.addRange(newRange);
        }
      }
    }
  }
  
  function getTotalItems() {
    return filteredCategories.value.reduce((total, category) => total + category.items.length, 0);
  }
  
  function getItemAtIndex(index: number) {
    let currentIndex = 0;
    for (const category of filteredCategories.value) {
      if (index < currentIndex + category.items.length) {
        return category.items[index - currentIndex];
      }
      currentIndex += category.items.length;
    }
    return null;
  }

  function getCategoryName(index: number) {
    let currentIndex = 0;
    for (const category of filteredCategories.value) {
      if (index < currentIndex + category.items.length) {
        return category.name;
      }
      currentIndex += category.items.length;
    }
    return null;
  }

  function expandItem(item, categoryName = 'DATA SOURCES') {
    expandedItem.value = { ...item, category: categoryName };
    isItemCardView.value = true;
  }

  function closeItemCard() {
    expandedItem.value = null;
    isItemCardView.value = false;
  }

  function selectItem(item, categoryName) {
    if (currentMentionStartIndex.value !== -1 && inputRef.value) {
      const mentionNode = document.createElement('span');
      mentionNode.className = 'mention text-blue-500 bg-gray-100';
      mentionNode.setAttribute('contenteditable', 'false');
      mentionNode.setAttribute('data-mention', `@${item.name || item.title || item.filename}`);
      mentionNode.setAttribute('data-id', item.id);
      mentionNode.setAttribute('data-category', categoryName);
      mentionNode.textContent = `@${item.name || item.title || item.filename}`;
      
      // Create a new range and set it to the correct position
      const range = document.createRange();
      const { node, offset } = findTextNodeAtIndex(inputRef.value, currentMentionStartIndex.value);
      range.setStart(node, offset);
      range.setEnd(node, offset + (inputRef.value.textContent.length - currentMentionStartIndex.value));
      
      // Delete the existing mention text and insert the new mention node
      range.deleteContents();
      range.insertNode(mentionNode);
      
      // Add a space after the mention
      const spaceNode = document.createTextNode(" ");
      range.setStartAfter(mentionNode);
      range.insertNode(spaceNode);

      // Set caret position after the space
      range.setStartAfter(spaceNode);
      range.collapse(true);
      
      // Apply the changes to the selection
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      
      // Ensure the input is focused
      inputRef.value.focus();
      
      // Update textContent
      textContent.value = inputRef.value.innerText;
      emit('update:modelValue', textContent.value);
      
      // Update mentions
      const category = mentions.value.find(cat => cat.name === categoryName);
      if (category && !category.items.some(mention => mention.id === item.id)) {
        category.items.push(item);
      }

      // Emit mentionsUpdated event
      emit('mentionsUpdated', mentions.value);
    }
    currentMentionStartIndex.value = -1; 
    showDropdown.value = false;
    selectedIndex.value = -1; // Reset selected index after selection
  }
  
  // Helper function to find the text node and offset at a given index
  function findTextNodeAtIndex(element, targetIndex) {
    let currentIndex = 0;
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  
    while (walker.nextNode()) {
      const nodeLength = walker.currentNode.length;
      if (currentIndex + nodeLength > targetIndex) {
        return { node: walker.currentNode, offset: targetIndex - currentIndex };
      }
      currentIndex += nodeLength;
    }
  
    // If we've reached here, the index is at the end of the content
    const lastTextNode = walker.currentNode || element.lastChild;
    return { node: lastTextNode, offset: lastTextNode.length };
  }
  
  function isCaretInsideMention(event: KeyboardEvent): boolean {
    const selection = window.getSelection();
    if (selection && selection.rangeCount > 0) {
      const range = selection.getRangeAt(0);
      const startContainer = range.startContainer;
      const mentionElement = startContainer.parentElement?.closest('.mention');
      
      if (mentionElement) {
        // If backspace is pressed at the start of a mention, allow deletion
        if (event.key === 'Backspace' && range.startOffset === 0) {
          return false;
        }
        // If delete is pressed at the end of a mention, allow deletion
        if (event.key === 'Delete' && range.startOffset === startContainer.textContent.length) {
          return false;
        }
        return true;
      }
    }
    return false;
  }
  
  function findAdjacentMention(event: KeyboardEvent): Element | null {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return null;

    const range = selection.getRangeAt(0);
    const editableDiv = inputRef.value;
    if (!editableDiv) return null;

    // Get the current node and offset
    const currentNode = range.startContainer;
    const currentOffset = range.startOffset;

    // Check for mentions when cursor is inside the editable div
    if (currentNode === editableDiv) {
      const childNodes = Array.from(editableDiv.childNodes);
      for (let i = 0; i < childNodes.length; i++) {
        const node = childNodes[i];
        if (node.nodeType === Node.ELEMENT_NODE && (node as Element).classList.contains('mention')) {
          if (event.key === 'Backspace' && i < currentOffset) {
            return node as Element;
          } else if (event.key === 'Delete' && i === currentOffset) {
            return node as Element;
          }
        }
      }
    }

    return null;
  }
  
  function deleteMention(mentionElement: Element) {
    const itemId = mentionElement.getAttribute('data-id');
    const categoryName = mentionElement.getAttribute('data-category');

    // Remove mention from the mentions structure
    if (itemId && categoryName) {
      const category = mentions.value.find(cat => cat.name === categoryName);
      if (category) {
        const index = category.items.findIndex(mention => mention.id === itemId);
        if (index !== -1) {
          category.items.splice(index, 1);
          // Emit mentionsUpdated event
          emit('mentionsUpdated', mentions.value);
        }
      }
    }

    // Remove the mention element from the DOM
    mentionElement.remove();

    // Update textContent
    textContent.value = inputRef.value.innerText;
    emit('update:modelValue', textContent.value);

    // Set the caret position
    const selection = window.getSelection();
    const range = document.createRange();
    range.setStartAfter(mentionElement.previousSibling || inputRef.value.firstChild);
    range.collapse(true);
    selection?.removeAllRanges();
    selection?.addRange(range);
  }
  
  // Expose mentions to the parent component
  
  function clearContent() {
    if (inputRef.value) {
      inputRef.value.innerHTML = '';
      textContent.value = '';
      // Reset other states
      showDropdown.value = false;
      selectedIndex.value = -1;
      currentMentionStartIndex.value = -1;
      // Clear mentions
      mentions.value.forEach(category => {
        category.items = [];
      });
    }
  }
  
  function handlePaste(event: ClipboardEvent) {
    // Get plain text from clipboard
    const text = event.clipboardData?.getData('text/plain') ?? '';
    
    // Insert text at cursor position
    const selection = window.getSelection();
    if (selection && selection.rangeCount > 0) {
      const range = selection.getRangeAt(0);
      range.deleteContents();
      range.insertNode(document.createTextNode(text));
      
      // Move cursor to end of pasted text
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }
    
    // Trigger input handler to process the new content
    handleInput({ target: inputRef.value } as Event);
  }
  
  // Add this method to update the content
  function updateContent(content: string) {
    if (inputRef.value) {
        inputRef.value.innerText = content;
        textContent.value = content;
        emit('update:modelValue', content);
    }
  }
  
  // Expose the methods
  defineExpose({ mentions, clearContent, updateContent });
  
  </script>
  
  <style scoped>
  .mention {
    color: blue;
    background-color: #eee;
    padding: 1px 4px;
    border-radius: 3px;
    font-weight: 500;
    white-space: nowrap;
    user-select: all;
  }
  
  [contenteditable]:empty:before {
    content: attr(placeholder);
    color: #888;
    font-style: italic;
  }
  </style>

