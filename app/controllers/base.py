import secrets
from typing import Annotated
from uuid import uuid4

from fastapi import Header, Request

from app.config import config
from app.models.exception import HttpException

MAX_TASK_ID_LENGTH = 128


def normalize_task_id(value: object) -> str:
    """Return a log-safe request ID, replacing invalid client input with a UUID."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TASK_ID_LENGTH
        or not value.isprintable()
    ):
        return str(uuid4())
    return value


def get_task_id(request: Request) -> str:
    return normalize_task_id(request.headers.get("x-task-id"))


def get_api_key(request: Request):
    api_key = request.headers.get("x-api-key")
    return api_key


def get_api_key_values(request: Request) -> list[str]:
    """Trả về tất cả Tiêu đề khóa API trong yêu cầu, giữ lại các giá trị trùng lặp để xác minh bảo mật."""

    # Tiêu đề Starlette cung cấp getlist(), có thể phân biệt các tiêu đề trùng lặp được gửi bởi proxy hoặc ứng dụng khách.
    # Yêu cầu gấp đôi nhẹ trong các bài kiểm tra đơn vị chỉ sử dụng một lệnh đơn giản, do đó, các dự phòng tương thích được giữ nguyên.
    get_list = getattr(request.headers, "getlist", None)
    if callable(get_list):
        return [value for value in get_list("x-api-key") if isinstance(value, str)]

    api_key = get_api_key(request)
    return [api_key] if isinstance(api_key, str) else []


def verify_token(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
):
    """Xác định xem có xác minh Khóa API theo cấu hình hay không.

    Khóa trống vẫn giữ lại chế độ không cần xác thực cục bộ hiện có; sau khi quản trị viên định cấu hình rõ ràng Khóa không trống, API
    Việc tải xuống sản phẩm nhiệm vụ và định tuyến sẽ yêu cầu khách hàng cung cấp cùng một tiêu đề yêu cầu thông qua tiêu đề yêu cầu ``x-api-key``.
    giá trị. Việc khai báo tham số cũng cho phép Swagger hiển thị tiêu đề yêu cầu để tạo điều kiện gỡ lỗi trong môi trường được bảo vệ.
    """

    configured_key = config.app.get("api_key", "")
    if configured_key in (None, ""):
        return None

    # Các mục cấu hình phải là chuỗi. Ở đây, các loại lỗi như danh sách và số bị từ chối để tránh ẩn ý chuỗi.
    # Việc chuyển đổi tạo ra hành vi xác thực khó phát hiện; thông báo lỗi cũng không chứa khóa thực tế.
    if not isinstance(configured_key, str):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=500,
            message="API authentication is misconfigured",
        )

    # Tham số FastAPI dùng để khai báo x-api-key trong OpenAPI; xác minh thực tế luôn đọc Yêu cầu,
    # Chỉ bằng cách này, tiêu đề có cùng tên mới có thể được gửi đi lặp lại. Tiêu đề trùng lặp cho các cặp proxy thông thường và proxy ngược
    # Thứ tự các giá trị của có thể khác nhau và do đó phải bị từ chối thay vì ngầm lấy giá trị đầu tiên hoặc cuối cùng.
    token_values = get_api_key_values(request)
    if not token_values and isinstance(x_api_key, str):
        token_values = [x_api_key]

    if len(token_values) != 1:
        raise HttpException(
            task_id=get_task_id(request),
            status_code=401,
            message="invalid API key",
        )

    # Compare_digest chỉ hỗ trợ ASCII cho str. Tiêu đề yêu cầu là đầu vào không đáng tin cậy và kẻ tấn công
    # Có thể gửi các ký tự Latin-1 để kích hoạt TypeError. Được mã hóa thống nhất thành byte UTF-8 và được giữ lại
    # So sánh thời gian liên tục, cũng hỗ trợ Khóa Unicode hợp pháp trong TOML.
    token = token_values[0]
    if not secrets.compare_digest(
        token.encode("utf-8"), configured_key.encode("utf-8")
    ):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=401,
            message="invalid API key",
        )

    return None
