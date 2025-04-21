<template>
    <div>
        <h1>Dashboard - Notion-like Editor Playground</h1>

        <div class="controls">
            <button @click="addCell" :disabled="!editor">
                Add Cell
            </button>
            <button @click="addChartBlock" :disabled="!editor">
                Add Chart Block to Selected Cell
            </button>
             <button @click="addTextBlockToSelectedCell" :disabled="!editor">
                Add Text Block to Selected Cell
            </button>
        </div>

        <editor-content :editor="editor" />
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, defineComponent, h, reactive /* IMPORTANT for inline component */ } from 'vue';
import { Editor, EditorContent, Node, mergeAttributes, NodeViewWrapper, NodeViewContent } from '@tiptap/vue-3';
import { VueNodeViewRenderer } from '@tiptap/vue-3';
import StarterKit from '@tiptap/starter-kit';
import type { NodeViewProps } from '@tiptap/vue-3';
import { NodeSelection } from '@tiptap/pm/state';
import Paragraph from '@tiptap/extension-paragraph'; // Explicitly import if needed
import type { Fragment } from '@tiptap/pm/model'; // Import Fragment type for clarity


// --- Node View Component for ChartBlock ---
const ChartBlockComponent = defineComponent({
  props: {
      node: { type: Object as () => Node, required: true },
      updateAttributes: { type: Function, required: true },
      selected: { type: Boolean, required: true },
      editor: { type: Object as () => Editor, required: true }, // Need editor to prevent text selection during resize
  },
  setup(props) {
      const chartType = props.node.attrs.chartType;
      const chartData = props.node.attrs.chartData;

      // --- Resizing Logic ---
      const isResizing = ref(false);
      const startPos = reactive({ x: 0, y: 0 });
      const startSize = reactive({ width: 0, height: 0 });

      const handleMouseDown = (event: MouseEvent) => {
          event.preventDefault();
          event.stopPropagation(); // Prevent tiptap selection/drag

          isResizing.value = true;
          startPos.x = event.clientX;
          startPos.y = event.clientY;
          startSize.width = props.node.attrs.width;
          startSize.height = props.node.attrs.height;

          // Add global listeners
          document.addEventListener('mousemove', handleMouseMove);
          document.addEventListener('mouseup', handleMouseUp);

          // Disable text selection globally while resizing
          document.body.style.userSelect = 'none';
           // Optional: Prevent editor interactions while resizing
           props.editor.setOptions({ editable: false });
      };

      const handleMouseMove = (event: MouseEvent) => {
          if (!isResizing.value) return;

          const deltaX = event.clientX - startPos.x;
          const deltaY = event.clientY - startPos.y;

          const minWidth = 50; // Set minimum dimensions
          const minHeight = 30;

          const newWidth = Math.max(minWidth, startSize.width + deltaX);
          const newHeight = Math.max(minHeight, startSize.height + deltaY);

          // Update node attributes directly
          props.updateAttributes({
              width: newWidth,
              height: newHeight,
          });
      };

      const handleMouseUp = () => {
          if (!isResizing.value) return;

          isResizing.value = false;
          document.removeEventListener('mousemove', handleMouseMove);
          document.removeEventListener('mouseup', handleMouseUp);

          // Re-enable text selection
          document.body.style.userSelect = '';
          // Re-enable editor interactions
          props.editor.setOptions({ editable: true });

          // Optional: Trigger a view update if needed, though attribute update should handle it
          // props.editor.view.updateState(props.editor.state);
      };

      // --- Render Function ---
      return () => {
          const width = props.node.attrs.width;
          const height = props.node.attrs.height;

          // Create Resize Handle VNode (only when selected)
          const resizeHandle = props.selected ? h('div', {
              class: 'resize-handle',
              style: {
                  position: 'absolute',
                  bottom: '0px',
                  right: '0px',
                  width: '10px',
                  height: '10px',
                  backgroundColor: 'blue',
                  cursor: 'nwse-resize',
                  border: '1px solid white',
              },
              onMousedown: handleMouseDown,
          }) : null;

          // Create Main Block VNode
          return h(NodeViewWrapper, {
              as: 'div',
              'data-type': 'chart-block',
              'data-chart-type': chartType,
              class: {
                  'chart-block-node': true,
                  'ProseMirror-selectednode': props.selected,
                  'is-resizing': isResizing.value, // Add class during resize
              },
              style: {
                  position: 'relative', // Needed for absolute positioned handle
                  border: '1px dashed blue',
                  padding: '0.5rem',
                  margin: '0.5rem 0',
                  backgroundColor: '#f0f8ff',
                  overflow: 'hidden', // Prevent content spill during resize
                  width: `${width}px`, // Apply width from attributes
                  height: `${height}px`, // Apply height from attributes
              }
          }, [
              `Chart: ${chartType} (${width}x${height})`, // Display dimensions for feedback
              resizeHandle // Add the handle if it exists
          ]);
      }
  }
});


