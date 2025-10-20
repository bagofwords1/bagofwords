<template>
  <div class="relative w-full">
    <div
      ref="inputRef"
      contenteditable="true"
      class="w-full text-sm outline-none resize-none bg-transparent text-gray-900 placeholder-gray-400 min-h-[40px] text-left"
      :style="{ maxHeight: maxHeight }"
      @input="handleInput"
      @keydown="handleKeydown"
      @paste.prevent="handlePaste"
      @click="handleClick"
    ></div>
    
    <!-- Dropdown for mentions -->
    <div 
      v-if="showDropdown" 
      ref="dropdownRef"
      class="absolute z-50 w-80 max-h-80 overflow-y-auto bg-white border border-gray-200 rounded-md shadow-md text-left"
      :style="dropdownStyle"
    >
      <!-- Loading state -->
      <div v-if="isLoadingMentions" class="p-2 text-left text-xs text-gray-500 flex items-center gap-2">
        <Spinner class="w-3 h-3" />
        <span>Loading…</span>
      </div>
      
      <!-- Search results view -->
      <div v-else-if="!expandedItem" class="py-2">
        <div v-for="(category, categoryIndex) in filteredCategories" :key="category.name">
          <div class="px-2 py-1 text-[12px] font-medium text-gray-500">{{ category.label }}</div>
          <div 
            v-for="(item, itemIndex) in category.items" 
            :key="item.id"
            :class="[
              'group px-2 py-1 cursor-pointer flex items-center justify-between hover:bg-gray-50',
              { 'bg-blue-50': selectedIndex === getCumulativeIndex(categoryIndex, itemIndex) }
            ]"
            :data-idx="getCumulativeIndex(categoryIndex, itemIndex)"
            @click="selectItem(item, category.name)"
          >
            <div class="flex items-center space-x-2 flex-1 min-w-0">
              <DataSourceIcon v-if="category.name === 'data_sources' || category.name === 'tables'" :type="item.icon_type" class="w-3.5 h-3.5 flex-shrink-0" />
              <Icon v-if="category.name === 'tables'" name="heroicons-table-cells" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
              <Icon v-else-if="category.name === 'files'" name="heroicons-document" class="w-3.5 flex-shrink-0 text-gray-500" />
              <Icon v-else-if="category.name === 'entities'" :name="item.entity_type === 'metric' ? 'heroicons-chart-bar' : 'heroicons-cube'" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
              
              <div class="flex flex-col min-w-0 flex-1">
                <span class="text-[12px] text-gray-900 truncate">{{ item.name }}</span>
              </div>
            </div>
            
            <button
              v-if="['data_sources', 'tables', 'entities'].includes(category.name)"
              @click.stop="expandItem(item, category.name)"
              class="text-gray-400 hover:text-gray-600 p-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
            >
              <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
        
        <div v-if="filteredCategories.length === 0" class="px-2 py-4 text-xs text-gray-500">
          No results found
        </div>
      </div>
      
      <!-- Expanded item detail view -->
      <div v-else class="p-2">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2 min-w-0">
            <button @click="closeItemCard" class="text-gray-500 hover:bg-gray-100 rounded p-1">
              <Icon name="heroicons-chevron-left" class="w-4 h-4" />
            </button>
            <DataSourceIcon v-if="expandedCategory === 'data_sources' || expandedCategory === 'tables'" :type="expandedItem?.icon_type" class="w-3.5 h-3.5 flex-shrink-0" />
            <Icon v-else-if="expandedCategory === 'files'" name="heroicons-document" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
            <Icon v-else-if="expandedCategory === 'entities'" :name="expandedItem?.entity_type === 'metric' ? 'heroicons-chart-bar' : 'heroicons-cube'" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
            <div class="text-[13px] font-medium truncate">{{ expandedItem?.name }}</div>
          </div>
          <button @click="selectItem(expandedItem, expandedCategory)" class="text-sm text-blue-600 hover:text-blue-700 font-medium px-1">+</button>
        </div>

        <!-- Data source details: description + tables list (client-side filtered) -->
        <div v-if="expandedCategory === 'data_sources'" class="space-y-2">
          <div v-if="expandedItem?.description" class="text-[12px] text-gray-600 leading-snug line-clamp-4">{{ expandedItem.description }}</div>
          <div>
            <div class="text-[11px] text-gray-500 mb-1">Tables</div>
            <div class="max-h-40 overflow-auto border rounded">
              <div 
                v-for="t in tablesForExpandedDataSource" 
                :key="t.id" 
                class="px-2 py-1 text-[12px] flex items-center gap-2 hover:bg-gray-50"
              >
                <DataSourceIcon :type="t.icon_type" class="w-3 h-3" />
                <span class="truncate">{{ t.name }}</span>
              </div>
              <div v-if="tablesForExpandedDataSource.length === 0" class="px-2 py-2 text-[12px] text-gray-400">No tables.</div>
            </div>
          </div>
        </div>

        <!-- Table details: non-selectable columns list -->
        <div v-else-if="expandedCategory === 'tables'" class="space-y-1">
          <div class="text-[11px] text-gray-500">Columns</div>
          <div class="flex flex-wrap gap-1 max-h-40 overflow-auto">
            <span 
              v-for="(col, idx) in (expandedItem?.columns || [])" 
              :key="idx" 
              class="px-1.5 py-0.5 bg-white rounded border text-[11px] text-gray-700"
            >
              {{ typeof col === 'string' ? col : (col as any).name }}
              <span v-if="typeof col === 'object' && (col as any).dtype" class="text-gray-400 ml-1">({{ (col as any).dtype }})</span>
            </span>
            <span v-if="!(expandedItem?.columns || []).length" class="text-[12px] text-gray-400">No columns.</span>
          </div>
        </div>

        <!-- Entity details: inline description + data preview inside dropdown -->
        <div v-else-if="expandedCategory === 'entities'" class="space-y-2">
          <div v-if="entityLoading" class="text-[11px] text-gray-500 flex items-center gap-2"><Spinner class="w-3 h-3" /> Loading…</div>
          <template v-else>
            <div v-if="(entityDetails?.description || expandedItem?.description)" class="text-[11px] text-gray-600 leading-snug">{{ entityDetails?.description || expandedItem?.description }}</div>
            <div v-if="entityPreviewColumns.length && entityPreviewRows.length" class="overflow-auto border rounded">
              <table class="min-w-full text-[11px]">
                <thead class="bg-gray-50 sticky top-0 border-b">
                  <tr>
                    <th v-for="col in entityPreviewColumns" :key="col" class="px-2 py-1 text-left font-medium text-gray-700">{{ col }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, rIdx) in entityPreviewRows" :key="rIdx" class="border-b">
                    <td v-for="col in entityPreviewColumns" :key="col" class="px-2 py-1 text-gray-800">{{ row[col] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="text-[12px] text-gray-400">No data available.</div>
            <div class="pt-1">
              <NuxtLink :to="`/catalog/${expandedItem?.id}`" class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50">Open page</NuxtLink>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'

interface MentionItem {
  id: string
  type: 'data_source' | 'datasource_table' | 'file' | 'entity'
  name: string
  subtitle?: string
  icon_type?: string
  entity_type?: string
  description?: string
  columns?: string[]
  status?: string
  data_source_id?: string
  data_source_name?: string
}

interface MentionCategory {
  name: string
  label: string
  items: MentionItem[]
}

const props = defineProps({
  modelValue: {
    type: String,
    default: ''
  },
  placeholder: {
    type: String,
    default: 'Type @ to mention...'
  },
  rows: {
    type: Number,
    default: 2
  },
  categories: {
    type: Array as () => string[],
    default: () => ['data_sources', 'tables', 'files', 'entities']
  },
  selectedDataSourceIds: {
    type: Array as () => string[],
    default: () => []
  }
})

const emit = defineEmits(['update:modelValue', 'update:mentions', 'update:mentionsGroups', 'submit'])

const inputRef = ref<HTMLDivElement | null>(null)
const dropdownRef = ref<HTMLDivElement | null>(null)
const textContent = ref('')
const showDropdown = ref(false)
const selectedIndex = ref(0)
const currentMentionStartIndex = ref(-1)
const expandedItem = ref<MentionItem | null>(null)
const expandedCategory = ref<string>('')
const detailsCache = ref<Record<string, any>>({})
const entityLoading = ref(false)
const mentions = ref<MentionItem[]>([])
const dropdownPosition = ref({ top: '0px', left: '0px' })
const allCategories = ref<MentionCategory[]>([])
const isLoadingMentions = ref(false)

const maxHeight = computed(() => {
  return props.rows === 2 ? '80px' : `${props.rows * 24}px`
})

const filteredCategories = computed(() => {
  if (currentMentionStartIndex.value === -1) return []
  
  const mentionText = textContent.value.slice(currentMentionStartIndex.value + 1).toLowerCase()
  const hasSelectedDataSources = props.selectedDataSourceIds.length > 0
  
  return allCategories.value
    .filter(cat => props.categories.includes(cat.name))
    .filter(cat => cat.name !== 'files')
    .map(category => {
      let items = category.items
      
      // CLIENT-SIDE filtering by selected data sources
      if (hasSelectedDataSources) {
        if (category.name === 'data_sources') {
          items = items.filter(item => props.selectedDataSourceIds.includes(item.id))
        } else if (category.name === 'tables') {
          items = items.filter(item => item.data_source_id && props.selectedDataSourceIds.includes(item.data_source_id))
        } else if (category.name === 'entities') {
          items = items.filter(item => Array.isArray((item as any).data_source_ids) && (item as any).data_source_ids.some((dsId: string) => props.selectedDataSourceIds.includes(dsId)))
        }
      }
      
      // Filter by search text
      items = items.filter(item => 
        (item.name || '').toLowerCase().includes(mentionText) ||
        (item.subtitle && item.subtitle.toLowerCase().includes(mentionText))
      )
      // Limit to 10 per category
      items = items.slice(0, 10)
      
      return {
        ...category,
        items
      }
    })
    .filter(category => category.items.length > 0)
})

const dropdownStyle = computed(() => ({
  bottom: '100%',
  left: '0px',
  marginBottom: '8px'
}))

function getCumulativeIndex(categoryIndex: number, itemIndex: number): number {
  let index = 0
  for (let i = 0; i < categoryIndex; i++) {
    index += filteredCategories.value[i].items.length
  }
  return index + itemIndex
}

function getTotalItems() {
  return filteredCategories.value.reduce((total, cat) => total + cat.items.length, 0)
}

function getItemAtIndex(index: number) {
  let currentIndex = 0
  for (const category of filteredCategories.value) {
    if (index < currentIndex + category.items.length) {
      return { item: category.items[index - currentIndex], category: category.name }
    }
    currentIndex += category.items.length
  }
  return null
}

function getCaretPosition(element: HTMLElement): number {
  const selection = window.getSelection()
  if (selection && selection.rangeCount > 0) {
    const range = selection.getRangeAt(0)
    const preCaretRange = range.cloneRange()
    preCaretRange.selectNodeContents(element)
    preCaretRange.setEnd(range.endContainer, range.endOffset)
    return preCaretRange.toString().length
  }
  return 0
}

function setCaretPosition(element: HTMLElement, position: number) {
  const range = document.createRange()
  const sel = window.getSelection()
  
  let currentPos = 0
  let found = false
  
  function searchNode(node: Node): boolean {
    if (node.nodeType === Node.TEXT_NODE) {
      const nodeLength = node.textContent?.length || 0
      if (currentPos + nodeLength >= position) {
        range.setStart(node, position - currentPos)
        range.collapse(true)
        found = true
        return true
      }
      currentPos += nodeLength
    } else if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).classList.contains('mention')) {
      const nodeLength = node.textContent?.length || 0
      if (currentPos + nodeLength >= position) {
        // If we're in a mention, place cursor after it
        range.setStartAfter(node)
        range.collapse(true)
        found = true
        return true
      }
      currentPos += nodeLength
    } else {
      for (const child of Array.from(node.childNodes)) {
        if (searchNode(child)) return true
      }
    }
    return false
  }
  
  searchNode(element)
  
  if (found && sel) {
    sel.removeAllRanges()
    sel.addRange(range)
  }
}

