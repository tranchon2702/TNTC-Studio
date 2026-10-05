import json
import math
import os
import re
import socket
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from os import path
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import bgm as bgm_service
from app.services import (
    elevenlabs_music,
    llm,
    loomloom,
    material,
    metaso_minimax,
    ofox,
    sonilo,
    subtitle,
    task_artifacts,
    twelvelabs,
    video,
    volcengine_seedance,
    voice,
)
from app.services import upload_post
from app.services import state as sm
from app.utils import file_security, utils


# Yêu cầu xuất bản có thể đợi tối đa vài phút và không thể tiếp tục chiếm hạn ngạch đồng thời của tác vụ tạo video.
# Nhóm luồng có kích thước cố định giới hạn thông lượng xuất bản trong phạm vi có thể kiểm soát được trong khi cho phép tạo các sản phẩm video sau
# Lập tức tiến vào trạng thái hoàn thành.
_cross_post_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mpt-cross-post",
)
_cross_post_max_pending_tasks = max(
    1,
    int(config.app.get("upload_post_max_pending_tasks", 10)),
)
_cross_post_slots = threading.BoundedSemaphore(_cross_post_max_pending_tasks)
_cross_post_registry_lock = threading.RLock()
_cross_post_futures: dict[str, Future] = {}
_cross_post_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_ACTIVE_CROSS_POST_STATES = {
    const.CROSS_POST_STATE_PENDING,
    const.CROSS_POST_STATE_PROCESSING,
}
_CROSS_POST_STATE_WRITE_ATTEMPTS = 3
_CROSS_POST_STATE_RETRY_DELAY_SECONDS = 0.1
_LOOMLOOM_STATE_WRITE_ATTEMPTS = 3
_LOOMLOOM_STATE_RETRY_DELAY_SECONDS = 0.1
_INTERRUPTED_CROSS_POST_ERROR = (
    "cross-posting was interrupted before the process completed"
)
# Map upload-post platform ids to the social platform names llm.py accepts.
_CROSS_POST_SOCIAL_PLATFORMS = {
    "tiktok": "tiktok",
    "instagram": "instagram_reels",
    "facebook": "facebook_reels",
}
# Dịch vụ nhạc phim chỉ cần triển khai ``is_enabled`` và ``generate_bgm``. Sự khác biệt của nhà cung cấp tập trung vào
# Phần mở rộng tệp, ngoại lệ lĩnh vực và mã cảnh báo WebUI; điều phối nhiệm vụ, đoản mạch 0 tập và suy giảm lỗi
# Tất cả đều sử dụng lại cùng một đường dẫn để tránh duy trì nhiều quy trình tương tự khi thêm nhà cung cấp mới sau này.
_VIDEO_MUSIC_PROVIDERS = {
    "sonilo": {
        "service": sonilo,
        "error_type": sonilo.SoniloError,
        "suffix": ".m4a",
        "warning_code": "sonilo_bgm_failed",
        "display_name": "Sonilo",
    },
    "elevenlabs": {
        "service": elevenlabs_music,
        "error_type": elevenlabs_music.ElevenLabsMusicError,
        "suffix": ".mp3",
        "warning_code": "elevenlabs_bgm_failed",
        "display_name": "ElevenLabs",
    },
}


def _get_video_music_prompt(params: VideoParams) -> str:
    """
    Đọc các từ gợi ý thực tế được nhà cung cấp nhạc phim hiện tại sử dụng.

    Nhiệm vụ mới sử dụng thống nhất các trường trung lập với nhà cung cấp; các tham số và tác vụ lịch sử Sonilo CLI cũ vẫn có thể chỉ
    ``sonilo_bgm_prompt``, vì vậy trường cũ chỉ được đọc nếu trường chung Sonilo trống.
    """
    prompt = str(params.video_music_prompt or "").strip()
    if params.bgm_type == "sonilo" and not prompt:
        prompt = str(params.sonilo_bgm_prompt or "").strip()
    return prompt


def is_task_busy(task: dict | None) -> bool:
    """Xác định xem tác vụ vẫn đang được tạo hay giải phóng để sử dụng lại bởi tất cả các mục xóa."""
    if not task:
        return False

    state = task.get("state")
    try:
        state = int(state)
    except (TypeError, ValueError):
        pass

    # Cả việc tạo video và xuất bản đa nền tảng đều có thể tiếp tục đọc thư mục tác vụ. thống nhất như một trạng thái bận rộn,
    # Điều này có thể tránh xảy ra trường hợp một quy tắc cho phép xóa và một quy tắc khác cấm các quy tắc sau khi API và WebUI duy trì các quy tắc riêng biệt.
    # Đã loại bỏ hành vi không nhất quán.
    return (
        state == const.TASK_STATE_PROCESSING
        or task.get("cross_post_state") in _ACTIVE_CROSS_POST_STATES
    )


def _register_cross_post_future(task_id: str, future: Future) -> None:
    """Đăng ký Tương lai đã xuất bản do quy trình hiện tại nắm giữ để phục hồi và thử nghiệm khởi động nhằm xác định trạng thái chạy thực tế."""
    with _cross_post_registry_lock:
        _cross_post_futures[task_id] = future


def _unregister_cross_post_future(task_id: str, future: Future | None = None) -> None:
    """Chỉ Tương lai phù hợp mới bị xóa để ngăn các cuộc gọi lại cũ vô tình xóa tác phẩm mới được đăng ký sau đó với cùng một tác vụ."""
    with _cross_post_registry_lock:
        current = _cross_post_futures.get(task_id)
        if current is None or (future is not None and current is not future):
            return
        _cross_post_futures.pop(task_id, None)


def _is_cross_post_active_in_process(task_id: str) -> bool:
    """Xác định xem quy trình hiện tại có còn giữ các nhiệm vụ xuất bản chưa hoàn thành hay không."""
    with _cross_post_registry_lock:
        future = _cross_post_futures.get(task_id)
        return future is not None and not future.done()


