"""Máy khách video MiTa MiniMax H3 Vincent.

Mô-đun này chỉ chịu trách nhiệm về giao thức MiniMax V2 của đặc vụ tháp bí mật: gửi các nhiệm vụ đã thanh toán, bỏ phiếu cho cùng một nhiệm vụ,
Phân tích kết quả tạo ra. Việc tạo tài liệu, tải tập tin và ghép phim theo yêu cầu vẫn được xử lý bởi dịch vụ ``material``.
Tránh kết hợp các thỏa thuận của nhà cung cấp với quy trình làm việc video địa phương.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect


DEFAULT_BASE_URL = "https://metaso.cn/api/minimax"
DEFAULT_MODEL_ID = "MiniMax-H3"
DEFAULT_RESOLUTION = "2K"
DEFAULT_MIN_DURATION_SECONDS = 4
DEFAULT_MAX_DURATION_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
MAX_PROMPT_LENGTH = 7000
MAX_POLL_RETRIES = 5
RETRY_BASE_SECONDS = 1.0
MAX_ERROR_TEXT_LENGTH = 500
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled", "canceled"})
SUPPORTED_RESOLUTIONS = frozenset({"768P", "2K"})


class MetasoMiniMaxError(RuntimeError):
    """Lỗi cấu hình, yêu cầu hoặc phản hồi xác định cho MiniMax."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        # Khi một tác vụ từ xa được tạo, tất cả các ngoại lệ tiếp theo đều mang cùng một ID. Dịch vụ nhiệm vụ có thể được thống nhất
        # Lưu manh mối khôi phục mà không biết bước bỏ phiếu, phân tích kết quả hoặc tải xuống không thành công.
        self.task_id = task_id


class MetasoMiniMaxUnconfirmedTaskError(MetasoMiniMaxError):
    """Đầu từ xa có thể đã tạo một tác vụ phải trả phí nhưng máy cục bộ không thể xác nhận trạng thái cuối cùng của tác vụ đó."""


class MetasoMiniMaxDownloadError(MetasoMiniMaxError):
    """Nhiệm vụ thanh toán từ xa đã thành công nhưng phim đã hoàn thành không thể tải xuống máy cục bộ."""


def get_api_key(settings: Mapping[str, Any] | None = None) -> str:
    """
    Đọc thông tin đăng nhập tháp bí mật với mức độ ưu tiên cố định.

    Secret Tower ``mk-`` Key và MiniMax chính thức thuộc các hệ thống tài khoản khác nhau nên không thể sử dụng lại.
    Dự án đã có ``minimax_api_key``. Cấu hình độc lập và các biến môi trường độc lập cũng có thể ngăn người dùng
    Thông tin đăng nhập tạo video bất ngờ thay đổi khi chuyển đổi Nhà cung cấp LLM.
    """
    settings = config.app if settings is None else settings
    configured = str(settings.get("metaso_minimax_api_key", "") or "").strip()
    environment_key = os.getenv("METASO_MINIMAX_API_KEY", "").strip()
    return configured or environment_key


def is_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    """Trả về xem cấu hình hiện tại có thông tin xác thực để gọi giao diện video của tháp bí mật hay không."""
    return bool(get_api_key(settings))


def _base_url() -> str:
    value = (
        str(
            config.app.get("metaso_minimax_base_url", DEFAULT_BASE_URL)
            or DEFAULT_BASE_URL
        )
        .strip()
        .rstrip("/")
    )
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MetasoMiniMaxError(
            "metaso_minimax_base_url must be an absolute HTTP(S) URL"
        )
    return value


def _resolution() -> str:
    configured = config.app.get("metaso_minimax_resolution", DEFAULT_RESOLUTION)
    value = str(configured).strip().upper()
    if value not in SUPPORTED_RESOLUTIONS:
        supported = ", ".join(sorted(SUPPORTED_RESOLUTIONS))
        # Độ phân giải ảnh hưởng trực tiếp đến chi phí sản xuất và người dùng không thể âm thầm trả lại 2K khi mắc lỗi rõ ràng. Chỉ các mục cấu hình
        # Chỉ sử dụng các giá trị mặc định khi thiếu hoàn toàn để tránh vô tình tạo ra một tác vụ tốn kém hơn dự kiến.
        raise MetasoMiniMaxError(
            f"Unsupported Metaso MiniMax resolution {value!r}; "
            f"expected one of: {supported}"
        )
    return value