// --- Node View Component for Cell ---
// In a real app, this would be in its own .vue file
const CellComponent = defineComponent({
    // Use full NodeViewProps type
    props: {
        node: { type: Object as () => Node, required: true },
        editor: { type: Object as () => Editor, required: true },
        getPos: { type: Function as unknown as () => (() => number | undefined), required: true },
        selected: { type: Boolean, required: true },
        // Add other props as needed
    },
    setup(props) {
        // --- Drag and Drop Logic ---
        let draggedNodeInfo: { node: any; pos: number; size: number; index: number; } | null = null;

        const handleDragStart = (event: DragEvent, childNode: any, childIndex: number) => {
            // Ensure getPos returns a valid function before calling it
            const currentPos = typeof props.getPos === 'function' ? props.getPos() : undefined;
            if (!event.dataTransfer || typeof currentPos !== 'number') {
                 console.error("Cannot get position in handleDragStart");
                 return;
            }
            event.dataTransfer.effectAllowed = 'move';

            let childPos = currentPos + 1; // Position *inside* the cell node
            for (let i = 0; i < childIndex; i++) {
                childPos += props.node.content.child(i).nodeSize;
            }

            draggedNodeInfo = { node: childNode, pos: childPos, size: childNode.nodeSize, index: childIndex };
            event.dataTransfer.setData('application/pm-slice', ''); // Use a specific mime type if needed, or just text/plain
            event.dataTransfer.setData('text/plain', ''); // Necessary for Firefox
            (event.target as HTMLElement)?.closest('.draggable-item')?.classList.add('dragging'); // Target the wrapper
             console.log('Drag Start:', draggedNodeInfo);
        };

        const handleDragOver = (event: DragEvent, childIndex: number) => {
            event.preventDefault();
             if (!draggedNodeInfo || draggedNodeInfo.index === childIndex) return; // Don't allow dropping onto itself visually
            event.dataTransfer!.dropEffect = 'move';
            (event.currentTarget as HTMLElement).classList.add('drag-over');
        };

       const handleDragLeave = (event: DragEvent) => {
           (event.currentTarget as HTMLElement).classList.remove('drag-over');
       };

        const handleDrop = (event: DragEvent, dropIndex: number) => {
            event.preventDefault();
            const dropTargetElement = (event.currentTarget as HTMLElement);
            dropTargetElement.classList.remove('drag-over');
            // Find the originally dragged element and remove its class too
            const draggedElement = props.editor.view.dom.querySelector('.dragging');
            draggedElement?.classList.remove('dragging');


            const currentPos = typeof props.getPos === 'function' ? props.getPos() : undefined;
            if (!draggedNodeInfo || typeof currentPos !== 'number') {
                console.error("Missing drag info or position in handleDrop");
                draggedNodeInfo = null; // Reset drag info
                return;
            }

            // Don't do anything if dropping in the same position
            if (draggedNodeInfo.index === dropIndex) {
                console.log("Dropped in the same place.");
                draggedNodeInfo = null;
                return;
            }

            const { node: draggedNode, pos: initialPos, size: draggedSize } = draggedNodeInfo;
            const cellStartPos = currentPos;

            // Calculate target position *within the document* based on drop index
            let targetDocPos = cellStartPos + 1;
            for (let i = 0; i < dropIndex; i++) {
                 // If iterating past the original index, need to account for the node not being there
                 if (i === draggedNodeInfo.index) continue;
                targetDocPos += props.node.content.child(i).nodeSize;
            }
             // If dropping after the original position, the node is still there when calculating insert pos
            if (dropIndex > draggedNodeInfo.index) {
                 targetDocPos += draggedNode.nodeSize;
            }


            console.log('Drop:', {
                draggedIndex: draggedNodeInfo.index,
                dropIndex,
                initialDocPos: initialPos,
                draggedSize,
                cellStartPos,
                calculatedTargetDocPos: targetDocPos,
             });

             // --- Transaction ---
            const tr = props.editor.state.tr;

            // Delete from original position
            tr.delete(initialPos, initialPos + draggedSize);

            // Calculate insert position *after* delete
            // If target was after original, its effective position shifts left by draggedSize
            const finalInsertPos = targetDocPos > initialPos ? targetDocPos - draggedSize : targetDocPos;

             console.log("Final Insert Pos:", finalInsertPos);

            // Validate final insert position
            if (finalInsertPos < cellStartPos + 1 || finalInsertPos > cellStartPos + 1 + props.node.content.size - draggedSize) {
                 console.error("Final insert position is out of bounds within the cell after deletion.", {
                    finalInsertPos,
                    cellStart: cellStartPos + 1,
                    cellEndAfterDelete: cellStartPos + 1 + props.node.content.size - draggedSize
                 });
                 draggedNodeInfo = null;
                 return; // Avoid dispatching invalid transaction
            }


            // Insert at the final adjusted position
            tr.insert(finalInsertPos, draggedNode);

            // Dispatch only if the transaction changes the document
            if (tr.docChanged) {
                props.editor.view.dispatch(tr);
                console.log("Transaction dispatched");
            } else {
                 console.log("Transaction did not change document.");
            }

            draggedNodeInfo = null; // Reset drag info
       };

        const handleDragEnd = (event: DragEvent) => {
           // General cleanup
           const draggedElement = (event.target as HTMLElement)?.closest('.draggable-item');
           draggedElement?.classList?.remove('dragging');
           props.editor.view.dom.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
           draggedNodeInfo = null;
           console.log('Drag End');
       };

        // Render the cell container and its children
        return () => {
             // Array to hold the virtual nodes for children
             const childrenVNodes: ReturnType<typeof h>[] = [];

             // Use forEach to iterate over the Fragment
             (props.node.content as Fragment).forEach((childNode, offset, index) => {
                 // Create a wrapper div for each child to handle drag events
                 const childVNode = h('div', {
                     class: 'draggable-item',
                     draggable: true,
                     onDragstart: (e: DragEvent) => handleDragStart(e, childNode, index),
                     onDragover: (e: DragEvent) => handleDragOver(e, index),
                     onDragleave: (e: DragEvent) => handleDragLeave(e),
                     onDrop: (e: DragEvent) => handleDrop(e, index),
                     onDragend: (e: DragEvent) => handleDragEnd(e),
                     // Try creating a more stable key if possible, e.g., using a unique node ID if available
                     key: `child-${index}-${childNode.type.name}-${childNode.attrs.id || childNode.textContent?.slice(0, 10) || index}`
                 }, [
                     // Use NodeViewContent to render the actual child node content
                     h(NodeViewContent, { as: 'div', class: 'item-content' })
                 ]);
                 childrenVNodes.push(childVNode); // Add the generated VNode to the array
             });


             // Create the wrapper element for the whole cell node view
             return h(NodeViewWrapper, {
                 as: 'div',
                 class: { 'cell-node': true, 'ProseMirror-selectednode': props.selected },
                 'data-type': 'cell',
             }, () => // Wrap the inner content rendering in a function
                 h('div', { class: 'cell-content-wrapper' }, childrenVNodes)
             );
        }
    }
});



