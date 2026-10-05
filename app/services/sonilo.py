import base64
import binascii
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from app.config import config
from app.services import bgm as bgm_service
from app.utils import utils


DEFAULT_BASE_URL = "https://api.sonilo.com"
VIDEO_TO_MUSIC_PATH = "/v1/video-to-music"
SERVICES_PATH = "/v1/account/services"
MAX_VIDEO_DURATION_SECONDS = 360
MAX_PROMPT_LENGTH = 2000
MAX_PROXY_BYTES = 300 * 1024 * 1024
MAX_GENERATED_AUDIO_BYTES = 30 * 1024 * 1024
VIDEO_TO_MUSIC_SERVICE_ID = "video_to_music"


class SoniloError(RuntimeError):
    """Cho biết yêu cầu Sonilo, giao thức phản hồi hoặc lỗi kiểm tra âm thanh được tạo."""


def get_api_key() -> str:
    """Ưu tiên đọc các cấu hình được lưu bởi WebUI và các biến môi trường được phép sử dụng khi không được định cấu hình."""
    configured_key = str(config.app.get("sonilo_api_key", "") or "").strip()
    return configured_key or os.getenv("SONILO_API_KEY", "").strip()


def is_enabled() -> bool:
    return bool(get_api_key())


def _base_url() -> str:
    return str(
        config.app.get("sonilo_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    ).rstrip(
        "/"
    )


def _request_timeout() -> tuple[int, int]:
    """Giới hạn phạm vi giá trị cấu hình để tránh số vô cực hoặc số âm khiến yêu cầu bị treo vĩnh viễn hoặc bị lỗi ngay lập tức."""
    raw_timeout = config.app.get("sonilo_timeout", 600)
    try:
        read_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        read_timeout = 600
    if not math.isfinite(read_timeout) or read_timeout <= 0:
        read_timeout = 600
    # Yêu cầu không chấp nhận thời gian chờ đọc 0 giây. Làm tròn giữ nguyên ý nghĩa hợp lệ của cấu hình thập phân và cả
    # Tránh ném ValueError không vào liên kết hạ cấp Sonilo sau khi 0,1~0,9 bị cắt ngắn xuống 0 bởi int().
    return 15, max(1, math.ceil(min(read_timeout, 1800)))


def _normalize_service_id(service_id: str) -> str:
    """
    Hợp nhất ID dịch vụ Sonilo thành định dạng gạch chân được sử dụng nội bộ trong dự án.

    2026-07-14 Giao diện thực tế trả về ``video_to_music``, nhưng ví dụ về tài liệu công khai sử dụng nó trong cùng ngày
    ``chuyển video thành nhạc``. Sự khác biệt chỉ nằm ở dấu phân cách từ, do đó định dạng được thống nhất trên các ranh giới giao thức của bên thứ ba,
    Tránh lỗi dương tính giả trong các thử nghiệm kết nối giao diện người dùng do sự mâu thuẫn tạm thời giữa tài liệu của nhà cung cấp và phản hồi sản xuất.
    """
    return service_id.strip().lower().replace("-", "_")


def _safe_response_error(response: requests.Response) -> str:
    """Chỉ thông tin phản hồi ngắn được giữ lại để tạo điều kiện thuận lợi cho việc định vị và tránh các trang bất thường làm ảnh hưởng đến nhật ký."""
    body = (response.text or "").strip().replace("\n", " ")[:500]
    return body or response.reason or "request failed"


def test_connection() -> dict[str, Any]:
    """
    Sử dụng giao diện danh sách dịch vụ không tiêu tốn tín dụng nhạc phim để xác minh Khóa API.

    JSON gốc được trả về để hỗ trợ giao diện người dùng hiển thị các dịch vụ có sẵn nhưng tiêu đề Khóa hoặc yêu cầu không bao giờ được ghi lại trong nhật ký.
    """
    api_key = get_api_key()
    if not api_key:
        raise SoniloError("Sonilo API key is required")
    try:
        response = requests.get(
            f"{_base_url()}{SERVICES_PATH}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(15, 30),
        )
    except requests.RequestException as exc:
        raise SoniloError(f"failed to connect to Sonilo: {exc}") from exc
    if not response.ok:
        raise SoniloError(
            f"Sonilo connection failed ({response.status_code}): "
            f"{_safe_response_error(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SoniloError("Sonilo returned an invalid service response") from exc
    if not isinstance(payload, dict):
        raise SoniloError("Sonilo returned an unexpected service response")
    available_services = payload.get("available_services")
    if not isinstance(available_services, list) or not all(
        isinstance(service_id, str) for service_id in available_services
    ):
        raise SoniloError("Sonilo returned an invalid service list")
    normalized_services = {
        _normalize_service_id(service_id) for service_id in available_services
    }
    if VIDEO_TO_MUSIC_SERVICE_ID not in normalized_services:
        raise SoniloError("Sonilo video-to-music service is not available for this key")
    logger.info("Sonilo connection test succeeded")
    return payload


def _remove_file(file_path: str) -> None:
    """Cố gắng hết sức để dọn sạch các tệp trung gian Sonilo và không ghi đè lên ngoại lệ ban đầu đang được người gọi xử lý."""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning(
            f"failed to remove Sonilo temporary file: path={file_path}, error={exc}"
        )


def _create_video_proxy(video_path: str) -> str:
    """
    Tạo video proxy H.264 không có rãnh âm thanh và 1280 pixel ở cạnh dài nhất.

    Sonilo chỉ cần phân tích nhịp điệu và nội dung của hình ảnh, việc tải phim gốc có độ phân giải cao lên sẽ làm tăng thời gian chờ đợi và lưu lượng truy cập.
    Không có lợi ích thực sự về chất lượng xây dựng. Tệp tác nhân được đặt trong cùng thư mục với tệp đầu vào và sẽ được dọn sạch sau khi hoàn thành tác vụ.
    """
    descriptor, proxy_path = tempfile.mkstemp(
        prefix=".sonilo-proxy-",
        suffix=".mp4",
        dir=os.path.dirname(os.path.abspath(video_path)),
    )
    os.close(descriptor)
    command = [
        utils.get_ffmpeg_binary(),
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        video_path,
        "-vf",
        (
            "scale=w=1280:h=1280:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2"
        ),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        proxy_path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _remove_file(proxy_path)
        raise SoniloError("Sonilo video proxy generation timed out") from exc
    except OSError as exc:
        _remove_file(proxy_path)
        raise SoniloError("failed to run FFmpeg for Sonilo video proxy") from exc
    if result.returncode != 0:
        _remove_file(proxy_path)
        detail = (result.stderr or "").strip().replace("\n", " ")[-500:]
        raise SoniloError(f"failed to generate Sonilo video proxy: {detail}")
    proxy_size = os.path.getsize(proxy_path) if os.path.isfile(proxy_path) else 0
    if proxy_size <= 0 or proxy_size > MAX_PROXY_BYTES:
        _remove_file(proxy_path)
        raise SoniloError("Sonilo video proxy is empty or exceeds the 300 MB limit")
    logger.info(
        f"Sonilo video proxy prepared: source={video_path}, size={proxy_size} bytes"
    )
    return proxy_path


def _parse_event(raw_line: bytes) -> dict[str, Any]:
    """Phân tích cú pháp nghiêm ngặt một NDJSON duy nhất và vô hiệu hóa âm thầm bỏ qua các phản hồi bị cắt ngắn hoặc không có đối tượng."""
    try:
        event = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SoniloError("Sonilo returned malformed streaming data") from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise SoniloError("Sonilo returned an invalid streaming event")
    return event


def _stream_audio(response: requests.Response, temp_audio_path: str) -> tuple[int, str]:
    """
    Ghi luồng nhạc phim đầu tiên vào tệp tạm thời theo thứ tự sự kiện và giới hạn kích thước tối đa.

    API có thể trả về nhiều luồng ứng viên cùng một lúc; sản phẩm hiện tại chỉ yêu cầu một BGM nên việc lựa chọn là cố định
    luồng_index=0. Nó sẽ chỉ được xuất bản sau khi sự kiện hoàn chỉnh đã được FFmpeg nhận và giải mã hoàn toàn.
    """
    total_bytes = 0
    title = ""
    completed = False
    with open(temp_audio_path, "wb") as output:
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            event = _parse_event(raw_line)
            event_type = event["type"]
            if event_type == "error":
                message = str(
                    event.get("message") or event.get("error") or "unknown error"
                )
                raise SoniloError(f"Sonilo generation failed: {message}")
            if event_type == "title":
                title = str(event.get("title") or event.get("data") or "")[:200]
                continue
            if event_type == "complete":
                completed = True
                break
            if event_type != "audio_chunk":
                logger.debug(f"ignoring unsupported Sonilo event: type={event_type}")
                continue

            stream_index = event.get("stream_index", 0)
            if stream_index != 0:
                continue
            encoded_chunk = event.get("data") or event.get("audio")
            if not isinstance(encoded_chunk, str) or not encoded_chunk:
                raise SoniloError("Sonilo returned an empty audio chunk")
            try:
                chunk = base64.b64decode(encoded_chunk, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SoniloError("Sonilo returned an invalid audio chunk") from exc
            if not chunk:
                raise SoniloError("Sonilo returned an empty audio chunk")
            total_bytes += len(chunk)
            if total_bytes > MAX_GENERATED_AUDIO_BYTES:
                raise SoniloError("Sonilo audio exceeds the 30 MB limit")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())

    if not completed:
        raise SoniloError("Sonilo stream ended before completion")
    if total_bytes <= 0:
        raise SoniloError("Sonilo returned no audio data")
    return total_bytes, title


def _request_bgm(video_path: str, output_path: str, prompt: str) -> str:
    """Yêu cầu một bản nhạc và lưu nó nguyên bản sau khi vượt qua quá trình xác minh âm thanh và giao thức hoàn chỉnh."""
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    descriptor, temp_audio_path = tempfile.mkstemp(
        prefix=".sonilo-audio-",
        suffix=Path(output_path).suffix or ".m4a",
        dir=output_dir,
    )
    os.close(descriptor)
    try:
        logger.info(
            f"requesting Sonilo background music: video={video_path}, "
            f"prompt_provided={bool(prompt)}"
        )
        try:
            with open(video_path, "rb") as video_file:
                response = requests.post(
                    f"{_base_url()}{VIDEO_TO_MUSIC_PATH}",
                    headers={"Authorization": f"Bearer {get_api_key()}"},
                    files={"video": (Path(video_path).name, video_file, "video/mp4")},
                    data={"prompt": prompt} if prompt else None,
                    stream=True,
                    timeout=_request_timeout(),
                )
                with response:
                    if not response.ok:
                        raise SoniloError(
                            f"Sonilo generation failed ({response.status_code}): "
                            f"{_safe_response_error(response)}"
                        )
                    total_bytes, title = _stream_audio(response, temp_audio_path)
        except requests.RequestException as exc:
            # Sự gián đoạn mạng trong iter_lines() cũng là những yêu cầu ngoại lệ và không thể chỉ ghi lại được
            # Trong giai đoạn thiết lập kết nối, nếu không, một nửa âm thanh có thể khiến tác vụ thoát ra bất thường mà không thể hạ cấp.
            raise SoniloError(f"failed to request Sonilo music: {exc}") from exc

        try:
            bgm_service.validate_audio_file(temp_audio_path, timeout_seconds=120)
        except (bgm_service.BgmUploadError, bgm_service.BgmServiceError) as exc:
            raise SoniloError("Sonilo returned audio that FFmpeg cannot decode") from exc
        os.replace(temp_audio_path, output_path)
        temp_audio_path = ""
        logger.info(
            f"Sonilo background music generated: output={output_path}, "
            f"size={total_bytes} bytes, title={title or '-'}"
        )
        return output_path
    finally:
        _remove_file(temp_audio_path)


def generate_bgm(
    video_path: str,
    output_path: str,
    video_duration: float,
    prompt: str = "",
) -> str:
    """Tạo nhạc nền Sonilo phù hợp với thời lượng của video được ghép."""
    if not get_api_key():
        raise SoniloError("Sonilo API key is required")
    if not os.path.isfile(video_path):
        raise SoniloError("Sonilo input video does not exist")
    try:
        duration = float(video_duration)
    except (TypeError, ValueError) as exc:
        raise SoniloError("Sonilo video duration is invalid") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise SoniloError("Sonilo video duration is invalid")
    if duration > MAX_VIDEO_DURATION_SECONDS:
        raise SoniloError("Sonilo supports videos up to 360 seconds")
    prompt = str(prompt or "").strip()
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise SoniloError("Sonilo music prompt exceeds 2000 characters")

    proxy_path = ""
    try:
        proxy_path = _create_video_proxy(video_path)
        return _request_bgm(proxy_path, output_path, prompt)
    except SoniloError:
        raise
    except OSError as exc:
        # Lỗi hệ thống tệp có thể xảy ra với các thư mục tạm thời, tệp proxy và cuối cùng là thay thế nguyên tử. chuyển đổi đồng đều thành
        # SoniloError, lớp điều phối tác vụ có thể bị hạ cấp xuống "không có nhạc nền" như được thiết kế và giữ lại trong phim hoàn thiện.
        raise SoniloError(f"Sonilo local file operation failed: {exc}") from exc
    finally:
        _remove_file(proxy_path)
