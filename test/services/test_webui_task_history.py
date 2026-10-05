import ast
import os
import re
from collections.abc import Mapping
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
TASK_HISTORY_HELPERS = {
    "_find_final_task_video",
    "_build_video_download_name",
    "_build_restore_upload_requirements",
    "_get_unmet_restore_upload_requirements",
}
TASK_HISTORY_CONSTANTS = {
    "_FINAL_VIDEO_PATTERN",
    "_DOWNLOAD_FILENAME_INVALID_PATTERN",
    "_WINDOWS_RESERVED_FILENAMES",
    "VOICE_MODE_TTS",
    "VOICE_MODE_UPLOAD",
    "VOICE_MODE_NONE",
}


def _load_task_history_helpers():
    """
    Cô lập và tải các hàm thuần túy trong lịch sử tác vụ không dựa vào Streamlit khỏi mục nhập WebUI.

    Việc nhập trực tiếp Main.py sẽ thực hiện một bộ kết xuất trang hoàn chỉnh. Bài kiểm tra chỉ biên dịch các hằng số và hàm mục tiêu, cả hai đều xác minh
    Việc triển khai thực tế sau khi hợp nhất cũng tránh được việc phải tháo rời lại một mô-đun sản xuất chỉ có một số chức năng để kiểm thử đơn vị.
    """
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    selected_nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in TASK_HISTORY_CONSTANTS
            for target in node.targets
        ):
            selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in TASK_HISTORY_HELPERS:
            selected_nodes.append(node)

    namespace = {"os": os, "re": re, "Mapping": Mapping}
    module = ast.fix_missing_locations(ast.Module(body=selected_nodes, type_ignores=[]))
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


TASK_HISTORY_NAMESPACE = _load_task_history_helpers()
find_final_task_video = TASK_HISTORY_NAMESPACE["_find_final_task_video"]
build_video_download_name = TASK_HISTORY_NAMESPACE["_build_video_download_name"]
windows_reserved_filenames = TASK_HISTORY_NAMESPACE["_WINDOWS_RESERVED_FILENAMES"]
build_restore_upload_requirements = TASK_HISTORY_NAMESPACE[
    "_build_restore_upload_requirements"
]
get_unmet_restore_upload_requirements = TASK_HISTORY_NAMESPACE[
    "_get_unmet_restore_upload_requirements"
]


def test_find_final_task_video_ignores_intermediate_files(tmp_path):
    """Lịch sử tác vụ chỉ có thể nhận dạng các phần cuối cùng đã hoàn thành và không thể sử dụng các tệp trung gian tổng hợp."""
    for file_name in (
        "combined-1.mp4",
        "temp-clip-1.mp4",
        "final-1TEMP_MPY_wvf_snd.mp4",
    ):
        (tmp_path / file_name).touch()

    assert find_final_task_video(str(tmp_path)) == ""


def test_find_final_task_video_returns_first_numbered_output(tmp_path):
    """Tác vụ nhiều phim nhất quán với kết quả thời gian chạy và video cuối cùng có số thứ tự nhỏ nhất sẽ được phát theo mặc định."""
    (tmp_path / "final-10.mp4").touch()
    (tmp_path / "final-2.mp4").touch()
    (tmp_path / "final-1.mp4").touch()

    assert find_final_task_video(str(tmp_path)) == str(tmp_path / "final-1.mp4")


def test_build_video_download_name_uses_subject_and_output_index():
    assert (
        build_video_download_name("A day: in / Shanghai?", 2, 3)
        == "A day in Shanghai-2.mp4"
    )


def test_build_video_download_name_handles_empty_and_long_subjects():
    assert build_video_download_name("  ...  ", 1, 1) == "video.mp4"
    assert len(build_video_download_name("a" * 100, 1, 1)) == 84


def test_build_video_download_name_avoids_windows_reserved_names():
    # Sử dụng danh sách rõ ràng các quy tắc chính thức để tránh phạm vi kiểm tra/mức độ hiểu sao chép mã sản xuất;
    # Nếu quá trình triển khai ghi sai phạm vi thì xác nhận đẳng thức đã đặt sẽ thất bại ngay lập tức thay vì bị bỏ sót trong quá trình triển khai.
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "COM¹",
        "COM²",
        "COM³",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
    assert windows_reserved_filenames == reserved_names

    for reserved_name in reserved_names:
        assert (
            build_video_download_name(reserved_name.lower(), 1, 1)
            == f"_{reserved_name.lower()}.mp4"
        )
        assert (
            build_video_download_name(f"{reserved_name}.topic", 2, 3)
            == f"_{reserved_name}.topic-2.mp4"
        )

    assert build_video_download_name("con .topic", 1, 1) == "_con .topic.mp4"


def test_build_video_download_name_does_not_overmatch_similar_names():
    """Chỉ xử lý các tên dành riêng thực sự của Windows và không vô tình làm hỏng các chủ đề thông thường liền kề nhưng hợp pháp."""

    for safe_name in ("COM0", "COM10", "LPT0", "LPT10", "COM⁴", "LPT⁴"):
        assert build_video_download_name(safe_name, 1, 1) == f"{safe_name}.mp4"


def test_restore_requirements_block_missing_uploaded_files():
    params = {
        "video_source": "local",
        "custom_audio_file": "/old-task/custom-audio.wav",
        "voice_name": "zh-CN-XiaoxiaoNeural-Female",
    }
    requirements = build_restore_upload_requirements(params)

    assert get_unmet_restore_upload_requirements(
        requirements,
        video_source="local",
        voice_name=params["voice_name"],
        has_local_materials=False,
        has_custom_audio=False,
    ) == {"local_materials", "custom_audio"}


def test_restore_requirements_allow_explicit_replacements():
    requirements = build_restore_upload_requirements(
        {
            "video_source": "local",
            "custom_audio_file": "/old-task/custom-audio.wav",
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
    )

    assert not get_unmet_restore_upload_requirements(
        requirements,
        video_source="pexels",
        voice_name="en-US-JennyNeural-Female",
        has_local_materials=False,
        has_custom_audio=False,
    )


def test_restore_requirements_require_file_in_upload_voice_mode():
    """Khi tiếp tục tải lên các tác vụ lồng tiếng, bạn phải chọn lại các tệp âm thanh để tiếp tục sử dụng chế độ tải lên."""
    requirements = build_restore_upload_requirements(
        {
            "video_source": "pexels",
            "custom_audio_file": "/old-task/custom-audio.wav",
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
    )

    assert get_unmet_restore_upload_requirements(
        requirements,
        video_source="pexels",
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        has_local_materials=False,
        has_custom_audio=False,
        voice_mode="upload",
    ) == {"custom_audio"}


def test_restore_requirements_allow_replacing_upload_with_other_voice_modes():
    """Khi người dùng chủ động chuyển sang lồng tiếng tự động hoặc không lồng tiếng, các tệp lịch sử đã tải lên sẽ không còn bị buộc phải khôi phục nữa."""
    requirements = build_restore_upload_requirements(
        {
            "video_source": "pexels",
            "custom_audio_file": "/old-task/custom-audio.wav",
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
    )

    for voice_mode in ("tts", "none"):
        assert not get_unmet_restore_upload_requirements(
            requirements,
            video_source="pexels",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
            has_local_materials=False,
            has_custom_audio=False,
            voice_mode=voice_mode,
        )
