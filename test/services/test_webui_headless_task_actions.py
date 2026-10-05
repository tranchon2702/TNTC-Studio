import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import state as sm
from app.utils import utils

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


@pytest.fixture
def headless_task_app(tmp_path, monkeypatch):
    # Sử dụng một thư mục tác vụ riêng để tránh đọc hoặc sửa đổi bản ghi bản dựng thực tế của nhà phát triển. Tệp thử nghiệm chỉ cần có
    # Streamlit được đăng ký làm tài nguyên đa phương tiện và không tham gia giải mã video nên không cần gọi nó trong các bài kiểm tra đơn vị.
    # FFmpeg tạo ra các tài liệu có thể bao phủ ổn định các nhánh giao diện người dùng mà không cần máy chủ để bàn.
    tasks_dir = tmp_path / "storage" / "tasks"
    task_dir = tasks_dir / "headless-test"
    task_dir.mkdir(parents=True)
    video_file = task_dir / "final-1.mp4"
    video_file.write_bytes(b"test video payload")

    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_dir))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_args, **_kwargs: ([], 0))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    # AppTest thực thi lại tập lệnh trang nhiều lần; việc lưu cấu hình phải được duy trì trong suốt vòng đời thử nghiệm
    # Cô lập để ngăn việc khởi tạo điều khiển vô tình ghi vào config.toml của nhà phát triển.
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        yield app, video_file


def _button_by_key_prefix(app, key_prefix):
    return next(button for button in app.button if str(button.key).startswith(key_prefix))


def test_headless_play_renders_and_closes_browser_preview(headless_task_app):
    app, video_file = headless_task_app

    _button_by_key_prefix(app, "play_task_all_headless-test").click()
    app.run()

    assert not app.exception
    assert app.session_state["task_preview_video_file"] == str(video_file.resolve())
    assert len(app.get("video")) == 1

    _button_by_key_prefix(app, "close_task_video_preview").click()
    app.run()

    assert "task_preview_video_file" not in app.session_state
    assert len(app.get("video")) == 0


def test_headless_open_folder_shows_host_mapped_path(headless_task_app):
    app, _ = headless_task_app

    _button_by_key_prefix(app, "open_task_all_headless-test").click()
    app.run()

    assert not app.exception
    assert any(
        "./storage/tasks/headless-test" in toast.value for toast in app.get("toast")
    )
