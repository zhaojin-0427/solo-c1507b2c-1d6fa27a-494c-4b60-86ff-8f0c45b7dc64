import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def job_payload():
    return {
        "job_name": "API 测试书",
        "page": {"width": 148, "height": 210},
        "total_pages": 64,
        "paper": {"width": 700, "height": 1000, "thickness": 0.1, "grain": "vertical"},
        "press": {"gripper_mm": 10, "gripper_edge": "bottom"},
        "binding": "left",
        "bleed_mm": 3,
        "safety_mm": 3,
        "marks_margin_mm": 5,
        "signature_options": [
            {"pages": 16, "style": "standard"},
            {"pages": 8, "style": "standard"},
            {"pages": 32, "style": "standard", "sheets": 2},
        ],
    }