// --- Custom Nodes Definitions ---

// Updated Cell Node to use Node View
const CellNode = Node.create({
  name: 'cell',
  group: 'block',
  content: 'block+', // Allows multiple blocks (paragraphs, charts) inside
  draggable: false, // Disable Tiptap's default drag handle for the cell itself
  isolating: true, // Important for node views managing content

  parseHTML() {
    return [{ tag: 'div[data-type="cell"]' }];
  },

  renderHTML({ HTMLAttributes }) {
    // The '0' indicates where content should be rendered if NOT using a Node View
    return ['div', mergeAttributes(HTMLAttributes, { 'data-type': 'cell' }), 0];
  },

  addNodeView() {
    return VueNodeViewRenderer(CellComponent);
  }
});

// Updated ChartBlock Node to include width/height attributes
const ChartBlockNode = Node.create({
    name: 'chartBlock',
    group: 'block',
    atom: true,
    draggable: false, // Resizing handle is separate from dragging the block itself

    addAttributes() {
        return {
            chartType: { default: 'bar' },
            chartData: { default: {} },
            // Add width and height attributes with defaults
            width: {
                default: 250,
                renderHTML: attributes => ({ style: `width: ${attributes.width}px` }), // Add style directly
                parseHTML: element => element.style.width ? parseInt(element.style.width, 10) : 250,
            },
            height: {
                default: 150,
                renderHTML: attributes => ({ style: `height: ${attributes.height}px` }), // Add style directly
                parseHTML: element => element.style.height ? parseInt(element.style.height, 10) : 150,
            },
        };
    },

    parseHTML() {
        return [{
            tag: 'div[data-type="chart-block"]',
            getAttrs: domNode => {
                 if (typeof domNode === 'string') return false;
                 const element = domNode as Element;
                 // Use the parseHTML logic defined in addAttributes for width/height
                 return {
                    chartType: element.getAttribute('data-chart-type') || 'bar',
                    chartData: JSON.parse(element.getAttribute('data-chart-data') || '{}'),
                    width: this.options.parseHTML?.(element) ?? 250, // Fallback if parseHTML isn't called directly here (should be)
                    height: this.options.parseHTML?.(element) ?? 150,
                 }
            }
        }];
    },

    // renderHTML merges attributes including the style from width/height
    renderHTML({ HTMLAttributes }) {
        return ['div', mergeAttributes(HTMLAttributes, { 'data-type': 'chart-block' })];
    },

    addNodeView() {
        // Pass editor prop to the component
        return VueNodeViewRenderer(ChartBlockComponent);
    }
});

