"""Application implementation - ASGI."""

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.controllers import base
from app.models.exception import HttpException
from app.router import root_api_router
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """Xử lý tập trung các nhật ký tắt và khôi phục quá trình khởi động quy trình API."""
    logger.info("startup event")

    configured_api_key = config.app.get("api_key", "")
    if configured_api_key in (None, ""):
        logger.warning(
            "API key authentication is disabled; keep the API on a trusted network"
        )
    elif isinstance(configured_api_key, str):
        # Chỉ phạm vi bảo vệ được ghi lại và Khóa, độ dài hoặc tóm tắt không được xuất ra để ngăn thông tin đăng nhập vào hệ thống nhật ký.
        logger.info("API key authentication is enabled for /api/v1 and /tasks")
    else:
        logger.error(
            "API key authentication is misconfigured: app.api_key must be a string"
        )

    # Việc xuất bản đa nền tảng được thực hiện bởi nhóm luồng quy trình hiện tại và sẽ không tiếp tục sau khi khởi động lại dịch vụ. Đặt Redis khi khởi động
    # Xác nhận rằng trạng thái hoạt động của quá trình thực thi bị mất đã chuyển thành lỗi để ngăn tác vụ bị xóa vĩnh viễn.
    from app.services import task as task_service

    task_service.recover_interrupted_cross_posts()
    try:
        yield
    finally:
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def parse_cors_allowed_origins(raw_origins: str | None) -> list[str]:
    """Phân tích danh sách trắng nguồn tên miền chéo của trình duyệt.

    CORS chỉ hạn chế JavaScript tên miền chéo trong trình duyệt và không ảnh hưởng đến Curl, Postman và n8n.
    Hoặc SDK máy chủ. Khi không được định cấu hình, một danh sách trống sẽ được trả về, cho biết rằng quyền truy cập tên miền chéo không được phép theo mặc định; người dùng làm
    Khi triển khai một giao diện người dùng web độc lập, hãy kích hoạt nó một cách rõ ràng thông qua ``CORS_ALLOWED_ORIGINS``.
    """

    if not raw_origins:
        return []

    # Xóa khoảng trắng xung quanh các mục được phân tách bằng dấu phẩy và bỏ qua các mục trống, tránh các định dạng biến môi trường phổ biến
    # ``https://a.example, https://b.example,`` tạo ra nguồn gốc không bao giờ có thể khớp được.
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


def configure_cors(instance: FastAPI, allowed_origins: list[str]) -> None:
    """Định cấu hình CORS với danh sách trắng rõ ràng; giữ chính sách cùng nguồn gốc mặc định cho danh sách trống."""

    if not allowed_origins:
        logger.info(
            "browser cross-origin API access is disabled; set "
            "CORS_ALLOWED_ORIGINS to enable trusted origins"
        )
        return

    allow_all_origins = "*" in allowed_origins
    configured_api_key = config.app.get("api_key", "")
    if allow_all_origins and configured_api_key in (None, ""):
        # ``*`` là chế độ tương thích được người dùng lựa chọn rõ ràng, do đó việc khởi động không bị buộc phải từ chối; tuy nhiên, nó không yêu cầu xác thực
        # Ở trạng thái này, nó sẽ cho phép bất kỳ trang web nào đọc và gọi API và phải để lại cảnh báo bảo mật có thể định vị được.
        logger.warning(
            "CORS allows every browser origin while API key authentication is "
            "disabled; configure app.api_key or restrict CORS_ALLOWED_ORIGINS"
        )

    instance.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        # Starlette sẽ phản ánh bất kỳ Nguồn gốc nào khi ``*`` được bật cùng lúc với thông tin xác thực.
        # Chế độ ký tự đại diện không yêu cầu xác thực cookie nên thông tin đăng nhập sẽ chủ động bị tắt; nguồn rõ ràng
        # Hành vi cũ vẫn được giữ lại để tránh ảnh hưởng đến chế độ yêu cầu thông tin xác thực của giao diện người dùng web độc lập hiện có.
        allow_credentials=not allow_all_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        # Khi giao diện người dùng HTTPS từ xa truy cập API cục bộ hoặc API LAN, các trình duyệt hiện đại cũng sẽ gửi thêm
        # Truy cập mạng riêng tư trước. Chỉ những nguồn được đưa vào danh sách trắng chính xác mới có thể được cấp phép;
        # Chế độ ký tự đại diện tiếp tục từ chối, ngăn chặn các trang web tùy tiện thăm dò dịch vụ mạng riêng của người dùng.
        allow_private_network=not allow_all_origins,
    )


