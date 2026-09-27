// The storage-change approval card must be visible while its tool card is
// collapsed: EditArtifactTool starts collapsed, and a card hidden inside the
// collapsible block leaves the run waiting on an approval nobody can see
// (the same regression was fixed once before). Parses the real SFC templates.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { parse } from '@vue/compiler-sfc'

const COLLAPSE = /isCollapsed/

function findWithAncestors(node, tag, ancestors = [], out = []) {
  if (node.type === 1 && node.tag === tag) out.push(ancestors)
  for (const child of node.children || []) findWithAncestors(child, tag, node.type === 1 ? [...ancestors, node] : ancestors, out)
  return out
}

function hidingDirectives(el) {
  return (el.props || []).filter((p) => p.type === 7 && ['if', 'else-if', 'show'].includes(p.name) && COLLAPSE.test(p.exp?.content || ''))
}

for (const file of ['EditArtifactTool.vue', 'CreateArtifactTool.vue']) {
  const source = fs.readFileSync(new URL(`../../components/tools/${file}`, import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.equal(errors.length, 0, `${file} parses`)
  const found = findWithAncestors(descriptor.template.ast, 'StorageChangeApproval')
  assert.equal(found.length, 1, `${file} renders exactly one StorageChangeApproval`)
  const hiding = found[0].flatMap(hidingDirectives)
  assert.deepEqual(hiding.map((d) => `v-${d.name}="${d.exp.content}"`), [], `${file}: approval card must not depend on isCollapsed`)
}

console.log('storageApprovalPlacement: ok')