// --- Editor Setup ---
const editor = ref<Editor | null>(null);

onMounted(() => {
  editor.value = new Editor({
    content: `
      <div data-type="cell"><p>Initial Cell Content</p></div>
    `,
    extensions: [
      StarterKit.configure({
          // Ensure Paragraph is included
          paragraph: {}, // Or just rely on StarterKit default
      }),
      // Ensure Paragraph extension is explicitly registered if needed (StarterKit usually handles it)
      // Paragraph,
      CellNode, // Use updated CellNode with Node View
      ChartBlockNode, // Use updated ChartBlockNode with Node View
      // Gapcursor might be useful here
      // Gapcursor,
    ],
    editorProps: {
      attributes: {
        class: 'tiptap-editor m-5 focus:outline-none border border-gray-300 p-4 rounded',
      },
    },
  });
});

onBeforeUnmount(() => {
  editor.value?.destroy();
});

// --- Commands ---

const addCell = () => {
  if (!editor.value) return;
  editor.value.chain().focus().insertContentAt(editor.value.state.doc.content.size, {
    type: CellNode.name,
    content: [{ type: 'paragraph', content: [{ type: 'text', text: 'New cell...' }] }]
  }).run();
};

// Helper to find the parent cell of the current selection
const findParentCellPos = (editorInstance: Editor): number | null => {
    if (!editorInstance?.state?.selection) {
        console.warn("findParentCellPos: Editor state or selection is missing.");
        return null;
    }
    const { selection } = editorInstance.state;
    const { $from, node } = selection;

    if (!$from) {
         console.warn("findParentCellPos: selection.$from is missing.");
         console.log("Selection details:", JSON.stringify(selection));
        return null;
    }

    console.log(`findParentCellPos: Checking selection type=${selection.type}, empty=${selection.empty}, anchor=${selection.anchor}, head=${selection.head}`);
    console.log(`  $from: depth=${$from.depth}, pos=${$from.pos}, parent=${$from.parent.type.name}, nodeBefore=${$from.nodeBefore?.type.name}, nodeAfter=${$from.nodeAfter?.type.name}`);

    // NEW LOGIC: Iterate upwards through depths
    for (let d = $from.depth; d >= 1; d--) { // Iterate from current depth down to depth 1 (direct children of doc)
        const ancestorNode = $from.node(d); // Get the node at this depth in the path
        console.log(`  Checking depth ${d}: Node type = ${ancestorNode.type.name}`);
        if (ancestorNode.type.name === CellNode.name) {
            const pos = $from.before(d); // Position before the node at this depth
            // Ensure pos is valid before returning
            if (typeof pos === 'number' && pos >= 0) {
                 console.log(`  Found cell at depth ${d}, returning pos: ${pos}`);
                return pos;
            } else {
                 console.warn(`  Found cell at depth ${d}, but calculated position (${pos}) is invalid.`);
                 // Continue loop just in case, though unlikely to find another valid one higher up
            }
        }
    }


    // Fallback Check: If selection directly selects the cell node itself (NodeSelection)
    if (node?.type.name === CellNode.name && selection instanceof NodeSelection) {
        console.log(`  Found cell via NodeSelection at pos ${selection.from}`);
        return selection.from;
    }

    console.warn("findParentCellPos: Could not find parent cell using depth iteration or NodeSelection.");
    return null;
}


