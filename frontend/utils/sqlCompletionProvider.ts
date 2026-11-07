// Monaco completion provider for SqlEditor.
//
// Monaco registers completion providers per language for every editor on the
// page, so this is registered once and finds which connection the asking
// editor belongs to by its model URI (each SqlEditor records its own).
import { sqlCursorContext, sqlSuggestions, type SqlCatalog } from '~/utils/sqlCompletion'

// Shared by every SqlEditor on the page.
export const completionRegistry = {
  models: new Map<string, string>(), // model URI -> connection id ('' = none)
  registered: false,
}

export function registerSqlCompletions(monaco: any, getCatalogs: () => Record<string, SqlCatalog | null>) {
  if (completionRegistry.registered) return
  completionRegistry.registered = true
  const K = monaco.languages.CompletionItemKind
  const kindOf = { table: K.Struct, column: K.Field, keyword: K.Keyword } as const

  monaco.languages.registerCompletionItemProvider('sql', {
    triggerCharacters: ['.', ' ', ','],
    provideCompletionItems(model: any, position: any, context: any) {
      const connectionId = completionRegistry.models.get(model.uri.toString())
      if (!connectionId) return { suggestions: [] }
      const catalog = getCatalogs()[connectionId] || null

      const word = model.getWordUntilPosition(position)
      const before = model.getValueInRange({
        startLineNumber: 1, startColumn: 1,
        endLineNumber: position.lineNumber, endColumn: word.startColumn,
      })
      const ctx = sqlCursorContext(before)
      // A space or comma only opens the list where a table name is expected;
      // anywhere else it would pop up after every word.
      const typedTrigger = context?.triggerCharacter
      if ((typedTrigger === ' ' || typedTrigger === ',') && ctx.kind !== 'table') {
        return { suggestions: [] }
      }

      const range = {
        startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
        startColumn: word.startColumn, endColumn: word.endColumn,
      }
      const items = sqlSuggestions(ctx, model.getValue(), catalog).slice(0, 2000)
      return {
        suggestions: items.map((s, i) => ({
          label: s.label,
          kind: kindOf[s.kind],
          insertText: s.insert,
          // Match on the plain name even when the insert is quoted.
          filterText: s.label,
          detail: s.detail,
          sortText: `${s.rank}${String(i).padStart(5, '0')}`,
          range,
        })),
      }
    },
  })
}
