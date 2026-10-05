import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger
from streamlit_tour import Tour

# Khi WebUI được chạy như một cổng thông tin độc lập, thư mục gốc của dự án cần được ưu tiên hơn các phần phụ thuộc của bên thứ ba.
# Ngăn chặn gói ứng dụng có cùng tên trong phần phụ thuộc che khuất gói ứng dụng của chính MoneyPrinterTurbo.
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services import (
    bridge,
    cache_manager,
    llm,
    loomloom,
    material,
    metaso_minimax,
    ofox,
    video,
    volcengine_seedance,
    voice,
    webui_task,
)
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.services import state as sm
from app.services import task as tm
from app.services import version_checker
from app.utils.logging_utils import configure_terminal_logger
from app.utils import utils

PROJECT_NAME = str(getattr(config, "project_name", "") or "TNTC Studio").strip()

st.set_page_config(
    page_title=PROJECT_NAME,
    page_icon=":material/videocam:",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": f"# {PROJECT_NAME}\nTrợ lý sáng tạo video ngắn thông minh cho YouTube và TikTok.",
    },
)


# Streamlit 1.59 sẽ hiển thị các lối vào nền tảng như Triển khai và thúc đẩy kỹ năng theo mặc định ở góc trên bên phải của trang.
# MoneyPrinterTurbo là một công cụ gốc dành cho người dùng cuối, những mục này sẽ để lại một khoảng trống lớn ở trên cùng,
# Nó cũng có thể khiến người dùng mới nhầm lẫn khi nghĩ rằng họ cần cài đặt các thành phần bổ sung. Thanh công cụ nền tảng Streamlit được ẩn thống nhất ở đây.
# Và nén không gian trên cùng của vùng chứa chính để chỉ để lại tiêu đề, lựa chọn ngôn ngữ và khu vực cài đặt doanh nghiệp của dự án.
style_file = Path(__file__).with_name("styles.css")
streamlit_style = f"<style>{style_file.read_text(encoding='utf-8')}</style>"
st.markdown(streamlit_style, unsafe_allow_html=True)
# Xác định thư mục tài nguyên
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
# Danh sách ngôn ngữ phải có sẵn trước khi khởi tạo trạng thái phiên để có thể ánh xạ ngôn ngữ trình duyệt tới
# Ngôn ngữ thực sự được dự án hỗ trợ; kết quả nhận dạng tự động chỉ nhập phiên hiện tại và không sửa đổi cấu hình chung.
locales = utils.load_locales(i18n_dir)
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
DEFAULT_KOKORO_BASE_URL = "http://127.0.0.1:8880/v1"
DEFAULT_KOKORO_MODEL = "kokoro"
# empty = ask the server for its voice list (GET {base_url}/audio/voices)
DEFAULT_KOKORO_VOICES: list[str] = []
ONBOARDING_TOUR_KEY = "mpt-onboarding-v1"
CUSTOM_LLM_ENDPOINT_ID = "custom"
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
LOOMLOOM_MAX_POLL_FAILURES = 5
# WebUI hiển thị các nguồn video được nhóm theo khả năng của vật liệu nhưng giá trị video_source ban đầu vẫn được giữ lại ở lớp dưới cùng.
# Nhóm video AI và trang cài đặt có chung trình tự kinh doanh: ưu tiên cho các nhà cung cấp dịch vụ hợp tác và theo Secret Tower, OOX,
# Đám mây tỷ lệ cược và động cơ núi lửa được sắp xếp; các dịch vụ còn lại sẽ được hiển thị sau. Bằng cách này, thứ tự của hai lối vào là nhất quán, đồng thời
# Ngữ nghĩa của trường trong config.toml, các tác vụ lịch sử và yêu cầu API không bị thay đổi và người dùng cũ không cần phải di chuyển cấu hình của họ.
VIDEO_SOURCE_GROUPS = {
    "stock_video": ("pexels", "pixabay", "coverr"),
    "ai_video": (
        "metaso_minimax",
        "ofox",
        "loomloom",
        "volcengine_seedance",
        "wavespeed",
    ),
    "ai_image": ("openai_image",),
    "local": ("local",),
}
# Khóa API của Bài đăng tải lên và người dùng xuất bản được quản lý tương ứng trên hai trang và tên người dùng xuất bản cũng được quản lý.
# Nó không giống như email đăng nhập. Việc bảo trì cổng thông tin tập trung có thể tránh được những sai lệch trong quá trình sao chép đa ngôn ngữ sau khi mã hóa các URL.
# Nó cũng tạo điều kiện cho người dùng hoàn tất cấu hình lần đầu và bảo trì tài khoản tiếp theo trực tiếp từ WebUI.
UPLOAD_POST_API_KEYS_URL = "https://app.upload-post.com/api-keys"
UPLOAD_POST_MANAGE_USERS_URL = "https://app.upload-post.com/manage-users"
# Cài đặt nội dung và mô tả nguồn video chia sẻ lối vào quảng cáo để tránh sự mâu thuẫn về thông số liên kết của hai vị trí.
OFOX_REFERRAL_URL = (
    "https://ofox.ai/?utm_source=github"
    "&utm_medium=sponsorship&utm_content=moneyprinterturbo"
)
# "Mặc định" là một trọng điểm dành riêng cho WebUI sẽ không được ghi vào config.toml hoặc chuyển tới FFmpeg.
# Phần phụ trợ tiếp tục sử dụng libx264 ổn định khi video_codec không được định cấu hình; để yên người lính gác này có thể phân biệt được
# "Tuân theo chính sách mặc định của dự án" và "Người dùng khắc phục rõ ràng libx264" để tạo điều kiện thuận lợi cho việc điều chỉnh bảo mật trong tương lai đối với chính sách mặc định.
DEFAULT_VIDEO_CODEC_OPTION = "__default__"
# Giao diện khả năng của LoomLoom chỉ trả về ID mẫu và tên hiển thị chứ không cung cấp giá. Chỉ người dùng bảo trì được xác nhận ở đây
# Giá tham chiếu được sử dụng để giúp lựa chọn mẫu mã; chi phí cuối cùng được giải quyết dựa trên cuộc gọi mô hình thực tế. Bảo hiểm bí danh cùng một lúc
# Tên hiển thị hiện tại và ID mẫu phổ biến. Các mẫu mới không được đưa vào sẽ đương nhiên trả về giá trống, điều này không ảnh hưởng đến việc lựa chọn hoặc báo giá.
LOOMLOOM_VIDEO_MODEL_PRICES = (
    (("veo31fast", "googleveo31fastpreview"), "￥0.700/秒", "￥0.700/秒"),
    (
        (
            "通义万相22图生视频fastlora",
            "通义万相22文生视频fastlora",
            "tongyiwanxiang22i2vfastlora",
            "tongyiwanxiang22t2vfastlora",
            "wanx22i2vfastlora",
            "wanx22t2vfastlora",
        ),
        "￥0.350–0.770/条",
        "￥0.350/条（480P）；￥0.770/条（720P）",
    ),
    (("即梦30文生视频720p", "jimeng30t2v720p"), "￥0.230/秒", "￥0.230/秒"),
    (("即梦30pro视频", "jimeng30pro视频", "jimeng30provideo"), "￥1.000/秒", "￥1.000/秒"),
    (("veo3", "googleveo3"), "￥1.400/秒", "￥1.400/秒"),
    (("veo31", "googleveo31"), "￥1.400/秒", "￥1.400/秒"),
    (
        ("klingv2", "可灵v2"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("klingv21master", "可灵v21master"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("viduq3pro",),
        "￥0.440–1.000/秒",
        "￥0.440/秒（540P）；￥0.940/秒（720P）；￥1.000/秒（1080P）",
    ),
)
DEFAULT_SUBTITLE_SETTINGS = {
    "subtitle_enabled": True,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "subtitle_position": "bottom",
    "subtitle_display_mode": "sentence",
    "subtitle_animation": "none",
    "custom_position": 70.0,
    "text_fore_color": "#FFFFFF",
    "font_size": 60,
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "subtitle_background_enabled": False,
    "subtitle_background_color": "#000000",
    "rounded_subtitle_background": False,
}
LOCAL_MATERIAL_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
}
CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    # Win32 cũng nhận dạng các số chỉ số trên Latin-1 ¹, 2, ³ là số thiết bị. Mặc dù loại đề tài này
    # Trường hợp này hiếm gặp nhưng vẫn có thể khiến quá trình tải xuống Windows không thành công, do đó, nó được xử lý giống như các tên dành riêng bằng số thông thường.
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in ("¹", "²", "³")
    }
)
_RUNTIME_CONFIG_SECTIONS = {
    "app": config.app,
    "azure": config.azure,
    "chatterbox": config.chatterbox,
    "kokoro": config.kokoro,
    "elevenlabs": config.elevenlabs,
    "minimax_tts": config.minimax_tts,
    "siliconflow": config.siliconflow,
    "fish_audio": config.fish_audio,
    "ui": config.ui,
}
# Cài đặt trước và sao lưu khóa sử dụng các mã định danh tệp riêng biệt. Khi nhập, trước tiên hãy xác minh lược đồ và phiên bản.
# Tránh nhầm lẫn các bản ghi tác vụ, config.toml hoặc JSON khác với các tệp xuất hàm chi phí.
SETTINGS_PRESET_SCHEMA = "moneyprinterturbo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "moneyprinterturbo-settings.json"
KEY_BACKUP_SCHEMA = "moneyprinterturbo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "moneyprinterturbo-keys.json"
# Các cài đặt trước chỉ mô tả các thông số xây dựng. Chất liệu, lồng tiếng và nhạc phim đều là các đường dẫn tệp cục bộ và các cài đặt trước thường cần phải có trên một máy tính khác.
# 机器或另一个容器里导入，带上这些路径只会指向不存在的文件。
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_materials",
        "custom_audio_file",
        "bgm_file",
    }
)
# Các khóa được xác định bằng hậu tố tên mục cấu hình. Miễn là Nhà cung cấp mới tiếp tục được đặt tên, nó sẽ được nhập tự động.
# Sao lưu, không cần duy trì danh sách khóa thứ hai.
CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "api_keys",
    "api_token",
    "access_key",
    "secret_key",
    "speech_key",
)
# Khi bạn chỉ khôi phục khóa mà không khôi phục các mục cấu hình đi kèm, thông tin xác thực vẫn không khả dụng. Các mục đồng hành này được sao lưu cùng với khóa.
CREDENTIAL_COMPANION_KEYS = {
    # Azure 语音必须同时知道区域。
    "azure": ("speech_region",),
    # Các trường bổ sung của nhà cung cấp được Cơ quan đăng ký khai báo, chẳng hạn như Cloudflare AI Gateway
    # ID tài khoản và ID cổng. Khi chỉ khôi phục API Key mà mất các trường này thì chuyển sang trường khác
    # Nhà cung cấp vẫn không thể gọi được sau khi máy được cài đặt. Đọc từ Sổ đăng ký cho phép bổ sung trong tương lai
    # Nhà cung cấp tự động chuyển sang trạng thái sao lưu và không cần duy trì danh sách trường thứ hai tại đây.
    "app": tuple(
        provider.config_key(field.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for field in provider.extra_fields
    ),
}

NON_LLM_COMPANION_KEYS = {
    "app": ("upload_post_username",)
}
# Cùng một phím có thể sử dụng các phím điều khiển riêng biệt ở các bảng khác nhau: bảng âm thanh chỉnh sửa trực tiếp Gemini và
# MiMo 的 LLM 密钥。恢复备份时必须清除每一个别名，否则遗留的旧值
# Khóa mới được khôi phục sẽ bị ghi đè trong lần chạy lại tiếp theo.
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("app", "gemini_api_key"): ("gemini_tts_api_key_input",),
    ("app", "mimo_api_key"): ("mimo_tts_api_key_input",),
}
# Phân vùng ui chỉ lưu các tùy chọn giao diện, không chứa bất kỳ thông tin xác thực nào và bị bỏ qua hoàn toàn trong quá trình sao lưu.
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})


# -----------------------------------------------------------------------------
# Khởi chạy cấu hình, trạng thái phiên và bản địa hóa
# -----------------------------------------------------------------------------


def _set_runtime_config(section_name, key, value):
    """
    Cập nhật cấu hình WebUI nhưng không chờ tác vụ nền đang tạo video.

    Trước khi tác vụ nền kết thúc, lớp cấu hình chỉ giữ lại giá trị mới nhất của cùng mục cấu hình; khi tác vụ giải phóng khóa cấu hình, nó sẽ tự động
    Áp dụng và lưu. Các giá trị điều khiển trang vẫn được Streamlit session_state duy trì nên
    chạy lại sẽ không thiết lập lại những gì người dùng vừa nhập vào cấu hình cũ.
    """
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    updated = config.update_config_nonblocking(config_section, key, value)
    if not updated:
        logger.debug(f"deferred WebUI config update: section={section_name}, key={key}")
    return updated


def _delete_runtime_config(section_name, key):
    """Xóa các mục cấu hình WebUI; các tác vụ nền sẽ được thực thi sau khoảng thời gian trễ đã định cấu hình."""
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    deleted = config.delete_config_nonblocking(config_section, key)
    if not deleted:
        logger.debug(f"deferred WebUI config delete: section={section_name}, key={key}")
    return deleted


def _save_runtime_config():
    """Yêu cầu lưu cấu hình WebUI; trả về ngay lập tức khi tác vụ nền chiếm cấu hình."""
    saved = config.try_save_config()
    if not saved:
        logger.debug("deferred WebUI config save until active task completes")
    return saved


def _saved_ui_choice(key, options, default):
    """Đọc một lựa chọn liên tục và hạ cấp cấu hình cũ hoặc các giá trị không hợp lệ được chỉnh sửa thủ công thành giá trị mặc định."""
    options = list(options)
    saved = config.ui.get(key, default)
    numeric_default = isinstance(default, (int, float)) and not isinstance(
        default, bool
    )
    # bool là một lớp con của int, ``True == 1``. Viết thủ công các tùy chọn số dưới dạng TOML
    # Các giá trị Boolean phải bị từ chối và không thể ngụy trang thành tùy chọn số đầu tiên.
    if numeric_default and isinstance(saved, bool):
        return default
    for option in options:
        if saved == option:
            # Trả về giá trị thực trong các tùy chọn và nhân tiện, giá trị tương đương TOML 1.0 được chuẩn hóa thành
            # Tùy chọn số nguyên 1 để tránh các loại tham số xuôi dòng bị trôi khi ghi cấu hình.
            return option

    # TOML 中的数值通常保留原类型；仍兼容用户手工写成字符串的情况。
    if numeric_default and isinstance(saved, str):
        try:
            converted = type(default)(saved)
        except (TypeError, ValueError):
            converted = None
        for option in options:
            if converted == option:
                return option
    return default


def _saved_ui_number(key, default, minimum, maximum, number_type=float):
    """Đọc và giới hạn các giá trị liên tục để ngăn cấu hình bất hợp pháp làm hỏng thanh trượt Streamlit."""
    try:
        saved = config.ui.get(key, default)
        if isinstance(saved, bool):
            raise ValueError("boolean is not a numeric setting")
        value = number_type(saved)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _saved_ui_bool(key, default):
    """兼容 TOML 布尔值和常见手工字符串，拒绝含义不明的旧值。"""
    value = config.ui.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _saved_ui_color(key, default):
    """Chỉ các màu thập lục phân sáu chữ số tiêu chuẩn mới được chuyển tới bộ chọn màu Streamlit."""
    value = str(config.ui.get(key, default) or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return default


def _saved_ui_text(key, default="", max_length=None):
    """Đọc văn bản liên tục và tôn trọng giới hạn độ dài tối đa của điều khiển WebUI tương ứng."""
    value = str(config.ui.get(key, default) or default)
    if max_length is not None:
        value = value[:max_length]
    return value


def _run_llm_read_operation(operation_name, operation):
    """
    Sử dụng cấu hình LLM hiện tại ổn định để thực hiện các yêu cầu chỉ đọc và tránh phải chờ tác vụ tạo video.

    Khi có thể lấy được khóa cấu hình ngay lập tức, biện pháp bảo vệ loại trừ lẫn nhau ban đầu sẽ tiếp tục được sử dụng; khi tác vụ video nền đã giữ khóa,
    Cấu hình chung sẽ không thay đổi cho đến khi kết thúc tác vụ, do đó cấu hình hiện tại có thể được sao chép một cách an toàn và phủ trang
    Nhà cung cấp, mẫu mã và chìa khóa chưa được giao. Bằng cách này, người viết quảng cáo mới sử dụng các tùy chọn mới nhất trong giao diện, đồng thời
    Không thay đổi tác vụ video đang được tạo.
    """
    with config.try_runtime_config_lock() as lock_acquired:
        # Lớp cấu hình giữ khóa hàng đợi trong quá trình sao chép các giá trị chung và xếp chồng các giá trị cần cập nhật, do đó ảnh chụp nhanh chỉ có thể nhìn thấy
        # Hoàn thành trạng thái trước hoặc sau khi cập nhật mà không trộn lẫn hai bộ thông số Nhà cung cấp.
        app_config_snapshot = config.snapshot_config_with_pending(config.app)
        if lock_acquired:
            return operation(app_config_snapshot)

    logger.info(
        f"run read-only LLM operation with active task configuration: "
        f"operation={operation_name}"
    )
    return operation(app_config_snapshot)


def _parse_chatterbox_voices(voices):
    # Chatterbox là một dịch vụ tự lưu trữ và danh sách bản vá được người dùng nhập thủ công vào WebUI.
    # Điều này tương thích thống nhất với các chuỗi được phân tách bằng dấu phẩy trong mảng TOML và hộp nhập liệu để tránh các hộp thả xuống,
    # Nút thử giọng và quy trình tạo tiếp theo sử dụng các định dạng khác nhau dẫn đến trạng thái không nhất quán.
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Nút của Streamlit sẽ kích hoạt chạy lại toàn bộ trang và hộp nhập cấu hình Chatterbox được đặt
    # Sau nút "Nghe tổng hợp giọng nói". Nếu bạn chỉ đọc config.chatterbox trong buổi thử giọng, bạn có thể không lấy được nó.
    # base_url/model/voices mà người dùng vừa điền vào hộp nhập. Đồng bộ hóa lần đầu tiên từ session_state,
    # Có thể đảm bảo rằng logic nút và logic hiển thị hộp đầu vào sử dụng cùng cấu hình mới nhất.
    _set_runtime_config(
        "chatterbox",
        "base_url",
        (
            st.session_state.get(
                "chatterbox_base_url_input",
                config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "api_key",
        st.session_state.get(
            "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "chatterbox",
        "model_id",
        (
            st.session_state.get(
                "chatterbox_model_input",
                config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
            )
            or DEFAULT_CHATTERBOX_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "chatterbox_voices_input",
                config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
            )
        ),
    )


def _sync_kokoro_config_from_session_state():
    # Danh mục âm thanh được hiển thị trước khi cài đặt hộp đầu vào và trạng thái trình duyệt được đồng bộ hóa trước tiên để đảm bảo rằng nó được sử dụng trong lần chạy lại này.
    # Điểm cuối mới và cấu hình âm thanh thủ công giúp loại bỏ nhu cầu sử dụng lại các điều khiển.
    _set_runtime_config(
        "kokoro",
        "base_url",
        (
            st.session_state.get(
                "kokoro_base_url_input",
                config.kokoro.get("base_url") or DEFAULT_KOKORO_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "kokoro",
        "api_key",
        st.session_state.get(
            "kokoro_api_key_input", config.kokoro.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "kokoro",
        "model_id",
        (
            st.session_state.get(
                "kokoro_model_input",
                config.kokoro.get("model_id") or DEFAULT_KOKORO_MODEL,
            )
            or DEFAULT_KOKORO_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "kokoro",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "kokoro_voices_input",
                config.kokoro.get("voices") or DEFAULT_KOKORO_VOICES,
            )
        ),
    )


def _get_kokoro_voice_options(saved_voice_name: str) -> list[str]:
    """Thư mục từ xa được lưu vào bộ đệm trong phiên và lựa chọn cuối cùng được giữ lại khi bị ngắt kết nối và lỗi không được coi là do người dùng thay đổi âm báo."""
    if config.kokoro.get("voices"):
        return voice.get_kokoro_voices()

    # 仅保留当前服务的一条缓存。 Kiểm tra lại ngay sau khi thay đổi điểm cuối/thông tin xác thực và bộ đệm sẽ không lưu Khóa văn bản rõ ràng;
    # Các thao tác UI khác trong vòng 30 giây không liên tục bị chặn trong 5 giây chờ dịch vụ được biết là ngoại tuyến.
    signature = (
        (config.kokoro.get("base_url") or "").strip().rstrip("/"),
        _credential_signature(config.kokoro.get("api_key", "")),
    )
    catalog = st.session_state.get("kokoro_voice_catalog", {})
    if catalog.get("signature") != signature:
        catalog = {"signature": signature, "voices": [], "checked_at": None}
    now = time.monotonic()
    if catalog["checked_at"] is None or now - catalog["checked_at"] >= 30:
        fetched = voice.get_kokoro_voices(fallback=False)
        catalog.update(checked_at=now, available=bool(fetched))
        if fetched:
            catalog["voices"] = fetched
        st.session_state["kokoro_voice_catalog"] = catalog

    options = list(catalog["voices"])
    if not catalog["available"]:
        st.warning(tr("Kokoro Voices Unavailable"))
        # Có thể không được lưu vào bộ nhớ đệm khi mở lần đầu, vẫn giữ lại lựa chọn thực trong hồ sơ; sau khi nối lại kết nối
        # 只有成功返回的新目录才能判定某个旧音色确实已被服务器删除。
        if voice.is_kokoro_voice(saved_voice_name) and saved_voice_name not in options:
            options.insert(0, saved_voice_name)
    return options or [f"kokoro:{voice.KOKORO_DEFAULT_VOICE}"]


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # 有些 OpenAI-compatible TTS 服务，例如 travisvn/chatterbox-tts-api，
    # Ngay cả khi phản hồi_format=mp3 được yêu cầu, nội dung WAV sẽ được trả về. Buổi thử giọng WebUI nếu đã sửa
    # Với âm thanh/mp3, trình duyệt có thể không phát được, vì vậy ở đây định dạng thực được xác định bằng tiêu đề tệp.
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
    ):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


def _build_uploaded_file_path(uploaded_file, target_dir, allowed_extensions, prefix):
    """Tạo đường dẫn lưu phía máy chủ được kiểm soát cho các tệp do trình duyệt tải lên."""
    original_name = os.path.basename(str(uploaded_file.name or ""))
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in allowed_extensions:
        logger.warning(
            f"reject unsupported uploaded file extension: {original_name or '<empty>'}"
        )
        raise ValueError("unsupported uploaded file type")

    normalized_target_dir = os.path.realpath(target_dir)
    os.makedirs(normalized_target_dir, exist_ok=True)
    # Không sử dụng lại tên tệp được trình duyệt chuyển vào và tránh ghi đè dấu phân cách đường dẫn, ký tự điều khiển hoặc cùng tên. UUID chỉ được sử dụng cho
    # Việc tải xuống phía máy chủ không thay đổi tên ban đầu mà người dùng nhìn thấy trong điều khiển tải lên.
    file_path = os.path.realpath(
        os.path.join(normalized_target_dir, f"{prefix}-{uuid4().hex}{extension}")
    )
    if os.path.commonpath([normalized_target_dir, file_path]) != normalized_target_dir:
        logger.warning(f"invalid uploaded file path: {file_path}")
        raise ValueError("invalid uploaded file path")
    return file_path


def _get_existing_local_media_files(local_videos_dir: str) -> list[str]:
    """Quét các tệp ảnh và video hợp lệ trong thư mục local_videos, bỏ qua tệp tạm."""
    if not os.path.isdir(local_videos_dir):
        return []
    valid_files = []
    try:
        entries = sorted(os.listdir(local_videos_dir))
    except Exception:
        return []
    for entry in entries:
        full_path = os.path.join(local_videos_dir, entry)
        if not os.path.isfile(full_path):
            continue
        if entry.endswith(".png.mp4") or entry.endswith(".temp") or entry.endswith(".cache"):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext in LOCAL_MATERIAL_EXTENSIONS or ext == ".webp":
            valid_files.append(full_path)
    return valid_files


def _initialize_session_state():
    """Trạng thái trang khởi tạo tập trung được bảo toàn trong các lần chạy lại."""
    if not st.session_state.get("cross_post_recovery_checked"):
        # WebUI có thể chạy độc lập mà không cần FastAPI nên cũng cần được xử lý trong quá trình khởi tạo phiên đầu tiên
        # Trạng thái xuất bản bị bỏ lại do quá trình khởi động lại. Khi quá trình khôi phục không thành công, không có dấu nào được ghi và các lần chạy lại tiếp theo sẽ thử lại.
        recovered = tm.recover_interrupted_cross_posts()
        if recovered is not None:
            st.session_state["cross_post_recovery_checked"] = True

    saved_ui_language = config.ui.get("language", "")
    browser_locale = st.context.locale
    initial_ui_language = utils.resolve_ui_language(
        saved_language=saved_ui_language,
        browser_locale=browser_locale,
        supported_languages=locales.keys(),
    )

    defaults = {
        "video_subject": "",
        "video_script": "",
        "video_terms": "",
        "paragraph_number_input": _saved_ui_number(
            "paragraph_number",
            1,
            llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            int,
        ),
        "video_script_prompt": _saved_ui_text(
            "video_script_prompt",
            max_length=llm.MAX_SCRIPT_PROMPT_LENGTH,
        ),
        "custom_system_prompt": _saved_ui_text(
            "custom_system_prompt",
            llm.DEFAULT_SCRIPT_SYSTEM_PROMPT,
            llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
        ),
        "match_materials_to_script": bool(
            config.app.get("match_materials_to_script", False)
        ),
        "custom_bgm_file_input": _saved_ui_text("custom_bgm_file"),
        "sonilo_bgm_prompt_input": _saved_ui_text(
            "sonilo_bgm_prompt",
            max_length=sonilo_service.MAX_PROMPT_LENGTH,
        ),
        "elevenlabs_music_prompt_input": _saved_ui_text(
            "elevenlabs_music_prompt",
            max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
        ),
        "subtitle_enabled_checkbox": _saved_ui_bool("subtitle_enabled", True),
        "stroke_color_picker": _saved_ui_color("stroke_color", "#000000"),
        "stroke_width_slider": _saved_ui_number(
            "stroke_width", 1.5, 0.0, 10.0
        ),
        "loomloom_candidate_count": _saved_ui_number(
            "loomloom_candidate_count",
            3,
            1,
            loomloom.MAX_SCRIPT_CANDIDATES,
            int,
        ),
        "loomloom_script_duration_seconds": _saved_ui_number(
            "loomloom_script_duration_seconds", 60, 10, 600, int
        ),
        "ui_language": initial_ui_language,
        # Các tài liệu cục bộ đã được đặt trên đĩa cho phép người dùng tiếp tục sử dụng lại chúng sau khi chỉ sửa đổi bản sao.
        "local_video_materials": [],
        # 生成按钮回调先登记任务，使顶部入口能立即显示运行中数量。
        "active_generation_tasks": {},
        # 最近一次从当前页面提交的任务。生成改为后台执行后，页面 Fragment
        # Trạng thái truy vấn theo ID này; làm mới không còn phụ thuộc vào tập lệnh trang cũ đang được thực thi.
        "current_generation_task_id": "",
        # Các truy vấn và thực thi LoomLoom phải giữ lại chính xác cùng một dữ liệu đầu vào và
        # clientRequestId để tránh các tác vụ thanh toán lặp lại do thử lại mạng gây ra.
        "loomloom_script_batch": None,
        "loomloom_script_quote": None,
        "loomloom_script_input_signature": "",
        "loomloom_client_request_id": "",
        "loomloom_run_id": "",
        "loomloom_run_status": "",
        "loomloom_run_error": "",
        "loomloom_poll_failure_count": 0,
        "loomloom_poll_retry_after": 0.0,
        "loomloom_poll_paused": False,
        "loomloom_script_candidates": (),
        "loomloom_candidate_errors": (),
        "loomloom_selected_candidate": 0,
        "loomloom_video_batch": None,
        "loomloom_video_quote": None,
        "loomloom_video_input_signature": "",
        "loomloom_video_client_request_id": "",
        "loomloom_video_confirm_charge": False,
        "loomloom_video_quote_error_signature": "",
        "loomloom_video_quote_error": "",
        "loomloom_video_capability": None,
        "loomloom_video_capability_fingerprint": "",
        "loomloom_video_capability_load_attempt": "",
        "loomloom_video_capability_error": "",
        "loomloom_video_model_id": "",
        # Khi sao chép hoặc lồng tiếng hoàn chỉnh được tạo lần đầu tiên, hãy sử dụng bản tóm tắt này và tự động
        # Điền số lượng tài liệu được đề xuất; nó sẽ bị xóa sau khi sử dụng để tránh ghi đè các điều chỉnh thủ công tiếp theo của người dùng.
        "loomloom_video_scene_autofill_digest": "",
        "wavespeed_confirm_charge": False,
        "volcengine_seedance_confirm_charge": False,
        "ofox_confirm_charge": False,
        "metaso_minimax_confirm_charge": False,
        # Video AI được tính phí theo phân khúc vật liệu. Theo mặc định, chỉ có một phân đoạn được tạo. Người dùng có thể chủ động tăng số lượng sau khi xác nhận hiệu quả.
        "loomloom_video_scene_count": _saved_ui_number(
            "loomloom_video_scene_count",
            1,
            1,
            loomloom.MAX_VIDEO_SCENES,
            int,
        ),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_initialize_session_state()


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key)
    if value is not None:
        return value
    # Các tính năng mới sẽ được duy trì trước tiên bằng tiếng Trung và tiếng Anh. Khi các ngôn ngữ khác thiếu bản dịch riêng lẻ, chúng sẽ chuyển sang tiếng Anh để tránh nhiều bản dịch.
    # Sau khi sao chép cùng một tiếng Anh sang ngôn ngữ, nó sẽ mất đồng bộ hóa trong một thời gian dài; khóa gốc chỉ được hiển thị khi khóa không tồn tại bằng tiếng Anh.
    return locales.get("en", {}).get("Translation", {}).get(key, key)


# -----------------------------------------------------------------------------
# Quản lý tác vụ: quét lịch sử, trạng thái đang chạy, khôi phục tham số và tương tác danh sách
# -----------------------------------------------------------------------------


def _format_task_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_task_subject(subject, max_length=30):
    subject = str(subject or "").replace("\n", " ").strip()
    if len(subject) <= max_length:
        return subject or "-"
    return f"{subject[:max_length]}..."


def _safe_load_task_script(task_path):
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}

    try:
        with open(script_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}


def _find_final_task_video(task_path: str) -> str:
    """
    Trả về phim cuối cùng có số thứ tự nhỏ nhất trong thư mục tác vụ.

    Quá trình tổng hợp cũng tạo ra các tệp tạm thời kết hợp, temp-clip và MoviePy, không thể
    Cho biết rằng tác vụ đã được hoàn thành thành công, vì vậy chỉ ``final-<serial number>.<extension>`` được chấp nhận ở đây.
    """
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match:
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)


def _build_restore_upload_requirements(params: Mapping) -> dict:
    """
    Ghi lại các phần phụ thuộc của tệp đã tải lên trong các tác vụ lịch sử mà Streamlit không thể tự động khôi phục.

    Các trình duyệt không cho phép các chương trình phục hồi lại file_uploader, do đó cần có nhật ký cục bộ riêng khi tiếp tục tác vụ
    Các phần phụ thuộc của vật liệu và âm thanh tùy chỉnh, đồng thời kiểm tra xem chúng đã được bổ sung hoặc thay thế tích cực hay chưa trước khi người dùng tái tạo.
    """
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """Trả về các phần phụ thuộc của tệp đã tải lên trước đây vẫn chưa được biểu mẫu hiện tại đáp ứng."""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            # Phiên bản mới của WebUI sử dụng giọng nói rõ ràng. Người dùng chuyển sang lồng tiếng tự động hoặc không lồng tiếng, biểu thị
            # Âm thanh được tải lên trong lịch sử đã được thay thế tích cực; tải lên lại chỉ được yêu cầu nếu chế độ tải lên tiếp tục được chọn.
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            # Giữ hành vi tương thích của người gọi cũ dựa trên âm sắc để tránh ảnh hưởng đến API và các công cụ kiểm tra hiện có.
            unmet.add("custom_audio")

    return unmet


def _queue_task_restore(task_id):
    # Danh sách tác vụ chạy theo từng đoạn và không thể sửa đổi trực tiếp trạng thái của điều khiển biểu mẫu chính đã tạo.
    # 这里只记录候选任务并触发整页 rerun，确认和参数恢复由主页面统一处理。
    st.session_state["task_restore_candidate_id"] = task_id
    st.session_state["task_manager_popover_nonce"] = (
        st.session_state.get("task_manager_popover_nonce", 0) + 1
    )
    st.rerun(scope="app")


def _normalize_task_state(state):
    if state in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return state
    try:
        return int(state)
    except (TypeError, ValueError):
        return state


def _active_generation_tasks():
    tasks = st.session_state.setdefault("active_generation_tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        st.session_state["active_generation_tasks"] = tasks
    return tasks


def _add_active_generation_task(task_id, subject=None):
    tasks = _active_generation_tasks()
    task = tasks.setdefault(task_id, {})
    task["subject"] = subject or task.get("subject") or task_id
    task["mtime"] = task.get("mtime") or datetime.now().timestamp()


def _remove_active_generation_task(task_id):
    tasks = _active_generation_tasks()
    if task_id in tasks:
        del tasks[task_id]
    if st.session_state.get("pending_generation_task_id") == task_id:
        del st.session_state["pending_generation_task_id"]


def _prepare_generation_task():
    # on_click của st.button sẽ được kích hoạt trước khi tập lệnh trang được thực thi lại. Tạo trước ID nhiệm vụ tại đây,
    # 顶部任务管理入口就能在同一次 rerun 中显示“生成中”数量。
    task_id = str(uuid4())
    st.session_state["pending_generation_task_id"] = task_id
    subject = st.session_state.get("video_subject") or st.session_state.get(
        "video_script"
    )
    _add_active_generation_task(task_id, subject=subject)


def _task_state_label(state, has_video):
    normalized_state = _normalize_task_state(state)
    if normalized_state == const.TASK_STATE_COMPLETE:
        return tr("Task Status Complete")
    if normalized_state == const.TASK_STATE_FAILED:
        return tr("Task Status Failed")
    if normalized_state == const.TASK_STATE_PROCESSING:
        return tr("Task Status Processing")
    if has_video:
        return tr("Task Status Complete")
    return tr("Task Status History")


def _task_state_filter_key(task):
    normalized_state = _normalize_task_state(task.get("state"))
    if normalized_state == const.TASK_STATE_PROCESSING:
        return "processing"
    if normalized_state == const.TASK_STATE_FAILED:
        return "failed"
    if normalized_state == const.TASK_STATE_COMPLETE or task["video_file"]:
        return "complete"
    return "history"


def _scan_history_tasks(limit=30):
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    # 任务管理 fragment 每两秒刷新一次。先只读取低成本的目录元数据并截取最近
    # task, sau đó phân tích script.json và danh sách video để tránh phải quét liên tục toàn bộ nội dung khi có nhiều task lịch sử.
    task_entries = []
    try:
        with os.scandir(tasks_root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    task_entries.append(
                        (
                            entry.stat(follow_symlinks=False).st_mtime,
                            entry.name,
                            entry.path,
                        )
                    )
                except OSError as e:
                    # Các thư mục tác vụ riêng lẻ có thể bị xóa và điều này sẽ không khiến toàn bộ bảng tác vụ trở nên vô dụng.
                    logger.debug(f"skip unavailable task directory: {entry.path}, {e}")
    except OSError as e:
        logger.warning(f"failed to scan task directory: {tasks_root}, {e}")
        return []

    task_entries.sort(key=lambda item: item[0], reverse=True)
    tasks = []
    for mtime, name, task_path in task_entries[:limit]:
        script_data = _safe_load_task_script(task_path)
        params_data = script_data.get("params", {}) if script_data else {}
        video_file = _find_final_task_video(task_path)
        subject = (
            params_data.get("video_subject")
            or script_data.get("script", "")[:40]
            or name
        )
        tasks.append(
            {
                "task_id": name,
                "subject": subject,
                "state": const.TASK_STATE_COMPLETE if video_file else None,
                "progress": 100 if video_file else 0,
                "mtime": mtime,
                "task_path": task_path,
                "video_file": video_file,
                "source": "history",
            }
        )

    return tasks


def _collect_task_summaries(limit=20):
    history_tasks = {task["task_id"]: task for task in _scan_history_tasks(limit=50)}

    try:
        runtime_tasks, _ = sm.state.get_all_tasks(1, 50)
    except Exception as e:
        logger.warning(f"failed to load runtime tasks: {e}")
        runtime_tasks = []

    for task in runtime_tasks:
        task_id = task.get("task_id", "")
        if not task_id:
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_task = history_tasks.get(task_id, {})
        video_files = task.get("videos") or []
        video_file = (
            video_files[0] if video_files else history_task.get("video_file", "")
        )
        subject = (
            task.get("video_subject")
            or history_task.get("subject")
            or (task.get("script", "")[:40] if task.get("script") else "")
            or task_id
        )

        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": subject,
            "state": task.get("state"),
            "cross_post_state": task.get("cross_post_state"),
            "progress": int(task.get("progress", 0) or 0),
            "mtime": os.path.getmtime(task_path)
            if os.path.isdir(task_path)
            else history_task.get("mtime", 0),
            "task_path": task_path,
            "video_file": video_file,
            "source": "runtime",
        }

    for task_id, active_task in _active_generation_tasks().items():
        history_task = history_tasks.get(task_id, {})
        if history_task and _task_state_filter_key(history_task) in {
            "complete",
            "failed",
        }:
            # 会话中的 active 标记只负责覆盖任务刚提交到状态存储前的极短窗口。
            # Sau khi tác vụ nền kết thúc, trạng thái cuối cùng thực sự phải chiếm ưu thế và các tác vụ thất bại không thể hiển thị lại khi được tạo.
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": active_task.get("subject")
            or history_task.get("subject")
            or task_id,
            "state": const.TASK_STATE_PROCESSING,
            "progress": history_task.get("progress", 0),
            "mtime": active_task.get("mtime")
            or history_task.get("mtime", datetime.now().timestamp()),
            "task_path": task_path,
            "video_file": history_task.get("video_file", ""),
            "source": "active",
        }

    tasks = list(history_tasks.values())
    return sorted(tasks, key=lambda item: item["mtime"], reverse=True)[:limit]


def _is_headless_server():
    # Trong triển khai Docker hoặc máy chủ không có máy tính để bàn, quy trình WebUI không có quyền truy cập vào môi trường máy tính để bàn của người dùng:
    # xdg-open/webbrowser sẽ chỉ bị lỗi âm thầm trong vùng chứa. Điều này nên được thay đổi thành xem trước trong trình duyệt
    # Video, sử dụng dấu nhắc đường dẫn thay vì mở thư mục. Việc triển khai máy tính để bàn macOS/Windows không bị ảnh hưởng.
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_task_path(task_path):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path: {normalized_path}")
        return
    if not os.path.isdir(normalized_path):
        return
    if _is_headless_server():
        # Thư mục lưu trữ thường được ánh xạ trở lại máy chủ dưới dạng ổ đĩa gắn kết và đường dẫn tương đối được nhắc để định vị tệp.
        rel_path = os.path.relpath(normalized_path, os.path.dirname(tasks_root))
        st.toast(f"{tr('Open Task Folder')}: ./storage/{rel_path}", icon=":material/folder_open:")
        return
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_path])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_path])
    except Exception as e:
        logger.error(f"failed to open task folder: {normalized_path}, {e}")
        webbrowser.open(f"file://{normalized_path}")


def _open_task_video(video_file):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_file = os.path.abspath(video_file)

    # Đường dẫn video đến từ quá trình quét thư mục tác vụ hoặc trạng thái thời gian chạy. Vẫn còn một hạn chế là chỉ có thể mở được thư mục tác vụ.
    # các tệp trong giao diện người dùng để ngăn các hoạt động giao diện người dùng bị mở rộng bởi các đường dẫn bất thường thành khả năng mở tệp cục bộ tùy ý.
    if not normalized_file.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task video path: {normalized_file}")
        return
    if not os.path.isfile(normalized_file):
        logger.warning(f"task video does not exist: {normalized_file}")
        return

    if _is_headless_server():
        # 无桌面环境时在任务面板内嵌播放器预览，代替调用系统播放器。
        st.session_state["task_preview_video_file"] = normalized_file
        return

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_file])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_file)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_file])
    except Exception as e:
        logger.error(f"failed to open task video: {normalized_file}, {e}")


def _delete_task(task_id, task_path, task_state=None):
    # Trạng thái hiển thị trang có thể bị tụt hậu so với các tác vụ nền. Đồng thời kiểm tra trạng thái đến và phiên hiện tại trước khi xóa
    # Tác vụ đang hoạt động và trạng thái mới nhất để tránh vô tình xóa khi tác vụ vừa mới bắt đầu hoặc video trung gian vừa được tạo.
    current_task = None
    try:
        current_task = sm.state.get_task(task_id)
    except Exception as e:
        logger.exception(f"failed to verify task state before deletion: {task_id}, {e}")
        return False

    task_snapshot = dict(current_task or {})
    task_snapshot.setdefault("state", task_state)
    if task_id in _active_generation_tasks():
        task_snapshot["state"] = const.TASK_STATE_PROCESSING

    if tm.is_task_busy(task_snapshot):
        logger.warning(f"refused to delete running task: {task_id}")
        return False

    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)

    # Việc xóa một tác vụ sẽ xóa trạng thái tác vụ và các tệp bản dựng cục bộ. Điều này phải được giới hạn ở bộ nhớ/tác vụ
    # 下，避免异常 task_path 造成误删其它本地目录。
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path for deletion: {normalized_path}")
        return False

    try:
        if hasattr(sm.state, "delete_task"):
            sm.state.delete_task(task_id)
        if os.path.isdir(normalized_path):
            shutil.rmtree(normalized_path)
        logger.info(f"deleted task: {task_id}")
        return True
    except Exception as e:
        logger.exception(f"failed to delete task: {task_id}, {e}")
        return False


def _count_processing_tasks(tasks):
    # Cổng quản lý tác vụ hàng đầu chỉ cần hiển thị số lượng tác vụ "đang tạo".
    # Đánh giá khóa trạng thái nội bộ được sử dụng lại ở đây để tránh dựa vào việc sao chép hiển thị đa ngôn ngữ để gây ra sự không thống nhất về mặt thống kê ở các ngôn ngữ khác nhau.
    processing_task_ids = {
        task["task_id"]
        for task in tasks
        if _task_state_filter_key(task) == "processing"
    }
    return len(processing_task_ids)


def _task_manager_label(processing_count):
    label = tr("Task Manager")
    if processing_count <= 0:
        return label
    return f"{label} · {processing_count}"


def _build_video_download_name(subject, index, total):
    """Tạo tên tệp tải xuống an toàn đa nền tảng dựa trên chủ đề video."""
    safe_subject = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub(" ", str(subject or ""))
    safe_subject = re.sub(r"\s+", " ", safe_subject).strip(" .")[:80].rstrip(" .")
    if not safe_subject:
        safe_subject = "video"
    # Win32 在识别设备名时会忽略扩展名前的尾随空格和句点。与背景音乐上传
    # Phù hợp với các quy tắc hiện có để tránh ``CON .topic`` bỏ qua việc bảo vệ tên dành riêng.
    windows_basename = safe_subject.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        safe_subject = f"_{safe_subject}"

    suffix = f"-{index}" if total > 1 else ""
    return f"{safe_subject}{suffix}.mp4"


def _render_task_table(filtered_tasks, key_prefix):
    with st.container(key=f"task_table_header_{key_prefix}"):
        header_cols = st.columns([1.1, 1.7, 3.0, 0.8, 1.6], vertical_alignment="center")
        header_cols[0].caption(tr("Task Status"))
        header_cols[1].caption(tr("Task Updated At"))
        header_cols[2].caption(tr("Task Subject"))
        header_cols[3].caption(tr("Task Progress"))
        header_cols[4].caption(tr("Task Actions"))

    if not filtered_tasks:
        st.info(tr("No Tasks Match Filter"))
        return

    visible_tasks = filtered_tasks[:12]
    list_height = min(390, max(96, len(visible_tasks) * 58))
    with st.container(height=list_height, border=False):
        for task in visible_tasks:
            task_id = task["task_id"]
            has_video = bool(task["video_file"] and os.path.isfile(task["video_file"]))
            is_processing = _task_state_filter_key(task) == "processing"
            is_busy = is_processing or tm.is_task_busy(task)
            has_restore_data = os.path.isfile(
                os.path.join(task["task_path"], "script.json")
            )
            safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

            # Sử dụng các cột + vùng chứa có viền gốc Streamlit để duy trì các hoạt động trên mỗi hàng.
            # So với các bảng HTML/CSS tùy chỉnh, phương pháp này ổn định hơn trước những thay đổi của phiên bản Streamlit;
            # So với khung dữ liệu, nó có thể giữ lại các hành động nội tuyến như phát, mở thư mục và xóa.
            with st.container(
                key=f"task_row_{key_prefix}_{safe_task_key}", border=True
            ):
                row_cols = st.columns(
                    [1.1, 1.7, 3.0, 0.8, 1.6],
                    vertical_alignment="center",
                )
                row_cols[0].write(_task_state_label(task["state"], has_video))
                row_cols[1].write(_format_task_time(task["mtime"]))
                row_cols[2].write(_format_task_subject(task["subject"]))
                row_cols[3].write(f"{task['progress']}%")

                action_cols = row_cols[4].columns(
                    4,
                    vertical_alignment="center",
                    gap="small",
                )
                with action_cols[0]:
                    play_label = tr("Play")
                    if st.button(
                        play_label,
                        key=f"play_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/play_arrow:",
                        help=play_label,
                        disabled=not has_video,
                    ):
                        _open_task_video(task["video_file"])

                with action_cols[1]:
                    open_label = tr("Open Task Folder")
                    if st.button(
                        open_label,
                        key=f"open_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/folder_open:",
                        help=open_label,
                    ):
                        _open_task_path(task["task_path"])

                with action_cols[2]:
                    restore_label = tr("Regenerate Task")
                    if st.button(
                        restore_label,
                        key=f"restore_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/replay:",
                        help=restore_label,
                        disabled=is_processing or not has_restore_data,
                    ):
                        _queue_task_restore(task_id)

                with action_cols[3]:
                    delete_label = tr("Delete Task")
                    delete_help = (
                        f"{delete_label} ({tr('Task Status Processing')})"
                        if is_busy
                        else delete_label
                    )
                    if st.button(
                        delete_label,
                        key=f"delete_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/delete:",
                        help=delete_help,
                        disabled=is_busy,
                    ):
                        if _delete_task(task_id, task["task_path"], task["state"]):
                            st.toast(tr("Task Deleted"))
                            st.rerun()
                        else:
                            st.error(tr("Task Delete Failed"))


def _render_task_manager_panel(tasks=None):
    tasks = tasks if tasks is not None else _collect_task_summaries()
    if not tasks:
        st.info(tr("No Tasks Yet"))
        return

    # Streamlit 1.59 hỗ trợ hiển thị lười biếng các Tab có trạng thái. Chỉ danh sách hiện tại được xây dựng lại khi chuyển đổi,
    # Tránh các đoạn được lập lịch để liên tục tạo bốn nhóm hàng nhiệm vụ và nút hành động cứ sau hai giây.
    status_tabs = [
        ("all", tr("All Tasks")),
        ("processing", tr("Task Status Processing")),
        ("complete", tr("Task Status Complete")),
        ("failed", tr("Task Status Failed")),
    ]
    tabs = st.tabs(
        [label for _, label in status_tabs],
        key="task_manager_status_tabs",
        on_change="rerun",
    )
    for (status_key, _), tab in zip(status_tabs, tabs):
        if not tab.open:
            continue
        with tab:
            filtered_tasks = [
                task
                for task in tasks
                if status_key == "all" or _task_state_filter_key(task) == status_key
            ]
            _render_task_table(filtered_tasks, status_key)

    _render_task_video_preview()


def _render_task_video_preview():
    # Dự phòng trong trình duyệt không có nút "Phát" khi triển khai trên máy tính để bàn: Trình phát kết xuất ở cuối bảng tác vụ.
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return

    tasks_root = os.path.abspath(utils.task_dir())
    if not (
        preview_file.startswith(tasks_root + os.sep) and os.path.isfile(preview_file)
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    preview_cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(preview_file))
    preview_cols[0].caption(f"{os.path.basename(preview_file)} · {task_name}")
    closed = preview_cols[1].button(
        "✕",
        key="close_task_video_preview",
        use_container_width=True,
        help=tr("Close"),
    )
    if closed:
        st.session_state.pop("task_preview_video_file", None)
        return
    st.video(preview_file)


@st.fragment(run_every="2s")
def _render_task_manager_entry():
    # Nhiệm vụ có thể được kích hoạt bởi trang hiện tại hoặc các trang khác. Lối vào được làm mới thường xuyên chỉ bằng cách sử dụng mảnh vỡ.
    # Chỉ có mã số tác vụ và nội dung cửa sổ bật lên mới được cập nhật mà không làm gián đoạn quá trình nhập biểu mẫu trang chính.
    task_summaries = _collect_task_summaries()
    processing_task_count = _count_processing_tasks(task_summaries)
    with st.container(key="task_manager_entry", width="content"):
        with st.popover(
            _task_manager_label(processing_task_count),
            width="content",
            key=(
                "task_manager_popover_"
                f"{st.session_state.get('task_manager_popover_nonce', 0)}"
            ),
        ):
            _render_task_manager_panel(task_summaries)


def _load_task_restore_payload(task_id):
    tasks_root = os.path.realpath(utils.task_dir())
    task_path = os.path.realpath(os.path.join(tasks_root, str(task_id)))
    try:
        if os.path.commonpath([tasks_root, task_path]) != tasks_root:
            raise ValueError("task path is outside the task directory")
    except ValueError as e:
        logger.warning(f"invalid task restore path: {task_id}, {e}")
        return None

    script_data = _safe_load_task_script(task_path)
    raw_params = script_data.get("params")
    if not isinstance(raw_params, dict):
        logger.warning(f"task has no restorable parameters: {task_id}")
        return None

    params_input = dict(raw_params)
    if script_data.get("script"):
        params_input["video_script"] = script_data["script"]
    if script_data.get("search_terms"):
        params_input["video_terms"] = script_data["search_terms"]

    try:
        params = VideoParams.model_validate(params_input).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"failed to validate task restore parameters: {task_id}, {e}")
        return None

    return {
        "task_id": str(task_id),
        "subject": params.get("video_subject") or script_data.get("script") or task_id,
        "params": params,
    }


def _infer_tts_server_from_voice(voice_name):
    if voice.is_no_voice(voice_name):
        return voice.NO_VOICE_NAME
    if voice.is_siliconflow_voice(voice_name):
        return "siliconflow"
    if voice.is_gemini_voice(voice_name):
        return "gemini-tts"
    if voice.is_mimo_voice(voice_name):
        return "mimo-tts"
    if voice.is_minimax_voice(voice_name):
        return "minimax-tts"
    if voice.is_elevenlabs_voice(voice_name):
        return "elevenlabs"
    if voice.is_chatterbox_voice(voice_name):
        return "chatterbox"
    if voice.is_kokoro_voice(voice_name):
        return "kokoro"
    if voice.is_fish_audio_voice(voice_name):
        return "fish_audio"
    if voice.is_azure_v2_voice(voice_name):
        return "azure-tts-v2"
    return "azure-tts-v1"


def _set_stable_widget_value(key, value):
    if value is not None:
        st.session_state[localized_widget_key(key)] = value


def _apply_pending_task_restore():
    payload = st.session_state.pop("task_restore_payload", None)
    if not payload:
        return False

    _apply_restored_params(payload["params"])
    st.session_state["task_restore_succeeded"] = True
    logger.info(f"restored task configuration: {payload['task_id']}")
    return True


def _apply_restored_params(params):
    """
    Viết một bản sao hoàn chỉnh của các tham số đã tạo trở lại trạng thái điều khiển trang.

    Khôi phục tác vụ lịch sử và cài đặt nhập trước sử dụng cùng một mô hình tham số, vì vậy chúng có chung cách thực hiện để tránh
    Khi thêm trường mới, chỉ một trong các đường dẫn được cập nhật. Người gọi phải thực thi trước khi hiển thị bất kỳ điều khiển nào, nếu không
    Streamlit sẽ từ chối sửa đổi trạng thái của điều khiển đã được khởi tạo.
    """
    video_terms = params.get("video_terms") or ""
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)

    # Viết quảng cáo và cài đặt tập lệnh nâng cao.
    st.session_state["video_subject"] = params.get("video_subject") or ""
    st.session_state["video_script"] = params.get("video_script") or ""
    st.session_state["video_terms"] = str(video_terms)
    _set_stable_widget_value(
        "script_language_select", params.get("video_language") or ""
    )
    st.session_state["paragraph_number_input"] = params.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params.get("video_script_prompt") or ""
    st.session_state["custom_system_prompt"] = (
        params.get("custom_system_prompt") or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # Cài đặt video. Máy chủ không thể ghi kiểm soát tải lên tài liệu, vì vậy người dùng cần phải chọn lại tài liệu cục bộ.
    video_source = params.get("video_source") or "pexels"
    _set_stable_widget_value("video_source_select", video_source)
    _set_stable_widget_value(
        "video_concat_mode_select", params.get("video_concat_mode") or "random"
    )
    _set_stable_widget_value(
        "video_transition_mode_select",
        params.get("video_transition_mode") or VideoTransitionMode.none.value,
    )
    _set_stable_widget_value(
        f"video_aspect_for_{video_source}",
        params.get("video_aspect") or VideoAspect.portrait.value,
    )
    _set_stable_widget_value(
        "video_fit_mode_select",
        params.get("video_fit_mode") or VideoFitMode.cover.value,
    )
    _set_stable_widget_value(
        "video_clip_duration_select", params.get("video_clip_duration", 3)
    )
    _set_stable_widget_value(
        "video_clip_speed_slider",
        # API có thể được viết nhanh hơn khả năng xử lý của WebUI và giai đoạn tạo tác vụ được chuẩn hóa một cách an toàn, nhưng
        # 历史记录仍可能保留原值。恢复任务前再次归一化，避免给 Streamlit
        # Việc chèn thanh trượt các giá trị ngoài giới hạn, NaN hoặc giá trị vô hạn gây ra trạng thái điều khiển bất thường.
        utils.normalize_clip_speed(params.get("video_clip_speed", 1.0)),
    )
    _set_stable_widget_value("video_count_select", params.get("video_count", 1))
    st.session_state["match_materials_to_script"] = bool(
        params.get("match_materials_to_script", False)
    )

    # Cài đặt âm thanh. Máy chủ TTS không ghi các tác vụ cũ, suy luận dựa trên voice_name lịch sử.
    voice_name = params.get("voice_name") or voice.NO_VOICE_NAME
    tts_server = _infer_tts_server_from_voice(voice_name)
    if params.get("custom_audio_file"):
        voice_mode = VOICE_MODE_UPLOAD
    elif voice.is_no_voice(voice_name):
        voice_mode = VOICE_MODE_NONE
    else:
        voice_mode = VOICE_MODE_TTS
    _set_stable_widget_value("voice_mode_control", voice_mode)
    if tts_server != voice.NO_VOICE_NAME:
        _set_stable_widget_value("tts_server_select", tts_server)
        _set_stable_widget_value(f"speech_synthesis_select_{tts_server}", voice_name)
    _set_stable_widget_value("voice_volume_select", params.get("voice_volume", 1.0))
    _set_stable_widget_value("voice_rate_select", params.get("voice_rate", 1.0))
    bgm_type = params.get("bgm_type") or ""
    _set_stable_widget_value("bgm_type_select", bgm_type)
    _set_stable_widget_value("bgm_volume_select", params.get("bgm_volume", 0.2))
    if bgm_type == "preset" and params.get("bgm_file"):
        # Điều khiển bài hát cài sẵn sử dụng tên tệp làm giá trị kinh doanh ổn định. Nhiệm vụ lịch sử có thể lưu đường dẫn tuyệt đối hoặc
        # Đường dẫn tương đối, sau khi lấy tên cơ sở thống nhất, nó có thể khớp với danh sách bài hát hiện được liệt kê an toàn.
        _set_stable_widget_value(
            "preset_song_select", os.path.basename(str(params["bgm_file"]))
        )
    st.session_state["custom_bgm_file_input"] = params.get("bgm_file") or ""
    st.session_state["sonilo_bgm_prompt_input"] = (
        params.get("video_music_prompt") or params.get("sonilo_bgm_prompt") or ""
    )
    st.session_state["elevenlabs_music_prompt_input"] = (
        params.get("video_music_prompt") or ""
    )

    # Cài đặt phụ đề.对旧任务中的越界数值做最小限幅,避免 Slider 无法初始化。
    st.session_state["subtitle_enabled_checkbox"] = bool(
        params.get("subtitle_enabled", True)
    )
    _set_stable_widget_value("font_name_select", params.get("font_name") or "")
    _set_stable_widget_value(
        "subtitle_position_select", params.get("subtitle_position") or "bottom"
    )
    _set_stable_widget_value(
        "subtitle_display_mode_select", params.get("subtitle_display_mode") or "sentence"
    )
    _set_stable_widget_value(
        "subtitle_animation_select", params.get("subtitle_animation") or "none"
    )
    custom_position = min(100.0, max(0.0, float(params.get("custom_position", 70.0))))
    st.session_state["custom_position_input"] = str(custom_position)
    st.session_state["font_color_picker"] = params.get("text_fore_color") or "#FFFFFF"
    st.session_state["font_size_slider"] = min(
        100, max(30, int(params.get("font_size", 60)))
    )
    st.session_state["stroke_color_picker"] = params.get("stroke_color") or "#000000"
    st.session_state["stroke_width_slider"] = min(
        10.0, max(0.0, float(params.get("stroke_width", 1.5)))
    )
    background_color = params.get("text_background_color")
    background_enabled = bool(background_color)
    st.session_state["subtitle_background_enabled_checkbox"] = background_enabled
    if isinstance(background_color, str):
        st.session_state["subtitle_background_color_picker"] = background_color
    st.session_state["rounded_subtitle_background_checkbox"] = bool(
        params.get("rounded_subtitle_background", False) and background_enabled
    )

    st.session_state.pop("local_video_materials_uploader", None)
    # Các tác vụ lịch sử chỉ lưu các đường dẫn vật liệu và không có gì đảm bảo rằng các tệp này sẽ vẫn tồn tại trong môi trường hiện tại.
    # Đồng thời, xóa các tài liệu đã tải lên được lưu trong bộ nhớ đệm trên trang hiện tại để tránh lạm dụng các tệp từ tác vụ khác sau khi khôi phục.
    st.session_state["local_video_materials"] = []
    st.session_state.pop("custom_audio_file_uploader", None)
    st.session_state.pop("custom_bgm_uploader", None)
    st.session_state.pop("custom_bgm_validation", None)
    st.session_state["task_restore_upload_requirements"] = (
        _build_restore_upload_requirements(params)
    )

    return True


def _dismiss_task_restore_dialog():
    st.session_state.pop("task_restore_candidate_id", None)


@st.dialog(
    tr("Regenerate Task"),
    width="small",
    on_dismiss=_dismiss_task_restore_dialog,
)
def _render_task_restore_dialog(task_id):
    payload = _load_task_restore_payload(task_id)
    if payload is None:
        st.error(tr("Task Restore Failed"))
        if st.button(tr("Cancel"), key="cancel_invalid_task_restore"):
            st.session_state.pop("task_restore_candidate_id", None)
            st.rerun(scope="app")
        return

    st.write(tr("Regenerate Task Confirmation"))
    st.caption(_format_task_subject(payload["subject"], max_length=80))
    cancel_col, load_col = st.columns(2)
    if cancel_col.button(
        tr("Cancel"),
        key="cancel_task_restore",
        use_container_width=True,
    ):
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")
    if load_col.button(
        tr("Load Task Configuration"),
        key="confirm_task_restore",
        type="primary",
        use_container_width=True,
    ):
        st.session_state["task_restore_payload"] = payload
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")


def _dismiss_settings_dialog():
    """Đóng cửa sổ bật lên cài đặt và đảm bảo rằng lần chạy lại toàn bộ trang tiếp theo sẽ không tự động mở lại."""
    st.session_state["settings_dialog_open"] = False


def _open_settings_dialog(target_tab=None):
    """Mở cửa sổ bật lên cài đặt và điều hướng trực tiếp đến tab doanh nghiệp được chỉ định."""
    st.session_state["settings_dialog_open"] = True
    if target_tab:
        # Chỉ có ID doanh nghiệp ổn định được lưu ở đây và văn bản đã dịch không được lưu; trước khi thực sự tạo các tab,
        # Ngôn ngữ giao diện hiện tại phân tích nhãn để ngăn bản sao cũ trở thành tùy chọn bất hợp pháp sau khi người dùng chuyển đổi ngôn ngữ.
        st.session_state["settings_dialog_target_tab"] = target_tab


def _open_material_settings_dialog():
    """Để sử dụng tính năng gọi lại thành phần nguồn video: mở trực tiếp cài đặt dịch vụ vật liệu."""
    _open_settings_dialog("material")


def _render_brand(available_update: str | None = None):
    """Render tên dự án và phiên bản cá nhân hóa."""
    st.markdown(
        f"""
        <h1 class="mpt-brand">
            <span class="mpt-brand__name">{html.escape(PROJECT_NAME)}</span>
            <span class="mpt-brand__version" style="opacity: 0.75; font-size: 0.85rem; font-weight: normal; margin-left: 8px;">v{html.escape(str(config.project_version))}</span>
        </h1>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every="1s")
def _render_pending_version_check():
    """Chỉ làm mới vùng thương hiệu khi chưa hoàn tất việc kiểm tra để tránh bị chặn hoặc thực hiện nhiều lần toàn bộ biểu mẫu trang."""
    snapshot = version_checker.poll_available_update(config.project_version)
    if snapshot.complete:
        # 检查完成后刷新一次整页，让顶部栏改为静态渲染并停止 fragment 轮询。
        # 该刷新发生在后台请求完成之后，不会延迟初始页面的其它内容。
        st.rerun(scope="app")
    _render_brand()


def _render_top_bar():
    """Hiển thị thanh trên cùng của trang bao gồm nhãn hiệu, quản lý tác vụ, cài đặt và chuyển đổi ngôn ngữ."""
    # Thanh trên cùng được chia làm 2 khu vực độc lập: khu vực thương hiệu và khu vực hoạt động. Màn hình hẹp của Streamlit
    # Bao bọc toàn bộ hai khu vực, sau đó tự động bọc bên trong khu vực hoạt động theo chiều rộng còn lại.
    with st.container(key="top_bar"):
        brand_col, actions_col = st.columns(
            [3.5, 2.0],
            vertical_alignment="center",
            gap="small",
        )

    with brand_col:
        update_snapshot = version_checker.poll_available_update(config.project_version)
        if update_snapshot.complete:
            _render_brand(update_snapshot.available_version)
        else:
            _render_pending_version_check()

    with actions_col:
        with st.container(
            key="top_bar_actions",
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            width="stretch",
        ):
            _render_task_manager_entry()

            st.button(
                tr("Settings"),
                key="open_settings_dialog_button",
                type="secondary",
                icon=":material/settings:",
                width="content",
                on_click=_open_settings_dialog,
            )

            language_codes = list(locales.keys())
            selected_index = 0
            for i, code in enumerate(language_codes):
                if code == st.session_state.get("ui_language", ""):
                    selected_index = i

            selected_language_code = st.selectbox(
                "Language / 语言",
                options=language_codes,
                index=selected_index,
                format_func=lambda code: locales[code].get("Language", code),
                key="top_language_code_selector",
                label_visibility="collapsed",
                width=180,
            )
            if selected_language_code:
                previous_language = st.session_state.get("ui_language", "")
                if selected_language_code != previous_language:
                    logger.info(
                        "UI language changed by user: "
                        f"previous_language={previous_language or '<empty>'}, "
                        f"selected_language={selected_language_code}"
                    )
                    st.session_state["ui_language"] = selected_language_code
                    # Nhận dạng tự động của trình duyệt chỉ ảnh hưởng đến phiên hiện tại; chỉ khi người dùng chủ động chuyển hộp thả xuống
                    # Ghi vào config.toml và các phiên mới tiếp theo sẽ được ưu tiên hơn lựa chọn rõ ràng này.
                    _set_runtime_config("ui", "language", selected_language_code)
                    _save_runtime_config()
                    # Buộc làm mới sau khi chuyển đổi ngôn ngữ để ngăn hộp chọn tiếp tục hiển thị bản sao ngôn ngữ cũ.
                    st.rerun()


support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "it-IT",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


# -----------------------------------------------------------------------------
# Các thành phần giao diện người dùng phổ biến, bộ nhớ đệm tài nguyên và ghi nhật ký
# -----------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def get_all_fonts():
    # Thư mục phông chữ hiếm khi thay đổi, nhưng Streamlit sẽ chạy lại trang mỗi khi điều khiển được tương tác. bộ nhớ đệm ngắn hạn
    # 可以避免连续重复 os.walk，同时保证新增字体后最多 30 秒即可被发现。
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


@st.cache_data(ttl=30, show_spinner=False)
def get_all_songs():
    # Nhạc nền và phông chữ sử dụng cùng một chiến lược chu kỳ ngắn, không có bộ nhớ đệm vĩnh viễn, có tính đến hiệu suất chạy lại và
    # Tình huống trong đó người dùng thêm tệp nhạc theo cách thủ công trong thời gian chạy.
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id phải luôn là UUID do máy chủ tạo. Ở đây chúng tôi thực hiện xác minh định dạng trước để tránh các ngoại lệ.
        # Truy cập các vị trí bên ngoài thư mục tác vụ thông qua việc ghép đường dẫn và tránh kích hoạt khi thư mục được mở sau đó.
        # Giải thích các ký tự đặc biệt của nền tảng shell.
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # Ngay cả khi xác minh UUID thành công, hãy xác nhận lại rằng đường dẫn cuối cùng vẫn nằm trong thư mục gốc của tác vụ để tránh
        # Nguy cơ vượt đường sẽ xuất hiện khi người gọi điều chỉnh nguồn task_id trong tương lai.
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                elif sys.platform.startswith("win"):
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as e:
                webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.exception(f"failed to open task folder: task_id={task_id}, error={e}")


@st.cache_resource
def init_log():
    # Trình xử lý nhật ký cơ bản là tài nguyên cấp quy trình, không phải trạng thái phiên trang. Streamlit cho mỗi thành phần
    # 交互都会 rerun 页面脚本，代码热重载也可能让缓存失效。日志初始化只能
    # 精确替换终端 Handler，不能清空正在生成任务使用的 WebUI 临时 Handler。
    _lvl = "DEBUG"

    return configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
    )


init_log()


def tr_optional(key, fallback_language=""):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key, "")
    if not value and fallback_language:
        fallback_loc = locales.get(fallback_language, {})
        value = fallback_loc.get("Translation", {}).get(key, "")
    return value if value else ""


def render_onboarding_tour():
    # Bootstrap chỉ bao gồm ba mục ổn định và không cố gắng kiểm soát Hộp thoại, Tab hoặc Biểu mẫu doanh nghiệp. Điều này sẽ cho phép
    # Người dùng mới hiểu toàn bộ quy trình và không kết hợp trạng thái khởi động với vòng đời thành phần động của Streamlit.
    steps = [
        Tour.bind(
            "open_settings_dialog_button",
            title=tr("Onboarding Model Settings Title"),
            desc=tr("Onboarding Model Settings Description"),
            side="bottom",
            align="end",
        ),
        Tour.bind(
            "main_settings_grid",
            title=tr("Onboarding Creation Settings Title"),
            desc=tr("Onboarding Creation Settings Description"),
            side="top",
            align="center",
        ),
        Tour.bind(
            "generate_video_button",
            title=tr("Onboarding Generate Video Title"),
            desc=tr("Onboarding Generate Video Description"),
            side="top",
            align="center",
        ),
    ]

    # Streamlit-tour 1.1.0 không hiển thị bản sao điều hướng trong các tham số xây dựng Python, nhưng phần cơ bản
    # Driver.js hỗ trợ ghi đè văn bản nút trong cấu hình cửa sổ bật lên ở mỗi bước. Nội địa hóa được tiêm thống nhất ở đây
    # Sao chép và thoát nội dung HTML vì thành phần sẽ hiển thị các trường này thông qua InternalHTML.
    previous_text = html.escape(tr("Onboarding Previous"))
    next_text = html.escape(tr("Onboarding Next"))
    done_text = html.escape(tr("Onboarding Done"))
    for index, step in enumerate(steps):
        step.popover["prevBtnText"] = f"&larr; {previous_text}"
        # Driver.js sẽ ghi đè lên mẫu tiến trình đã thay thế các biến khi hợp nhất cấu hình một bước, do đó, trực tiếp
        # Viết bước hiện tại và tổng số bước để tránh trang hiển thị phần giữ chỗ {{current}} chưa được giải quyết.
        step.popover["progressText"] = f"{index + 1} / {len(steps)}"
        if index == len(steps) - 1:
            step.popover["doneBtnText"] = done_text
        else:
            step.popover["nextBtnText"] = f"{next_text} &rarr;"

    tour = Tour(
        steps=steps,
        key=ONBOARDING_TOUR_KEY,
        show_progress=True,
        animate=True,
        overlay_opacity=0.55,
        one_time_tour=True,
    )

    # Mỗi phiên Streamlit chỉ được bắt đầu tích cực một lần. Việc nó đã được hoàn thành hay chưa được xác định bởi thành phần thông qua trình duyệt.
    # localStorage để tránh chạy lại trang hoặc tương tác điều khiển chung do liên tục bật lên quá trình khởi động.
    auto_start_key = f"{ONBOARDING_TOUR_KEY}-auto-started"
    if not st.session_state.get(auto_start_key, False):
        st.session_state[auto_start_key] = True
        tour.start()


def _render_generation_logs(task_id):
    """渲染后台任务日志快照，不从工作线程访问 Streamlit 会话状态。"""
    if config.ui.get("hide_log", False):
        return

    log_records = webui_task.get_task_logs(task_id)
    if not log_records:
        return

    st.code("\n".join(log_records))


def _render_generation_task_snapshot(task_id, task):
    """Kết xuất tiến trình, lý do lỗi hoặc phim cuối cùng dựa trên ảnh chụp nhanh trong kho lưu trữ trạng thái."""
    if not task:
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state = _normalize_task_state(task.get("state"))
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(
            progress,
            text=f"{tr('Task Progress')}: {progress}%",
        )
        _render_generation_logs(task_id)
        return

    if state == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        _render_generation_logs(task_id)
        return

    video_files = task.get("videos") or []
    if state != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    st.success(tr("Video Generation Completed"))
    for warning in task.get("warnings") or []:
        if isinstance(warning, Mapping) and warning.get("code") == "batch_materials_reused":
            st.warning(
                tr("Batch Material Reuse Warning").format(
                    index=warning.get("video_index", ""), count=warning.get("count", 0)
                )
            )
        elif isinstance(warning, Mapping) and warning.get("code") == "sonilo_bgm_failed":
            st.warning(
                tr("Sonilo BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        elif (
            isinstance(warning, Mapping)
            and warning.get("code") == "elevenlabs_bgm_failed"
        ):
            st.warning(
                tr("ElevenLabs BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        else:
            st.warning(str(warning))

    try:
        player_cols = st.columns(len(video_files) * 2 + 1)
        for i, url in enumerate(video_files):
            with player_cols[i * 2 + 1]:
                st.video(url)
                if not os.path.isfile(url):
                    logger.warning(
                        f"generated video is unavailable for download: "
                        f"task_id={task_id}, video_file={url}"
                    )
                    continue

                download_label = tr("Download Video")
                if len(video_files) > 1:
                    download_label = f"{download_label} {i + 1}"
                download_name = _build_video_download_name(
                    task.get("video_subject"),
                    i + 1,
                    len(video_files),
                )
                with open(url, "rb") as video_file:
                    st.download_button(
                        download_label,
                        data=video_file,
                        file_name=download_name,
                        mime=mimetypes.guess_type(url)[0] or "video/mp4",
                        key=f"download_generated_video_{task_id}_{i}",
                        icon=":material/download:",
                        on_click="ignore",
                        use_container_width=True,
                    )
    except Exception as exc:
        logger.exception(
            f"failed to render generated video preview: task_id={task_id}, "
            f"video_files={video_files}, error={exc}"
        )

    _render_generation_logs(task_id)
    if st.session_state.get("handled_generation_task_id") != task_id:
        # Các mảnh có thể lặp đi lặp lại cùng một nhiệm vụ hoàn thành. Bất kể việc mở thư mục tự động có được bật hay không,
        # 每个任务都只处理一次完成事件，避免重复弹出资源管理器或重复写入日志。
        st.session_state["handled_generation_task_id"] = task_id
        if config.ui.get("open_task_folder_on_completion", True):
            open_task_folder(task_id)
        logger.info(f"{tr('Video Generation Completed')}: task_id={task_id}")


@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    """Chỉ thăm dò ý kiến ​​trong quá trình chạy nhiệm vụ; chuyển về kết quả tĩnh sau khi kết thúc để dừng việc làm mới theo lịch trình không cần thiết."""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query WebUI generation task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        # Các tập lệnh toàn trang giờ đây không còn logic tạo tốn thời gian và có thể chạy lại một cách an toàn và thay đổi kết quả thành tĩnh
        # kết xuất. Bằng cách này, trình duyệt sẽ không giữ lại vĩnh viễn Đoạn bỏ phiếu dài hai giây sau khi hoàn thành nhiệm vụ.
        st.rerun(scope="app")

    _render_generation_task_snapshot(task_id, task)


def _render_current_generation_task():
    """Khôi phục giao diện người dùng có thể truy vấn của các tác vụ được gửi gần đây nhất cho trang hiện tại bên dưới nút tạo."""
    task_id = st.session_state.get("current_generation_task_id", "")
    if not task_id:
        return

    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query current WebUI task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, task)
        return

    _render_running_generation_task(task_id)


def get_llm_provider_tips(provider_id, **kwargs):
    # Bản sao mô tả nhà cung cấp LLM sử dụng thống nhất quy tắc `llm_provider_tips.<provider_id>`.
    # Bằng cách này, khi thêm nhà cung cấp, bạn chỉ cần điền bản sao bằng ngôn ngữ; nếu không có bản sao, khối nhắc nhở sẽ không được hiển thị.
    # Tránh xếp chồng một số lượng lớn các hướng dẫn được mã hóa cứng bằng tiếng Trung và tiếng Anh trong Main.py.
    provider = get_llm_provider(provider_id)
    if provider is None:
        return ""

    # Hướng dẫn cấu hình của nhà cung cấp hiện duy trì hai bộ mẫu tiêu chuẩn bằng tiếng Trung và tiếng Anh; ngôn ngữ giao diện khác
    # Sử dụng tiếng Anh thống nhất để tránh tình trạng đồng bộ hóa lâu dài sau khi sao chép tiếng Anh sang ngôn ngữ. Một ngôn ngữ nhất định sẽ được hoàn thành sau này.
    # Sau khi được dịch hoàn toàn, nó sẽ được thêm vào phạm vi bảo trì độc lập tại đây.
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    tips = (
        locales.get(tips_language, {}).get("Translation", {}).get(provider.tips_key, "")
    )
    if not tips:
        return tips

    service_endpoint = provider.preferred_service_endpoint(
        prefer_international=tips_language == "en"
    )
    api_key_url = (
        service_endpoint.api_key_url
        if service_endpoint
        else provider.effective_api_key_url()
    )
    format_context = {
        "api_key_url": api_key_url,
        "default_model": provider.default_model,
        "default_base_url": (
            service_endpoint.base_url
            if service_endpoint
            else provider.effective_default_base_url
        ),
        "model_docs_url": service_endpoint.model_docs_url if service_endpoint else "",
        **{
            f"default_{field.config_suffix}": field.default_value
            for field in provider.extra_fields
        },
        **kwargs,
    }
    try:
        return tips.format(**format_context)
    except Exception as e:
        logger.warning(f"format llm provider tips failed: {provider_id}, {e}")
        return tips


def format_llm_connection_error(provider_id, base_url, error):
    """Bổ sung các đề xuất kiểm tra cấu hình đối với các lỗi xác thực được bản địa hóa rõ ràng trong khi vẫn giữ nguyên các phản hồi ban đầu."""
    error_text = str(error or "").strip()
    normalized_error = error_text.lower()
    authentication_markers = (
        "401",
        "authentication",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    provider = get_llm_provider(provider_id)
    if provider is None or not provider.service_endpoints or not any(
        marker in normalized_error for marker in authentication_markers
    ):
        return error_text

    message = tr_optional(
        provider.authentication_error_key,
        fallback_language="en",
    )
    if not message:
        return error_text
    return message.format(base_url=base_url or "-", error=error_text)


def get_llm_provider_label(provider):
    return tr_optional(provider.label_key) or provider.default_label


def get_tts_provider_tips(provider_id):
    # Hướng dẫn cấu hình TTS áp dụng chiến lược bảo trì giống như Nhà cung cấp LLM: chỉ duy trì tiếng Trung và tiếng Anh.
    # Các ngôn ngữ giao diện khác chuyển về tiếng Anh để tránh tình trạng không đồng bộ hóa lâu dài sau khi sao chép.
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    return (
        locales.get(tips_language, {})
        .get("Translation", {})
        .get(f"tts_provider_tips.{provider_id}", "")
    )


def localized_widget_key(name, *parts):
    # Một số hộp chọn Streamlit sử dụng các phím ổn định để ghi nhớ trạng thái lựa chọn nhưng hiển thị văn bản từ ngôn ngữ.
    # 语言切换时把语言也放进 key，可以强制重建控件，避免选中项仍显示旧语言。
    language = st.session_state.get("ui_language", config.ui.get("language", ""))
    suffix_parts = [name, language, *[str(part) for part in parts if part]]
    return "_".join(suffix_parts)


def stable_selectbox(label, options, default_value, key, format_func=None, **kwargs):
    # Streamlit 1.59 对 selectbox 的状态复用更敏感：如果控件没有固定 key，
    # 或者真实选项只是一组临时下标，页面 rerun 后容易被重新计算的 index 覆盖，
    # Hiệu suất là lựa chọn đầu tiên của người dùng không có hiệu lực và cần phải được chọn lại. Trình trợ giúp này sử dụng các giá trị kinh doanh ổn định một cách thống nhất
    # Là một tùy chọn thực sự và lưu giá trị trong session_state; bản sao hiển thị chỉ vượt qua format_func
    # Chuyển đổi để tránh sao chép bản dịch, thứ tự tùy chọn hoặc thay đổi cấu hình ngược dòng ảnh hưởng đến trạng thái lựa chọn.
    options = list(options)
    if not options:
        raise ValueError(f"selectbox options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    accepts_custom_value = bool(kwargs.get("accept_new_options"))
    has_valid_custom_value = (
        accepts_custom_value
        and isinstance(selected_value, str)
        and bool(selected_value.strip())
    )
    if selected_value not in options and not has_valid_custom_value:
        # Nếu các tùy chọn ngược dòng thay đổi (ví dụ: danh sách âm thanh thay đổi sau khi chuyển đổi nhà cung cấp TTS),
        # Giá trị cũ không còn hiệu lực. Khởi tạo session_state trực tiếp trước khi điều khiển được tạo và sau đó chỉ để khóa
        # Trạng thái quản lý không còn được chuyển sang chỉ mục cùng một lúc. Điều này tránh Streamlit khi chạy lại
        # Giá trị vừa được người dùng chọn sẽ bị ghi đè bằng chỉ số được tính toán lại, khiến lựa chọn đầu tiên không có hiệu lực.
        st.session_state[widget_key] = default_value

    if format_func is None:
        format_func = str

    return st.selectbox(
        label,
        options=options,
        format_func=format_func,
        key=widget_key,
        **kwargs,
    )


# Hộp chọn gốc của Streamlit hiện không hỗ trợ nhóm chọn lọc HTML. Ở đây chúng tôi sử dụng cái đi kèm với 1,59
# Các thành phần v2 đóng gói <select>/<optgroup> gốc mà không đưa vào các phần phụ thuộc giao diện người dùng trong khi vẫn giữ lại trình duyệt
# Điều hướng bàn phím gốc, ngữ nghĩa có thể truy cập và trải nghiệm lựa chọn trên thiết bị di động. Thành phần chỉ chuyển các giá trị nghiệp vụ cố định và văn bản đã dịch,
# Không nhận bất kỳ HTML nào và ngăn nội dung cấu hình nhập vào bên trongHTML ở ranh giới.
_GROUPED_SELECT_COMPONENT = st.components.v2.component(
    "mpt_grouped_select",
    html="""
        <div class="mpt-grouped-select">
            <div class="mpt-grouped-select__label-row">
                <label class="mpt-grouped-select__label"></label>
                <button class="mpt-grouped-select__settings" type="button"></button>
            </div>
            <div class="mpt-grouped-select__control">
                <select></select>
            </div>
        </div>
    """,
    css="""
        .mpt-grouped-select {
            width: 100%;
            color: var(--st-text-color);
            font-family: var(--st-font);
        }

        .mpt-grouped-select__label-row {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.45rem;
            margin-bottom: 0.35rem;
        }

        .mpt-grouped-select__label {
            font-size: 0.875rem;
            line-height: 1.25rem;
        }

        .mpt-grouped-select__settings {
            padding: 0;
            border: 0;
            background: transparent;
            color: var(--st-link-color);
            font: inherit;
            font-size: 0.8rem;
            line-height: 1.25rem;
            cursor: pointer;
        }

        .mpt-grouped-select__settings:hover {
            text-decoration: underline;
            text-underline-offset: 0.15rem;
        }

        .mpt-grouped-select__settings:focus-visible {
            border-radius: 0.2rem;
            outline: 2px solid var(--st-primary-color);
            outline-offset: 2px;
        }

        .mpt-grouped-select__control {
            position: relative;
        }

        .mpt-grouped-select__control::after {
            position: absolute;
            top: 50%;
            right: 1rem;
            width: 0.55rem;
            height: 0.55rem;
            border-right: 2px solid currentColor;
            border-bottom: 2px solid currentColor;
            content: "";
            pointer-events: none;
            transform: translateY(-70%) rotate(45deg);
        }

        .mpt-grouped-select select {
            width: 100%;
            min-height: 2.5rem;
            padding: 0.45rem 2.75rem 0.45rem 0.75rem;
            border: 1px solid color-mix(in srgb, currentColor 20%, transparent);
            border-radius: 0.5rem;
            outline: none;
            appearance: none;
            background: var(--st-secondary-background-color);
            color: inherit;
            font: inherit;
            cursor: pointer;
        }

        .mpt-grouped-select select:hover {
            border-color: color-mix(in srgb, currentColor 36%, transparent);
        }

        .mpt-grouped-select select:focus-visible {
            border-color: var(--st-primary-color);
            box-shadow: 0 0 0 1px var(--st-primary-color);
        }
    """,
    js="""
        export default function(component) {
            const { data, parentElement, setTriggerValue } = component;
            const label = parentElement.querySelector("label");
            const settings = parentElement.querySelector(".mpt-grouped-select__settings");
            const select = parentElement.querySelector("select");

            label.textContent = data.label;
            settings.textContent = data.settingsLabel;
            settings.hidden = !data.settingsLabel;
            select.id = data.controlId;
            label.htmlFor = data.controlId;
            select.setAttribute("aria-label", data.label);
            select.replaceChildren();

            for (const groupData of data.groups) {
                const group = document.createElement("optgroup");
                group.label = groupData.label;
                for (const optionData of groupData.options) {
                    const option = document.createElement("option");
                    option.value = optionData.value;
                    option.textContent = optionData.label;
                    group.appendChild(option);
                }
                select.appendChild(group);
            }

            select.value = data.value;
            const handleChange = () => {
                setTriggerValue("selected", select.value);
            };
            const handleSettings = () => {
                setTriggerValue("settings", true);
            };
            select.addEventListener("change", handleChange);
            settings.addEventListener("click", handleSettings);

            return () => {
                select.removeEventListener("change", handleChange);
                settings.removeEventListener("click", handleSettings);
            };
        }
    """,
)


def grouped_selectbox(
    label,
    groups,
    default_value,
    key,
    format_func=None,
    settings_label="",
    on_settings=None,
):
    """Hiển thị một hộp thả xuống duy nhất có tiêu đề nhóm không thể chọn và trả về giá trị doanh nghiệp ổn định."""
    if format_func is None:
        format_func = str

    normalized_groups = []
    valid_values = []
    for group_label, options in groups:
        normalized_options = []
        for option in options:
            valid_values.append(option)
            normalized_options.append(
                {"value": option, "label": str(format_func(option))}
            )
        if normalized_options:
            normalized_groups.append(
                {"label": str(group_label), "options": normalized_options}
            )

    if not valid_values:
        raise ValueError(f"grouped selectbox options cannot be empty: {key}")
    if len(set(valid_values)) != len(valid_values):
        raise ValueError(f"grouped selectbox options must be unique: {key}")
    if default_value not in valid_values:
        default_value = valid_values[0]

    # Các lựa chọn doanh nghiệp được lưu trong cùng khóa phiên với hộp chọn cũ, cài đặt khôi phục cài sẵn và
    # Logic chuyển đổi ngôn ngữ không cần phải phân nhánh; bản thân thành phần đó sử dụng một khóa độc lập để tránh xung đột với trạng thái kinh doanh.
    widget_key = localized_widget_key(key)
    if widget_key not in st.session_state:
        st.session_state[widget_key] = default_value
    selected_value = st.session_state[widget_key]
    if selected_value not in valid_values:
        selected_value = default_value
        st.session_state[widget_key] = selected_value

    result = _GROUPED_SELECT_COMPONENT(
        key=f"{widget_key}_component",
        data={
            "label": label,
            "settingsLabel": settings_label,
            # Liên kết rõ ràng các nhãn hiển thị với các lựa chọn gốc. Khóa thành phần bao gồm tên doanh nghiệp cố định và
            # Nó bao gồm các mã ngôn ngữ và là duy nhất trong trang. Thật thuận tiện cho việc nhấp chuột để tập trung vào các điều khiển nhãn.
            # Nó cũng không đưa ra các ID ngẫu nhiên khiến trạng thái giao diện người dùng được xây dựng lại mỗi lần chạy lại.
            "controlId": f"{widget_key}_control",
            "value": selected_value,
            "groups": normalized_groups,
        },
        on_selected_change=lambda: None,
        on_settings_change=on_settings or (lambda: None),
    )
    changed_value = getattr(result, "selected", None)
    if changed_value in valid_values and changed_value != selected_value:
        st.session_state[widget_key] = changed_value
        # Thành phần v2 Khi vòng tập lệnh hiện tại trả về một sự kiện, dữ liệu được chuyển đến giao diện người dùng trong vòng này vẫn là
        # Giá trị cũ trước khi sự kiện xảy ra. Tự động chạy lại ngay lập tức, cho phép các thành phần và điều khiển phụ thuộc vào video_source
        # Giá trị mới được nhận cùng lúc; nếu không hộp thả xuống sẽ bị dữ liệu cũ ghi đè trong thời gian ngắn và người dùng chỉ được chọn một lần.
        st.rerun()

    return selected_value


def sync_script_order_concat_mode():
    """Đã sửa lỗi sử dụng nối tuần tự khi bật khớp chuỗi sao chép và khôi phục lựa chọn ban đầu khi tắt."""
    widget_key = localized_widget_key("video_concat_mode_select")
    previous_key = "video_concat_mode_before_script_order_match"
    match_script_order = bool(st.session_state.get("match_materials_to_script", False))

    if match_script_order:
        current_mode = st.session_state.get(widget_key, VideoConcatMode.random.value)
        if current_mode != VideoConcatMode.sequential.value:
            st.session_state[previous_key] = current_mode
        st.session_state[widget_key] = VideoConcatMode.sequential.value
        return

    previous_mode = st.session_state.pop(previous_key, None)
    if previous_mode in {
        VideoConcatMode.sequential.value,
        VideoConcatMode.random.value,
    }:
        st.session_state[widget_key] = previous_mode


def reset_script_system_prompt():
    """将高级脚本设置中的系统提示词恢复为当前版本的默认内容。"""
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT


def reset_subtitle_settings():
    """Khôi phục các giá trị mặc định trong điều khiển phụ đề WebUI và cấu hình lưu giữ."""
    defaults = DEFAULT_SUBTITLE_SETTINGS
    st.session_state["subtitle_enabled_checkbox"] = defaults["subtitle_enabled"]
    _set_stable_widget_value("font_name_select", defaults["font_name"])
    _set_stable_widget_value("subtitle_position_select", defaults["subtitle_position"])
    _set_stable_widget_value(
        "subtitle_display_mode_select", defaults["subtitle_display_mode"]
    )
    _set_stable_widget_value(
        "subtitle_animation_select", defaults["subtitle_animation"]
    )
    st.session_state["custom_position_input"] = str(defaults["custom_position"])
    st.session_state["font_color_picker"] = defaults["text_fore_color"]
    st.session_state["font_size_slider"] = defaults["font_size"]
    st.session_state["stroke_color_picker"] = defaults["stroke_color"]
    st.session_state["stroke_width_slider"] = defaults["stroke_width"]
    st.session_state["subtitle_background_enabled_checkbox"] = defaults[
        "subtitle_background_enabled"
    ]
    st.session_state["subtitle_background_color_picker"] = defaults[
        "subtitle_background_color"
    ]
    st.session_state["rounded_subtitle_background_checkbox"] = defaults[
        "rounded_subtitle_background"
    ]

    # 同步会持久化的 UI 选项，确保恢复后刷新页面仍保持默认设置。
    for key in (
        "subtitle_enabled",
        "font_name",
        "subtitle_position",
        "subtitle_display_mode",
        "subtitle_animation",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "subtitle_background_enabled",
        "subtitle_background_color",
        "rounded_subtitle_background",
    ):
        if key in defaults:
            _set_runtime_config("ui", key, defaults[key])


@st.dialog(tr("Final Prompt Preview"), width="large")
def render_script_prompt_preview(prompt):
    """展示将要发送给大模型的完整脚本生成提示词。"""
    st.code(prompt, language="markdown", wrap_lines=True)


def stable_segmented_control(
    label, options, default_value, key, format_func=None, **kwargs
):
    """Sử dụng các giá trị nghiệp vụ ổn định để tạo các điều khiển phân đoạn chọn radio nhằm ngăn trạng thái bị ghi đè bởi bản sao hiển thị sau khi chuyển đổi ngôn ngữ."""
    options = list(options)
    if not options:
        raise ValueError(f"segmented control options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default_value

    return st.segmented_control(
        label,
        options=options,
        selection_mode="single",
        required=True,
        format_func=format_func or str,
        key=widget_key,
        **kwargs,
    )


@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (
        (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    )
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _get_material_api_keys(config_key):
    """Chuyển đổi Khóa API vật liệu trong cấu hình thành chuỗi có thể chỉnh sửa WebUI."""
    api_keys = config.app.get(config_key, [])
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    return ", ".join(api_keys)


def _save_material_api_keys(config_key, value):
    """保存逗号分隔的素材 API Key，并允许用户显式清空旧配置。"""
    normalized_value = value.replace(" ", "")
    _set_runtime_config(
        "app",
        config_key,
        normalized_value.split(",") if normalized_value else [],
    )


def _format_file_size(size_bytes):
    """Định dạng số byte thành văn bản nhỏ gọn phù hợp để hiển thị trên trang cài đặt."""
    size = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


@st.cache_data(ttl=30, show_spinner=False)
def _get_video_cache_stats(max_age_days=None):
    """
    短周期缓存目录统计，避免设置弹窗内普通控件交互反复扫描大量文件。

    Khóa bộ đệm chứa số ngày dọn dẹp, do đó phạm vi chuyển đổi sẽ chỉ quét một lần trên mỗi phạm vi; chủ động làm mới hoặc dọn dẹp
    Nó sẽ bị xóa rõ ràng khi hoàn tất và bộ đệm trong tối đa 30 giây sẽ không ảnh hưởng đến quá trình quét thứ cấp trong quá trình xóa thực tế.
    """
    return cache_manager.get_video_cache_stats(max_age_days=max_age_days)


def _render_cache_management_settings(panel):
    """Kết xuất các hoạt động thống kê, xem trước và dọn dẹp bảo mật cho bộ đệm tài liệu video trực tuyến mặc định."""
    with panel:
        cleanup_message = st.session_state.pop("video_cache_cleanup_message", None)
        if cleanup_message:
            message_type, message = cleanup_message
            if message_type == "success":
                st.success(message)
            else:
                st.warning(message)

        st.caption(tr("Video Cache Directory"))
        st.code(cache_manager.video_cache_dir(), language="text")

        total_stats = _get_video_cache_stats()
        metric_count, metric_size, metric_oldest = st.columns(3)
        metric_count.metric(tr("Cache File Count"), total_stats.file_count)
        metric_size.metric(
            tr("Cache Total Size"), _format_file_size(total_stats.total_size)
        )
        oldest_text = (
            datetime.fromtimestamp(total_stats.oldest_mtime).strftime("%Y-%m-%d")
            if total_stats.oldest_mtime is not None
            else "-"
        )
        metric_oldest.metric(tr("Oldest Cache Date"), oldest_text)

        st.caption(tr("Video Cache Management Help"))
        cleanup_options = (30, 7, 90, None)
        cleanup_labels = {
            30: tr("Cache Older Than 30 Days"),
            7: tr("Cache Older Than 7 Days"),
            90: tr("Cache Older Than 90 Days"),
            None: tr("All Video Cache"),
        }
        max_age_days = st.selectbox(
            tr("Cache Cleanup Range"),
            options=cleanup_options,
            format_func=lambda value: cleanup_labels[value],
            key="video_cache_cleanup_range",
        )
        cleanup_preview = _get_video_cache_stats(max_age_days=max_age_days)
        st.info(
            tr("Cache Cleanup Preview").format(
                count=cleanup_preview.file_count,
                size=_format_file_size(cleanup_preview.total_size),
            )
        )

        confirm_nonce = st.session_state.get("video_cache_cleanup_confirm_nonce", 0)
        confirmed = st.checkbox(
            tr("Confirm Cache Cleanup"),
            key=f"video_cache_cleanup_confirm_{confirm_nonce}",
        )
        refresh_col, open_col, cleanup_col = st.columns(3)
        if refresh_col.button(
            tr("Refresh Cache Stats"),
            key="refresh_video_cache_stats",
            use_container_width=True,
            icon=":material/refresh:",
        ):
            _get_video_cache_stats.clear()
            st.rerun(scope="fragment")

        if open_col.button(
            tr("Open Cache Directory"),
            key="open_video_cache_directory",
            use_container_width=True,
            icon=":material/folder_open:",
        ):
            webbrowser.open(Path(cache_manager.video_cache_dir()).as_uri())

        cleanup_disabled = not confirmed or cleanup_preview.file_count == 0
        if cleanup_col.button(
            tr("Clean Cache Now"),
            key="clean_video_cache_now",
            type="primary",
            disabled=cleanup_disabled,
            use_container_width=True,
            icon=":material/delete_sweep:",
        ):
            result = cache_manager.clean_video_cache(max_age_days=max_age_days)
            message_key = (
                "Cache Cleanup Completed With Failures"
                if result.failed_count
                else "Cache Cleanup Completed"
            )
            st.session_state["video_cache_cleanup_message"] = (
                "warning" if result.failed_count else "success",
                tr(message_key).format(
                    count=result.deleted_count,
                    size=_format_file_size(result.deleted_size),
                    failed=result.failed_count,
                ),
            )
            # Streamlit không cho phép sửa đổi session_state có cùng tên sau khi điều khiển được khởi tạo. bằng cách tăng dần
            # nonce cho phép chạy lại đoạn tiếp theo để tạo các điều khiển mới không được kiểm tra để tránh phải dọn dẹp sau khi hoàn thành
            # Trạng thái xác nhận nguy hiểm được giữ lại.
            st.session_state["video_cache_cleanup_confirm_nonce"] = confirm_nonce + 1
            _get_video_cache_stats.clear()
            st.rerun(scope="fragment")


# -----------------------------------------------------------------------------
# Thiết lập xuất, nhập và sao lưu khóa mặc định
# -----------------------------------------------------------------------------


def _is_credential_config_key(key):
    """Xác định xem tên mục cấu hình có đại diện cho thông tin xác thực hay không."""
    return str(key).endswith(CREDENTIAL_KEY_SUFFIXES)


def _is_backup_config_key(section_name, key):
    """Bản thân thông tin xác thực và các mục cấu hình hỗ trợ của nó là một phần của phạm vi sao lưu khóa."""
    if _is_credential_config_key(key):
        return True
    if key in CREDENTIAL_COMPANION_KEYS.get(section_name, ()):
        return True
    return key in NON_LLM_COMPANION_KEYS.get(section_name, ())


def _credential_widget_state_keys(section_name, key):
    """
    Trả về tất cả các khóa điều khiển Streamlit tương ứng với một mục cấu hình thông tin xác thực nhất định.

    密码输入框都带 key，Streamlit 中 session_state 的值优先于控件的 value
    参数。恢复备份后必须清除这些残留控件状态，否则页面会继续显示旧密钥，
    Và ghi lại các giá trị cũ vào cấu hình trong lần chạy lại tiếp theo, khiến cho việc khôi phục dường như không hiệu quả. nhiều tấm
    共用同一个密钥时会各自持有控件状态，因此返回默认 key 和全部别名。
    """
    if section_name == "app":
        default_widget_key = f"{key}_input"
    else:
        default_widget_key = f"{section_name}_{key}_input"
    return (
        default_widget_key,
        *CREDENTIAL_WIDGET_STATE_ALIASES.get((section_name, key), ()),
    )


def _normalize_backup_value(value):
    """Chuẩn hóa các giá trị sao lưu và loại bỏ các chuỗi trống cũng như danh sách trống để tránh ghi đè các cấu hình trống trong quá trình khôi phục."""
    if isinstance(value, list):
        items = [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _collect_key_backup(config_sections):
    """Thu thập tất cả các khóa được điền và các mục cấu hình đi kèm của chúng từ phân vùng cấu hình thời gian chạy."""
    backup = {}
    for section_name, section in config_sections.items():
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        entries = {}
        for key, value in section.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                entries[key] = normalized_value
        if entries:
            backup[section_name] = entries
    return backup


def _count_backup_keys(backup):
    """Đếm số lượng mục cấu hình trong bản sao lưu, mục này được sử dụng cho lời nhắc giao diện và vô hiệu hóa các mục xuất trống."""
    return sum(len(entries) for entries in backup.values())


def _build_key_backup_payload(config_sections, app_version):
    """Xây dựng nội dung tập tin sao lưu chính."""
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(app_version),
        "keys": _collect_key_backup(config_sections),
    }


def _load_transfer_payload(raw_bytes, schema, version):
    """
    Phân tích tệp xuất và xác minh rằng nó thực sự có cùng phiên bản của tính năng này.

    用户可能上传任意 JSON。这里只接受声明了正确 schema 和版本的文件，让错误
    Lời nhắc vẫn ở mục nhập thay vì ghi nội dung không thể nhận dạng vào trạng thái cấu hình hoặc điều khiển.
    Các trình soạn thảo Windows có thể lưu JSON bằng BOM và do đó giải mã dưới dạng utf-8-sig.
    """
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("exported file must contain a JSON object")
    if payload.get("schema") != schema:
        raise ValueError(f"unexpected schema: {payload.get('schema')!r}")
    if payload.get("version") != version:
        raise ValueError(f"unsupported version: {payload.get('version')!r}")
    return payload


def _parse_key_backup(raw_bytes, config_sections):
    """
    Phân tích tệp sao lưu khóa và chỉ giữ lại các phân vùng và mục cấu hình được phiên bản hiện tại nhận dạng.

    Tệp sao lưu có thể được chỉnh sửa thủ công hoặc có thể từ phiên bản mới hơn. Các phân vùng không xác định hoặc các mục cấu hình không có khóa luôn
    Bỏ qua để tránh ghi đè cấu hình không liên quan đến thông tin xác thực thông qua chức năng nhập.
    """
    payload = _load_transfer_payload(raw_bytes, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("key backup file has no keys object")

    restored = {}
    for section_name, entries in keys.items():
        if section_name not in config_sections:
            continue
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        if not isinstance(entries, dict):
            continue
        section_entries = {}
        for key, value in entries.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                section_entries[key] = normalized_value
        if section_entries:
            restored[section_name] = section_entries

    if not restored:
        raise ValueError("key backup file contains no restorable keys")
    return restored


def _build_settings_preset_payload(params, app_version):
    """Xây dựng nội dung của tệp mặc định tham số xây dựng."""
    preset_params = {
        key: value
        for key, value in params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    if params.get("bgm_type") == "preset" and params.get("bgm_file"):
        try:
            builtin_bgm_path = bgm_service.resolve_builtin_bgm_file(
                str(params["bgm_file"])
            )
        except ValueError:
            # Các tệp tùy chỉnh là tài nguyên gốc và không thể nhập vào cài đặt trước cài đặt di động. Duy trì trong tình huống bất thường
            # Hành vi loại trừ tồn tại để tránh xuất các tệp chứa đường dẫn tuyệt đối hoặc UUID không tồn tại trên thiết bị khác.
            pass
        else:
            preset_params["bgm_file"] = Path(builtin_bgm_path).name
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(app_version),
        "params": preset_params,
    }


def _parse_settings_preset(raw_bytes):
    """
    Phân tích tệp cài sẵn và gửi tới VideoParams để xác minh.

    Các cài đặt trước có thể được tạo trên các máy khác hoặc được chỉnh sửa thủ công. Xác minh mô hình thống nhất có thể sử dụng lại
    Ràng buộc phạm vi giá trị, các giá trị đặt trước không hợp lệ sẽ bị từ chối khi nhập, thay vì bị lỗi khi tác vụ được tạo.
    """
    payload = _load_transfer_payload(
        raw_bytes, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION
    )
    preset_params = payload.get("params")
    if not isinstance(preset_params, dict):
        raise ValueError("settings preset file has no params object")

    params_input = {
        key: value
        for key, value in preset_params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    if preset_params.get("bgm_type") == "preset" and preset_params.get("bgm_file"):
        # Đặt giá trị đặt trước chỉ có thể khôi phục các bài hát cài sẵn thực sự tồn tại trong phiên bản hiện tại. Lớp dịch vụ cũng từ chối dấu phân cách thư mục
        # Tải tệp lên cùng với người dùng để ngăn các tệp đã nhập đọc đường dẫn cục bộ tùy ý thông qua chức năng nghe.
        builtin_bgm_path = bgm_service.resolve_builtin_bgm_file(
            str(preset_params["bgm_file"])
        )
        params_input["bgm_file"] = Path(builtin_bgm_path).name
    # video_subject là trường bắt buộc của VideoParams, nhưng cài đặt trước chỉ cho phép lưu cài đặt kiểu.
    params_input.setdefault("video_subject", "")
    return VideoParams.model_validate(params_input).model_dump(mode="json")


def _apply_key_backup(restored_keys):
    """Ghi khóa được phân tích cú pháp trở lại cấu hình thời gian chạy và xóa trạng thái còn lại của điều khiển tương ứng."""
    restored_count = 0
    for section_name, entries in restored_keys.items():
        for key, value in entries.items():
            _set_runtime_config(section_name, key, value)
            for widget_key in _credential_widget_state_keys(section_name, key):
                st.session_state.pop(widget_key, None)
            restored_count += 1
    # Danh sách âm thanh ElevenLabs được lưu vào bộ nhớ đệm theo khóa và phải được kéo lại sau khi thay đổi sang bản sao lưu khác.
    for cache_key in list(st.session_state.keys()):
        if str(cache_key).startswith("elevenlabs_voices_"):
            del st.session_state[cache_key]
    return restored_count


def _apply_pending_settings_preset():
    """Áp dụng các cài đặt trước đã nhập trước khi hiển thị bất kỳ điều khiển nào."""
    preset_params = st.session_state.pop("settings_preset_payload", None)
    if not preset_params:
        return False

    _apply_restored_params(preset_params)
    logger.info("applied imported settings preset")
    return True


def _render_settings_transfer(params):
    """Cổng xuất và nhập để hiển thị và tạo các cài đặt trước tham số."""
    with st.expander(tr("Settings Preset"), expanded=False):
        st.caption(tr("Settings Preset Help"))
        preset_payload = _build_settings_preset_payload(
            params.model_dump(mode="json"), config.project_version
        )
        st.download_button(
            tr("Export Settings"),
            data=json.dumps(preset_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=SETTINGS_PRESET_FILE_NAME,
            mime="application/json",
            use_container_width=True,
            key="export_settings_preset_button",
            icon=":material/download:",
        )
        uploaded_preset = st.file_uploader(
            tr("Import Settings"),
            type=["json"],
            key="settings_preset_uploader",
        )
        if uploaded_preset is None:
            return
        # Tệp đã tải lên sẽ xuất hiện lại mỗi lần nó được chạy lại. Ghi lại nhận dạng tập tin đã xử lý,
        # Điều này giúp người dùng không bị ghi đè nhiều lần bởi cùng một cài đặt trước sau khi thay đổi điều khiển.
        if st.session_state.get("settings_preset_file_id") == uploaded_preset.file_id:
            return

        st.session_state["settings_preset_file_id"] = uploaded_preset.file_id
        try:
            preset_params = _parse_settings_preset(uploaded_preset.getvalue())
        except Exception as e:
            logger.warning(f"failed to import settings preset: {e}")
            st.error(tr("Settings Preset Import Failed"))
            return

        st.session_state["settings_preset_payload"] = preset_params
        st.rerun()


def _render_key_backup_settings(panel):
    """Xuất và khôi phục cổng thông tin để hiển thị bản sao lưu khóa."""
    with panel:
        backup_message = st.session_state.pop("key_backup_message", None)
        if backup_message:
            message_type, message = backup_message
            if message_type == "success":
                st.success(message)
            else:
                st.error(message)

        st.caption(tr("Key Backup Help"))
        st.warning(tr("Key Backup Warning"))

        backup_payload = _build_key_backup_payload(
            _RUNTIME_CONFIG_SECTIONS, config.project_version
        )
        backup_key_count = _count_backup_keys(backup_payload["keys"])
        st.caption(tr("Key Backup Summary").format(count=backup_key_count))
        st.download_button(
            tr("Export Keys"),
            data=json.dumps(backup_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=KEY_BACKUP_FILE_NAME,
            mime="application/json",
            disabled=backup_key_count == 0,
            use_container_width=True,
            key="export_key_backup_button",
            icon=":material/download:",
        )

        uploaded_backup = st.file_uploader(
            tr("Import Keys"),
            type=["json"],
            key="key_backup_uploader",
        )
        if uploaded_backup is None:
            return
        if st.session_state.get("key_backup_file_id") == uploaded_backup.file_id:
            return

        st.session_state["key_backup_file_id"] = uploaded_backup.file_id
        try:
            restored_keys = _parse_key_backup(
                uploaded_backup.getvalue(), _RUNTIME_CONFIG_SECTIONS
            )
        except Exception as e:
            logger.warning(f"failed to import key backup: {e}")
            st.session_state["key_backup_message"] = (
                "error",
                tr("Key Restore Failed"),
            )
        else:
            restored_count = _apply_key_backup(restored_keys)
            _save_runtime_config()
            logger.info(f"restored keys from backup file: count={restored_count}")
            st.session_state["key_backup_message"] = (
                "success",
                tr("Keys Restored").format(count=restored_count),
            )
        # Ô nhập key TTS trên trang chính cũng cần đọc cấu hình đã khôi phục nên toàn bộ trang được làm mới.
        # 设置弹窗的打开状态保存在 session_state 中，刷新后会重新展开。
        st.rerun(scope="app")


# -----------------------------------------------------------------------------
# Cài đặt và cửa sổ bật lên từ nhắc nhở
# -----------------------------------------------------------------------------


# Cài đặt là hoạt động tần số thấp. Sử dụng Hộp thoại cỡ trung bình để tránh chiếm không gian dọc của trang chính trong thời gian dài.
# Đồng thời, kiểm soát độ rộng dòng đọc để tránh hiện tượng cửa sổ pop-up xuất hiện quá lỏng lẻo trên các thiết bị màn hình rộng.
# Hộp thoại kế thừa hành vi phân đoạn và tương tác kiểm soát nội bộ chỉ vẽ lại cửa sổ bật lên; cấu hình được lưu riêng ở cuối chức năng.
# Kích hoạt đồng bộ hóa toàn trang thông qua lệnh gọi lại khi đóng để đảm bảo rằng quá trình tạo sẽ đọc cài đặt giao diện và Nhà cung cấp mới nhất.
@st.dialog(
    tr("Settings"),
    width="medium",
    on_dismiss=_dismiss_settings_dialog,
)
def _render_settings_dialog():
    with st.container():
        # 历史 hide_config 只用于隐藏旧基础设置面板。改为固定设置入口后，该值
        # Nó không còn có ý nghĩa mà người dùng có thể nhìn thấy và được chuyển đồng bộ sang false để ngăn cấu hình cũ ảnh hưởng đến các phiên bản tiếp theo.
        _set_runtime_config("app", "hide_config", False)
        settings_tab_labels = [
            tr("LLM Settings Tab"),
            tr("Material API Tab"),
            tr("Auto-Publish Settings"),
            tr("Interface Settings Tab"),
            tr("Key Backup Tab"),
            tr("Cache Management Tab"),
        ]
        settings_tab_targets = {
            "llm": tr("LLM Settings Tab"),
            "material": tr("Material API Tab"),
        }
        settings_tabs_key = localized_widget_key("settings_dialog_tabs")
        target_tab = st.session_state.pop("settings_dialog_target_tab", None)
        if target_tab in settings_tab_targets:
            # st.tabs sử dụng nhãn hiển thị làm giá trị trạng thái. Nút vào chỉ lưu ID doanh nghiệp ổn định.
            # 到这里再写入当前语言的 label，即可精确定位且兼容语言切换。
            st.session_state[settings_tabs_key] = settings_tab_targets[target_tab]

        (
            middle_config_panel,
            right_config_panel,
            publish_config_panel,
            left_config_panel,
            key_backup_panel,
            cache_config_panel,
        ) = st.tabs(
            settings_tab_labels,
            key=settings_tabs_key,
            on_change="rerun",
        )

        with publish_config_panel:
            st.write(tr("Automatically publish generated videos to social media using upload-post.com"))
            st.info(
                tr("Upload-Post Setup Guide").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL,
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL,
                )
            )

            is_enabled = config.app.get("upload_post_enabled", False)
            is_auto = config.app.get("upload_post_auto_upload", False)

            # Hai khóa này độc lập: được bật cho phép các quy trình bên ngoài gọi Tải lên-Bài đăng,
            # auto_upload xác định xem có tự động xuất bản sau khi kết xuất hoàn tất hay không. Kết hợp thành một hộp kiểm sẽ ở
            # Trong cấu hình có hai phím không nhất quán, chỉ cần mở hộp thoại cài đặt và ghi lại kích hoạt thành Sai.
            upload_post_enabled = st.checkbox(
                tr("Enable Upload-Post Integration"),
                value=is_enabled,
                key="upload_post_enabled_checkbox"
            )
            if upload_post_enabled != is_enabled:
                _set_runtime_config("app", "upload_post_enabled", upload_post_enabled)

            upload_post_auto_upload = st.checkbox(
                tr("Enable Auto-Publish"),
                value=is_auto,
                key="upload_post_auto_upload_checkbox"
            )
            if upload_post_auto_upload != is_auto:
                _set_runtime_config("app", "upload_post_auto_upload", upload_post_auto_upload)

            upload_post_api_key = st.text_input(
                tr("Upload-Post API Key"),
                value=config.app.get("upload_post_api_key", ""),
                type="password",
                help=tr("Upload-Post API Key Help").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL
                ),
                key="upload_post_api_key_input"
            )
            if upload_post_api_key != config.app.get("upload_post_api_key", ""):
                _set_runtime_config("app", "upload_post_api_key", upload_post_api_key)

            upload_post_username = st.text_input(
                tr("Upload-Post Profile Username"),
                value=config.app.get("upload_post_username", ""),
                help=tr("Upload-Post Profile Username Help").format(
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL
                ),
                key="upload_post_username_input"
            )
            if upload_post_username != config.app.get("upload_post_username", ""):
                _set_runtime_config("app", "upload_post_username", upload_post_username)

            upload_post_platforms = st.multiselect(
                tr("Platforms"),
                options=["tiktok", "instagram", "youtube"],
                default=config.app.get("upload_post_platforms", ["tiktok", "instagram"]),
                help="Select platforms to publish to",
                key="upload_post_platforms_multiselect"
            )
            if upload_post_platforms != config.app.get("upload_post_platforms", ["tiktok", "instagram"]):
                _set_runtime_config("app", "upload_post_platforms", upload_post_platforms)

            if "youtube" in upload_post_platforms:
                yt_status_options = ["public", "private", "unlisted"]
                yt_saved = config.app.get("upload_post_youtube_privacy_status", "public")
                if yt_saved not in yt_status_options:
                    yt_saved = "public"
                upload_post_youtube_privacy_status = st.selectbox(
                    tr("YouTube Privacy Status"),
                    options=yt_status_options,
                    index=yt_status_options.index(yt_saved),
                    key="upload_post_youtube_privacy_status_selectbox"
                )
                if upload_post_youtube_privacy_status != config.app.get("upload_post_youtube_privacy_status", "public"):
                    _set_runtime_config("app", "upload_post_youtube_privacy_status", upload_post_youtube_privacy_status)

                # Tuyên bố về đối tượng chỉ ảnh hưởng đến việc xuất bản trên YouTube và không thay đổi các yêu cầu đối với nội dung được tạo hoặc các nền tảng khác.
                # Sử dụng các tùy chọn boolean thực sự và tránh coi văn bản hoặc chuỗi hiển thị dưới dạng tham số API.
                saved_audience = config.app.get("upload_post_youtube_made_for_kids", False)
                audience_labels = {False: tr("Not Made for Kids"), True: tr("Made for Kids")}
                made_for_kids = st.selectbox(
                    tr("YouTube Audience"),
                    options=[False, True],
                    # 非法配置保持未选择，不在打开设置时擅自改成非儿童声明。
                    index=int(saved_audience) if isinstance(saved_audience, bool) else None,
                    format_func=audience_labels.get,
                    help=tr("YouTube Audience Help"),
                    key="upload_post_youtube_made_for_kids_selectbox",
                )
                if isinstance(made_for_kids, bool):
                    _set_runtime_config("app", "upload_post_youtube_made_for_kids", made_for_kids)

        # Bảng điều khiển bên trái - Cài đặt nhật ký
        with left_config_panel:
            hide_log = st.checkbox(
                tr("Hide Log"),
                value=config.ui.get("hide_log", False),
                key="hide_log_checkbox",
            )
            _set_runtime_config("ui", "hide_log", hide_log)

        _render_cache_management_settings(cache_config_panel)
        # 密钥恢复会写回配置并清除密码控件状态，必须在下面渲染这些控件之前执行。
        _render_key_backup_settings(key_backup_panel)

        # Bảng giữa - Thiết lập LLM

        with middle_config_panel:
            # Thứ tự thả xuống, nhãn mặc định và id nhà cung cấp ổn định đều đến từ Cơ quan đăng ký; ngôn ngữ
            # 只覆盖展示文案，不再让 Main.py 维护第二份 Provider 列表。
            llm_provider_ids = [
                provider.provider_id for provider in LLM_PROVIDER_REGISTRY
            ]
            llm_provider_labels = {
                provider.provider_id: get_llm_provider_label(provider)
                for provider in LLM_PROVIDER_REGISTRY
            }
            saved_llm_provider = config.app.get(
                "llm_provider", DEFAULT_LLM_PROVIDER_ID
            ).lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = DEFAULT_LLM_PROVIDER_ID

            llm_provider = stable_selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                default_value=saved_llm_provider,
                key="llm_provider_select",
                format_func=lambda provider_id: llm_provider_labels[provider_id],
            )
            # Hiển thị biểu mẫu cấu hình và mô tả Nhà cung cấp cạnh nhau, giảm ngắt dòng trong các mô tả dài trong các cột hẹp.
            # Đồng thời, tận dụng tối đa không gian theo chiều ngang của bảng cài đặt cơ bản.
            llm_form_panel, llm_help_panel = st.columns(
                [0.9, 1.1],
                gap="large",
                vertical_alignment="top",
            )
            llm_helper = llm_help_panel.container()
            _set_runtime_config("app", "llm_provider", llm_provider)
            llm_provider_spec = get_llm_provider(llm_provider)
            if llm_provider_spec is None:
                # Trong trường hợp bình thường, tất cả các tùy chọn thả xuống đều đến từ Cơ quan đăng ký và sẽ không vào nhánh này; kín đáo
                # Các lỗi rõ ràng được sử dụng để chẩn đoán trạng thái phiên bị hỏng hoặc lần truy cập tiếp theo bị bỏ lỡ.
                raise RuntimeError(f"unsupported llm provider: {llm_provider}")

            llm_api_key = config.app.get(llm_provider_spec.config_key("api_key"), "")
            configured_llm_base_url = config.app.get(
                llm_provider_spec.config_key("base_url"), ""
            )
            llm_default_base_url = llm_provider_spec.effective_default_base_url
            llm_base_url = configured_llm_base_url or llm_default_base_url
            llm_model_name = llm_provider_spec.resolve_model_name(
                config.app.get(llm_provider_spec.config_key("model_name"), "")
            )

            provider_tip_context = {}
            selected_service_endpoint = None
            if llm_provider_spec.service_endpoints:
                # Các nhà cung cấp như Kimi sử dụng các hệ thống tài khoản khác nhau cho các trang web Trung Quốc và quốc tế của họ. Chỉ cho phép người dùng
                # Chọn khu vực dịch vụ, sau đó sử dụng API đồng bộ hóa sổ đăng ký để áp dụng cho cổng vào và URL cơ sở.
                # Tránh lỗi lắp ráp thủ công. Nếu có cấu hình URL cơ sở trống, trang tiếng Trung sẽ tiếp tục được sử dụng. Chỉ một
                # Đối với các cấu hình mới chưa điền Key, mục nhập tương ứng sẽ được đề xuất dựa trên ngôn ngữ giao diện.
                selected_service_endpoint = (
                    llm_provider_spec.select_service_endpoint(
                        configured_llm_base_url,
                        has_api_key=bool(str(llm_api_key).strip()),
                        prefer_international=(
                            st.session_state.get("ui_language", "en") != "zh"
                        ),
                    )
                )
                endpoint_options = [
                    endpoint.endpoint_id
                    for endpoint in llm_provider_spec.service_endpoints
                ] + [CUSTOM_LLM_ENDPOINT_ID]
                default_endpoint_id = (
                    selected_service_endpoint.endpoint_id
                    if selected_service_endpoint
                    else CUSTOM_LLM_ENDPOINT_ID
                )
                endpoint_labels = {
                    endpoint.endpoint_id: (
                        tr_optional(
                            llm_provider_spec.endpoint_label_key(endpoint.endpoint_id),
                            fallback_language="en",
                        )
                        or endpoint.default_label
                    )
                    for endpoint in llm_provider_spec.service_endpoints
                }
                endpoint_labels[CUSTOM_LLM_ENDPOINT_ID] = (
                    tr_optional("Custom API Endpoint", fallback_language="en")
                    or "Custom API Endpoint"
                )
                with llm_form_panel:
                    selected_endpoint_id = stable_selectbox(
                        tr_optional(
                            llm_provider_spec.endpoint_selector_label_key,
                            fallback_language="en",
                        )
                        or tr("API Platform"),
                        options=endpoint_options,
                        default_value=default_endpoint_id,
                        key=f"{llm_provider}_service_endpoint_select",
                        format_func=lambda endpoint_id: endpoint_labels[endpoint_id],
                        help=(
                            tr_optional(
                                llm_provider_spec.endpoint_selector_help_key,
                                fallback_language="en",
                            )
                            or None
                        ),
                    )
                selected_service_endpoint = next(
                    (
                        endpoint
                        for endpoint in llm_provider_spec.service_endpoints
                        if endpoint.endpoint_id == selected_endpoint_id
                    ),
                    None,
                )
                if selected_service_endpoint:
                    llm_base_url = selected_service_endpoint.base_url
                    provider_tip_context.update(
                        {
                            "api_key_url": selected_service_endpoint.api_key_url,
                            "default_base_url": selected_service_endpoint.base_url,
                            "model_docs_url": selected_service_endpoint.model_docs_url,
                        }
                    )
                else:
                    # Chế độ tùy chỉnh chỉ giữ lại các địa chỉ được người dùng lưu rõ ràng và không ngụy trang khu vực tiêu chuẩn
                    # thành một giá trị tùy chỉnh. Khi đầu vào trống, cấu hình sẽ không được duy trì và sẽ trở về mặc định tương thích vào lần tiếp theo.
                    llm_base_url = str(configured_llm_base_url or "").strip()

            if llm_provider == "ollama":
                llm_default_base_url = config.get_default_ollama_base_url()
                if not llm_base_url:
                    llm_base_url = llm_default_base_url
                docker_hint = ""
                if config.is_running_in_container():
                    docker_hint = tr_optional(
                        "llm_provider_tips.ollama.docker_hint",
                        fallback_language="en",
                    )
                provider_tip_context["docker_hint"] = docker_hint

            tips = get_llm_provider_tips(llm_provider, **provider_tip_context)
            if tips:
                with llm_helper:
                    st.info(tips)

            st_llm_api_key = llm_api_key
            if llm_provider_spec.show_api_key:
                st_llm_api_key = llm_form_panel.text_input(
                    tr("API Key"),
                    value=llm_api_key,
                    type="password",
                    key=f"{llm_provider}_api_key_input",
                )

            st_llm_base_url = llm_base_url
            if llm_provider_spec.show_base_url:
                st_llm_base_url = llm_form_panel.text_input(
                    tr("Base Url"),
                    value=llm_base_url,
                    key=(
                        f"{llm_provider}_base_url_"
                        f"{selected_service_endpoint.endpoint_id}_input"
                        if selected_service_endpoint
                        else f"{llm_provider}_base_url_custom_input"
                    ),
                    disabled=selected_service_endpoint is not None,
                )
            st_llm_model_name = ""
            if llm_provider == "groq":
                effective_api_key = st_llm_api_key or llm_api_key
                effective_base_url = st_llm_base_url or llm_base_url
                groq_models = get_groq_model_ids(
                    api_key=effective_api_key,
                    base_url=effective_base_url,
                )

                if groq_models:
                    selected_index = 0
                    if llm_model_name in groq_models:
                        selected_index = groq_models.index(llm_model_name)

                    st_llm_model_name = llm_form_panel.selectbox(
                        tr("Model Name"),
                        options=groq_models,
                        index=selected_index,
                        key="groq_model_name_select",
                    )
                else:
                    st_llm_model_name = llm_form_panel.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key="groq_model_name_input",
                    )
                    if effective_api_key:
                        llm_form_panel.caption(tr("Groq Model List Load Failed"))
                    else:
                        llm_form_panel.caption(
                            tr("Groq API Key Required for Model List")
                        )
            else:
                st_llm_model_name = llm_form_panel.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
            # Hộp nhập hiển thị giá trị mặc định của Sổ đăng ký, nhưng cấu hình chỉ lưu giá trị ghi đè thực tế của người dùng.
            # Bằng cách này, sau khi mô hình mặc định và URL cơ sở được cập nhật, những người dùng chưa tùy chỉnh có thể tự động theo dõi chúng.
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("api_key"),
                st_llm_api_key,
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("base_url"),
                normalize_provider_override(
                    st_llm_base_url,
                    llm_default_base_url,
                ),
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("model_name"),
                normalize_provider_override(
                    st_llm_model_name,
                    llm_provider_spec.default_model,
                ),
            )

            # Các trường dành riêng cho nhà cung cấp cũng được Cơ quan đăng ký khai báo. Ví dụ Cổng AI của Cloudflare
            # ID tài khoản là bắt buộc; không cần phải thêm phán đoán trong Main.py khi thêm các trường tương tự trong tương lai.
            for field in llm_provider_spec.extra_fields:
                field_config_key = llm_provider_spec.config_key(field.config_suffix)
                field_value = llm_form_panel.text_input(
                    tr(field.label_key),
                    value=(config.app.get(field_config_key, "") or field.default_value),
                    type="password" if field.secret else "default",
                    key=f"{llm_provider}_{field.config_suffix}_input",
                )
                _set_runtime_config(
                    "app",
                    field_config_key,
                    normalize_provider_override(
                        field_value,
                        field.default_value,
                    ),
                )

            if llm_provider == "gemini":
                thinking_options = [
                    "HIGH (Tư duy sâu - Khuyên dùng)",
                    "MEDIUM (Trung bình)",
                    "LOW (Thấp)",
                    "OFF (Tắt suy nghĩ)",
                ]
                current_thinking = str(
                    config.app.get("gemini_thinking_level", "high")
                ).lower()
                index_map = {"high": 0, "medium": 1, "low": 2, "off": 3}
                sel_idx = index_map.get(current_thinking, 0)
                selected_thinking = llm_form_panel.selectbox(
                    "Mức độ suy nghĩ (Thinking Level)",
                    options=thinking_options,
                    index=sel_idx,
                    key="gemini_thinking_level_select",
                    help="Gemini 3.8 Flash hỗ trợ cơ chế suy nghĩ (Thinking) để kịch bản sâu sắc, logic và tránh hời hợt.",
                )
                level_code = ["high", "medium", "low", "off"][
                    thinking_options.index(selected_thinking)
                ]
                _set_runtime_config("app", "gemini_thinking_level", level_code)

            if llm_form_panel.button(
                tr("Test LLM Connection"),
                key="test_llm_connection_button",
                use_container_width=True,
                type="secondary",
                icon=":material/network_check:",
            ):
                with config.try_runtime_config_lock() as lock_acquired:
                    if not lock_acquired:
                        llm_form_panel.warning(tr("Runtime Configuration Busy"))
                    else:
                        with llm_form_panel.spinner(tr("Testing LLM Connection")):
                            connection_ok, connection_error, connection_elapsed = (
                                llm.test_connection()
                            )

                if not lock_acquired:
                    connection_ok = None
                elif connection_ok:
                    llm_form_panel.success(
                        tr("LLM Connection Test Succeeded").format(
                            provider=llm_provider_labels[llm_provider],
                            model=st_llm_model_name or "-",
                            elapsed=f"{connection_elapsed:.2f}",
                        )
                    )
                else:
                    connection_error = format_llm_connection_error(
                        llm_provider,
                        st_llm_base_url,
                        connection_error,
                    )
                    llm_form_panel.error(
                        tr("LLM Connection Test Failed").format(error=connection_error)
                    )

        # Bảng bên phải - Cài đặt khóa API
        with right_config_panel:
            # Nhà cung cấp vật liệu Nhấp vào "Tìm kiếm tài liệu trong kho/video do AI tạo/hình ảnh do AI tạo"
            # Nhóm để tránh tất cả các trường bị trộn lẫn trong một danh sách dài khi số lượng Nhà cung cấp tăng lên.
            # Việc nhóm chỉ điều chỉnh mức độ hiển thị và không thay đổi các phím cấu hình hiện có. Sau khi nâng cấp, người dùng cũ
            # Giá trị config.toml ban đầu sẽ tiếp tục được đọc.
            with st.container(border=True):
                st.markdown(f"#### {tr('Stock Video APIs')}")
                st.caption(tr("Stock Video APIs Help"))

                pexels_api_key = _get_material_api_keys("pexels_api_keys")
                pixabay_api_key = _get_material_api_keys("pixabay_api_keys")
                coverr_api_key = _get_material_api_keys("coverr_api_keys")
                pexels_api_key = st.text_input(
                    tr("Pexels API Key"),
                    value=pexels_api_key,
                    type="password",
                    key="pexels_api_keys_input",
                )
                _save_material_api_keys("pexels_api_keys", pexels_api_key)

                pixabay_api_key = st.text_input(
                    tr("Pixabay API Key"),
                    value=pixabay_api_key,
                    type="password",
                    key="pixabay_api_keys_input",
                )
                _save_material_api_keys("pixabay_api_keys", pixabay_api_key)

                coverr_api_key = st.text_input(
                    tr("Coverr API Key"),
                    value=coverr_api_key,
                    type="password",
                    key="coverr_api_keys_input",
                )
                _save_material_api_keys("coverr_api_keys", coverr_api_key)

            with st.container(border=True):
                st.markdown(f"#### {tr('AI Video Generation APIs')}")
                st.caption(tr("AI Video Generation APIs Help"))

                # Nhà cung cấp dịch vụ tạo video hiển thị đầu tiên theo nhà tài trợ, theo thứ tự trong nhà tài trợ
                # Phù hợp với VIDEO_SOURCE_GROUPS: Tháp bí mật, OOX, Đám mây tỷ lệ, Động cơ núi lửa.
                st.markdown(f"**{tr('Metaso MiniMax H3')}**")
                metaso_api_key = st.text_input(
                    tr("Metaso MiniMax API Key"),
                    value=str(
                        config.app.get("metaso_minimax_api_key", "") or ""
                    ).strip(),
                    type="password",
                    help=tr("Metaso MiniMax API Key Help"),
                    key="metaso_minimax_api_key_input",
                )
                _set_runtime_config(
                    "app", "metaso_minimax_api_key", metaso_api_key.strip()
                )
                configured_metaso_base_url = str(
                    config.app.get(
                        "metaso_minimax_base_url",
                        metaso_minimax.DEFAULT_BASE_URL,
                    )
                    or metaso_minimax.DEFAULT_BASE_URL
                ).strip()
                metaso_base_url = st.text_input(
                    tr("Metaso MiniMax Base URL"),
                    value=(
                        ""
                        if configured_metaso_base_url == metaso_minimax.DEFAULT_BASE_URL
                        else configured_metaso_base_url
                    ),
                    placeholder=metaso_minimax.DEFAULT_BASE_URL,
                    key="metaso_minimax_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "metaso_minimax_base_url",
                    metaso_base_url.strip() or metaso_minimax.DEFAULT_BASE_URL,
                )
                configured_metaso_resolution = (
                    str(
                        config.app.get(
                            "metaso_minimax_resolution",
                            metaso_minimax.DEFAULT_RESOLUTION,
                        )
                    )
                    .strip()
                    .upper()
                )
                metaso_resolution_options = sorted(
                    metaso_minimax.SUPPORTED_RESOLUTIONS,
                    key=lambda value: value != metaso_minimax.DEFAULT_RESOLUTION,
                )
                resolution_is_valid = (
                    configured_metaso_resolution
                    in metaso_minimax.SUPPORTED_RESOLUTIONS
                )
                if not resolution_is_valid:
                    # Độ phân giải ảnh hưởng trực tiếp đến việc thanh toán. Trường hợp cấu hình thủ công bị lỗi, giữ nguyên giá trị ban đầu và hỏi người dùng
                    # Đây là một lựa chọn chủ động và không thể âm thầm thay đổi thành 2K đắt tiền hơn khi cửa sổ bật lên cài đặt được mở.
                    st.error(
                        tr("Metaso MiniMax Invalid Resolution").format(
                            value=configured_metaso_resolution,
                            supported=", ".join(metaso_resolution_options),
                        )
                    )
                metaso_resolution = st.selectbox(
                    tr("Metaso MiniMax Resolution"),
                    options=metaso_resolution_options,
                    index=(
                        metaso_resolution_options.index(configured_metaso_resolution)
                        if resolution_is_valid
                        else None
                    ),
                    key="metaso_minimax_resolution_input",
                    help=tr("Metaso MiniMax Resolution Help"),
                    placeholder=tr("Select Metaso MiniMax Resolution"),
                )
                if metaso_resolution is not None:
                    _set_runtime_config(
                        "app", "metaso_minimax_resolution", metaso_resolution
                    )

                st.divider()
                st.markdown("**OfoxAI**")
                st.caption(f"[OfoxAI]({OFOX_REFERRAL_URL}) · {tr('OFox AI Video Help')}")
                ofox_api_key = st.text_input(
                    tr("OFox API Key"),
                    value=str(config.app.get("ofox_api_key", "") or ""),
                    type="password",
                    key="ofox_api_key_input",
                )
                _set_runtime_config("app", "ofox_api_key", ofox_api_key.strip())
                ofox_model = st.text_input(
                    tr("OFox Text-to-Video Model"),
                    value=str(
                        config.app.get(
                            "ofox_text_to_video_model",
                            ofox.DEFAULT_MODEL_ID,
                        )
                        or ofox.DEFAULT_MODEL_ID
                    ),
                    key="ofox_text_to_video_model_input",
                )
                _set_runtime_config(
                    "app", "ofox_text_to_video_model", ofox_model.strip()
                )
                configured_ofox_base_url = str(
                    config.app.get("ofox_base_url", ofox.DEFAULT_BASE_URL)
                    or ofox.DEFAULT_BASE_URL
                ).strip()
                ofox_base_url = st.text_input(
                    tr("OFox Base URL"),
                    value=(
                        ""
                        if configured_ofox_base_url == ofox.DEFAULT_BASE_URL
                        else configured_ofox_base_url
                    ),
                    placeholder=ofox.DEFAULT_BASE_URL,
                    key="ofox_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "ofox_base_url",
                    ofox_base_url.strip() or ofox.DEFAULT_BASE_URL,
                )
                ofox_vendor_options = [
                    (tr("OFox Vendor BytePlus"), "byteplus"),
                    (tr("OFox Vendor Volcengine"), "volcengine"),
                    (tr("OFox Vendor Auto"), ""),
                ]
                configured_ofox_vendor = str(
                    config.app.get("ofox_provider", ofox.DEFAULT_PROVIDER_TYPE)
                    or ""
                ).strip()
                if configured_ofox_vendor not in {
                    value for _, value in ofox_vendor_options
                }:
                    # Giữ lựa chọn này khi người dùng ghim tên nhà cung cấp khác theo cách thủ công trong config.toml.
                    # Tránh bị ghi đè trở lại giá trị mặc định bởi hộp thả xuống khi mở trang cài đặt.
                    ofox_vendor_options.append(
                        (configured_ofox_vendor, configured_ofox_vendor)
                    )
                selected_ofox_vendor = stable_selectbox(
                    tr("OFox Upstream Vendor"),
                    options=[value for _, value in ofox_vendor_options],
                    default_value=configured_ofox_vendor,
                    key="ofox_provider_select",
                    format_func=lambda value: dict(
                        (v, label) for label, v in ofox_vendor_options
                    )[value],
                    help=tr("OFox Upstream Vendor Help"),
                )
                _set_runtime_config("app", "ofox_provider", selected_ofox_vendor)

                st.divider()
                st.markdown(f"**{tr('Shengsuan Cloud AI Video')}**")
                app_config_snapshot = config.snapshot_config_with_pending(config.app)
                if (
                    str(app_config_snapshot.get("llm_provider", "") or "").lower()
                    == "shengsuanyun"
                ):
                    # Khi Nhà cung cấp mô hình ngôn ngữ lớn (LLM) được chọn để giành được đám mây, quá trình tạo video sẽ được sử dụng lại trực tiếp.
                    # Đối với cùng một khóa, hộp nhập độc lập dễ bị mơ hồ sẽ không còn được hiển thị.
                    st.caption(tr("Shengsuan Cloud API Key Reused"))
                else:
                    configured_loomloom_token = str(
                        app_config_snapshot.get("loomloom_api_token", "") or ""
                    ).strip()
                    loomloom_api_token = st.text_input(
                        tr("Shengsuan Cloud API Key"),
                        value=configured_loomloom_token,
                        type="password",
                        key="loomloom_api_token_input",
                        help=tr("Shengsuan Cloud API Key Help"),
                        placeholder=tr("Shengsuan Cloud API Key Placeholder"),
                    ).strip()
                    _set_runtime_config(
                        "app", "loomloom_api_token", loomloom_api_token
                    )

                st.divider()
                seedance_api_key_value = str(
                    config.app.get("volcengine_seedance_api_key", "") or ""
                ).strip()
                shared_ark_api_key = str(
                    config.app.get("volcengine_api_key", "") or ""
                ).strip()
                environment_ark_api_key = os.getenv(
                    "VOLCENGINE_ARK_API_KEY", ""
                ).strip()
                seedance_reuses_llm_key = bool(
                    not seedance_api_key_value
                    and not environment_ark_api_key
                    and shared_ark_api_key
                )
                seedance_title = f"**{tr('Volcano Engine Seedance')}**"
                if seedance_reuses_llm_key:
                    # Chỉ không thể nhìn thấy trực tiếp khóa mô hình ngôn ngữ lớn (LLM) được sử dụng lại từ hộp nhập hiện tại, vì vậy hãy giữ lời nhắc này.
                    # Điều này có thể giúp người dùng không nhầm tưởng rằng họ phải điền thông tin nhiều lần; trạng thái cấu hình chung sẽ không được mô tả lại.
                    seedance_title += f" :blue[{tr('Reusing LLM API Key')}]"
                st.markdown(seedance_title)
                seedance_api_key = st.text_input(
                    tr("Volcano Engine Ark API Key"),
                    value=seedance_api_key_value,
                    type="password",
                    help=tr("Volcano Engine Ark API Key Help"),
                    key="volcengine_seedance_api_key_input",
                )
                _set_runtime_config(
                    "app", "volcengine_seedance_api_key", seedance_api_key.strip()
                )
                configured_seedance_model = str(
                    config.app.get(
                        "volcengine_seedance_model",
                        volcengine_seedance.DEFAULT_MODEL_ID,
                    )
                    or volcengine_seedance.DEFAULT_MODEL_ID
                ).strip()
                seedance_model = st.text_input(
                    tr("Volcano Engine Seedance Model"),
                    # Các giá trị mặc định tích hợp được hiển thị thông qua phần giữ chỗ, do người dùng xác định
                    # ID mô hình hoặc điểm truy cập vẫn được hiển thị và lưu dưới dạng giá trị thực.
                    value=(
                        ""
                        if configured_seedance_model
                        == volcengine_seedance.DEFAULT_MODEL_ID
                        else configured_seedance_model
                    ),
                    placeholder=volcengine_seedance.DEFAULT_MODEL_ID,
                    key="volcengine_seedance_model_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_model",
                    seedance_model.strip() or volcengine_seedance.DEFAULT_MODEL_ID,
                )
                configured_seedance_base_url = str(
                    config.app.get(
                        "volcengine_seedance_base_url",
                        volcengine_seedance.DEFAULT_BASE_URL,
                    )
                    or volcengine_seedance.DEFAULT_BASE_URL
                ).strip()
                seedance_base_url = st.text_input(
                    tr("Volcano Engine Ark Base URL"),
                    value=(
                        ""
                        if configured_seedance_base_url
                        == volcengine_seedance.DEFAULT_BASE_URL
                        else configured_seedance_base_url
                    ),
                    placeholder=volcengine_seedance.DEFAULT_BASE_URL,
                    key="volcengine_seedance_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_base_url",
                    seedance_base_url.strip() or volcengine_seedance.DEFAULT_BASE_URL,
                )

                st.divider()
                wavespeed_api_key = _get_material_api_keys("wavespeed_api_keys")
                st.markdown("**WaveSpeed**")
                wavespeed_api_key = st.text_input(
                    tr("WaveSpeed API Key"),
                    value=wavespeed_api_key,
                    type="password",
                    key="wavespeed_api_keys_input",
                )
                _save_material_api_keys("wavespeed_api_keys", wavespeed_api_key)


            with st.container(border=True):
                st.markdown(f"#### {tr('AI Image Generation APIs')}")
                st.caption(tr("AI Image Generation APIs Help"))
                st.markdown(f"**{tr('OpenAI Compatible Text-to-Image')}**")

                openai_image_base_url = st.text_input(
                    tr("OpenAI Image Base URL"),
                    value=str(config.app.get("openai_image_base_url", "") or ""),
                    placeholder="https://api.openai.com/v1",
                    key="openai_image_base_url_input",
                )
                _set_runtime_config(
                    "app", "openai_image_base_url", openai_image_base_url.strip()
                )

                openai_image_api_key = _get_material_api_keys(
                    "openai_image_api_keys"
                )
                openai_image_api_key = st.text_input(
                    tr("OpenAI Image API Key"),
                    value=openai_image_api_key,
                    type="password",
                    help=tr("OpenAI Image API Key Help"),
                    key="openai_image_api_keys_input",
                )
                _save_material_api_keys(
                    "openai_image_api_keys", openai_image_api_key
                )

                openai_image_model = st.text_input(
                    tr("OpenAI Image Model"),
                    value=str(config.app.get("openai_image_model", "") or ""),
                    placeholder="gpt-image-2",
                    key="openai_image_model_input",
                )
                _set_runtime_config(
                    "app", "openai_image_model", openai_image_model.strip()
                )
                # Chỉ các giá trị tham chiếu được hiển thị và các điểm cuối chính thức của OpenAI không được ghi dưới dạng cấu hình mặc định.
                # Không có giá trị thống nhất cho URL cơ sở và ID mẫu của các dịch vụ tương thích; để trống sẽ không
                # Nếu người dùng kết nối nhầm vào giao diện thanh toán chính thức mà không hề hay biết thì cấu hình cũ sẽ không bị ghi đè.
                st.caption(tr("OpenAI Image Configuration Example"))

                with st.expander(
                    tr("OpenAI Image Advanced Settings"), expanded=False
                ):
                    openai_image_size = st.text_input(
                        tr("OpenAI Image Size"),
                        value=str(config.app.get("openai_image_size", "") or ""),
                        placeholder="1024x1536",
                        help=tr("OpenAI Image Size Help"),
                        key="openai_image_size_input",
                    )
                    _set_runtime_config(
                        "app", "openai_image_size", openai_image_size.strip()
                    )

                    openai_image_prompt_template = st.text_input(
                        tr("OpenAI Image Prompt Template"),
                        value=str(
                            config.app.get("openai_image_prompt_template", "") or ""
                        ),
                        placeholder="cinematic photo of {term}, photorealistic",
                        help=tr("OpenAI Image Prompt Template Help"),
                        key="openai_image_prompt_template_input",
                    )
                    _set_runtime_config(
                        "app",
                        "openai_image_prompt_template",
                        openai_image_prompt_template.strip(),
                    )

    _save_runtime_config()


# -----------------------------------------------------------------------------
# 主生成表单：文案、视频、音频与字幕面板
# -----------------------------------------------------------------------------


def _create_loomloom_script_backend():
    """Tạo ứng dụng khách sao chép hàng loạt từ cấu hình WebUI/config.toml hiện tại."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.LoomLoomSettings.from_mapping(app_config_snapshot)
    return loomloom.LoomLoomScriptBackend(settings)


def _create_loomloom_video_backend():
    """Tạo ứng dụng khách video bằng SkillBot mặc định của dự án và thông tin xác thực hiện hợp lệ."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.video_settings_from_mapping(app_config_snapshot)
    return loomloom.LoomLoomVideoBackend(settings)


def _effective_loomloom_api_token():
    """Đọc Khóa API Winning Cloud chưa được đặt trong WebUI hoặc trong config.toml."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    return loomloom.resolve_api_token(app_config_snapshot)


def _effective_script_generation_backend():
    """Đọc phương pháp tạo bản sao chép có chứa các thay đổi sẽ được lưu trong WebUI."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    backend = str(
        app_config_snapshot.get("script_generation_backend", "local") or "local"
    ).strip()
    return backend if backend in {"local", "loomloom"} else "local"




def _script_generation_method_help(selected_backend):
    """Hãy để nội dung dấu chấm hỏi của “Phương pháp tạo bản sao chép” tuân thủ nghiêm ngặt sự lựa chọn hiện tại."""
    if selected_backend != "loomloom":
        return tr("Script Generation Method Help")

    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    guidance = [tr("LoomLoom Batch Script Generation Help")]
    if (
        str(app_config_snapshot.get("llm_provider", "") or "").strip().lower()
        == "shengsuanyun"
    ):
        guidance.append(tr("Shengsuan Cloud API Key Reused"))
    guidance.append(tr("Shengsuan Cloud API Key Link"))
    return "\n\n".join(guidance)


def _loomloom_video_scene_prompts(video_terms, subject, scene_count):
    """Một số lượng mô tả cảnh giới hạn được tạo dựa trên từ khóa nội dung cho mô hình video để tạo phân đoạn nội dung theo phân đoạn."""
    if isinstance(video_terms, str):
        terms = [
            term.strip() for term in re.split(r"[,，\n]", video_terms) if term.strip()
        ]
    elif isinstance(video_terms, list):
        terms = [
            str(term or "").strip() for term in video_terms if str(term or "").strip()
        ]
    else:
        terms = []
    fallback = str(subject or "").strip()
    if not terms and fallback:
        terms = [fallback]
    if not terms:
        return ()
    return tuple(
        (
            terms[index % len(terms)]
            if index < len(terms)
            else f"{terms[index % len(terms)]}; alternative camera angle {index + 1}"
        )
        for index in range(int(scene_count))
    )


def _loomloom_video_signature(batch, credential_fingerprint):
    """Kết hợp tất cả thông tin đầu vào về hóa đơn và bản tóm tắt chứng từ vào chữ ký và buộc báo giá lại sau khi thay đổi thông số."""
    payload = {
        "inputRows": [dict(row) for row in batch.input_rows],
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _loomloom_video_account_signature(token):
    """服务地址和凭据共同隔离模型目录及报价，不能跨端点复用已确认状态。"""
    values = config.snapshot_config_with_pending(config.app)
    base_url = str(values.get("loomloom_base_url") or loomloom.DEFAULT_BASE_URL).strip().rstrip("/")
    payload = json.dumps([base_url, str(token or "").strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_loomloom_video_capability(token, *, force=False):
    """Hồ sơ bộ đệm theo thông tin xác thực hiện tại; giữ lại kết quả thành công gần đây nhất cho cùng một thông tin xác thực khi làm mới không thành công."""
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return None

    fingerprint = _loomloom_video_account_signature(normalized_token)
    if st.session_state.get("loomloom_video_capability_fingerprint") != fingerprint:
        st.session_state["loomloom_video_capability"] = None
        st.session_state["loomloom_video_capability_fingerprint"] = fingerprint
        st.session_state["loomloom_video_capability_load_attempt"] = ""
        st.session_state["loomloom_video_capability_error"] = ""

    should_load = force or (
        st.session_state.get("loomloom_video_capability_load_attempt") != fingerprint
    )
    if should_load:
        st.session_state["loomloom_video_capability_load_attempt"] = fingerprint
        try:
            capability = _create_loomloom_video_backend().resolve_video_capability()
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(
                f"failed to load LoomLoom video capability: error={type(exc).__name__}"
            )
            st.session_state["loomloom_video_capability_error"] = str(exc)
        else:
            st.session_state["loomloom_video_capability"] = capability
            st.session_state["loomloom_video_capability_error"] = ""

    capability = st.session_state.get("loomloom_video_capability")
    return (
        capability if isinstance(capability, loomloom.LoomLoomVideoCapability) else None
    )


def _normalize_loomloom_model_identifier(value):
    """Thống nhất các dấu phân cách và kiểu chữ của tên hiển thị và ID mẫu để khớp an toàn trong bảng giá địa phương."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _loomloom_video_model_price(model):
    """Trả về mô hình đã biết (giá ngắn trong hộp thả xuống, giá tham chiếu đầy đủ sau khi chọn); trả về giá trị null cho mô hình chưa biết."""
    identifiers = {
        _normalize_loomloom_model_identifier(model.model_id),
        _normalize_loomloom_model_identifier(model.display_name),
    }
    for aliases, compact_price, detailed_price in LOOMLOOM_VIDEO_MODEL_PRICES:
        if identifiers.intersection(aliases):
            return compact_price, detailed_price
    return "", ""


def _format_loomloom_video_model_option(model):
    """Hiển thị một mức giá ngắn ở bên phải tên mẫu máy để tránh việc nhiều mức giá phân giải khiến hộp thả xuống quá rộng."""
    compact_price, _ = _loomloom_video_model_price(model)
    return f"{model.display_name} · {compact_price}" if compact_price else model.display_name


def _effective_voice_rate_before_audio_panel():
    """Bảng điều khiển video được đặt trước bảng âm thanh và cần đọc tốc độ giọng nói hiện tại từ trạng thái hoặc cấu hình điều khiển hiện có."""
    raw_rate = st.session_state.get(
        localized_widget_key("voice_rate_select"),
        config.ui.get("voice_rate", 1.0),
    )
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return rate if math.isfinite(rate) and rate > 0 else 1.0


def _matching_full_voice_preview_duration(script, voice_rate):
    """Thời lượng thực tế của buổi thử giọng hoàn chỉnh sẽ chỉ được sử dụng khi nội dung, nhà cung cấp, âm sắc và tốc độ nói không thay đổi."""
    cached = st.session_state.get("voice_preview_audio")
    if not isinstance(cached, dict) or cached.get("preview_type") != "full":
        return None
    script_digest = hashlib.sha256(str(script or "").encode("utf-8")).hexdigest()
    if cached.get("content_digest") != script_digest:
        return None

    current_tts_server = st.session_state.get(
        localized_widget_key("tts_server_select"),
        config.ui.get("tts_server", "azure-tts-v1"),
    )
    current_voice_name = st.session_state.get(
        localized_widget_key(f"speech_synthesis_select_{current_tts_server}"),
        config.ui.get("voice_name", ""),
    )
    try:
        cached_voice_rate = float(cached.get("voice_rate", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        cached.get("tts_server") != current_tts_server
        or cached.get("voice_name") != current_voice_name
        or not math.isfinite(cached_voice_rate)
        or not math.isclose(cached_voice_rate, voice_rate)
    ):
        return None

    duration = cached.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None
    return float(duration)


def _loomloom_video_coverage_plan(params):
    """Số lượng tài liệu được đề xuất dựa trên thời lượng tường thuật thực tế hoặc ước tính; những phần còn thiếu vẫn được bổ sung bằng logic vòng lặp ban đầu."""
    script = str(params.video_script or "").strip()
    if not script:
        return None

    voice_rate = _effective_voice_rate_before_audio_panel()
    actual_duration = _matching_full_voice_preview_duration(script, voice_rate)
    if actual_duration is not None:
        duration_min = duration_max = actual_duration
        basis_key = "AI Video Duration Basis Actual"
    else:
        estimated = _estimate_voiceover_duration_range(script, voice_rate)
        if not estimated:
            return None
        duration_min, duration_max = estimated
        basis_key = "AI Video Duration Basis Estimated"

    clip_duration = max(float(params.video_clip_duration or 1), 1.0)
    needed_min = max(math.ceil(duration_min / clip_duration), 1)
    needed_max = max(math.ceil(duration_max / clip_duration), needed_min)
    return {
        "script_digest": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "basis_key": basis_key,
        "duration_min": float(duration_min),
        "duration_max": float(duration_max),
        "clip_duration": clip_duration,
        "needed_min": needed_min,
        "needed_max": needed_max,
        # Giá trị được đề xuất trước tiên bao gồm giới hạn trên thận trọng nhưng sẽ không bao giờ vượt quá giới hạn trên của các tác vụ phải trả phí được máy chủ cho phép.
        "recommended_count": min(needed_max, loomloom.MAX_VIDEO_SCENES),
    }


def _format_numeric_range(minimum, maximum, digits=1):
    if math.isclose(float(minimum), float(maximum)):
        return f"{float(maximum):.{digits}f}"
    return f"{float(minimum):.{digits}f}–{float(maximum):.{digits}f}"


def _selected_loomloom_video_model(capability):
    """Trả về lựa chọn người dùng vẫn nằm trong số các ứng cử viên Hồ sơ hiện tại mà không cần khôi phục im lặng."""
    selected_model_id = str(
        st.session_state.get("loomloom_video_model_id", "") or ""
    ).strip()
    eligible_model_ids = {model.model_id for model in capability.models}
    return selected_model_id if selected_model_id in eligible_model_ids else ""


def _current_loomloom_video_quote_context(params):
    """Xây dựng loạt trích dẫn video mặc định của SkillBot dựa trên thông số trang hiện tại."""
    token = _effective_loomloom_api_token()
    fingerprint = _loomloom_video_account_signature(token) if token else ""
    capability = st.session_state.get("loomloom_video_capability")
    if (
        not isinstance(capability, loomloom.LoomLoomVideoCapability)
        or st.session_state.get("loomloom_video_capability_fingerprint") != fingerprint
    ):
        return None, ""
    model_id = _selected_loomloom_video_model(capability)
    scene_count = int(st.session_state.get("loomloom_video_scene_count", 1) or 1)
    prompts = _loomloom_video_scene_prompts(
        params.video_terms,
        params.video_subject or params.video_script,
        scene_count,
    )
    aspect_ratio = str(
        params.video_aspect.value
        if isinstance(params.video_aspect, VideoAspect)
        else params.video_aspect
    )
    if (
        not token
        or not model_id
        or not prompts
        or aspect_ratio not in capability.aspect_ratios
    ):
        return None, ""
    try:
        batch = _create_loomloom_video_backend().prepare_video_batch(
            subject=params.video_subject or params.video_script,
            scene_prompts=prompts,
            model_id=model_id,
            aspect_ratio=aspect_ratio,
        )
    except (loomloom.LoomLoomError, ValueError):
        return None, ""
    return batch, _loomloom_video_signature(batch, fingerprint)


def _retry_loomloom_video_quote():
    """Khóa lỗi được giải phóng khi người dùng chủ động thử lại; xác nhận thanh toán trước đó không được sử dụng."""
    st.session_state["loomloom_video_quote_error_signature"] = ""
    st.session_state["loomloom_video_quote_error"] = ""
    st.session_state["loomloom_video_confirm_charge"] = False


def _render_loomloom_video_settings(params):
    """Hiển thị video mặc định của SkillBot, quá trình vô hiệu hóa báo giá và xác nhận thanh toán."""
    st.caption(tr("Shengsuan Cloud AI Video Help"))
    if (
        str(
            config.snapshot_config_with_pending(config.app).get("llm_provider", "")
            or ""
        ).lower()
        == "shengsuanyun"
    ):
        st.caption(tr("Shengsuan Cloud API Key Reused"))

    token = _effective_loomloom_api_token()

    refresh_models = st.button(
        tr("Refresh AI Video Models"),
        key="loomloom_refresh_video_models",
        use_container_width=True,
        disabled=not token,
    )
    capability = _load_loomloom_video_capability(token, force=refresh_models)
    capability_error = str(
        st.session_state.get("loomloom_video_capability_error", "") or ""
    ).strip()
    if capability_error:
        st.warning(tr("AI Video Model List Load Failed").format(error=capability_error))

    if capability is not None:
        models_by_id = {model.model_id: model for model in capability.models}
        selected_model_id = str(
            st.session_state.get("loomloom_video_model_id", "") or ""
        ).strip()
        if not selected_model_id:
            selected_model_id = capability.default_model_id
            st.session_state["loomloom_video_model_id"] = selected_model_id

        model_options = list(models_by_id)
        if selected_model_id not in models_by_id:
            # Giữ lại lựa chọn ban đầu đã hết hạn, giúp người dùng nhìn rõ trạng thái và chủ động chọn lại. Trực tiếp đặt điều khiển
            # Việc thay đổi sang mô hình mặc định mới sẽ khiến các trích dẫn cũ không phù hợp với nhận thức của người dùng.
            model_options.insert(0, selected_model_id)

        selected_model_id = stable_selectbox(
            tr("AI Video Model"),
            options=model_options,
            default_value=selected_model_id,
            key="loomloom_video_model_select",
            format_func=lambda model_id: (
                _format_loomloom_video_model_option(models_by_id[model_id])
                if model_id in models_by_id
                else tr("Unavailable AI Video Model").format(model=model_id)
            ),
        )
        st.session_state["loomloom_video_model_id"] = selected_model_id
        if selected_model_id not in models_by_id:
            st.error(tr("Selected AI Video Model Unavailable"))
        else:
            _, detailed_price = _loomloom_video_model_price(
                models_by_id[selected_model_id]
            )
            if detailed_price:
                st.caption(
                    tr("AI Video Model Reference Price").format(price=detailed_price)
                )

        current_aspect_ratio = str(
            params.video_aspect.value
            if isinstance(params.video_aspect, VideoAspect)
            else params.video_aspect
        )
        if current_aspect_ratio not in capability.aspect_ratios:
            st.error(tr("Selected AI Video Ratio Unavailable"))

    coverage_plan = _loomloom_video_coverage_plan(params)
    pending_autofill_digest = str(
        st.session_state.get("loomloom_video_scene_autofill_digest", "") or ""
    )
    if (
        coverage_plan is not None
        and pending_autofill_digest == coverage_plan["script_digest"]
    ):
        # 只在“刚生成文案”或“刚取得完整试听真实时长”时推荐一次。
        # Sau khi sử dụng dấu, nó sẽ không bị ghi đè nữa và số lượng phân đoạn do người dùng điều chỉnh thủ công sẽ được giữ lại hoàn toàn.
        st.session_state["loomloom_video_scene_count"] = coverage_plan[
            "recommended_count"
        ]
        st.session_state["loomloom_video_scene_autofill_digest"] = ""

    scene_count = st.number_input(
        tr("AI Video Scene Count"),
        min_value=1,
        max_value=loomloom.MAX_VIDEO_SCENES,
        step=1,
        key="loomloom_video_scene_count",
    )
    _set_runtime_config("ui", "loomloom_video_scene_count", int(scene_count))
    if coverage_plan is not None:
        coverage_seconds = int(scene_count) * coverage_plan["clip_duration"]
        shortfall_min = max(
            coverage_plan["duration_min"] - coverage_seconds, 0.0
        )
        shortfall_max = max(
            coverage_plan["duration_max"] - coverage_seconds, 0.0
        )
        duration_basis = tr(coverage_plan["basis_key"]).format(
            duration=_format_numeric_range(
                coverage_plan["duration_min"], coverage_plan["duration_max"]
            )
        )
        coverage_message = tr("AI Video Material Coverage").format(
            basis=duration_basis,
            clip=_format_numeric_range(
                coverage_plan["clip_duration"], coverage_plan["clip_duration"]
            ),
            needed=_format_numeric_range(
                coverage_plan["needed_min"], coverage_plan["needed_max"], digits=0
            ),
            count=int(scene_count),
            coverage=_format_numeric_range(coverage_seconds, coverage_seconds),
            shortfall=_format_numeric_range(shortfall_min, shortfall_max),
        )
        if shortfall_max > 0:
            st.warning(coverage_message)
        else:
            st.caption(coverage_message)

    batch, input_signature = _current_loomloom_video_quote_context(params)
    if not token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    quote_is_current = bool(
        quote_result is not None
        and quoted_batch is not None
        and st.session_state.get("loomloom_video_input_signature") == input_signature
    )
    # Các yêu cầu tự động bị tạm dừng sau khi cùng một bộ tham số không tránh được việc lặp lại việc chờ hết thời gian chờ dịch vụ đối với các tương tác trang thông thường.
    # Chữ ký bao gồm số tài khoản, điểm cuối và tất cả thông tin đầu vào thanh toán; giá sẽ được hỏi sau khi thay đổi tham số hoặc người dùng chủ động thử lại.
    if st.session_state.get("loomloom_video_quote_error_signature") != input_signature:
        st.session_state["loomloom_video_quote_error_signature"] = ""
        st.session_state["loomloom_video_quote_error"] = ""
    quote_failed = bool(st.session_state.get("loomloom_video_quote_error"))
    # Báo giá không tạo ra các nhiệm vụ phải trả phí và việc thực thi thực tế vẫn yêu cầu người dùng kiểm tra và xác nhận một cách rõ ràng.
    if token and batch is not None and not quote_is_current and not quote_failed:
        st.session_state["loomloom_video_confirm_charge"] = False
        try:
            quote_result = _create_loomloom_video_backend().quote(batch)
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(f"failed to quote LoomLoom videos: error={exc}")
            st.session_state["loomloom_video_quote_error_signature"] = input_signature
            st.session_state["loomloom_video_quote_error"] = str(exc) or type(exc).__name__
        else:
            st.session_state["loomloom_video_batch"] = batch
            st.session_state["loomloom_video_quote"] = quote_result
            st.session_state["loomloom_video_input_signature"] = input_signature
            st.session_state["loomloom_video_client_request_id"] = (
                f"mpt-video-{uuid4()}"
            )
            st.session_state["loomloom_video_confirm_charge"] = False
            logger.info(
                "LoomLoom video quote ready: "
                f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
            )

    if st.session_state.get("loomloom_video_quote_error"):
        st.error(st.session_state["loomloom_video_quote_error"])
        st.button(
            tr("Retry AI Video Quote"),
            key="loomloom_retry_video_quote",
            on_click=_retry_loomloom_video_quote,
        )

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    if quote_result is not None and quoted_batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        if quote_result.estimated_buyer_payable_t == 0:
            st.warning(tr("AI Video Quote Estimate Incomplete"))
        else:
            st.success(
                tr(
                    "AI Video Quote Summary Singular"
                    if quote_result.task_count == 1
                    else "AI Video Quote Summary"
                ).format(
                    tasks=quote_result.task_count,
                    amount=display_amount,
                    currency=quote_result.currency,
                )
            )
        quote_is_current = (
            st.session_state.get("loomloom_video_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        st.checkbox(
            tr("Confirm AI Video Charge"),
            key="loomloom_video_confirm_charge",
            help=tr("Confirm AI Video Charge Help"),
            disabled=not quote_is_current,
        )


def _loomloom_script_signature(
    *,
    subject,
    language,
    candidate_count,
    duration_seconds,
    style,
    credential_fingerprint,
):
    payload = {
        "subject": str(subject or "").strip(),
        "language": str(language or "auto").strip() or "auto",
        "candidateCount": int(candidate_count),
        "durationSeconds": int(duration_seconds),
        "style": str(style or "").strip(),
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@st.fragment(run_every=2)
def _render_antigravity_bridge_status(params):
    state = bridge.get_bridge_state()
    status = state.get("status", "idle")
    progress = state.get("progress", 0)
    message = state.get("message", "")
    images = state.get("images", [])
    script = state.get("script", "")
    terms = state.get("terms", [])
    total_imgs = state.get("total_images", 5)

    if status in ("pending", "processing"):
        st.info(f"⏳ **Antigravity Bridge:** {message}")
        st.progress(min(max(progress, 0), 100) / 100.0)
        st.caption(
            f"Tiến độ: **{progress}%** • {len(images)}/{total_imgs} ảnh hoàn thành • Đang kết nối Antigravity..."
        )
        if images:
            st.write("Ảnh đã tạo xong:")
            cols = st.columns(min(len(images), 4))
            for idx, img in enumerate(images[-4:]):
                with cols[idx]:
                    if os.path.exists(img.get("path", "")):
                        st.image(
                            img["path"],
                            caption=img.get("name", ""),
                            use_container_width=True,
                        )
    elif status == "completed":
        st.success(f"🎉 **Antigravity hoàn thành:** {message}")
        st.progress(1.0)
        c_apply, c_reset = st.columns([3, 1])
        with c_apply:
            if st.button(
                "📥 Áp dụng Kịch bản & Danh sách ảnh vào Studio",
                key="apply_bridge_result",
                type="primary",
                use_container_width=True,
            ):
                if script:
                    st.session_state["video_script"] = script
                if terms:
                    st.session_state["video_terms"] = ", ".join(terms)
                st.session_state["use_existing_local_folder"] = True
                _set_runtime_config("app", "video_source", "local")
                st.toast("Đã nạp kịch bản và ảnh vào Studio!")
                bridge.update_bridge_state(status="applied")
                st.rerun()
        with c_reset:
            if st.button("Đóng", key="reset_bridge_state_btn"):
                bridge.reset_bridge_state()
                st.rerun()
        if images:
            with st.expander(
                f"Xem trước {len(images)} ảnh 9:16 do Antigravity tạo", expanded=True
            ):
                cols = st.columns(min(len(images), 4))
                for idx, img in enumerate(images[:4]):
                    with cols[idx]:
                        if os.path.exists(img.get("path", "")):
                            st.image(
                                img["path"],
                                caption=img.get("name", ""),
                                use_container_width=True,
                            )
                if len(images) > 4:
                    st.caption(
                        f"...và {len(images) - 4} ảnh khác trong thư mục storage/local_videos/"
                    )
    elif status == "failed":
        st.error(f"❌ Lỗi: {message}")
        if st.button("Thử lại", key="retry_bridge_btn"):
            bridge.reset_bridge_state()
            st.rerun()


def _render_antigravity_bridge_panel(params):
    """Khối Cầu nối Antigravity Bridge: Tự động viết kịch bản & tạo trọn bộ ảnh 9:16 có Live Progress."""
    with st.container(border=True):
        c_title, c_badge = st.columns([3, 1])
        with c_title:
            st.markdown("##### 🚀 Antigravity Bridge (Tạo Kịch Bản & Trọn Bộ Ảnh)")
        with c_badge:
            st.caption("DeepMind Pro")

        st.caption(
            "Tự động kết nối Antigravity để viết kịch bản và vẽ toàn bộ ảnh phân cảnh 9:16 lưu vào thư mục máy tính."
        )

        state = bridge.get_bridge_state()
        curr_status = state.get("status", "idle")

        if curr_status not in ("pending", "processing"):
            if st.button(
                "✨ Yêu Cầu Antigravity Tạo Kịch Bản & Bộ Ảnh",
                key="send_antigravity_bridge_btn",
                type="primary",
                use_container_width=True,
                icon=":material/rocket_launch:",
                disabled=not bool((params.video_subject or "").strip()),
                help="Gửi chủ đề sang Antigravity để sinh kịch bản và vẽ trọn bộ ảnh 9:16.",
            ):
                bridge.create_bridge_request(
                    subject=params.video_subject, total_images=5
                )
                tools_dir = os.path.join(root_dir, "tools", "process_bridge.py")
                subprocess.Popen([sys.executable, tools_dir])
                st.toast("Đã kích hoạt Antigravity Bridge!")
                st.rerun()

        _render_antigravity_bridge_status(params)


def _render_local_script_generation(params):
    """保留 MoneyPrinterTurbo 原有的本地 LLM 脚本生成路径。"""
    if not st.button(
        tr("Generate Video Script and Keywords"),
        key="auto_generate_script",
        use_container_width=True,
        type="secondary",
        icon=":material/auto_awesome:",
    ):
        return

    if not params.video_subject:
        st.toast(tr("Please Enter the Video Subject First"))
        st.warning(tr("Please Enter the Video Subject First"))
        return

    with st.spinner(tr("Generating Video Script and Keywords")):

        def generate_script_and_terms(app_config_snapshot):
            script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=params.video_script_prompt,
                custom_system_prompt=params.custom_system_prompt,
                app_config=app_config_snapshot,
            )
            terms = llm.generate_terms(
                params.video_subject,
                script,
                amount=8 if params.match_materials_to_script else 5,
                match_script_order=params.match_materials_to_script,
                app_config=app_config_snapshot,
            )
            return script, terms

        script, terms = _run_llm_read_operation(
            "generate_script_and_terms",
            generate_script_and_terms,
        )
        if "Error: " in script:
            st.error(tr(script))
        elif "Error: " in terms:
            st.error(tr(terms))
        else:
            st.session_state["video_script"] = script
            st.session_state["video_terms"] = ", ".join(terms)
            st.session_state["loomloom_video_scene_autofill_digest"] = (
                hashlib.sha256(script.strip().encode("utf-8")).hexdigest()
            )


def _render_loomloom_candidates():
    candidates = tuple(st.session_state.get("loomloom_script_candidates") or ())
    errors = tuple(st.session_state.get("loomloom_candidate_errors") or ())
    if errors:
        st.warning(
            tr("LoomLoom Candidate Errors").format(
                count=len(errors),
                details="; ".join(
                    f"#{error.row_index + 1}: {error.message}" for error in errors
                ),
            )
        )
    if not candidates:
        return

    selected_index = st.radio(
        tr("Choose Script Candidate"),
        options=list(range(len(candidates))),
        key="loomloom_selected_candidate",
        format_func=lambda index: (
            f"#{candidates[index].row_index + 1} {candidates[index].script[:80]}"
        ),
    )
    selected = candidates[selected_index]
    st.code(selected.script, language=None, wrap_lines=True)
    st.caption(", ".join(selected.video_terms))
    if st.button(
        tr("Use Selected Candidate"),
        key="loomloom_apply_candidate",
        type="primary",
        use_container_width=True,
    ):
        st.session_state["video_script"] = selected.script
        st.session_state["video_terms"] = ", ".join(selected.video_terms)
        # Phù hợp với thế hệ copywriting mô hình ngôn ngữ lớn (LLM) thông thường: số lượng tài liệu chỉ được đề xuất một lần sau khi áp dụng một ứng viên mới.
        st.session_state["loomloom_video_scene_autofill_digest"] = (
            hashlib.sha256(selected.script.strip().encode("utf-8")).hexdigest()
        )
        st.toast(tr("LoomLoom Candidate Applied"))


def _handle_loomloom_poll_error(run_id, exc):
    """Thực hiện tính năng lùi có giới hạn đối với các lỗi kiểm tra tác vụ tập lệnh và dừng kiểm tra vòng ngay lập tức đối với các lỗi xác định."""
    logger.warning(f"failed to poll LoomLoom run: run_id={run_id}, error={exc}")
    failure_count = int(st.session_state.get("loomloom_poll_failure_count", 0) or 0) + 1
    retryable = isinstance(exc, loomloom.LoomLoomAPIError) and exc.retryable
    if not retryable or failure_count >= LOOMLOOM_MAX_POLL_FAILURES:
        st.session_state["loomloom_run_error"] = str(exc)
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        # Truy vấn không thành công không có nghĩa là nhiệm vụ thanh toán từ xa không thành công. Giữ run_id và tạm dừng bỏ phiếu tự động để cho phép người dùng
        # Bạn có thể tiếp tục truy vấn cùng một tác vụ; nếu bạn hủy ID và gửi lại, bạn có thể bị tính phí hai lần.
        st.session_state["loomloom_poll_paused"] = True
        st.rerun(scope="app")
        return

    retry_delay = min(2**failure_count, 30)
    st.session_state["loomloom_poll_failure_count"] = failure_count
    st.session_state["loomloom_poll_retry_after"] = time.monotonic() + retry_delay
    st.warning(
        tr("LoomLoom Poll Retry Warning").format(
            attempt=failure_count,
            max_attempts=LOOMLOOM_MAX_POLL_FAILURES,
        )
    )


@st.fragment(run_every="2s")
def _render_loomloom_run_progress():
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if not run_id or st.session_state.get("loomloom_poll_paused", False):
        return
    retry_after = float(st.session_state.get("loomloom_poll_retry_after", 0.0) or 0.0)
    retry_wait_seconds = max(0, int(math.ceil(retry_after - time.monotonic())))
    if retry_wait_seconds > 0:
        st.info(
            tr("LoomLoom Poll Retry Pending").format(
                seconds=retry_wait_seconds,
            )
        )
        return
    try:
        backend = _create_loomloom_script_backend()
        run = backend.get_run(run_id)
    except loomloom.LoomLoomError as exc:
        _handle_loomloom_poll_error(run_id, exc)
        return

    st.session_state["loomloom_run_status"] = run.status
    if run.status == "completed":
        try:
            result = backend.get_script_results(run_id)
        except loomloom.LoomLoomError as exc:
            _handle_loomloom_poll_error(run_id, exc)
            return
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        st.session_state["loomloom_poll_paused"] = False
        st.session_state["loomloom_script_candidates"] = result.candidates
        st.session_state["loomloom_candidate_errors"] = result.errors
        st.session_state["loomloom_selected_candidate"] = 0
        st.session_state["loomloom_run_id"] = ""
        st.rerun(scope="app")
        return
    if run.status in {"failed", "cancelled", "canceled"}:
        st.session_state["loomloom_run_error"] = run.first_error_message or run.status
        st.session_state["loomloom_run_id"] = ""
        st.session_state["loomloom_poll_paused"] = False
        st.rerun(scope="app")
        return

    st.session_state["loomloom_poll_failure_count"] = 0
    st.session_state["loomloom_poll_retry_after"] = 0.0
    st.info(
        tr("LoomLoom Run Progress").format(
            completed=run.completed_tasks,
            total=run.total_tasks,
        )
    )


def _render_loomloom_script_generation(params):
    st.caption(tr("LoomLoom Batch Script Generation Help"))
    effective_token = _effective_loomloom_api_token()
    if not effective_token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    candidate_col, duration_col = st.columns(2)
    candidate_count = candidate_col.number_input(
        tr("Script Candidate Count"),
        min_value=1,
        max_value=loomloom.MAX_SCRIPT_CANDIDATES,
        step=1,
        key="loomloom_candidate_count",
    )
    duration_seconds = duration_col.number_input(
        tr("Target Script Duration Seconds"),
        min_value=10,
        max_value=600,
        step=10,
        key="loomloom_script_duration_seconds",
    )
    _set_runtime_config("ui", "loomloom_candidate_count", int(candidate_count))
    _set_runtime_config(
        "ui", "loomloom_script_duration_seconds", int(duration_seconds)
    )
    input_signature = _loomloom_script_signature(
        subject=params.video_subject,
        language=params.video_language,
        candidate_count=candidate_count,
        duration_seconds=duration_seconds,
        style=params.video_script_prompt,
        credential_fingerprint=(
            hashlib.sha256(effective_token.encode("utf-8")).hexdigest()
            if effective_token
            else ""
        ),
    )

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_scripts",
        use_container_width=True,
        type="secondary",
        icon=":material/request_quote:",
        disabled=not effective_token or bool(st.session_state.get("loomloom_run_id")),
    ):
        if not params.video_subject:
            st.toast(tr("Please Enter the Video Subject First"))
            st.warning(tr("Please Enter the Video Subject First"))
        else:
            try:
                backend = _create_loomloom_script_backend()
                batch = backend.prepare_script_batch(
                    subject=params.video_subject,
                    candidate_count=int(candidate_count),
                    language=params.video_language,
                    duration_seconds=int(duration_seconds),
                    style=params.video_script_prompt,
                )
                quote_result = backend.quote(batch)
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to quote LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_script_batch"] = batch
                st.session_state["loomloom_script_quote"] = quote_result
                st.session_state["loomloom_script_input_signature"] = input_signature
                st.session_state["loomloom_client_request_id"] = f"mpt-{uuid4()}"
                st.session_state["loomloom_run_id"] = ""
                st.session_state["loomloom_run_status"] = "quoted"
                st.session_state["loomloom_run_error"] = ""
                st.session_state["loomloom_poll_failure_count"] = 0
                st.session_state["loomloom_poll_retry_after"] = 0.0
                st.session_state["loomloom_poll_paused"] = False
                st.session_state["loomloom_script_candidates"] = ()
                st.session_state["loomloom_candidate_errors"] = ()
                st.session_state["loomloom_confirm_charge"] = False
                logger.info(
                    "LoomLoom script quote ready: "
                    f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                    f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
                )

    quote_result = st.session_state.get("loomloom_script_quote")
    batch = st.session_state.get("loomloom_script_batch")
    if quote_result is not None and batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "LoomLoom Quote Summary Singular"
                if quote_result.task_count == 1
                else "LoomLoom Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_script_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        confirm_charge = st.checkbox(
            tr("Confirm LoomLoom Charge"),
            key="loomloom_confirm_charge",
            disabled=not quote_is_current,
        )
        run_in_progress = bool(st.session_state.get("loomloom_run_id"))
        if st.button(
            tr("Run LoomLoom Batch"),
            key="loomloom_execute_scripts",
            use_container_width=True,
            type="primary",
            disabled=(not quote_is_current or not confirm_charge or run_in_progress),
        ):
            try:
                execution = _create_loomloom_script_backend().execute(
                    batch,
                    client_request_id=st.session_state["loomloom_client_request_id"],
                    listing_version_id=quote_result.listing_version_id,
                    confirm=True,
                )
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to execute LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_run_id"] = execution.run_id
                st.session_state["loomloom_run_status"] = "running"
                st.session_state["loomloom_poll_paused"] = False
                # Chỉ được phép bắt đầu một đợt thanh toán cho mỗi báo giá. Trạng thái nền chỉ phụ thuộc vào run_id, submit
                # Sau đó, báo giá và ID yêu cầu bình thường có thể bị loại bỏ; sau khi thất bại, người dùng cần báo giá lại và thử lại.
                st.session_state["loomloom_script_batch"] = None
                st.session_state["loomloom_script_quote"] = None
                st.session_state["loomloom_script_input_signature"] = ""
                st.session_state["loomloom_client_request_id"] = ""
                logger.info(
                    f"LoomLoom script run submitted: run_id={execution.run_id}, "
                    f"tasks={len(batch.input_rows)}"
                )
                st.toast(tr("LoomLoom Run Submitted"))

    run_error = str(st.session_state.get("loomloom_run_error", "") or "").strip()
    if run_error:
        st.error(tr("LoomLoom Run Failed").format(error=run_error))
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if run_id and st.session_state.get("loomloom_poll_paused", False):
        retry_col, stop_col = st.columns(2)
        if retry_col.button(
            tr("Resume LoomLoom Status Check"),
            key="loomloom_resume_status_check",
            use_container_width=True,
            type="secondary",
        ):
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_failure_count"] = 0
            st.session_state["loomloom_poll_retry_after"] = 0.0
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
        if stop_col.button(
            tr("Stop Tracking LoomLoom Run"),
            key="loomloom_stop_tracking_run",
            use_container_width=True,
            type="secondary",
            help=tr("Stop Tracking LoomLoom Run Help"),
        ):
            # 这里只停止本地状态查询，不声称取消远端执行。用户确认放弃跟踪后
            # Sau khi xóa run_id, lần chạy trả phí tiếp theo vẫn cần được báo giá lại và xác nhận.
            st.session_state["loomloom_run_id"] = ""
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
    # Chỉ các lô đang thực sự chạy mới bắt đầu bỏ phiếu hai giây và giai đoạn báo giá và giai đoạn hiển thị kết quả sẽ không được tạo.
    # Các đoạn thời gian tránh các yêu cầu mạng vô nghĩa và chạy lại khi người dùng ở lại trang.
    if run_id and not st.session_state.get("loomloom_poll_paused", False):
        _render_loomloom_run_progress()
    _render_loomloom_candidates()


def _render_script_settings(panel, params):
    """Kết xuất cài đặt sao chép và cập nhật các tham số tạo."""
    with panel:
        with st.container(border=True):
            st.write(tr("Video Script Settings"))
            # Hàng nhãn cần chứa mục nhập "Định cấu hình mô hình ngôn ngữ lớn (LLM)", vì vậy không thể sử dụng text_area nữa
            # Thẻ tích hợp. Sau khi thu thập nhãn và hộp nhập vào cùng một vùng chứa trường, khoảng cách bên trong có thể được che đi.
            # Ngoài ra, hãy giữ trường theo nhịp điệu bên ngoài nhất quán với các điều khiển biểu mẫu khác trên trang.
            with st.container(key="video_subject_field"):
                with st.container(
                    key="video_subject_label_row",
                    horizontal=True,
                    vertical_alignment="center",
                    gap="small",
                ):
                    st.markdown(
                        tr("Video Subject"),
                        help=tr("Video Subject Help"),
                        width="content",
                    )
                    st.button(
                        tr("Configure LLM"),
                        key="open_llm_settings_from_subject",
                        type="tertiary",
                        on_click=_open_settings_dialog,
                        args=("llm",),
                    )
                params.video_subject = st.text_area(
                    tr("Video Subject"),
                    placeholder=tr("Video Subject Placeholder"),
                    height=96,
                    key="video_subject",
                    label_visibility="collapsed",
                ).strip()

            video_languages = [
                (tr("Auto Detect"), ""),
            ]
            for code in support_locales:
                video_languages.append((code, code))

            selected_language_code = stable_selectbox(
                tr("Script Language"),
                options=[value for _, value in video_languages],
                default_value=_saved_ui_choice(
                    "video_language",
                    [value for _, value in video_languages],
                    "",
                ),
                key="script_language_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_languages
                )[value],
            )
            params.video_language = selected_language_code
            _set_runtime_config("ui", "video_language", params.video_language)

            # 使用带 key 的局部容器限定折叠入口样式，保持 expander 的原生交互，
            # Đồng thời, tránh các kiểu vô tình làm hỏng các khu vực gấp khác như “Cài đặt cơ bản” ở đầu trang.
            with st.container(key="advanced_settings_script"):
                with st.expander(tr("Advanced Script Settings"), expanded=False):
                    script_backend_options = ["local", "loomloom"]
                    script_backend_labels = {
                        "local": tr("Local LLM Script Generation"),
                        "loomloom": tr("Shengsuan Cloud Batch Script Generation"),
                    }
                    script_backend_widget_key = localized_widget_key(
                        "script_generation_backend_select"
                    )
                    current_script_backend = st.session_state.get(
                        script_backend_widget_key,
                        _effective_script_generation_backend(),
                    )
                    script_generation_backend = stable_selectbox(
                        tr("Script Generation Method"),
                        options=script_backend_options,
                        default_value=_effective_script_generation_backend(),
                        key="script_generation_backend_select",
                        format_func=lambda value: script_backend_labels[value],
                        help=_script_generation_method_help(current_script_backend),
                    )
                    _set_runtime_config(
                        "app", "script_generation_backend", script_generation_backend
                    )

                    params.paragraph_number = st.slider(
                        tr("Script Paragraph Number"),
                        min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                        max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                        key="paragraph_number_input",
                    )
                    _set_runtime_config(
                        "ui", "paragraph_number", params.paragraph_number
                    )
                    params.video_script_prompt = st.text_area(
                        tr("Custom Script Requirements"),
                        height=100,
                        max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                        placeholder=tr("Custom Script Requirements Placeholder"),
                        key="video_script_prompt",
                    ).strip()
                    _set_runtime_config(
                        "ui", "video_script_prompt", params.video_script_prompt
                    )

                    system_prompt = st.text_area(
                        tr("Custom System Prompt"),
                        height=240,
                        max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                        key="custom_system_prompt",
                    ).strip()
                    # Nội dung mặc định được duy trì thống nhất bởi lớp dịch vụ. Mặc dù giao diện hiển thị trực tiếp các từ nhắc nhở mặc định nhưng nó chỉ
                    # Chỉ những sửa đổi thực tế do người dùng thực hiện mới được chuyển cùng với nhiệm vụ để tránh phiên bản cũ của quy tắc mặc định được củng cố trong các nhiệm vụ lịch sử.
                    params.custom_system_prompt = (
                        ""
                        if system_prompt == llm.DEFAULT_SCRIPT_SYSTEM_PROMPT.strip()
                        else system_prompt
                    )
                    _set_runtime_config(
                        "ui", "custom_system_prompt", params.custom_system_prompt
                    )

                    restore_prompt_col, preview_prompt_col = st.columns(2)
                    if restore_prompt_col.button(
                        tr("Restore Default System Prompt"),
                        key="restore_default_system_prompt",
                        icon=":material/restart_alt:",
                        on_click=reset_script_system_prompt,
                        use_container_width=True,
                    ):
                        st.toast(tr("Default System Prompt Restored"))
                    if preview_prompt_col.button(
                        tr("Preview Final Prompt"),
                        key="preview_final_script_prompt",
                        icon=":material/preview:",
                        use_container_width=True,
                    ):
                        render_script_prompt_preview(
                            llm.build_script_prompt(
                                video_subject=params.video_subject,
                                language=params.video_language,
                                paragraph_number=params.paragraph_number,
                                video_script_prompt=params.video_script_prompt,
                                custom_system_prompt=params.custom_system_prompt,
                            )
                        )

            # Khám phá mô hình chỉ nâng cao chất liệu video và không thay đổi nhà cung cấp bản sao được người dùng lựa chọn rõ ràng.
            if _effective_script_generation_backend() == "loomloom":
                _render_loomloom_script_generation(params)
            else:
                _render_local_script_generation(params)
                _render_antigravity_bridge_panel(params)
            params.video_script = st.text_area(
                tr("Video Script"),
                help=tr("Video Script Help"),
                height=180,
                key="video_script",
            )

            # Khối tùy chọn: Đồng nhất nhân vật cố định (Consistent Character)
            with st.expander(":material/person: Nhân Vật Cố Định Cho Video (Tùy Chọn)", expanded=False):
                params.character_anchor_enabled = st.checkbox(
                    "Đồng nhất một nhân vật duy nhất cho toàn bộ video",
                    value=bool(st.session_state.get("char_anchor_enabled", False)),
                    help="AI sẽ giữ nguyên khuôn mặt, vóc dáng và phong cách của một người mẫu cố định trong mọi phân cảnh.",
                    key="char_anchor_enabled_checkbox",
                )
                st.session_state["char_anchor_enabled"] = params.character_anchor_enabled

                if params.character_anchor_enabled:
                    char_source = st.radio(
                        "Cách nạp nhân vật:",
                        ["Dùng ảnh người mẫu tham chiếu", "Chỉ nhập mô tả nhân vật bằng chữ"],
                        horizontal=True,
                        key="char_source_radio",
                    )
                    if char_source == "Dùng ảnh người mẫu tham chiếu":
                        col_c1, col_c2 = st.columns(2)
                        with col_c1:
                            if st.button(
                                "Lấy ảnh vừa Thử Đồ",
                                icon=":material/sync:",
                                use_container_width=True,
                                key="char_get_tryon_btn",
                            ):
                                last_tryon = st.session_state.get("latest_tryon_result")
                                if last_tryon and os.path.isfile(last_tryon):
                                    st.session_state["char_img_selected"] = last_tryon
                                    st.success("Đã nạp ảnh người mẫu từ Tab Thử Đồ!")
                                else:
                                    st.warning("Chưa có ảnh từ Tab Thử Đồ Ảo.")

                        uploaded_char_img = st.file_uploader(
                            "Hoặc tải ảnh người mẫu từ máy tính:",
                            type=["png", "jpg", "jpeg", "webp"],
                            key="char_model_file_uploader",
                        )
                        if uploaded_char_img:
                            temp_dir = Path("storage/models")
                            temp_dir.mkdir(parents=True, exist_ok=True)
                            c_p = temp_dir / f"model_{uploaded_char_img.name}"
                            with open(c_p, "wb") as f:
                                f.write(uploaded_char_img.getbuffer())
                            st.session_state["char_img_selected"] = str(c_p)

                        selected_char_img = st.session_state.get("char_img_selected")
                        if selected_char_img and os.path.isfile(selected_char_img):
                            params.character_image_path = selected_char_img
                            st.image(selected_char_img, caption="Người mẫu tham chiếu", use_container_width=True)

                            if st.button(
                                "Trích Xuất Nhận Dạng AI (Gemini Vision)",
                                icon=":material/auto_awesome:",
                                use_container_width=True,
                                key="char_extract_vision_btn",
                            ):
                                with st.spinner("Gemini Vision đang quét khuôn mặt và phong cách..."):
                                    ok, desc = llm.extract_character_profile_from_image(selected_char_img)
                                    if ok:
                                        st.session_state["character_prompt_val"] = desc
                                        st.success("Trích xuất nhận dạng thành công!")
                                    else:
                                        st.error(f"Lỗi: {desc}")

                    params.character_prompt = st.text_area(
                        "Mô tả nhận dạng nhân vật (Character Anchor Prompt):",
                        value=st.session_state.get(
                            "character_prompt_val",
                            "young 22-year-old Vietnamese female model, slender, delicate oval face, long wavy dark hair, elegant modern outfit",
                        ),
                        height=90,
                        help="Mô tả này sẽ được AI khóa vào từng phân cảnh để đảm bảo cùng 1 người mẫu xuất hiện.",
                        key="character_prompt_input_box",
                    ).strip()
                    st.session_state["character_prompt_val"] = params.character_prompt

            if _effective_script_generation_backend() == "loomloom":
                st.caption(tr("LoomLoom Video Terms Reuse Help"))
            elif st.button(
                tr("Generate Video Keywords"),
                key="auto_generate_terms",
                use_container_width=True,
                type="secondary",
                icon=":material/auto_awesome:",
            ):
                if not params.video_script:
                    # Từ khóa video cần được trích xuất dựa trên bản sao. Nếu bản sao trống, bạn sẽ được nhắc trước và lệnh gọi mô hình sẽ bị bỏ qua.
                    st.toast(tr("Please Enter the Video Subject"))
                    st.warning(tr("Please Enter the Video Subject"))
                else:
                    with st.spinner(tr("Generating Video Keywords")):
                        char_p = (
                            params.character_prompt
                            if params.character_anchor_enabled
                            else ""
                        )
                        terms = _run_llm_read_operation(
                            "generate_terms",
                            lambda app_config_snapshot: llm.generate_terms(
                                params.video_subject,
                                params.video_script,
                                amount=8 if params.match_materials_to_script else 5,
                                match_script_order=params.match_materials_to_script,
                                app_config=app_config_snapshot,
                                character_prompt=char_p,
                            ),
                        )
                        if "Error: " in terms:
                            st.error(tr(terms))
                        else:
                            st.session_state["video_terms"] = ", ".join(terms)

            params.video_terms = st.text_area(
                tr("Video Keywords"),
                help=tr("Video Keywords Help"),
                key="video_terms",
            )

            if params.video_terms.strip():
                if st.button(
                    "🎨 Tạo trước bộ ảnh AI vào thư mục Local",
                    key="generate_local_ai_images_btn",
                    use_container_width=True,
                    help="Tự động vẽ ảnh AI chuẩn 9:16 cho từng từ khóa trên và lưu thẳng vào thư mục storage/local_videos để xem trước!",
                    icon=":material/palette:",
                ):
                    raw_terms = [t.strip() for t in params.video_terms.split(",") if t.strip()]
                    if not raw_terms:
                        st.warning("Vui lòng có ít nhất một từ khóa video.")
                    else:
                        local_dir = utils.storage_dir("local_videos", create=True)
                        progress_bar = st.progress(0, text="Đang bắt đầu vẽ ảnh AI...")
                        success_count = 0
                        for idx, term in enumerate(raw_terms):
                            progress_bar.progress(
                                (idx) / len(raw_terms),
                                text=f"Đang vẽ ảnh {idx + 1}/{len(raw_terms)}: {term}...",
                            )
                            prompt = f"digital illustration of {term}, cinematic lighting, 9:16 vertical composition, ultra detailed"
                            if params.character_anchor_enabled and params.character_prompt:
                                prompt = f"{params.character_prompt}, {prompt}"
                            img_bytes = material._request_pollinations_image(prompt, params.video_aspect)
                            if img_bytes:
                                fname = f"{(idx + 1):02d}_ai_{re.sub(r'[^a-zA-Z0-9]', '_', term)[:25]}.png"
                                save_path = os.path.join(local_dir, fname)
                                with open(save_path, "wb") as f:
                                    f.write(img_bytes)
                                success_count += 1
                        progress_bar.progress(1.0, text=f"Hoàn thành! Đã tạo {success_count}/{len(raw_terms)} ảnh.")
                        st.toast(f"Đã tạo {success_count} ảnh AI vào thư mục storage/local_videos!")
                        st.rerun()


def _render_video_settings(panel, params):
    """Kết xuất cài đặt video và trả lại tài liệu cục bộ đã chọn lần này."""
    uploaded_files = []
    with panel:
        with st.container(border=True):
            st.write(tr("Video Settings"))
            video_concat_modes = [
                (tr("Sequential"), "sequential"),
                (tr("Random"), "random"),
            ]
            video_source_labels = {
                "pexels": tr("Pexels"),
                "pixabay": tr("Pixabay"),
                "coverr": tr("Coverr"),
                "wavespeed": tr("WaveSpeed AI Video"),
                "volcengine_seedance": tr("Volcano Engine Seedance"),
                "ofox": tr("OFox AI Video"),
                "metaso_minimax": tr("Metaso MiniMax H3"),
                "loomloom": tr("Shengsuan Cloud AI Video"),
                "openai_image": tr("OpenAI Compatible Text-to-Image"),
                "local": tr("Local file"),
            }
            saved_video_source_name = str(
                config.app.get("video_source", "pexels") or "pexels"
            )
            params.video_source = grouped_selectbox(
                tr("Video Source"),
                groups=(
                    (tr("Stock Video"), VIDEO_SOURCE_GROUPS["stock_video"]),
                    (tr("AI Video"), VIDEO_SOURCE_GROUPS["ai_video"]),
                    (tr("AI Image"), VIDEO_SOURCE_GROUPS["ai_image"]),
                    (tr("Local Material"), VIDEO_SOURCE_GROUPS["local"]),
                ),
                default_value=saved_video_source_name,
                key="video_source_select",
                format_func=video_source_labels.get,
                settings_label=tr("Configure Material Sources"),
                on_settings=_open_material_settings_dialog,
            )
            _set_runtime_config("app", "video_source", params.video_source)

            loomloom_video_capability = None
            if params.video_source == "loomloom":
                # Đọc bộ đệm càng sớm càng tốt để việc kiểm soát tỷ lệ khung hình thấp hơn bị ràng buộc trực tiếp bởi Cấu hình hiện tại.
                # Sau khi nhập Key lần đầu tiên Streamlit sẽ chạy lại và nạp vào đây.
                loomloom_video_capability = _load_loomloom_video_capability(
                    _effective_loomloom_api_token()
                )

            if params.video_source == "wavespeed":
                st.caption(tr("WaveSpeed AI Video Help"))
            if params.video_source == "volcengine_seedance":
                st.caption(tr("Volcano Engine Seedance Help"))
            if params.video_source == "ofox":
                st.caption(f"[OfoxAI]({OFOX_REFERRAL_URL}) · {tr('OFox AI Video Help')}")
            if params.video_source == "metaso_minimax":
                st.caption(tr("Metaso MiniMax H3 Help"))
            if params.video_source == "local":
                # Việc xác minh loại tệp của Streamlit rất nhạy cảm với trường hợp của phần mở rộng và cả dạng chữ hoa và chữ thường đều được cho phép ở đây.
                local_file_types = sorted(
                    extension.removeprefix(".")
                    for extension in LOCAL_MATERIAL_EXTENSIONS
                )
                uploaded_files = st.file_uploader(
                    tr("Upload Local Files"),
                    type=local_file_types
                    + [file_type.upper() for file_type in local_file_types],
                    accept_multiple_files=True,
                    key="local_video_materials_uploader",
                )

                local_videos_dir = utils.storage_dir("local_videos", create=True)
                existing_media = _get_existing_local_media_files(local_videos_dir)
                with st.expander(
                    f"📁 Thư mục Local / Antigravity AI ({len(existing_media)} tệp có sẵn)",
                    expanded=not bool(uploaded_files) and bool(existing_media),
                ):
                    st.caption(f"Đường dẫn: `{local_videos_dir}`")
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        st.checkbox(
                            "Sử dụng tệp có sẵn trong thư mục nếu không tải file mới",
                            value=True,
                            key="use_existing_local_folder",
                        )
                    with c2:
                        if st.button("🔄 Quét lại", key="refresh_local_files_btn"):
                            st.rerun()

                    if existing_media:
                        st.success(
                            f"Đã phát hiện **{len(existing_media)} tệp** (ảnh/video). "
                            "Hệ thống sẽ tự động ghép các tệp này theo thứ tự tên tệp!"
                        )
                        preview_cols = st.columns(min(len(existing_media), 4))
                        for idx, pth in enumerate(existing_media[:4]):
                            with preview_cols[idx]:
                                fname = os.path.basename(pth)
                                ext = os.path.splitext(fname)[1].lower()
                                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                                    st.image(pth, caption=fname, use_container_width=True)
                                else:
                                    st.write(f"🎬 {fname}")
                        if len(existing_media) > 4:
                            st.caption(f"Và {len(existing_media) - 4} tệp khác...")
                    else:
                        st.info(
                            "Chưa có tệp trong thư mục. Bạn có thể yêu cầu Antigravity tạo ảnh "
                            "hoặc nạp ảnh vào thư mục trên."
                        )

            # So khớp trình tự sao chép sẽ duy trì thứ tự tường thuật từ khi tạo từ khóa đến tổng hợp cuối cùng, vì vậy khi nó được bật
            # Nối tuần tự là lựa chọn duy nhất phù hợp với logic thực thi thực tế. Đồng bộ hóa các giá trị điều khiển ngăn giao diện vẫn hiển thị
            # "Nối ngẫu nhiên", trong khi vẫn giữ lại lựa chọn ban đầu của người dùng và tự động khôi phục sau khi đóng.
            sync_script_order_concat_mode()
            selected_concat_mode = stable_selectbox(
                tr("Video Concat Mode"),
                options=[value for _, value in video_concat_modes],
                default_value=_saved_ui_choice(
                    "video_concat_mode",
                    [value for _, value in video_concat_modes],
                    VideoConcatMode.random.value,
                ),
                key="video_concat_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_concat_modes
                )[value],
                disabled=bool(st.session_state.get("match_materials_to_script", False)),
            )
            params.video_concat_mode = VideoConcatMode(selected_concat_mode)

            params.match_materials_to_script = st.checkbox(
                tr("Match Materials to Script Order"),
                help=tr("Match Materials to Script Order Help"),
                key="match_materials_to_script",
                on_change=sync_script_order_concat_mode,
            )
            _set_runtime_config(
                "app",
                "match_materials_to_script",
                params.match_materials_to_script,
            )
            # Khi tính năng khớp tuần tự được bật, tuần tự là giá trị bắt buộc bắt nguồn và không được ghi đè giá trị của người dùng.
            # Chức năng này là ưu tiên nối đã chọn; sau khi tắt nó, ngẫu nhiên/tuần tự trước đó vẫn có thể được khôi phục.
            if not params.match_materials_to_script:
                _set_runtime_config(
                    "ui", "video_concat_mode", params.video_concat_mode.value
                )

            # Chế độ chuyển tiếp video
            video_transition_modes = [
                (tr("None"), VideoTransitionMode.none.value),
                (tr("Shuffle"), VideoTransitionMode.shuffle.value),
                (tr("FadeIn"), VideoTransitionMode.fade_in.value),
                (tr("FadeOut"), VideoTransitionMode.fade_out.value),
                (tr("SlideIn"), VideoTransitionMode.slide_in.value),
                (tr("SlideOut"), VideoTransitionMode.slide_out.value),
                (tr("ZoomIn"), VideoTransitionMode.zoom_in.value),
                (tr("ZoomOut"), VideoTransitionMode.zoom_out.value),
            ]
            selected_transition_mode = stable_selectbox(
                tr("Video Transition Mode"),
                options=[value for _, value in video_transition_modes],
                default_value=_saved_ui_choice(
                    "video_transition_mode",
                    [value for _, value in video_transition_modes],
                    VideoTransitionMode.none.value,
                ),
                key="video_transition_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_transition_modes
                )[value],
            )
            params.video_transition_mode = VideoTransitionMode(selected_transition_mode)
            _set_runtime_config(
                "ui",
                "video_transition_mode",
                params.video_transition_mode.value,
            )

            video_aspect_ratios = [
                (tr("Portrait"), VideoAspect.portrait.value),
                (tr("Landscape"), VideoAspect.landscape.value),
            ]
            if loomloom_video_capability is not None:
                ratio_labels = {value: label for label, value in video_aspect_ratios}
                video_aspect_ratios = [
                    (ratio_labels[value], value)
                    for value in loomloom_video_capability.aspect_ratios
                ]
            # 99% thư viện Coverr là màn hình ngang 16:9. Màn hình dọc mặc định sẽ khiến màn hình bị bao quanh bởi rất nhiều viền đen.
            # Sử dụng khóa tiện ích dành riêng cho nguồn để mỗi nguồn ghi nhớ lựa chọn khía cạnh của nó:
            #   - Chuyển sang coverr lần đầu tiên → Cảnh mặc định (chỉ mục = 1)
            #   - Các nguồn khác theo Portrait(index=0)
            #   - Nếu người dùng thay đổi khía cạnh theo cách thủ công theo một nguồn nhất định, session_state sẽ được ghi nhớ.
            #     Lựa chọn của người dùng sẽ được tôn trọng khi quay lại cùng một nguồn vào lần sau và sẽ không bị buộc phải ghi đè lại.
            default_aspect_index = 1 if params.video_source == "coverr" else 0
            video_aspect_values = [value for _, value in video_aspect_ratios]
            video_aspect_config_key = f"video_aspect_{params.video_source}"
            selected_aspect_ratio = stable_selectbox(
                tr("Video Ratio"),
                options=video_aspect_values,
                default_value=_saved_ui_choice(
                    video_aspect_config_key,
                    video_aspect_values,
                    video_aspect_ratios[default_aspect_index][1],
                ),
                key=f"video_aspect_for_{params.video_source}",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_aspect_ratios
                )[value],
            )
            params.video_aspect = VideoAspect(selected_aspect_ratio)
            _set_runtime_config(
                "ui", video_aspect_config_key, params.video_aspect.value
            )

            video_fit_modes = [
                (tr("Fill and Crop"), VideoFitMode.cover.value),
                (tr("Fit with Black Bars"), VideoFitMode.contain.value),
            ]
            selected_fit_mode = stable_selectbox(
                tr("Video Fit Mode"),
                options=[value for _, value in video_fit_modes],
                default_value=_saved_ui_choice(
                    "video_fit_mode",
                    [value for _, value in video_fit_modes],
                    VideoFitMode.cover.value,
                ),
                key="video_fit_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_fit_modes
                )[value],
                help=tr("Video Fit Mode Help"),
            )
            params.video_fit_mode = VideoFitMode(selected_fit_mode)
            _set_runtime_config(
                "ui", "video_fit_mode", params.video_fit_mode.value
            )

            # Khoảng thời gian điều khiển từ xa của MiniMax H3 là từ 4 đến 15 giây. Sử dụng toàn bộ khả năng khi chọn Tháp Bí Mật
            # Phạm vi này không chỉ ngăn 2/3 giây bị tính phí là 4 giây mà còn làm cho WebUI nhất quán với CLI và lớp dịch vụ.
            video_clip_durations = (
                list(
                    range(
                        metaso_minimax.DEFAULT_MIN_DURATION_SECONDS,
                        metaso_minimax.DEFAULT_MAX_DURATION_SECONDS + 1,
                    )
                )
                if params.video_source == "metaso_minimax"
                else [2, 3, 4, 5, 6, 7, 8, 9, 10]
            )
            params.video_clip_duration = stable_selectbox(
                tr("Clip Duration"),
                options=video_clip_durations,
                default_value=_saved_ui_choice(
                    "video_clip_duration",
                    video_clip_durations,
                    5 if params.video_source == "metaso_minimax" else 3,
                ),
                key="video_clip_duration_select",
                help=tr("Clip Duration Help"),
            )
            _set_runtime_config(
                "ui", "video_clip_duration", params.video_clip_duration
            )
            clip_speed_key = localized_widget_key("video_clip_speed_slider")
            # session_state có thể đến từ tác vụ cũ, tham số API hoặc trạng thái trang cũ. Trước khi điều khiển được tạo
            # Chuẩn hóa thống nhất không chỉ giữ lại các lựa chọn hợp pháp mà còn đảm bảo rằng thanh trượt luôn nhận được 0,5 ~ 2,0
            # 范围内的有限浮点数。
            st.session_state[clip_speed_key] = utils.normalize_clip_speed(
                st.session_state.get(
                    clip_speed_key,
                    _saved_ui_number("video_clip_speed", 1.0, 0.5, 2.0),
                )
            )
            params.video_clip_speed = st.slider(
                tr("Clip Speed"),
                min_value=0.5,
                max_value=2.0,
                step=0.05,
                format="%.2fx",
                key=clip_speed_key,
                help=tr("Clip Speed Help"),
            )
            _set_runtime_config("ui", "video_clip_speed", params.video_clip_speed)
            video_count_options = [1, 2, 3, 4, 5]
            params.video_count = stable_selectbox(
                tr("Number of Videos Generated Simultaneously"),
                options=video_count_options,
                default_value=_saved_ui_choice(
                    "video_count", video_count_options, 1
                ),
                key="video_count_select",
            )
            _set_runtime_config("ui", "video_count", params.video_count)

            video_codec_options = [
                (tr("Default Video Encoder"), DEFAULT_VIDEO_CODEC_OPTION),
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get(
                "video_codec", DEFAULT_VIDEO_CODEC_OPTION
            )
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                # Các phiên bản cũ hơn hoặc cấu hình thủ công có thể để lại các giá trị không hợp lệ. Giao diện người dùng trở về "mặc định" thay vì thay thế người dùng
                # Đã sửa một bộ mã hóa nhất định và phần phụ trợ vẫn sẽ phân giải thành libx264 theo chính sách ổn định.
                saved_video_codec = DEFAULT_VIDEO_CODEC_OPTION
            selected_video_codec = stable_selectbox(
                tr("Video Encoder"),
                options=saved_video_codec_values,
                default_value=saved_video_codec,
                key="video_encoder_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_codec_options
                )[value],
                help=tr("Video Encoder Help"),
            )
            if selected_video_codec == DEFAULT_VIDEO_CODEC_OPTION:
                # Chế độ mặc định không duy trì các bộ mã hóa cụ thể, cho phép cấu hình thể hiện "tuân theo các giá trị mặc định của dự án".
                _delete_runtime_config("app", "video_codec")
            else:
                _set_runtime_config("app", "video_codec", selected_video_codec)

            if params.video_source == "loomloom":
                _render_loomloom_video_settings(params)

            if params.video_source == "wavespeed":
                _render_wavespeed_video_settings(params)
            if params.video_source == "volcengine_seedance":
                _render_seedance_video_settings(params)
            if params.video_source == "ofox":
                _render_ofox_video_settings(params)
            if params.video_source == "metaso_minimax":
                _render_metaso_minimax_video_settings(params)
    return uploaded_files


def _render_wavespeed_video_settings(params):
    """
    渲染 WaveSpeed 生成数量估算与计费确认。

    Khi tạo hóa đơn cho mỗi mặt hàng, người dùng phải có thể xem số lượng phân khúc gần đúng sẽ được tạo trước khi gửi. Các ước tính hoàn toàn mang tính địa phương
    Hoàn thành: Chia khoảng thời gian ước tính thời lượng lồng tiếng cho thời lượng clip để có được số lượng clip cần cover. Dòng nguyên liệu
    本身按需逐段生成、凑够所需时长即停，因此实际生成数以运行时为准，
    估算只用于量级提示，不参与任务执行。
    """
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""),
        params.voice_rate,
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("WaveSpeed Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("WaveSpeed Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm WaveSpeed Charge"),
        key="wavespeed_confirm_charge",
        help=tr("Confirm WaveSpeed Charge Help"),
    )


def _render_seedance_video_settings(params):
    """Hiển thị số lượng nhiệm vụ phải trả dự kiến ​​và yêu cầu người dùng xác nhận rõ ràng phí tạo Ark."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Volcano Engine Seedance Billing Notice").format(
                min=min_clips, max=max_clips
            )
        )
    else:
        st.warning(tr("Volcano Engine Seedance Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Volcano Engine Seedance Charge"),
        key="volcengine_seedance_confirm_charge",
        help=tr("Confirm Volcano Engine Seedance Charge Help"),
    )


def _render_ofox_video_settings(params):
    """Hiển thị số lượng nhiệm vụ phải trả phí dự kiến ​​và yêu cầu người dùng xác nhận rõ ràng rằng OOX tạo ra phí."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("OFox Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("OFox Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm OFox Charge"),
        key="ofox_confirm_charge",
        help=tr("Confirm OFox Charge Help"),
    )


def _render_metaso_minimax_video_settings(params):
    """Hiển thị số lượng nhiệm vụ phải trả ước tính và yêu cầu người dùng xác nhận phí tạo Secret Tower MiniMax."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    voice_mode = st.session_state.get(
        localized_widget_key("voice_mode_control"),
        config.ui.get("voice_mode"),
    )
    if voice_mode == VOICE_MODE_UPLOAD:
        # Cài đặt video được hiển thị trước cài đặt âm thanh. Tại thời điểm này, giá trị thực của các tệp mới được tải lên trong vòng này không thể đọc được một cách đáng tin cậy.
        # khoảng thời gian. Chế độ tải lên không còn hiển thị các con số được tính toán dựa trên văn bản tập lệnh để tránh người dùng nhầm tưởng rằng một
        # Âm thanh 5 giây cũng sẽ tạo ra nhiều tác vụ phải trả phí dựa trên thời gian viết quảng cáo dài hơn; thời lượng thực tế của tệp sẽ vẫn chiếm ưu thế trong thời gian chạy.
        st.warning(
            tr("Metaso MiniMax Billing Notice Uploaded Audio").format(
                resolution=str(
                    config.app.get(
                        "metaso_minimax_resolution",
                        metaso_minimax.DEFAULT_RESOLUTION,
                    )
                    or metaso_minimax.DEFAULT_RESOLUTION
                ),
                duration=clip_duration,
                count=video_count,
            )
        )
    elif estimated_range := _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    ):
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Metaso MiniMax Billing Notice").format(
                min=min_clips,
                max=max_clips,
                resolution=str(
                    config.app.get(
                        "metaso_minimax_resolution",
                        metaso_minimax.DEFAULT_RESOLUTION,
                    )
                    or metaso_minimax.DEFAULT_RESOLUTION
                ),
            )
        )
    else:
        st.warning(tr("Metaso MiniMax Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Metaso MiniMax Charge"),
        key="metaso_minimax_confirm_charge",
        help=tr("Confirm Metaso MiniMax Charge Help"),
    )


def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """
    Ước tính cục bộ thời lượng lồng tiếng hoàn chỉnh, trả về giới hạn trên và dưới thận trọng tính bằng giây.

    该估算只用于帮助用户在调用付费 TTS 前判断文案量级，不参与任务执行。
    Tiếng Trung, tiếng Nhật và tiếng Hàn được ước tính dựa trên tốc độ ký tự và các ngôn ngữ khác sử dụng phân đoạn từ không gian được ước tính dựa trên tốc độ từ.
    Các dấu ngắt câu phổ biến cũng được bao gồm. Các nhà cung cấp, âm sắc và tông màu khác nhau sẽ gây ra sai lệch thực tế, vì vậy giao diện
    Một khoảng phải được trình bày thay vì một kết quả đơn lẻ giả chính xác.
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(re.findall(r"[,，.。!?！？;；:：]", normalized_text))

    # 4,2 từ/giây và 2,6 từ/giây gần bằng tốc độ bình luận hàng ngày; nhấn 0,12 giây cho dấu câu để thêm một chút tạm dừng.
    # voice_rate chỉ được sử dụng làm công cụ sửa đổi ước tính. TTS được tạo một phần không thực thi nghiêm ngặt việc phóng đại, vì vậy cuối cùng
    # Khoảng ±15% vẫn được giữ lại để tránh người dùng nhầm tưởng rằng giá trị này tương đương với kết quả thực ở phía máy chủ.
    base_seconds = len(script_chars) / 4.2 + len(words) / 2.6 + punctuation_count * 0.12
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )


def _get_voice_preview_sample(voice_name: str) -> str:
    """Trả về bản thử giọng ngắn phù hợp với âm sắc hiện tại mà không cần sử dụng bản sao video đầy đủ của người dùng."""
    # ElevenLabs 音色缺少明确语言字段时，根据展示名称中的越南语字符选择
    # Hãy nghe bản sao và tránh sử dụng ngôn ngữ không khớp rõ ràng để đánh giá hiệu ứng âm sắc.
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        display = parts[2] if len(parts) >= 3 else ""
        vietnamese_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
        if any(char in vietnamese_chars for char in display):
            return "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
    return tr("Voice Example")


def _voice_preview_fingerprint(
    *,
    preview_type: str,
    content: str,
    tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    provider_signature: dict,
) -> str:
    """Tạo dấu vân tay trong bộ đệm thử giọng và tự động vô hiệu hóa các kết quả thử giọng cũ sau khi có bất kỳ thay đổi nào về tham số lồng tiếng."""
    payload = {
        "preview_type": preview_type,
        "content": content,
        "tts_server": tts_server,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_volume": voice_volume,
        "provider_signature": provider_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_signature(value: str) -> str:
    """
    Tạo thông báo xác thực chỉ được sử dụng để xác định tính vô hiệu của bộ đệm.

    Bản tóm tắt không được ghi vào tệp cấu hình, nhật ký hoặc tác vụ. Sau khi người dùng sửa đổi API Key, phần tóm tắt sẽ thay đổi, do đó
    Buộc thu hồi dịch vụ lồng tiếng hiện tại để tránh các bộ đệm thử giọng cũ khiến thông tin xác thực mới không hợp lệ xuất hiện.
    """
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def _get_voice_preview_provider_signature(tts_server: str) -> dict:
    """
    Trả về cấu hình Nhà cung cấp không nhạy cảm ảnh hưởng đến kết quả nghe.

    Khóa API chỉ tham gia vào dấu vân tay bộ đệm dưới dạng thông báo một chiều và thông tin xác thực ban đầu không nhập vào bộ đệm hoặc nhật ký. Người mẫu,
    服务地址、区域或凭证发生变化时都必须重新生成试听，否则界面可能继续播放
    Âm thanh theo cấu hình Nhà cung cấp cũ khiến người dùng lầm tưởng rằng cài đặt hiện tại đã có hiệu lực.
    """
    if tts_server == "azure-tts-v2":
        return {
            "speech_region": config.azure.get("speech_region", ""),
            "credential": _credential_signature(config.azure.get("speech_key", "")),
        }
    if tts_server == "siliconflow":
        return {
            "credential": _credential_signature(config.siliconflow.get("api_key", ""))
        }
    if tts_server == "gemini-tts":
        return {
            "credential": _credential_signature(config.app.get("gemini_api_key", ""))
        }
    if tts_server == "mimo-tts":
        return {"credential": _credential_signature(config.app.get("mimo_api_key", ""))}
    if tts_server == "minimax-tts":
        return {
            "base_url": voice.get_minimax_tts_endpoint(),
            "model_id": config.minimax_tts.get("model_id", ""),
            "voice_id": config.minimax_tts.get("voice_id", ""),
            "credential": _credential_signature(voice.get_minimax_tts_api_key()),
        }
    if tts_server == "elevenlabs":
        return {
            "model_id": config.elevenlabs.get("model_id", ""),
            "credential": _credential_signature(config.elevenlabs.get("api_key", "")),
        }
    if tts_server == "chatterbox":
        return {
            "base_url": config.chatterbox.get("base_url", ""),
            "model_id": config.chatterbox.get("model_id", ""),
            "credential": _credential_signature(config.chatterbox.get("api_key", "")),
        }
    if tts_server == "kokoro":
        return {
            "base_url": config.kokoro.get("base_url", ""),
            "model_id": config.kokoro.get("model_id", ""),
            "credential": _credential_signature(config.kokoro.get("api_key", "")),
        }
    return {}


def _synthesize_voice_preview(
    *,
    content: str,
    preview_type: str,
    selected_tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
) -> dict | None:
    """Bản thử giọng được tạo một lần và được chuyển vào bộ nhớ đệm, các tệp tạm thời không được lưu giữ trong các phiên."""
    if selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()
    if selected_tts_server == "kokoro":
        _sync_kokoro_config_from_session_state()

    temp_dir = utils.storage_dir("temp", create=True)
    audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
    logger.info(
        f"generating {preview_type} voice preview: "
        f"voice={voice_name}, rate={voice_rate}, volume={voice_volume}, "
        f"text_length={len(content)}"
    )
    try:
        with config.try_runtime_config_lock() as lock_acquired:
            if not lock_acquired:
                return {"busy": True}
            sub_maker = voice.tts(
                text=content,
                voice_name=voice_name,
                voice_rate=voice_rate,
                voice_file=audio_file,
                voice_volume=voice_volume,
            )
        if not sub_maker or not os.path.exists(audio_file):
            logger.error(f"{preview_type} voice preview did not produce an audio file")
            return None

        with open(audio_file, "rb") as file:
            audio_bytes = file.read()
        if not audio_bytes:
            logger.error(f"voice preview audio file is empty: {audio_file}")
            return None

        duration = voice.get_audio_duration(audio_file)
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            logger.warning(
                f"voice preview duration is unavailable: "
                f"preview_type={preview_type}, voice={voice_name}"
            )
            duration = None

        return {
            "audio_bytes": audio_bytes,
            "mime_type": _detect_audio_mime(audio_file, audio_bytes),
            "duration": duration,
            "preview_type": preview_type,
            "sub_maker": sub_maker,
            # Bảng video phía trước bảng âm thanh chỉ sử dụng
            # Thời lượng thử giọng đầy đủ; những buổi thử giọng ngắn hoặc bản sao cũ sẽ không bao giờ thay đổi số lượng tài liệu được đề xuất.
            "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "tts_server": selected_tts_server,
            "voice_name": voice_name,
            "voice_rate": float(voice_rate),
        }
    finally:
        # Trình phát của trình duyệt sử dụng byte bộ nhớ và các tệp có thể được dọn sạch sau khi đọc để tránh tích tụ các tệp tạm thời để nghe thường xuyên.
        try:
            os.remove(audio_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Lỗi dọn dẹp không được ghi đè lên các phản hồi hoặc ngoại lệ TTS thực, nhưng các đường dẫn và lỗi hệ thống cần được giữ nguyên,
            # Thật thuận tiện để khắc phục các vấn đề về môi trường như quyền và hệ thống tệp chỉ đọc.
            logger.warning(
                f"failed to delete voice preview file {audio_file}: {str(exc)}"
            )


def _render_voice_preview(params, friendly_names, selected_tts_server, voice_name):
    """Thực hiện các buổi thử giọng ngắn với chi phí thấp, ước tính thời lượng viết quảng cáo đầy đủ và bản xem trước lồng tiếng đầy đủ theo yêu cầu."""
    if not friendly_names:
        return

    script_content = str(params.video_script or "").strip()
    estimated_range = _estimate_voiceover_duration_range(
        script_content,
        params.voice_rate,
    )
    if estimated_range:
        st.caption(
            tr("Estimated Voiceover Duration").format(
                min=estimated_range[0],
                max=estimated_range[1],
            )
        )
    else:
        st.caption(tr("Voiceover Script Required"))

    sample_content = _get_voice_preview_sample(voice_name)
    provider_signature = _get_voice_preview_provider_signature(selected_tts_server)
    preview_columns = st.columns(2)
    short_preview_requested = preview_columns[0].button(
        tr("Play Voice"),
        key="play_voice_button",
        icon=":material/graphic_eq:",
        use_container_width=True,
    )
    full_preview_requested = preview_columns[1].button(
        tr("Generate Full Voiceover Preview"),
        key="generate_full_voiceover_preview_button",
        icon=":material/article:",
        help=tr("Full Voiceover Preview Cost Hint"),
        use_container_width=True,
        disabled=not bool(script_content),
    )

    preview_type = ""
    preview_content = ""
    if short_preview_requested:
        preview_type = "sample"
        preview_content = sample_content
    elif full_preview_requested:
        preview_type = "full"
        preview_content = script_content

    sample_fingerprint = _voice_preview_fingerprint(
        preview_type="sample",
        content=sample_content,
        tts_server=selected_tts_server,
        voice_name=voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=provider_signature,
    )
    full_fingerprint = (
        _voice_preview_fingerprint(
            preview_type="full",
            content=script_content,
            tts_server=selected_tts_server,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_volume=params.voice_volume,
            provider_signature=provider_signature,
        )
        if script_content
        else ""
    )

    if preview_type:
        requested_fingerprint = (
            sample_fingerprint if preview_type == "sample" else full_fingerprint
        )
        cached_preview = st.session_state.get("voice_preview_audio")
        if (
            not cached_preview
            or cached_preview.get("fingerprint") != requested_fingerprint
        ):
            try:
                with st.spinner(tr("Synthesizing Voice")):
                    preview_result = _synthesize_voice_preview(
                        content=preview_content,
                        preview_type=preview_type,
                        selected_tts_server=selected_tts_server,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_volume=params.voice_volume,
                    )
            except Exception as exc:
                logger.exception(f"failed to generate {preview_type} voice preview")
                st.error(tr("Voice Preview Failed").format(error=str(exc)))
            else:
                if preview_result and preview_result.get("busy"):
                    st.warning(tr("Voice Preview Busy"))
                elif preview_result:
                    preview_result["fingerprint"] = requested_fingerprint
                    st.session_state["voice_preview_audio"] = preview_result
                    if (
                        preview_type == "full"
                        and params.video_source == "loomloom"
                        and isinstance(preview_result.get("duration"), (int, float))
                        and math.isfinite(preview_result["duration"])
                        and preview_result["duration"] > 0
                    ):
                        # 视频设置先于音频设置渲染。完整试听成功后触发一次 rerun，
                        # Hãy để số lượng tài liệu trên ngay lập tức được đề xuất lại theo thời lượng tường thuật thực tế và làm mới lời nhắc đưa tin.
                        st.session_state["loomloom_video_scene_autofill_digest"] = (
                            preview_result["content_digest"]
                        )
                        st.rerun()
                else:
                    st.error(tr("Voice Preview No Audio"))

    cached_preview = st.session_state.get("voice_preview_audio")
    valid_fingerprints = {sample_fingerprint, full_fingerprint}
    if (
        cached_preview
        and cached_preview.get("fingerprint") in valid_fingerprints
        and cached_preview.get("audio_bytes")
    ):
        # Nó sẽ chỉ phát tự động khi người dùng nhấp vào "Âm thanh âm thanh" một cách rõ ràng lần này. Các điều khiển khác cho Streamlit
        # Nó cũng sẽ kích hoạt việc chạy lại trang; nếu tính năng tự động phát được bật vĩnh viễn cho âm thanh được lưu trong bộ nhớ đệm, hãy sửa đổi mọi cài đặt
        # Có thể chơi thử giọng cũ ngay từ đầu. Tiếp tục phát lại thủ công cho toàn bộ buổi thử giọng để tránh âm thanh dài
        # Làm gián đoạn người dùng một cách bất ngờ sau khi quá trình xây dựng hoàn tất.
        should_autoplay = bool(
            short_preview_requested
            and cached_preview.get("preview_type") == "sample"
            and cached_preview.get("fingerprint") == sample_fingerprint
        )
        st.audio(
            cached_preview["audio_bytes"],
            format=cached_preview.get("mime_type", "audio/mp3"),
            autoplay=should_autoplay,
        )
        if cached_preview.get("preview_type") == "full":
            duration = cached_preview.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                st.caption(
                    tr("Actual Voiceover Duration").format(duration=f"{duration:.1f}")
                )
            else:
                st.warning(tr("Voice Preview Duration Unavailable"))


def _get_reusable_full_voice_preview(params, voice_mode: str) -> dict | None:
    """
    Trả về bộ đệm thử giọng hoàn chỉnh khớp chính xác với các tham số bản dựng hiện tại.

    Chỉ bản sao chép hoàn chỉnh mới được sử dụng lại cho buổi thử giọng và các mẫu giai điệu ngắn không bao giờ được đưa vào nhiệm vụ chính thức. Dấu vân tay bao phủ thống nhất việc viết quảng cáo,
    Nhà cung cấp, âm sắc, tốc độ giọng nói, âm lượng và tóm tắt cấu hình không nhạy cảm; mọi thay đổi tham số sẽ tự nhiên quay trở lại
    正常 TTS 流程。字幕时间轴和有效时长同样是必需条件，避免只复用音频后让
    Edge 字幕链路失去 SubMaker。
    """
    if voice_mode != VOICE_MODE_TTS:
        return None

    script_content = str(params.video_script or "").strip()
    selected_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    if (
        not script_content
        or not params.voice_name
        # Các video chính thức sẽ áp dụng thống nhất âm lượng lồng tiếng trong giai đoạn tổng hợp MoviePy; một số Nhà cung cấp sẽ
        # Tăng âm lượng được viết trực tiếp trong giai đoạn TTS.
        # Do đó, trước tiên, chúng tôi thận trọng quay lại quy trình ban đầu để tránh đưa ra các đánh giá đặc biệt cho Nhà cung cấp đối với một số ít trường hợp.
        or not math.isclose(float(params.voice_volume), 1.0)
    ):
        return None

    expected_fingerprint = _voice_preview_fingerprint(
        preview_type="full",
        content=script_content,
        tts_server=selected_tts_server,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=_get_voice_preview_provider_signature(selected_tts_server),
    )
    cached_preview = st.session_state.get("voice_preview_audio")
    if (
        not cached_preview
        or cached_preview.get("fingerprint") != expected_fingerprint
        or cached_preview.get("preview_type") != "full"
        or not cached_preview.get("audio_bytes")
        or cached_preview.get("sub_maker") is None
    ):
        return None

    duration = cached_preview.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None

    return {
        "audio_bytes": bytes(cached_preview["audio_bytes"]),
        "duration": float(duration),
        "sub_maker": cached_preview["sub_maker"],
        "script": script_content,
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }


def _sync_minimax_tts_api_key_input():
    """
    Đồng bộ hóa kiểm soát mật khẩu MiniMax TTS và trả lại Khóa hiện hợp lệ.

    Khóa MiniMax LLM được phép sử dụng lại khi Khóa chuyên dụng TTS trống. Khóa chia sẻ chỉ được sử dụng cho điều khiển hiện tại và
    Các yêu cầu không được tự động sao chép vào [minimax_tts] để tránh việc duy trì nhiều lần các thông tin xác thực tương tự trong tệp cấu hình.
    """
    widget_key = "minimax_tts_api_key_input"
    configured_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    shared_key = str(
        config.app.get("minimax_api_key", "") or os.getenv("MINIMAX_API_KEY", "") or ""
    ).strip()
    effective_key = configured_key or shared_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # Trình duyệt có thể phát lại trạng thái mật khẩu trống khi kết nối lại. Khôi phục thông tin xác thực đã định cấu hình để ngăn giá trị null ghi đè cấu hình.
        # Đồng thời, đảm bảo rằng yêu cầu thử giọng chạy lại hiện tại có thể trực tiếp sử dụng Khóa hợp lệ.
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored MiniMax TTS API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = effective_key
        entered_key = effective_key

    if entered_key and entered_key != effective_key:
        _set_runtime_config("minimax_tts", "api_key", entered_key)

    return entered_key


def _get_cached_minimax_voices(api_key: str, endpoint: str) -> list[dict[str, str]]:
    """Đọc kết quả truy vấn bản vá MiniMax cho phiên hiện tại theo trang web và tóm tắt thông tin xác thực."""
    cache = st.session_state.get("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cached_voices = cache.get(cache_key, [])
    return cached_voices if isinstance(cached_voices, list) else []


def _cache_minimax_voices(
    api_key: str,
    endpoint: str,
    voices: list[dict[str, str]],
):
    """Bộ nhớ đệm chủ động truy vấn âm sắc để tránh các yêu cầu lặp lại đối với MiniMax sau khi chạy lại các điều khiển thông thường."""
    cache = st.session_state.setdefault("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cache[cache_key] = voices


def _render_minimax_tts_settings() -> tuple[list[str], dict[str, str]]:
    """Hiển thị cấu hình MiniMax TTS và trả về các tùy chọn cũng như văn bản được bộ chọn bản vá hợp nhất sử dụng."""
    effective_api_key = _sync_minimax_tts_api_key_input()
    effective_api_key = st.text_input(
        tr("MiniMax TTS API Key"),
        type="password",
        key="minimax_tts_api_key_input",
    ).strip()

    dedicated_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    minimax_tts_endpoints = [voice.MINIMAX_TTS_GLOBAL_URL, voice.MINIMAX_TTS_CN_URL]
    effective_endpoint = voice.get_minimax_tts_endpoint()
    if effective_endpoint not in minimax_tts_endpoints:
        effective_endpoint = voice.MINIMAX_TTS_GLOBAL_URL
    minimax_tts_base_url = stable_selectbox(
        tr("MiniMax TTS Endpoint"),
        options=minimax_tts_endpoints,
        default_value=effective_endpoint,
        key="minimax_tts_endpoint_select",
        # Khi sử dụng lại Khóa LLM, bạn phải đi theo khu vực đặt LLM để ngăn giao diện cho phép bạn chọn một khóa thực tế
        # Địa chỉ sẽ không hợp lệ; bạn có thể chọn trang web riêng lẻ sau khi điền Khóa TTS độc lập.
        disabled=not dedicated_key,
    )
    if dedicated_key:
        _set_runtime_config("minimax_tts", "base_url", minimax_tts_base_url)

    configured_model = config.minimax_tts.get(
        "model_id", voice.MINIMAX_TTS_DEFAULT_MODEL
    )
    if configured_model not in voice.MINIMAX_TTS_MODELS:
        configured_model = voice.MINIMAX_TTS_DEFAULT_MODEL
    minimax_tts_model = stable_selectbox(
        tr("MiniMax TTS Model"),
        options=list(voice.MINIMAX_TTS_MODELS),
        default_value=configured_model,
        key="minimax_tts_model_select",
    )
    _set_runtime_config("minimax_tts", "model_id", minimax_tts_model)

    if st.button(
        tr("Load MiniMax Voices"),
        key="load_minimax_voices_button",
        icon=":material/refresh:",
        use_container_width=True,
    ):
        try:
            available_voices = voice.get_minimax_voice_catalog(
                api_key=effective_api_key,
                endpoint=minimax_tts_base_url,
                voice_type="all",
            )
        except Exception as exc:
            # Ở đây các trường hợp ngoại lệ phải được hiển thị cho người dùng và ghi lại.
            # Hoặc lỗi mạng là chuyện thường xảy ra, việc âm thầm trả về danh sách trống sẽ khiến người dùng lầm tưởng rằng tài khoản không có âm thanh.
            logger.warning(f"load MiniMax voices failed: {exc}")
            st.error(tr("MiniMax Voices Load Failed").format(error=str(exc)))
        else:
            _cache_minimax_voices(
                effective_api_key,
                minimax_tts_base_url,
                available_voices,
            )
            st.success(tr("MiniMax Voices Loaded").format(count=len(available_voices)))

    available_voices = _get_cached_minimax_voices(
        effective_api_key,
        minimax_tts_base_url,
    )
    voice_labels = {
        f"minimax:{item['voice_id']}": (
            f"{item['voice_name']} ({item['voice_id']})"
            if item["voice_name"] != item["voice_id"]
            else item["voice_id"]
        )
        for item in available_voices
    }
    configured_voice_id = str(
        config.minimax_tts.get("voice_id", voice.MINIMAX_TTS_DEFAULT_VOICE)
        or voice.MINIMAX_TTS_DEFAULT_VOICE
    ).strip()
    configured_voice = f"minimax:{configured_voice_id}"
    # Nếu bạn chưa nhấp để lấy âm thanh, giao diện tạm thời không khả dụng hoặc âm thanh nhân bản không được định cấu hình để sử dụng trong danh sách thì âm thanh đó vẫn được giữ lại.
    # ID giọng nói hiện tại đảm bảo rằng quá trình tạo ban đầu không phụ thuộc vào kết quả truy vấn giọng nói từ xa.
    voice_labels.setdefault(configured_voice, configured_voice_id)
    return list(voice_labels), voice_labels


def _sync_elevenlabs_api_key_input():
    """
    Đồng bộ hóa kiểm soát mật khẩu ElevenLabs, các biến môi trường và cấu hình liên tục, đồng thời trả về Khóa hiện hợp lệ.

    Streamlit có thể phát lại kiểm soát mật khẩu trống khi tab trình duyệt được kết nối với dịch vụ được khởi động lại
    tình trạng. Giá trị null này không thể được phân biệt một cách đáng tin cậy với việc xóa do người dùng thực hiện, vì vậy khi tệp cấu hình hoặc biến môi trường vẫn có
    Chìa khóa, ưu tiên khôi phục giá trị hợp lệ để ngăn trạng thái trống ghi đè cấu hình và đảm bảo rằng việc chạy lại này có thể được tải ngay lập tức.
    âm sắc. Khi cần xóa hoàn toàn Key, bạn nên sửa lại file cấu hình hoặc biến môi trường để tránh đánh giá sai trong quá trình kết nối lại.
    """
    widget_key = "elevenlabs_api_key_input"
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    env_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    effective_key = configured_key or env_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # Trạng thái trống sau khi kết nối lại không thể ghi đè thông tin xác thực hợp lệ và phải được khôi phục trước khi hiển thị danh sách âm thanh.
        # Ngược lại, mặc dù tệp cấu hình chưa bị xóa nhưng trang hiện tại sẽ vẫn sử dụng Khóa trống để yêu cầu ElevenLabs.
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored ElevenLabs API key after empty session replay")
    elif not had_widget_state:
        # Khởi tạo trước rồi tạo điều khiển để tránh truyền giá trị và session_state cùng lúc để kích hoạt Streamlit
        # Cảnh báo xung đột giá trị mặc định; chỉ khởi tạo nó để trống khi không có Khóa.
        st.session_state[widget_key] = entered_key

    if entered_key and entered_key != effective_key:
        # Chỉ những giá trị mới được người dùng chủ động nhập mới được thả vào config.toml. Các biến môi trường không được chèn lấp dưới dạng giá trị hợp lệ
        # Các khóa được chèn được sao chép vào tệp, vùng chứa hoặc nền tảng triển khai chỉ còn lại trong môi trường thời gian chạy.
        for cache_key in list(st.session_state.keys()):
            if str(cache_key).startswith("elevenlabs_voices_"):
                del st.session_state[cache_key]
        _set_runtime_config("elevenlabs", "api_key", entered_key)

    return entered_key


def _render_elevenlabs_api_key_input(label_key):
    """
    Hiển thị trạng thái đầu vào Khóa API duy nhất mà ElevenLabs TTS chia sẻ với nhạc nền.

    Nếu hai phím widget được sử dụng cho TTS và nhạc nền trên cùng một trang, Streamlit sẽ giữ lại các giá trị cũ tương ứng.
    Các hộp nhập liệu được hiển thị sau cũng ghi đè cấu hình được chia sẻ. Ở đây, một khóa được sử dụng và các biến môi trường được xử lý tập trung.
    Việc chèn lấp, cập nhật cấu hình và vô hiệu hóa bộ nhớ đệm âm thanh đảm bảo rằng giao diện hiển thị và tác vụ nền luôn đọc cùng một giá trị.
    """
    _sync_elevenlabs_api_key_input()
    return st.text_input(
        tr(label_key),
        type="password",
        key="elevenlabs_api_key_input",
    ).strip()


def _render_background_music_settings(params, elevenlabs_api_key_rendered=False):
    """Hiển thị cài đặt âm lượng và nguồn nhạc nền, đồng thời trả lại tệp đã tải lên để lưu lần này."""
    uploaded_bgm_file = None
    previous_bgm_type = st.session_state.get("last_rendered_bgm_type")
    st.divider()
    bgm_options = [
        (tr("No Background Music"), ""),
        (tr("Random Background Music"), "random"),
        (tr("Preset Song"), "preset"),
        (tr("Custom Background Music"), "custom"),
        (tr("Sonilo Background Music"), "sonilo"),
        (tr("ElevenLabs Background Music"), "elevenlabs"),
    ]
    selected_bgm_type = stable_selectbox(
        tr("Background Music Source"),
        options=[value for _, value in bgm_options],
        default_value=_saved_ui_choice(
            "bgm_type",
            [value for _, value in bgm_options],
            "random",
        ),
        key="bgm_type_select",
        format_func=lambda value: dict((v, label) for label, v in bgm_options)[value],
    )
    params.bgm_type = selected_bgm_type
    _set_runtime_config("ui", "bgm_type", params.bgm_type)
    if params.bgm_type == "sonilo":
        configured_key = str(config.app.get("sonilo_api_key", "") or "").strip()
        effective_key = configured_key or os.getenv("SONILO_API_KEY", "").strip()
        entered_key = st.text_input(
            tr("Sonilo API Key"),
            value=effective_key,
            type="password",
            key="sonilo_api_key_input",
        ).strip()
        # Người dùng yêu cầu Khóa được định cấu hình phải được điền trực tiếp vào hộp nhập mật khẩu. Giá trị cấu hình được ưu tiên hơn các biến môi trường;
        # Chỉ ghi lại khi người dùng thực sự thay đổi đầu vào hoặc sử dụng cấu hình để tránh thay đổi Key trong biến môi trường.
        # Sao chép vào config.toml mà không cần thao tác gì.
        if configured_key or entered_key != effective_key:
            _set_runtime_config("app", "sonilo_api_key", entered_key)
    elif params.bgm_type == "elevenlabs":
        if elevenlabs_api_key_rendered:
            # Khi hộp nhập liệu dùng chung đã được hiển thị trong khu vực TTS, tiện ích thứ hai sẽ không còn được tạo để tránh hai tiện ích độc lập.
            # các giá trị session_state ghi đè lên nhau. Văn bản mô tả giúp người dùng định vị cấu hình được chia sẻ ở trên.
            st.caption(tr("ElevenLabs API Key Help"))
        else:
            _render_elevenlabs_api_key_input("ElevenLabs Music API Key")

    bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    params.bgm_volume = stable_selectbox(
        tr("Background Music Volume"),
        options=bgm_volume_options,
        default_value=_saved_ui_choice("bgm_volume", bgm_volume_options, 0.2),
        key="bgm_volume_select",
        format_func=lambda value: f"{int(value * 100)}%",
        disabled=not params.bgm_type,
    )
    _set_runtime_config("ui", "bgm_volume", params.bgm_volume)
    bgm_enabled = bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)

    if params.bgm_type == "custom":
        uploaded_bgm_file = st.file_uploader(
            tr("Upload Background Music"),
            type=[
                extension.removeprefix(".")
                for extension in bgm_service.SUPPORTED_BGM_EXTENSIONS
            ],
            accept_multiple_files=False,
            key="custom_bgm_uploader",
            help=tr("Upload Background Music Help"),
            # Streamlit 默认会在控件上展示全局 200MB 上限。这里必须与服务层
            # Giới hạn cứng 30 MB vẫn được giữ nguyên để tránh bị máy chủ từ chối chỉ khi giao diện cho phép lựa chọn và submit.
            max_upload_size=bgm_service.MAX_BGM_UPLOAD_BYTES // (1024 * 1024),
        )
        if uploaded_bgm_file is not None and bgm_enabled:
            try:
                safe_name = bgm_service.sanitize_upload_filename(uploaded_bgm_file.name)
                # Streamlit 在调整音量等任意控件后都会重新执行页面。使用内容哈希
                # Phân biệt các tệp đã tải lên và lưu vào bộ đệm kết quả giải mã hoàn chỉnh trong phiên hiện tại. Bạn không thể chỉ dựa vào cùng một tên,
                # Việc sử dụng sai kết quả cũ cho các tệp có cùng kích thước cũng tránh việc gọi FFmpeg liên tục cho mỗi lần chạy lại.
                validation_key = (
                    safe_name,
                    uploaded_bgm_file.size,
                    hashlib.sha256(uploaded_bgm_file.getbuffer()).hexdigest(),
                )
                cached_validation = st.session_state.get("custom_bgm_validation")
                if (
                    not cached_validation
                    or cached_validation.get("key") != validation_key
                ):
                    try:
                        bgm_service.validate_bgm_upload(
                            uploaded_bgm_file.name, uploaded_bgm_file
                        )
                    except bgm_service.BgmUploadError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "upload",
                        }
                        # Các kết quả không thành công của cùng một tệp vân tay sẽ được nhập vào bộ đệm phiên, vì vậy ở đây chỉ
                        # 首次真实执行校验时记录一次，避免普通控件 rerun 刷屏。
                        logger.warning(
                            "WebUI background music validation rejected: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    except bgm_service.BgmServiceError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "service",
                        }
                        logger.error(
                            "WebUI background music validation failed: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    else:
                        cached_validation = {
                            "key": validation_key,
                            "error": "",
                            "error_type": "",
                        }
                    st.session_state["custom_bgm_validation"] = cached_validation

                if cached_validation.get("error"):
                    if cached_validation.get("error_type") == "service":
                        raise bgm_service.BgmServiceError(cached_validation["error"])
                    raise bgm_service.BgmUploadError(cached_validation["error"])
            except bgm_service.BgmUploadError:
                # Các tệp bất hợp pháp không thể kế thừa tên của lần tải lên hợp lệ cuối cùng, nếu không các tham số tác vụ vẫn có thể trỏ đến
                # BGM lịch sử. Giữ giá trị trả về UploadFile để nó vẫn được hoàn thiện khi người dùng nhấp vào Tạo
                # Máy chủ xác minh việc chặn thay vì âm thầm tạo video không có nhạc nền.
                params.bgm_file = ""
                st.error(tr("Invalid Background Music"))
            except bgm_service.BgmServiceError:
                params.bgm_file = ""
                st.error(tr("Background Music Validation Failed"))
            else:
                # Trình phát và thông báo "Sẵn sàng" sẽ chỉ được hiển thị sau khi quá trình xác minh giải mã hoàn chỉnh được thông qua. Tập tin vẫn chỉ nhấp chuột
                # Tiếp tục trong quá trình xây dựng, người dùng chỉ xem trước hoặc sau đó xóa tệp sẽ không gây ô nhiễm bộ nhớ/bgm.
                uploaded_mime_type = str(getattr(uploaded_bgm_file, "type", "") or "")
                preview_mime_type = (
                    uploaded_mime_type
                    if uploaded_mime_type.startswith("audio/")
                    else mimetypes.guess_type(safe_name)[0] or "audio/mpeg"
                )
                st.audio(uploaded_bgm_file, format=preview_mime_type)
                st.info(f"{tr('Background Music Ready')}: {safe_name}")
                params.bgm_file = safe_name

        # Streamlit 会在条件控件暂时不渲染时清理其 widget state。
        # Sử dụng giá trị ổn định để khôi phục khi chuyển trở lại từ các nguồn BGM khác; dưới cùng một nguồn
        # previous_bgm_type không thay đổi khi người dùng chủ động xóa nó, do đó nó sẽ không bị trả lại bởi giá trị cũ.
        if previous_bgm_type != "custom":
            st.session_state["custom_bgm_file_input"] = _saved_ui_text(
                "custom_bgm_file"
            )
        custom_bgm_file = st.text_input(
            tr("Custom Background Music File"),
            key="custom_bgm_file_input",
            disabled=uploaded_bgm_file is not None,
        )
        _set_runtime_config(
            "ui", "custom_bgm_file", custom_bgm_file.strip()
        )
        if uploaded_bgm_file is None and custom_bgm_file and bgm_enabled:
            # 文件名由服务层映射到 storage/bgm 或 resource/songs 后校验，
            # Giao diện người dùng không chấp nhận bất kỳ đường dẫn nào ngoài hai thư mục được liệt kê trong danh sách cho phép.
            params.bgm_file = custom_bgm_file.strip()
        elif not bgm_enabled:
            # Kiểm soát tải lên tiếp tục giữ lại các tệp do người dùng chọn và lần chạy lại tiếp theo sau khi tăng âm lượng sẽ tự động
            # Hoàn thành xác minh; các tham số tác vụ hiện tại phải được xóa để ngăn tác vụ âm lượng 0 lưu hoặc phân tích cú pháp tệp.
            params.bgm_file = ""

    if params.bgm_type == "preset":
        # Lớp dịch vụ đã hoàn thành thống nhất phần mở rộng, tệp tạm thời và xác minh liên kết tượng trưng. Trực tiếp tái sử dụng nó ở đây
        # 结果，避免 UI 维护第二套枚举规则，后续新增格式时也不会出现差异。
        available_song_paths = bgm_service.list_builtin_bgm_files()
        songs_by_name = {
            os.path.basename(song_path): song_path for song_path in available_song_paths
        }
        available_songs = list(songs_by_name)
        if not available_songs:
            st.warning(tr("No Background Music Available"))
            params.bgm_file = ""
        else:
            default_preset_song = _saved_ui_text("preset_song", available_songs[0])
            requested_preset_song = st.session_state.get(
                localized_widget_key("preset_song_select"), default_preset_song
            )
            if requested_preset_song not in available_songs:
                # 历史任务或其它版本导出的设置可能引用当前安装中不存在的歌曲。
                # Sau lời nhắc rõ ràng, stable_selectbox sẽ trở lại bài hát đầu tiên để tránh việc thay đổi bài hát một cách âm thầm.
                st.warning(tr("Selected Background Music Unavailable"))
            selected_song = stable_selectbox(
                tr("Preset Song"),
                options=available_songs,
                default_value=(
                    default_preset_song
                    if default_preset_song in available_songs
                    else available_songs[0]
                ),
                key="preset_song_select",
            )
            _set_runtime_config("ui", "preset_song", selected_song)
            # Nghe trực tuyến được cung cấp ngay sau khi người dùng chọn bài hát. Trình phát đọc dữ liệu vừa truyền qua lớp dịch vụ
            # Đường dẫn thực có được bằng xác minh danh sách trắng không chấp nhận bất kỳ đường dẫn tệp nào được nhập trên trang.
            selected_song_path = songs_by_name[selected_song]
            preview_mime_type = (
                mimetypes.guess_type(selected_song_path)[0] or "audio/mpeg"
            )
            preview_available = True
            try:
                # Khi Streamlit không đọc được đường dẫn, nó sẽ đưa OSError vào một ngoại lệ bên trong, dẫn đến kết quả như sau
                # Không thể xử lý bằng lỗi tập tin. Hãy tự mình đọc các byte trước, điều này không chỉ duy trì hành vi của người chơi mà còn cho phép
                # Các tình huống như lỗi tạm thời khi gắn Docker và thay đổi về quyền sẽ dần dần rơi vào các nhánh có thể kiểm soát được.
                selected_song_bytes = Path(selected_song_path).read_bytes()
            except OSError as exc:
                preview_available = False
                # 文件可能在枚举后被其它进程删除。试听失败不能中断页面或视频
                # Chỉnh sửa tham số, nhưng cần lưu giữ nhật ký để xác định vị trí môi trường đang chạy và các vấn đề lắp đặt.
                logger.warning(
                    "failed to preview preset background music: "
                    f"name={selected_song}, error={str(exc)}"
                )
                st.warning(tr("Background Music Preview Failed"))
            else:
                st.audio(selected_song_bytes, format=preview_mime_type)
            if bgm_enabled and preview_available:
                params.bgm_file = selected_song
            else:
                params.bgm_file = ""

    if params.bgm_type == "sonilo":
        if previous_bgm_type != "sonilo":
            st.session_state["sonilo_bgm_prompt_input"] = _saved_ui_text(
                "sonilo_bgm_prompt",
                max_length=sonilo_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("Sonilo Music Prompt"),
            key="sonilo_bgm_prompt_input",
            max_chars=sonilo_service.MAX_PROMPT_LENGTH,
            help=tr("Sonilo Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "sonilo_bgm_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("Sonilo Multiple Videos Warning"))
        if st.button(
            tr("Test Sonilo Connection"),
            key="test_sonilo_connection_button",
            use_container_width=True,
        ):
            try:
                sonilo_service.test_connection()
            except sonilo_service.SoniloError as exc:
                logger.warning(f"Sonilo connection test failed: {exc}")
                st.error(tr("Sonilo Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("Sonilo Connection Test Succeeded"))
    elif params.bgm_type == "elevenlabs":
        if previous_bgm_type != "elevenlabs":
            st.session_state["elevenlabs_music_prompt_input"] = _saved_ui_text(
                "elevenlabs_music_prompt",
                max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("ElevenLabs Music Prompt"),
            key="elevenlabs_music_prompt_input",
            max_chars=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            help=tr("ElevenLabs Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "elevenlabs_music_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("ElevenLabs Multiple Videos Warning"))
        if st.button(
            tr("Test ElevenLabs Connection"),
            key="test_elevenlabs_music_connection_button",
            use_container_width=True,
        ):
            try:
                elevenlabs_music_service.test_connection()
            except elevenlabs_music_service.ElevenLabsPaidPlanRequiredError:
                st.error(tr("ElevenLabs Paid Plan Required"))
            except elevenlabs_music_service.ElevenLabsMusicError as exc:
                logger.warning(f"ElevenLabs connection test failed: {exc}")
                st.error(tr("ElevenLabs Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("ElevenLabs Connection Test Succeeded"))
    if params.bgm_type == "sonilo" and bgm_enabled and not sonilo_service.is_enabled():
        # Lớp tác vụ không tạo hoặc trộn nhạc nền Sonilo ở âm lượng 0, do đó không cần Dấu nhắc phím;
        # Phán quyết này chia sẻ các quy tắc của lớp dịch vụ với mục nhập nhiệm vụ để tránh sự phân chia giữa lời nhắc giao diện và điều kiện thực thi thực tế.
        st.warning(tr("Sonilo API Key Required"))
    elif (
        params.bgm_type == "elevenlabs"
        and bgm_enabled
        and not elevenlabs_music_service.is_enabled()
    ):
        st.warning(tr("ElevenLabs API Key Required"))
    st.session_state["last_rendered_bgm_type"] = params.bgm_type
    return uploaded_bgm_file


def _render_audio_settings(panel, params):
    """Kết xuất cài đặt âm thanh và trả về âm thanh đã tải lên cũng như chế độ lồng tiếng hiện tại."""
    with panel:
        with st.container(border=True):
            st.write(tr("Audio Settings"))

            # Chế độ lồng tiếng là trạng thái cấp đầu tiên của cài đặt âm thanh, có nhiệm vụ phân biệt rõ ràng lồng tiếng tự động, tải lên của người dùng và không lồng tiếng.
            # Khi cấu hình cũ không có voice_mode thì thiết bị canh gác không giọng nói theo tts_server gốc vẫn tương thích.
            saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
            saved_voice_mode = config.ui.get("voice_mode")
            if saved_voice_mode not in {
                VOICE_MODE_TTS,
                VOICE_MODE_UPLOAD,
                VOICE_MODE_NONE,
            }:
                saved_voice_mode = (
                    VOICE_MODE_NONE
                    if saved_tts_server == voice.NO_VOICE_NAME
                    else VOICE_MODE_TTS
                )
            voice_mode_options = [VOICE_MODE_TTS, VOICE_MODE_UPLOAD, VOICE_MODE_NONE]
            voice_mode_labels = {
                VOICE_MODE_TTS: tr("Automatic Voiceover"),
                VOICE_MODE_UPLOAD: tr("Upload Voiceover"),
                VOICE_MODE_NONE: tr("No Voiceover"),
            }
            voice_mode = stable_segmented_control(
                tr("Voiceover Mode"),
                options=voice_mode_options,
                default_value=saved_voice_mode,
                key="voice_mode_control",
                format_func=lambda value: voice_mode_labels[value],
                width="stretch",
            )
            _set_runtime_config("ui", "voice_mode", voice_mode)
            tts_mode_enabled = voice_mode == VOICE_MODE_TTS

            # Trình đơn thả xuống Nhà cung cấp chỉ chịu trách nhiệm chọn dịch vụ lồng tiếng tự động; không có lồng tiếng đã được kiểm soát bởi chế độ trên.
            # Nó không còn được đưa vào danh sách với tư cách là Nhà cung cấp TTS để ngăn hai mục nhập thể hiện cùng một trạng thái.
            tts_servers = [
                ("azure-tts-v1", "Azure TTS V1 (Edge TTS)"),
                ("azure-tts-v2", "Azure TTS V2"),
                ("siliconflow", "SiliconFlow TTS"),
                ("gemini-tts", "Google Gemini TTS"),
                ("mimo-tts", "Xiaomi MiMo TTS"),
                ("minimax-tts", "MiniMax TTS"),
                ("elevenlabs", "ElevenLabs TTS"),
                ("chatterbox", "Chatterbox TTS"),
                ("kokoro", "Kokoro TTS"),
                ("viettts", "VietTTS (Tiếng Việt)"),
                ("fish_audio", "Fish Audio TTS"),
            ]

            tts_server_values = [server_value for server_value, _ in tts_servers]
            if saved_tts_server not in tts_server_values:
                saved_tts_server = "azure-tts-v1"

            if tts_mode_enabled:
                selected_tts_server = stable_selectbox(
                    tr("Voiceover Service"),
                    options=tts_server_values,
                    default_value=saved_tts_server,
                    key="tts_server_select",
                    format_func=lambda value: dict(
                        (v, label) for v, label in tts_servers
                    )[value],
                )
            else:
                # Chế độ lồng tiếng không tự động không hiển thị điều khiển TTS nhưng vẫn giữ lại lựa chọn cuối cùng và có thể tiếp tục sử dụng lựa chọn đó sau khi chuyển trở lại.
                selected_tts_server = saved_tts_server

            _set_runtime_config("ui", "tts_server", selected_tts_server)

            # Mô tả dịch vụ tuân theo lựa chọn Nhà cung cấp, trước tiên cho người dùng biết những gì cần chuẩn bị, sau đó nhập âm sắc và
            # Cấu hình thông tin xác thực. Các nhà cung cấp không có mô tả sẽ không hiển thị các khối gợi ý trống.
            if tts_mode_enabled:
                provider_tips = get_tts_provider_tips(selected_tts_server)
                if provider_tips:
                    st.info(provider_tips)

            # MiniMax chỉ sử dụng lại bộ chọn "Âm thanh lồng tiếng" chung bên dưới. Chức năng cấu hình của nhà cung cấp chịu trách nhiệm
            # Làm mới giọng nói từ xa và quay lại văn bản thân thiện mà không hiển thị hộp thả xuống Voice ID và giọng nói.
            minimax_voices = []
            minimax_voice_labels = {}
            if tts_mode_enabled and selected_tts_server == "minimax-tts":
                minimax_voices, minimax_voice_labels = _render_minimax_tts_settings()

            # Nhận danh sách âm thanh dựa trên máy chủ TTS đã chọn
            filtered_voices = []
            saved_voice_name = config.ui.get("voice_name", "")
            elevenlabs_api_key_rendered = False

            if not tts_mode_enabled:
                # 上传音频和无配音模式不加载远程音色，减少无意义的网络请求和界面噪音。
                filtered_voices = []
            elif selected_tts_server == "siliconflow":
                # Nhận danh sách âm thanh chảy dựa trên silicon
                filtered_voices = voice.get_siliconflow_voices()
            elif selected_tts_server == "gemini-tts":
                # 获取Gemini TTS的声音列表
                filtered_voices = voice.get_gemini_voices()
            elif selected_tts_server == "mimo-tts":
                # Lấy danh sách âm cài sẵn cho Xiaomi MiMo TTS
                filtered_voices = voice.get_mimo_voices()
            elif selected_tts_server == "minimax-tts":
                filtered_voices = minimax_voices
            elif selected_tts_server == "elevenlabs":
                # Danh sách âm sắc được hiển thị trước hộp nhập Key. Nó phải được khôi phục về trạng thái kết nối lại và đọc.
                # Các biến cấu hình/môi trường, nếu không trang sẽ tải và lưu vào bộ nhớ đệm một danh sách âm thanh trống có Khóa trống.
                saved_elevenlabs_api_key = _sync_elevenlabs_api_key_input()
                cache_key = f"elevenlabs_voices_{saved_elevenlabs_api_key}"
                if cache_key not in st.session_state:
                    st.session_state[cache_key] = voice.get_elevenlabs_voices(
                        saved_elevenlabs_api_key
                    )
                filtered_voices = st.session_state[cache_key]
            elif selected_tts_server == "chatterbox":
                # Giọng nói cài sẵn cho các dịch vụ Chatterbox tự lưu trữ (từ cấu hình giọng nói [chatterbox])
                _sync_chatterbox_config_from_session_state()
                filtered_voices = voice.get_chatterbox_voices()
            elif selected_tts_server == "kokoro":
                # Giọng nói cho các dịch vụ Kokoro tự lưu trữ: [kokoro] Đọc từ máy chủ/âm thanh/giọng nói khi giọng nói trống
                _sync_kokoro_config_from_session_state()
                filtered_voices = _get_kokoro_voice_options(saved_voice_name)
            elif selected_tts_server == "viettts":
                # VietTTS: giọng Việt chuẩn, chạy local (dangvansam/viet-tts)
                filtered_voices = voice.get_viettts_voices()
            elif selected_tts_server == "fish_audio":
                filtered_voices = voice.get_fish_audio_voices()
            else:
                # Nhận danh sách âm thanh của Azure
                all_voices = voice.get_all_azure_voices(filter_locals=None)

                # 根据选择的TTS服务器筛选声音
                for v in all_voices:
                    if selected_tts_server == "azure-tts-v2":
                        # Phiên bản V2 của âm thanh có chứa "v2" trong tên của chúng
                        if "V2" in v:
                            filtered_voices.append(v)
                    else:
                        # Phiên bản V1 của âm thanh không chứa "v2" trong tên của nó
                        if "V2" not in v:
                            filtered_voices.append(v)

            def _friendly(v):
                if voice.is_no_voice(v):
                    return tr("No Voice Selected")
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v) or voice.is_kokoro_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                if voice.is_viettts_voice(v):
                    return v.split(":", 1)[1] if ":" in v else v
                if voice.is_minimax_voice(v):
                    return minimax_voice_labels.get(v, v.split(":", 1)[1])
                if voice.is_fish_audio_voice(v):
                    parts = v.split(":", 2)
                    display_name = parts[2] if len(parts) >= 3 else v
                    return (
                        display_name.replace("Female", tr("Female"))
                        .replace("Male", tr("Male"))
                    )
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )

            friendly_names = {v: _friendly(v) for v in filtered_voices}

            # Các danh mục cũ của Song Tử đặt giới tính giả định vào giá trị (ví dụ: Charon-Nam). Theo căn bản
            # Tên giọng nói được ánh xạ tới giá trị kiểu chính thức mới và giọng nói ban đầu của người dùng sẽ được giữ lại sau khi nâng cấp.
            if (
                selected_tts_server == "gemini-tts"
                and saved_voice_name not in friendly_names
            ):
                saved_gemini_voice = voice.parse_gemini_voice_name(saved_voice_name)
                saved_voice_name = next(
                    (
                        candidate
                        for candidate in filtered_voices
                        if voice.parse_gemini_voice_name(candidate)
                        == saved_gemini_voice
                    ),
                    saved_voice_name,
                )

            saved_voice_name_index = 0

            # Kiểm tra xem âm thanh đã lưu có nằm trong danh sách âm thanh hiện được lọc hay không
            if saved_voice_name in friendly_names:
                saved_voice_name_index = list(friendly_names.keys()).index(
                    saved_voice_name
                )
            else:
                # Nếu không, hãy chọn giọng nói mặc định dựa trên ngôn ngữ giao diện người dùng hiện tại
                for i, v in enumerate(filtered_voices):
                    if v.lower().startswith(st.session_state["ui_language"].lower()):
                        saved_voice_name_index = i
                        break

            # Nếu không tìm thấy âm thanh phù hợp, âm thanh đầu tiên sẽ được sử dụng
            if saved_voice_name_index >= len(friendly_names) and friendly_names:
                saved_voice_name_index = 0

            # Đảm bảo có tùy chọn âm thanh
            if tts_mode_enabled and friendly_names:
                voice_name = stable_selectbox(
                    tr("Voiceover Voice"),
                    options=list(friendly_names.keys()),
                    default_value=list(friendly_names.keys())[saved_voice_name_index],
                    key=f"speech_synthesis_select_{selected_tts_server}",
                    format_func=lambda value: friendly_names.get(
                        value,
                        str(value).removeprefix("minimax:"),
                    ),
                    # MiniMax hỗ trợ người dùng nhập trực tiếp các bản sao ngoài danh sách hoặc tạo ID âm thanh; người khác
                    # Nhà cung cấp duy trì hành vi của bộ chọn ban đầu và không mở rộng phạm vi ảnh hưởng của sửa đổi này.
                    accept_new_options=selected_tts_server == "minimax-tts",
                )

                if selected_tts_server == "minimax-tts":
                    custom_voice_id = str(voice_name or "").strip()
                    if custom_voice_id and not voice.is_minimax_voice(custom_voice_id):
                        voice_name = f"minimax:{custom_voice_id}"
                    if voice.is_minimax_voice(voice_name):
                        _set_runtime_config(
                            "minimax_tts",
                            "voice_id",
                            voice_name.split(":", 1)[1],
                        )

                params.voice_name = voice_name
                if not voice.is_no_voice(voice_name):
                    # Trọng điểm giữ chỗ chỉ được sử dụng cho màn hình bị tắt ở chế độ không tự động và không ghi đè lên thông tin trước đó của người dùng
                    # Âm đã chọn thực tế có thể được khôi phục về cài đặt ban đầu sau khi chuyển về chế độ lồng tiếng tự động.
                    _set_runtime_config("ui", "voice_name", voice_name)
            elif tts_mode_enabled:
                # Nếu không có âm thanh, một thông báo nhắc nhở sẽ được hiển thị.
                st.warning(
                    tr(
                        "No voices available for the selected TTS server. Please select another server."
                    )
                )
                voice_name = ""
                params.voice_name = ""
                _set_runtime_config("ui", "voice_name", "")
            else:
                # Chế độ lồng tiếng không tự động không hiển thị các điều khiển âm sắc và chỉ sử dụng lại các giá trị đã lưu để duy trì cấu trúc tham số ổn định.
                voice_name = saved_voice_name or voice.NO_VOICE_NAME
                params.voice_name = voice_name

            # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
            if tts_mode_enabled and (
                selected_tts_server == "azure-tts-v2"
                or (voice_name and voice.is_azure_v2_voice(voice_name))
            ):
                saved_azure_speech_region = config.azure.get("speech_region", "")
                saved_azure_speech_key = config.azure.get("speech_key", "")
                azure_speech_region = st.text_input(
                    tr("Speech Region"),
                    value=saved_azure_speech_region,
                    key="azure_speech_region_input",
                )
                azure_speech_key = st.text_input(
                    tr("Speech Key"),
                    value=saved_azure_speech_key,
                    type="password",
                    key="azure_speech_key_input",
                )
                _set_runtime_config("azure", "speech_region", azure_speech_region)
                _set_runtime_config("azure", "speech_key", azure_speech_key)

            if tts_mode_enabled and selected_tts_server == "gemini-tts":
                # Gemini TTS và Gemini LLM chia sẻ cùng một khóa; cung cấp quyền truy cập trực tiếp vào bảng điều khiển âm thanh,
                # Người dùng không cần phải chuyển đổi Nhà cung cấp LLM trước để hoàn tất cấu hình giọng nói.
                gemini_tts_api_key = st.text_input(
                    tr("Gemini API Key"),
                    value=config.app.get("gemini_api_key", ""),
                    type="password",
                    key="gemini_tts_api_key_input",
                )
                _set_runtime_config("app", "gemini_api_key", gemini_tts_api_key)

            # 当选择硅基流动时，显示API key输入框和说明信息
            if tts_mode_enabled and (
                selected_tts_server == "siliconflow"
                or (voice_name and voice.is_siliconflow_voice(voice_name))
            ):
                saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

                siliconflow_api_key = st.text_input(
                    tr("SiliconFlow API Key"),
                    value=saved_siliconflow_api_key,
                    type="password",
                    key="siliconflow_api_key_input",
                )

                _set_runtime_config("siliconflow", "api_key", siliconflow_api_key)

            # Khi chọn Xiaomi MiMo TTS, Khóa API của nhà cung cấp MiMo LLM sẽ được sử dụng lại.
            # Bằng cách này, nếu người dùng sử dụng MiMo để tạo copywriting và lời nói cùng lúc, họ chỉ cần duy trì một khóa.
            if tts_mode_enabled and (
                selected_tts_server == "mimo-tts"
                or (voice_name and voice.is_mimo_voice(voice_name))
            ):
                saved_mimo_api_key = config.app.get("mimo_api_key", "")

                mimo_api_key = st.text_input(
                    tr("MiMo API Key"),
                    value=saved_mimo_api_key,
                    type="password",
                    key="mimo_tts_api_key_input",
                )

                _set_runtime_config("app", "mimo_api_key", mimo_api_key)

            # ElevenLabs API key section
            if tts_mode_enabled and (
                selected_tts_server == "elevenlabs"
                or (voice_name and voice.is_elevenlabs_voice(voice_name))
            ):
                _render_elevenlabs_api_key_input(
                    "ElevenLabs API Key",
                )
                elevenlabs_api_key_rendered = True

                _elevenlabs_models = [
                    "eleven_multilingual_v2",
                    "eleven_flash_v2_5",
                    "eleven_v3",
                ]
                saved_elevenlabs_model = config.elevenlabs.get(
                    "model_id", "eleven_multilingual_v2"
                )
                if saved_elevenlabs_model not in _elevenlabs_models:
                    saved_elevenlabs_model = "eleven_multilingual_v2"
                elevenlabs_model = stable_selectbox(
                    tr("ElevenLabs Model"),
                    options=_elevenlabs_models,
                    default_value=saved_elevenlabs_model,
                    key="elevenlabs_model_select",
                )
                _set_runtime_config("elevenlabs", "model_id", elevenlabs_model)

            # Fish Audio API settings section
            if tts_mode_enabled and (
                selected_tts_server == "fish_audio"
                or (voice_name and voice.is_fish_audio_voice(voice_name))
            ):
                saved_fish_api_key = (
                    config.fish_audio.get("api_key", "")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else ""
                )
                fish_audio_api_key = st.text_input(
                    tr("Fish Audio API Key"),
                    value=saved_fish_api_key,
                    type="password",
                    key="fish_audio_api_key_input",
                )
                _set_runtime_config("fish_audio", "api_key", fish_audio_api_key)

                _fish_audio_models = [
                    "s2.1-pro-free",
                    "s2.1-pro",
                    "s2-pro",
                ]
                saved_fish_model = (
                    config.fish_audio.get("model", "s2.1-pro-free")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else "s2.1-pro-free"
                )
                if saved_fish_model not in _fish_audio_models:
                    saved_fish_model = "s2.1-pro-free"
                fish_model = stable_selectbox(
                    tr("Fish Audio Model"),
                    options=_fish_audio_models,
                    default_value=saved_fish_model,
                    key="fish_audio_model_select",
                )
                _set_runtime_config("fish_audio", "model", fish_model)

            # Chatterbox API settings section (self-hosted, OpenAI-compatible)
            if tts_mode_enabled and (
                selected_tts_server == "chatterbox"
                or (voice_name and voice.is_chatterbox_voice(voice_name))
            ):
                chatterbox_base_url = st.text_input(
                    tr("Chatterbox Base URL"),
                    value=config.chatterbox.get("base_url")
                    or DEFAULT_CHATTERBOX_BASE_URL,
                    key="chatterbox_base_url_input",
                    placeholder=tr("Chatterbox Base URL Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox", "base_url", (chatterbox_base_url or "").strip()
                )

                chatterbox_api_key = st.text_input(
                    tr("Chatterbox API Key"),
                    value=config.chatterbox.get("api_key", ""),
                    type="password",
                    key="chatterbox_api_key_input",
                )
                _set_runtime_config("chatterbox", "api_key", chatterbox_api_key)

                chatterbox_model = st.text_input(
                    tr("Chatterbox Model"),
                    value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
                    key="chatterbox_model_input",
                )
                _set_runtime_config(
                    "chatterbox",
                    "model_id",
                    (chatterbox_model or DEFAULT_CHATTERBOX_MODEL).strip(),
                )

                _saved_chatterbox_voices = (
                    _parse_chatterbox_voices(config.chatterbox.get("voices"))
                    or DEFAULT_CHATTERBOX_VOICES
                )
                if isinstance(_saved_chatterbox_voices, list):
                    _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
                chatterbox_voices = st.text_input(
                    tr("Chatterbox Voices"),
                    value=str(_saved_chatterbox_voices or ""),
                    key="chatterbox_voices_input",
                    placeholder=tr("Chatterbox Voices Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox",
                    "voices",
                    _parse_chatterbox_voices(chatterbox_voices),
                )

            # Kokoro API settings section (self-hosted, OpenAI-compatible; voices listed from the server when left empty)
            if tts_mode_enabled and (
                selected_tts_server == "kokoro"
                or (voice_name and voice.is_kokoro_voice(voice_name))
            ):
                kokoro_base_url = st.text_input(
                    tr("Kokoro Base URL"),
                    value=config.kokoro.get("base_url")
                    or DEFAULT_KOKORO_BASE_URL,
                    key="kokoro_base_url_input",
                    placeholder=tr("Kokoro Base URL Placeholder"),
                )
                _set_runtime_config(
                    "kokoro", "base_url", (kokoro_base_url or "").strip()
                )

                kokoro_api_key = st.text_input(
                    tr("Kokoro API Key"),
                    value=config.kokoro.get("api_key", ""),
                    type="password",
                    key="kokoro_api_key_input",
                )
                _set_runtime_config("kokoro", "api_key", kokoro_api_key)

                kokoro_model = st.text_input(
                    tr("Kokoro Model"),
                    value=config.kokoro.get("model_id") or DEFAULT_KOKORO_MODEL,
                    key="kokoro_model_input",
                )
                _set_runtime_config(
                    "kokoro",
                    "model_id",
                    (kokoro_model or DEFAULT_KOKORO_MODEL).strip(),
                )

                _saved_kokoro_voices = (
                    _parse_chatterbox_voices(config.kokoro.get("voices"))
                    or DEFAULT_KOKORO_VOICES
                )
                if isinstance(_saved_kokoro_voices, list):
                    _saved_kokoro_voices = ", ".join(_saved_kokoro_voices)
                kokoro_voices = st.text_input(
                    tr("Kokoro Voices"),
                    value=str(_saved_kokoro_voices or ""),
                    key="kokoro_voices_input",
                    placeholder=tr("Kokoro Voices Placeholder"),
                )
                _set_runtime_config(
                    "kokoro",
                    "voices",
                    _parse_chatterbox_voices(kokoro_voices),
                )

            # Ba chế độ chỉ hiển thị các điều khiển thực sự cần thiết cho tác vụ hiện tại. Tự động lồng tiếng với âm lượng và tốc độ nói có thể điều chỉnh;
            # Tải lên âm thanh chỉ yêu cầu tệp và âm lượng; không lồng tiếng sẽ không còn hiển thị cài đặt không hợp lệ.
            params.voice_name = (
                voice.NO_VOICE_NAME if voice_mode == VOICE_MODE_NONE else voice_name
            )
            params.voice_volume = 1.0
            params.voice_rate = 1.0
            uploaded_audio_file = None
            voice_volume_options = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
            voice_rate_options = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]

            if tts_mode_enabled:
                voice_control_cols = st.columns(2)
                with voice_control_cols[0]:
                    params.voice_volume = stable_selectbox(
                        tr("Voiceover Volume"),
                        options=voice_volume_options,
                        default_value=_saved_ui_choice(
                            "voice_volume", voice_volume_options, 1.0
                        ),
                        key="voice_volume_select",
                        format_func=lambda value: f"{int(value * 100)}%",
                        help=tr("Voiceover Volume Help"),
                    )

                with voice_control_cols[1]:
                    params.voice_rate = stable_selectbox(
                        tr("Voiceover Speed"),
                        options=voice_rate_options,
                        default_value=_saved_ui_choice(
                            "voice_rate", voice_rate_options, 1.0
                        ),
                        key="voice_rate_select",
                        format_func=lambda value: f"{value:.1f}×",
                        help=tr("Voiceover Speed Help"),
                    )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                _set_runtime_config("ui", "voice_rate", params.voice_rate)

                # Việc thử giọng phải được đặt sau bộ điều khiển âm lượng và tốc độ giọng nói, đảm bảo rằng cuộc gọi sử dụng các giá trị điều khiển hiện tại.
                _render_voice_preview(
                    params,
                    friendly_names,
                    selected_tts_server,
                    voice_name,
                )
            elif voice_mode == VOICE_MODE_UPLOAD:
                custom_audio_file_types = sorted(
                    extension.removeprefix(".") for extension in CUSTOM_AUDIO_EXTENSIONS
                )
                uploaded_audio_file = st.file_uploader(
                    tr("Upload Voiceover File"),
                    type=custom_audio_file_types
                    + [file_type.upper() for file_type in custom_audio_file_types],
                    accept_multiple_files=False,
                    key="custom_audio_file_uploader",
                    help=tr("Upload Voiceover File Help"),
                )
                params.voice_volume = stable_selectbox(
                    tr("Voiceover Volume"),
                    options=voice_volume_options,
                    default_value=_saved_ui_choice(
                        "voice_volume", voice_volume_options, 1.0
                    ),
                    key="voice_volume_select",
                    format_func=lambda value: f"{int(value * 100)}%",
                    help=tr("Voiceover Volume Help"),
                )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                if uploaded_audio_file:
                    st.audio(uploaded_audio_file, format="audio/mp3")
                    st.info(
                        tr(
                            "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                        )
                    )
            uploaded_bgm_file = _render_background_music_settings(
                params,
                elevenlabs_api_key_rendered=elevenlabs_api_key_rendered,
            )
    return uploaded_audio_file, uploaded_bgm_file, voice_mode


def _render_subtitle_settings(panel, params):
    """Kết xuất cài đặt phụ đề và cập nhật các thông số tạo."""
    with panel:
        with st.container(border=True):
            st.write(tr("Subtitle Settings"))
            st.session_state.setdefault(
                "subtitle_enabled_checkbox",
                _saved_ui_bool(
                    "subtitle_enabled",
                    DEFAULT_SUBTITLE_SETTINGS["subtitle_enabled"],
                ),
            )
            params.subtitle_enabled = st.checkbox(
                tr("Enable Subtitles"),
                key="subtitle_enabled_checkbox",
            )
            _set_runtime_config("ui", "subtitle_enabled", params.subtitle_enabled)
            subtitle_settings_disabled = not params.subtitle_enabled
            font_names = get_all_fonts()
            saved_font_name = config.ui.get(
                "font_name", DEFAULT_SUBTITLE_SETTINGS["font_name"]
            )
            saved_font_name_index = 0
            if saved_font_name in font_names:
                saved_font_name_index = font_names.index(saved_font_name)
            params.font_name = stable_selectbox(
                tr("Font"),
                options=font_names,
                default_value=font_names[saved_font_name_index] if font_names else "",
                key="font_name_select",
                disabled=subtitle_settings_disabled,
            )
            _set_runtime_config("ui", "font_name", params.font_name)

            subtitle_positions = [
                (tr("Top"), "top"),
                (tr("Center"), "center"),
                (tr("Bottom"), "bottom"),
                (tr("2/3 from Bottom"), "two_thirds_bottom"),
                (tr("Custom"), "custom"),
            ]
            saved_subtitle_position = config.ui.get(
                "subtitle_position", DEFAULT_SUBTITLE_SETTINGS["subtitle_position"]
            )
            saved_position_index = 2
            for i, (_, pos_value) in enumerate(subtitle_positions):
                if pos_value == saved_subtitle_position:
                    saved_position_index = i
                    break
            selected_subtitle_position = stable_selectbox(
                tr("Position"),
                options=[value for _, value in subtitle_positions],
                default_value=subtitle_positions[saved_position_index][1],
                key="subtitle_position_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_positions
                ).get(value, value),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_position = selected_subtitle_position
            _set_runtime_config("ui", "subtitle_position", params.subtitle_position)

            # Subtitle Display Mode (Sentence vs Single Word)
            subtitle_display_modes = [
                (tr("Sentence by Sentence"), "sentence"),
                (tr("Single Word (Word by Word)"), "word_by_word"),
            ]
            saved_display_mode = config.ui.get(
                "subtitle_display_mode",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_display_mode"],
            )
            saved_mode_idx = 0
            for i, (_, mode_val) in enumerate(subtitle_display_modes):
                if mode_val == saved_display_mode:
                    saved_mode_idx = i
                    break
            selected_display_mode = stable_selectbox(
                tr("Display Mode"),
                options=[val for _, val in subtitle_display_modes],
                default_value=subtitle_display_modes[saved_mode_idx][1],
                key="subtitle_display_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_display_modes
                ).get(value, value),
                help=tr("Word-by-word Timing Help"),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_display_mode = selected_display_mode
            _set_runtime_config(
                "ui", "subtitle_display_mode", params.subtitle_display_mode
            )

            # Subtitle Animation (None vs Pop Spring)
            subtitle_animations = [
                (tr("None"), "none"),
                (tr("Pop Up (Spring)"), "pop_spring"),
            ]
            saved_anim = config.ui.get(
                "subtitle_animation",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_animation"],
            )
            saved_anim_idx = 0
            for i, (_, anim_val) in enumerate(subtitle_animations):
                if anim_val == saved_anim:
                    saved_anim_idx = i
                    break
            selected_anim = stable_selectbox(
                tr("Subtitle Animation"),
                options=[val for _, val in subtitle_animations],
                default_value=subtitle_animations[saved_anim_idx][1],
                key="subtitle_animation_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_animations
                ).get(value, value),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_animation = selected_anim
            _set_runtime_config(
                "ui", "subtitle_animation", params.subtitle_animation
            )

            if params.subtitle_position == "custom":
                saved_custom_position = config.ui.get(
                    "custom_position", DEFAULT_SUBTITLE_SETTINGS["custom_position"]
                )
                st.session_state.setdefault(
                    "custom_position_input", str(saved_custom_position)
                )
                custom_position = st.text_input(
                    tr("Custom Position (% from top)"),
                    key="custom_position_input",
                    disabled=subtitle_settings_disabled,
                )
                try:
                    params.custom_position = float(custom_position)
                    if params.custom_position < 0 or params.custom_position > 100:
                        st.error(tr("Please enter a value between 0 and 100"))
                    else:
                        _set_runtime_config(
                            "ui", "custom_position", params.custom_position
                        )
                except ValueError:
                    st.error(tr("Please enter a valid number"))

            # Nhãn màu cho các ngôn ngữ không phải tiếng Trung thường dài hơn tiếng Trung. Để lại chiều rộng thích hợp cho bộ chọn màu,
            # Tránh gói nhãn trong khi vẫn chừa đủ chỗ cho thanh trượt kích thước phông chữ điều khiển.
            font_cols = st.columns([0.42, 0.58])
            with font_cols[0]:
                saved_text_fore_color = config.ui.get(
                    "text_fore_color", DEFAULT_SUBTITLE_SETTINGS["text_fore_color"]
                )
                st.session_state.setdefault("font_color_picker", saved_text_fore_color)
                params.text_fore_color = st.color_picker(
                    tr("Font Color"),
                    key="font_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "text_fore_color", params.text_fore_color)

            with font_cols[1]:
                saved_font_size = config.ui.get(
                    "font_size", DEFAULT_SUBTITLE_SETTINGS["font_size"]
                )
                st.session_state.setdefault("font_size_slider", saved_font_size)
                params.font_size = st.slider(
                    tr("Font Size"),
                    30,
                    100,
                    key="font_size_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "font_size", params.font_size)

            stroke_cols = st.columns([0.42, 0.58])
            with stroke_cols[0]:
                st.session_state.setdefault(
                    "stroke_color_picker",
                    _saved_ui_color(
                        "stroke_color", DEFAULT_SUBTITLE_SETTINGS["stroke_color"]
                    ),
                )
                params.stroke_color = st.color_picker(
                    tr("Stroke Color"),
                    key="stroke_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_color", params.stroke_color)
            with stroke_cols[1]:
                st.session_state.setdefault(
                    "stroke_width_slider",
                    _saved_ui_number(
                        "stroke_width",
                        DEFAULT_SUBTITLE_SETTINGS["stroke_width"],
                        0.0,
                        10.0,
                    ),
                )
                params.stroke_width = st.slider(
                    tr("Stroke Width"),
                    0.0,
                    10.0,
                    key="stroke_width_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_width", params.stroke_width)

            # Tên bản địa hóa của công tắc nền thường dài hơn nhãn màu, do đó cho phép công tắc chiếm nhiều không gian hơn một chút.
            subtitle_bg_cols = st.columns([0.55, 0.45])
            saved_subtitle_background_enabled = config.ui.get(
                "subtitle_background_enabled",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_enabled"],
            )
            st.session_state.setdefault(
                "subtitle_background_enabled_checkbox",
                saved_subtitle_background_enabled,
            )
            with subtitle_bg_cols[0]:
                subtitle_background_enabled = st.checkbox(
                    tr("Enable Subtitle Background"),
                    key="subtitle_background_enabled_checkbox",
                    disabled=subtitle_settings_disabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_enabled",
                subtitle_background_enabled,
            )

            # 背景颜色和圆角样式都从属于字幕背景开关。子控件始终保留在页面中，
            # Khi tắt công tắc chính, công tắc này sẽ bị tắt đồng bộ để tránh hiện tượng nhảy bố cục do một điều khiển biến mất trong khi điều khiển khác bị tắt.
            # Các giá trị màu vẫn được lưu trong cấu hình giao diện người dùng và lựa chọn trước đó của người dùng có thể được khôi phục sau khi bật lại nền;
            # Tham số được chuyển tới dịch vụ tạo được đặt thành Sai để đảm bảo rằng trạng thái tắt không thực sự hiển thị nền.
            saved_subtitle_background_color = config.ui.get(
                "subtitle_background_color",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_color"],
            )
            st.session_state.setdefault(
                "subtitle_background_color_picker",
                saved_subtitle_background_color,
            )
            with subtitle_bg_cols[1]:
                selected_subtitle_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    key="subtitle_background_color_picker",
                    disabled=subtitle_settings_disabled
                    or not subtitle_background_enabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_color",
                selected_subtitle_background_color,
            )
            params.text_background_color = (
                selected_subtitle_background_color
                if subtitle_background_enabled
                else False
            )

            saved_rounded_subtitle_background = config.ui.get(
                "rounded_subtitle_background",
                DEFAULT_SUBTITLE_SETTINGS["rounded_subtitle_background"],
            )
            # Khi tắt nền, nền tròn sẽ không có nền có thể hiển thị. Vô hiệu hóa điều khiển ở đây nhưng vẫn giữ nguyên cấu hình ban đầu.
            # Lần tiếp theo người dùng bật lại nền phụ đề có thể tiếp tục sử dụng tùy chọn góc bo tròn đã lưu trước đó.
            rounded_background_disabled = (
                subtitle_settings_disabled or not subtitle_background_enabled
            )
            st.session_state.setdefault(
                "rounded_subtitle_background_checkbox",
                saved_rounded_subtitle_background,
            )
            selected_rounded_subtitle_background = st.checkbox(
                tr("Rounded Subtitle Background"),
                help=tr("Rounded Subtitle Background Help"),
                disabled=rounded_background_disabled,
                key="rounded_subtitle_background_checkbox",
            )
            params.rounded_subtitle_background = (
                selected_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            )
            if not subtitle_settings_disabled and subtitle_background_enabled:
                _set_runtime_config(
                    "ui",
                    "rounded_subtitle_background",
                    selected_rounded_subtitle_background,
                )

            if video.subtitle_colors_are_indistinguishable(params):
                # Cấu hình màu tương tự vẫn là lựa chọn hợp pháp của người dùng nên chỉ được nhắc trong vùng cài đặt phụ đề.
                # Does not prevent generation. Người dùng có thể quyết định có tiếp tục hay không dựa trên nhu cầu hình ảnh thực tế.
                st.warning(tr("Subtitle Colors Are Indistinguishable"))

            subtitle_preview_text = params.video_script or params.video_subject
            selected_font_path = os.path.join(font_dir, params.font_name)
            if (
                params.subtitle_enabled
                and subtitle_preview_text
                and not video.subtitle_font_supports_text(
                    selected_font_path, subtitle_preview_text
                )
            ):
                st.warning(tr("Subtitle Font Does Not Support Text"))

            if st.button(
                tr("Restore Default Subtitle Settings"),
                key="restore_default_subtitle_settings",
                icon=":material/restart_alt:",
                on_click=reset_subtitle_settings,
                use_container_width=True,
            ):
                st.toast(tr("Default Subtitle Settings Restored"))


def _render_generation_controls(
    params, uploaded_files, uploaded_audio_file, uploaded_bgm_file, voice_mode
):
    """
    Xác minh các phần phụ thuộc đã tạo, gửi tác vụ cũng như hiển thị nhật ký và kết quả phân đoạn.

    Quay lại trang này để xem tác vụ mới đã được gửi thành công hay chưa. Lưu không chặn đã được yêu cầu trước khi gửi, người gọi
    据此跳过页面末尾的重复请求。主脚本必须及时结束，定时 Fragment 才能持续
    刷新进度和任务日志。
    """
    restore_upload_requirements = st.session_state.get(
        "task_restore_upload_requirements", {}
    )
    local_dir_has_files = False
    if params.video_source == "local":
        local_dir = utils.storage_dir("local_videos", create=True)
        local_dir_has_files = bool(_get_existing_local_media_files(local_dir)) and st.session_state.get(
            "use_existing_local_folder", True
        )

    has_local_materials = bool(
        uploaded_files
        or local_dir_has_files
        or st.session_state.get("local_video_materials", [])
    )
    has_custom_audio = bool(uploaded_audio_file)
    unmet_restore_requirements = _get_unmet_restore_upload_requirements(
        restore_upload_requirements,
        video_source=params.video_source,
        voice_name=params.voice_name or "",
        has_local_materials=has_local_materials,
        has_custom_audio=has_custom_audio,
        voice_mode=voice_mode,
    )
    if "local_materials" in unmet_restore_requirements:
        st.warning(tr("Task Restore Local Materials Warning"))
    if "custom_audio" in unmet_restore_requirements:
        st.warning(tr("Task Restore Custom Audio Warning"))
    if restore_upload_requirements and not unmet_restore_requirements:
        # Người dùng đã tải lại tệp lên hoặc chủ động chuyển đổi nguồn/âm vật liệu. Tại thời điểm này, sự phụ thuộc tải lên của các nhiệm vụ lịch sử
        # Nó đã được xử lý rõ ràng và dấu đã được xóa để ngăn các bản dựng bình thường tiếp theo tiếp tục hiển thị lời nhắc cũ.
        st.session_state.pop("task_restore_upload_requirements", None)

    _render_settings_transfer(params)

    start_button = st.button(
        tr("Generate Video"),
        use_container_width=True,
        type="primary",
        key="generate_video_button",
        on_click=_prepare_generation_task,
    )
    render_onboarding_tour()
    if start_button:
        _save_runtime_config()
        task_id = st.session_state.get("pending_generation_task_id") or str(uuid4())
        _add_active_generation_task(
            task_id,
            subject=params.video_subject or params.video_script or task_id,
        )
        if not params.video_subject and not params.video_script:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            st.stop()

        if params.video_source not in [
            "pexels",
            "pixabay",
            "coverr",
            "wavespeed",
            "volcengine_seedance",
            "ofox",
            "metaso_minimax",
            "loomloom",
            "openai_image",
            "local",
        ]:
            _remove_active_generation_task(task_id)
            st.error(tr("Please Select a Valid Video Source"))
            st.stop()

        if params.video_source == "pexels" and not config.app.get(
            "pexels_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pexels API Key"))
            st.stop()

        if params.video_source == "pixabay" and not config.app.get(
            "pixabay_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pixabay API Key"))
            st.stop()

        if params.video_source == "coverr" and not config.app.get(
            "coverr_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Coverr API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not config.app.get(
            "wavespeed_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the WaveSpeed API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not st.session_state.get(
            "wavespeed_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm WaveSpeed Charge Required"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not (
            volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Volcano Engine Ark API Key"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not st.session_state.get(
            "volcengine_seedance_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Volcano Engine Seedance Charge Required"))
            st.stop()

        if params.video_source == "ofox" and not (
            ofox.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the OFox API Key"))
            st.stop()

        if params.video_source == "ofox" and not st.session_state.get(
            "ofox_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm OFox Charge Required"))
            st.stop()

        if params.video_source == "metaso_minimax" and not (
            metaso_minimax.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Metaso MiniMax API Key"))
            st.stop()

        if params.video_source == "metaso_minimax" and not st.session_state.get(
            "metaso_minimax_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Metaso MiniMax Charge Required"))
            st.stop()

        if params.video_source == "openai_image" and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Configure the OpenAI Image Source"))
            st.stop()

        loomloom_video_request = None
        if params.video_source == "loomloom":
            current_batch, current_signature = _current_loomloom_video_quote_context(
                params
            )
            quoted_batch = st.session_state.get("loomloom_video_batch")
            quote_result = st.session_state.get("loomloom_video_quote")
            quote_is_current = bool(
                current_batch is not None
                and isinstance(quoted_batch, loomloom.LoomLoomVideoBatch)
                and quote_result is not None
                and st.session_state.get("loomloom_video_input_signature")
                == current_signature
            )
            if not quote_is_current:
                _remove_active_generation_task(task_id)
                st.error(tr("AI Video Quote Required"))
                st.stop()
            if not st.session_state.get("loomloom_video_confirm_charge", False):
                _remove_active_generation_task(task_id)
                st.error(tr("Confirm AI Video Charge Required"))
                st.stop()
            try:
                video_backend = _create_loomloom_video_backend()
                loomloom_video_request = loomloom.LoomLoomConfirmedVideoRequest(
                    settings=video_backend.settings,
                    batch=current_batch,
                    listing_version_id=quote_result.listing_version_id,
                    client_request_id=st.session_state[
                        "loomloom_video_client_request_id"
                    ],
                )
                loomloom_video_request.validate()
            except (loomloom.LoomLoomError, ValueError) as exc:
                _remove_active_generation_task(task_id)
                st.error(str(exc))
                st.stop()

        if (
            params.bgm_type == "sonilo"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not sonilo_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Sonilo API Key Required"))
            st.stop()

        if (
            params.bgm_type == "elevenlabs"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not elevenlabs_music_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("ElevenLabs API Key Required"))
            st.stop()

        if params.video_source == "local" and not has_local_materials:
            # Việc tiếp tục thực thi khi tài liệu cục bộ trống trước tiên sẽ tạo ra TTS/phụ đề và cuối cùng sẽ thất bại trong giai đoạn tiền xử lý tài liệu.
            # Việc chặn trước khi tác vụ bắt đầu có thể tránh được các lệnh gọi API vô nghĩa và các tệp trung gian.
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Local Materials First"))
            st.stop()

        if voice_mode == VOICE_MODE_UPLOAD and not uploaded_audio_file:
            # Tải âm thanh lên là phương pháp lồng tiếng được người dùng lựa chọn rõ ràng và TTS không thể được trả về âm thầm khi thiếu tệp.
            # 在任务启动前拦截，避免产生与用户选择不一致的成片。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Voiceover File First"))
            st.stop()

        if "custom_audio" in unmet_restore_requirements:
            # Âm thanh tùy chỉnh lịch sử không thể được tự động chèn lấp. Khi người dùng chưa tải lại và chưa chủ động thay đổi âm sắc,
            # Phải ngăn chặn dự phòng im lặng cho TTS, nếu không, kết quả được tạo lại sẽ không nhất quán với giọng nói tác vụ ban đầu.
            _remove_active_generation_task(task_id)
            st.error(tr("Task Restore Custom Audio Warning"))
            st.stop()

        if uploaded_bgm_file and bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        ):
            try:
                saved_bgm_name = bgm_service.save_bgm_upload(
                    uploaded_bgm_file.name, uploaded_bgm_file
                )
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI background music upload rejected: {str(exc)}")
                st.error(tr("Invalid Background Music"))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI background music upload failed: {str(exc)}")
                st.error(tr("Background Music Validation Failed"))
                st.stop()
            # Sau khi lưu thành công, chỉ có tên tệp được ghi vào tham số tác vụ. Dịch vụ video sẽ nằm trong hai danh sách trắng BGM
            # Phân tích lại trong thư mục để tránh việc tồn tại hoặc hiển thị đường dẫn tuyệt đối đến máy chủ cho người dùng.
            params.bgm_file = saved_bgm_name
        elif uploaded_bgm_file:
            # Ở mức âm lượng 0, dịch vụ video sẽ không sử dụng bất kỳ nhạc nền nào nên các file tải lên đã được xem trước sẽ không còn nữa.
            # 持久化到 storage。用户之后调高音量时可直接再次点击生成完成保存。
            params.bgm_file = ""

        if uploaded_audio_file:
            task_dir = utils.task_dir(task_id)
            try:
                custom_audio_path = _build_uploaded_file_path(
                    uploaded_audio_file,
                    task_dir,
                    CUSTOM_AUDIO_EXTENSIONS,
                    "custom-audio",
                )
            except ValueError:
                _remove_active_generation_task(task_id)
                st.error(tr("Unsupported Upload File Type"))
                st.stop()
            with open(custom_audio_path, "wb") as f:
                f.write(uploaded_audio_file.getbuffer())
            params.custom_audio_file = custom_audio_path

        if uploaded_files:
            local_videos_dir = utils.storage_dir("local_videos", create=True)
            # Mỗi lần bạn upload lại, tài liệu được chọn lần này sẽ được dùng làm tiêu chuẩn để tránh việc bổ sung lặp lại tài liệu cũ.
            params.video_materials = []
            persisted_local_materials = []
            for file in uploaded_files:
                try:
                    file_path = _build_uploaded_file_path(
                        file,
                        local_videos_dir,
                        LOCAL_MATERIAL_EXTENSIONS,
                        "material",
                    )
                except ValueError:
                    _remove_active_generation_task(task_id)
                    st.error(tr("Unsupported Upload File Type"))
                    st.stop()
                with open(file_path, "wb") as f:
                    f.write(file.getbuffer())
                    m = MaterialInfo()
                    m.provider = "local"
                    m.url = file_path
                    params.video_materials.append(m)
                    persisted_local_materials.append(
                        {
                            "provider": m.provider,
                            "url": m.url,
                            "duration": m.duration,
                        }
                    )
            # Viết tài liệu video đã được tải lên và lưu cục bộ vào phiên để sử dụng lại trực tiếp khi chỉ bản sao được sửa đổi sau này.
            st.session_state["local_video_materials"] = persisted_local_materials
        elif (
            params.video_source == "local"
            and st.session_state.get("use_existing_local_folder", True)
            and _get_existing_local_media_files(utils.storage_dir("local_videos", create=True))
        ):
            # Tự động nạp toàn bộ ảnh/video do Antigravity hoặc người dùng đặt trong storage/local_videos
            local_videos_dir = utils.storage_dir("local_videos", create=True)
            existing_files = _get_existing_local_media_files(local_videos_dir)
            params.video_materials = []
            for fpath in existing_files:
                m = MaterialInfo()
                m.provider = "local"
                m.url = fpath
                params.video_materials.append(m)
        elif (
            params.video_source == "local" and st.session_state.get("local_video_materials")
        ):
            # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
            params.video_materials = []
            for material_entry in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material_entry.get("provider", "local")
                m.url = material_entry.get("url", "")
                m.duration = material_entry.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        reusable_voice_preview = _get_reusable_full_voice_preview(
            params,
            voice_mode,
        )
        if reusable_voice_preview:
            # Bộ đệm thử giọng chỉ tồn tại cho phiên Streamlit hiện tại. Viết âm thanh vào thư mục tác vụ đích trước khi gửi.
            # Luồng nền sau đó chỉ đọc các tệp riêng của tác vụ; ngay cả khi trang chạy lại, trình duyệt đã bị đóng hoặc
            # Khi người dùng thử các âm sắc khác, điều này sẽ không ảnh hưởng đến các tác vụ tạo đã được xếp hàng đợi.
            preview_audio_file = os.path.join(
                utils.task_dir(task_id),
                "audio.mp3",
            )
            with open(preview_audio_file, "wb") as file:
                file.write(reusable_voice_preview.pop("audio_bytes"))
            reusable_voice_preview["audio_file"] = preview_audio_file
            logger.info(
                f"reuse full voice preview for task: "
                f"task_id={task_id}, duration={reusable_voice_preview['duration']:.2f}s"
            )

        try:
            st.toast(tr("Generating Video"))
            logger.info(tr("Start Generating Video"))
            logger.info(utils.to_json(params))
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=reusable_voice_preview,
                loomloom_video_request=loomloom_video_request,
            )
            if loomloom_video_request is not None:
                # 一个报价只允许提交一次。后台请求自带稳定幂等 ID；提交成功后
                # Xóa báo giá trang và bạn phải yêu cầu lại và xác nhận vào lần tiếp theo nó được tạo.
                st.session_state["loomloom_video_batch"] = None
                st.session_state["loomloom_video_quote"] = None
                st.session_state["loomloom_video_input_signature"] = ""
                st.session_state["loomloom_video_client_request_id"] = ""
        except Exception:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Generation Failed"))
            st.stop()

        st.session_state["current_generation_task_id"] = task_id
        logger.info(f"WebUI generation task submitted: task_id={task_id}")

    _render_current_generation_task()
    return start_button


def _render_application():
    """Hiển thị thanh trên cùng, cửa sổ bật lên, biểu mẫu được tạo và kết quả tác vụ theo một thứ tự cố định."""
    _render_top_bar()

    if st.session_state.get("settings_dialog_open", False):
        _render_settings_dialog()

    if _apply_pending_settings_preset():
        st.success(tr("Settings Preset Imported"))

    restore_applied = _apply_pending_task_restore()
    restore_candidate_id = st.session_state.get("task_restore_candidate_id")
    if restore_candidate_id:
        _render_task_restore_dialog(restore_candidate_id)
    restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
    if restore_applied or restore_succeeded:
        st.success(tr("Task Configuration Loaded"))

def _render_tryon_studio():
    st.markdown("### Thử Đồ Ảo Cho Người Mẫu (Shopee & TikTok Affiliate)")
    st.caption("Cho người mẫu AI của kênh mặc chuẩn xác bộ trang phục thật từ ảnh sản phẩm Shopee hoặc TikTok Shop.")

    col1, col2, col3 = st.columns([1, 1, 1.2])

    person_file_path = None
    garment_file_path = None

    with col1:
        st.subheader("1. Ảnh Người Mẫu")
        existing_models = []
        models_dir = Path("storage/models")
        if models_dir.is_dir():
            for f in sorted(models_dir.glob("*.png"), key=os.path.getmtime, reverse=True):
                existing_models.append(str(f))
            for f in sorted(models_dir.glob("*.jpg"), key=os.path.getmtime, reverse=True):
                existing_models.append(str(f))

        model_source = st.radio(
            "Nguồn ảnh người mẫu:",
            ["Tải lên từ máy tính", "Chọn từ thư viện mẫu / Extension"],
            horizontal=True,
            key="tryon_model_source_radio",
        )

        if model_source == "Tải lên từ máy tính":
            uploaded_person = st.file_uploader(
                "Tải ảnh người mẫu (Toàn thân hoặc nửa người):",
                type=["png", "jpg", "jpeg", "webp"],
                key="tryon_person_uploader",
            )
            if uploaded_person:
                temp_dir = Path("storage/temp")
                temp_dir.mkdir(parents=True, exist_ok=True)
                p_path = temp_dir / f"person_{uploaded_person.name}"
                with open(p_path, "wb") as f:
                    f.write(uploaded_person.getbuffer())
                person_file_path = str(p_path)
                st.image(str(p_path), caption="Người mẫu được chọn", use_container_width=True)
        else:
            if existing_models:
                selected_model_path = st.selectbox(
                    "Chọn ảnh từ thư viện:",
                    options=existing_models,
                    format_func=lambda x: Path(x).name,
                    key="tryon_existing_model_select",
                )
                person_file_path = selected_model_path
                st.image(selected_model_path, caption="Người mẫu được chọn", use_container_width=True)
            else:
                st.info("Chưa có ảnh trong thư viện. Hãy tải ảnh lên từ máy tính hoặc dùng Extension tải về.")

    with col2:
        st.subheader("2. Ảnh Trang Phục")
        uploaded_garment = st.file_uploader(
            "Tải ảnh trang phục (váy, áo, đầm Shopee/TikTok):",
            type=["png", "jpg", "jpeg", "webp"],
            key="tryon_garment_uploader",
        )
        if uploaded_garment:
            temp_dir = Path("storage/temp")
            temp_dir.mkdir(parents=True, exist_ok=True)
            g_path = temp_dir / f"garment_{uploaded_garment.name}"
            with open(g_path, "wb") as f:
                f.write(uploaded_garment.getbuffer())
            garment_file_path = str(g_path)
            st.image(str(g_path), caption="Trang phục mẫu", use_container_width=True)

        garment_desc = st.text_input(
            "Loại trang phục (ngắn gọn):",
            value="váy thời trang nữ tính",
            help="Ví dụ: đầm xòe, váy body, áo sơ mi, áo croptop...",
            key="tryon_garment_desc_input",
        )

    with col3:
        st.subheader("3. Tùy Chọn & Kết Quả")
        tryon_cfg = config.app.get("tryon", {})
        engine_mode = st.radio(
            "Chế độ xử lý:",
            ["Miễn Phí 100% (Hugging Face IDM-VTON)", "Trả Phí Tốc Độ Cao (Replicate API)"],
            index=0 if tryon_cfg.get("provider", "free_hf") == "free_hf" else 1,
            key="tryon_engine_mode_select",
        )

        provider = "free_hf" if "Miễn Phí" in engine_mode else "replicate"
        api_token = None
        if provider == "replicate":
            api_token = st.text_input(
                "Replicate API Token:",
                value=tryon_cfg.get("replicate_api_token", ""),
                type="password",
                help="Lấy tại replicate.com/account/api-tokens (chỉ dùng khi muốn xử lý nhanh tức thì dưới 5s)",
                key="tryon_replicate_token_input",
            )

        start_tryon = st.button(
            "Ghép Đồ Cho Người Mẫu",
            icon=":material/auto_awesome:",
            type="primary",
            use_container_width=True,
            disabled=not (person_file_path and garment_file_path),
            key="start_tryon_button",
        )

        if start_tryon:
            from app.services import tryon as tryon_service
            with st.spinner("Đang kết nối AI và ghép trang phục lên người mẫu... Quá trình có thể mất từ 30s đến 60s..."):
                success, result_path, msg = tryon_service.run_tryon(
                    person_image_path=person_file_path,
                    garment_image_path=garment_file_path,
                    garment_description=garment_desc,
                    provider=provider,
                    api_token=api_token,
                )
                if success:
                    st.success("Ghép đồ thành công!")
                    st.session_state["latest_tryon_result"] = result_path
                else:
                    st.error(f"Thất bại: {msg}")

        latest_result = st.session_state.get("latest_tryon_result")
        if latest_result and os.path.isfile(latest_result):
            st.image(latest_result, caption="Kết quả mẫu mặc đồ thật", use_container_width=True)
            with open(latest_result, "rb") as file:
                st.download_button(
                    label="Tải ảnh kết quả về máy",
                    icon=":material/download:",
                    data=file,
                    file_name=Path(latest_result).name,
                    mime="image/png",
                    use_container_width=True,
                    key="download_tryon_result_button",
                )


def _render_dance_studio():
    """Giao diện tạo video người mẫu AI nhảy theo video mẫu TikTok/Douyin, chuẩn hóa 60 FPS bằng GPU NVIDIA RTX."""
    st.header("Video Nhảy AI (Motion Transfer & 60 FPS RTX)")
    st.caption(
        "Tạo video người mẫu AI nhảy theo video mẫu TikTok/Douyin, chuẩn hóa chuyển động & thời lượng khớp 100% "
        "để chọn âm thanh xu hướng trực tiếp trên TikTok, nâng cấp 60 FPS siêu mượt bằng GPU NVIDIA RTX."
    )

    col1, col2, col3 = st.columns([1, 1, 1])

    # --- CỘT 1: VIDEO NHẢY MẪU ---
    with col1:
        st.subheader("1. Video Nhảy Mẫu")
        video_source_mode = st.radio(
            "Nguồn video nhảy:",
            ["Dán Link TikTok / Douyin / Shorts", "Tải video từ máy (.mp4)"],
            horizontal=True,
            key="dance_source_radio",
        )

        from app.services import dance as dance_service

        # Dọn dẹp nếu trước đó từng lưu file test
        if "sample_dance_7s.mp4" in str(st.session_state.get("dance_motion_video_path", "")):
            st.session_state.pop("dance_motion_video_path", None)
            st.session_state.pop("dance_ref_info", None)

        if video_source_mode == "Dán Link TikTok / Douyin / Shorts":
            tiktok_url = st.text_input(
                "Dán link video TikTok/Douyin nhảy mẫu:",
                placeholder="https://vt.tiktok.com/... hoặc https://www.tiktok.com/@.../video/...",
                key="dance_tiktok_url_input",
            )
            btn_download = st.button(
                "Tải Video Sạch",
                icon=":material/download:",
                type="primary",
                use_container_width=True,
                disabled=not tiktok_url,
                key="dance_btn_download_tiktok",
            )
            if btn_download:
                with st.spinner("Đang tải video TikTok chất lượng cao không watermark..."):
                    res = dance_service.download_tiktok_video(tiktok_url)
                    if res.get("status") == "success":
                        st.session_state["dance_ref_info"] = res
                        st.session_state["dance_motion_video_path"] = res["video_path"]
                        st.success("Tải video thành công!")
                    else:
                        st.error(f"Thất bại: {res.get('message')}")
        else:
            uploaded_vid = st.file_uploader(
                "Tải file video nhảy (.mp4, .mov):",
                type=["mp4", "mov", "webm"],
                key="dance_local_vid_uploader",
            )
            if uploaded_vid:
                temp_dir = Path("storage/dance")
                temp_dir.mkdir(parents=True, exist_ok=True)
                v_path = temp_dir / f"uploaded_{uploaded_vid.name}"
                with open(v_path, "wb") as f:
                    f.write(uploaded_vid.getbuffer())
                v_info = dance_service.get_video_info(str(v_path))
                st.session_state["dance_ref_info"] = {
                    "video_path": str(v_path),
                    "duration": v_info["duration"],
                    "fps": v_info["fps"],
                    "width": v_info["width"],
                    "height": v_info["height"],
                }
                st.session_state["dance_motion_video_path"] = str(v_path)


        # Hiển thị video mẫu và công cụ cắt đoạn
        ref_info = st.session_state.get("dance_ref_info")
        if ref_info and os.path.isfile(ref_info["video_path"]):
            st.video(ref_info["video_path"])
            st.info(
                f"Thời lượng: **{ref_info.get('duration', 0)}s** | "
                f"FPS: **{ref_info.get('fps', 30)}** | "
                f"Kích thước: **{ref_info.get('width', 1080)}x{ref_info.get('height', 1920)}**"
            )

            st.markdown("**Cắt đoạn nhảy tối ưu (Khuyên dùng 5s - 7s):**")
            dur_choice = st.selectbox(
                "Thời lượng mong muốn:",
                [
                    "5 giây (Tối ưu loop TikTok & Tiết kiệm chi phí)",
                    "7 giây (Chuẩn điệp khúc bốc lửa)",
                    "10 giây",
                    "Giữ nguyên độ dài gốc",
                ],
                key="dance_dur_choice_select",
            )

            if dur_choice != "Giữ nguyên độ dài gốc":
                chosen_sec = 5.0 if "5" in dur_choice else (7.0 if "7" in dur_choice else 10.0)
                orig_dur = float(ref_info.get("duration", 0.0))
                max_start = max(0.0, orig_dur - chosen_sec)
                start_val = st.slider(
                    "Bắt đầu từ giây thứ:",
                    min_value=0.0,
                    max_value=float(max_start) if max_start > 0 else 0.0,
                    value=0.0,
                    step=0.5,
                    key="dance_trim_slider",
                )

                if st.button("Cắt & Chuẩn Hóa 9:16 (30 FPS)", icon=":material/content_cut:", use_container_width=True, key="dance_btn_trim"):
                    trimmed_target = f"storage/dance/trimmed_{int(start_val)}_{int(chosen_sec)}s.mp4"
                    ok, msg = dance_service.trim_dance_video(
                        ref_info["video_path"],
                        start_val,
                        chosen_sec,
                        trimmed_target,
                    )
                    if ok:
                        st.session_state["dance_motion_video_path"] = trimmed_target
                        st.success("Cắt đoạn nhảy thành công!")
                    else:
                        st.error(f"Lỗi cắt video: {msg}")

            curr_motion = st.session_state.get("dance_motion_video_path")
            if curr_motion and curr_motion != ref_info["video_path"] and os.path.isfile(curr_motion):
                st.caption("Đoạn nhảy đã cắt sẵn sàng cho AI:")
                st.video(curr_motion)

    # --- CỘT 2: ẢNH NGƯỜI MẪU & GÓI KLING AI ---
    with col2:
        st.subheader("2. Ảnh Người Mẫu")
        c_m1, c_m2 = st.columns(2)
        with c_m1:
            if st.button("Lấy Ảnh Thử Đồ", icon=":material/sync:", use_container_width=True, key="dance_btn_use_tryon"):
                latest_tryon = st.session_state.get("latest_tryon_result")
                if latest_tryon and os.path.isfile(latest_tryon):
                    st.session_state["dance_model_image_path"] = latest_tryon
                    st.success("Đã nạp ảnh người mẫu từ Tab Thử Đồ!")
                else:
                    st.warning("Chưa có ảnh thử đồ từ Tab 2.")
        with c_m2:
            if st.button("Dùng Mẫu Nữ Test", icon=":material/face:", use_container_width=True, key="dance_btn_use_sample_model"):
                sample_model_path = "storage/models/model_vietnamese_girl.png"
                if os.path.isfile(sample_model_path):
                    st.session_state["dance_model_image_path"] = sample_model_path
                    st.success("Đã nạp người mẫu Việt Nam xinh đẹp!")
                else:
                    st.warning("Chưa tìm thấy file ảnh mẫu.")

        uploaded_model_file = st.file_uploader(
            "Hoặc tải ảnh mẫu khác (.png, .jpg):",
            type=["png", "jpg", "jpeg", "webp"],
            key="dance_model_file_uploader",
        )
        if uploaded_model_file:
            temp_dir = Path("storage/dance")
            temp_dir.mkdir(parents=True, exist_ok=True)
            m_path = temp_dir / f"model_{uploaded_model_file.name}"
            with open(m_path, "wb") as f:
                f.write(uploaded_model_file.getbuffer())
            st.session_state["dance_model_image_path"] = str(m_path)

        model_img_path = st.session_state.get("dance_model_image_path")
        if model_img_path and os.path.isfile(model_img_path):
            st.image(model_img_path, caption="Người mẫu được chọn", use_container_width=True)

            st.markdown("---")
            st.markdown("##### Chế độ tạo video:")
            dance_mode = st.radio(
                "Phương thức tạo video nhảy:",
                [
                    "Tạo Tự Động 100% Trong Tool (1-Click AI)",
                    "Xuất Gói Cho Kling Web (Miễn phí 0đ)",
                ],
                key="dance_engine_mode_radio",
            )

            if "Tự Động" in dance_mode:
                st.caption("AI tự động phân tích khung xương, tạo video nhảy và nâng cấp 60 FPS ngay trong tool, không cần mở web.")
                config_token = (getattr(config, "dance", {}) or {}).get("replicate_api_token", "") or (getattr(config, "tryon", {}) or {}).get("replicate_api_token", "")

                if config_token:
                    st.caption(":material/check_circle: **Đã nạp API Token từ cấu hình (`config.toml`)**")
                    with st.expander("Thay đổi API Token (Tùy chọn)"):
                        custom_token = st.text_input(
                            "Replicate Token mới:",
                            value=config_token,
                            type="password",
                            key="dance_api_token_input",
                        )
                        if custom_token:
                            config_token = custom_token
                else:
                    config_token = st.text_input(
                        "Replicate API Token:",
                        value="",
                        type="password",
                        help="Nhập token từ replicate.com để chạy tự động mô hình MimicMotion/Kling trong tool.",
                        key="dance_api_token_input",
                    )

                btn_auto_dance = st.button(
                    "Tạo Video Nhảy AI (1-Click Trong Tool)",
                    icon=":material/smart_toy:",
                    type="primary",
                    use_container_width=True,
                    disabled=not st.session_state.get("dance_motion_video_path"),
                    key="dance_btn_run_auto_api",
                )
                if btn_auto_dance:
                    if not config_token:
                        st.error("Chưa có API Token. Vui lòng cấu hình replicate_api_token trong config.toml.")
                    else:
                        with st.spinner("AI đang tạo video nhảy từ ảnh và cử động... (khoảng 1-2 phút)"):
                            ok, res_vid, msg = dance_service.generate_dance_video_api(
                                model_img_path,
                                st.session_state["dance_motion_video_path"],
                                api_token=config_token,
                            )
                            if ok:
                                st.session_state["dance_kling_raw_video"] = res_vid
                                st.success("Tạo video thành công! Đang tự động nâng cấp 60 FPS...")
                                out_enhanced = f"storage/dance/final_dance_60fps_{int(time.time())}.mp4"
                                ok_gpu, res_60fps = dance_service.enhance_video_60fps_gpu(
                                    res_vid,
                                    out_enhanced,
                                    target_fps=60,
                                    sharpen=True,
                                )
                                if ok_gpu:
                                    st.session_state["dance_final_enhanced_video"] = res_60fps
                                    st.success("Hoàn tất tạo video nhảy 60 FPS siêu mượt!")
                                else:
                                    st.session_state["dance_final_enhanced_video"] = res_vid
                            else:
                                st.error(f"Lỗi tạo video: {msg}")
            else:
                st.caption("Dành cho bạn muốn dùng 66 credit miễn phí mỗi ngày trên web klingai.com mà không cần mua API.")
                btn_kling = st.button(
                    "Chuẩn Hóa & Xuất Cho Kling AI Web",
                    icon=":material/inventory_2:",
                    type="primary",
                    use_container_width=True,
                    disabled=not st.session_state.get("dance_motion_video_path"),
                    key="dance_btn_prepare_kling",
                )
                if btn_kling:
                    with st.spinner("Đang chuẩn hóa ảnh mẫu 9:16 và video chuyển động..."):
                        ok, pkg = dance_service.prepare_kling_package(
                            model_img_path,
                            st.session_state["dance_motion_video_path"],
                        )
                        if ok:
                            st.session_state["dance_kling_pkg"] = pkg
                            st.success("Đã chuẩn hóa ảnh 9:16 và video nhảy!")
                        else:
                            st.error(f"Thất bại: {pkg.get('error')}")

                kling_pkg = st.session_state.get("dance_kling_pkg")
                if kling_pkg:
                    st.info(f"Thư mục: `{kling_pkg['folder']}`")
                    c_k1, c_k2 = st.columns(2)
                    with c_k1:
                        if st.button("Mở Thư Mục", icon=":material/folder_open:", use_container_width=True, key="dance_btn_open_pkg"):
                            os.startfile(kling_pkg["folder"])
                    with c_k2:
                        st.link_button("Mở Kling AI", "https://klingai.com", icon=":material/open_in_new:", use_container_width=True)

                    st.markdown("**Gợi ý Prompt Kling AI:**")
                    st.code(
                        "A stunning young Vietnamese woman dancing gracefully and energetically following the reference video motion, smooth hip movement, charming smile, confident expression, photorealistic 8k, cinematic lighting, natural body physics, highly detailed fabric movement, 60fps.",
                        language="text",
                    )
                    st.markdown("**Negative Prompt:**")
                    st.code(
                        "deformed limbs, distorted face, extra arms, bad anatomy, blurry, flickering, jittery, low quality, artifacts, watermark",
                        language="text",
                    )

    # --- CỘT 3: NÂNG CẤP 60 FPS BẰNG GPU NVIDIA RTX ---
    with col3:
        st.subheader("3. Nâng Cấp 60 FPS Bằng GPU RTX")
        if "sample_dance_7s.mp4" in str(st.session_state.get("dance_kling_raw_video", "")):
            st.session_state.pop("dance_kling_raw_video", None)

        uploaded_kling = st.file_uploader(
            "Kéo thả video sau khi Kling AI tạo xong vào đây:",
            type=["mp4", "mov", "webm"],
            key="dance_kling_rendered_uploader",
        )
        if uploaded_kling:
            temp_dir = Path("storage/dance")
            temp_dir.mkdir(parents=True, exist_ok=True)
            k_path = temp_dir / f"kling_raw_{uploaded_kling.name}"
            with open(k_path, "wb") as f:
                f.write(uploaded_kling.getbuffer())
            st.session_state["dance_kling_raw_video"] = str(k_path)

        kling_raw = st.session_state.get("dance_kling_raw_video")
        if kling_raw and os.path.isfile(kling_raw):
            st.caption("Video nhận từ Kling AI:")
            st.video(kling_raw)

            chk_60fps = st.checkbox(
                "Nâng cấp 60 FPS mượt mà (Smooth 60 FPS)",
                value=True,
                key="dance_chk_60fps",
            )
            chk_sharpen = st.checkbox(
                "Tăng độ nét chi tiết trang phục bằng GPU RTX",
                value=True,
                key="dance_chk_sharpen",
            )
            chk_ultra = st.checkbox(
                "Nội suy nâng cao (Motion Interpolation)",
                value=False,
                key="dance_chk_ultra",
            )

            btn_enhance = st.button(
                "Nâng Cấp Bằng GPU RTX (NVENC)",
                icon=":material/bolt:",
                type="primary",
                use_container_width=True,
                key="dance_btn_run_enhance",
            )

            if btn_enhance:
                with st.spinner("Đang kích hoạt GPU NVIDIA RTX để nâng cấp 60 FPS và độ nét..."):
                    out_enhanced = "storage/dance/final_dance_60fps.mp4"
                    ok, res_path = dance_service.enhance_video_60fps_gpu(
                        kling_raw,
                        out_enhanced,
                        target_fps=60 if chk_60fps else 30,
                        sharpen=chk_sharpen,
                        ultra_smooth=chk_ultra,
                    )
                    if ok:
                        st.session_state["dance_final_enhanced_video"] = res_path
                        st.success("Nâng cấp 60 FPS thành công bằng GPU RTX!")
                    else:
                        st.error(f"Lỗi nâng cấp: {res_path}")

        final_vid = st.session_state.get("dance_final_enhanced_video")
        if final_vid and os.path.isfile(final_vid):
            st.caption("Video 60 FPS sẵn sàng đăng TikTok:")
            st.video(final_vid)
            with open(final_vid, "rb") as f:
                st.download_button(
                    "Tải Video 60 FPS Về Máy",
                    icon=":material/download:",
                    data=f,
                    file_name="dance_model_60fps.mp4",
                    mime="video/mp4",
                    use_container_width=True,
                    key="dance_download_final_btn",
                )
            if st.button("Mở Thư Mục Chứa Video", icon=":material/folder_open:", use_container_width=True, key="dance_open_final_folder_btn"):
                os.startfile(str(Path(final_vid).parent.resolve()))

            st.markdown(
                """> **Quy trình đăng TikTok lên xu hướng:**
> 1. Mở app TikTok trên điện thoại, bấm dấu **+** và chọn video 60 FPS này.
> 2. Bấm **"Thêm âm thanh"** -> Chọn đúng bài nhạc gốc của clip nhảy.
> 3. Từng cú lắc hông, nhún nhảy sẽ **khớp 100% từng nhịp drop bass**, vừa chuẩn thuật toán âm thanh thịnh hành vừa không lo dính bản quyền!"""
            )


def _render_application():
    """Hiển thị thanh trên cùng, cửa sổ bật lên, biểu mẫu được tạo và kết quả tác vụ theo một thứ tự cố định."""
    _render_top_bar()

    if st.session_state.get("settings_dialog_open", False):
        _render_settings_dialog()

    if _apply_pending_settings_preset():
        st.success(tr("Settings Preset Imported"))

    restore_applied = _apply_pending_task_restore()
    restore_candidate_id = st.session_state.get("task_restore_candidate_id")
    if restore_candidate_id:
        _render_task_restore_dialog(restore_candidate_id)
    restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
    if restore_applied or restore_succeeded:
        st.success(tr("Task Configuration Loaded"))

    tab_video, tab_tryon, tab_dance = st.tabs([
        ":material/videocam: " + tr("Generate Video"),
        ":material/checkroom: Thử Đồ Ảo AI (Affiliate)",
        ":material/motion_photos_auto: Video Nhảy AI & 60 FPS RTX",
    ])

    with tab_video:
        with st.container(key="main_settings_grid"):
            panel = st.columns(4)
        left_panel = panel[0]
        middle_panel = panel[1]
        audio_panel = panel[2]
        right_panel = panel[3]

        params = VideoParams(video_subject="")
        params.match_materials_to_script = bool(
            st.session_state.get("match_materials_to_script", False)
        )
        _render_script_settings(left_panel, params)

        uploaded_files = _render_video_settings(middle_panel, params)
        uploaded_audio_file, uploaded_bgm_file, voice_mode = _render_audio_settings(
            audio_panel, params
        )

        _render_subtitle_settings(right_panel, params)

        generation_submitted = _render_generation_controls(
            params,
            uploaded_files,
            uploaded_audio_file,
            uploaded_bgm_file,
            voice_mode,
        )

        if not generation_submitted:
            _save_runtime_config()

    with tab_tryon:
        _render_tryon_studio()

    with tab_dance:
        _render_dance_studio()


_render_application()


