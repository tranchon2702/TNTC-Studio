import itertools
import io
import math
import os
import random
import gc
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import ExitStack, redirect_stdout
from functools import lru_cache
from typing import List
from loguru import logger
import numpy as np
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageDraw, ImageFont

from app.config import config
from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services.utils import video_effects
from app.utils import file_security, utils

class SubClippedVideoClip:
    def __init__(
        self,
        file_path,
        start_time=None,
        end_time=None,
        width=None,
        height=None,
        duration=None,
        source_file_path=None,
    ):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        self.source_file_path = source_file_path or file_path
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# Sự kết hợp ffmpeg/AAC trong Docker dễ bị biến động về chất lượng âm thanh hơn trong cấu hình mặc định.
# Ở đây, tốc độ bit âm thanh được tăng lên rõ ràng để tránh hiện tượng méo tiếng rõ ràng do giá trị mặc định quá thấp trong giai đoạn sản xuất.
audio_bitrate = "192k"
fps = 30
# Khi FFmpeg ghép/chuyển mã ở tốc độ khung hình, thời lượng cuối cùng có thể ngắn hơn hàng chục mili giây so với thời lượng lý thuyết mà MoviePy đọc.
# Ở đây, một giới hạn an toàn nhỏ được dành cho nội dung video để tránh màn hình đen hoặc màn hình đen ở cuối âm thanh do làm tròn khung hình.
# Nói lắp hoặc không có hình ảnh ở đoạn tường thuật ngắn cuối cùng.
_VIDEO_DURATION_SAFETY_MARGIN = 0.1
_MIN_MATERIAL_DIMENSION = 480
# Ứng dụng nhắn tin và một số bộ mã hóa sẽ làm tròn kích thước màn hình. Ví dụ: WhatsApp sẽ làm tròn số 9:16
# Vật liệu được nén thành 478x850, tức là nhỏ hơn 2 pixel so với 480. Nhấn trực tiếp vào thẻ cứng 480 sẽ khiến tất cả các vật liệu đó bị biến dạng.
# Bị loại bỏ và cuối cùng thất bại vì "không tìm thấy tài liệu hợp lệ". Để lại một chút khoan dung ở đây,
# Nó không chỉ có thể vượt qua vật liệu thấp hơn ngưỡng một chút do làm tròn mà còn chặn vật liệu thực sự có độ phân giải thấp.
_MIN_DIMENSION_TOLERANCE = 10
_DEFAULT_VIDEO_CODEC = "libx264"
_SUBTITLE_SPRING_DURATION_SECONDS = 0.18
_MIN_SUBTITLE_SPRING_SCALE = 0.05
_MAX_SUBTITLE_SPRING_SCALE = 1.35
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_runtime_disabled_video_codecs = set()


def _get_subtitle_spring_scale(time_seconds: float, duration_seconds: float) -> float:
    """Trả về tỷ lệ thu phóng được sử dụng bởi hoạt ảnh trả lại phụ đề tại thời điểm đã chỉ định."""
    if duration_seconds <= 0 or time_seconds >= duration_seconds:
        return 1.0

    progress = max(0.0, min(time_seconds / duration_seconds, 1.0))
    scale = 1.0 - math.exp(-6.0 * progress) * math.cos(2.5 * math.pi * progress)
    return max(
        _MIN_SUBTITLE_SPRING_SCALE,
        min(scale, _MAX_SUBTITLE_SPRING_SCALE),
    )


