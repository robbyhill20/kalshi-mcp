class Rejection(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class APIError(Rejection):
    def __init__(self, code: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(code)
        self.status = status
        self.ambiguous = ambiguous
