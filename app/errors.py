"""领域错误：方案被拒绝时抛出，由 API 层转换为 422。"""


class DomainError(Exception):
    """拼版领域错误。errors 为 [{"code": ..., "message": ...}, ...]。

    payload 为可选的附加响应数据（如配帖标冲突时的完整计算结果），
    由 API 层并入 422 响应体。
    """

    def __init__(self, errors: list[dict], payload: dict | None = None):
        self.errors = errors
        self.payload = payload
        super().__init__("; ".join(e["message"] for e in errors))


def err(code: str, message: str) -> dict:
    return {"code": code, "message": message}
