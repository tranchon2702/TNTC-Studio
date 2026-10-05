import threading
from collections import deque

from loguru import logger

from app.config import config
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as tm
from app.services.loomloom import LoomLoomConfirmedVideoRequest
from app.utils.logging_utils import format_log_record


# Cấu hình của WebUI được lưu trữ trong từ điển toàn cầu ở cấp quy trình. Việc triển khai đồng bộ hóa ban đầu sẽ được duy trì trong quá trình xây dựng đầy đủ
# run_config_lock, vì vậy các phiên trình duyệt khác nhau thực sự được thực thi tuần tự. Ở đây số lượng đồng thời được cố định
# Nó là 1, không chỉ duy trì tính nhất quán của cấu hình ban đầu mà còn ngăn nhiều luồng chờ đợi một cách vô nghĩa bên ngoài khóa cấu hình.
_task_manager = InMemoryTaskManager(
    max_concurrent_tasks=1,
    max_queued_tasks=max(1, int(config.app.get("max_queued_tasks", 100))),
)
_task_logs: dict[str, deque[str]] = {}
_task_logs_lock = threading.RLock()
_MAX_LOG_TASKS = 20
_MAX_LOG_RECORDS_PER_TASK = 1000
# Streamlit không thể trực tiếp đẩy các bản cập nhật thành phần theo các luồng nền mà chỉ có thể được thăm dò thông qua Fragment. 0,5 giây
# Chỉ cần làm cho nhật ký WebUI gần với đầu ra thời gian thực của thiết bị đầu cuối là đủ, nhưng sẽ không tiếp tục chiếm tài nguyên trình duyệt như làm mới tần số cao.
TASK_LOG_REFRESH_INTERVAL_SECONDS = 0.5


def _append_task_log(task_id: str, message: str) -> None:
    """Lưu một số lượng nhật ký giới hạn cho mỗi tác vụ để bỏ phiếu an toàn bằng Streamlit Fragments."""
    with _task_logs_lock:
        records = _task_logs.get(task_id)
        if records is None:
            # Chỉ giữ nhật ký của các tác vụ gần đây nhất để ngăn dịch vụ WebUI tiếp tục chiếm bộ nhớ sau khi chạy trong một thời gian dài.
            # dict duy trì thứ tự chèn; nhật ký tác vụ chỉ được sử dụng để chẩn đoán giao diện và việc loại bỏ bản ghi sớm nhất không ảnh hưởng đến tác vụ.
            if len(_task_logs) >= _MAX_LOG_TASKS:
                oldest_task_id = next(iter(_task_logs))
                _task_logs.pop(oldest_task_id, None)
            records = deque(maxlen=_MAX_LOG_RECORDS_PER_TASK)
            _task_logs[task_id] = records
        records.append(message.rstrip())


def get_task_logs(task_id: str) -> list[str]:
    """Trả lại ảnh chụp nhanh nhật ký để tránh giữ các khóa được sử dụng bởi các luồng nền trong quá trình hiển thị trang."""
    with _task_logs_lock:
        return list(_task_logs.get(task_id, ()))


