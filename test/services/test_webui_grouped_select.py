from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import voice


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


class _GroupedSelectHarness:
    """Chỉ thay thế thành phần nguồn video, giữ nguyên việc triển khai thực sự phiên bản v2 của các Thành phần khác trong trang."""

    def __init__(self):
        self.selected = None
        self.calls = []
        self.declaration = None
        self._original_component = st.components.v2.component

    def declare(self, name, *args, **kwargs):
        # Các thành phần của bên thứ ba như hướng dẫn cho người mới sử dụng cũng sử dụng Thành phần v2. Truyền tải những tuyên bố này tránh
        # Sơ khai kiểm tra thay đổi các chức năng khác của trang và chỉ kiểm soát kết quả lựa chọn nguồn video mà trường hợp sử dụng này quan tâm.
        if name != "mpt_grouped_select":
            return self._original_component(name, *args, **kwargs)

        self.declaration = kwargs

        def render(**render_kwargs):
            self.calls.append(render_kwargs)
            return SimpleNamespace(selected=self.selected)

        return render


@contextmanager
def _running_app(harness, *, saved_video_source="pexels"):
    """Giữ các thành phần, cấu hình và truy vấn bản vá bên ngoài được tách biệt trong suốt trường hợp sử dụng."""
    test_app_config = dict(config.app, video_source=saved_video_source)
    test_ui_config = dict(config.ui, language="en")
    with (
        patch(
            "streamlit.components.v2.component",
            side_effect=harness.declare,
        ),
        patch.object(config, "app", test_app_config),
        patch.object(config, "ui", test_ui_config),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(
            voice,
            "get_all_azure_voices",
            return_value=["en-US-JennyNeural-Female"],
        ),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        yield app


def test_grouped_video_source_applies_first_change_and_allows_switching_back():
    """Sự kiện thay đổi sẽ cập nhật trạng thái doanh nghiệp và không thể yêu cầu người dùng thực hiện các lựa chọn lặp lại."""
    harness = _GroupedSelectHarness()
    with _running_app(harness) as app:
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"

        harness.selected = "pixabay"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert app.session_state["video_source_select_en"] == "pixabay"
        # grouped_selectbox sẽ chủ động chạy lại trong vòng sự kiện; kết xuất cuối cùng phải đặt giá trị mới
        # Được chuyển trở lại giao diện người dùng, nếu không thành phần này vẫn có thể bị ghi đè bởi dữ liệu cũ.
        assert harness.calls[-1]["data"]["value"] == "pixabay"

        harness.selected = "pexels"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"


def test_grouped_video_source_ignores_unknown_event_and_repairs_saved_value():
    """Cả cấu hình đã hết hạn và sự kiện giả mạo đều không thể khiến trang chuyển sang trạng thái nguồn không xác định."""
    harness = _GroupedSelectHarness()
    with _running_app(harness, saved_video_source="removed-provider") as app:
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"

        harness.selected = "unknown-provider"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"


def test_grouped_video_source_keeps_groups_and_accessible_label_binding():
    """Dữ liệu thành phần phải duy trì thứ tự nhóm và cung cấp ID kiểm soát ổn định cho các nhãn hiển thị."""
    harness = _GroupedSelectHarness()
    with _running_app(harness):
        data = harness.calls[-1]["data"]
        assert data["controlId"] == "video_source_select_en_control"
        assert [group["label"] for group in data["groups"]] == [
            "Stock Video",
            "AI Video",
            "AI Image",
            "Local Files",
        ]
        assert [
            option["value"] for group in data["groups"] for option in group["options"]
        ] == [
            "pexels",
            "pixabay",
            "coverr",
            "metaso_minimax",
            "ofox",
            "loomloom",
            "volcengine_seedance",
            "wavespeed",
            "openai_image",
            "local",
        ]

        # AppTest hiện không hiển thị DOM bên trong của Components v2 nên cũng xác minh việc khai báo thành phần
        # Sử dụng controlId để liên kết nhãn/chọn và cho phép các hàng nhãn bao bọc một cách tự nhiên trong màn hình hẹp.
        assert "label.htmlFor = data.controlId" in harness.declaration["js"]
        assert "select.id = data.controlId" in harness.declaration["js"]
        assert "flex-wrap: wrap" in harness.declaration["css"]
