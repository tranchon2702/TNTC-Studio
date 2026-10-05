import math
import os
import time
from typing import Any, Mapping
from urllib.parse import quote_plus

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect


DEFAULT_BASE_URL = "https://api.ofox.ai/v1"
DEFAULT_MODEL_ID = "bytedance/seedance-2.0-fast"
DEFAULT_RESOLUTION = "720p"
# Kênh nhà sản xuất quốc tế được cố định theo mặc định: chính sách nội dung nhất quán hơn khi hướng tới khán giả toàn cầu; nếu cấu hình rõ ràng trống, nó sẽ được trả về.
# Cổng được phân phối giữa các nhà cung cấp có sẵn theo trọng lượng.
DEFAULT_PROVIDER_TYPE = "byteplus"
# Mô hình mặc định byteance/seedance-2.0-fast chỉ chấp nhận 4-15 giây (giá trị xác minh được đo phía máy chủ).
# Các mô hình tùy chọn khác có các khoảng thời gian khác nhau (ví dụ: alibaba/wan-2.7 là 2-15 giây) và chúng phải được đồng bộ hóa khi chuyển đổi mô hình.
# Điều chỉnh phạm vi trong cấu hình; các yêu cầu nằm ngoài phạm vi sẽ bị API từ chối với số điểm rõ ràng là 400 và sẽ không bị tính phí.
DEFAULT_MIN_DURATION_SECONDS = 4
DEFAULT_MAX_DURATION_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
MAX_POLL_RETRIES = 5
RETRY_BASE_SECONDS = 1.0
MAX_ERROR_TEXT_LENGTH = 500
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
TERMINAL_SUCCESS_STATUSES = frozenset({"completed", "succeeded"})
TERMINAL_FAILURE_STATUSES = frozenset(
    {"failed", "error", "cancelled", "canceled", "expired"}
)
# Đường dẫn thành công chính thức: đang chờ xử lý (việc mua lại sẽ được gửi bởi thượng nguồn) → được xếp hàng → in_progress → đã hoàn thành
ACTIVE_STATUSES = frozenset({"pending", "queued", "in_progress"})


class OFoxError(RuntimeError):
    """Lỗi cấu hình, yêu cầu hoặc phản hồi xác định."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        # Miễn là tác vụ từ xa đã được tạo, tất cả các loại lỗi đều mang ID tác vụ. Không cần trình độ cao hơn
        # Logic khôi phục được duy trì riêng biệt theo các danh mục con ngoại lệ và WebUI/API cũng có thể hiển thị ổn định cơ sở để khắc phục sự cố.
        self.task_id = task_id


class OFoxUnconfirmedTaskError(OFoxError):
    """Đầu từ xa có thể đã tạo một tác vụ phải trả phí nhưng máy cục bộ không thể xác nhận trạng thái cuối cùng của tác vụ đó."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message, task_id=task_id)


class OFoxDownloadError(OFoxError):
    """Tác vụ thanh toán từ xa đã thành công nhưng video được tạo không tải được xuống máy cục bộ."""

    def __init__(self, message: str, task_id: str):
        super().__init__(message, task_id=task_id)


def get_api_key(settings: Mapping[str, Any] | None = None) -> str:
    """
    Đọc thông tin đăng nhập OFox với mức độ ưu tiên rõ ràng và duy nhất.

    Khóa riêng trong tệp cấu hình có mức độ ưu tiên cao nhất; các biến môi trường thời gian chạy được hỗ trợ duy nhất rõ ràng về mặt ngữ nghĩa
    ``OFOX_API_KEY``。
    """
    settings = config.app if settings is None else settings
    configured = str(settings.get("ofox_api_key", "") or "").strip()
    environment_key = os.getenv("OFOX_API_KEY", "").strip()
    return configured or environment_key


def is_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    return bool(get_api_key(settings))