def _is_windows_process_alive(process_id: int) -> bool:
    """Xác định trạng thái quy trình thông qua API Win32 chỉ đọc để tránh vô tình chấm dứt quy trình bằng os.kill."""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes xử lý các giá trị trả về không được khai báo theo mặc định là int 32 bit. Xử lý quy trình Windows 64-bit có thể
    # Do đó bị cắt bớt và chữ ký hàm Win32 phải được khai báo rõ ràng trước khi gọi.
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            # Khi một quy trình tồn tại nhưng người dùng hiện tại không có quyền truy vấn, nó phải được coi là còn hoạt động một cách thận trọng để tránh lỗi.
            # Tái chế các tác vụ xuất bản đang được thực hiện bởi các tài khoản khác.
            return True
        logger.warning(
            "failed to open cross-post owner process on Windows, "
            f"process_id: {process_id}, error_code: {error_code}"
        )
        return True

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            logger.warning(
                "failed to read cross-post owner process state on Windows, "
                f"process_id: {process_id}, error_code: {error_code}"
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_cross_post_owner_alive(owner: str | None) -> bool:
    """Xác định xem quy trình gốc của tác vụ xuất bản liên tục có còn tồn tại hay không."""
    if not owner:
        return False

    try:
        hostname, process_id_text, _ = owner.split(":", 2)
        process_id = int(process_id_text)
    except (TypeError, ValueError):
        logger.warning(f"invalid cross-post owner metadata: {owner}")
        return False

    # Các quy trình trên các máy chủ khác không thể được phát hiện một cách đáng tin cậy. Hãy thận trọng khi triển khai nhiều máy chủ dùng chung Redis
    # Nó được coi là vẫn đang chạy để tránh nút hiện tại vô tình xóa tệp video đang được nút khác đọc.
    if hostname != socket.gethostname():
        return True

    # Liệu có còn công việc xuất bản thực sự trong quy trình hiện tại hay không đã được cơ quan đăng ký Tương lai xác định chính xác. chạy đến
    # Điều này cho thấy không có Tương lai tương ứng trong sổ đăng ký. Ngay cả khi chủ sở hữu hoàn toàn nhất quán với quy trình hiện tại thì cũng nên
    # Được coi là bị gián đoạn; điều này có thể bao gồm các tình huống trong đó việc ghi trạng thái cuối cùng tiếp tục thất bại và Tương lai đã kết thúc.
    if process_id == os.getpid():
        return False

    # os.kill(pid, 0) của Windows có ngữ nghĩa khác với POSIX và có thể trực tiếp chấm dứt quá trình đích.
    # Sử dụng API Win32 chỉ yêu cầu quyền truy vấn và không gửi bất kỳ tín hiệu nào đến quy trình đích.
    if os.name == "nt":
        return _is_windows_process_alive(process_id)

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.warning(
            f"failed to inspect cross-post owner process, owner: {owner}, error: {exc}"
        )
        return True
    return True


def _mark_task_failed(
    task_id: str,
    stage: str,
    error: str,
    details: dict | None = None,
) -> dict:
    """Ghi lại thông tin lỗi có cấu trúc và lưu giữ tiến độ đạt được trước khi nhiệm vụ thất bại."""
    existing_task = None
    try:
        existing_task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read task state before failure update: {exc}")

    # Các chức năng dịch vụ cụ thể thường có nguyên nhân lỗi chính xác hơn lớp điều phối. Kiểm tra kết quả trống tiếp theo
    # Nó không thể bị ghi đè bằng bản sao chung nữa, nếu không người gọi API sẽ vẫn chỉ nhìn thấy thông tin khó hiểu.
    if (
        existing_task
        and existing_task.get("state") == const.TASK_STATE_FAILED
        and existing_task.get("error")
    ):
        return existing_task

    message = str(error or "unknown task error").strip()
    progress = int((existing_task or {}).get("progress", 0) or 0)
    logger.error(f"task failed, task_id: {task_id}, stage: {stage}, error: {message}")
    failure = {
        "task_id": task_id,
        "state": const.TASK_STATE_FAILED,
        "progress": progress,
        "failed_stage": stage,
        "error": message,
    }
    # Một số tác vụ bên ngoài đã tạo ID từ xa có thể được sử dụng để khôi phục hoặc khắc phục sự cố. Trạng thái lỗi cần được giữ lại
    # Đây là các trường không nhạy cảm nhưng không cho phép người gọi ghi đè cấu trúc trạng thái, tiến trình và lỗi thống nhất.
    failure_details = {
        key: value for key, value in dict(details or {}).items() if key not in failure
    }
    failure.update(failure_details)
    sm.state.update_task(
        task_id,
        state=failure["state"],
        progress=failure["progress"],
        failed_stage=failure["failed_stage"],
        error=failure["error"],
        **failure_details,
    )
    return failure


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        _mark_task_failed(task_id, "script", "failed to generate video script")
        return None

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        char_prompt = (
            params.character_prompt
            if getattr(params, "character_anchor_enabled", False)
            else ""
        )
        # Sau khi các tài liệu được bật và khớp theo thứ tự copywriting, bản thân các từ khóa cũng phải được tạo theo thứ tự tường thuật kịch bản;
        # Mặt khác, ngay cả khi bạn tải xuống và ghép tuần tự trong tương lai, bạn chỉ có thể sử dụng lại một bộ từ khóa chung.
        # Vấn đề “màn hình có nội dung sau xuất hiện sớm” không thể cải thiện được.
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=utils.remove_pause_tags(video_script),
            amount=8 if params.match_materials_to_script else 5,
            match_script_order=params.match_materials_to_script,
            character_prompt=char_prompt,
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        _mark_task_failed(
            task_id,
            "terms",
            "failed to generate video search terms",
        )
        return None

    # Tùy chọn sắp xếp lại ngữ nghĩa TwelveLabs Marengo: trả về thứ tự ban đầu khi không được bật mà không có bất kỳ tác dụng phụ nào.
    # Trong chế độ so khớp trình tự, bản thân thứ tự các từ khóa đã là thứ tự tường thuật của kịch bản và phải giữ nguyên nên được bỏ qua.
    if not params.match_materials_to_script:
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }
    task_artifacts.write_script_data(task_id, script_data)


def resolve_custom_audio_file(
    task_id: str,
    custom_audio_file: str | None,
    *,
    allow_server_file_input: bool = False,
) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    # A missing path that otherwise stays inside the task directory is safe to
    # report precisely. Paths outside that boundary use the same generic error
    # regardless of whether they exist, so callers cannot probe the host filesystem.
    if str(task_dir_error) == "file does not exist":
        raise task_dir_error

    # HTTP requests and other untrusted callers must never turn a submitted path
    # into a server-side file read. WebUI uploads already live in the task directory;
    # only the local CLI explicitly opts into resolving files elsewhere on the host.
    if not allow_server_file_input:
        raise ValueError(
            "custom audio file must be stored within the current task directory"
        ) from task_dir_error

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