def _scale_subtitle_frame_on_canvas(frame: np.ndarray, scale: float) -> np.ndarray:
    """
    Chia tỷ lệ khung tiêu đề hoặc mặt nạ trong suốt xung quanh trung tâm trong khi vẫn duy trì cùng kích thước canvas.

    MoviePy lưu riêng khung màu phụ đề và mặt nạ trong suốt. Hoạt ảnh thoát phải được sử dụng hoàn toàn cho cả hai
    Tỷ lệ và cắt xén tương tự, nếu không, khung hình đầu tiên của hoạt ảnh sẽ coi vùng trong suốt là đường viền văn bản màu đen và tổng hợp nó thành video
    thượng đẳng. Mảng hai chiều biểu thị mặt nạ có giá trị từ 0 đến 1 và mảng ba chiều biểu thị khung màu RGB/RGBA.
    """
    if frame.ndim not in (2, 3):
        raise ValueError("subtitle frame must be a 2D mask or 3D color frame")

    height, width = frame.shape[:2]
    scaled_width = max(1, int(round(width * scale)))
    scaled_height = max(1, int(round(height * scale)))
    offset = ((width - scaled_width) // 2, (height - scaled_height) // 2)

    if frame.ndim == 2:
        # Mặt nạ MoviePy sử dụng số dấu phẩy động từ 0 đến 1 và chế độ L của Pillow sử dụng 0 đến 255; sau khi chuyển đổi
        # Sau đó khôi phục loại và phạm vi ban đầu để đảm bảo rằng ngữ nghĩa minh bạch của CompositeVideoClip không thay đổi.
        mask_image = Image.fromarray(
            np.clip(frame * 255.0, 0, 255).astype(np.uint8)
        )
        resized_mask = mask_image.resize(
            (scaled_width, scaled_height),
            Image.Resampling.BILINEAR,
        )
        mask_canvas = Image.new("L", (width, height), 0)
        mask_canvas.paste(resized_mask, offset)
        return (np.asarray(mask_canvas) / 255.0).astype(frame.dtype, copy=False)

    if frame.shape[2] not in (3, 4):
        raise ValueError("subtitle color frame must use RGB or RGBA channels")
    color_image = Image.fromarray(frame)
    resized_color = color_image.resize(
        (scaled_width, scaled_height),
        Image.Resampling.BILINEAR,
    )
    background = (0, 0, 0, 0) if frame.shape[2] == 4 else (0, 0, 0)
    color_canvas = Image.new(color_image.mode, (width, height), background)
    color_canvas.paste(resized_color, offset)
    return np.asarray(color_canvas).astype(frame.dtype, copy=False)


def _apply_subtitle_spring_animation(clip, subtitle_duration: float):
    """Chia tỷ lệ khung màu phụ đề và mặt nạ cùng lúc để tránh khung hình đầu tiên màu đen của hiệu ứng nảy."""
    animation_duration = min(
        _SUBTITLE_SPRING_DURATION_SECONDS,
        max(0.0, subtitle_duration),
    )
    if animation_duration <= 0:
        return clip

    def transform_frame(get_frame, time_seconds):
        frame = get_frame(time_seconds)
        scale = _get_subtitle_spring_scale(time_seconds, animation_duration)
        if scale == 1.0:
            return frame
        return _scale_subtitle_frame_on_canvas(frame, scale)

    # apply_to=["mask"] là chìa khóa để khắc phục: MoviePy chỉ xử lý các khung màu theo mặc định, do đó cách triển khai cũ
    # Giữ lại kích thước ban đầu của mặt nạ trong thời gian ngắn khi mỗi phụ đề xuất hiện và hiển thị đường viền văn bản màu đen.
    return clip.transform(transform_frame, apply_to=["mask"])


def _get_required_video_duration(audio_duration: float) -> float:
    """
    Trả về thời lượng mục tiêu của việc ghép tài liệu video.

    Tình huống sử dụng: Khi tổng hợp video, thời lượng của tài liệu cần bao phủ âm thanh tường thuật. Cứ làm "vừa bằng"
    Khi nói đến thời lượng âm thanh, FFmpeg có thể làm cho video cuối cùng ngắn hơn một chút do làm tròn tốc độ khung hình, do đó, nó bổ sung thêm một thời lượng đồng đều.
    Biên độ nhẹ. Chức năng này độc lập, tạo điều kiện thuận lợi cho việc thử nghiệm và điều chỉnh kích thước lề sau đó dựa trên phản hồi thực tế.
    """
    return max(0.0, float(audio_duration) + _VIDEO_DURATION_SAFETY_MARGIN)


def is_material_resolution_acceptable(width: int, height: int) -> bool:
    """
    Xác định xem độ phân giải vật liệu có đủ để tổng hợp hay không.

    Kích thước tối thiểu danh nghĩa là 480x480, nhưng cho phép `_MIN_DIMENSION_TOLERANCE` pixel bên dưới,
    Kích thước được làm tròn xuống theo bộ mã hóa/ứng dụng nhắn tin tương thích (ví dụ: 478x850 cho WhatsApp).
    """
    min_dimension = _MIN_MATERIAL_DIMENSION - _MIN_DIMENSION_TOLERANCE
    return width >= min_dimension and height >= min_dimension


def _prioritize_unique_source_clips(
    subclipped_items: List[SubClippedVideoClip],
    concat_mode: VideoConcatMode,
    source_usage: dict[str, int] | None = None,
    source_groups: dict[str, str] | None = None,
) -> List[SubClippedVideoClip]:
    """
    Ưu tiên mỗi nguồn nguyên liệu chỉ xuất hiện một lần để giảm khả năng nguyên liệu giống nhau xuất hiện lặp đi lặp lại trong phim thành phẩm.

    Các tài liệu trên mạng thường gặp phải tình trạng “một video dài bị cắt thành nhiều đoạn ngắn”. Logic cũ là
    Ở chế độ ngẫu nhiên, tất cả các clip ngắn được xáo trộn trực tiếp, tạo ra nhiều lát của cùng một video nguồn.
    Được phân phối ở phần đầu và phần giữa, người dùng sẽ cảm nhận được nội dung được lặp lại. Chức năng này chỉ điều chỉnh thứ tự đoạn:
    Trước tiên hãy phát clip dài nhất trong mỗi tệp nguồn và sử dụng các clip còn lại làm bản sao lưu; khi tổng thời lượng của tài liệu không đủ,
    Các phân đoạn tiếp theo vẫn được phép hoàn thành độ dài âm thanh để tránh làm ảnh hưởng đến tỷ lệ tạo video thành công. Ưu tiên lâu nhất
    Mục đích của việc cắt là tránh việc chọn ngẫu nhiên các đoạn clip ngắn rời rạc ở cuối video dẫn đến việc sử dụng lại sớm dù đã có đủ tư liệu.
    """
    if not subclipped_items:
        return []

    concat_mode_value = getattr(concat_mode, "value", concat_mode)
    if concat_mode_value != VideoConcatMode.random.value:
        if source_usage is None:
            return subclipped_items
        if not source_groups:
            return sorted(
                subclipped_items,
                key=lambda item: source_usage.get(item.source_file_path, 0),
            )
        # Keep keyword rounds in order while rotating candidates within each keyword.
        groups = {}
        for item in subclipped_items:
            key = source_groups.get(item.source_file_path, item.source_file_path)
            groups.setdefault(key, []).append(item)
        for items in groups.values():
            items.sort(key=lambda item: source_usage.get(item.source_file_path, 0))
        return [
            item
            for row in itertools.zip_longest(*groups.values())
            for item in row
            if item is not None
        ]

    grouped_items: dict[str, list[SubClippedVideoClip]] = {}
    for item in subclipped_items:
        grouped_items.setdefault(item.source_file_path, []).append(item)

    primary_items = []
    overflow_items = []
    for items in grouped_items.values():
        primary_item = max(items, key=lambda item: item.duration)
        primary_items.append(primary_item)
        overflow_items.extend(item for item in items if item is not primary_item)

    random.shuffle(primary_items)
    random.shuffle(overflow_items)
    if source_usage is not None:
        # Stable sorting retains randomness among equally used sources.
        primary_items.sort(key=lambda item: source_usage.get(item.source_file_path, 0))
        overflow_items.sort(key=lambda item: source_usage.get(item.source_file_path, 0))
    logger.info(
        "prioritized unique video materials, "
        f"sources: {len(grouped_items)}, "
        f"primary clips: {len(primary_items)}, "
        f"fallback clips: {len(overflow_items)}"
    )
    return primary_items + overflow_items


def get_ffmpeg_binary():
    """
    Tương thích với những người gọi trước đây đọc đường dẫn FFmpeg trực tiếp từ dịch vụ video.

    Logic phân tích cú pháp thực sự đã được trích xuất thành `app.utils.utils.get_ffmpeg_binary()`, video, voice
    Tập hợp ưu tiên tương tự nên được sử dụng lại với các liên kết mới tiếp theo; bao bì mỏng được giữ lại ở đây để tránh các chữ viết bên ngoài hoặc
    Các thử nghiệm cũ đã đưa ra AttributionError khi nhập trực tiếp `app.services.video.get_ffmpeg_binary`.
    """
    return utils.get_ffmpeg_binary()


def _get_configured_video_codec() -> str:
    """
    Đọc bộ mã hóa video do người dùng định cấu hình.

    Cấu hình này dành cho người dùng nâng cao đang cố gắng kích hoạt phần cứng như NVENC/AMF/QSV/VideoToolbox
    mã hóa. Ở đây chỉ có một danh sách trắng cố định được cố tình cho phép để tránh người dùng điền lỗi sau khi mở bất kỳ tham số FFmpeg nào.
    Các tham số khiến định dạng đầu ra không thể kiểm soát được và thậm chí khiến tác vụ tạo không thành công ở các giai đoạn tiếp theo.
    """
    configured_codec = str(
        config.app.get("video_codec", _DEFAULT_VIDEO_CODEC) or _DEFAULT_VIDEO_CODEC
    ).strip()
    if configured_codec not in _SUPPORTED_VIDEO_CODECS:
        logger.warning(
            f"unsupported video codec configured: {configured_codec}, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC
    return configured_codec


@lru_cache(maxsize=16)
def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:
    """
    Kiểm tra xem FFmpeg hiện tại có khai báo hỗ trợ cho bộ mã hóa được chỉ định hay không.

    Điều này chỉ có thể chứng minh rằng FFmpeg bao gồm bộ mã hóa này khi biên dịch chứ không thể chứng minh phần cứng và trình điều khiển máy hiện tại.
    Phải có sẵn. Do đó, nó vẫn sẽ quay trở lại libx264 khi mã hóa thực tế không thành công.
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {str(exc)}"
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {(result.stderr or result.stdout or '').strip()}"
        )
        return False
    return codec in result.stdout


def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    """
    Trả về bộ mã hóa video thực tế được sử dụng lần này.

    Khi người dùng chọn bộ mã hóa phần cứng, trước tiên hãy thực hiện phát hiện danh sách bộ mã hóa FFmpeg; nếu quá trình này đã
    Nếu mã hóa thực tế không thành công, nó sẽ được khôi phục trực tiếp để tránh lỗi lặp lại cho mọi phân đoạn trong một tác vụ.
    """
    selected_codec = preferred_codec or _get_configured_video_codec()
    if selected_codec == _DEFAULT_VIDEO_CODEC:
        return _DEFAULT_VIDEO_CODEC

    if selected_codec in _runtime_disabled_video_codecs:
        logger.warning(
            f"video codec {selected_codec} was disabled after a runtime failure, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, selected_codec):
        logger.warning(
            f"ffmpeg encoder {selected_codec} is not available, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    return selected_codec


def _disable_runtime_video_codec(codec: str, reason: str):
    if codec == _DEFAULT_VIDEO_CODEC:
        return
    _runtime_disabled_video_codecs.add(codec)
    logger.warning(
        f"video codec {codec} failed, fallback to {_DEFAULT_VIDEO_CODEC}. "
        f"reason: {reason}"
    )


def _get_temp_audio_dir(output_dir: str) -> str:
    """
    Return the directory to use for MoviePy's temporary audio file.

    On Windows, Windows Defender can lock files written to the task output
    directory while scanning them, causing MoviePy to fail with a
    PermissionError (WinError 32) on the TEMP_MPY_wvf_snd temp file and
    leaving the final MP4 at 0 bytes.  Using the system temp directory
    sidesteps the scan without changing behaviour on other platforms.

    On Linux/macOS/Docker the output directory is returned unchanged so
    existing behaviour is preserved.
    """
    if sys.platform == "win32":
        return tempfile.gettempdir()
    return output_dir


def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):
    """
    Sau khi mã hóa phần cứng không thành công, hãy thử lại với libx264. Bộ mã hóa phần cứng sẽ chỉ bị vô hiệu hóa nếu thử lại thành công.

    Nguyên nhân khiến FFmpeg bị lỗi trên Windows phức tạp hơn: có thể do card đồ họa/driver không hỗ trợ hoặc có thể do đầu ra
    Các vấn đề chung về IO như chiếm giữ tệp, quyền truy cập thư mục và chặn phần mềm chống vi-rút. Khi chỉ libx264 có thể viết thành công,
    Chỉ khi đó, chúng tôi mới có thể xác định rằng lỗi ban đầu rất có thể đến từ chính bộ mã hóa phần cứng, để tránh vô tình làm hỏng các tác vụ tiếp theo.
    """
    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)
    _disable_runtime_video_codec(failed_codec, reason)
    return _DEFAULT_VIDEO_CODEC


def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):
    """
    Viết video bằng bộ mã hóa được chỉ định và tự động thử lại bằng libx264 nếu không thành công.

    Việc có sẵn bộ mã hóa phần cứng không chỉ phụ thuộc vào FFmpeg mà còn phụ thuộc vào card đồ họa, trình điều khiển và môi trường chạy hiện tại.
    Toàn bộ tác vụ tạo không thể thất bại vì bộ mã hóa nâng cao không khả dụng, do đó, dự phòng được xử lý tập trung ở đây.
    """
    effective_codec = _get_effective_video_codec(codec)
    try:
        clip.write_videofile(output_file, codec=effective_codec, **kwargs)
        return effective_codec
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        return _fallback_write_videofile(
            clip,
            output_file,
            failed_codec=effective_codec,
            reason=str(exc),
            **kwargs,
        )


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer sử dụng dấu ngoặc đơn để bao bọc đường dẫn và trước tiên, các dấu ngoặc đơn trong đường dẫn cần phải được thoát.
    return file_path.replace("'", "'\\''")


def _format_ffmpeg_concat_path(file_path: str) -> str:
    """
    Tạo đường dẫn trong danh sách tệp giải mã concat.

    Tài liệu chính thức của FFmpeg yêu cầu các ký tự đặc biệt và khoảng trắng trong danh sách concat cần phải được thoát; cửa sổ
    Dấu gạch chéo ngược trong đường dẫn tuyệt đối cũng dễ dàng được phân tích cú pháp dưới dạng ký tự thoát. Ở đây nó được chuyển đổi thống nhất thành dạng dấu gạch chéo về phía trước.
    Đặt `C:\\Users\\...` trở thành `C:/Users/...`, rồi xử lý các dấu ngoặc đơn, tương thích với macOS/Linux.
    """
    absolute_path = os.path.abspath(file_path)
    return _escape_ffmpeg_concat_path(absolute_path.replace("\\", "/"))


def concat_video_clips_with_ffmpeg(
    clip_files: List[str],
    output_file: str,
    threads: int,
    output_dir: str,
    max_duration: float | None = None,
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            fp.write(f"file '{_format_ffmpeg_concat_path(clip_file)}'\n")

    def build_command(codec: str) -> list[str]:
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_file,
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
        ]
        if max_duration is not None and max_duration > 0:
            command.extend(["-t", f"{max_duration:.3f}"])
        command.append(output_file)
        return command

    def run_concat(codec: str):
        command = build_command(codec)
        # Sử dụng ffmpeg để nối và mã hóa chỉ một lần để tránh mã hóa lại nhiều lần khi hợp nhất phân đoạn MoviePy theo phân đoạn.
        # Điều này làm giảm nguy cơ suy giảm chất lượng hình ảnh và chuyển màu.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
        return codec

    try:
        effective_codec = _get_effective_video_codec()
        try:
            return run_concat(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    finally:
        delete_files(concat_list_file)


def _sanitize_image_file(image_path: str) -> str:
    # Mặc dù Pillow có thể mở một số hình ảnh cục bộ nhưng chúng sẽ bị hỏng do siêu dữ liệu EXIF/eXIf bị hỏng.
    # ImageClip ném ra một ngoại lệ trực tiếp trong giai đoạn phân tích cú pháp. Tại đây, hãy xuất lại "hình ảnh sạch" và loại bỏ siêu dữ liệu xấu.
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # Xuất sang PNG một cách thống nhất để tránh các đường dẫn siêu dữ liệu khác nhau của JPEG/PNG tiếp tục tạo ra các khối xấu.
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # Ưu tiên mở trực tiếp ảnh gốc; nếu thất bại do siêu dữ liệu bị hỏng, hãy thử tạo một bản sao không có siêu dữ liệu.
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    Mở tệp video một cách im lặng để ngăn MoviePy 2.1.x in trực tiếp thông tin thăm dò ffmpeg ra thiết bị xuất chuẩn.

    lý lịch:
    Phiên bản phụ thuộc hiện tại của `FFMPEG_VideoReader` chứa nội bộ `print(self.infos)` và
    `print(ffmpeg command)`, sẽ xuất ra khi đọc đoạn video ở giữa mà không có đoạn âm thanh
    `audio_found: Sai`. Đây chỉ là siêu dữ liệu của tài liệu đầu vào, không có nghĩa là phim cuối cùng sẽ không có âm thanh.
    Nhưng nó sẽ đánh lừa người dùng WebUI/cuối khi nghĩ rằng quá trình xây dựng không thành công.

    hoàn thành:
    1. Chỉ chuyển hướng thiết bị xuất chuẩn trong cửa sổ ngắn mở VideoFileClip;
    2. Mặc định `audio=False`, vì âm thanh gốc của tài liệu không cần phải được giữ nguyên trong giai đoạn tài liệu video của dự án.
       Âm thanh cuối cùng sẽ được gắn thống nhất trong giai đoạn `generate_video()`;
    3. Nếu thư viện phụ thuộc thực hiện xuất nội dung, hãy hạ cấp nội dung đó xuống nhật ký gỡ lỗi để hỗ trợ khắc phục sự cố nếu cần.
    """
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    # Khi lặp qua video, cùng một đường dẫn clip tạm thời xuất hiện nhiều lần trong danh sách mối nối FFmpeg.
    # Các bản sao phải được giữ lại trong quá trình nối, nhưng việc làm sạch chỉ có thể được xóa một lần; ở đây, các bản sao được loại bỏ theo thứ tự ban đầu, sao cho tất cả
    # Người gọi có hành vi bình thường và tránh liên tục xuất FileNotFoundError sau khi lần xóa đầu tiên thành công.
    unique_files = dict.fromkeys(file for file in files if file)
    for file in unique_files:
        try:
            os.remove(file)
        except FileNotFoundError:
            # Hành động dọn dẹp cho phép các tệp không còn tồn tại, chẳng hạn như đường dẫn bị lỗi FFmpeg hoặc việc dọn dẹp đồng thời có
            # Tái chế tập tin; đây không phải là vấn đề liên quan đến người dùng và sẽ không gây ô nhiễm nhật ký bản dựng.
            continue
        except OSError as e:
            # Các quyền, hệ thống tệp chỉ đọc hoặc ngoại lệ đĩa sẽ để lại các tệp tạm thời thực sự và tiếp tục cảnh báo
            # Thật thuận tiện để xác định các vấn đề môi trường dựa trên các đường dẫn cụ thể và lỗi hệ thống.
            logger.warning(f"failed to delete temporary file {file}: {str(e)}")


def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        try:
            resolved_bgm_file = bgm_service.resolve_bgm_file(bgm_file)
        except ValueError as exc:
            # Bgm_file trong yêu cầu API xuất phát từ đầu vào của người dùng và chỉ được phép phân tích cú pháp thành BGM của người dùng hoặc tích hợp sẵn
            # Thư mục bài hát, ngăn MoviePy đọc bất kỳ tệp máy chủ nào như cấu hình và khóa.
            logger.warning(
                f"reject unsafe bgm file: {bgm_file}, error: {str(exc)}"
            )
            return ""
        return resolved_bgm_file

    if bgm_type == "random":
        files = bgm_service.list_bgm_files()
        # Khi thư mục nhạc nền trống, nó sẽ trực tiếp chuyển về "không có BGM" để tránh Random.choice([]) đưa ra ngoại lệ.
        if not files:
            logger.warning("no background music files found")
            return ""
        return random.choice(files)

    return ""


def _fit_clip_to_canvas(
    clip,
    *,
    target_width: int,
    target_height: int,
    fit_mode: VideoFitMode | str = VideoFitMode.cover,
):
    """Resize a clip to an exact canvas using cover/crop or contain/letterbox."""
    source_width, source_height = (int(value) for value in clip.size)
    target_width = int(target_width)
    target_height = int(target_height)
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError(
            "video dimensions must be positive: "
            f"source={source_width}x{source_height}, "
            f"target={target_width}x{target_height}"
        )

    mode = VideoFitMode(fit_mode)
    if (source_width, source_height) == (target_width, target_height):
        return clip

    # Exact aspect-ratio matches do not need either a crop or a background.
    if source_width * target_height == source_height * target_width:
        return clip.resized(new_size=(target_width, target_height))

    width_scale = target_width / source_width
    height_scale = target_height / source_height

    if mode == VideoFitMode.cover:
        # ceil guarantees the resized clip covers the complete canvas despite
        # floating-point rounding. Any excess is removed symmetrically.
        scale_factor = max(width_scale, height_scale)
        resized_width = max(target_width, math.ceil(source_width * scale_factor))
        resized_height = max(target_height, math.ceil(source_height * scale_factor))
        resized_clip = clip.resized(new_size=(resized_width, resized_height))
        crop_x = max(0, (resized_width - target_width) // 2)
        crop_y = max(0, (resized_height - target_height) // 2)
        return resized_clip.cropped(
            x1=crop_x,
            y1=crop_y,
            width=target_width,
            height=target_height,
        )

    # contain preserves the legacy behavior: show the complete source frame,
    # centered over a black canvas when the aspect ratios differ.
    scale_factor = min(width_scale, height_scale)
    resized_width = max(1, min(target_width, int(source_width * scale_factor)))
    resized_height = max(1, min(target_height, int(source_height * scale_factor)))
    background = ColorClip(
        size=(target_width, target_height), color=(0, 0, 0)
    ).with_duration(clip.duration)
    resized_clip = clip.resized(
        new_size=(resized_width, resized_height)
    ).with_position("center")
    return CompositeVideoClip(
        [background, resized_clip], size=(target_width, target_height)
    ).with_duration(clip.duration)


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
    clip_speed: float = 1.0,
    video_fit_mode: VideoFitMode = VideoFitMode.cover,
    source_usage: dict[str, int] | None = None,
    source_groups: dict[str, str] | None = None,
    used_video_paths: List[str] | None = None,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    try:
        # Ở đây bạn chỉ cần đọc thời lượng của âm thanh tường thuật để xác định độ dài của đoạn ghép video tài liệu; nó sẽ không được sử dụng lại sau này.
        # âm thanh_clip. Đóng ngay sau khi đọc xong để tránh thoát sớm hoặc rò rỉ đường dẫn bất thường của các thẻ xử lý tệp.
        audio_duration = audio_clip.duration
    finally:
        close_clip(audio_clip)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")
    required_video_duration = _get_required_video_duration(audio_duration)
    logger.info(
        f"required video duration: {required_video_duration:.2f} seconds "
        f"(audio duration + {_VIDEO_DURATION_SAFETY_MARGIN:.2f}s safety margin)"
    )

    # Tương thích với trường hợp chế độ chuyển đổi không được chuyển khi gọi trực tiếp API, để tránh sự cố khi truy cập .value sau đó.
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    normalized_clip_speed = utils.normalize_clip_speed(clip_speed)
    if normalized_clip_speed != 1.0:
        # Chỉ ghi lại giá trị hiệu quả cuối cùng một lần sẽ thuận tiện cho việc xác định vấn đề chuẩn hóa các tham số ngoài giới hạn API.
        # Đồng thời tránh xuất ra nhiều lần các bản ghi giống nhau trong các đường dẫn nóng trên mỗi đoạn.
        logger.info(f"clip playback speed: {normalized_clip_speed:.2f}x")
    # max_clip_duration giới hạn thời gian phát lại cuối cùng trong phim hoàn chỉnh chứ không phải thời gian đọc video nguồn.
    # MoviePy phát 1,5 giây cảnh quay nguồn ở tốc độ 0,5 lần và sẽ nhận được clip 3 giây, phát ở tốc độ gấp đôi
    # Đoạn phim nguồn dài 6 giây cũng sẽ tạo ra clip dài 3 giây. Vì vậy, thời lượng nguồn phải được suy ra theo tốc độ trước khi cắt; nếu như
    # Nó vẫn đọc trong 3 giây trước khi chạy chậm lại và cắt xén, nhưng đoạn tiếp theo bắt đầu từ giây thứ 3 của video nguồn và bỏ qua phần giữa.
    # Đoạn phim dài 1,5 giây. Tính toán này cũng đảm bảo rằng các mốc thời gian nguồn ở các tốc độ khác nhau là liên tục và không chồng chéo.
    source_clip_duration = max_clip_duration * normalized_clip_speed
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    fit_mode = VideoFitMode(video_fit_mode)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = _open_video_clip_quietly(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        close_clip(clip)
        
        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + source_clip_duration, clip_duration)

            # Giữ tất cả các phân đoạn hợp lệ.
            # Điều này sẽ không làm mất nội dung "toàn bộ video ngắn hơn max_clip_duration".
            # Nó sẽ không nuốt chửng đoạn nội dung nhỏ còn sót lại ở cuối một video dài.
            if end_time > start_time:
                subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                        source_file_path=video_path,
                    )
                )

            start_time = end_time
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    subclipped_items = _prioritize_unique_source_clips(
        subclipped_items=subclipped_items,
        concat_mode=video_concat_mode,
        **({"source_usage": source_usage, "source_groups": source_groups}
           if source_usage is not None else {}),
    )
        
    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration >= required_video_duration:
            break
        
        logger.debug(
            f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, "
            f"source: {os.path.basename(subclipped_item.source_file_path)}, "
            f"current duration: {video_duration:.2f}s, "
            f"remaining: {required_video_duration - video_duration:.2f}s"
        )
        
        try:
            clip = _open_video_clip_quietly(subclipped_item.file_path).subclipped(
                subclipped_item.start_time, subclipped_item.end_time
            )
            # Tốc độ phát lại là một thuộc tính của chính vật liệu và phải được áp dụng trước khi chuyển đổi. Bằng cách này, Fade/Slide đợi một giây để chuyển đổi.
            # Nó sẽ không tuân theo tốc độ vật liệu đến 0,5 giây hoặc 2 giây; việc cắt xén thời lượng tối đa tiếp theo sẽ tiếp tục như
            # Giới hạn an toàn cho các lỗi dấu phẩy động hoặc thời lượng vật liệu bất thường, đảm bảo rằng clip cuối cùng không vượt quá giới hạn cấu hình.
            if normalized_clip_speed != 1.0:
                clip = clip.with_speed_scaled(normalized_clip_speed)
            # Normalize every source clip before transitions are applied. In cover mode
            # the clip fills the canvas and the excess edges are cropped; contain keeps
            # the complete source frame and uses black bars for the unused area.
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                clip_ratio = clip.w / clip.h
                video_ratio = video_width / video_height
                logger.debug(
                    "resizing clip, "
                    f"source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, "
                    f"target: {video_width}x{video_height}, ratio: {video_ratio:.2f}, "
                    f"fit_mode: {fit_mode.value}"
                )
                clip = _fit_clip_to_canvas(
                    clip,
                    target_width=video_width,
                    target_height=video_height,
                    fit_mode=fit_mode,
                )

            shuffle_side = random.choice(["left", "right", "top", "bottom"])
            if transition_value in (None, VideoTransitionMode.none.value):
                clip = clip
            elif transition_value == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, 1)
            elif transition_value == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, 1)
            elif transition_value == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, 1, shuffle_side)
            elif transition_value == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, 1, shuffle_side)
            elif transition_value == VideoTransitionMode.zoom_in.value:
                clip = video_effects.zoomin_transition(clip, 1)
            elif transition_value == VideoTransitionMode.zoom_out.value:
                clip = video_effects.zoomout_transition(clip, 1)
            elif transition_value == VideoTransitionMode.shuffle.value:
                transition_funcs = [
                    lambda c: video_effects.fadein_transition(c, 1),
                    lambda c: video_effects.fadeout_transition(c, 1),
                    lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                    lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                    lambda c: video_effects.zoomin_transition(c, 1),
                    lambda c: video_effects.zoomout_transition(c, 1),
                ]
                shuffle_transition = random.choice(transition_funcs)
                clip = shuffle_transition(clip)

            if clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)
                
            # wirte clip to temp file
            clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
            _write_videofile_with_codec_fallback(
                clip,
                clip_file,
                codec=_get_configured_video_codec(),
                logger=None,
                fps=fps,
            )

            # Store clip duration before closing
            clip_duration_saved = clip.duration
            close_clip(clip)

            processed_clips.append(
                SubClippedVideoClip(
                    file_path=clip_file,
                    duration=clip_duration_saved,
                    width=clip_w,
                    height=clip_h,
                    source_file_path=subclipped_item.source_file_path,
                )
            )
            video_duration += clip_duration_saved
            
        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
    
    # loop processed clips until the video duration covers the audio duration and the small safety margin.
    if video_duration < required_video_duration:
        logger.warning(
            f"video duration ({video_duration:.2f}s) is shorter than required duration "
            f"({required_video_duration:.2f}s), looping clips to match audio length."
        )
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= required_video_duration:
                break
            processed_clips.append(clip)
            video_duration += clip.duration
        logger.info(
            f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, "
            f"required duration: {required_video_duration:.2f}s, "
            f"looped {len(processed_clips)-len(base_clips)} clips"
        )
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    clip_files = [clip.file_path for clip in processed_clips]
    logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
    concat_video_clips_with_ffmpeg(
        clip_files=clip_files,
        output_file=combined_video_path,
        threads=threads,
        output_dir=output_dir,
        max_duration=audio_duration,
    )
    if used_video_paths is not None:
        # Exclude safety-margin clips that FFmpeg trims entirely from the output.
        elapsed = 0.0
        for clip in processed_clips:
            if elapsed >= audio_duration:
                break
            used_video_paths.append(clip.source_file_path)
            elapsed += clip.duration
    
    # clean temp files
    delete_files(clip_files)
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Việc gói phụ đề phải được hoàn thành trước khi thực sự tạo TextClip, nếu không MoviePy sẽ chỉ nhấn vào văn bản gốc
    # Tính diện tích hiển thị. Ở đây PIL dùng để đo chiều rộng theo font chữ và cỡ chữ hiện tại, đảm bảo mỗi dòng càng rộng càng tốt
    # Kiểm soát nó trong phạm vi chiều rộng có sẵn của video để tránh kích thước phông chữ lớn hoặc các câu tiếng Trung dài trực tiếp tràn màn hình.
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    # Những gì getbbox() trả về là "chiều cao mực hiển thị của glyph hiện tại", chứ không phải chiều cao dòng phông chữ. Ví dụ, chỉ
    # Các ký tự tiếng Anh không có hậu duệ như A, m, n, v.v. sẽ thiếu gốc. Khi có nhiều dòng, lỗi này sẽ tích lũy từng dòng.
    # Cuối cùng, dòng cuối cùng của TextClip bị canvas cắt đi. đi lên + đi xuống xuất phát từ chính phông chữ,
    # Nó không bị ảnh hưởng bởi sự kết hợp ngôn ngữ và ký tự cụ thể và phù hợp với mô hình vẽ cơ bản của MoviePy.
    ascent, descent = font.getmetrics()
    line_height = int(ascent + descent)
    if line_height <= 0:
        # Phông chữ TrueType/OpenType bình thường sẽ không được nhập vào đây; giữ nhật ký chẩn đoán và chi tiết kích thước phông chữ,
        # Tránh tạo phụ đề có chiều cao bằng 0 sau khi phông chữ bị hỏng hoặc phông chữ khác thường trả về số liệu bất thường.
        logger.warning(
            "invalid subtitle font metrics, fallback to font size: "
            f"ascent={ascent}, descent={descent}, fontsize={fontsize}"
        )
        line_height = max(1, int(fontsize))

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, line_height
        left, top, right, bottom = font.getbbox(inner_text)
        # Hộp bbox vẫn phù hợp để đo chiều rộng thực tế cần thiết cho việc gói dòng; chiều cao phải luôn sử dụng chiều cao dòng phông chữ ổn định.
        return right - left, line_height

    width, height = get_text_size(text)
    if width <= max_width:
        # Các mục SRT cho phép tác giả ngắt dòng theo cách thủ công. Ngay cả khi toàn bộ văn bản không cần phải được bọc lại theo chiều rộng,
        # Chiều cao của canvas vẫn phải được tính toán dựa trên số hàng hiện có, nếu không hàng thứ hai và các hàng tiếp theo sẽ bị cắt.
        return text, (text.count("\n") + 1) * line_height

    def split_long_token(token):
        # Khi bản thân mã thông báo quá rộng (thường gặp trong các câu dài không có dấu cách trong tiếng Trung hoặc các từ dài trong tiếng Anh),
        # Suy thoái thành sự phân chia cấp độ nhân vật. Điểm mấu chốt là: khi phát hiện một ứng viên quá rộng, hãy gửi ứng viên trước đó trước
        # Hiện tại vẫn hợp lệ và sau đó ký tự hiện tại được đưa vào dòng tiếp theo. Không thể nhét các ký tự cực rộng vào dòng trước đó.
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    line_start_punctuation = "，。！？；：、,.!?;:)]}）】》」』”’"
    for index in range(1, len(lines)):
        # Khi một câu tiếng Trung dài được chia thành các ký tự, dấu chấm cuối cùng, dấu phẩy và dấu câu kết thúc khác có thể được tách ra
        # Đặt xuống dòng tiếp theo khiến nền phụ đề nổi lên bất thường, nhìn giống như một chấm nhỏ rơi trên văn bản chính.
        # dưới. Ở đây, không cần thiết kế lại thuật toán dòng mới, từ cuối cùng của dòng trước là
        # Di chuyển nó lên phía trước dòng chấm câu và để dấu chấm câu theo sau màn hình văn bản. Nó tương thích với dấu câu đóng phổ biến trong tiếng Trung và tiếng Anh.
        if not lines[index] or lines[index][0] not in line_start_punctuation:
            continue
        if len(lines[index - 1]) <= 1:
            continue

        candidate = f"{lines[index - 1][-1]}{lines[index]}"
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            lines[index] = candidate
            lines[index - 1] = lines[index - 1][:-1]

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    # Chiều cao phụ thuộc vào kết quả cuối cùng. Ngắt dòng rõ ràng trong văn bản gốc có thể được giữ lại trong mã thông báo,
    # Tại thời điểm này, độ dài của danh sách dòng tạm thời không bằng số dòng thực sự được MoviePy hiển thị.
    height = (result.count("\n") + 1) * line_height
    return result, height


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    # Màu nền phụ đề đến từ các tham số API/WebUI và có thể trống hoặc ở định dạng không đều. Ở đây chúng tôi chỉ chấp nhận
    # Ở dạng #RRGGBB, các giá trị không hợp lệ sẽ chuyển về màu đen để ngăn giai đoạn kết xuất PIL ném ra các ngoại lệ và làm gián đoạn tác vụ.
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        except ValueError:
            pass
    return (0, 0, 0)


