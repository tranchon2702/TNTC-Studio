import traceback
from typing import Any

from loguru import logger


class HttpException(Exception):
    def __init__(
        self, task_id: str, status_code: int, message: str = "", data: Any = None
    ):
        self.message = message
        self.status_code = status_code
        self.data = data
        # Retrieve the exception stack trace information.
        tb_str = traceback.format_exc().strip()
        if not tb_str or tb_str == "NoneType: None":
            msg = f"HttpException: {status_code}, {task_id}, {message}"
        else:
            msg = f"HttpException: {status_code}, {task_id}, {message}\n{tb_str}"

        # 400/401 đều là các vấn đề đầu vào của khách hàng được mong đợi. Đặc biệt sau khi bật xác thực, quá trình quét mạng công cộng có thể
        # Một số lượng lớn các khóa không hợp lệ được tạo ra; sử dụng CẢNH BÁO để giữ lại thông tin định vị và tránh làm nhiễm LỖI
        # Báo động. Lỗi cấu hình phía máy chủ và các ngoại lệ khác vẫn là LỖI.
        if status_code in (400, 401):
            logger.warning(msg)
        else:
            logger.error(msg)


class FileNotFoundException(Exception):
    pass