def _resolve_reusable_voice_preview(
    task_id: str,
    params,
    video_script: str,
    voice_preview: dict | None,
) -> tuple[str, float, object] | None:
    """
    Xác minh và phân tích cú pháp toàn bộ bộ đệm thử giọng do WebUI gửi.

    Tải trọng này không phải là tham số API công khai và chỉ có thể đến từ WebUI của quy trình hiện tại. Mặc dù vậy, các tác vụ nền
    Vẫn kiểm tra lại phần copywriting và tất cả các thông số lồng tiếng, đồng thời giới hạn âm thanh nằm trong thư mục tác vụ hiện tại; mọi mâu thuẫn sẽ được
    Hoàn nguyên về TTS bình thường để ngăn các buổi thử giọng hết hạn làm ảnh hưởng đến bộ phim cuối cùng.
    """
    if not voice_preview:
        return None

    expected_values = {
        "script": str(video_script or "").strip(),
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }
    if not math.isclose(float(params.voice_volume), 1.0) or any(
        voice_preview.get(key) != value for key, value in expected_values.items()
    ):
        logger.info(
            f"skip stale voice preview cache, task_id: {task_id}, "
            "reason: voice parameters changed"
        )
        return None

    preview_file = path.realpath(str(voice_preview.get("audio_file") or ""))
    task_root = path.realpath(utils.task_dir(task_id))
    try:
        preview_is_task_local = path.commonpath([task_root, preview_file]) == task_root
    except ValueError:
        preview_is_task_local = False

    duration = voice_preview.get("duration")
    sub_maker = voice_preview.get("sub_maker")
    if (
        not preview_is_task_local
        or not path.isfile(preview_file)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or sub_maker is None
    ):
        logger.warning(
            f"skip invalid voice preview cache, task_id: {task_id}, "
            f"audio_file: {preview_file or '<empty>'}"
        )
        return None

    logger.info(
        f"using full voice preview audio, task_id: {task_id}, duration: {duration:.2f}s"
    )
    return preview_file, math.ceil(duration), sub_maker


def generate_audio(
    task_id,
    params,
    video_script,
    voice_preview=None,
    *,
    allow_server_file_input: bool = False,
):
    """
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    """
    logger.info("\n\n## generating audio")
    # /mô hình yêu cầu âm thanh và /phụ đề không chứa tệp custom_audio_file,
    # Việc đọc tương thích được thực hiện ở đây để tránh lỗi thuộc tính khi điều chỉnh trực tiếp giao diện.
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    try:
        custom_audio_file = resolve_custom_audio_file(
            task_id,
            requested_custom_audio_file,
            allow_server_file_input=allow_server_file_input,
        )
    except ValueError as exc:
        _mark_task_failed(
            task_id,
            "audio",
            f"invalid custom audio file: {exc}",
        )
        return None, None, None

    if not custom_audio_file:
        reusable_preview = _resolve_reusable_voice_preview(
            task_id,
            params,
            video_script,
            voice_preview,
        )
        if reusable_preview:
            return reusable_preview

        logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            _mark_task_failed(
                task_id,
                "audio",
                "failed to synthesize audio; verify the selected voice and TTS connectivity",
            )
            return None, None, None
        # Measure the real written audio_file, not sub_maker.cues[-1].end:
        # the latter is the last WORD BOUNDARY, and TTS leaves a fixed tail
        # past it (Edge TTS: ~0.88s at any length - 19% of a 7-word clip but
        # 1.4% of a 153-word one, so short scripts suffer most). The
        # under-count sizes paid generate_bgm() calls, is reported as
        # audio_duration to the API/WebUI, and under-sources
        # download_videos() material, scaled by video_count.
        file_duration = voice.get_audio_duration(audio_file)
        audio_duration = math.ceil(
            file_duration if file_duration > 0 else voice.get_audio_duration(sub_maker)
        )
        if audio_duration == 0:
            _mark_task_failed(task_id, "audio", "generated audio duration is zero")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            _mark_task_failed(
                task_id,
                "audio",
                "custom audio duration is zero",
            )
            return None, None, None
        return custom_audio_file, audio_duration, None