def _tls_verify() -> bool:
    value = config.app.get("tls_verify", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _bounded_float(key: str, default: float, minimum: float, maximum: float) -> float:
    """Đọc cấu hình dấu phẩy động giới hạn, được giới hạn ở phạm vi an toàn sẽ không làm quá tải máy từ xa hoặc máy cục bộ."""
    try:
        value = float(config.app.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _redact_secret(value: Any, api_key: str) -> str:
    """Giữ lại văn bản có thể khắc phục sự cố trong khi xóa khóa API, khóa mã hóa URL và thông tin xác thực proxy."""
    text = str(value or "")
    if api_key:
        text = text.replace(api_key, "***")
        encoded = quote_plus(api_key)
        if encoded != api_key:
            text = text.replace(encoded, "***")
    for proxy_url in config.proxy.values():
        proxy_secret = str(proxy_url or "")
        if proxy_secret:
            text = text.replace(proxy_secret, "***")
    return text[:MAX_ERROR_TEXT_LENGTH]


def _response_error(response: Any, api_key: str) -> str:
    """Tương thích với cấu trúc lỗi lồng nhau của MiniMax V2 và giới hạn độ dài phản hồi trong nhật ký."""
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {_status_code(response)}"
    if not isinstance(payload, dict):
        return f"HTTP {_status_code(response)}"

    error = payload.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        message = error.get("message")
        http_code = error.get("http_code")
    else:
        error_type = None
        message = payload.get("message") or error
        http_code = payload.get("code")
    detail = ": ".join(
        str(item) for item in (error_type, http_code, message) if item not in (None, "")
    )
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


def _normalize_duration(minimum_duration: int) -> tuple[int, int]:
    """Trả về "thời lượng yêu cầu của người dùng, thời lượng gửi thực tế", được sử dụng cho ràng buộc giải thích nhật ký tối thiểu 4 giây."""
    try:
        requested = int(minimum_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetasoMiniMaxError(
            "Metaso MiniMax clip duration must be a positive integer"
        ) from exc
    if requested <= 0:
        raise MetasoMiniMaxError(
            "Metaso MiniMax clip duration must be a positive integer"
        )
    duration = min(
        max(requested, DEFAULT_MIN_DURATION_SECONDS),
        DEFAULT_MAX_DURATION_SECONDS,
    )
    return requested, duration


def generate_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    """Gửi bài tập video Secret Tower MiniMax H3 Vincent và chờ kết quả có thể tải xuống."""
    api_key = get_api_key()
    if not api_key:
        raise MetasoMiniMaxError("Metaso MiniMax requires an API key")

    term = str(search_term or "").strip()
    if not term:
        # Một từ nhắc trống thường chỉ ra rằng việc phân chia tập lệnh ngược dòng không thành công. Không thể kiểm tra giao diện thanh toán với đầu vào không hợp lệ.
        # Nếu không, ngay cả khi thiết bị đầu cuối từ xa chấp nhận nó, nó sẽ chỉ tạo ra các tài liệu thanh toán không thể sử dụng được.
        raise MetasoMiniMaxError("Metaso MiniMax search term must not be empty")
    if len(term) > MAX_PROMPT_LENGTH:
        raise MetasoMiniMaxError(
            f"Metaso MiniMax search term exceeds {MAX_PROMPT_LENGTH} characters"
        )

    aspect = VideoAspect(video_aspect)
    requested_duration, duration = _normalize_duration(minimum_duration)
    if duration != requested_duration:
        logger.info(
            "Metaso MiniMax clip duration adjusted to H3 limits: "
            f"requested={requested_duration}s, using={duration}s, "
            f"supported={DEFAULT_MIN_DURATION_SECONDS}-{DEFAULT_MAX_DURATION_SECONDS}s"
        )
    resolution = _resolution()
    payload = {
        "model": DEFAULT_MODEL_ID,
        "content": [{"type": "text", "text": term}],
        "resolution": resolution,
        "duration": duration,
        "ratio": aspect.value,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    base_url = _base_url()
    create_url = f"{base_url}/v2/video_generation"
    logger.info(
        "generating video with Metaso MiniMax H3: "
        f"resolution={resolution}, ratio={aspect.value}, duration={duration}s, "
        f"prompt_length={len(term)}"
    )

    # Khi xảy ra thời gian chờ POST hoặc 5xx, thiết bị đầu cuối từ xa có thể đã được tạo và tính phí. Giao diện không cung cấp ứng dụng khách
    # Khóa này không có giá trị nên nó sẽ không bao giờ được truyền lại tự động; lớp trên sẽ dừng các từ khóa tiếp theo để tránh bị khấu trừ nhiều lần.
    try:
        response = requests.post(
            create_url,
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_tls_verify(),
            timeout=(30, 60),
        )
    except Exception as exc:
        raise MetasoMiniMaxUnconfirmedTaskError(
            "Metaso MiniMax submission returned no response; a paid task may "
            "already exist remotely: "
            f"error={type(exc).__name__}, detail={_redact_secret(exc, api_key)}"
        ) from exc

    status_code = _status_code(response)
    if status_code >= 500:
        raise MetasoMiniMaxUnconfirmedTaskError(
            f"Metaso MiniMax submission failed with HTTP {status_code}; a paid "
            "task may already exist remotely"
        )
    if not 200 <= status_code < 300:
        raise MetasoMiniMaxError(
            "Metaso MiniMax video generation request rejected: "
            f"HTTP {status_code}, {_response_error(response, api_key)}"
        )
    try:
        body = response.json()
    except Exception as exc:
        raise MetasoMiniMaxUnconfirmedTaskError(
            "Metaso MiniMax submission returned an unreadable response; a paid "
            f"task may already exist remotely: error={type(exc).__name__}"
        ) from exc

    task_id = str(body.get("task_id") or "").strip() if isinstance(body, dict) else ""
    if not task_id:
        raise MetasoMiniMaxUnconfirmedTaskError(
            "Metaso MiniMax accepted the submission without returning a task id"
        )
    logger.info(f"Metaso MiniMax paid task created: id={task_id}")

    task = _wait_for_task(
        task_id=task_id,
        base_url=base_url,
        headers=headers,
        api_key=api_key,
    )
    content = task.get("content")
    video_url = content.get("url") if isinstance(content, dict) else None
    if not isinstance(video_url, str) or not video_url.startswith(
        ("http://", "https://")
    ):
        raise MetasoMiniMaxError(
            f"Metaso MiniMax task succeeded without a downloadable video: id={task_id}",
            task_id=task_id,
        )

    actual_duration = task.get("duration", duration)
    try:
        actual_duration = int(actual_duration)
    except (TypeError, ValueError, OverflowError):
        actual_duration = duration
    if actual_duration <= 0:
        actual_duration = duration

    return [
        MaterialInfo(
            provider="metaso_minimax",
            url=video_url,
            duration=actual_duration,
            source_info={
                "provider": "metaso_minimax",
                "search_term": term,
                "asset_id": task_id,
                # 2K/768P của MiniMax là tên thông số kỹ thuật và giao diện không hứa hẹn kích thước pixel cố định.
                # Đừng đoán chiều rộng/chiều cao. Nếu sau này cần kích thước chính xác thì giá trị phát hiện của tệp đã tải xuống sẽ chiếm ưu thế.
                "rendition": {"id": task_id},
            },
        )
    ]


def _wait_for_task(
    *,
    task_id: str,
    base_url: str,
    headers: dict[str, str],
    api_key: str,
) -> dict[str, Any]:
    """Thăm dò nhiệm vụ được trả phí tương tự cho đến khi nó thành công, thất bại rõ ràng hoặc trạng thái không thể được xác nhận cục bộ."""
    deadline = time.monotonic() + _bounded_float(
        "metaso_minimax_run_timeout",
        DEFAULT_RUN_TIMEOUT_SECONDS,
        60.0,
        7200.0,
    )
    poll_interval = _bounded_float(
        "metaso_minimax_poll_interval",
        DEFAULT_POLL_INTERVAL_SECONDS,
        1.0,
        60.0,
    )
    query_url = f"{base_url}/v2/query/video_generation/{quote(task_id, safe='')}"
    consecutive_failures = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MetasoMiniMaxUnconfirmedTaskError(
                "Metaso MiniMax task is still running after the configured local "
                f"wait timeout: id={task_id}",
                task_id=task_id,
            )

        # Thời gian chờ kết nối/đọc được tính thời gian riêng và một nửa thời gian còn lại được sử dụng để đảm bảo một GET.
        # Sẽ không cố ý vượt quá tổng thời hạn nhiệm vụ. Sau khi hết hạn, cuộc bỏ phiếu tiếp theo sẽ không được bắt đầu.
        phase_timeout = max(min(remaining / 2.0, 30.0), 0.001)
        try:
            response = requests.get(
                query_url,
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
                raise MetasoMiniMaxUnconfirmedTaskError(
                    "Metaso MiniMax task status is unknown: "
                    f"http_status={status_code}, "
                    f"detail={_response_error(response, api_key)}",
                    task_id=task_id,
                )
            body = response.json()
            task = body.get("task") if isinstance(body, dict) else None
            if not isinstance(task, dict):
                raise MetasoMiniMaxUnconfirmedTaskError(
                    "Metaso MiniMax task status response is malformed",
                    task_id=task_id,
                )
        except MetasoMiniMaxUnconfirmedTaskError:
            raise
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise MetasoMiniMaxUnconfirmedTaskError(
                    "Metaso MiniMax polling failed and the paid task state is "
                    f"unknown: error={type(exc).__name__}, "
                    f"detail={_redact_secret(exc, api_key)}",
                    task_id=task_id,
                ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MetasoMiniMaxUnconfirmedTaskError(
                    "Metaso MiniMax task is still running after the configured "
                    f"local wait timeout: id={task_id}",
                    task_id=task_id,
                ) from exc
            consecutive_failures += 1
            if consecutive_failures > MAX_POLL_RETRIES:
                raise MetasoMiniMaxUnconfirmedTaskError(
                    "Metaso MiniMax polling failed after retries; the paid task "
                    f"may still be running remotely: id={task_id}",
                    task_id=task_id,
                ) from exc
            delay = min(RETRY_BASE_SECONDS * consecutive_failures, remaining)
            logger.warning(
                "Metaso MiniMax polling hit a transient error; retrying the same "
                f"task: id={task_id}, attempt={consecutive_failures}/"
                f"{MAX_POLL_RETRIES}, retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        consecutive_failures = 0
        status = str(task.get("status") or "").strip().lower()
        logger.info(f"Metaso MiniMax task status: id={task_id}, status={status}")
        if status == "succeeded":
            return task
        if status in TERMINAL_FAILURE_STATUSES:
            raise MetasoMiniMaxError(
                "Metaso MiniMax task did not produce a video: "
                f"id={task_id}, status={status}, "
                f"detail={_redact_secret(task.get('error'), api_key)}",
                task_id=task_id,
            )
        if status not in ACTIVE_STATUSES:
            raise MetasoMiniMaxUnconfirmedTaskError(
                "Metaso MiniMax returned an unknown task status: "
                f"id={task_id}, status={status!r}",
                task_id=task_id,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MetasoMiniMaxUnconfirmedTaskError(
                "Metaso MiniMax task is still running after the configured local "
                f"wait timeout: id={task_id}",
                task_id=task_id,
            )
        time.sleep(min(poll_interval, remaining))