def _base_url() -> str:
    return str(
        config.app.get("ofox_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    ).rstrip("/")


def _model_id() -> str:
    return str(
        config.app.get("ofox_text_to_video_model", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID
    ).strip()


def _resolution() -> str:
    """
    Đọc độ phân giải thế hệ.

    OFox xác minh độ phân giải ở phía máy chủ theo mô hình (ví dụ: mô hình nhanh Seedance mặc định chỉ chấp nhận
    480p/720p), các giá trị không hợp lệ sẽ bị từ chối 400 rõ ràng và sẽ không có tác vụ trả phí nào được tạo,
    Do đó, chỉ có các khoảng trống được xóa ở đây và danh sách trắng cục bộ không được duy trì - danh sách trắng sẽ được cập nhật với thư mục mô hình từ xa.
    Hết hạn do thay đổi. Giá trị NULL trở về mặc định để tránh gửi chuỗi trống đến đầu từ xa.
    """
    value = str(config.app.get("ofox_resolution", DEFAULT_RESOLUTION) or "").strip()
    return value or DEFAULT_RESOLUTION


def _provider_type() -> str:
    """
    Đọc định tuyến của nhà cung cấp ngược dòng (định tuyến của nhà cung cấp).

    Một số mẫu OOX được cung cấp bởi nhiều nhà sản xuất thượng nguồn (chẳng hạn như dòng volcengine và Seedance
    byteplus), mỗi nhà sản xuất đều có chính sách nội dung và tính khả dụng theo khu vực riêng. Mặc định được ghim vào byteplus
    (Các nhà sản xuất quốc tế có chính sách nội dung nhất quán hơn dành cho khán giả toàn cầu và lộ trình có thể dự đoán được); cấu hình tên của các nhà sản xuất khác
    Nếu nó được cấu hình rõ ràng dưới dạng một chuỗi trống, nó sẽ không được ghim và sẽ được cổng phân phối theo trọng lượng. Nhà sản xuất trái phép
    Tên sẽ bị API từ chối với 400 ``invalid_provider_type`` độc quyền và sẽ không có khoản thanh toán nào được thực hiện.
    nhiệm vụ, do đó danh sách trắng của nhà cung cấp không được duy trì cục bộ.
    """
    value = config.app.get("ofox_provider", DEFAULT_PROVIDER_TYPE)
    if value is None:
        return DEFAULT_PROVIDER_TYPE
    return str(value).strip()


def _config_bool(key: str, default: bool) -> bool:
    value = config.app.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _bounded_float(key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.app.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)


def _duration_bounds() -> tuple[int, int]:
    def read(key: str, default: int) -> int:
        try:
            value = int(config.app.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 1 else default

    minimum = read("ofox_min_duration", DEFAULT_MIN_DURATION_SECONDS)
    maximum = read("ofox_max_duration", DEFAULT_MAX_DURATION_SECONDS)
    return minimum, max(minimum, maximum)


def _tls_verify() -> bool:
    return _config_bool("tls_verify", True)


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _redact_secret(value: Any, secret: str) -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "***")
        encoded = quote_plus(secret)
        if encoded != secret:
            text = text.replace(encoded, "***")
    for proxy_url in config.proxy.values():
        proxy_secret = str(proxy_url or "")
        if proxy_secret:
            text = text.replace(proxy_secret, "***")
    return text[:MAX_ERROR_TEXT_LENGTH]


def _response_error(response: Any, api_key: str) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {_status_code(response)}"
    if not isinstance(payload, dict):
        return f"HTTP {_status_code(response)}"
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code") or payload.get("code")
        message = error.get("message") or payload.get("message")
    else:
        code = payload.get("code")
        message = payload.get("message") or error
    detail = ": ".join(str(item) for item in (code, message) if item not in (None, ""))
    return _redact_secret(detail or f"HTTP {_status_code(response)}", api_key)


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    response = getattr(error, "response", None)
    return response is not None and _status_code(response) in RETRYABLE_STATUS_CODES


def generate_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    """Gửi bài tập video OOX và đợi địa chỉ kết quả có thể tải xuống."""
    api_key = get_api_key()
    if not api_key:
        raise OFoxError("OFox video generation requires an OFox API key")

    term = str(search_term or "").strip()
    if not term:
        # Các từ nhắc trống có thể đến từ các ngoại lệ phân tách tập lệnh ngược dòng. Các nguồn tạo trả phí không thể gửi nó đến đầu xa,
        # Ngược lại, ngay cả khi giao diện chấp nhận yêu cầu, bạn sẽ chỉ nhận được những video không thể sử dụng được và bị tính phí.
        raise OFoxError("OFox search term must not be empty")

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    requested_duration = max(int(minimum_duration), 1)
    minimum, maximum = _duration_bounds()
    duration = min(max(requested_duration, minimum), maximum)
    if duration != requested_duration:
        # Việc tạo dài hơn yêu cầu sẽ không ảnh hưởng tới phim cuối cùng: quá trình biên tập vẫn được cắt bớt theo thời lượng của clip; tạo ra lâu hơn yêu cầu
        # Ngắn hơn chỉ xảy ra khi yêu cầu vượt quá giới hạn trên của mô hình và nó chỉ có thể hội tụ đến giới hạn trên tại thời điểm này.
        logger.info(
            f"ofox clip duration clamped to the configured model range: "
            f"requested={requested_duration}s, using={duration}s "
            f"(configured {minimum}-{maximum}s)"
        )
    payload = {
        "model": _model_id(),
        "prompt": term,
        "duration": duration,
        "resolution": _resolution(),
        "aspect_ratio": aspect.value,
    }
    provider_type = _provider_type()
    if provider_type:
        payload["provider"] = {"type": provider_type}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    videos_url = f"{_base_url()}/videos"
    logger.info(
        "generating video with OFox: "
        f"model={payload['model']}, term={term!r}, duration={duration}s"
    )

    # Giao diện gửi không tự động thử lại: thời gian chờ hoặc 5xx có thể xảy ra sau khi tác vụ trả phí được tạo.
    # Việc thử lại một cách mù quáng sẽ dẫn đến việc bị khấu trừ nhiều lần. Một lỗi xác định chỉ được xác định khi nhận được phản hồi từ chối rõ ràng.
    try:
        response = requests.post(
            videos_url,
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_tls_verify(),
            timeout=(30, 60),
        )
    except Exception as exc:
        raise OFoxUnconfirmedTaskError(
            "OFox submission returned no response; a paid task may already "
            "exist remotely: "
            f"error={type(exc).__name__}, detail={_redact_secret(exc, api_key)}"
        ) from exc

    status_code = _status_code(response)
    if status_code >= 500:
        raise OFoxUnconfirmedTaskError(
            f"OFox submission failed with HTTP {status_code}; a paid task may "
            "already exist remotely"
        )
    if not 200 <= status_code < 300:
        # 4xx bị từ chối rõ ràng (chẳng hạn như thời lượng/độ phân giải vượt quá phạm vi hỗ trợ của kiểu máy), điều khiển từ xa
        # Không có nhiệm vụ nào được tạo và không có rủi ro thanh toán hai lần; thông báo lỗi chứa thông báo lỗi do máy chủ đưa ra.
        # Phạm vi giá trị pháp lý được gửi trực tiếp tới người dùng để sửa đổi cấu hình.
        raise OFoxError(
            "OFox video generation request rejected: "
            f"HTTP {status_code}, {_response_error(response, api_key)}"
        )
    try:
        body = response.json()
    except Exception as exc:
        raise OFoxUnconfirmedTaskError(
            "OFox submission returned an unreadable response; a paid task may "
            f"already exist remotely: error={type(exc).__name__}"
        ) from exc
    task_id = str(body.get("id") or "").strip() if isinstance(body, dict) else ""
    if not task_id:
        raise OFoxUnconfirmedTaskError(
            "OFox accepted the submission without returning a task id"
        )
    logger.info(f"OFox video task created: id={task_id}")

    task = _wait_for_task(
        task_id=task_id,
        videos_url=videos_url,
        headers=headers,
        api_key=api_key,
    )
    if task is None:
        return []

    # Khuyến nghị chính thức là trước tiên hãy sử dụng mirror_urls (địa chỉ chữ ký liên tục OFox CDN) và chỉ bật phản chiếu ở thượng nguồn.
    # Quay lại khi bị thiếu), quay lại unsigned_urls khi bị thiếu (liên kết trực tiếp tạm thời ngược dòng, có thể hết hạn trong vòng 24 giờ
    # Trông chờ). Cả hai đều phải được giữ lại toàn bộ và có sẵn để tải xuống ngay lập tức mà không có thông tin nguồn dài hạn nào được ghi cho chúng.
    video_url = ""
    for field in ("mirror_urls", "unsigned_urls"):
        urls = task.get(field)
        for candidate in urls if isinstance(urls, list) else []:
            if isinstance(candidate, str) and candidate.startswith(
                ("http://", "https://")
            ):
                video_url = candidate
                break
        if video_url:
            break
    if not video_url:
        raise OFoxError(
            f"OFox task completed without a downloadable video: id={task_id}",
            task_id=task_id,
        )

    return [
        MaterialInfo(
            provider="ofox",
            url=video_url,
            duration=duration,
            source_info={
                "provider": "ofox",
                "search_term": term,
                "asset_id": task_id,
                "rendition": {
                    "id": task_id,
                    "width": video_width,
                    "height": video_height,
                },
            },
        )
    ]


def _wait_for_task(
    *,
    task_id: str,
    videos_url: str,
    headers: dict[str, str],
    api_key: str,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + _bounded_float(
        "ofox_run_timeout",
        DEFAULT_RUN_TIMEOUT_SECONDS,
        60.0,
        7200.0,
    )
    poll_interval = _bounded_float(
        "ofox_poll_interval",
        DEFAULT_POLL_INTERVAL_SECONDS,
        0.5,
        60.0,
    )
    consecutive_failures = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OFoxUnconfirmedTaskError(
                "OFox task is still running after the configured local wait "
                f"timeout: id={task_id}",
                task_id=task_id,
            )

        # Thời gian chờ kết nối/đọc của các yêu cầu được tính giờ riêng biệt, vì vậy mỗi yêu cầu sẽ sử dụng tổng thời gian còn lại.
        # một nửa. Ngay cả khi kết nối và số lần đọc đạt đến giới hạn trên, một lượt yêu cầu sẽ không cố ý vượt quá tổng thời hạn;
        # Thư viện mạng có thể vẫn có một lỗi lập lịch nhỏ và việc kiểm tra thời hạn tiếp theo sẽ ngăn cản việc thử lại lần nữa.
        phase_timeout = max(min(remaining / 2.0, 30.0), 0.001)
        try:
            response = requests.get(
                f"{videos_url}/{task_id}",
                headers=headers,
                proxies=config.proxy,
                verify=_tls_verify(),
                timeout=(phase_timeout, phase_timeout),
            )
            status_code = _status_code(response)
            if status_code in RETRYABLE_STATUS_CODES:
                raise requests.exceptions.HTTPError(
                    f"HTTP {status_code}", response=response
                )
            if not 200 <= status_code < 300:
                raise OFoxUnconfirmedTaskError(
                    "OFox task status is unknown: "
                    f"http_status={status_code}, "
                    f"detail={_response_error(response, api_key)}",
                    task_id=task_id,
                )
            body = response.json()
            if not isinstance(body, dict):
                raise OFoxUnconfirmedTaskError(
                    "OFox task status response is malformed", task_id=task_id
                )
        except OFoxUnconfirmedTaskError:
            raise
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise OFoxUnconfirmedTaskError(
                    "OFox polling failed and the paid task state is unknown: "
                    f"error={type(exc).__name__}, "
                    f"detail={_redact_secret(exc, api_key)}",
                    task_id=task_id,
                ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OFoxUnconfirmedTaskError(
                    "OFox task is still running after the configured local wait "
                    f"timeout: id={task_id}",
                    task_id=task_id,
                ) from exc
            consecutive_failures += 1
            if consecutive_failures > MAX_POLL_RETRIES:
                raise OFoxUnconfirmedTaskError(
                    "OFox polling failed after retries; the paid task may still "
                    f"be running remotely: id={task_id}",
                    task_id=task_id,
                ) from exc
            delay = min(RETRY_BASE_SECONDS * consecutive_failures, remaining)
            logger.warning(
                "OFox polling hit a transient error; retrying the same task: "
                f"id={task_id}, attempt={consecutive_failures}/{MAX_POLL_RETRIES}, "
                f"retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        consecutive_failures = 0
        status = str(body.get("status") or "").strip().lower()
        if status in TERMINAL_SUCCESS_STATUSES:
            return body
        if status in TERMINAL_FAILURE_STATUSES:
            error_detail = body.get("error")
            logger.error(
                "OFox task did not produce a video: "
                f"id={task_id}, status={status}, "
                f"detail={_redact_secret(error_detail, api_key)}"
            )
            # Một lỗi rõ ràng ở đầu từ xa có nghĩa là tác vụ đã kết thúc và có thể tiếp tục với các đoạn tiếp theo một cách an toàn; qua
            # Người gọi quyết định có thử lại với từ khóa khác hay không.
            return None
        if status not in ACTIVE_STATUSES:
            raise OFoxUnconfirmedTaskError(
                f"OFox returned an unknown task status: id={task_id}, "
                f"status={status!r}",
                task_id=task_id,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OFoxUnconfirmedTaskError(
                "OFox task is still running after the configured local wait "
                f"timeout: id={task_id}",
                task_id=task_id,
            )
        time.sleep(min(poll_interval, remaining))