def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    """
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    """
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if not subtitle_provider:
        logger.info("subtitle provider is empty, skip subtitle generation")
        return ""

    if sub_maker is None and subtitle_provider != "whisper":
        # Âm thanh tùy chỉnh sẽ không đi qua TTS, do đó không có phản hồi TTS từ Edge/Azure, v.v.
        # dòng thời gian của sub_maker. Chỉ Whisper mới có thể chép phụ đề trực tiếp từ tệp âm thanh;
        # Các nhà cung cấp phụ đề khác tiếp tục duy trì hoạt động cũ của họ và tránh tạo ra các mốc thời gian trống do nhầm lẫn.
        logger.warning(
            "subtitle maker is missing, skip subtitle generation for provider: "
            f"{subtitle_provider}"
        )
        return ""

    is_word_level = getattr(params, "subtitle_display_mode", "sentence") == "word_by_word"

    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script,
            sub_maker=sub_maker,
            subtitle_file=subtitle_path,
            word_level=is_word_level,
        )
        if not os.path.exists(subtitle_path):
            # Phụ đề Edge đôi khi không tạo ra tệp vì dòng thời gian và bản sao không khớp nhau. Không phải ở đây
            # Tự động chuyển sang Whisper, nếu không lần đầu bị lỗi có thể tải hàng gigabyte mà người dùng không hề biết
            # người mẫu. Tải mô hình chỉ được phép khi Whisper được cấu hình rõ ràng và sẽ được giữ lại nếu Edge bị lỗi.
            # Bỏ phụ đề của video và ghi lại lý do để tránh tình trạng hao phí ổ đĩa và mạng không mong muốn.
            logger.warning(
                "edge subtitle generation did not produce a subtitle file; "
                "skip subtitles without falling back to whisper"
            )
            return ""

    if subtitle_provider == "whisper":
        subtitle.create(
            audio_file=audio_file,
            subtitle_file=subtitle_path,
            word_level=is_word_level,
        )
        if not is_word_level:
            logger.info("\n\n## correcting subtitle")
            subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def get_video_materials(
    task_id,
    params,
    video_terms,
    audio_duration,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            _mark_task_failed(
                task_id,
                "materials",
                "no valid local video materials were found",
            )
            return None
        return [material_info.url for material_info in materials]
    elif params.video_source == "loomloom":
        if not isinstance(
            loomloom_video_request, loomloom.LoomLoomConfirmedVideoRequest
        ):
            _mark_task_failed(
                task_id,
                "materials",
                "LoomLoom video generation requires a confirmed quote",
            )
            return None

        request = loomloom_video_request
        logger.info(
            "\n\n## generating "
            f"{len(request.batch.input_rows)} video materials with LoomLoom"
        )
        run_id = ""
        try:
            request.validate()
            backend = loomloom.LoomLoomVideoBackend(request.settings)
            execution = backend.execute(
                request.batch,
                client_request_id=request.client_request_id,
                listing_version_id=request.listing_version_id,
                confirm=True,
            )
            run_id = execution.run_id
            # Việc trả về lệnh thực thi cho biết rằng tác vụ đã thanh toán đã được đầu cuối từ xa chấp nhận. ID chạy phải được viết trước
            # Nhật ký quy trình, ngay cả khi phần phụ trợ trạng thái như Redis sau đó không khả dụng, nhân viên vận hành và bảo trì vẫn có thể dựa vào nhật ký
            # Khi định vị các tác vụ ở phía WinCloud, mã định danh duy nhất không thể chỉ tồn tại trong các biến cục bộ.
            logger.info(
                "LoomLoom paid video run created: "
                f"task_id={task_id}, run_id={run_id}, "
                f"listing_version_id={request.listing_version_id}"
            )
            # ID từ xa được ghi lại ngay khi tác vụ phải trả phí được tạo. Ngay cả khi các cuộc thăm dò tiếp theo hết thời gian, nhật ký và nhiệm vụ
            # Trạng thái này vẫn có thể giúp người dùng hoặc nhân viên hỗ trợ nền tảng xác định và truy xuất các sản phẩm đã tạo. phụ trợ trạng thái
            # Lỗi chỉ có thể làm giảm khả năng quan sát và không thể làm gián đoạn các tác vụ từ xa cũng như quá trình tải xuống sản phẩm đã bắt đầu sạc.
            _record_loomloom_run_reference(
                task_id=task_id,
                run_id=run_id,
                listing_version_id=request.listing_version_id,
            )
            backend.wait_for_run(run_id)
            return list(
                backend.download_video_results(
                    run_id,
                    utils.task_dir(task_id),
                )
            )
        except (loomloom.LoomLoomError, ValueError) as exc:
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details={
                    "loomloom_run_id": run_id,
                    "loomloom_listing_version_id": request.listing_version_id,
                },
            )
            return None
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        # Chế độ khớp tuần tự chỉ có hiệu lực khi người dùng bật nó một cách rõ ràng. Ở đây bắt buộc phải tải tài liệu theo thứ tự từ khóa
        # Việc bỏ phiếu ngăn chặn một từ khóa ban đầu nhất định tải xuống quá nhiều tài liệu và loại bỏ các chủ đề kịch bản tiếp theo ra khỏi dòng thời gian cuối cùng.
        try:
            downloaded_videos = material.download_videos(
                task_id=task_id,
                search_terms=video_terms,
                source=params.video_source,
                video_aspect=params.video_aspect,
                video_concat_mode=(
                    VideoConcatMode.sequential
                    if params.match_materials_to_script
                    else params.video_concat_mode
                ),
                audio_duration=audio_duration * params.video_count,
                max_clip_duration=params.video_clip_duration,
                match_script_order=params.match_materials_to_script,
                character_prompt=(
                    params.character_prompt
                    if getattr(params, "character_anchor_enabled", False)
                    else ""
                ),
            )
        except volcengine_seedance.VolcEngineSeedanceError as exc:
            # Trạng thái chưa được xác nhận và được tạo nhưng không tải xuống được đều tương ứng với một đầu từ xa có thể được khôi phục trong bảng điều khiển Ark.
            # Nhiệm vụ. Thống nhất ghi trạng thái lỗi từ task_id do ngoại lệ mang theo để tránh các nhánh ngoại lệ khác nhau
            # Mỗi bản duy trì thông tin khôi phục và lại bỏ lỡ thông tin đó trong các lần mở rộng tiếp theo.
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"volcengine_seedance_task_id": remote_task_id}
                if remote_task_id
                else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except ofox.OFoxError as exc:
            # Ngữ nghĩa khôi phục tương tự như Ark: Trạng thái chưa được xác nhận và được tạo nhưng không tải xuống được đều tương ứng với một ngữ nghĩa có sẵn trong
            # Các tác vụ từ xa được bảng điều khiển OOX khôi phục sẽ ghi thống nhất trạng thái lỗi từ task_id do ngoại lệ mang theo.
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"ofox_task_id": remote_task_id} if remote_task_id else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        except metaso_minimax.MetasoMiniMaxError as exc:
            # Nhiệm vụ Secret Tower và nhiệm vụ Ark sử dụng các lối vào phục hồi và tên trường khác nhau và không thể hợp nhất thành một.
            # Remote_task_id bị xáo trộn. Giữ tiền tố nhà cung cấp rõ ràng cho API và WebUI
            # và nhật ký vận hành và bảo trì để định vị trực tiếp nền tảng tương ứng.
            remote_task_id = str(getattr(exc, "task_id", "") or "").strip()
            details = (
                {"metaso_minimax_task_id": remote_task_id} if remote_task_id else None
            )
            _mark_task_failed(
                task_id,
                "materials",
                str(exc),
                details=details,
            )
            return None
        if not downloaded_videos:
            _mark_task_failed(
                task_id,
                "materials",
                f"failed to download video materials from {params.video_source}",
            )
            return None
        return downloaded_videos


def _record_loomloom_run_reference(
    *, task_id: str, run_id: str, listing_version_id: str
) -> bool | None:
    """
    Cố gắng hết sức để lưu các LoomLoom Run trả phí đã được tạo và không để lỗi trạng thái làm gián đoạn các tác vụ từ xa.

    Trả về True có nghĩa là quá trình lưu đã thành công, Sai có nghĩa là bản ghi tác vụ không còn tồn tại và Không có nghĩa là phần phụ trợ trạng thái
    Vẫn không khả dụng sau số lần thử lại có giới hạn. Người gọi nên tiếp tục bỏ phiếu và tải xuống bất kể kết quả như thế nào, bởi vì
    thực thi đã có tác dụng phụ phải trả tiền bên ngoài, việc dừng quy trình cục bộ sẽ chỉ khiến sản phẩm khó truy xuất hơn.
    """
    fields = {
        "loomloom_run_id": run_id,
        "loomloom_listing_version_id": listing_version_id,
    }
    for attempt in range(1, _LOOMLOOM_STATE_WRITE_ATTEMPTS + 1):
        try:
            updated = sm.state.patch_task(task_id, **fields)
        except Exception as exc:
            if attempt >= _LOOMLOOM_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    "failed to persist LoomLoom paid run after retries: "
                    f"task_id={task_id}, run_id={run_id}, attempts={attempt}, "
                    f"error={exc}"
                )
                return None
            logger.warning(
                "retry LoomLoom paid run state update: "
                f"task_id={task_id}, run_id={run_id}, attempt={attempt}, "
                f"error={exc}"
            )
            time.sleep(_LOOMLOOM_STATE_RETRY_DELAY_SECONDS)
            continue

        if updated is False:
            logger.warning(
                "could not persist LoomLoom paid run because task is missing: "
                f"task_id={task_id}, run_id={run_id}"
            )
        return updated

    return None