def _run_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
) -> dict:
    """
    Thực thi đường dẫn video hiện có trong chuỗi nền.

    Phần chìm của Loguru là tài nguyên cấp quy trình, vì vậy nó phải được lọc theo luồng công việc hiện tại. Nếu không thì chạy đồng thời
    Nhiệm vụ API hoặc nhật ký trang khác được trộn lẫn với nhiệm vụ hiện tại. Trang chỉ đọc ảnh chụp nhanh danh sách thông thường và không đọc từ nền
    Chuỗi truy cập Streamlit session_state để tránh nguyên nhân cốt lõi gây nhầm lẫn đường dẫn delta trong quá trình làm mới.
    """
    log_handler_id = None
    worker_thread_id = threading.get_ident()
    try:
        if capture_logs:
            log_handler_id = logger.add(
                lambda message: _append_task_log(task_id, str(message)),
                level="DEBUG",
                format=format_log_record,
                colorize=False,
                filter=lambda record: record["thread"].id == worker_thread_id,
            )

        # Tác vụ đầy đủ vẫn sử dụng khóa cấu hình ban đầu, ngăn phiên WebUI khác sửa đổi nó trong quá trình xây dựng
        # Các cấu hình cấp quy trình như nhà cung cấp và khóa khiến các cài đặt khác nhau được sử dụng trước và sau cùng một video.
        with config.runtime_config_lock():
            return tm.start(
                task_id=task_id,
                params=params,
                voice_preview=voice_preview,
                loomloom_video_request=loomloom_video_request,
            )
    except Exception as exc:
        # tm.start đã chịu trách nhiệm chuyển đổi các ngoại lệ của quy trình thành trạng thái lỗi; ở đây bồn rửa nhật ký bảo vệ bổ sung,
        # Các lớp bao bọc WebUI như khóa cấu hình. Bất kỳ ngoại lệ luồng nền nào đều phải rời khỏi trạng thái cuối cùng và không thể để tác vụ
        # Người quản lý tiếp tục hiển thị "Tòa nhà" vĩnh viễn sau khi thoát khỏi chuỗi công việc.
        error = f"{type(exc).__name__}: {exc}"
        failure = {
            "task_id": task_id,
            "state": const.TASK_STATE_FAILED,
            "progress": 0,
            "failed_stage": "webui_worker",
            "error": error,
        }
        sm.state.update_task(
            task_id,
            state=failure["state"],
            progress=failure["progress"],
            failed_stage=failure["failed_stage"],
            error=failure["error"],
        )
        logger.exception(
            f"unexpected WebUI generation worker failure, "
            f"task_id={task_id}, error={exc}"
        )
        return failure
    finally:
        if log_handler_id is not None:
            try:
                logger.remove(log_handler_id)
            except ValueError:
                logger.debug(
                    f"WebUI task log handler already removed: task_id={task_id}"
                )


def submit_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool = True,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
) -> None:
    """
    Đăng ký và gửi tác vụ tạo video WebUI và quay lại ngay sau cuộc gọi.

    Trạng thái tác vụ phải được viết trước khi chuỗi được bắt đầu. Bằng cách này, tác vụ có thể được truy vấn khi quá trình thực thi tập lệnh hiện tại của trang kết thúc.
    Việc làm mới trình duyệt hoặc kết nối lại WebSocket cũng không dựa vào phần giữ chỗ cho các trang cũ trong bộ nhớ.
    """
    task_params = params.model_copy(deep=True)
    # Tải trọng xem trước chỉ chứa các đường dẫn âm thanh không thể thay đổi, ảnh chụp nhanh thông số và dòng thời gian phụ đề chỉ đọc. Sao chép từ điển bên ngoài,
    # Điều này ngăn các lần chạy lại trang tiếp theo ảnh hưởng đến các tác vụ đã được gửi tới hàng đợi nền khi thay thế các trường được lưu trong bộ nhớ đệm.
    voice_preview_snapshot = dict(voice_preview) if voice_preview else None
    # Các yêu cầu đã được xác nhận là các đối tượng dữ liệu đã được cố định và chỉ được gửi trong quy trình hiện tại. Khóa API sẽ không nhập
    # VideoParams, trạng thái tác vụ, nhật ký hoặc lịch sử vị trí ổ đĩa sẽ không bị ảnh hưởng khi chạy lại trang tiếp theo.
    loomloom_request_snapshot = loomloom_video_request
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=0,
        video_subject=task_params.video_subject or task_params.video_script or task_id,
    )
    try:
        _task_manager.add_task(
            _run_generation,
            task_id=task_id,
            params=task_params,
            capture_logs=capture_logs,
            voice_preview=voice_preview_snapshot,
            loomloom_video_request=loomloom_request_snapshot,
        )
    except Exception as exc:
        # Các lỗi lập lịch, như lỗi đường ống, phải có thể truy vấn được để tránh hiển thị vĩnh viễn trong trình quản lý tác vụ.
        # "Tạo ra". Việc duy trì các loại ngoại lệ giúp dễ dàng xác định nhanh chóng các vấn đề về hàng đợi từ Docker hoặc nhật ký gốc.
        error = f"{type(exc).__name__}: {exc}"
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            failed_stage="scheduling",
            error=error,
        )
        logger.exception(
            f"failed to submit WebUI generation task, task_id={task_id}, error={exc}"
        )
        raise
