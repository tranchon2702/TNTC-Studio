import json
import math
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
import threading
from typing import Any, Iterable
from uuid import uuid4

from loguru import logger

from app.models import const


def get_response(status: int, data: Any = None, message: str = ""):
    obj = {
        "status": status,
    }
    if data:
        obj["data"] = data
    if message:
        obj["message"] = message
    return obj


def to_json(obj):
    try:
        # Define a helper function to handle different types of objects
        def serialize(o):
            # If the object is a serializable type, return it directly
            if isinstance(o, (int, float, bool, str)) or o is None:
                return o
            # If the object is binary data, convert it to a base64-encoded string
            elif isinstance(o, bytes):
                return "*** binary data ***"
            # If the object is a dictionary, recursively process each key-value pair
            elif isinstance(o, dict):
                return {k: serialize(v) for k, v in o.items()}
            # If the object is a list or tuple, recursively process each element
            elif isinstance(o, (list, tuple)):
                return [serialize(item) for item in o]
            # If the object is a custom type, attempt to return its __dict__ attribute
            elif hasattr(o, "__dict__"):
                return serialize(o.__dict__)
            # Return None for other cases (or choose to raise an exception)
            else:
                return None

        # Use the serialize function to process the input object
        serialized_obj = serialize(obj)

        # Serialize the processed object into a JSON string
        return json.dumps(serialized_obj, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"failed to serialize object to json: {str(e)}")
        return None


def get_uuid(remove_hyphen: bool = False):
    u = str(uuid4())
    if remove_hyphen:
        u = u.replace("-", "")
    return u


_CLIP_SPEED_MIN = 0.5
_CLIP_SPEED_MAX = 2.0