def _get_material_source_groups(task_id: str, video_paths: list[str]) -> dict[str, str]:
    """Recover keyword groups from the downloaded material manifest."""
    try:
        with open(path.join(utils.task_dir(task_id), "script.json"), encoding="utf-8") as file:
            payload = json.load(file)
        groups = {
            record["local_file"]: record["search_term"]
            for record in payload.get("material_sources", [])
            if isinstance(record, dict)
            and isinstance(record.get("local_file"), str)
            and isinstance(record.get("search_term"), str)
            and record["search_term"]
        }
        return {
            file: groups[path.basename(file)]
            for file in video_paths
            if path.basename(file) in groups
        }
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        logger.warning(f"Cannot read material keyword groups: task_id={task_id}, error={exc}")
        return {}


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, audio_duration
):
    final_video_paths = []
    combined_video_paths = []
    warnings = []
    allocate_batch_materials = params.video_count > 1 and params.video_source in {
        "pexels", "pixabay", "coverr", "local"
    }
    source_usage = {}
    material_selections = []
    source_groups = (
        _get_material_source_groups(task_id, downloaded_videos)
        if (allocate_batch_materials and params.match_materials_to_script
            and params.video_source != "local")
        else {}
    )
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_requested = (
        video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    # Matching preserves keyword order; batch allocation varies each keyword's candidates.
    if params.match_materials_to_script:
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        used_video_paths = []
        batch_options = (
            {
                "source_usage": source_usage,
                "source_groups": source_groups,
                "used_video_paths": used_video_paths,
            }
            if allocate_batch_materials else {}
        )
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_fit_mode=params.video_fit_mode,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
            clip_speed=params.video_clip_speed,
            **batch_options,
        )
        if allocate_batch_materials:
            selected_sources = list(dict.fromkeys(used_video_paths))
            reused_sources = [file for file in selected_sources if source_usage.get(file, 0)]
            for file in selected_sources:
                source_usage[file] = source_usage.get(file, 0) + 1
            material_selections.append({
                "video_index": index,
                "local_files": [path.basename(file) for file in used_video_paths],
                "reused_files": [path.basename(file) for file in reused_sources],
            })
            task_artifacts.patch_script_data(task_id, material_selections=material_selections)
            logger.info(
                f"Batch material allocation: video_index={index}, "
                f"sources={len(selected_sources)}, reused_sources={len(reused_sources)}"
            )
            if reused_sources:
                warnings.append({
                    "code": "batch_materials_reused",
                    "video_index": index,
                    "count": len(reused_sources),
                })

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        # Trong chế độ nhạc nền video, tính năng phân tích BGM mặc định bị vô hiệu hóa rõ ràng để ngăn chặn bgm_file còn sót lại từ các tác vụ cũ.
        # lạm dụng. Chỉ khi âm lượng lớn hơn 0 thì tác nhân mới được tạo và API trả phí mới được gọi; nếu âm lượng bằng 0, nó sẽ bị bỏ qua một cách đồng đều.
        bgm_file_override = "" if video_music_provider else None
        if video_music_requested:
            service = video_music_provider["service"]
            display_name = video_music_provider["display_name"]
            warning_code = video_music_provider["warning_code"]
            generated_bgm_path = path.join(
                utils.task_dir(task_id),
                (f"{params.bgm_type}-bgm-{index}{video_music_provider['suffix']}"),
            )
            try:
                service.generate_bgm(
                    video_path=combined_video_path,
                    output_path=generated_bgm_path,
                    video_duration=audio_duration,
                    prompt=_get_video_music_prompt(params),
                )
                bgm_file_override = generated_bgm_path
            except video_music_provider["error_type"] as exc:
                # Khi video, tường thuật và phụ đề đã được tạo xong, lỗi tạm thời của nhạc nền bên thứ ba sẽ không lãng phí toàn bộ
                # Nhiệm vụ. BGM bị tắt rõ ràng đối với video hiện tại và kết quả hạ cấp được trả về WebUI để nhắc nhở người dùng.
                logger.warning(
                    f"{display_name} BGM generation failed: task_id={task_id}, "
                    f"video_index={index}, error={exc}"
                )
                bgm_file_override = ""
                warnings.append({"code": warning_code, "video_index": index})

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        bgm_mix_succeeded = video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
            bgm_file_override=bgm_file_override,
        )
        if (
            video_music_provider is not None
            and bgm_file_override
            and not bgm_mix_succeeded
        ):
            # Bên thứ ba đã trả lại thành công và vượt qua xác minh FFmpeg, nhưng bản kết hợp cuối cùng của MoviePy vẫn có thể
            # Không thành công do môi trường hoạt động. Dịch vụ video sẽ giữ lại đoạn phim đã hoàn thiện không có BGM; khi việc tạo API không thành công
            # ghi đè trống nên cảnh báo sẽ không được thêm vào nhiều lần.
            warnings.append(
                {
                    "code": video_music_provider["warning_code"],
                    "video_index": index,
                }
            )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths, warnings


def _patch_cross_post_state(task_id: str, **kwargs) -> bool | None:
    """Các trường phát hành cập nhật an toàn; số lần thử lại có giới hạn trong trường hợp lỗi phụ trợ trạng thái nhất thời."""
    for attempt in range(1, _CROSS_POST_STATE_WRITE_ATTEMPTS + 1):
        try:
            return sm.state.patch_task(task_id, **kwargs)
        except Exception as exc:
            # Việc ngắt kết nối ngắn hạn khỏi Redis sẽ không khiến các tác vụ bị kẹt trong tình trạng chờ xử lý/đang chờ xử lý mãi mãi. Trạng thái phát hành
            # Tần suất viết rất thấp. Một số lần cố định và thời gian chờ ngắn có thể được sử dụng để khắc phục các lỗi nhất thời. Đồng thời
            # Tránh chặn vô hạn các chủ đề nền. Lỗi cuối cùng giữ lại ngăn xếp hoàn chỉnh để dễ dàng định vị.
            if attempt >= _CROSS_POST_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to update cross-post state after retries, "
                    f"task_id: {task_id}, fields: {', '.join(kwargs)}, "
                    f"attempts: {attempt}, error: {exc}"
                )
                return None

            logger.warning(
                f"retry cross-post state update, task_id: {task_id}, "
                f"fields: {', '.join(kwargs)}, attempt: {attempt}, error: {exc}"
            )
            time.sleep(_CROSS_POST_STATE_RETRY_DELAY_SECONDS)

    return None


