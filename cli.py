from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import sys
from typing import TYPE_CHECKING, Any, Sequence
from uuid import UUID, uuid4

from loguru import logger

if TYPE_CHECKING:
    from app.models.schema import MaterialInfo, VideoParams


DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoNeural-Female"
# Tương ứng với VOICE_MODE_NONE và VOICE_MODE_UPLOAD của webui/Main.py. Hiện tại không có
# Các hằng số này được chia sẻ, vì vậy hãy giữ nguyên nghĩa của chúng ở đây và trích dẫn nguồn.
UI_VOICE_MODE_NONE = "none"
# Tích hợp mặc định cho vị trí phụ đề. VideoParams cũng có giá trị mặc định cùng tên, nhưng đó là trường Pydantic
# Theo mặc định, config.ui chỉ được đọc một lần khi mô-đun được nhập: lúc này, các giá trị không hợp lệ trong config.toml sẽ là
# Được cố định trực tiếp vào mô hình, nó sẽ không được xác minh hay thay thế trong quá trình thử nghiệm. Do đó CLI giữ một bản sao cho chính nó,
# Đảm bảo rằng "các giá trị được lưu bất hợp pháp rơi về giá trị mặc định" luôn đúng cho dòng lệnh.
DEFAULT_SUBTITLE_POSITION = "bottom"
DEFAULT_CUSTOM_POSITION = 70.0
UI_VOICE_MODE_UPLOAD = "upload"
# Cả hai phương pháp lồng tiếng đã lưu này đều không có nghĩa là tự động lồng tiếng.
UI_VOICE_MODES_WITHOUT_TTS = frozenset({UI_VOICE_MODE_NONE, UI_VOICE_MODE_UPLOAD})
_PIPELINE_STAGES = ("script", "terms", "audio", "subtitle", "materials", "video")
_CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_BATCH_FILE_MAX_BYTES = 1024 * 1024
_BATCH_TASK_MAX_COUNT = 100
# Tệp kê khai argparse tác vụ đơn và tệp kê khai hàng loạt phải chia sẻ cùng một bộ sưu tập nguồn. Hai lần bảo trì thủ công trước đó đã dẫn đến
# openai_image chỉ khả dụng ở lối vào một nhiệm vụ; sau khi xác định tập trung, Nhà cung cấp mới sẽ không bị bỏ lỡ nữa
# Xác minh hàng loạt. Chỉ những nguồn đã được CLI hỗ trợ công khai mới được đưa vào đây và các quy trình dành riêng cho WebUI không bị buộc phải tiết lộ.
_CLI_VIDEO_SOURCES = (
    "pexels",
    "pixabay",
    "coverr",
    "volcengine_seedance",
    "ofox",
    "metaso_minimax",
    "openai_image",
    "local",
)


class _CliHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Tự động hiển thị các giá trị mặc định có ý nghĩa trong khi vẫn giữ nguyên bố cục của các ví dụ nhiều dòng."""

    def _get_help_string(self, action):
        help_text = action.help or ""
        if (
            "%(default)" not in help_text
            and action.default not in (None, "", argparse.SUPPRESS)
            and action.option_strings
            and "default:" not in help_text.lower()
        ):
            help_text += " (default: %(default)s)"
        return help_text


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"value must be >= 1, got {parsed}")
    return parsed


def _paragraph_count(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 10:
        raise argparse.ArgumentTypeError(
            f"paragraph-number must be between 1 and 10, got {parsed}"
        )
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(f"value must be a finite number >= 0, got {value!r}")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be a finite number > 0, got {value!r}")
    return parsed


def _percent_position(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError(
            f"custom-position must be a finite number between 0 and 100, got {value!r}"
        )
    return parsed


def _hex_color(value: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError(
            f"color must use #RRGGBB format, got {value!r}"
        )
    return value


def _subtitle_position(value: str) -> str:
    """Xác minh vị trí của phụ đề đã lưu và phạm vi giá trị phù hợp với tham số dòng lệnh."""
    if value not in ("top", "center", "bottom", "custom"):
        raise argparse.ArgumentTypeError(
            f"subtitle-position must be one of: top, center, bottom, custom, got {value!r}"
        )
    return value


def _task_id(value: str) -> str:
    """Mã nhận dạng tác vụ tùy chỉnh CLI chỉ chấp nhận UUID để tránh giá trị được hiểu là đường dẫn hệ thống tệp."""
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"task-id must be a valid UUID, got {value!r}"
        ) from exc


_TRANSITION_MODE_VALUES = {
    "none": None,
    "shuffle": "Shuffle",
    "fade-in": "FadeIn",
    "fade-out": "FadeOut",
    "slide-in": "SlideIn",
    "slide-out": "SlideOut",
}


def _transition_mode(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized not in _TRANSITION_MODE_VALUES:
        allowed = ", ".join(_TRANSITION_MODE_VALUES)
        raise argparse.ArgumentTypeError(
            f"video-transition-mode must be one of: {allowed}"
        )
    return _TRANSITION_MODE_VALUES[normalized]


def _video_fit_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"cover", "contain"}:
        raise argparse.ArgumentTypeError(
            "video-fit-mode must be one of: cover, contain"
        )
    return normalized


def _bgm_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "none":
        return ""
    if normalized in {"", "random", "custom", "sonilo"}:
        return normalized
    raise argparse.ArgumentTypeError(
        "bgm-type must be one of: none, random, custom, sonilo"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate MoneyPrinterTurbo videos without the WebUI.\n\n"
            "Provider settings and credentials are read from config.toml.\n"
            "Default full-video generation requires a configured LLM and Pexels API key.\n"
            "The default Edge TTS voice requires no API key."
        ),
        epilog="""
Examples:
  Generate a complete video with the default Edge TTS voice:
    uv run python cli.py --video-subject "How AI is changing everyday life"

  Generate from local files. Relative paths use the current working directory;
  absolute paths are also accepted:
    uv run python cli.py --video-subject "How AI is changing everyday life" \\
      --video-source local --video-materials "./1.mp4,./2.mp4"

  Generate with a prepared script and no voiceover:
    uv run python cli.py --video-script "Your complete script" \\
      --voice-name no-voice --stop-at video

  Stop after script generation:
    uv run python cli.py --video-subject "How AI is changing everyday life" --stop-at script

  Run a JSON array or JSONL manifest. CLI options provide defaults and each object
  overrides VideoParams fields for one task:
    uv run python cli.py --batch-file ./tasks.jsonl --stop-at script

Pipeline stages:
  script     Generate or return the script.
  terms      Generate material search terms; unavailable with local materials.
  audio      Generate TTS, silent audio, or use --custom-audio-file.
  subtitle   Generate subtitles when enabled.
  materials  Download online materials or preprocess local files.
  video      Generate the final video and run configured cross-posting.
  The command stops immediately after the selected stage and prints that stage's result.