function handleInput(event: Event) {
  const target = event.target as HTMLDivElement
  
  // Preserve mention nodes - ensure they don't get broken
  const mentionNodes = target.querySelectorAll('.mention')
  mentionNodes.forEach(node => {
    if (node.childNodes.length > 1 || (node.childNodes[0] && node.childNodes[0].nodeType !== Node.TEXT_NODE)) {
      const mentionText = node.getAttribute('data-mention-id')
      if (mentionText) {
        node.textContent = node.textContent || `@${mentionText}`
      }
    }
  })
  
  textContent.value = target.innerText
  
  const cursorPosition = getCaretPosition(target)
  const textBeforeCursor = textContent.value.slice(0, cursorPosition)
  const lastAtIndex = textBeforeCursor.lastIndexOf('@')
  
  // Check if we're typing a mention (@ followed by text without space)
  if (lastAtIndex !== -1 && !textBeforeCursor.slice(lastAtIndex + 1).includes(' ')) {
    // Make sure we're not inside an existing mention
    const selection = window.getSelection()
    if (selection && selection.rangeCount > 0) {
      const range = selection.getRangeAt(0)
      const container = range.startContainer
      const isInsideMention = container.parentElement?.classList.contains('mention') ||
                             container.parentElement?.closest('.mention')
      
      if (!isInsideMention) {
        currentMentionStartIndex.value = lastAtIndex
        showDropdown.value = true
        selectedIndex.value = 0
      } else {
        showDropdown.value = false
        currentMentionStartIndex.value = -1
      }
    }
  } else {
    showDropdown.value = false
    currentMentionStartIndex.value = -1
  }
  
  emit('update:modelValue', textContent.value)
  updateMentionsList()
}

