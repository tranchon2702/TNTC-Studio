import runpy
from pathlib import Path
from unittest.mock import patch

from app.config import config


ROOT_DIR = Path(__file__).resolve().parent.parent


def test_main_starts_uvicorn_with_runtime_config():
    """
    Điểm khởi động dịch vụ chỉ chịu trách nhiệm chuyển cấu hình chạy cho Uvicorn. Tại đây mock quá trình khởi động máy chủ thực sự,
    vừa tránh việc kiểm thử chiếm dụng cổng, vừa xác nhận địa chỉ lắng nghe, cổng và cấu hình hot reload không bị mất ở tầng khởi tạo.
    """
    with (
        patch.object(config, "listen_host", "127.0.0.1"),
        patch.object(config, "listen_port", 8765),
        patch.object(config, "reload_debug", True),
        patch("uvicorn.run") as run_server,
    ):
        runpy.run_path(str(ROOT_DIR / "main.py"), run_name="__main__")

    run_server.assert_called_once_with(
        app="app.asgi:app",
        host="127.0.0.1",
        port=8765,
        reload=True,
        log_level="warning",
    )
