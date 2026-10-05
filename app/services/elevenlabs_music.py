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


DEFAULT_BASE_URL = "https://api.elevenlabs.io"
VIDEO_TO_MUSIC_PATH = "/v1/music/video-to-music"
SUBSCRIPTION_PATH = "/v1/user/subscription"
DEFAULT_MODEL_ID = "music_v2"
SUPPORTED_MODEL_IDS = frozenset({"music_v1", "music_v2"})
MAX_VIDEO_DURATION_SECONDS = 600
MAX_PROMPT_LENGTH = 1000
MAX_PROXY_BYTES = 200 * 1024 * 1024
MAX_GENERATED_AUDIO_BYTES = 50 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 500


class ElevenLabsMusicError(RuntimeError):
    """Cho biết yêu cầu nhạc phim, tạo proxy hoặc xác minh âm thanh trả về của ElevenLabs không thành công."""


class ElevenLabsPaidPlanRequiredError(ElevenLabsMusicError):
    """Cho biết Khóa hợp lệ nhưng gói tài khoản hiện tại không bao gồm API âm nhạc ElevenLabs."""


class ElevenLabsAuthenticationError(ElevenLabsMusicError):
    """Cho biết Khóa API ElevenLabs bị thiếu hoặc đã bị máy chủ từ chối."""


def get_api_key() -> str:
    """
    Đọc Khóa API được chia sẻ của ElevenLabs.

    Nhạc nền sử dụng cấu hình tài khoản giống như ElevenLabs TTS hiện có để tránh việc người dùng phải bảo trì nhiều lần trong WebUI.
    Hai bản sao của Key; các biến môi trường chỉ được sử dụng làm nguồn dự phòng khi cấu hình cục bộ không được điền vào.
    """
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    return configured_key or os.getenv("ELEVENLABS_API_KEY", "").strip()


def is_enabled() -> bool:
    return bool(get_api_key())