function handleKeydown(event: KeyboardEvent) {
  if (showDropdown.value) {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        selectedIndex.value = (selectedIndex.value + 1) % getTotalItems()
        scrollSelectedIntoView()
        break
      case 'ArrowUp':
        event.preventDefault()
        selectedIndex.value = (selectedIndex.value - 1 + getTotalItems()) % getTotalItems()
        scrollSelectedIntoView()
        break
      case 'Enter':
        event.preventDefault()
        const selected = getItemAtIndex(selectedIndex.value)
        if (selected) {
          selectItem(selected.item, selected.category)
        }
        break
      case 'ArrowRight':
        event.preventDefault()
        const toExpand = getItemAtIndex(selectedIndex.value)
        if (toExpand && ['data_sources', 'tables', 'entities'].includes(toExpand.category)) {
          expandItem(toExpand.item, toExpand.category)
        }
        break
      case 'ArrowLeft':
        if (expandedItem.value) {
          event.preventDefault()
          closeItemCard()
        }
        break
      case 'Escape':
        event.preventDefault()
        if (expandedItem.value) {
          closeItemCard()
        } else {
          showDropdown.value = false
        }
        break
    }
  } else {
    // When dropdown is not shown, handle Enter to submit (without Shift)
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      emit('submit')
    }
  }
  
  // Prevent typing inside mentions
  const selection = window.getSelection()
  if (selection && selection.rangeCount > 0) {
    const range = selection.getRangeAt(0)
    const container = range.startContainer
    const mentionElement = container.parentElement?.closest('.mention')
    
    // If we're inside a mention and trying to type a character, prevent it
    if (mentionElement && event.key.length === 1) {
      event.preventDefault()
      return
    }
  }
  
  // Handle backspace/delete on mentions
  if (event.key === 'Backspace' || event.key === 'Delete') {
    const selection = window.getSelection()
    if (selection && selection.rangeCount > 0) {
      const range = selection.getRangeAt(0)
      const node = range.startContainer
      
      // Check if we're inside or adjacent to a mention
      const mentionElement = node.parentElement?.closest('.mention')
      if (mentionElement) {
        event.preventDefault()
        mentionElement.remove()
        textContent.value = inputRef.value?.innerText || ''
        emit('update:modelValue', textContent.value)
        updateMentionsList()
        return
      }
      
      // Check if we're adjacent to a mention (for delete key)
      if (event.key === 'Backspace' && node.previousSibling?.nodeType === Node.ELEMENT_NODE &&
          (node.previousSibling as HTMLElement).classList.contains('mention')) {
        event.preventDefault()
        node.previousSibling.remove()
        textContent.value = inputRef.value?.innerText || ''
        emit('update:modelValue', textContent.value)
        updateMentionsList()
        return
      }
      
      // Check for delete key on next sibling
      if (event.key === 'Delete' && node.nextSibling?.nodeType === Node.ELEMENT_NODE &&
          (node.nextSibling as HTMLElement).classList.contains('mention')) {
        event.preventDefault()
        node.nextSibling.remove()
        textContent.value = inputRef.value?.innerText || ''
        emit('update:modelValue', textContent.value)
        updateMentionsList()
        return
      }
    }
  }
}