const addChartBlock = () => {
    if (!editor.value) return;

    // Chain focus and then run the insertion logic
    editor.value.chain().focus().command(({ editor: currentEditor }) => {
         // Now that focus is hopefully set, find the cell
         const parentCellPos = findParentCellPos(currentEditor);

        if (parentCellPos !== null) {
            const cellNode = currentEditor.state.doc.nodeAt(parentCellPos);
            if (cellNode) {
                const insertPos = parentCellPos + 1 + cellNode.content.size;
                console.log("Inserting chart block at pos:", insertPos);
                // Use the 'currentEditor' from the command context for chaining
                currentEditor.chain().insertContentAt(insertPos, {
                    type: ChartBlockNode.name,
                    // Use default width/height initially
                    attrs: { chartType: 'pie', chartData: { value: 50 } }
                })
                .focus(insertPos + 1) // Focus after insertion
                .run();
                return true; // Indicate command success
            } else {
                console.warn("Could not find cell node at pos:", parentCellPos);
            }
        } else {
            alert("Please place the cursor inside a cell or select a cell to add a chart block.");
        }
        return false; // Indicate command failure
    }).run(); // Run the outer chain (focus + custom command)

};

const addTextBlockToSelectedCell = () => {
     if (!editor.value) return;

     editor.value.chain().focus().command(({ editor: currentEditor }) => {
         const parentCellPos = findParentCellPos(currentEditor);

        if (parentCellPos !== null) {
            const cellNode = currentEditor.state.doc.nodeAt(parentCellPos);
             if (cellNode) {
                const insertPos = parentCellPos + 1 + cellNode.content.size;
                 currentEditor.chain().insertContentAt(insertPos, {
                    type: 'paragraph',
                    content: [{ type: 'text', text: 'New text block...' }]
                }).focus(insertPos + 1) // Focus inside new paragraph
                .run();
                 return true;
             } else {
                 console.warn("Could not find cell node at pos:", parentCellPos);
             }
        } else {
             alert("Please place the cursor inside a cell or select a cell to add a text block.");
        }
         return false;
     }).run();
};

</script>

<style>
/* General Editor Styling */
.tiptap-editor {
  border: 1px solid #ccc;
  padding: 1rem;
  border-radius: 4px;
  min-height: 200px;
}

.tiptap-editor:focus {
  outline: 2px solid blue;
}

/* Cell Node Styling */
.cell-node {
  border: 1px solid #ccc;
  background-color: #f9f9f9;
  padding: 1rem;
  margin-bottom: 1rem;
  min-height: 50px;
  position: relative; /* Needed for absolute positioning of potential handles */
}

.cell-node.ProseMirror-selectednode {
    outline: 2px solid lightblue; /* Highlight selected cell */
}

/* Styling for draggable items within a cell */
.draggable-item {
  /* border: 1px dashed #aaa; */ /* Removed border for cleaner look */
  padding: 0.1rem 0; /* Minimal padding */
  margin: 0.25rem 0;
  background-color: transparent; /* Let content background show */
  cursor: grab;
  position: relative; /* For potential absolute elements like handles */
}
/* Add a visual grab handle maybe? */
/* .draggable-item::before {
    content: '⠿';
    position: absolute;
    left: -15px;
    top: 50%;
    transform: translateY(-50%);
    cursor: grab;
    color: #aaa;
} */


.draggable-item:active {
    cursor: grabbing;
}

.draggable-item .item-content {
    /* Style the actual content container if needed */
    /* background-color: white; */
    /* padding: 0.5rem; */ /* Add padding here if desired */
}


.draggable-item.dragging {
  opacity: 0.4;
  background-color: #eef;
}

/* Visual feedback for drop target */
.draggable-item.drag-over {
  outline: 2px dashed green;
  /* background-color: #e8f5e9; */
}

/* Chart Block Node specific styling (rendered by ChartBlockComponent) */
.chart-block-node {
  /* Styles moved into the component's render logic for dynamic binding */
  display: block; /* Ensure chart blocks take full width or as defined */
}

.chart-block-node.ProseMirror-selectednode {
    outline: 2px solid darkblue; /* Highlight selected chart */
}

.controls {
    margin-bottom: 1rem;
    display: flex;
    gap: 0.5rem;
}

/* Add style for resize handle */
.resize-handle {
    /* Style defined inline for simplicity, but could be a class */
}

/* Optional: Style during resize */
.chart-block-node.is-resizing {
    opacity: 0.8;
    outline: 2px dashed darkblue;
}
</style>