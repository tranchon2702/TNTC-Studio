"""Đọc và ghi an toàn các tập tin liên tục trong thư mục tác vụ."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from app.utils import utils


def _script_file(task_id: str) -> Path:
    """Trả về đường dẫn tệp kê khai tập lệnh tác vụ và sử dụng lại logic tạo thư mục tác vụ thống nhất."""
    return Path(utils.task_dir(task_id)) / "script.json"


def _write_json_atomic(target: Path, payload: Mapping[str, Any]) -> None:
    """
    Viết JSON nguyên tử vào thư mục đích để tránh gián đoạn quá trình và để lại một nửa tệp.

    Tệp tạm thời và tệp đích phải nằm trong cùng một thư mục để đảm bảo rằng ``os.replace``
    Ngữ nghĩa thay thế nguyên tử được duy trì trong hệ thống tệp cục bộ và thư mục gắn kết Docker. Nó sẽ không được sửa đổi cho đến khi ghi thành công.
    Các tập tin hiện có; trong trường hợp ngoại lệ, chỉ các tệp tạm thời được tạo lần này sẽ được dọn sạch và lỗi sẽ được chuyển cho người gọi để quyết định xem có
    Ảnh hưởng đến quá trình chính.
    """
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                payload,
                temp_file,
                ensure_ascii=False,
                indent=4,
                default=lambda value: value.__dict__,
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_script_data(task_id: str, payload: Mapping[str, Any]) -> None:
    """Tạo hoặc thay thế hoàn toàn tệp kê khai ``script.json`` của tác vụ."""
    _write_json_atomic(_script_file(task_id), payload)


def patch_script_data(task_id: str, **updates: Any) -> bool:
    """
    Bổ sung danh sách nhiệm vụ trong khi giữ lại các trường ban đầu và trả về Sai nếu không thành công.

    Nguồn của tài liệu là thông tin chẩn đoán phụ trợ và không thể do quyền của tệp, các bất thường của đĩa tạm thời hoặc hư hỏng tệp lịch sử gây ra.
    Chặn việc tạo video. Do đó, mục này sẽ ghi lại ngoại lệ hoàn chỉnh và phân hủy; khi danh sách nhiệm vụ được tạo lần đầu tiên, nó vẫn sẽ được sử dụng.
    ``write_script_data``, quy trình chính quyết định cách xử lý khi dữ liệu tác vụ cơ bản không được ghi.
    """
    try:
        target = _script_file(task_id)
        with target.open("r", encoding="utf-8") as script_file:
            payload = json.load(script_file)
        if not isinstance(payload, dict):
            raise ValueError("task script data must be a JSON object")

        payload.update(updates)
        _write_json_atomic(target, payload)
        return True
    except FileNotFoundError:
        # ``download_videos`` cũng có thể được gọi độc lập bằng các bài kiểm tra, tập lệnh hoặc mã của bên thứ ba. Tại thời điểm này, không có
        # Danh sách việc cần làm là tình huống bình thường và không được tạo cảnh báo hoặc tạo tệp không đầy đủ cho các bản ghi phụ.
        logger.debug(
            f"skip task script update because script.json does not exist: "
            f"task_id={task_id}"
        )
        return False
    except Exception as exc:
        logger.warning(
            "failed to update task script data: "
            f"task_id={task_id}, fields={sorted(updates)}, "
            f"error={type(exc).__name__}, detail={exc}"
        )
        return False