function handleClick(event: MouseEvent) {
  // If clicking on a mention, select the entire mention
  const target = event.target as HTMLElement
  if (target.classList.contains('mention')) {
    event.preventDefault()
    const range = document.createRange()
    range.selectNode(target)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
  }
}

function handlePaste(event: ClipboardEvent) {
  const text = event.clipboardData?.getData('text/plain') || ''
  const selection = window.getSelection()
  if (selection && selection.rangeCount > 0) {
    const range = selection.getRangeAt(0)
    range.deleteContents()
    range.insertNode(document.createTextNode(text))
    range.collapse(false)
    selection.removeAllRanges()
    selection.addRange(range)
  }
  handleInput({ target: inputRef.value } as Event)
}

function expandItem(item: MentionItem, category: string) {
  expandedItem.value = item
  expandedCategory.value = category
  if (category === 'entities' && item?.id) {
    loadEntityInline(String(item.id))
  }
}

function closeItemCard() {
  expandedItem.value = null
  expandedCategory.value = ''
}

function selectItem(item: MentionItem, category: string) {
  if (currentMentionStartIndex.value !== -1 && inputRef.value) {
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0) return
    
    // Create the mention node
    const mentionNode = document.createElement('span')
    mentionNode.className = 'mention'
    mentionNode.setAttribute('contenteditable', 'false')
    mentionNode.setAttribute('data-mention-id', item.id)
    mentionNode.setAttribute('data-mention-type', item.type)
    mentionNode.textContent = `@${item.name}`
    
    // Find the text node and position where @ starts
    const walker = document.createTreeWalker(
      inputRef.value,
      NodeFilter.SHOW_TEXT,
      null
    )
    
    let currentPos = 0
    let targetNode: Node | null = null
    let offsetInNode = 0
    
    while (walker.nextNode()) {
      const node = walker.currentNode
      const nodeLength = node.textContent?.length || 0
      
      if (currentPos + nodeLength > currentMentionStartIndex.value) {
        targetNode = node
        offsetInNode = currentMentionStartIndex.value - currentPos
        break
      }
      currentPos += nodeLength
    }
    
    if (!targetNode) {
      // Fallback: couldn't find the text node, bail out
      console.warn('Could not find text node for mention insertion')
      return
    }
    
    // Calculate how much text to delete (the @ and any search text)
    const currentCursorPos = getCaretPosition(inputRef.value)
    const lengthToDelete = currentCursorPos - currentMentionStartIndex.value
    
    // Split the text node at the @ position
    const textNode = targetNode as Text
    const beforeText = textNode.textContent?.slice(0, offsetInNode) || ''
    const afterText = textNode.textContent?.slice(offsetInNode + lengthToDelete) || ''
    
    // Create a document fragment to hold the new content
    const fragment = document.createDocumentFragment()
    
    if (beforeText) {
      fragment.appendChild(document.createTextNode(beforeText))
    }
    
    fragment.appendChild(mentionNode)
    fragment.appendChild(document.createTextNode(' '))
    
    if (afterText) {
      fragment.appendChild(document.createTextNode(afterText))
    }
    
    // Replace the text node with our fragment
    textNode.parentNode?.replaceChild(fragment, textNode)
    
    // Set cursor after the mention and space
    const range = document.createRange()
    const spaceNode = mentionNode.nextSibling
    if (spaceNode) {
      range.setStartAfter(spaceNode)
      range.collapse(true)
      selection.removeAllRanges()
      selection.addRange(range)
    }
    
    // Update state
    textContent.value = inputRef.value.innerText
    emit('update:modelValue', textContent.value)
    
    currentMentionStartIndex.value = -1
    showDropdown.value = false
    expandedItem.value = null
    selectedIndex.value = 0
    
    updateMentionsList()
  }
}

