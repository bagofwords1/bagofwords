class ResolveError(Exception):
    pass


class ValidationError(Exception):
    def __init__(self, details):
        super().__init__("validation error")
        self.details = details


class TimeoutError(Exception):
    pass


class RuntimeToolError(Exception):
    pass

