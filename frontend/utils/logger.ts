// Production-safe logging utility
export class Logger {
  private static isDevelopment = process.env.NODE_ENV === 'development'

  static log(message: string, ...args: any[]) {
    if (this.isDevelopment) {
      console.log(message, ...args)
    }
  }

  static warn(message: string, ...args: any[]) {
    if (this.isDevelopment) {
      console.warn(message, ...args)
    }
  }

  static error(message: string, ...args: any[]) {
    // Always log errors, but in production send to monitoring service
    if (this.isDevelopment) {
      console.error(message, ...args)
    } else {
      // In production, you might want to send to Sentry or similar
      console.error(message, ...args)
    }
  }

  static debug(message: string, ...args: any[]) {
    if (this.isDevelopment) {
      console.debug(message, ...args)
    }
  }
}