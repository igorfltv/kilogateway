class GatewayError(Exception):
    def __init__(self, code: str, status: int = 503, retry_after: int | None = None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)