def normalize_clip_speed(value, default: float = 1.0) -> float:
    """Chuẩn hóa tốc độ phát của đoạn clip về phạm vi an toàn được WebUI hỗ trợ."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default

    # NaN sẽ bỏ qua việc so sánh độ lớn thông thường và lan truyền khi MoviePy tính toán duration; giá trị vô cực cũng
    # không phải đầu vào hợp lệ của người dùng. Cả hai đều được fallback về giá trị mặc định, đảm bảo API và lệnh gọi trực tiếp nội bộ đều không sinh ra
    # timeline không hợp lệ. Giá trị 0 và giá trị âm cũng không thể đại diện cho tốc độ phát bình thường.
    if not math.isfinite(speed) or speed <= 0:
        return default

    return min(max(speed, _CLIP_SPEED_MIN), _CLIP_SPEED_MAX)


def root_dir():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def storage_dir(sub_dir: str = "", create: bool = False):
    d = os.path.join(root_dir(), "storage")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if create and not os.path.exists(d):
        os.makedirs(d)

    return d


def resource_dir(sub_dir: str = ""):
    d = os.path.join(root_dir(), "resource")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    return d


def task_dir(sub_dir: str = ""):
    d = os.path.join(storage_dir(), "tasks")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def font_dir(sub_dir: str = ""):
    d = resource_dir("fonts")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def song_dir(sub_dir: str = ""):
    d = resource_dir("songs")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def public_dir(sub_dir: str = ""):
    d = resource_dir("public")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if not os.path.exists(d):
        os.makedirs(d)
    return d


def get_ffmpeg_binary() -> str:
    """
    Phân giải tệp thực thi FFmpeg mà tiến trình hiện tại nên sử dụng.

    Lý do bổ sung:
    1. Mã hóa video, tạo âm thanh im lặng, chuyển mã âm thanh pydub đều phụ thuộc vào FFmpeg;
    2. Bản portable trên Windows, Docker và thư mục cài đặt tùy chỉnh của người dùng thường xuất hiện PATH không nhất quán;
    3. Phân giải tập trung cho phép tất cả các bên gọi sử dụng cùng một bộ ưu tiên, giảm thiểu sự cố thực tế khi một luồng chạy được
       nhưng luồng khác lại không tìm thấy FFmpeg.

    Độ ưu tiên:
    1. IMAGEIO_FFMPEG_EXE: Cấu hình rõ ràng theo quy ước của MoviePy/imageio;
    2. ffmpeg trong PATH của hệ thống;
    3. Binary tích hợp sẵn do dependency imageio-ffmpeg cung cấp;
    4. Chuỗi "ffmpeg" làm fallback dự phòng, giao cho subprocess bộc lộ lỗi cụ thể hơn lúc chạy.
    """
    configured_ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured_ffmpeg:
        return configured_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled_ffmpeg:
            return bundled_ffmpeg
    except Exception as exc:
        logger.warning(f"failed to resolve bundled ffmpeg binary: {str(exc)}")

    return "ffmpeg"


_FFMPEG_INSTALL_HINT = (
    "Install FFmpeg on your system, or set app.ffmpeg_path in config.toml to "
    "the full path of an ffmpeg executable (e.g. downloaded from "
    "https://www.gyan.dev/ffmpeg/builds/)."
)


def check_ffmpeg_ready(timeout: int = 10) -> bool:
    """
    Thực hiện kiểm tra thăm dò trước xem FFmpeg có khả dụng không trước khi thực sự bắt đầu tạo video.

    Lý do bổ sung:
    Trước đây việc FFmpeg bị thiếu/không khả dụng chỉ xuất hiện trong các khâu ghép video, tạo track âm thanh im lặng,... dưới dạng
    ``RuntimeError: No ffmpeg exe could be found`` hoặc lỗi subprocess, người dùng thường phải đợi tác vụ chạy được phần lớn thời gian mới lần đầu thấy lỗi này,
    và bản thân lỗi cũng không chỉ ra bất kỳ cách giải quyết nào. Tại đây trong pipeline tác vụ dùng chung (``_run_pipeline`` của
    app/services/task.py) thăm dò trước một lần, đưa ra gợi ý sớm có thể thao tác (nhất quán với thói quen dùng từ của các logger.warning khác trong dự án).
    API, CLI, WebUI đều đi qua pipeline này nên cả ba nhánh đều có hiệu lực thống nhất.

    Chỉ thực hiện một lệnh gọi nhẹ ``-version``, không kích hoạt tải xuống hay thay đổi quy trình chính;
    Bên gọi cần coi giá trị trả về là điều kiện tiên quyết bắt buộc — imageio-ffmpeg==0.6.0 bị khóa của dự án
    sẽ không tự động tải bù một binary khả dụng khi thực sự dùng, do đó việc phát hiện thất bại bắt buộc phải làm cho
    giai đoạn cần FFmpeg dừng lại ngay lập tức, thay vì tiếp tục chạy đến bước ghép video mới báo lỗi.
    """
    ffmpeg_bin = get_ffmpeg_binary()
    try:
        completed = subprocess.run(
            [ffmpeg_bin, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning(
            f"no usable ffmpeg executable found (tried: {ffmpeg_bin}). "
            f"{_FFMPEG_INSTALL_HINT}"
        )
        return False
    except Exception as exc:
        logger.warning(
            f"failed to probe ffmpeg ({ffmpeg_bin}): {exc}. {_FFMPEG_INSTALL_HINT}"
        )
        return False

    if completed.returncode != 0:
        logger.warning(
            f"ffmpeg ({ffmpeg_bin}) probe exited with status {completed.returncode}; "
            f"video generation may fail later. {_FFMPEG_INSTALL_HINT}"
        )
        return False

    logger.info(f"ffmpeg check passed, using: {ffmpeg_bin}")
    return True


def run_in_background(func, *args, **kwargs):
    def run():
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"run_in_background error: {e}", exc_info=True)

    thread = threading.Thread(target=run, daemon=False)
    thread.start()
    return thread


def time_convert_seconds_to_hmsm(seconds) -> str:
    hours = int(seconds // 3600)
    seconds = seconds % 3600
    minutes = int(seconds // 60)
    milliseconds = int(seconds * 1000) % 1000
    seconds = int(seconds % 60)
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, seconds, milliseconds)


def text_to_srt(idx: int, msg: str, start_time: float, end_time: float) -> str:
    start_time = time_convert_seconds_to_hmsm(start_time)
    end_time = time_convert_seconds_to_hmsm(end_time)
    srt = """%d
