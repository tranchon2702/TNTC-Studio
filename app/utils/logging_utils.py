import os
import threading

from loguru import logger


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)
LOG_RECORD_FORMAT = (
    "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
    "<level>{level}</> | "
    '"{file.path}:{line}":<blue> {function}</> '
    "- <level>{message}</>\n"
)
# Khi Loguru khởi động, handler terminal mặc định có ID là 0. Khi WebUI tải lại chỉ được thay thế
# đầu ra terminal cơ bản này, không được gọi logger.remove() để xóa toàn bộ handler, nếu không các sink
# tạm thời thu thập nhật ký WebUI của các tác vụ đang chạy cũng sẽ bị xóa bỏ.
_terminal_handler_id: int | None = 0
_terminal_handler_lock = threading.RLock()


def _project_relative_path(file_path):
    """
    Rút ngắn đường dẫn tuyệt đối thành đường dẫn tương đối của dự án bắt đầu bằng ``./`` và luôn dùng dấu gạch chéo thuận.

    Trên Windows, dự án có thể khởi động qua ổ đĩa mạng ánh xạ hoặc ổ đĩa ``subst``. Khi đó đường dẫn trong ngăn xếp gọi
    vẫn là ``X:\\MoneyPrinterTurbo\\...``, trong khi ``PROJECT_ROOT`` qua ``realpath``
    phân giải lại thành ``C:\\...``, khiến ``os.path.relpath`` ném thẳng ``ValueError``.
    Nếu hàm định dạng ném lỗi, loguru sẽ bắt lấy và hủy bỏ toàn bộ bản ghi, bảng nhật ký trên terminal và WebUI sẽ
    đồng thời bị trống, vì vậy ở đây bắt buộc phải fallback trả về đường dẫn gốc. Tương tự với các tệp ngoài thư mục dự án:
    việc ghép ``./`` vào đường dẫn truy vết ngược ``..`` sẽ chỉ tạo ra kết quả khó đọc hơn.
    """
    try:
        relative_path = os.path.relpath(file_path, PROJECT_ROOT)
    except ValueError:
        return file_path
    if relative_path == os.pardir or relative_path.startswith(os.pardir + os.sep):
        return file_path
    # Hàm relpath của Windows trả về đường dẫn ngăn cách bằng dấu gạch chéo ngược, nếu ghép trực tiếp sẽ thành ``./app\\utils``
    # kiểu phân cách hỗn hợp này không nhất quán với nhật ký trên các nền tảng khác.
    return f"./{relative_path.replace(os.sep, '/')}"


def format_log_record(record):
    """
    Định dạng thống nhất nhật ký terminal và WebUI.

    Loguru sẽ chuyển cùng một bản ghi cho nhiều sink. Sink đầu tiên có thể đã chuyển đổi đường dẫn tuyệt đối
    thành đường dẫn tương đối của dự án, vì vậy tại đây tương thích đồng thời cả đường dẫn tuyệt đối và đường dẫn đã định dạng bắt đầu bằng ``./``.
    Sink WebUI sẽ tắt màu sắc, nhưng thời gian, cấp độ, vị trí gọi và nội dung thông báo vẫn giữ nhất quán với terminal.
    """
    file_path = record["file"].path
    if os.path.isabs(file_path):
        record["file"].path = _project_relative_path(file_path)

    # Thông điệp nhật ký đôi khi chứa đường dẫn tuyệt đối của tệp tác vụ. Việc rút ngắn thống nhất thành đường dẫn tương đối của dự án có thể
    # tránh việc WebUI và terminal hiển thị hai nội dung khác nhau do điểm khởi tạo khác nhau.
    record["message"] = record["message"].replace(PROJECT_ROOT, ".")
    return LOG_RECORD_FORMAT


def configure_terminal_logger(sink, level: str, colorize: bool = True) -> int:
    """
    Thay thế an toàn handler nhật ký terminal ở cấp tiến trình, đồng thời giữ lại handler dành riêng cho tác vụ.

    Streamlit khi hot reload mã nguồn hoặc hết hạn cache có thể thực thi lại việc khởi tạo nhật ký. Tại đây chỉ xóa chính xác
    đầu ra terminal cũ theo handler ID đã ghi nhận, do đó sẽ không làm gián đoạn nhật ký WebUI đang được ghi bởi các tác vụ nền.
    Khóa (lock) dùng để bảo vệ việc cập nhật ID khi nhiều phiên trình duyệt khởi tạo đồng thời.
    """
    global _terminal_handler_id

    with _terminal_handler_lock:
        if _terminal_handler_id is not None:
            try:
                logger.remove(_terminal_handler_id)
            except ValueError:
                # Kiểm thử hoặc điểm vào bên ngoài có thể đã gỡ bỏ handler này. Tiếp tục tạo đầu ra terminal mới
                # mà không làm ảnh hưởng đến các sink nhật ký khác vẫn đang hoạt động.
                pass

        _terminal_handler_id = logger.add(
            sink,
            level=level,
            format=format_log_record,
            colorize=colorize,
        )
        return _terminal_handler_id
