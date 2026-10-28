// /composables/useSpeechToText.ts
//
// Voice input for the chat composer, built on the browser's Web Speech API
// (`SpeechRecognition` / `webkitSpeechRecognition`). Nothing is uploaded to
// the Bag of Words server: the browser hands audio to its vendor's speech
// service and streams text back. Chrome, Edge and Safari support it; Firefox
// does not, so callers must hide the control when `isSupported` is false.
//
// One "take" is one press of the microphone: `start()` opens the recognizer,
// interim and final results are merged and handed to `onTranscript` as they
// arrive, and `stop()` (or the browser ending the session on its own) settles
// the take with `onEnd`. `abort()` discards the take without a final result.
//
// The org-level gate (`enable_voice_input`) is not checked here; see
// `useOrgSettings().isVoiceInputEnabled`.

import { ref, computed, onBeforeUnmount } from 'vue'

export type SpeechToTextErrorCode =
  | 'not-allowed'
  | 'no-speech'
  | 'audio-capture'
  | 'network'
  | 'unsupported'
  | 'unknown'

export interface UseSpeechToTextOptions {
  /** Called with the full transcript of the current take on every update. */
  onTranscript: (transcript: string, isFinal: boolean) => void
  /** Called once the take is over (user stop, browser end, or an error). */
  onEnd?: () => void
  /** Called when the recognizer reports an error worth telling the user about. */
  onError?: (code: SpeechToTextErrorCode) => void
  /** BCP-47 language tag; omitted = browser/OS default. */
  lang?: () => string | undefined
  /** Keep listening through pauses until stopped. Default true. */
  continuous?: boolean
}

type RecognitionCtor = new () => any

function resolveRecognitionCtor(): RecognitionCtor | null {
  if (typeof window === 'undefined') return null
  const w = window as any
  return (w.SpeechRecognition || w.webkitSpeechRecognition || null) as RecognitionCtor | null
}

/**
 * Map the app's short locale codes to a BCP-47 tag the recognizer accepts.
 * Unknown codes pass through unchanged (the API tolerates bare codes).
 */
export function speechLangForLocale(locale: string | undefined | null): string | undefined {
  if (!locale) return undefined
  const map: Record<string, string> = {
    en: 'en-US',
    es: 'es-ES',
    he: 'he-IL',
    fr: 'fr-FR',
    de: 'de-DE',
    it: 'it-IT',
    pt: 'pt-BR',
    ru: 'ru-RU',
    sv: 'sv-SE',
    ar: 'ar-SA',
  }
  return map[locale] || locale
}

export const useSpeechToText = (options: UseSpeechToTextOptions) => {
  const Ctor = resolveRecognitionCtor()
  const isSupported = computed(() => Ctor !== null)
  const isListening = ref(false)

  let recognition: any = null
  // Finalized segments of the current take, in order. Interim text is
  // appended on top of these for display but never stored.
  let finalSegments: string[] = []
  let aborted = false

  function buildTranscript(interim: string): string {
    const parts = [...finalSegments]
    if (interim) parts.push(interim)
    return parts.join(' ').replace(/\s+/g, ' ').trim()
  }

  function teardown() {
    if (!recognition) return
    recognition.onresult = null
    recognition.onerror = null
    recognition.onend = null
    recognition.onstart = null
    recognition = null
  }

  function start(): boolean {
    if (!Ctor || isListening.value) return false
    finalSegments = []
    aborted = false

    const rec = new Ctor()
    rec.continuous = options.continuous ?? true
    rec.interimResults = true
    rec.maxAlternatives = 1
    const lang = options.lang?.()
    if (lang) rec.lang = lang

    rec.onstart = () => { isListening.value = true }

    rec.onresult = (event: any) => {
      if (aborted) return
      let interim = ''
      // `resultIndex` marks the first result that changed in this event;
      // everything before it has already been handled.
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i]
        const text = (result[0]?.transcript || '').trim()
        if (!text) continue
        if (result.isFinal) finalSegments.push(text)
        else interim += (interim ? ' ' : '') + text
      }
      options.onTranscript(buildTranscript(interim), interim.length === 0)
    }

    rec.onerror = (event: any) => {
      const raw = String(event?.error || 'unknown')
      // "aborted" is what the browser reports after our own abort(); not an
      // error from the user's point of view.
      if (raw === 'aborted') return
      const known: SpeechToTextErrorCode[] = ['not-allowed', 'no-speech', 'audio-capture', 'network']
      const code = (known.includes(raw as SpeechToTextErrorCode) ? raw : 'unknown') as SpeechToTextErrorCode
      options.onError?.(code)
    }

    rec.onend = () => {
      isListening.value = false
      teardown()
      options.onEnd?.()
    }

    recognition = rec
    try {
      rec.start()
    } catch {
      // Chrome throws InvalidStateError if start() races a previous session.
      teardown()
      isListening.value = false
      options.onError?.('unknown')
      return false
    }
    return true
  }

  /** Stop listening; any pending audio is still transcribed before onEnd. */
  function stop() {
    if (!recognition) return
    try { recognition.stop() } catch { /* already stopped */ }
  }

  /** Stop immediately and discard the take. */
  function abort() {
    if (!recognition) return
    aborted = true
    try { recognition.abort() } catch { /* already stopped */ }
  }

  onBeforeUnmount(() => {
    aborted = true
    if (recognition) {
      try { recognition.abort() } catch { /* ignore */ }
      teardown()
    }
    isListening.value = false
  })

  return { isSupported, isListening, start, stop, abort }
}