def _rounded_subtitle_background_clip(
    width: int,
    height: int,
    color: str,
    alpha: int = 140,
    radius: int = 16,
) -> ImageClip:
    # Nền phụ đề mới chỉ được sử dụng khi người dùng bật nó một cách rõ ràng: vẽ một tấm đế tròn bán trong suốt từ hình ảnh RGBA,
    # Sau đó chuyển nó cho MoviePy dưới dạng ImageClip trong suốt để tham gia tổng hợp. Bằng cách này, đường dẫn mặc định vẫn hoàn toàn không thay đổi.
    # Đồng thời, bạn có thể thử nghiệm hình ảnh phụ đề nhẹ nhàng hơn với chi phí thấp.
    rgb = _hex_to_rgb(color)
    safe_alpha = max(0, min(255, int(alpha)))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, max(0, width - 1), max(0, height - 1)],
        radius=max(0, int(radius)),
        fill=(rgb[0], rgb[1], rgb[2], safe_alpha),
    )
    return ImageClip(np.array(img), transparent=True)


def _get_visible_center_position(
    text_clip: TextClip,
    container_width: int,
    container_height: int,
) -> tuple[int, int]:
    """
    Đặt TextClip vào giữa vùng chứa nền theo các pixel hiển thị thực tế của văn bản.

    TextClip của MoviePy tạo một khung vẽ trong suốt dựa trên chiều cao và đường cơ sở của dòng phông chữ. nhiều phông chữ
    Có thể thấy rằng glyph không nằm ở trung tâm hình học của khung vẽ này, trực tiếp `with_position("center")`
    Toàn bộ khung vẽ trong suốt sẽ được căn giữa, khiến phụ đề trông cao hơn hoặc thấp hơn. Đọc ở đây TextClip
    Mặt nạ trong suốt chỉ tính toán offset dựa trên bbox thực sự có pixel để người dùng có thể nhìn thấy văn bản
    Trực quan tập trung vào nền phụ đề.
    """
    x = int(round((container_width - text_clip.w) / 2))
    y = int(round((container_height - text_clip.h) / 2))

    try:
        if text_clip.mask is None:
            return x, y

        mask_frame = text_clip.mask.get_frame(0)
        ys, _ = np.where(mask_frame > 0.01)
        if len(ys) == 0:
            return x, y

        visible_top = int(ys.min())
        visible_bottom = int(ys.max())
        visible_height = visible_bottom - visible_top + 1
        y = int(round((container_height - visible_height) / 2 - visible_top))
    except Exception as exc:
        logger.debug(f"failed to center subtitle text by visible mask: {str(exc)}")

    return x, y