Output and exit status:
  Task files are written to storage/tasks/<task-id>/. A successful command prints one
  JSON object to stdout and exits with 0. Batch mode prints one JSON summary after all
  tasks finish. Task failures exit with 1; argument or manifest errors exit with 2
  before any batch task starts. Runtime logs are written to stderr.

Batch manifests:
  --batch-file accepts a UTF-8 JSON array or JSONL file with one object per non-empty
  line (up to 100 tasks and 1 MiB). Objects may override VideoParams fields; unknown
  fields are rejected. Every merged task needs video_subject or video_script.
  The manifest path is relative to the current working directory. Relative
  custom_audio_file and local video_materials[].url values declared in a manifest are
  relative to the manifest directory. Relative paths supplied by CLI options remain
  relative to the current working directory. bgm_file and font_name keep their managed
  storage/resource lookup rules. --stop-at is a batch-wide CLI option. The summary has
  total, succeeded, failed, and tasks keys; every task entry has index, task_id,
  status, result, failed_stage, and error.
""",
        formatter_class=_CliHelpFormatter,
    )

    content_group = parser.add_argument_group("script and content")
    content_group.add_argument(
        "--video-subject",
        default="",
        help="video topic; required unless --video-script is provided",
    )
    content_group.add_argument(
        "--video-script",
        default="",
        help="complete script; skips LLM script generation when provided",
    )
    content_group.add_argument(
        "--video-terms",
        default=None,
        help="comma-separated material search terms; generated automatically when omitted",
    )
    content_group.add_argument(
        "--video-language",
        default=None,
        help=(
            "script language code, such as zh-CN or en-US (default: auto-detect)"
        ),
    )
    content_group.add_argument(
        "--paragraph-number",
        type=_paragraph_count,
        default=None,
        help="number of generated script paragraphs, from 1 to 10 (default: 1)",
    )
    content_group.add_argument(
        "--video-script-prompt",
        default=None,
        help="additional requirements for LLM script generation",
    )
    content_group.add_argument(
        "--custom-system-prompt",
        default=None,
        help="replace the default LLM system prompt for script generation",
    )

    material_group = parser.add_argument_group("materials and pipeline")
    material_group.add_argument(
        "--video-source",
        default="pexels",
        choices=_CLI_VIDEO_SOURCES,
        help="video material provider; online providers require matching API keys in config.toml",
    )
    material_group.add_argument(
        "--video-materials",
        default="",
        metavar="PATH[,PATH...]",
        help=(
            "comma-separated local image/video paths for --video-source local; relative "
            "paths use the current working directory, then storage/local_videos as a "
            "compatibility fallback; absolute paths are accepted"
        ),
    )
    material_group.add_argument(
        "--stop-at",
        default="video",
        choices=_PIPELINE_STAGES,
        help="stop after this pipeline stage; see the stage order below",
    )
    material_group.add_argument(
        "--confirm-seedance-charge",
        action="store_true",
        help=(
            "confirm that Volcano Engine Seedance creates paid Ark tasks; required "
            "with --video-source volcengine_seedance for materials or video output"
        ),
    )
    material_group.add_argument(
        "--confirm-ofox-charge",
        action="store_true",
        help=(
            "confirm that OFox video generation creates paid tasks; required "
            "with --video-source ofox for materials or video output"
        ),
    )
    material_group.add_argument(
        "--confirm-metaso-minimax-charge",
        action="store_true",
        help=(
            "confirm that Metaso MiniMax H3 creates paid video tasks; required "
            "with --video-source metaso_minimax for materials or video output"
        ),
    )

    video_group = parser.add_argument_group("video output")
    video_group.add_argument(
        "--video-count",
        type=_positive_int,
        default=1,
        help="number of output videos, at least 1",
    )
    video_group.add_argument(
        "--video-aspect",
        choices=["9:16", "16:9", "1:1"],
        default="9:16",
        help="output aspect ratio: portrait, landscape, or square",
    )
    video_group.add_argument(
        "--video-fit-mode",
        type=_video_fit_mode,
        choices=["cover", "contain"],
        default=None,
        help=(
            "fit mismatched source clips by filling and center-cropping (cover) "
            "or preserving the full frame with black bars (contain); default: cover"
        ),
    )
    video_group.add_argument(
        "--video-concat-mode",
        choices=["random", "sequential"],
        default=None,
        help="source clip concatenation order (default: random)",
    )
    video_group.add_argument(
        "--video-transition-mode",
        type=_transition_mode,
        default=None,
        metavar="{none,shuffle,fade-in,fade-out,slide-in,slide-out}",
        help="transition applied between source clips (default: none)",
    )
    video_group.add_argument(
        "--video-clip-duration",
        type=_positive_int,
        default=None,
        help=(
            "maximum duration of each source clip in seconds, at least 1 (default: 5)"
        ),
    )
    video_group.add_argument(
        "--match-materials-to-script",
        default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "preserve script keyword order while selecting and concatenating materials "
            "(default: disabled)"
        ),
    )
    video_group.add_argument(
        "--n-threads",
        type=_positive_int,
        default=None,
        help="FFmpeg worker thread count, at least 1 (default: 2)",
    )

    audio_group = parser.add_argument_group("voiceover and background music")
    audio_group.add_argument(
        "--voice-name",
        default=None,
        help=(
            f"TTS voice identifier. Defaults to [ui].voice_name from "
            f"config.toml, otherwise {DEFAULT_VOICE_NAME}. A saved "
            "[ui].voice_mode of 'none' or 'upload' resolves to no-voice "
            "instead, unless this option is given. "
            "Use 'no-voice' for silent output. Provider-specific identifiers "
            "use prefixes such as gemini:, mimo:, elevenlabs:, chatterbox:, and kokoro:"
        ),
    )
    audio_group.add_argument(
        "--voice-volume",
        type=_non_negative_float,
        default=None,
        help=(
            "final voiceover volume multiplier, a finite number >= 0 (default: "
            "[ui].voice_volume from config.toml; 1.0 when unset)"
        ),
    )
    audio_group.add_argument(
        "--voice-rate",
        type=_positive_float,
        default=None,
        help=(
            "speech rate multiplier, a finite number > 0 (default: "
            "[ui].voice_rate from config.toml; 1.0 when unset)"
        ),
    )
    audio_group.add_argument(
        "--custom-audio-file",
        default=None,
        metavar="PATH",
        help=(
            "existing MP3/WAV/M4A/AAC/FLAC/OGG voiceover; relative paths use the "
            "current working directory. This skips TTS; set subtitle_provider=whisper "
            "to transcribe it"
        ),
    )
    audio_group.add_argument(
        "--bgm-type",
        type=_bgm_type,
        default=None,
        metavar="{none,random,custom,sonilo}",
        help=(
            "background music mode; Sonilo reads its API key from config.toml or "
            "SONILO_API_KEY; --bgm-file implies custom when omitted "
            "(default: random)"
        ),
    )
    audio_group.add_argument(
        "--sonilo-bgm-prompt",
        default=None,
        help="optional music style prompt for Sonilo, up to 2000 characters",
    )
    audio_group.add_argument(
        "--bgm-file",
        default=None,
        metavar="PATH",
        help=(
            "custom supported audio file inside storage/bgm or resource/songs; "
            "accepts a filename or an allowed managed path"
        ),
    )
    audio_group.add_argument(
        "--bgm-volume",
        type=_non_negative_float,
        default=None,
        help=(
            "background music volume multiplier, a finite number >= 0 (default: 0.2)"
        ),
    )

    subtitle_group = parser.add_argument_group("subtitles")
    subtitle_group.add_argument(
        "--subtitle-enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "enable subtitles; use --no-subtitle-enabled to disable "
            "(default: [ui].subtitle_enabled from config.toml; enabled when "
            "unset)"
        ),
    )
    subtitle_group.add_argument(
        "--font-name",
        default=None,
        help=(
            "subtitle font filename inside resource/fonts "
            "(default: [ui].font_name from config.toml; "
            "STHeitiMedium.ttc when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--subtitle-position",
        choices=["top", "center", "bottom", "custom"],
        default=None,
        help=(
            "subtitle vertical position (default: [ui].subtitle_position from "
            "config.toml; bottom when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--custom-position",
        type=_percent_position,
        default=None,
        help=(
            "custom position as percent from top, 0-100; requires "
            "--subtitle-position custom (default: [ui].custom_position from "
            "config.toml; 70 when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--text-fore-color",
        type=_hex_color,
        default=None,
        help=(
            "subtitle text color in #RRGGBB format; quote the value in shells "
            "that treat # as a comment (default: [ui].text_fore_color from "
            "config.toml; #FFFFFF when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--font-size",
        type=_positive_int,
        default=None,
        help=(
            "subtitle font size (default: [ui].font_size from config.toml; "
            "60 when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--stroke-color",
        type=_hex_color,
        default=None,
        help=(
            "subtitle outline color in #RRGGBB format (default: "
            "[ui].stroke_color from config.toml; #000000 when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--stroke-width",
        type=_non_negative_float,
        default=None,
        help=(
            "subtitle outline width, a finite number >= 0 (default: "
            "[ui].stroke_width from config.toml; 1.5 when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--subtitle-background-enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "enable subtitle background; use --no-subtitle-background-enabled to "
            "disable (default: [ui].subtitle_background_enabled from "
            "config.toml; disabled when unset)"
        ),
    )
    subtitle_group.add_argument(
        "--subtitle-background-color",
        type=_hex_color,
        default=None,
        help=(
            "subtitle background color in #RRGGBB format (default: "
            "[ui].subtitle_background_color from config.toml)"
        ),
    )
    subtitle_group.add_argument(
        "--rounded-subtitle-background",
        default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "use a rounded subtitle background (default: "
            "[ui].rounded_subtitle_background from config.toml; "
            "disabled when unset)"
        ),
    )

    execution_group = parser.add_argument_group("execution")
    execution_mode = execution_group.add_mutually_exclusive_group()
    execution_mode.add_argument(
        "--task-id",
        type=_task_id,
        default=None,
        help="custom UUID used for storage/tasks/<task-id>; generated automatically when omitted",
    )
    execution_mode.add_argument(
        "--batch-file",
        default=None,
        metavar="PATH",
        help=(
            "UTF-8 JSON array or JSONL task manifest; conflicts with --task-id and "
            "generates a UUID for every task"
        ),
    )
    args = parser.parse_args(argv)

    if (
        not args.batch_file
        and not args.video_subject.strip()
        and not args.video_script.strip()
    ):
        parser.error("one of --video-subject or --video-script is required")

    if not args.batch_file and args.video_source == "local" and args.stop_at == "terms":
        parser.error(
            "--stop-at terms has no effect with --video-source local "
            "(search terms are not generated for local sources)"
        )

    stage_requires_materials = args.stop_at in {"materials", "video"}
    has_video_materials = bool((args.video_materials or "").strip())
    if (
        not args.batch_file
        and args.video_source == "local"
        and stage_requires_materials
        and not has_video_materials
    ):
        parser.error(
            "--video-materials is required with --video-source local when "
            "--stop-at is materials or video"
        )
    if not args.batch_file and args.video_source != "local" and has_video_materials:
        parser.error("--video-materials can only be used with --video-source local")
    if (
        not args.batch_file
        and args.video_source == "volcengine_seedance"
        and stage_requires_materials
        and not args.confirm_seedance_charge
    ):
        parser.error(
            "--confirm-seedance-charge is required with "
            "--video-source volcengine_seedance"
        )
    if (
        not args.batch_file
        and args.video_source == "ofox"
        and stage_requires_materials
        and not args.confirm_ofox_charge
    ):
        parser.error(
            "--confirm-ofox-charge is required with --video-source ofox"
        )
    if (
        not args.batch_file
        and args.video_source == "metaso_minimax"
        and stage_requires_materials
        and not args.confirm_metaso_minimax_charge
    ):
        parser.error(
            "--confirm-metaso-minimax-charge is required with "
            "--video-source metaso_minimax"
        )

    if args.bgm_file:
        if args.bgm_type in (None, "custom"):
            args.bgm_type = "custom"
        elif not args.batch_file:
            parser.error("--bgm-file can only be combined with --bgm-type custom")

    if args.sonilo_bgm_prompt:
        if args.bgm_type in (None, "sonilo"):
            args.bgm_type = "sonilo"
        elif not args.batch_file:
            parser.error(
                "--sonilo-bgm-prompt can only be combined with --bgm-type sonilo"
            )

    if (
        not args.batch_file
        and args.custom_position is not None
        and args.subtitle_position != "custom"
    ):
        parser.error("--custom-position requires --subtitle-position custom")
    # Chỉ --no-subtitle-enabled rõ ràng mới được tính là xung đột. Giá trị mặc định bây giờ là Không có,
    # Trạng thái đóng đã lưu được xử lý trong build_video_params và không nên báo cáo lỗi tham số tại đây.
    if (
        not args.batch_file
        and args.stop_at == "subtitle"
        and args.subtitle_enabled is False
    ):
        parser.error("--stop-at subtitle cannot be combined with --no-subtitle-enabled")
    if not args.batch_file and args.subtitle_background_enabled is False and (
        args.subtitle_background_color is not None
        or args.rounded_subtitle_background is True
    ):
        parser.error(
            "subtitle background color or rounding cannot be enabled together with "
            "--no-subtitle-background-enabled"
        )

    return args


def _ui_config_value(ui_config, key: str, expected_type, checker=None):
    """
    Đọc cài đặt WebUI được lưu trong ``[ui]`` và trả về ``None`` nếu không có giá trị khả dụng.

    Phần cấu hình này cũng có thể được chỉnh sửa thủ công, do đó, các mục không khả dụng sẽ bị loại bỏ trực tiếp tại đây, cho phép người gọi quay lại
    Các giá trị mặc định được tích hợp sẵn, thay vì truyền dữ liệu bẩn, kích hoạt truy nguyên hoặc ``VideoParams``
    lỗi xác minh.

    ``checker`` sử dụng lại cùng một loạt các hàm loại được sử dụng trên dòng lệnh (chẳng hạn như ``_hex_color``), vì vậy hãy lưu lại
    Các giá trị và tham số dòng lệnh tuân theo chính xác các quy tắc giá trị giống nhau, ví dụ: âm lượng không được âm và màu phải là
    ``#RRGGBB``。
    """
    value = ui_config.get(key)
    if value is None:
        return None
    # ``isinstance(True, int)`` là đúng, vì vậy bạn phải loại trừ rõ ràng bool khi mong đợi một số.
    if isinstance(value, bool) != (expected_type is bool):
        return None
    # ``1`` trong TOML là một số nguyên, nhưng nó cũng là một giá trị pháp lý cho các trường như âm lượng và tốc độ nói.
    if expected_type is float and isinstance(value, int):
        value = float(value)
    if not isinstance(value, expected_type):
        return None
    if expected_type is str and not value.strip():
        return None
    if checker is not None:
        try:
            return checker(str(value))
        except argparse.ArgumentTypeError:
            return None
    return value


def _resolve_subtitle_enabled(args: argparse.Namespace, ui_config) -> bool:
    """
    Phân tích phụ đề theo mức độ ưu tiên: Dòng lệnh > Giá trị đã lưu WebUI > Được bật theo mặc định.

    ``--stop-at subtitle`` yêu cầu tạo phụ đề một cách rõ ràng, vì vậy trạng thái đóng đã lưu không cho phép giai đoạn này
    Trở thành không hoạt động; sự kết hợp rõ ràng --no-subtitle-enabled với giai đoạn này đã bị vô hiệu hóa trong quá trình xác thực tham số
    Bị từ chối, vì vậy ở đây chúng ta chỉ phải giải quyết việc lưu giá trị.
    """
    if args.subtitle_enabled is not None:
        return args.subtitle_enabled
    if args.stop_at == "subtitle":
        return True
    saved = _ui_config_value(ui_config, "subtitle_enabled", bool)
    return True if saved is None else saved


def _resolve_voice_name(args: argparse.Namespace, ui_config) -> str:
    """
    Phân tích âm thanh theo mức độ ưu tiên: Dòng lệnh > Phương pháp và âm thanh lồng tiếng đã lưu trên WebUI > Giá trị mặc định tích hợp.

    WebUI lưu "không có giọng nói" dưới dạng voice_mode riêng biệt, trong khi vẫn giữ lại lựa chọn thực sự cuối cùng của người dùng.
    Âm sắc có thể được khôi phục sau khi chuyển về chế độ lồng tiếng tự động. Vì vậy bạn không thể chỉ đọc voice_name ở đây, nếu không nó sẽ được lưu lại
    Không có trạng thái lồng tiếng nào bị bỏ qua và có thể kích hoạt lại yêu cầu của nhà cung cấp dịch vụ trả phí.
    """
    from app.services.voice import NO_VOICE_NAME

    if args.voice_name:
        return args.voice_name
    # Không lồng tiếng và tải lên âm thanh của riêng bạn đều cho thấy rằng người dùng không muốn lồng tiếng tự động. Đường dẫn tệp cho chế độ tải lên sẽ không
    # Viết [ui], CLI không thể sao chép nội dung tải lên; sử dụng âm đã lưu vào thời điểm này sẽ âm thầm kích hoạt TTS trả phí
    # yêu cầu, vì vậy cả hai chế độ đều được ánh xạ thành không có giọng nói và --voice-name được chuyển rõ ràng khi cần lồng tiếng.
    if _ui_config_value(ui_config, "voice_mode", str) in UI_VOICE_MODES_WITHOUT_TTS:
        return NO_VOICE_NAME
    return _ui_config_value(ui_config, "voice_name", str) or DEFAULT_VOICE_NAME


def build_video_params(args: argparse.Namespace) -> VideoParams:
    # Trợ giúp và xác minh thông số không yêu cầu tải cấu hình ứng dụng. Chỉ nhập mô hình khi thực sự xây dựng các tham số tác vụ,
    # Tránh tạo nhật ký khởi tạo cấu hình khi thực thi ``cli.py -h``.
    from app.config import config
    from app.models.schema import MaterialInfo, VideoParams

    ui_config = config.ui

    video_terms = args.video_terms
    if video_terms:
        video_terms = [
            term.strip() for term in re.split(r"[,，]", video_terms) if term.strip()
        ]

    video_materials = None
    materials_arg = args.video_materials or ""
    if materials_arg.strip():
        video_materials = [
            # Actual duration will be detected during video processing; use 0 as placeholder.
            MaterialInfo(provider="local", url=item.strip(), duration=0)
            for item in materials_arg.split(",")
            if item.strip()
        ]

    params_kwargs = {
        "video_subject": args.video_subject.strip(),
        "video_script": args.video_script,
        "video_terms": video_terms,
        "video_source": args.video_source,
        "video_materials": video_materials,
        "video_count": args.video_count,
        "video_aspect": args.video_aspect,
        "voice_name": _resolve_voice_name(args, ui_config),
        "subtitle_enabled": _resolve_subtitle_enabled(args, ui_config),
    }

    optional_arg_names = [
        "video_language",
        "paragraph_number",
        "video_script_prompt",
        "custom_system_prompt",
        "video_fit_mode",
        "video_concat_mode",
        "video_transition_mode",
        "video_clip_duration",
        "match_materials_to_script",
        "n_threads",
        "voice_volume",
        "voice_rate",
        "custom_audio_file",
        "bgm_type",
        "bgm_file",
        "bgm_volume",
        "sonilo_bgm_prompt",
        "font_name",
        "subtitle_position",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "rounded_subtitle_background",
    ]
    for name in optional_arg_names:
        value = getattr(args, name)
        if value is not None:
            params_kwargs[name] = value

    # Khi không có tham số dòng lệnh nào được truyền vào một cách rõ ràng, giá trị được lưu bởi WebUI sẽ được sử dụng. Chỉ cần thêm vào những điều trên mà dòng lệnh chưa thực hiện được
    # Trường cần thiết lập; nếu thiếu giá trị đã lưu, giá trị mặc định của VideoParams sẽ tiếp tục được sử dụng.
    ui_defaults = (
        ("video_fit_mode", str, _video_fit_mode),
        ("font_name", str, None),
        ("text_fore_color", str, _hex_color),
        ("font_size", int, _positive_int),
        ("rounded_subtitle_background", bool, None),
        ("voice_volume", float, _non_negative_float),
        ("voice_rate", float, _positive_float),
        ("stroke_color", str, _hex_color),
        ("stroke_width", float, _non_negative_float),
    )
    for name, expected_type, checker in ui_defaults:
        if name in params_kwargs:
            continue
        value = _ui_config_value(ui_config, name, expected_type, checker)
        if value is not None:
            params_kwargs[name] = value

    # Vị trí phụ đề phải được CLI xác định và không thể để mặc định trường thời gian nhập được mô tả ở trên.
    if "subtitle_position" not in params_kwargs:
        params_kwargs["subtitle_position"] = (
            _ui_config_value(ui_config, "subtitle_position", str, _subtitle_position)
            or DEFAULT_SUBTITLE_POSITION
        )
    if "custom_position" not in params_kwargs:
        saved_custom_position = _ui_config_value(
            ui_config, "custom_position", float, _percent_position
        )
        params_kwargs["custom_position"] = (
            DEFAULT_CUSTOM_POSITION
            if saved_custom_position is None
            else saved_custom_position
        )

    if args.subtitle_background_enabled is False:
        params_kwargs["text_background_color"] = False
        params_kwargs["rounded_subtitle_background"] = False
    elif args.subtitle_background_color is not None:
        params_kwargs["text_background_color"] = args.subtitle_background_color
    elif args.subtitle_background_enabled is True:
        # Người dùng chỉ bật nền chứ không ghi đè lên màu nên màu được lưu bởi WebUI sẽ được sử dụng trước tiên.
        # Chỉ dự phòng về nền mặc định nếu không có giá trị đã lưu.
        params_kwargs["text_background_color"] = (
            _ui_config_value(
                ui_config, "subtitle_background_color", str, _hex_color
            )
            or True
        )
    else:
        # "Tắt nền" cộng với màu sắc khi kết hợp dòng lệnh là lỗi tham số; nhưng là một cài đặt đã lưu,
        # Sự kết hợp tương tự sẽ không làm gián đoạn quá trình chạy, nó chỉ có nghĩa là vô hiệu hóa nền.
        ui_enabled = _ui_config_value(
            ui_config, "subtitle_background_enabled", bool
        )
        ui_color = _ui_config_value(
            ui_config, "subtitle_background_color", str, _hex_color
        )
        if ui_enabled is False:
            params_kwargs["text_background_color"] = False
            if args.rounded_subtitle_background is None:
                params_kwargs["rounded_subtitle_background"] = False
        elif ui_color is not None:
            params_kwargs["text_background_color"] = ui_color
        elif ui_enabled is True:
            params_kwargs["text_background_color"] = True

    return VideoParams(**params_kwargs)


def _load_batch_manifest(raw_path: str) -> tuple[str, list[dict[str, Any]]]:
    expanded_path = os.path.expanduser(raw_path.strip())
    if not expanded_path:
        raise ValueError("--batch-file path cannot be empty")

    candidate = (
        expanded_path
        if os.path.isabs(expanded_path)
        else os.path.join(os.getcwd(), expanded_path)
    )
    manifest_path = os.path.realpath(candidate)
    if not os.path.isfile(manifest_path):
        raise ValueError(f"batch manifest does not exist or is not a file: {raw_path}")

    with open(manifest_path, "rb") as manifest_file:
        payload = manifest_file.read(_BATCH_FILE_MAX_BYTES + 1)
    if len(payload) > _BATCH_FILE_MAX_BYTES:
        raise ValueError("batch manifest exceeds the 1 MiB limit")

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("batch manifest must be UTF-8 encoded") from exc
    if not text.strip():
        raise ValueError("batch manifest must contain at least one task")

    if text.lstrip().startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON array in batch manifest: {exc.msg}"
            ) from exc
        if not isinstance(parsed, list):
            raise ValueError("batch manifest JSON must be an array")
        entries = parsed
    else:
        entries = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSONL in batch manifest at line {line_number}: {exc.msg}"
                ) from exc

    if not entries:
        raise ValueError("batch manifest must contain at least one task")
    if len(entries) > _BATCH_TASK_MAX_COUNT:
        raise ValueError(
            f"batch manifest contains {len(entries)} tasks; "
            f"the limit is {_BATCH_TASK_MAX_COUNT}"
        )

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"batch task {index} must be a JSON object")
    return manifest_path, entries


def _validate_batch_entry_fields(
    entry: dict[str, Any],
    *,
    index: int,
    allowed_fields: set[str],
) -> None:
    unknown_fields = sorted(set(entry) - allowed_fields)
    if unknown_fields:
        raise ValueError(
            f"batch task {index} contains unknown VideoParams fields: "
            f"{', '.join(unknown_fields)}"
        )

    materials = entry.get("video_materials")
    if not isinstance(materials, list):
        return
    allowed_material_fields = {"provider", "url", "duration", "source_info"}
    for material_index, material in enumerate(materials, start=1):
        if not isinstance(material, dict):
            continue
        unknown_material_fields = sorted(set(material) - allowed_material_fields)
        if unknown_material_fields:
            raise ValueError(
                f"batch task {index} material {material_index} contains unknown "
                f"MaterialInfo fields: {', '.join(unknown_material_fields)}"
            )


def _manifest_relative_path(raw_path: str, manifest_directory: str) -> str:
    expanded_path = os.path.expanduser(raw_path.strip())
    if not expanded_path or os.path.isabs(expanded_path):
        return expanded_path
    return os.path.realpath(os.path.join(manifest_directory, expanded_path))


def _resolve_batch_entry_paths(
    params: VideoParams,
    *,
    override_fields: set[str],
    manifest_directory: str,
) -> None:
    if "custom_audio_file" in override_fields and params.custom_audio_file:
        params.custom_audio_file = _manifest_relative_path(
            params.custom_audio_file,
            manifest_directory,
        )

    if (
        "video_materials" in override_fields
        and params.video_source == "local"
        and params.video_materials
    ):
        for material in params.video_materials:
            material.url = _manifest_relative_path(
                material.url,
                manifest_directory,
            )


def _validate_batch_task_params(
    params: VideoParams,
    *,
    stop_at: str,
    custom_position_is_explicit: bool,
    seedance_charge_confirmed: bool,
    ofox_charge_confirmed: bool,
    metaso_minimax_charge_confirmed: bool,
) -> None:
    if not params.video_subject.strip() and not params.video_script.strip():
        raise ValueError("one of video_subject or video_script is required")

    if params.video_source not in _CLI_VIDEO_SOURCES:
        raise ValueError(
            "video_source must be one of: " + ", ".join(_CLI_VIDEO_SOURCES)
        )
    for field_name, value in (
        ("video_aspect", params.video_aspect),
        ("video_concat_mode", params.video_concat_mode),
    ):
        # These schema fields remain Optional for compatibility with historical
        # API payloads, but the video pipeline always dereferences their enum
        # values. A manifest's explicit null must fail before any batch task starts.
        if value is None:
            raise ValueError(f"{field_name} cannot be null")
    if params.video_source == "local" and stop_at == "terms":
        raise ValueError(
            "stop_at=terms has no effect with video_source=local"
        )
    if (
        params.video_source == "local"
        and stop_at in {"materials", "video"}
        and not params.video_materials
    ):
        raise ValueError(
            "video_materials is required with video_source=local when "
            "stop_at is materials or video"
        )
    if params.video_source != "local" and params.video_materials:
        raise ValueError("video_materials can only be used with video_source=local")
    if (
        params.video_source == "volcengine_seedance"
        and stop_at in {"materials", "video"}
        and not seedance_charge_confirmed
    ):
        raise ValueError(
            "--confirm-seedance-charge is required for Volcano Engine Seedance"
        )
    if (
        params.video_source == "ofox"
        and stop_at in {"materials", "video"}
        and not ofox_charge_confirmed
    ):
        raise ValueError("--confirm-ofox-charge is required for OFox video generation")
    if (
        params.video_source == "metaso_minimax"
        and stop_at in {"materials", "video"}
        and not metaso_minimax_charge_confirmed
    ):
        raise ValueError(
            "--confirm-metaso-minimax-charge is required for Metaso MiniMax H3"
        )

    if stop_at == "subtitle" and not params.subtitle_enabled:
        raise ValueError("stop_at=subtitle cannot be combined with disabled subtitles")
    if params.subtitle_position not in {"top", "center", "bottom", "custom"}:
        raise ValueError(
            "subtitle_position must be one of: top, center, bottom, custom"
        )
    if custom_position_is_explicit and params.subtitle_position != "custom":
        raise ValueError("custom_position requires subtitle_position=custom")
    if not math.isfinite(params.custom_position) or not 0 <= params.custom_position <= 100:
        raise ValueError("custom_position must be a finite number between 0 and 100")
    if params.video_clip_speed is not None and (
        not math.isfinite(params.video_clip_speed)
        or not 0.5 <= params.video_clip_speed <= 2.0
    ):
        raise ValueError("video_clip_speed must be a finite number between 0.5 and 2.0")
    if params.text_background_color is False and params.rounded_subtitle_background:
        raise ValueError(
            "rounded_subtitle_background requires an enabled subtitle background"
        )

    color_values = {
        "text_fore_color": params.text_fore_color,
        "stroke_color": params.stroke_color,
        "text_background_color": (
            params.text_background_color
            if isinstance(params.text_background_color, str)
            else None
        ),
    }
    for name, value in color_values.items():
        if value is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError(f"{name} must use #RRGGBB format")

    numeric_constraints = (
        ("voice_volume", params.voice_volume, 0, False),
        ("voice_rate", params.voice_rate, 0, True),
        ("bgm_volume", params.bgm_volume, 0, False),
        ("stroke_width", params.stroke_width, 0, False),
    )
    for name, value, minimum, exclusive in numeric_constraints:
        if value is None:
            continue
        if not math.isfinite(value) or (
            value <= minimum if exclusive else value < minimum
        ):
            comparison = ">" if exclusive else ">="
            raise ValueError(f"{name} must be a finite number {comparison} {minimum}")

    for name, value in (
        ("n_threads", params.n_threads),
        ("font_size", params.font_size),
    ):
        if value is not None and value < 1:
            raise ValueError(f"{name} must be >= 1")

    if params.bgm_type not in {"", "random", "custom", "sonilo"}:
        raise ValueError("bgm_type must be one of: none, random, custom, sonilo")
    if params.bgm_file and params.bgm_type != "custom":
        raise ValueError("bgm_file requires bgm_type=custom")
    if params.sonilo_bgm_prompt and params.bgm_type != "sonilo":
        raise ValueError("sonilo_bgm_prompt requires bgm_type=sonilo")


def _build_batch_tasks(args: argparse.Namespace) -> list[VideoParams]:
    from app.models.schema import VideoParams

    manifest_path, entries = _load_batch_manifest(args.batch_file)
    manifest_directory = os.path.dirname(manifest_path)
    base_params = build_video_params(args)
    allowed_fields = set(VideoParams.model_fields)
    base_values = {
        field_name: getattr(base_params, field_name) for field_name in allowed_fields
    }
    tasks: list[VideoParams] = []

    for index, entry in enumerate(entries, start=1):
        _validate_batch_entry_fields(
            entry,
            index=index,
            allowed_fields=allowed_fields,
        )
        try:
            params = VideoParams.model_validate(
                {**copy.deepcopy(base_values), **entry}
            )
            override_fields = set(entry)
            _resolve_batch_entry_paths(
                params,
                override_fields=override_fields,
                manifest_directory=manifest_directory,
            )
            _validate_batch_task_params(
                params,
                stop_at=args.stop_at,
                custom_position_is_explicit=(
                    args.custom_position is not None
                    or "custom_position" in override_fields
                ),
                seedance_charge_confirmed=args.confirm_seedance_charge,
                ofox_charge_confirmed=args.confirm_ofox_charge,
                metaso_minimax_charge_confirmed=(
                    args.confirm_metaso_minimax_charge
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid batch task {index}: {exc}") from exc
        tasks.append(params)

    validation_plans = []
    for index, params in enumerate(tasks, start=1):
        try:
            validation_plans.append(_validate_cli_files(params, stop_at=args.stop_at))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid batch task {index}: {exc}") from exc

    prepared_paths: dict[str, str] = {}
    created_paths: list[str] = []
    try:
        for index, (local_videos_dir, resolved_materials) in enumerate(
            validation_plans, start=1
        ):
            try:
                _prepare_cli_materials(
                    local_videos_dir,
                    resolved_materials,
                    prepared_paths=prepared_paths,
                    created_paths=created_paths,
                )
            except OSError as exc:
                raise ValueError(f"invalid batch task {index}: {exc}") from exc
    except Exception:
        _remove_cli_material_copies(created_paths)
        raise
    return tasks


def _resolve_cli_file(
    raw_path: str,
    *,
    description: str,
    fallback_dir: str | None = None,
) -> str:
    """
    Giải quyết các tham số tệp CLI thành các đường dẫn tuyệt đối dựa trên thư mục làm việc hiện tại,
    và xác nhận sự tồn tại của nó trước khi nhiệm vụ bắt đầu.

    Các phiên bản cũ hơn của cảnh quay cục bộ luôn được giải quyết liên quan đến ``storage/local_videos``. Để tương thích với các tập lệnh hiện có,
    Cho phép bạn quay lại thư mục hiện tại khi không tìm thấy đường dẫn tương đối; đường dẫn tuyệt đối luôn được người dùng nhập vào.
    Phân tích trực tiếp.
    """
    expanded_path = os.path.expanduser(raw_path.strip())
    if not expanded_path:
        raise ValueError(f"{description} path cannot be empty")

    candidate = (
        expanded_path
        if os.path.isabs(expanded_path)
        else os.path.join(os.getcwd(), expanded_path)
    )
    resolved_path = os.path.realpath(candidate)
    if not os.path.isfile(resolved_path) and fallback_dir and not os.path.isabs(expanded_path):
        resolved_path = os.path.realpath(os.path.join(fallback_dir, expanded_path))

    if not os.path.isfile(resolved_path):
        raise ValueError(f"{description} file does not exist: {raw_path}")
    return resolved_path


def _path_is_within_directory(file_path: str, directory: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(directory), os.path.realpath(file_path)]
        ) == os.path.realpath(directory)
    except ValueError:
        # Không thể tính toán đường dẫn chung cho các ký tự ổ đĩa Windows khác nhau và tệp này rõ ràng không có trong thư mục đích vào thời điểm này.
        return False


def _resolve_managed_resource_file(
    raw_path: str,
    *,
    resource_dir: str,
    description: str,
) -> str:
    """Phân tích các tệp tài nguyên dự án và đảm bảo rằng các đường dẫn tuyệt đối vẫn nằm trong các thư mục tài nguyên tương ứng."""
    from app.utils import utils

    expanded_path = os.path.expanduser(raw_path.strip())
    candidates = (
        [expanded_path]
        if os.path.isabs(expanded_path)
        else [
            os.path.join(resource_dir, expanded_path),
            os.path.join(utils.root_dir(), expanded_path),
        ]
    )
    for candidate in candidates:
        resolved_path = os.path.realpath(candidate)
        if os.path.isfile(resolved_path) and _path_is_within_directory(
            resolved_path, resource_dir
        ):
            return resolved_path
    raise ValueError(
        f"{description} file must exist inside {resource_dir}: {raw_path}"
    )


def _validate_cli_files(
    params: VideoParams, stop_at: str
) -> tuple[str, list[tuple[MaterialInfo, str, str]]]:
    """
    Phân tích cú pháp và xác minh các tệp CLI mà không có tác dụng phụ, tránh việc để lại bản sao tài liệu hàng loạt.

    Âm thanh, BGM và phông chữ tùy chỉnh được chuẩn hóa thành đường dẫn hoặc tên mà lớp dịch vụ có thể sử dụng; tài sản địa phương
    Chỉ các nguồn và tiện ích mở rộng mới được phân tích cú pháp, thực sự được sao chép bởi ``_prepare_cli_materials`` trong tất cả các mục nhập hàng loạt
    Sau khi vượt qua xác minh, nó sẽ được hoàn thành thống nhất.
    """
    from app.models import const
    from app.services import bgm as bgm_service
    from app.utils import utils

    # FFmpeg phát hiện đường dẫn tác vụ được chia sẻ (task.start) đã được chuyển đến app/services/task.py
    # Thực hiện kiểm tra cứng thống nhất tại đây: lỗi phát hiện sẽ khiến tác vụ kết thúc với lỗi ở giai đoạn trước, run_cli()
    # Mã thoát khác 0 sẽ được trả về tương ứng. Ở đây chúng tôi sẽ không lặp lại kiểm tra không chặn quy trình để tránh xung đột với đường ống.
    # Kết quả xét xử không nhất quán.

    local_material_extensions = {
        *(f".{extension}" for extension in const.FILE_TYPE_VIDEOS),
        *(f".{extension}" for extension in const.FILE_TYPE_IMAGES),
        ".avi",
        ".flv",
    }

    if params.custom_audio_file:
        params.custom_audio_file = _resolve_cli_file(
            params.custom_audio_file,
            description="custom audio",
        )
        audio_extension = os.path.splitext(params.custom_audio_file)[1].lower()
        if audio_extension not in _CUSTOM_AUDIO_EXTENSIONS:
            allowed = ", ".join(sorted(_CUSTOM_AUDIO_EXTENSIONS))
            raise ValueError(
                f"unsupported custom audio type {audio_extension or '<none>'}; "
                f"allowed extensions: {allowed}"
            )

    if params.bgm_type == "custom":
        if not bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
            # Ở mức âm lượng 0, luồng xuống sẽ bỏ qua tất cả BGM một cách đồng đều. Xóa tham số file ở đây cùng lúc để tránh
            # CLI thực hiện phân giải đường dẫn, kiểm tra sự tồn tại hoặc định dạng cho một tệp sẽ không được đọc
            # kiểm tra.
            params.bgm_file = ""
        elif not params.bgm_file:
            # Việc thiếu tệp có cấu thành lỗi hay không tùy thuộc vào công tắc BGM chung và không thể sử dụng trong giai đoạn argparse
            # Chặn vô điều kiện, nếu không ``custom + 0%`` sẽ không nhất quán với hành vi của WebUI và lớp dịch vụ.
            raise ValueError("--bgm-file is required when --bgm-type is custom")
        else:
            try:
                # CLI, WebUI và các dịch vụ tác vụ phải có chung ranh giới tệp BGM. Trực tiếp tại đây
                # Tái sử dụng phân tích lớp dịch vụ, hỗ trợ cả thư mục tải lên của người dùng và thư mục bài hát tích hợp, cũng như
                # Tự động kế thừa các định dạng âm thanh mới và quy tắc bảo mật đường dẫn để tránh nhiều lối vào.
                # Duy trì danh sách trắng.
                params.bgm_file = bgm_service.resolve_bgm_file(params.bgm_file)
            except ValueError as exc:
                supported_extensions = ", ".join(
                    bgm_service.SUPPORTED_BGM_EXTENSIONS
                )
                raise ValueError(
                    "background music must be a supported audio file inside "
                    f"storage/bgm or resource/songs ({supported_extensions}): "
                    f"{params.bgm_file}"
                ) from exc

    if params.subtitle_enabled and params.font_name and stop_at == "video":
        font_path = _resolve_managed_resource_file(
            params.font_name,
            resource_dir=utils.font_dir(),
            description="subtitle font",
        )
        if not font_path.lower().endswith((".ttf", ".ttc")):
            raise ValueError("subtitle font must use the .ttf or .ttc extension")
        # Hạ lưu nối đường dẫn dựa trên tên tệp trong tài nguyên/phông chữ, do đó tên tệp đơn giản vẫn được giữ lại.
        params.font_name = os.path.basename(font_path)

    if params.video_source != "local" or stop_at not in {"materials", "video"}:
        return "", []

    local_videos_dir = utils.storage_dir("local_videos")
    resolved_materials: list[tuple[MaterialInfo, str, str]] = []
    for material in params.video_materials or []:
        source_path = _resolve_cli_file(
            material.url,
            description="local material",
            fallback_dir=local_videos_dir,
        )
        extension = os.path.splitext(source_path)[1].lower()
        if extension not in local_material_extensions:
            allowed = ", ".join(sorted(local_material_extensions))
            raise ValueError(
                f"unsupported local material type {extension or '<none>'}: "
                f"{material.url}; allowed extensions: {allowed}"
            )
        resolved_materials.append((material, source_path, extension))

    return local_videos_dir, resolved_materials


def _remove_cli_material_copies(created_paths: Sequence[str]) -> None:
    """Best-effort cleanup for managed copies created by one CLI preparation."""
    for file_path in reversed(created_paths):
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except OSError as exc:
            logger.warning(
                f"failed to remove prepared CLI material: path={file_path}, "
                f"error={exc}"
            )


def _prepare_cli_materials(
    local_videos_dir: str,
    resolved_materials: Sequence[tuple[MaterialInfo, str, str]],
    *,
    prepared_paths: dict[str, str] | None = None,
    created_paths: list[str] | None = None,
) -> None:
    """Copy validated local materials once and update every matching reference."""
    if not resolved_materials:
        return

    os.makedirs(local_videos_dir, exist_ok=True)
    if prepared_paths is None:
        prepared_paths = {}
    if created_paths is None:
        created_paths = []

    for material, source_path, extension in resolved_materials:
        prepared_path = prepared_paths.get(source_path)
        if prepared_path is None:
            if _path_is_within_directory(source_path, local_videos_dir):
                prepared_path = source_path
            else:
                prepared_path = os.path.join(
                    local_videos_dir,
                    f"cli-material-{uuid4().hex}{extension}",
                )
                try:
                    shutil.copy2(source_path, prepared_path)
                except OSError:
                    try:
                        if os.path.exists(prepared_path):
                            os.remove(prepared_path)
                    except OSError:
                        pass
                    raise
                created_paths.append(prepared_path)
                logger.info(
                    "copied CLI local material into managed storage: "
                    f"source={source_path}, target={prepared_path}"
                )
            prepared_paths[source_path] = prepared_path

        material.url = prepared_path


def prepare_cli_files(params: VideoParams, stop_at: str) -> None:
    """Validate and prepare files for one trusted local CLI task."""
    local_videos_dir, resolved_materials = _validate_cli_files(params, stop_at)
    created_paths: list[str] = []
    try:
        _prepare_cli_materials(
            local_videos_dir,
            resolved_materials,
            created_paths=created_paths,
        )
    except Exception:
        _remove_cli_material_copies(created_paths)
        raise


def _run_batch_tasks(args: argparse.Namespace, tasks: list[VideoParams]) -> int:
    from app.services import task as tm
    from app.utils import utils

    task_summaries: list[dict[str, Any]] = []
    used_task_ids: set[str] = set()
    failed_count = 0

    for index, params in enumerate(tasks, start=1):
        task_id = utils.get_uuid()
        if task_id in used_task_ids:
            task_id = str(uuid4())
        used_task_ids.add(task_id)
        logger.info(
            f"start CLI batch task: index={index}, task_id={task_id}, "
            f"stop_at={args.stop_at}"
        )

        result = None
        failed_stage = None
        error = None
        try:
            result = tm.start(
                task_id=task_id,
                params=params,
                stop_at=args.stop_at,
                allow_server_file_input=True,
            )
        except Exception as exc:
            failed_stage = "runtime"
            error = str(exc)
            logger.exception(
                "CLI batch task failed with an unexpected error: "
                f"index={index}, task_id={task_id}, error={exc}"
            )
        else:
            if not isinstance(result, dict) or not result:
                failed_stage = "unknown"
                error = "empty or invalid task result"
            elif result.get("state") == tm.const.TASK_STATE_FAILED:
                failed_stage = result.get("failed_stage") or "unknown"
                error = result.get("error") or "unknown task error"

            if error is not None:
                logger.error(
                    f"CLI batch task failed: index={index}, task_id={task_id}, "
                    f"stop_at={args.stop_at}, stage={failed_stage}, error={error}"
                )

        status = "failed" if error is not None else "succeeded"
        if status == "failed":
            failed_count += 1
        task_summaries.append(
            {
                "index": index,
                "task_id": task_id,
                "status": status,
                "result": result,
                "failed_stage": failed_stage,
                "error": error,
            }
        )

    print(
        json.dumps(
            {
                "total": len(task_summaries),
                "succeeded": len(task_summaries) - failed_count,
                "failed": failed_count,
                "tasks": task_summaries,
            },
            ensure_ascii=False,
        )
    )
    return 1 if failed_count else 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.batch_file:
        try:
            tasks = _build_batch_tasks(args)
        except (ValueError, OSError) as exc:
            logger.error(f"invalid CLI batch input: {exc}")
            return 2
        return _run_batch_tasks(args, tasks)

    try:
        params = build_video_params(args)
        prepare_cli_files(params, stop_at=args.stop_at)
    except (ValueError, OSError) as exc:
        logger.error(f"invalid CLI input: {exc}")
        return 2

    # Các đối số trợ giúp được thoát trực tiếp trong pars_args. Trì hoãn việc nhập khẩu dịch vụ kinh doanh tại đây,
    # Đảm bảo rằng đầu ra -h/--help sạch mà không thay đổi quá trình khởi tạo của tác vụ thực tế.
    from app.services import task as tm
    from app.utils import utils

    task_id = args.task_id or utils.get_uuid()
    logger.info(f"start CLI task: task_id={task_id}, stop_at={args.stop_at}")
    try:
        result = tm.start(
            task_id=task_id,
            params=params,
            stop_at=args.stop_at,
            # CLI inputs come from the local operator rather than an HTTP client.
            # Preserve support for arbitrary local audio paths without weakening the
            # task service's secure default for API and WebUI callers.
            allow_server_file_input=True,
        )
    except Exception as exc:
        logger.exception(
            f"CLI task failed with an unexpected error: task_id={task_id}, error={exc}"
        )
        return 1
    if not result or result.get("state") == tm.const.TASK_STATE_FAILED:
        failed_stage = result.get("failed_stage", "unknown") if result else "unknown"
        error = result.get("error", "unknown task error") if result else "empty result"
        logger.error(
            f"CLI task failed: task_id={task_id}, stop_at={args.stop_at}, "
            f"stage={failed_stage}, error={error}"
        )
        return 1

    print(json.dumps({"task_id": task_id, "result": result}, ensure_ascii=False))
    return 0


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 before anything is printed.

    Windows consoles default to a legacy code page (cp1252 in Western
    Europe). Generating a French video produces U+202F, the narrow no-break
    space French typography puts before ':' and '!', and Loguru's own progress
    lines carry circled digits. Either one raises UnicodeEncodeError, which
    kills the process *after* the video was written successfully -- so the run
    reports failure and never prints where the file is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


if __name__ == "__main__":
    _force_utf8_console()
    raise SystemExit(run_cli())