def _record_cross_post_failure(
    task_id: str,
    error: Exception,
    results: list[dict] | None = None,
) -> None:
    """Các lỗi xuất bản được lưu lại trên cơ sở nỗ lực tốt nhất; thông tin chẩn đoán được nhật ký giữ lại khi phần phụ trợ trạng thái không khả dụng."""
    updated = _patch_cross_post_state(
        task_id,
        cross_post_state=const.CROSS_POST_STATE_FAILED,
        cross_post_results=results or None,
        cross_post_error=str(error),
        cross_post_owner=None,
    )
    if updated is False:
        logger.warning(f"discard cross-post failure for missing task: {task_id}")


def _ensure_cross_post_terminal_state(task_id: str) -> None:
    """Sau khi Tương lai kết thúc, những nhiệm vụ còn đang hoạt động sẽ hội tụ thành thất bại."""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        # Đây đã là lệnh gọi lại cuối cùng của Tương lai và không có lệnh gọi đồng bộ nào tiếp theo để xử lý ngoại lệ.
        # Sau khi phần phụ trợ trạng thái được khôi phục, lần khởi động quy trình tiếp theo sẽ vẫn xử lý trạng thái cũ thông qua logic khôi phục.
        logger.exception(
            f"failed to verify final cross-post state, task_id: {task_id}, error: {exc}"
        )
        return

    if not task or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES:
        return

    logger.warning(
        f"cross-post worker ended without terminal state, task_id: {task_id}, "
        f"state: {task.get('cross_post_state')}"
    )
    _record_cross_post_failure(
        task_id,
        RuntimeError("cross-post worker ended without persisting a terminal state"),
        task.get("cross_post_results"),
    )


def recover_interrupted_cross_posts(page_size: int = 100) -> int | None:
    """
    Đánh dấu các tác vụ xuất bản không thể khôi phục được sau khi khởi động lại quá trình là không thành công.

    Xuất bản đa nền tảng sử dụng nhóm luồng trong quy trình hiện tại chứ không phải hàng đợi tác vụ liên tục. Khi quá trình bắt đầu,
    Việc chờ xử lý/đang chờ xử lý còn lại trong Redis sẽ không tự động tiếp tục thực thi; nếu họ tiếp tục bị đối xử như
    Trong khi chạy, người dùng sẽ không bao giờ có thể xóa tác vụ. Trạng thái quét phân trang ở đây chỉ xử lý tiến trình hiện tại chứ không
    Tương ứng với các bản ghi hoạt động của Tương lai và giữ lại kết quả video được tạo.
    """
    recovered = 0
    page = 1

    while True:
        try:
            tasks, total = sm.state.get_all_tasks(page, page_size)
        except Exception as exc:
            logger.exception(f"failed to recover interrupted cross-post tasks: {exc}")
            return None

        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if (
                not task_id
                or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES
                or _is_cross_post_active_in_process(task_id)
                or _is_cross_post_owner_alive(task.get("cross_post_owner"))
            ):
                continue

            updated = _patch_cross_post_state(
                task_id,
                cross_post_state=const.CROSS_POST_STATE_FAILED,
                cross_post_error=_INTERRUPTED_CROSS_POST_ERROR,
                cross_post_owner=None,
            )
            if updated is True:
                recovered += 1

        if page * page_size >= total or not tasks:
            break
        page += 1

    if recovered:
        logger.warning(f"recovered interrupted cross-post tasks: {recovered}")
    return recovered


def _run_cross_post(
    task_id: str,
    video_paths: tuple[str, ...],
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: tuple[str, ...],
    youtube_privacy_status: str,
    youtube_made_for_kids: bool = False,
) -> None:
    """Việc xuất bản đa nền tảng được thực hiện ở chế độ nền và chỉ các trường tác vụ liên quan đến xuất bản mới được thêm vào."""
    results = []
    try:
        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_PROCESSING,
            cross_post_error=None,
            cross_post_owner=_cross_post_process_owner,
        )
        if state_updated is not True:
            # Sai có nghĩa là tác vụ đã bị xóa, Không có nghĩa là phần phụ trợ trạng thái tạm thời không khả dụng. Trong cả hai trường hợp
            # Giao diện của bên thứ ba sẽ không được tiếp tục gọi, nếu không người dùng sẽ không thể truy vấn hoặc kiểm soát bản phát hành này.
            if state_updated is False:
                logger.warning(f"skip cross-post for missing task: {task_id}")
            else:
                _record_cross_post_failure(
                    task_id,
                    RuntimeError("failed to persist cross-post processing state"),
                )
            return

        logger.info(
            f"cross-post started, task_id: {task_id}, platforms: {', '.join(platforms)}"
        )
        youtube_extra = None
        post_title = video_subject or "Check out this video! #shorts #viral"
        if platforms:
            has_youtube = any(platform.startswith("youtube") for platform in platforms)
            social_platform = "youtube_shorts"
            if not has_youtube:
                first = (platforms[0] or "").strip().lower()
                # llm.py resolves unknown ids to its default platform.
                social_platform = _CROSS_POST_SOCIAL_PLATFORMS.get(first, first)
            metadata = llm.generate_social_metadata(
                video_subject=video_subject,
                video_script=video_script,
                language=video_language or "",
                platform=social_platform,
            )
            if has_youtube:
                youtube_extra = {
                    "youtube_title": metadata.get("title", video_subject),
                    "youtube_description": metadata.get("caption", ""),
                    "tags": metadata.get("hashtags", []),
                    "privacyStatus": youtube_privacy_status,
                    "selfDeclaredMadeForKids": youtube_made_for_kids,
                    "containsSyntheticMedia": True,
                }
            post_title = (
                metadata.get("caption")
                or metadata.get("title")
                or video_subject
                or "Check out this video! #shorts #viral"
            )

        for video_path in video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=post_title,
                platforms=list(platforms),
                youtube_extra=youtube_extra,
            )
            if not isinstance(result, dict):
                result = {
                    "success": False,
                    "error": "Upload-Post returned an invalid response",
                }
            results.append(result)

        failures = [result for result in results if not result.get("success")]
        if failures:
            error_messages = [
                str(
                    result.get("error")
                    or result.get("message")
                    or "unknown upload error"
                )
                for result in failures
            ]
            cross_post_state = const.CROSS_POST_STATE_FAILED
            cross_post_error = "; ".join(error_messages)
            logger.warning(
                f"cross-post completed with failures, task_id: {task_id}, "
                f"failed: {len(failures)}, total: {len(results)}"
            )
        else:
            cross_post_state = const.CROSS_POST_STATE_COMPLETE
            cross_post_error = None
            logger.success(
                f"cross-post completed, task_id: {task_id}, videos: {len(results)}"
            )

        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=cross_post_state,
            cross_post_results=results,
            cross_post_error=cross_post_error,
            cross_post_owner=None,
        )
        if state_updated is False:
            logger.warning(f"discard cross-post result for missing task: {task_id}")
        elif state_updated is None:
            # Khi quá trình tải lên kết thúc nhưng kết quả không được lưu giữ thì không thể tiếp tục quá trình xử lý.
            # Việc ghi trạng thái không thành công sẽ được thử lại với số lần thử lại có giới hạn, ít nhất là cho phép người gọi có được trạng thái cuối cùng rõ ràng.
            _record_cross_post_failure(
                task_id,
                RuntimeError("failed to persist final cross-post result"),
                results,
            )
    except Exception as exc:
        # Việc không xuất bản chỉ ảnh hưởng đến trạng thái xuất bản và không thể ghi đè ngược các tác vụ video đã hoàn thành.
        # Văn bản gốc của ngoại lệ được ghi vào trạng thái tác vụ và người gọi API có thể xác định sự cố mà không cần truy cập nhật ký máy chủ.
        logger.exception(f"cross-post failed, task_id: {task_id}, error: {exc}")
        _record_cross_post_failure(task_id, exc, results)