def subtitle_colors_are_indistinguishable(params: VideoParams) -> bool:
    """Xác định xem văn bản phụ đề và nền có cùng màu hay không và nhắc nhở người dùng rằng họ có thể không nhìn rõ phụ đề."""
    if not params.subtitle_enabled or not params.text_background_color:
        return False

    def normalize_color(value):
        if isinstance(value, bool):
            return "#000000" if value else ""
        return str(value or "").strip().lower()

    text_color = normalize_color(params.text_fore_color)
    background_color = normalize_color(params.text_background_color)
    return bool(text_color and text_color == background_color)


@lru_cache(maxsize=64)
def _subtitle_font_supports_sample(font_path: str, sample: str) -> bool:
    """Kiểm tra xem phông chữ có chứa các ký tự cần thiết cho văn bản mẫu hay không và lưu trữ các kết quả kiểm tra trùng lặp."""
    try:
        font = ImageFont.truetype(font_path, 30)
        missing_mask = font.getmask("\U0010ffff")
        missing_signature = (
            missing_mask.size,
            missing_mask.getbbox(),
            bytes(missing_mask),
        )
        for char in sample:
            char_mask = font.getmask(char)
            char_signature = (
                char_mask.size,
                char_mask.getbbox(),
                bytes(char_mask),
            )
            if char_mask.getbbox() is None or char_signature == missing_signature:
                return False
        return True
    except Exception as e:
        # Lỗi phát hiện phông chữ không ngăn cản người dùng xây dựng; giữ nhật ký để khắc phục sự cố tương thích môi trường.
        logger.warning(f"failed to inspect subtitle font glyphs: {font_path}, {e}")
        return True


