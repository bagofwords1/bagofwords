// A connector can name its own catalog items (Neo4j: node and relationship
// types); every other connector keeps the noun of its data_shape.
//   cd frontend && node tests/unit/catalogNouns.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../../composables/useCatalogCount.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText;
const mod = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

const NEO4J = ['node or relationship type', 'node and relationship types'];
assert.equal(mod.connectionCatalogLabel({data_shape: 'objects', catalog_nouns: NEO4J, table_count: 5}), '5 node and relationship types');
assert.equal(mod.connectionCatalogLabel({data_shape: 'objects', catalog_nouns: NEO4J, table_count: 1}), '1 node or relationship type');
assert.equal(mod.connectionCatalogLabel({data_shape: 'objects', table_count: 3}), '3 collections', 'MongoDB keeps its noun');
assert.equal(mod.connectionCatalogLabel({data_shape: 'tables', catalog_nouns: null, table_count: 2}), '2 tables');

const {computeFromAgent} = mod.useCatalogCount();
const registry = {neo4j: {type: 'neo4j', data_shape: 'objects', catalog_nouns: NEO4J}, mongodb: {type: 'mongodb', data_shape: 'objects'}};
assert.equal(computeFromAgent({connections: [{type: 'neo4j', table_count: 4}]}, registry).label, '4 node and relationship types');
assert.equal(computeFromAgent({connections: [{type: 'neo4j', table_count: 4}, {type: 'mongodb', table_count: 1}]}, registry).label,
  '5 collections', 'mixed types fall back to the shared shape noun');
console.log('PASS: catalog nouns follow a connector override, else the data_shape noun');
