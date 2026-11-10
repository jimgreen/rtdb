class APIError(Exception):
    def __init__(self, status, message, *, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details