def subtitle_font_supports_text(font_path: str, text: str) -> bool:
    """Kiểm tra xem phông chữ có thể vẽ được chữ cái và số trong văn bản hay không, bỏ qua khoảng trắng và dấu câu."""
    sample = "".join(
        dict.fromkeys(
            char
            for char in str(text or "")
            if unicodedata.category(char)[0] in {"L", "N"}
        )
    )[:64]
    if not sample:
        return True
    return _subtitle_font_supports_sample(font_path, sample)


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    bgm_file_override: str | None = None,
) -> bool:
    """
    Tổng hợp video cuối cùng và trả lời xem quá trình xử lý nhạc nền có thành công hay không.

    Giá trị trả về chỉ mô tả trạng thái xử lý BGM: Trả về true khi BGM không được yêu cầu hoặc trộn thành công; được yêu cầu
    BGM nhưng trả về Sai nếu tải, hiệu ứng hoặc trộn không thành công. Ngay cả khi BGM bị lỗi, nó sẽ chỉ tiếp tục xuất ra
    Video tường thuật cho phép lớp điều phối tác vụ quyết định xem có hiển thị cảnh báo xuống cấp cho người dùng hay không.
    """
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def resolve_subtitle_background_color():
        # Tương thích với các tham số lịch sử: `text_background_color` trong API có thể là giá trị Boolean,
        # Cũng có thể là một chuỗi màu thực tế. Chuẩn hóa thống nhất ở đây để tránh chuyển đổi Đúng/Sai
        # Kết quả hiển thị không mong muốn xảy ra sau khi chuyển trực tiếp tới TextClip.
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )
        has_subtitle_background = bool(bg_color)
        # Nền tròn được tạo theo chiều rộng thực tế của văn bản và khoảng trắng bên trái và bên phải phải được hạn chế hơn; nền hình chữ nhật cũ vẫn được giữ lại
        # Giới hạn an toàn lớn hơn để tránh phụ đề dài bị cắt cạnh hoặc cắt xén trong cấu hình cũ.
        padding_ratio = 0.4 if rounded_bg_enabled else 0.6
        pad_x = int(params.font_size * padding_ratio) if has_subtitle_background else 0
        # Nền phụ đề cần để lại phần đệm rõ ràng ở bên trái và bên phải của văn bản. Đầu tiên trừ đi chiều rộng có sẵn
        # đệm rồi ngắt dòng để tránh tiếng Anh dài hoặc cỡ chữ lớn chỉ lấp đầy 90% chiều rộng video.
        # Văn bản được dán vào cạnh của hộp nền và trông như bị cắt bớt. Nền hình chữ nhật thông thường và nền góc tròn
        # Logic này được tuân theo; phụ đề không có nền duy trì độ rộng tối đa ban đầu.
        text_max_width = max(1, int(max_width) - 2 * pad_x)
        wrapped_txt, txt_height = wrap_text(
            phrase,
            max_width=text_max_width,
            font=font_path,
            fontsize=params.font_size,
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        # Pillow/MoviePy sẽ mở rộng nét vẽ lên cạnh trên và dưới của glyph và bao gồm phần này trong mỗi dòng
        # chiều cao hành trình. Nếu chỉ thêm một nét khoảng trắng bên ngoài toàn bộ khối phụ đề thì một nét đậm gồm nhiều dòng văn bản
        # Lỗi vẫn sẽ tích lũy từng hàng. Ở đây, số dòng thực tế được bao gồm trong không gian nét hai mặt. Theo mặc định, chỉ có những nét mảnh
        # Thêm một chút chiều cao và "cỡ chữ nhỏ + nét dày + nhiều dòng" có thể được hiển thị đầy đủ.
        stroke_padding = int(params.stroke_width * 2 * line_count)
        text_clip_margin_y = max(
            int(params.font_size * 0.3), int(params.stroke_width * 2)
        )
        # MoviePy sẽ tự động thu nhỏ chiều cao của hộp văn bản dưới `method=label`. Khi gặp phụ đề nhiều dòng,
        # Khi sử dụng nét vẽ hoặc màu nền rất dễ bị cắt mất nửa dưới của dòng cuối cùng. Được chuyển rõ ràng vào đây
        # Chiều cao vừa phải hơn, có tính đến khoảng cách dòng và khoảng trắng bổ sung trên và dưới để đảm bảo phụ đề
        # Cả khung nền và văn bản đều có thể được hiển thị đầy đủ.
        clip_h = int(
            txt_height
            + vertical_padding
            + (interline * line_count)
            + stroke_padding
        )

        if rounded_bg_enabled:
            # Nền tròn cần vừa với chiều rộng của văn bản, thay vì chiếm 90% chiều rộng của video. Sử dụng nó ở đây đầu tiên
            # PIL đo dòng văn bản dài nhất và thêm khoảng đệm ngang để tránh khoảng đệm quá rộng cho phụ đề ngắn.
            try:
                font = ImageFont.truetype(font_path, params.font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(params.font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(box_w, None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            clip_h = max(clip_h, text_clip.h)
            bg_clip = _rounded_subtitle_background_clip(
                width=box_w,
                height=clip_h,
                color=bg_color,
                alpha=140,
                radius=radius,
            )
            text_position = _get_visible_center_position(text_clip, box_w, clip_h)
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=(box_w, clip_h),
            )
        elif bg_color:
            size = (
                int(max_width),
                clip_h,
            )
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(int(max_width), None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            size = (size[0], max(size[1], text_clip.h))
            bg_clip = _rounded_subtitle_background_clip(
                width=size[0],
                height=size[1],
                color=bg_color,
                alpha=255,
                radius=0,
            )
            text_position = _get_visible_center_position(text_clip, size[0], size[1])
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=size,
            )
        else:
            size = (
                int(max_width),
                clip_h,
            )
            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=size,
                text_align="center",
            )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)

        # Hoạt ảnh thoát chỉ được bật khi người dùng chọn nó một cách rõ ràng; mặc định là không có và đường dẫn hiển thị phụ đề gốc hoàn toàn được sử dụng.
        anim_type = getattr(params, "subtitle_animation", "none")
        if anim_type in ("pop_spring", "spring", "pop"):
            _clip = _apply_subtitle_spring_animation(_clip, duration)

        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position in ("two_thirds_bottom", "two_thirds", "2/3_bottom"):
            # 2/3 from the bottom = 1/3 from the top: y = (video_height - _clip.h) * (1/3)
            y_two_thirds = (video_height - _clip.h) / 3.0
            _clip = _clip.with_position(("center", y_two_thirds))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    # CompositeAudioClip.close() của MoviePy không đóng AudioFileClip con. Được sử dụng ở đây
    # ExitStack nắm giữ rõ ràng tất cả các trình đọc tệp thô, đảm bảo thành công, ngoại lệ phụ đề, lỗi phối lại và
    # Các đường dẫn như lỗi ghi video có thể giải phóng quy trình con FFmpeg, đặc biệt là để ngăn các tệp Windows bị chiếm dụng.
    with ExitStack() as clip_stack:
        source_video_clip = clip_stack.enter_context(
            _open_video_clip_quietly(video_path)
        )
        voice_source_clip = clip_stack.enter_context(AudioFileClip(audio_path))
        video_clip = source_video_clip
        audio_clip = voice_source_clip.with_effects(
            [afx.MultiplyVolume(params.voice_volume)]
        )

        def make_textclip(text):
            return TextClip(
                text=text,
                font=font_path,
                font_size=params.font_size,
            )

        if subtitle_path and os.path.exists(subtitle_path):
            sub = clip_stack.enter_context(
                SubtitlesClip(
                    subtitles=subtitle_path,
                    encoding="utf-8",
                    make_textclip=make_textclip,
                )
            )
            text_clips = []
            for item in sub.subtitles:
                clip = create_text_clip(subtitle_item=item)
                text_clips.append(clip)
            video_clip = CompositeVideoClip([video_clip, *text_clips])
            clip_stack.callback(video_clip.close)

        bgm_enabled = bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        )
        if not bgm_enabled and params.bgm_type:
            # Tất cả các nguồn BGM đều có chung quy tắc đoản mạch này. Không thể phân tích ngẫu nhiên hoặc khi âm lượng không lớn hơn 0
            # Các tệp tùy chỉnh cũng không thể tải các tệp được nhà cung cấp trả về để tránh việc IO và phối lại vô nghĩa.
            logger.info(
                f"skipping background music because volume is not positive: "
                f"type={params.bgm_type}, volume={params.bgm_volume}"
            )

        # Nhạc nền của nhà cung cấp có thể được chuyển trực tiếp vào tệp tương ứng từ lớp điều phối tác vụ. Không có nghĩa là sử dụng ngẫu nhiên/tùy chỉnh
        # Phân tích cú pháp BGM, một chuỗi trống sẽ vô hiệu hóa BGM này một cách rõ ràng; nhưng nguồn nào cũng phải vượt qua quy định chung về âm lượng trước.
        bgm_file = ""
        if bgm_enabled:
            bgm_file = (
                bgm_file_override
                if bgm_file_override is not None
                else get_bgm_file(
                    bgm_type=params.bgm_type,
                    bgm_file=params.bgm_file,
                )
            )
        bgm_mix_succeeded = True
        if bgm_file:
            try:
                bgm_effects = [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                ]
                # Nhạc ngẫu nhiên/tùy chỉnh được phân tích cú pháp trong dịch vụ có thể ngắn hơn phim cuối cùng và cần được lặp lại; lớp nhiệm vụ
                # Tệp được chuyển qua ghi đè cho biết rằng nhà cung cấp đã hoàn thành việc điều chỉnh thời lượng. Đây là cơ sở
                # Nguồn tệp xác định xem có quay vòng hay không, để tránh sửa đổi danh sách trắng tên mỗi khi nhà cung cấp được thêm vào trong tương lai.
                if bgm_file_override is None:
                    bgm_effects.append(afx.AudioLoop(duration=video_clip.duration))
                bgm_source_clip = clip_stack.enter_context(AudioFileClip(bgm_file))
                bgm_clip = bgm_source_clip.with_effects(bgm_effects)
                audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
            except Exception:
                bgm_mix_succeeded = False
                # Ghi lại ngăn xếp hoàn chỉnh và bối cảnh ổn định để dễ dàng phân biệt giữa giải mã tệp, hiệu ứng MoviePy và
                # CompositeAudioClip không thành công; nội dung tệp và Khóa API sẽ không được nhập vào nhật ký.
                logger.exception(
                    f"failed to mix background music: type={params.bgm_type}, "
                    f"file={bgm_file}"
                )

        final_video_clip = video_clip.with_audio(audio_clip)
        clip_stack.callback(final_video_clip.close)
        # Sử dụng rõ ràng tốc độ lấy mẫu của âm thanh đầu vào; nếu không thể lấy được, hãy quay lại 44100Hz mặc định của MoviePy.
        # Điều này có thể làm giảm sự biến động về chất lượng âm thanh do lấy mẫu lại trong các môi trường khác nhau, đặc biệt là Docker.
        output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
        _write_videofile_with_codec_fallback(
            final_video_clip,
            output_file=output_file,
            codec=_get_configured_video_codec(),
            audio_codec=audio_codec,
            audio_fps=output_audio_fps,
            audio_bitrate=audio_bitrate,
            temp_audiofile_path=_get_temp_audio_dir(output_dir),
            threads=params.n_threads or 2,
            logger=None,
            fps=fps,
        )
        return bgm_mix_succeeded