def _run_cross_post_with_slot(*args) -> None:
    """Thực hiện các tác vụ xuất bản và đảm bảo rằng dung lượng hàng đợi được trả về khi thành công, thất bại hoặc ngoại lệ."""
    try:
        _run_cross_post(*args)
    except Exception as exc:
        # _run_cross_post đã xử lý ngoại lệ dự kiến; đây là dòng bảo vệ cuối cùng chống lại sự bổ sung trong tương lai
        # Các ngoại lệ do logic đưa ra chỉ được lưu trữ trong Hợp đồng tương lai mà không ai có thể đọc được.
        task_id = str(args[0]) if args else "unknown"
        logger.exception(f"cross-post worker crashed, task_id: {task_id}, error: {exc}")
        if args:
            _record_cross_post_failure(task_id, exc)
    finally:
        _cross_post_slots.release()


def _finalize_cross_post_future(task_id: str, future: Future) -> None:
    """Dọn dẹp các đăng ký trong tương lai và đảm bảo việc hủy, ngoại lệ và lỗi ghi trạng thái đều hội tụ."""
    _unregister_cross_post_future(task_id, future)

    try:
        error = future.exception()
    except CancelledError:
        logger.warning(f"cross-post future was cancelled, task_id: {task_id}")
        # Khi Tương lai bị hủy trước khi bắt đầu thực thi, cuối cùng thì công nhân sẽ không chạy, vì vậy nó cần
        # Trả về dung lượng hàng đợi trong lệnh gọi lại và thay đổi trạng thái liên tục thành lỗi.
        _cross_post_slots.release()
        _record_cross_post_failure(
            task_id,
            RuntimeError("cross-post job was cancelled before execution"),
        )
        return
    except Exception as exc:
        logger.exception(
            f"failed to inspect cross-post future, task_id: {task_id}, error: {exc}"
        )
        _ensure_cross_post_terminal_state(task_id)
        return

    if error is not None:
        logger.error(
            f"cross-post future failed, task_id: {task_id}, "
            f"error: {type(error).__name__}: {error}"
        )

    _ensure_cross_post_terminal_state(task_id)


def _schedule_cross_post(
    task_id: str,
    video_paths: list[str],
    params: VideoParams,
    video_script: str,
    platforms: list[str],
    youtube_privacy_status: str,
    youtube_made_for_kids: bool = False,
) -> str | None:
    """Gửi một nhiệm vụ xuất bản nền; trả về Không nếu thành công và trả về lý do lỗi có thể truy vấn nếu lập kế hoạch không thành công."""
    if not _cross_post_slots.acquire(blocking=False):
        error = "cross-post queue is full; publishing was skipped"
        logger.warning(
            f"skip cross-post because queue is full, task_id: {task_id}, "
            f"capacity: {_cross_post_max_pending_tasks}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=error,
            cross_post_owner=None,
        )
        return error

    try:
        future = _cross_post_executor.submit(
            _run_cross_post_with_slot,
            task_id,
            tuple(video_paths),
            params.video_subject or "",
            video_script,
            params.video_language or "",
            tuple(platforms),
            youtube_privacy_status,
            youtube_made_for_kids,
        )
        _register_cross_post_future(task_id, future)
        future.add_done_callback(partial(_finalize_cross_post_future, task_id))
    except RuntimeError as exc:
        _unregister_cross_post_future(task_id)
        _cross_post_slots.release()
        logger.exception(
            f"failed to schedule cross-post, task_id: {task_id}, error: {exc}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=f"failed to schedule cross-post: {exc}",
            cross_post_owner=None,
        )
        return f"failed to schedule cross-post: {exc}"

    return None