def _base_url() -> str:
    return str(
        config.elevenlabs.get("music_base_url", DEFAULT_BASE_URL)
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _model_id() -> str:
    """Chỉ những mẫu hiện được hiển thị bởi Video-to-Music chính thức mới được phép, với tính năng dự phòng an toàn trong trường hợp cấu hình sai."""
    model_id = str(
        config.elevenlabs.get("music_model_id", DEFAULT_MODEL_ID)
        or DEFAULT_MODEL_ID
    ).strip()
    return model_id if model_id in SUPPORTED_MODEL_IDS else DEFAULT_MODEL_ID


def _request_timeout() -> tuple[int, int]:
    """Hạn chế thời gian chờ đọc nhạc phim, có tính đến việc sản xuất các video dài tốn nhiều thời gian và khả năng khôi phục các cấu hình sai."""
    raw_timeout = config.elevenlabs.get("music_timeout", 600)
    try:
        read_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        read_timeout = 600
    if not math.isfinite(read_timeout) or read_timeout <= 0:
        read_timeout = 600
    return 15, max(1, math.ceil(min(read_timeout, 1800)))


def _safe_response_error(response: requests.Response) -> str:
    """Chỉ đọc văn bản lỗi hạn chế của bên thứ ba để tránh phản hồi ngoại lệ do cạn kiệt bộ nhớ hoặc gây ô nhiễm nhật ký tác vụ."""
    try:
        body_bytes = next(
            response.iter_content(chunk_size=MAX_ERROR_BODY_BYTES),
            b"",
        )
    except requests.RequestException:
        body_bytes = b""
    if isinstance(body_bytes, bytes):
        body = body_bytes.decode(
            response.encoding or "utf-8",
            errors="replace",
        )
    else:
        body = str(body_bytes)
    body = body.strip().replace("\n", " ")[:MAX_ERROR_BODY_BYTES]
    return body or response.reason or "request failed"


def test_connection() -> dict[str, Any]:
    """
    Sử dụng giao diện đăng ký không tiêu tốn tín dụng tạo nhạc để kiểm tra gói tài khoản và Khóa API.

    Giao diện này chỉ có thể xác nhận Key có thể truy cập thông tin thuê bao và tài khoản không phải là gói miễn phí chứ không thể chứng minh Key hiện tại
    Phải có quyền điểm cuối Âm nhạc. ElevenLabs cho phép hạn chế theo điểm cuối, hạn ngạch và IP
    Chìa khóa, do đó, lời nhắc thành công về giao diện người dùng phải duy trì ranh giới này và các quyền thực tế cuối cùng vẫn được yêu cầu xây dựng xác nhận.
    Hóa đơn và chi tiết sử dụng trong phản hồi sẽ không được ghi vào nhật ký để tránh ghi lại quyền riêng tư của tài khoản.
    """
    api_key = get_api_key()
    if not api_key:
        raise ElevenLabsAuthenticationError("ElevenLabs API key is required")
    try:
        with requests.get(
            f"{_base_url()}{SUBSCRIPTION_PATH}",
            headers={"xi-api-key": api_key},
            timeout=(15, 30),
            stream=True,
        ) as response:
            if response.status_code == 401:
                raise ElevenLabsAuthenticationError(
                    "ElevenLabs API key was rejected (401): "
                    f"{_safe_response_error(response)}"
                )
            if not response.ok:
                raise ElevenLabsMusicError(
                    "ElevenLabs account check failed "
                    f"({response.status_code}): "
                    f"{_safe_response_error(response)}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ElevenLabsMusicError(
                    "ElevenLabs returned an invalid subscription response"
                ) from exc
    except requests.RequestException as exc:
        raise ElevenLabsMusicError(
            f"failed to connect to ElevenLabs: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ElevenLabsMusicError(
            "ElevenLabs returned an unexpected subscription response"
        )
    tier = str(payload.get("tier") or "").strip().lower()
    if not tier:
        raise ElevenLabsMusicError(
            "ElevenLabs subscription response does not include an account tier"
        )
    if tier == "free":
        raise ElevenLabsPaidPlanRequiredError(
            "ElevenLabs Music API requires a paid plan; "
            "the current account is on the free tier"
        )
    logger.info(f"ElevenLabs account and plan check succeeded: tier={tier}")
    return payload


def validate_generation_access() -> None:
    """
    Loại bỏ các tài khoản được xác định là không thể tạo nhạc phim trước khi hệ thống video đắt tiền bắt đầu.

    Các gói miễn phí và khóa không hợp lệ là các lỗi xác định và phải được chấm dứt ngay lập tức để tránh tiêu tốn LLM và TTS trước tiên.
    và hạn ngạch dịch vụ vật chất. Giao diện đăng ký cũng có thể bị ảnh hưởng bởi phạm vi điểm cuối chỉ dành cho Âm nhạc, các hạn chế về IP hoặc
    Không thể truy cập do sự cố mạng tạm thời; những kết quả này không chứng minh rằng API nhạc không khả dụng nên chỉ ghi lại cảnh báo,
    Tiếp tục để yêu cầu xây dựng thực tế xác định kết quả và tránh chặn các lỗi Khóa bị hạn chế nhưng có sẵn.
    """
    try:
        test_connection()
    except (ElevenLabsPaidPlanRequiredError, ElevenLabsAuthenticationError):
        raise
    except ElevenLabsMusicError as exc:
        logger.warning(
            "ElevenLabs account preflight was inconclusive; "
            f"generation will verify Music API access: error={exc}"
        )


def _remove_file(file_path: str) -> None:
    """Cố gắng hết sức để dọn sạch các tệp trung gian của ElevenLabs và không ghi đè lên ngoại lệ ban đầu đang được người gọi xử lý."""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning(
            "failed to remove ElevenLabs temporary file: "
            f"path={file_path}, error={exc}"
        )


def _create_video_proxy(video_path: str) -> str:
    """
    Tạo video proxy H.264 không có rãnh âm thanh và 1280 pixel ở cạnh dài nhất.

    Video-to-Music chỉ phân tích hình ảnh, tải lên phim gốc có độ phân giải cao sẽ không cải thiện nhạc nền cũng như không tăng lưu lượng truy cập.
    và thời gian chờ đợi. Proxy được giới hạn nghiêm ngặt ở giới hạn 200 MB chính thức và sẽ bị xóa khi kết thúc yêu cầu.
    """
    descriptor, proxy_path = tempfile.mkstemp(
        prefix=".elevenlabs-music-proxy-",
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
        "-fs",
        str(MAX_PROXY_BYTES),
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
        raise ElevenLabsMusicError(
            "ElevenLabs video proxy generation timed out"
        ) from exc
    except OSError as exc:
        _remove_file(proxy_path)
        raise ElevenLabsMusicError(
            "failed to run FFmpeg for ElevenLabs video proxy"
        ) from exc
    if result.returncode != 0:
        _remove_file(proxy_path)
        detail = (result.stderr or "").strip().replace("\n", " ")[-500:]
        raise ElevenLabsMusicError(
            f"failed to generate ElevenLabs video proxy: {detail}"
        )
    proxy_size = os.path.getsize(proxy_path) if os.path.isfile(proxy_path) else 0
    if proxy_size <= 0 or proxy_size > MAX_PROXY_BYTES:
        _remove_file(proxy_path)
        raise ElevenLabsMusicError(
            "ElevenLabs video proxy is empty or exceeds the 200 MB limit"
        )
    logger.info(
        "ElevenLabs video proxy prepared: "
        f"source={video_path}, size={proxy_size} bytes"
    )
    return proxy_path


def _stream_audio(response: requests.Response, temp_audio_path: str) -> int:
    """Lưu âm thanh theo từng đoạn và giới hạn kích thước tối đa để ngăn các phản hồi bất thường làm cạn kiệt đĩa cục bộ."""
    total_bytes = 0
    with open(temp_audio_path, "wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > MAX_GENERATED_AUDIO_BYTES:
                raise ElevenLabsMusicError(
                    "ElevenLabs audio exceeds the 50 MB limit"
                )
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if total_bytes <= 0:
        raise ElevenLabsMusicError("ElevenLabs returned no audio data")
    return total_bytes


def _request_bgm(video_path: str, output_path: str, prompt: str) -> str:
    """Yêu cầu nhạc nền ElevenLabs, tải xuống đầy đủ và vượt qua xác minh FFmpeg trước khi phát hành nguyên bản."""
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    descriptor, temp_audio_path = tempfile.mkstemp(
        prefix=".elevenlabs-music-",
        suffix=Path(output_path).suffix or ".mp3",
        dir=output_dir,
    )
    os.close(descriptor)
    try:
        model_id = _model_id()
        logger.info(
            "requesting ElevenLabs background music: "
            f"video={video_path}, model={model_id}, "
            f"prompt_provided={bool(prompt)}"
        )
        request_data = {"model_id": model_id}
        if prompt:
            request_data["description"] = prompt
        try:
            with open(video_path, "rb") as video_file:
                response = requests.post(
                    f"{_base_url()}{VIDEO_TO_MUSIC_PATH}",
                    headers={"xi-api-key": get_api_key()},
                    params={"output_format": "mp3_44100_128"},
                    files=[
                        (
                            # Tài liệu chính thức hiển thị mảng biểu mẫu là ``video[]``, nhưng 2026-07-18
                            # Giao diện sản xuất sẽ trả về 422 cho trường này và tham số Starlette thực tế được đặt tên
                            # ``video``. Khi tải lên nhiều lần, yêu cầu có thể tiếp tục thêm các trường có cùng tên.
                            "videos",
                            (Path(video_path).name, video_file, "video/mp4"),
                        )
                    ],
                    data=request_data,
                    stream=True,
                    timeout=_request_timeout(),
                )
                with response:
                    if not response.ok:
                        raise ElevenLabsMusicError(
                            "ElevenLabs generation failed "
                            f"({response.status_code}): "
                            f"{_safe_response_error(response)}"
                        )
                    total_bytes = _stream_audio(response, temp_audio_path)
        except requests.RequestException as exc:
            # Việc ngắt kết nối trong giai đoạn tải xuống cũng được coi là yêu cầu không thành công. Logic hạ cấp nhiệm vụ phải được nhập và không thể để lại một nửa tin nhắn.
            # Âm thanh có thể khiến video được tạo bị lỗi tổng thể do sự biến động của mạng bên thứ ba.
            raise ElevenLabsMusicError(
                f"failed to request ElevenLabs music: {exc}"
            ) from exc

        try:
            bgm_service.validate_audio_file(temp_audio_path, timeout_seconds=120)
        except (bgm_service.BgmUploadError, bgm_service.BgmServiceError) as exc:
            raise ElevenLabsMusicError(
                "ElevenLabs returned audio that FFmpeg cannot decode"
            ) from exc
        os.replace(temp_audio_path, output_path)
        temp_audio_path = ""
        logger.info(
            "ElevenLabs background music generated: "
            f"output={output_path}, size={total_bytes} bytes"
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
    """Tạo nhạc nền ElevenLabs cho video được ghép phù hợp với thời lượng và hình ảnh."""
    if not get_api_key():
        raise ElevenLabsMusicError("ElevenLabs API key is required")
    if not os.path.isfile(video_path):
        raise ElevenLabsMusicError("ElevenLabs input video does not exist")
    try:
        duration = float(video_duration)
    except (TypeError, ValueError) as exc:
        raise ElevenLabsMusicError(
            "ElevenLabs video duration is invalid"
        ) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ElevenLabsMusicError("ElevenLabs video duration is invalid")
    if duration > MAX_VIDEO_DURATION_SECONDS:
        raise ElevenLabsMusicError(
            "ElevenLabs supports videos up to 600 seconds"
        )
    prompt = str(prompt or "").strip()
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ElevenLabsMusicError(
            "ElevenLabs music prompt exceeds 1000 characters"
        )

    proxy_path = ""
    try:
        proxy_path = _create_video_proxy(video_path)
        return _request_bgm(proxy_path, output_path, prompt)
    except ElevenLabsMusicError:
        raise
    except OSError as exc:
        raise ElevenLabsMusicError(
            f"ElevenLabs local file operation failed: {exc}"
        ) from exc
    finally:
        _remove_file(proxy_path)