def is_browser_origin_allowed(
    request: Request, allowed_origins: list[str]
) -> bool:
    """Xác định xem nguồn yêu cầu của trình duyệt có cùng nguồn gốc hay nguồn thuộc danh sách trắng rõ ràng."""

    origin = request.headers.get("origin")
    if not origin:
        # Curl, Postman, n8n và SDK phía máy chủ thường không gửi Origin. Bảo lưu loại yêu cầu này,
        # Tránh các bản sửa lỗi bảo mật làm thay đổi nhầm hợp đồng gọi của các ứng dụng khách API hiện có.
        return True
    if "*" in allowed_origins or origin in allowed_origins:
        return True

    # Trình duyệt cũng có thể gửi Origin cho POST có cùng nguồn gốc. Chỉ so sánh sơ đồ + thẩm quyền, bỏ qua
    # Các tham số đường dẫn và truy vấn; nếu việc triển khai proxy ngược không chuyển tiếp chính xác lược đồ/máy chủ mạng công cộng, bạn có thể rõ ràng
    # CORS_ALLOWED_ORIGINS khai báo các nguồn bên ngoài để tránh dựa vào các tiêu đề chuyển tiếp không đáng tin cậy.
    request_url = urlsplit(str(request.url))
    request_origin = f"{request_url.scheme}://{request_url.netloc}"
    return origin == request_origin


def configure_browser_access(instance: FastAPI, allowed_origins: list[str]) -> None:
    """Định cấu hình đồng thời chính sách phản hồi CORS của trình duyệt và bảo vệ nguồn gốc phía máy chủ."""

    @instance.middleware("http")
    async def reject_untrusted_browser_origin(request: Request, call_next):
        """Chủ động từ chối các nguồn trình duyệt không đáng tin cậy, đáp ứng các yêu cầu đơn giản mà không cần kiểm tra trước CORS."""

        if not is_browser_origin_allowed(request, allowed_origins):
            origin = request.headers.get("origin", "")
            logger.warning(
                f"blocked untrusted browser origin: method={request.method}, "
                f"path={request.url.path}, origin={origin}"
            )
            return JSONResponse(
                status_code=403,
                content=utils.get_response(
                    status=403,
                    message="cross-origin browser request is not allowed",
                ),
            )

        return await call_next(request)

    # Phần mềm trung gian CORS cuối cùng đã được đăng ký và nằm ở lớp bảo vệ Origin bên ngoài: việc kiểm tra trước đáng tin cậy có thể trực tiếp thành công.
    # Các lượt xem trước không đáng tin cậy sẽ bị CORS từ chối; các yêu cầu thực tế không có kiểm tra trước (preflight) sẽ vẫn được đưa vào bộ phận bảo vệ 403 ở trên.
    configure_cors(instance, allowed_origins)


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    return instance


app = get_application()


@app.middleware("http")
async def protect_generated_task_files(request: Request, call_next):
    """Bảo vệ định tuyến tĩnh của các sản phẩm tác vụ để ngăn tải xuống trực tiếp bỏ qua xác thực API.

    ``/tasks`` được StaticFiles gắn độc lập và không thể sử dụng lại các phần phụ thuộc APIRouter.
    Vì vậy, verify_token tương tự được gọi trong phần mềm trung gian. Chức năng xác thực sẽ được sử dụng nếu nó không được cấu hình
    api_key; Các yêu cầu chiếu trước TÙY CHỌN cũng được dành riêng cho việc xử lý phần mềm trung gian CORS.
    """

    request_path = request.url.path
    is_task_file = request_path == "/tasks" or request_path.startswith("/tasks/")
    if is_task_file and request.method != "OPTIONS":
        try:
            base.verify_token(request)
        except HttpException as exception:
            return exception_handler(request, exception)

    return await call_next(request)


# Theo mặc định, chính sách cùng nguồn gốc của trình duyệt được tuân thủ; tên miền chéo chỉ được bật khi người dùng định cấu hình rõ ràng nguồn trang web đáng tin cậy.
cors_allowed_origins = parse_cors_allowed_origins(
    os.getenv("CORS_ALLOWED_ORIGINS", "")
)
configure_browser_access(app, cors_allowed_origins)

task_dir = utils.task_dir()
app.mount("/tasks", StaticFiles(directory=task_dir, html=True), name="")

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")
