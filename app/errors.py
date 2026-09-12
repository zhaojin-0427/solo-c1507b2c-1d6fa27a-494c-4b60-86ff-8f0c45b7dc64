"""领域错误：方案被拒绝时抛出，由 API 层转换为 422。"""


class DomainError(Exception):
    """拼版领域错误。errors 为 [{"code": ..., "message": ...}, ...]。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


def err(code: str, message: str) -> dict:
    return {"code": code, "message": message}