const tablesForExpandedDataSource = computed(() => {
  if (!expandedItem.value || expandedCategory.value !== 'data_sources') return [] as any[]
  const dsId = String(expandedItem.value.id)
  const tablesCategory = allCategories.value.find(c => c.name === 'tables')
  const items = (tablesCategory?.items || []).filter((t: any) => (t.data_source_id || t.datasource_id) === dsId)
  return items.slice(0, 50)
})

const entityDetails = computed(() => {
  const id = expandedItem.value?.id
  if (!id) return null
  return detailsCache.value[id] || { title: expandedItem.value?.name, description: expandedItem.value?.description }
})

async function loadEntityInline(id: string) {
  if (detailsCache.value[id]) return
  entityLoading.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id}`, { method: 'GET' })
    if (!error.value && data.value) {
      detailsCache.value[id] = data.value
    }
  } catch {}
  entityLoading.value = false
}

const entityPreviewColumns = computed<string[]>(() => {
  const d = entityDetails.value as any
  if (!d) return []
  if (Array.isArray(d?.data?.columns) && d.data.columns.length) {
    return d.data.columns.map((c: any) => c.field || c.headerName || c.name || c)
  }
  const rows = d?.data?.rows
  if (Array.isArray(rows) && rows[0]) return Object.keys(rows[0])
  return []
})

const entityPreviewRows = computed<any[]>(() => {
  const d = entityDetails.value as any
  const rows = d?.data?.rows
  if (Array.isArray(rows)) return rows.slice(0, 20)
  return []
})

function updateMentionsList() {
  if (!inputRef.value) return
  
  const mentionNodes = inputRef.value.querySelectorAll('.mention')
  const newMentions: MentionItem[] = []
  
  mentionNodes.forEach(node => {
    const id = node.getAttribute('data-mention-id')
    const type = node.getAttribute('data-mention-type')
    
    // Find the full item from our categories
    for (const category of allCategories.value) {
      const item = category.items.find(i => i.id === id)
      if (item) {
        newMentions.push(item)
        break
      }
    }
  })
  
  mentions.value = newMentions
  emit('update:mentions', newMentions)
  emit('update:mentionsGroups', buildMentionGroups(newMentions))
}

function buildMentionGroups(selected: MentionItem[]) {
  const groups: { name: string, items: any[] }[] = []
  const files: any[] = []
  const dataSources: any[] = []
  const tables: any[] = []
  const entities: any[] = []

  for (const m of selected) {
    if (m.type === 'file') {
      files.push({ id: m.id, filename: m.name })
    } else if (m.type === 'data_source') {
      dataSources.push({ id: m.id, name: m.name })
    } else if (m.type === 'datasource_table') {
      tables.push({ id: m.id, name: m.name, datasource_id: m.data_source_id, data_source_name: m.data_source_name })
    } else if (m.type === 'entity') {
      entities.push({ id: m.id, title: m.name, entity_type: m.entity_type })
    }
  }

  if (files.length) groups.push({ name: 'FILES', items: files })
  if (dataSources.length) groups.push({ name: 'DATA SOURCES', items: dataSources })
  if (tables.length) groups.push({ name: 'TABLES', items: tables })
  if (entities.length) groups.push({ name: 'ENTITIES', items: entities })

  return groups
}

function setPlaceholder() {
  if (inputRef.value && inputRef.value.innerText.trim() === '') {
    inputRef.value.setAttribute('data-placeholder', props.placeholder)
  }
}

// Helper to format time ago
function formatTimeAgo(dateStr: string | null): string {
  if (!dateStr) return ''
  try {
    const date = new Date(dateStr)
    const now = new Date()
    const seconds = Math.floor((now.getTime() - date.getTime()) / 1000)
    
    if (seconds < 60) return 'just now'
    if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`
    if (seconds < 604800) return `${Math.floor(seconds / 86400)} days ago`
    if (seconds < 2592000) return `${Math.floor(seconds / 604800)} weeks ago`
    return `${Math.floor(seconds / 2592000)} months ago`
  } catch {
    return ''
  }
}