def _run_pipeline(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    allow_server_file_input: bool = False,
):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "volcengine_seedance"
        and not volcengine_seedance.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "Volcano Engine Seedance requires an Ark API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "ofox"
        and not ofox.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "OFox video generation requires an OFox API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "metaso_minimax"
        and not metaso_minimax.is_enabled()
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "Metaso MiniMax requires an API key",
        )

    if (
        stop_at in {"materials", "video"}
        and params.video_source == "openai_image"
        and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        )
    ):
        return _mark_task_failed(
            task_id,
            "preflight",
            "OpenAI image source requires openai_image_base_url and "
            "openai_image_model in config.toml (openai_image_api_keys is "
            "optional for local gateways that need no auth)",
        )

    # Nhà cung cấp nhạc phim chỉ cần thiết cho quá trình sản xuất hoàn chỉnh. Chặn sớm các nhiệm vụ hoàn thành bị thiếu Chìa khóa để tránh
    # LLM, TTS và tín dụng dịch vụ vật chất được sử dụng trước tiên; giao diện sản phẩm trung gian vẫn có thể được sử dụng độc lập.
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_enabled = (
        stop_at == "video"
        and video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    if video_music_enabled:
        service = video_music_provider["service"]
        display_name = video_music_provider["display_name"]
        if not service.is_enabled():
            return _mark_task_failed(
                task_id,
                "preflight",
                f"{display_name} background music requires an API key",
            )

        # WebUI giới hạn độ dài đầu vào, nhưng các tác vụ API, CLI và lịch sử có thể bỏ qua các điều khiển giao diện người dùng.
        # Trước khi tạo kịch bản, lồng tiếng và tài liệu, hãy xác minh lại theo giới hạn trên của nhà cung cấp để tránh tổng hợp video hoàn chỉnh.
        # Chỉ có yêu cầu từ bên thứ ba bị từ chối. Lớp dịch vụ vẫn giữ nguyên xác thực giống như tuyến phòng thủ cuối cùng khi gọi trực tiếp.
        music_prompt = _get_video_music_prompt(params)
        max_prompt_length = int(getattr(service, "MAX_PROMPT_LENGTH", 0) or 0)
        if max_prompt_length and len(music_prompt) > max_prompt_length:
            return _mark_task_failed(
                task_id,
                "preflight",
                (f"{display_name} music prompt exceeds {max_prompt_length} characters"),
            )

        # Nhà cung cấp có thể chọn cung cấp dịch vụ kiểm tra trước tài khoản miễn phí. Chức năng kiểm tra chỉ nên ném xác định
        # Lỗi; khi không thể xác nhận sự dao động của mạng hoặc phạm vi cho phép, lớp dịch vụ sẽ ghi lại cảnh báo và tiếp tục tạo thực tế.
        validate_access = getattr(service, "validate_generation_access", None)
        if callable(validate_access):
            try:
                validate_access()
            except video_music_provider["error_type"] as exc:
                return _mark_task_failed(task_id, "preflight", str(exc))

    # Chỉ các tập lệnh/thuật ngữ trung gian không yêu cầu FFmpeg (chúng không tạo ra âm thanh hoặc video). API,
    # Cả CLI và WebUI đều thực hiện các tác vụ thông qua mục chia sẻ này, do đó việc phát hiện được thống nhất ở đây thay vì
    # Việc lặp lại việc kiểm tra ở mỗi lối vào có thể đảm bảo rằng hoạt động của ba đường dẫn là nhất quán. Đặt nó vào bản nhạc Key để xác minh
    # Sau đó, để không thay đổi thứ tự "đầu tiên không thành công" và thông báo lỗi ban đầu của những lần xác minh đó.
    if stop_at not in ("script", "terms") and not utils.check_ffmpeg_ready():
        return _mark_task_failed(
            task_id,
            "preflight",
            "ffmpeg is not available; install ffmpeg or set app.ffmpeg_path "
            "in config.toml to a working ffmpeg executable",
        )

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        error = (
            video_script.removeprefix("Error: ").strip()
            if isinstance(video_script, str) and "Error: " in video_script
            else "failed to generate video script"
        )
        return _mark_task_failed(task_id, "script", error)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            return _mark_task_failed(
                task_id,
                "terms",
                "failed to generate video search terms",
            )

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id,
        params,
        video_script,
        voice_preview=voice_preview,
        allow_server_file_input=allow_server_file_input,
    )
    if not audio_file:
        return _mark_task_failed(
            task_id,
            "audio",
            "failed to prepare narration audio",
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    downloaded_videos = get_video_materials(
        task_id,
        params,
        video_terms,
        audio_duration,
        loomloom_video_request=loomloom_video_request,
    )
    if not downloaded_videos:
        return _mark_task_failed(
            task_id,
            "materials",
            "failed to prepare video materials",
        )

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # Chỉ có quá trình tạo video hoàn chỉnh mới cần xử lý chế độ ghép video;
    # Điều này tránh các yêu cầu như/phụ đề và/âm thanh truy cập vào các trường không tồn tại.
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths, generation_warnings = (
        generate_final_videos(
            task_id,
            params,
            downloaded_videos,
            audio_file,
            subtitle_path,
            audio_duration,
        )
    )

    if not final_video_paths:
        return _mark_task_failed(
            task_id,
            "video",
            "failed to generate final video",
        )

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. Trước tiên hãy hoàn thành nhiệm vụ tạo video, sau đó gửi nó để xuất bản trên nhiều nền tảng nếu cần. Tải lên của bên thứ ba có thể tốn thời gian
    # Nó không chặn việc trả lại kết quả video trong vài phút và cũng không ảnh hưởng xấu đến các video đã được tạo.
    cross_post_enabled = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    platforms = (
        list(upload_post.upload_post_service.platforms) if cross_post_enabled else []
    )
    should_cross_post = cross_post_enabled and bool(platforms)
    if cross_post_enabled and not platforms:
        logger.warning(
            f"skip cross-post because no platforms are configured, task_id: {task_id}"
        )
    cross_post_state = const.CROSS_POST_STATE_PENDING if should_cross_post else None

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": _cross_post_process_owner if should_cross_post else None,
        "warnings": generation_warnings or None,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )

    if should_cross_post:
        scheduling_error = _schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=video_script,
            platforms=platforms,
            youtube_privacy_status=(
                upload_post.upload_post_service.youtube_privacy_status
            ),
            # Đã sửa lỗi lựa chọn đối tượng khi xếp hàng, các sửa đổi tiếp theo đối với WebUI sẽ không thay đổi phần khai báo của các video được xếp hàng.
            youtube_made_for_kids=(
                upload_post.upload_post_service.youtube_made_for_kids
            ),
        )
        # Hàng đợi đã đầy hoặc nhóm luồng bị đóng, đây là lỗi lập kế hoạch nhận biết đồng bộ hóa. Trạng thái nhiệm vụ đã được xác định bởi chức năng lập lịch
        # Cập nhật, ảnh chụp nhanh trả về được sửa đồng bộ ở đây để ngăn người gọi nhận được thông báo đang chờ xử lý không nhất quán với các truy vấn tiếp theo.
        if scheduling_error:
            kwargs["cross_post_state"] = const.CROSS_POST_STATE_FAILED
            kwargs["cross_post_error"] = scheduling_error
            kwargs["cross_post_owner"] = None

    return kwargs


def start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    voice_preview: dict | None = None,
    loomloom_video_request: loomloom.LoomLoomConfirmedVideoRequest | None = None,
    allow_server_file_input: bool = False,
):
    """
    Thực thi quy trình nhiệm vụ và đảm bảo rằng các trường hợp ngoại lệ không mong muốn cũng được chuyển đổi thành trạng thái lỗi có thể truy vấn được.

    ``allow_server_file_input`` chỉ dành cho sử dụng CLI gốc. API HTTP và WebUI phải được giữ nguyên
    Giá trị mặc định để âm thanh tùy chỉnh luôn được liên kết với thư mục tác vụ hiện tại.
    """
    try:
        return _run_pipeline(
            task_id,
            params,
            stop_at=stop_at,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
            allow_server_file_input=allow_server_file_input,
        )
    except Exception as exc:
        logger.exception(
            f"unexpected task pipeline failure, task_id: {task_id}, error: {exc}"
        )
        return _mark_task_failed(
            task_id,
            "pipeline",
            f"{type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
