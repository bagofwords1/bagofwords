import { customRef } from 'vue'

export function useDebouncedRef(fn: Function, delay: number) {
    let timeout: NodeJS.Timeout
    
    return customRef((track, trigger) => {
        return {
            get() {
                track()
                return fn
            },
            set(value) {
                clearTimeout(timeout)
                timeout = setTimeout(() => {
                    fn()
                    trigger()
                }, delay)
            }
        }
    })
}