// Fetch available mentions from API (no server-side filtering; filter client-side)
async function fetchAvailableMentions() {
  if (isLoadingMentions.value) return
  
  isLoadingMentions.value = true
  
  try {
    // Always fetch full mention lists; filter client-side by selectedDataSourceIds
    const url = `/mentions/available`
    
    const { data, error } = await useMyFetch(url, { method: 'GET' })
    
    if (error.value) {
      console.error('Failed to fetch mentions:', error.value)
      return
    }
    
    if (data.value) {
      // Transform API response to include display fields
      const apiData = data.value as any
      
      allCategories.value = [
        {
          name: 'data_sources',
          label: 'Data Sources',
          items: (apiData.data_sources || []).map((ds: any) => ({
            ...ds,
            subtitle: ds.description || ds.data_source_type,
            icon_type: ds.data_source_type,
          }))
        },
        {
          name: 'entities',
          label: 'Catalog',
          items: (apiData.entities || []).map((entity: any) => ({
            ...entity,
            name: entity.title,
            subtitle: entity.entity_type,
          }))
        },
        {
          name: 'files',
          label: 'Files',
          items: (apiData.files || []).map((file: any) => ({
            ...file,
            name: file.filename,
            subtitle: formatTimeAgo(file.created_at),
          }))
        },
        {
          name: 'tables',
          label: 'Tables',
          items: (apiData.tables || []).map((table: any) => ({
            ...table,
            // Normalize field for client-side filtering compatibility
            data_source_id: table.data_source_id || table.datasource_id,
            subtitle: table.data_source_name,
            icon_type: table.data_source_type,
          }))
        }
      ]
    }
  } catch (err) {
    console.error('Error fetching mentions:', err)
  } finally {
    isLoadingMentions.value = false
  }
}