%s --> %s
%s
        """ % (
        idx,
        start_time,
        end_time,
        msg,
    )
    return srt


def str_contains_punctuation(word):
    for p in const.PUNCTUATIONS:
        if p in word:
            return True
    return False


def split_string_by_punctuations(s):
    result = []
    txt = ""

    previous_char = ""
    next_char = ""
    for i in range(len(s)):
        char = s[i]
        if char == "\n":
            result.append(txt.strip())
            txt = ""
            continue

        if i > 0:
            previous_char = s[i - 1]
        if i < len(s) - 1:
            next_char = s[i + 1]

        if char == "." and previous_char.isdigit() and next_char.isdigit():
            # # In the case of "withdraw 10,000, charged at 2.5% fee", the dot in "2.5" should not be treated as a line break marker
            txt += char
            continue

        if char == "," and previous_char.isdigit() and next_char.isdigit():
            # Dấu phẩy ngăn cách hàng nghìn trong số tiếng Anh không phải dấu ngắt câu, ví dụ "1,000 years".
            # Boundary từ của Edge TTS thường trả về các con số dạng này dưới dạng nội dung liên tục;
            # Nếu tại đây tách thành "1" và "000 years", việc tổng hợp phụ đề sau đó sẽ không khớp được kịch bản gốc,
            # dẫn đến việc fallback sai lầm sang Whisper.
            txt += char
            continue

        if char not in const.PUNCTUATIONS:
            txt += char
        else:
            result.append(txt.strip())
            txt = ""
    result.append(txt.strip())
    # filter empty string
    result = list(filter(None, result))
    return result


PAUSE_TAG_KEYWORDS = (
    r"pause|pausa|silence|silencio|silêncio|silenzio|stille|"
    r"пауза|тишина|停顿|暂停|静音|ポーズ|一時停止|無音|일시중지|정지"
)
# 匹配所有包含停顿关键词的标签（方括号或圆括号），无论其参数合法与否均匹配，
# 以确保非法标签（如 [pause: -2s]、[pause: nope]、[pause: 0s]）在合成前被彻底清除而不会泄漏给 TTS
PAUSE_TAG_PATTERN = re.compile(
    rf"[\[\(]\s*(?:{PAUSE_TAG_KEYWORDS})\b(?:\s*[:：]?\s*([^\]\)]*?))?\s*[\]\)]",
    re.IGNORECASE,
)

# 停顿时长安全阈值（单位：秒）：
# 最小有效停顿为 0.1 秒（100ms），小于此值的请求会被校验并修正为 0.1s；
# 小于等于 0 秒或非法非数字的停顿标签会被判定为无效标签并直接移除，不朗读也不生成静音；
# 最大停顿上限为 10.0 秒，超过部分会被安全截断。
MIN_PAUSE_DURATION_SECONDS = 0.1
MAX_PAUSE_DURATION_SECONDS = 10.0


def has_pause_tags(text: str) -> bool:
    """Kiểm tra xem văn bản có chứa thẻ tạm dừng/pause hay không."""
    if not text:
        return False
    return bool(PAUSE_TAG_PATTERN.search(text))


def remove_pause_tags(text: str) -> str:
    """
    Loại bỏ tất cả các thẻ tạm dừng/pause trong văn bản kịch bản (bao gồm cả thẻ hợp lệ và không hợp lệ).

    Khi tách câu phụ đề, trích xuất từ khóa LLM hoặc truyền văn bản phát âm cho TTS, bắt buộc phải xóa các dấu hiệu không phát âm này,
    tránh để các thẻ không hợp lệ hoặc chưa xử lý bị đọc to hoặc dùng làm từ khóa tìm kiếm hình ảnh.
    """
    if not text:
        return ""
    cleaned = PAUSE_TAG_PATTERN.sub(" ", text)
    # Hợp nhất các khoảng trắng ngang liên tiếp, giữ lại dấu xuống dòng
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def parse_script_with_pauses(text: str) -> list[tuple[str, Any]]:
    """
    Phân tích cú pháp văn bản và các thẻ tạm dừng trong kịch bản.

    Các thẻ tạm dừng liên tiếp sẽ tự động được hợp nhất thành một đoạn tạm dừng;
    Các thẻ không hợp lệ (như tham số không phải số, thời lượng nhỏ hơn hoặc bằng 0) sẽ bị loại bỏ trực tiếp và bỏ qua, tuyệt đối không truyền làm lời thoại cho TTS;
    Tạm dừng quá nhỏ dưới MIN_PAUSE_DURATION_SECONDS (0.1s/100ms) sẽ được kiểm tra và nâng lên 0.1s;
    Tạm dừng quá dài vượt quá MAX_PAUSE_DURATION_SECONDS (10.0s) sẽ được giới hạn trong ngưỡng an toàn tối đa.

    Returns:
        Danh sách tuple có thứ tự, có dạng [("speech", "kịch bản"), ("pause", 2.0), ...]
    """
    if not text:
        return []

    segments: list[tuple[str, Any]] = []
    last_idx = 0

    for match in PAUSE_TAG_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_idx:
            speech_text = text[last_idx:start].strip()
            if speech_text:
                segments.append(("speech", speech_text))

        raw_arg = match.group(1)
        if raw_arg is None or not raw_arg.strip():
            # Khi không chỉ định tham số, mặc định tạm dừng 1.0 giây
            duration = 1.0
        else:
            raw_arg_str = raw_arg.strip()
            num_match = re.match(
                r"^([+-]?\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|ms|msec|msecs|秒|毫秒)?$",
                raw_arg_str,
                re.IGNORECASE,
            )
            if not num_match:
                logger.warning(
                    f"invalid pause tag value '{raw_arg_str}', tag removed and ignored"
                )
                last_idx = end
                continue

            val = float(num_match.group(1))
            unit = (num_match.group(2) or "s").lower()
            duration = val / 1000.0 if ("ms" in unit or "毫秒" in unit) else val

        if duration <= 0:
            logger.warning(
                f"invalid non-positive pause duration {duration}s, tag removed and ignored"
            )
            last_idx = end
            continue

        if duration < MIN_PAUSE_DURATION_SECONDS:
            logger.warning(
                f"pause duration {duration:.3f}s is below minimum limit of {MIN_PAUSE_DURATION_SECONDS}s (100ms), "
                f"clamped to {MIN_PAUSE_DURATION_SECONDS}s"
            )
            duration = MIN_PAUSE_DURATION_SECONDS

        if duration > MAX_PAUSE_DURATION_SECONDS:
            logger.warning(
                f"pause duration {duration}s exceeds maximum limit of {MAX_PAUSE_DURATION_SECONDS}s, "
                f"clamped to {MAX_PAUSE_DURATION_SECONDS}s"
            )
            duration = MAX_PAUSE_DURATION_SECONDS

        # Hợp nhất các thẻ tạm dừng xuất hiện liên tiếp thành một đoạn tạm dừng, tránh tạo ra các tệp im lặng phân mảnh
        if segments and segments[-1][0] == "pause":
            merged_duration = min(
                segments[-1][1] + duration, MAX_PAUSE_DURATION_SECONDS
            )
            segments[-1] = ("pause", merged_duration)
        else:
            segments.append(("pause", duration))

        last_idx = end

    if last_idx < len(text):
        speech_text = text[last_idx:].strip()
        if speech_text:
            segments.append(("speech", speech_text))

    return segments


def normalize_script_for_subtitle_matching(video_script: str) -> str:
    """
    Làm sạch văn bản kịch bản trước khi so khớp phụ đề.

    Người dùng có thể nhập thủ công các dấu phân cách Markdown, nhấn mạnh tiêu đề hoặc các ký hiệu định dạng như `_`.
    Các ký tự này thường không xuất hiện trong kết quả nhận diện của TTS/Whisper; nếu tiếp tục tham gia
    so khớp từng dòng phụ đề, số dòng kịch bản sẽ nhiều hơn số dòng phụ đề thực tế, cuối cùng có thể bù ra
    `00:00:00,000 --> 00:00:00,000`, khiến phần mềm dựng phim không thể import được file SRT.
    """
    video_script = remove_pause_tags(video_script or "")
    underscore_count = video_script.count("_")
    video_script = video_script.replace("_", "")
    cleaned_lines = []
    removed_separator_lines = 0
    for line in video_script.splitlines():
        line = line.strip()
        # Dấu phân cách Markdown hoặc ký hiệu nhấn mạnh khi đứng riêng một dòng sẽ không được TTS đọc, bắt buộc phải xóa
        # khỏi dòng kịch bản, tránh việc tổng hợp phụ đề bị kẹt ở những dòng mục tiêu "không thể phát âm" này.
        if re.fullmatch(r"[-*_]{3,}", line):
            removed_separator_lines += 1
            continue
        cleaned_lines.append(line)

    normalized_script = "\n".join(cleaned_lines).strip()
    if underscore_count or removed_separator_lines:
        logger.debug(
            "normalized script for subtitle matching, "
            f"removed underscores: {underscore_count}, "
            f"removed markdown separator lines: {removed_separator_lines}"
        )
    return normalized_script


def md5(text):
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def resolve_ui_language(
    saved_language: str | None,
    browser_locale: str | None,
    supported_languages: Iterable[str],
    default_language: str = "en",
) -> str:
    """
    Chọn ngôn ngữ giao diện theo thứ tự ưu tiên "Cài đặt đã lưu, ngôn ngữ trình duyệt, ngôn ngữ mặc định".

    Trình duyệt thường trả về locale có mã vùng, ví dụ ``zh-CN``, ``pt-BR``. Tệp ngôn ngữ sử dụng
    các mã cơ bản như ``zh``, ``pt``, do đó trước tiên thử so khớp hoàn toàn, sau đó mới fallback về mã ngôn ngữ
    trước dấu gạch nối. Hàm duy trì logic thuần túy, tránh ghép ngữ cảnh trình duyệt và ghi cấu hình vào tầng tiện ích, thuận tiện cho việc kiểm thử.
    """
    supported = [str(language).strip() for language in supported_languages]
    supported_by_lower = {
        language.lower(): language for language in supported if language
    }

    def match_language(value: str | None) -> str | None:
        normalized = str(value or "").strip().replace("_", "-").lower()
        if not normalized:
            return None
        if normalized in supported_by_lower:
            return supported_by_lower[normalized]
        base_language = normalized.split("-", 1)[0]
        return supported_by_lower.get(base_language)

    saved_match = match_language(saved_language)
    if saved_match:
        return saved_match

    browser_match = match_language(browser_locale)
    if browser_match:
        return browser_match

    default_match = match_language(default_language)
    if default_match:
        return default_match

    # Dự án bình thường luôn chứa tiếng Anh; giữ lại tập hợp ngôn ngữ rỗng làm dự phòng fallback, tránh việc thư mục ngôn ngữ bị hỏng khiến việc khởi tạo
    # trang ném ngoại lệ trực tiếp, hàm dịch sau đó sẽ tiếp tục hiển thị key ban đầu để tiện chẩn đoán.
    return supported[0] if supported else default_language


@lru_cache(maxsize=8)
def load_locales(i18n_dir):
    # Mỗi tương tác trên WebUI đều kích hoạt Streamlit thực thi lại script, tệp ngôn ngữ không thay đổi trong lúc chạy,
    # do đó lưu cache kết quả phân tích cú pháp, tránh việc đọc và phân tích lặp đi lặp lại tất cả các tệp i18n JSON.
    _locales = {}
    for root, dirs, files in os.walk(i18n_dir):
        for file in files:
            if file.endswith(".json"):
                lang = file.split(".")[0]
                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                    _locales[lang] = json.loads(f.read())
    return _locales


def parse_extension(filename):
    return Path(filename).suffix.lower().lstrip('.')