def render_image_zoom_video(image_path: str, clip_duration: int = 5) -> str:
    """
    Hiển thị một hình ảnh cục bộ thành một clip mp4 với khả năng khuếch đại chậm, trả về đường dẫn tệp đầu ra.

    Quá trình tiền xử lý vật liệu cục bộ và vật liệu hình ảnh Vincent tương thích với OpenAI chia sẻ kết xuất "hình ảnh → đoạn" này
    Logic: ImageClip được phát trong một khoảng thời gian cố định theo clip_duration và được xếp chồng lên nhau với tốc độ khoảng 3% mỗi giây.
    Khuếch đại động giúp hình ảnh tĩnh không bị mờ trong phim hoàn thiện. Ngoại lệ hiển thị được đưa ra bởi người gọi riêng lẻ
    Xử lý hợp đồng nguồn nguyên liệu không thành công.
    """
    clip = ImageClip(image_path).with_duration(clip_duration).with_position("center")
    try:
        # Apply a zoom effect using the resize method.
        # A lambda function is used to make the zoom effect dynamic over time.
        # The zoom effect starts from the original size and gradually scales up to 120%.
        # t represents the current time, and clip.duration is the total duration of the clip.
        # Note: 1 represents 100% size, so 1.2 represents 120%.
        zoom_clip = clip.resized(
            lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
        )

        # Optionally, create a composite video clip containing the zoomed clip.
        # This is useful if you want to add other elements to the video.
        final_clip = CompositeVideoClip([zoom_clip])
        try:
            # Output the video to a file.
            video_file = f"{image_path}.mp4"
            final_clip.write_videofile(video_file, fps=30, logger=None)
            return video_file
        finally:
            close_clip(final_clip)
    finally:
        close_clip(clip)


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    # WebUI có thể chuyển vào danh sách vật liệu trống trong một số trường hợp thế hệ thứ cấp. Ở đây, nó trả về trực tiếp một kết quả trống để tránh đưa ra các ngoại lệ NoneType.
    if not materials:
        return []

    # Chỉ những tài liệu vượt qua quá trình xác minh tiền xử lý mới được trả lại để ngăn hình ảnh có độ phân giải thấp xâm nhập vào quá trình tổng hợp video tiếp theo.
    valid_materials = []
    local_videos_dir = utils.storage_dir("local_videos", create=True)

    for material in materials:
        if not material.url:
            continue

        try:
            material_source_path = file_security.resolve_path_within_directory(
                local_videos_dir, material.url
            )
        except ValueError as exc:
            # Đường dẫn vật liệu của video_source cục bộ xuất phát từ các tham số API và phải được giới hạn trong thư mục vật liệu chuyên dụng.
            # Người dùng được phép truyền tên file và cũng tương thích với các đường dẫn tuyệt đối được lịch sử trả về nhưng không được phép thoát ra hệ thống.
            # Các thư mục khác để tránh việc đọc tệp tùy ý hoặc phát hiện các tệp nhạy cảm cục bộ thông qua MoviePy.
            logger.warning(
                f"skip unsafe local material: {material.url}, "
                f"local_videos_dir: {local_videos_dir}, error: {str(exc)}"
            )
            continue

        ext = utils.parse_extension(material_source_path)
        try:
            # Tài liệu hình ảnh được đọc trực tiếp dưới dạng hình ảnh để tránh đánh giá sai VideoFileClip và gây ra các nhánh dự phòng không ổn định.
            if ext in const.FILE_TYPE_IMAGES:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            else:
                clip = _open_video_clip_quietly(material_source_path)
        except Exception:
            # Nó sẽ quay trở lại chế độ hình ảnh khi có tiện ích mở rộng không chuẩn hoặc phát hiện không thành công, tương thích với tình huống lịch sử tải trực tiếp đường dẫn hình ảnh cục bộ lên.
            try:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            width = clip.size[0]
            height = clip.size[1]
            if not is_material_resolution_acceptable(width, height):
                logger.warning(
                    f"low resolution material: {width}x{height}, minimum "
                    f"{_MIN_MATERIAL_DIMENSION}x{_MIN_MATERIAL_DIMENSION} required "
                    f"(tolerance {_MIN_DIMENSION_TOLERANCE}px)"
                )
                # Đóng tài nguyên ngay sau khi phát hiện tài liệu có độ phân giải thấp và không trả lại tài liệu cho các quy trình tiếp theo.
                close_clip(clip)
                continue

            if ext in const.FILE_TYPE_IMAGES:
                logger.info(f"processing image: {material_source_path}")
                # Vật liệu đã được mở một lần khi phát hiện kích thước. Ở đây, tay cầm phát hiện được nhả ra trước rồi mới được hiển thị.
                # Đoạn hình ảnh để xuất.
                close_clip(clip)
                video_file = render_image_zoom_video(
                    material_source_path, clip_duration
                )
                material.url = video_file
                logger.success(f"image processed: {video_file}")
            else:
                # Các tài liệu video thông thường chỉ cần đọc kích thước để xác minh và nhả tay cầm ngay sau khi xác minh hoàn tất.
                close_clip(clip)
                # Update url to the resolved absolute path so that downstream
                # stages (combine_videos) can open the file without re-resolving.
                material.url = material_source_path
        except Exception:
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