onMounted(async () => {
  setPlaceholder()
  
  // Fetch available mentions
  await fetchAvailableMentions()
  
  // Set initial content if provided
  if (props.modelValue && inputRef.value) {
    inputRef.value.innerText = props.modelValue
    textContent.value = props.modelValue
  }
})

// Client-side filtering only; no refetch when selected data sources change

watch(() => props.modelValue, (newVal) => {
  if (inputRef.value && newVal !== inputRef.value.innerText) {
    inputRef.value.innerText = newVal
    textContent.value = newVal
  }
})

function scrollSelectedIntoView() {
  if (!dropdownRef.value) return
  const container = dropdownRef.value
  const selectedEl = container.querySelector(`[data-idx="${selectedIndex.value}"]`) as HTMLElement | null
  if (!selectedEl) return
  const cTop = container.scrollTop
  const cBottom = cTop + container.clientHeight
  const eTop = selectedEl.offsetTop
  const eBottom = eTop + selectedEl.offsetHeight
  if (eTop < cTop) {
    container.scrollTop = eTop
  } else if (eBottom > cBottom) {
    container.scrollTop = eBottom - container.clientHeight
  }
}
</script>

<style>
[contenteditable] {
  overflow-y: auto;
  text-align: left !important;
  vertical-align: top;
  line-height: 1.5;
}

[contenteditable]:empty:before {
  content: attr(data-placeholder);
  color: #9ca3af;
  pointer-events: none;
  font-style: normal;
}

[contenteditable]:focus {
  outline: none;
}

/* Style mentions - Cursor-style minimal design */
.mention {
  display: inline !important;
  padding: 1px 3px;
  border-radius: 3px;
  background-color: rgba(99, 102, 241, 0.10) !important;
  user-select: all;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.mention:hover {
  background-color: rgba(99, 102, 241, 0.15) !important;
}
</style>

