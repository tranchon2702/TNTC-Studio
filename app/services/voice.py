import asyncio
import base64
import io
import inspect
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import wave
from datetime import datetime, timedelta
from typing import Union
from urllib.parse import urlparse
from xml.sax.saxutils import escape, unescape

import edge_tts
import requests
from edge_tts import SubMaker
from edge_tts.srt_composer import Subtitle
from loguru import logger
from moviepy.video.tools import subtitles
from moviepy.audio.io.AudioFileClip import AudioFileClip
from openai import OpenAI

from app.config import config
from app.utils import utils

_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS = 30.0
_MIMO_DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
_MIMO_DEFAULT_TTS_MODEL = "mimo-v2.5-tts"
MINIMAX_TTS_GLOBAL_URL = "https://api.minimax.io/v1/t2a_v2"
MINIMAX_TTS_CN_URL = "https://api.minimaxi.com/v1/t2a_v2"
MINIMAX_TTS_DEFAULT_MODEL = "speech-2.8-hd"
MINIMAX_TTS_DEFAULT_VOICE = "English_expressive_narrator"
MINIMAX_TTS_MODELS = (
    "speech-2.8-hd", "speech-2.8-turbo", "speech-2.6-hd", "speech-2.6-turbo",
    "speech-02-hd", "speech-02-turbo", "speech-01-hd", "speech-01-turbo",
)
GEMINI_TTS_VOICES = (
    ("Zephyr", "Bright"),
    ("Puck", "Upbeat"),
    ("Charon", "Informative"),
    ("Kore", "Firm"),
    ("Fenrir", "Excitable"),
    ("Leda", "Youthful"),
    ("Orus", "Firm"),
    ("Aoede", "Breezy"),
    ("Callirrhoe", "Easy-going"),
    ("Autonoe", "Bright"),
    ("Enceladus", "Breathy"),
    ("Iapetus", "Clear"),
    ("Umbriel", "Easy-going"),
    ("Algieba", "Smooth"),
    ("Despina", "Smooth"),
    ("Erinome", "Clear"),
    ("Algenib", "Gravelly"),
    ("Rasalgethi", "Informative"),
    ("Laomedeia", "Upbeat"),
    ("Achernar", "Soft"),
    ("Alnilam", "Firm"),
    ("Schedar", "Even"),
    ("Gacrux", "Mature"),
    ("Pulcherrima", "Forward"),
    ("Achird", "Friendly"),
    ("Zubenelgenubi", "Casual"),
    ("Vindemiatrix", "Gentle"),
    ("Sadachbia", "Lively"),
    ("Sadaltager", "Knowledgeable"),
    ("Sulafat", "Warm"),
)
_MINIMAX_TTS_MAX_AUDIO_HEX_CHARS = 100 * 1024 * 1024
NO_VOICE_NAME = "no-voice"
# `none` là cờ không lồng tiếng được sử dụng trong PR #981. Giá trị này tương thích với giá trị này trong thời gian ngắn để tránh
# Người dùng API đã gọi nhánh này theo cách thủ công sẽ không hợp lệ ngay sau khi nâng cấp; WebUI và mã mới sẽ được sử dụng thống nhất
# Rõ ràng hơn `không có giọng nói`.
_NO_VOICE_ALIASES = {NO_VOICE_NAME, "none"}


def _configure_pydub_ffmpeg(audio_segment_cls):
    configured_ffmpeg = utils.get_ffmpeg_binary()
    if configured_ffmpeg:
        audio_segment_cls.converter = configured_ffmpeg


def mktimestamp(time_unit: float) -> str:
    """
    Chuyển đổi đơn vị thời gian 100 nano giây được edge_tts sử dụng thành dấu thời gian phụ đề.

    edge_tts 7.x không còn xuất `mktimestamp` trong phiên bản cũ nữa mà là liên kết phụ đề cũ trong dự án
    Chức năng định dạng này cũng cần thiết để tương thích với Azure v2, Gemini, SiliconFlow, v.v.
    Dòng thời gian phụ đề được xây dựng theo cách thủ công, do đó việc triển khai tương đương được tích hợp ở đây.
    """
    hour = math.floor(time_unit / 10**7 / 3600)
    minute = math.floor((time_unit / 10**7 / 60) % 60)
    seconds = (time_unit / 10**7) % 60
    return f"{hour:02d}:{minute:02d}:{seconds:06.3f}"


def get_siliconflow_voices() -> list[str]:
    """
    Nhận danh sách âm thanh chảy dựa trên silicon

    Returns:
        Danh sách giọng nói, ở định dạng ["siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex", ...]
    """
    # Danh sách âm thanh và giới tính tương ứng dựa trên silicon (để hiển thị)
    voices_with_gender = [
        ("FunAudioLLM/CosyVoice2-0.5B", "alex", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "anna", "Female"),
        ("FunAudioLLM/CosyVoice2-0.5B", "bella", "Female"),
        ("FunAudioLLM/CosyVoice2-0.5B", "benjamin", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "charles", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "claire", "Female"),
        ("FunAudioLLM/CosyVoice2-0.5B", "david", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "diana", "Female"),
    ]

    # Thêm siliconflow: tiền tố và định dạng làm tên hiển thị
    return [
        f"siliconflow:{model}:{voice}-{gender}"
        for model, voice, gender in voices_with_gender
    ]


def get_gemini_voices() -> list[str]:
    """
    Nhận danh sách giai điệu cài sẵn Gemini TTS chính thức.

    Google không xuất bản siêu dữ liệu giới tính cho những âm thanh này nên hộp thả xuống sử dụng mô tả kiểu chính thức.
    Tránh viết giới tính giả định vào id giọng nói liên tục. Nguồn danh mục âm thanh:
    https://ai.google.dev/gemini-api/docs/speech-generation#voice-options

    Returns:
        Danh sách âm thanh ở định dạng ["gemini:Zephyr-Bright", "gemini:Puck-Upbeat", ...]
    """
    return [f"gemini:{voice}-{style}" for voice, style in GEMINI_TTS_VOICES]


def get_mimo_voices() -> list[str]:
    """
    Nhận danh sách âm cài sẵn cho Xiaomi MiMo V2.5 TTS.

    Hiện tại, chỉ có chế độ âm cài sẵn `mimo-v2.5-tts` trong tài liệu chính thức được kết nối. thiết kế âm thanh
    `mimo-v2.5-tts-voicedesign` và bản sao âm thanh `mimo-v2.5-tts-voiceclone`
    Cần có các biểu mẫu đầu vào bổ sung và quy trình tải lên tài liệu. Không trộn chúng vào các hộp thả xuống TTS thông thường để tránh
    Người dùng lầm tưởng rằng việc chọn id giọng nói sẽ hoàn thành tất cả các khả năng nâng cao.
    """
    voices_with_gender = [
        ("mimo_default", "Female"),
        ("冰糖", "Female"),
        ("茉莉", "Female"),
        ("苏打", "Male"),
        ("白桦", "Male"),
        ("Mia", "Female"),
        ("Chloe", "Female"),
        ("Milo", "Male"),
        ("Dean", "Male"),
    ]

    return [f"mimo:{voice}-{gender}" for voice, gender in voices_with_gender]


def get_minimax_voices(voice_id: str | None = None) -> list[str]:
    """Trả về bản vá MiniMax hiện được định cấu hình để sử dụng theo định dạng lập lịch TTS thống nhất."""
    voice_id = str(
        voice_id
        or config.minimax_tts.get("voice_id", MINIMAX_TTS_DEFAULT_VOICE)
        or MINIMAX_TTS_DEFAULT_VOICE
    ).strip()
    return [f"minimax:{voice_id}"]


def get_elevenlabs_voices(api_key: str) -> list[str]:
    if not api_key:
        return []
    try:
        url = "https://api.elevenlabs.io/v2/voices"
        params = {"is_favorite": "true", "page_size": 100}
        headers = {"xi-api-key": api_key}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(
                f"ElevenLabs voices fetch failed with status {response.status_code}: {response.text}"
            )
            return []
        data = response.json()
        voices = data.get("voices", [])
        return [
            f"elevenlabs:{v['voice_id']}:{v['name']}"
            for v in voices
            if v.get("voice_id") and v.get("name") and v.get("status") != "disabled"
        ]
    except Exception as e:
        logger.warning(f"ElevenLabs voices fetch failed: {str(e)}")
        return []


def get_chatterbox_voices() -> list[str]:
    """Return the configured Chatterbox voices.

    Chatterbox is self-hosted, so there is no global voice catalog. Operators
    list the voice names exposed by their server via ``[chatterbox] voices``
    (a TOML array, or a comma-separated string). Each entry is normalised to
    the ``chatterbox:<name>`` format used by the TTS dispatcher.
    """
    voices = config.chatterbox.get("voices", []) or []
    if isinstance(voices, str):
        voices = [v.strip() for v in voices.split(",") if v.strip()]
    result = []
    for v in voices:
        v = str(v).strip()
        if not v:
            continue
        result.append(v if v.startswith("chatterbox:") else f"chatterbox:{v}")
    if not result:
        # keep the dropdown usable even before any voice is configured
        result = ["chatterbox:default-Female"]
    return result


KOKORO_DEFAULT_VOICE = "af_heart"


def _normalize_kokoro_voices(entries) -> list[str]:
    """Thống nhất cấu hình thủ công và định dạng cũ và mới phía máy chủ, chỉ nhận ID thực và tránh chuyển đổi đối tượng thành tên âm sắc."""
    if isinstance(entries, str):
        entries = entries.split(",")
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        name = entry.get("id") if isinstance(entry, dict) else entry
        if not isinstance(name, str):
            continue
        name = name.strip().removeprefix("kokoro:").strip()
        if name:
            value = f"kokoro:{name}"
            if value not in result:
                result.append(value)
    return result


def get_kokoro_voices(*, fallback: bool = True) -> list[str]:
    """Âm thanh thủ công được ưu tiên, nếu không thì máy chủ sẽ được truy vấn; giao diện người dùng có thể tắt các cài đặt mặc định để nhận biết các ngắt kết nối và duy trì các lựa chọn.

    Phiên bản cũ của máy chủ trả về danh sách các chuỗi, phiên bản mới trả về danh sách các đối tượng chứa id. Không ở lớp dịch vụ khi thất bại
    Trạng thái phiên bộ đệm; thư mục thành công cuối cùng được WebUI giữ lại để ngăn những người dùng/điểm cuối khác nhau lây nhiễm lẫn nhau.
    """
    voices = _normalize_kokoro_voices(config.kokoro.get("voices"))
    if not voices:
        base_url = (config.kokoro.get("base_url", "") or "").strip().rstrip("/")
        if base_url:
            try:
                headers = {}
                api_key = config.kokoro.get("api_key", "")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                response = requests.get(
                    f"{base_url}/audio/voices", headers=headers, timeout=5
                )
                if response.status_code == 200:
                    data = response.json()
                    listed = data.get("voices", []) if isinstance(data, dict) else data
                    voices = _normalize_kokoro_voices(listed)
                    if not voices:
                        logger.warning("kokoro voice list contains no valid voice IDs")
                else:
                    logger.warning(
                        f"kokoro voices request failed with status {response.status_code}"
                    )
            except Exception as e:
                # Không xuất URL/văn bản ngoại lệ để ngăn các tham số truy vấn hoặc thông tin xác thực từ các địa chỉ tự lưu trữ vào nhật ký.
                logger.warning(f"kokoro voice list unavailable ({type(e).__name__})")
    return voices or ([f"kokoro:{KOKORO_DEFAULT_VOICE}"] if fallback else [])


def get_fish_audio_voices() -> list[str]:
    """Return configured Fish Audio voices.

    Each entry follows the format ``fish_audio:<reference_id>:<display_name>``.
    When ``reference_id`` is "default", Fish Audio's built-in default voice is
    used (no ``reference_id`` is sent in the API request).  Operators can list
    additional public or cloned voices via ``[fish_audio] voices`` in the
    config file.
    """
    result = [
        "fish_audio:2324c907b9a94c64ab4afb941e5b3408:Clear Female-Female",
        "fish_audio:7b6131ba75ba47c98a46c847db729ab6:Clear Male-Male",
        "fish_audio:default:Default Voice",
    ]
    voices = config.fish_audio.get("voices", []) or []
    if isinstance(voices, str):
        voices = [v.strip() for v in voices.split(",") if v.strip()]
    for entry in voices:
        entry = str(entry).strip()
        if not entry:
            continue
        if entry.startswith("fish_audio:"):
            result.append(entry)
        elif ":" in entry:
            # "<reference_id>:<display_name>"
            result.append(f"fish_audio:{entry}")
        else:
            # bare reference_id
            result.append(f"fish_audio:{entry}:{entry}")
    return result


_AZURE_VOICES_DATA_FILE = os.path.join(
    os.path.dirname(__file__), "data", "azure_voices.json"
)
_azure_voices_cache = None


def _load_azure_voices() -> list[dict]:
    global _azure_voices_cache
    if _azure_voices_cache is None:
        with open(_AZURE_VOICES_DATA_FILE, "r", encoding="utf-8") as f:
            _azure_voices_cache = json.load(f)
    return _azure_voices_cache


def get_all_azure_voices(filter_locals=None) -> list[str]:
    voices = []
    for item in _load_azure_voices():
        name = item["name"]
        gender = item["gender"]
        # Áp dụng bộ lọc
        if filter_locals and any(
            name.lower().startswith(fl.lower()) for fl in filter_locals
        ):
            voices.append(f"{name}-{gender}")
        elif not filter_locals:
            voices.append(f"{name}-{gender}")

    voices.sort()
    return voices


def parse_voice_name(name: str):
    # zh-CN-XiaoyiNeural-Female
    # zh-CN-YunxiNeural-Male
    # zh-CN-XiaoxiaoMultilingualNeural-V2-Female
    name = name.replace("-Female", "").replace("-Male", "").strip()
    return name


def is_azure_v2_voice(voice_name: str):
    voice_name = parse_voice_name(voice_name)
    if voice_name.endswith("-V2"):
        return voice_name.replace("-V2", "").strip()
    return ""


def is_siliconflow_voice(voice_name: str):
    """Kiểm tra xem đó có phải là âm thanh của dòng chảy dựa trên silicon không"""
    return voice_name.startswith("siliconflow:")


def is_gemini_voice(voice_name: str):
    """Kiểm tra xem đó có phải là âm thanh của Gemini TTS không"""
    return voice_name.startswith("gemini:")


def parse_gemini_voice_name(voice_name: str | None) -> str:
    """Trích xuất tên bản vá đặt trước được API Google sử dụng từ các giá trị thả xuống Gemini cũ và mới."""
    if not is_gemini_voice(voice_name or ""):
        return ""
    return (voice_name or "").split(":", 1)[1].split("-", 1)[0].strip()


def is_mimo_voice(voice_name: str):
    """Kiểm tra xem đó có phải là âm thanh của Xiaomi MiMo TTS không"""
    return voice_name.startswith("mimo:")


def is_minimax_voice(voice_name: str | None) -> bool:
    return (voice_name or "").startswith("minimax:")


def is_elevenlabs_voice(voice_name: str) -> bool:
    return (voice_name or "").startswith("elevenlabs:")


def get_elevenlabs_api_key() -> str:
    """
    Đọc Khóa API được ElevenLabs TTS sử dụng.

    Các tệp cấu hình được ưu tiên, các biến môi trường chỉ được sử dụng làm nguồn dự phòng nếu không được định cấu hình. Các tính năng WebUI và nhạc nền đã được
    Hỗ trợ ``ELEVENLABS_API_KEY``, TTS phải sử dụng quy tắc tương tự, nếu không thì chỉ thông qua môi trường container
    Khi triển khai các biến, danh sách âm sắc có thể được tải bình thường, nhưng giọng nói tổng hợp thực tế sẽ báo cáo sai rằng Khóa không được định cấu hình.
    """
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    return configured_key or os.getenv("ELEVENLABS_API_KEY", "").strip()


def is_chatterbox_voice(voice_name: str) -> bool:
    return (voice_name or "").startswith("chatterbox:")


def is_kokoro_voice(voice_name: str) -> bool:
    return (voice_name or "").startswith("kokoro:")


def is_viettts_voice(voice_name: str) -> bool:
    """Check whether the voice belongs to a self-hosted VietTTS server."""
    return (voice_name or "").startswith("viettts:")


def get_viettts_voices() -> list[str]:
    """Return configured VietTTS voices.

    VietTTS (dangvansam/viet-tts, MIT) is an open-source Vietnamese TTS with
    an OpenAI-compatible API.  Operators list voice names via ``[viettts] voices``
    in the config.  When the list is empty, the function tries to query the
    server's ``/voices`` endpoint.
    """
    voices_cfg = config.viettts.get("voices", []) or []
    if isinstance(voices_cfg, str):
        voices_cfg = [v.strip() for v in voices_cfg.split(",") if v.strip()]
    result = []
    for v in voices_cfg:
        v = str(v).strip()
        if not v:
            continue
        result.append(v if v.startswith("viettts:") else f"viettts:{v}")

    if not result:
        base_url = (config.viettts.get("base_url", "") or "").strip().rstrip("/")
        if base_url:
            try:
                headers = {}
                api_key = config.viettts.get("api_key", "")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                response = requests.get(
                    f"{base_url}/voices", headers=headers, timeout=5
                )
                if response.status_code == 200:
                    data = response.json()
                    listed = data if isinstance(data, list) else data.get("voices", [])
                    for entry in listed:
                        name = entry.get("id") if isinstance(entry, dict) else entry
                        if isinstance(name, str) and name.strip():
                            value = f"viettts:{name.strip()}"
                            if value not in result:
                                result.append(value)
                else:
                    logger.warning(
                        f"viettts voices request failed with status {response.status_code}"
                    )
            except Exception as e:
                logger.warning(f"viettts voice list unavailable ({type(e).__name__})")

    if not result:
        result = ["viettts:nsnd-le-chuc"]
    return result


def is_fish_audio_voice(voice_name: str) -> bool:
    return (voice_name or "").startswith("fish_audio:")


def get_fish_audio_api_key() -> str:
    configured_key = str(config.fish_audio.get("api_key", "") if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict) else "").strip()
    return configured_key or os.getenv("FISH_API_KEY", "").strip()


def is_no_voice(voice_name: str | None) -> bool:
    """
    Xác định xem người dùng có chọn rõ ràng chế độ "không lồng tiếng" hay không.

    Ở đây, chuỗi trống được cố tình không coi là không có lồng tiếng: giọng trống có nhiều khả năng là cấu hình bị hỏng hoặc phiên bản cũ.
    Trạng thái WebUI bị mất hoặc thiếu tham số giao diện. Chỉ những người canh gác rõ ràng mới vào nhánh im lặng,
    Điều này ngăn chặn các lỗi thực sự được ngụy trang dưới dạng các bản dựng thông thường.
    """
    return str(voice_name or "").strip().lower() in _NO_VOICE_ALIASES


def is_azure_v1_voice(voice_name: str | None) -> bool:
    """
    Kiểm tra xem âm thanh có phải là cài đặt trước Azure TTS v1 (Edge TTS) hay không.

    Phiên bản đầu tiên của liên kết tổng hợp được phân đoạn cho thẻ tạm dừng ([pause: ...]) chỉ dành cho Edge TTS (Azure TTS v1),
    Tránh thay đổi yêu cầu thanh toán, tần suất và hành vi mặc định của các nhà cung cấp khác như Gemini, Fish Audio, SiliconFlow, Kokoro, v.v.
    """
    if not voice_name:
        return False
    name = str(voice_name).strip()
    if is_no_voice(name):
        return False
    if is_azure_v2_voice(name):
        return False
    if is_siliconflow_voice(name):
        return False
    if is_gemini_voice(name):
        return False
    if is_mimo_voice(name):
        return False
    if is_minimax_voice(name):
        return False
    if is_elevenlabs_voice(name):
        return False
    if is_chatterbox_voice(name):
        return False
    if is_kokoro_voice(name):
        return False
    if is_viettts_voice(name):
        return False
    if is_fish_audio_voice(name):
        return False
    return True


def estimate_no_voice_duration(text: str) -> float:
    """
    Ước tính độ dài dòng thời gian video ổn định cho chế độ không lồng tiếng.

    Việc không lồng tiếng vẫn yêu cầu trình giữ chỗ âm thanh để điều khiển việc cắt bớt cảnh hiện có, dòng thời gian phụ đề và bố cục cuối cùng.
    Giữ chiến lược ước tính càng đơn giản càng tốt:
    1. Các ký tự tiếng Trung và các ký tự CJK khác ước tính khoảng 4,2 từ/giây;
    2. Tiếng Anh/Số ước tính khoảng 2,7 từ/giây;
    3. Văn bản bằng các ngôn ngữ khác ước tính khoảng 4,0 ký tự/giây, bao gồm tiếng Nga, tiếng Ả Rập,
       Các văn bản tiếng Nhật Kana, tiếng Hàn và các văn bản không phải ASCII khác;
    4. Thêm một chút ngắt quãng ở mỗi đoạn câu để việc chuyển phụ đề không quá chặt chẽ;
    5. Ít nhất 3 giây để tránh các tập lệnh cực ngắn tạo ra âm thanh 0 giây.
    """
    normalized_text = (text or "").strip()
    if not normalized_text:
        return 3.0

    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", normalized_text))
    words = len(re.findall(r"[A-Za-z0-9]+", normalized_text))
    ascii_word_chars = sum(len(word) for word in re.findall(r"[A-Za-z0-9]+", normalized_text))
    other_text_chars = 0
    for char in normalized_text:
        # Các danh mục Unicode bắt đầu bằng L để biểu thị các chữ cái trong nhiều ngôn ngữ khác nhau và N để biểu thị các số. Trước đây đã một mình rồi
        # Các từ CJK và ASCII được tính và chỉ văn bản còn lại được tính ở đây để tránh việc đếm tiếng Anh lặp lại.
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            other_text_chars += 1
    other_text_chars = max(other_text_chars - cjk_chars - ascii_word_chars, 0)
    sentence_count = max(len(utils.split_string_by_punctuations(normalized_text)), 1)

    cjk_duration = cjk_chars / 4.2
    word_duration = words / 2.7
    other_text_duration = other_text_chars / 4.0
    pause_duration = max(sentence_count - 1, 0) * 0.35
    return max(3.0, cjk_duration + word_duration + other_text_duration + pause_duration)


def generate_silent_audio(duration_seconds: float, output_file: str) -> bool:
    """
    Tạo âm thanh im lặng.

    Hỗ trợ tạo trực tiếp âm thanh PCM WAV 16 bit đơn âm (chính xác đến một mẫu duy nhất, không có độ trễ mã hóa),
    Hoặc tạo âm thanh MP3 qua FFmpeg anullsrc (làm trình giữ chỗ cho chế độ "không lồng tiếng").
    """
    ensure_file_path_exists(output_file)
    duration_seconds = max(
        float(duration_seconds or 0), utils.MIN_PAUSE_DURATION_SECONDS
    )

    if output_file.lower().endswith(".wav"):
        sample_rate = 24000
        num_samples = int(round(duration_seconds * sample_rate))
        with wave.open(output_file, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * num_samples)
        return os.path.exists(output_file) and os.path.getsize(output_file) > 0

    ffmpeg_binary = utils.get_ffmpeg_binary()
    command = [
        ffmpeg_binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        f"{duration_seconds:.3f}",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        output_file,
    ]

    logger.info(
        f"generating silent audio for no-voice mode, duration: {duration_seconds:.2f}s"
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "failed to generate silent audio: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
        return False
    if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
        logger.error(
            "silent audio output file is missing or empty, "
            f"file: {output_file}, duration: {duration_seconds:.2f}s"
        )
        return False
    return True


def _single_tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    if is_no_voice(voice_name):
        duration_seconds = estimate_no_voice_duration(text)
        if not generate_silent_audio(duration_seconds, voice_file):
            return None

        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=text,
            audio_duration_seconds=duration_seconds,
        )

    if is_azure_v2_voice(voice_name):
        return azure_tts_v2(
            text,
            voice_name,
            voice_file,
            voice_rate=voice_rate,
        )
    elif is_siliconflow_voice(voice_name):
        # Trích xuất mô hình và giọng nói từ voice_name
        # Định dạng: siliconflow:model:Voice-Giới tính
        parts = voice_name.split(":")
        if len(parts) >= 3:
            model = parts[1]
            # Xóa hậu tố giới tính, chẳng hạn như "alex-Male" -> "alex"
            voice_with_gender = parts[2]
            voice = voice_with_gender.split("-")[0]
            # Xây dựng các tham số giọng nói hoàn chỉnh theo định dạng "model:voice"
            full_voice = f"{model}:{voice}"
            return siliconflow_tts(
                text, model, full_voice, voice_rate, voice_file, voice_volume
            )
        else:
            logger.error(f"Invalid siliconflow voice name format: {voice_name}")
            return None
    elif is_gemini_voice(voice_name):
        # Trích xuất tên giọng nói từ voice_name
        # Định dạng: gemini:Voice-Style; cũng tiếp tục tương thích với gemini cũ:Voice-Gender.
        voice = parse_gemini_voice_name(voice_name)
        if voice:
            return gemini_tts(text, voice, voice_rate, voice_file, voice_volume)
        else:
            logger.error(f"Invalid gemini voice name format: {voice_name}")
            return None
    elif is_mimo_voice(voice_name):
        # Trích xuất tên giọng nói từ voice_name
        # Định dạng: mimo:giọng-Giới tính; nếu người gọi đã thực thi phân tích cú pháp_voice_name,
        # thì đó có thể là mimo:voice. Cả hai định dạng đều tương thích.
        parts = voice_name.split(":")
        if len(parts) >= 2:
            voice_with_gender = parts[1]
            voice = voice_with_gender.split("-")[0]
            return mimo_tts(text, voice, voice_rate, voice_file, voice_volume)
        else:
            logger.error(f"Invalid mimo voice name format: {voice_name}")
            return None
    elif is_minimax_voice(voice_name):
        voice_id = voice_name.split(":", 1)[1].strip()
        if voice_id:
            return minimax_tts(text, voice_id, voice_rate, voice_file, voice_volume)
        logger.error(f"Invalid MiniMax voice name format: {voice_name}")
        return None
    elif is_elevenlabs_voice(voice_name):
        # Định dạng: Elevenlabs:{voice_id}:{name}
        parts = voice_name.split(":")
        if len(parts) >= 2:
            voice_id = parts[1]
            return elevenlabs_tts(text, voice_id, voice_file, voice_rate, voice_volume)
        else:
            logger.error(f"Invalid elevenlabs voice name format: {voice_name}")
            return None
    elif is_chatterbox_voice(voice_name):
        # Định dạng: chatterbox:<voice>, giọng nói có thể có hậu tố -Female/-Male để hiển thị
        parts = voice_name.split(":", 1)
        if len(parts) >= 2 and parts[1].strip():
            chatterbox_voice = parts[1].strip()
            if chatterbox_voice.endswith(("-Female", "-Male")):
                chatterbox_voice = chatterbox_voice.rsplit("-", 1)[0]
            return chatterbox_tts(
                text, chatterbox_voice, voice_file, voice_rate, voice_volume
            )
        else:
            logger.error(f"Invalid chatterbox voice name format: {voice_name}")
            return None
    elif is_kokoro_voice(voice_name):
        # Định dạng: kokoro:<voice>, giọng nói có thể có hậu tố -Female/-Male để hiển thị
        parts = voice_name.split(":", 1)
        if len(parts) >= 2 and parts[1].strip():
            kokoro_voice = parts[1].strip()
            if kokoro_voice.endswith(("-Female", "-Male")):
                kokoro_voice = kokoro_voice.rsplit("-", 1)[0]
            return kokoro_tts(
                text, kokoro_voice, voice_file, voice_rate, voice_volume
            )
        else:
            logger.error(f"Invalid kokoro voice name format: {voice_name}")
            return None
    elif is_viettts_voice(voice_name):
        # Format: viettts:<voice>
        parts = voice_name.split(":", 1)
        if len(parts) >= 2 and parts[1].strip():
            viet_voice = parts[1].strip()
            return viettts_tts(
                text, viet_voice, voice_file, voice_rate, voice_volume
            )
        else:
            logger.error(f"Invalid viettts voice name format: {voice_name}")
            return None
    elif is_fish_audio_voice(voice_name):
        parts = voice_name.split(":")
        reference_id = parts[1] if len(parts) >= 2 else "default"
        if reference_id == "default":
            reference_id = None
        return fish_audio_tts(text, voice_file, voice_rate, voice_volume, reference_id=reference_id)
    return azure_tts_v1(text, voice_name, voice_rate, voice_file)


def _concat_audio_files(audio_files: list[str], output_file: str) -> bool:
    """
    Hợp nhất nhiều phân đoạn âm thanh bằng cách sử dụng giải mã PCM và mã hóa lại thống nhất.

    Tất cả các phân đoạn đầu vào đều được chuyển đổi thống nhất thành các mẫu PCM tiêu chuẩn (24000Hz 16-bit mono) để ghép nối liền mạch.
    Cuối cùng, mã hóa nó thành tệp đích (chẳng hạn như MP3) trong một lần, giải quyết hoàn toàn độ trễ và phần đệm (độ trễ/đệm) của bộ mã hóa của từng đoạn MP3.
    Việc tích tụ âm thanh và video dẫn đến vấn đề không đồng bộ và trôi phụ đề.
    """
    if not audio_files:
        return False
    ensure_file_path_exists(output_file)
    if len(audio_files) == 1:
        if audio_files[0] != output_file:
            shutil.copyfile(audio_files[0], output_file)
        return True

    target_sample_rate = 24000
    combined_pcm = bytearray()
    ffmpeg_binary = utils.get_ffmpeg_binary()

    with tempfile.TemporaryDirectory() as concat_temp:
        for idx, f in enumerate(audio_files):
            if not os.path.exists(f) or os.path.getsize(f) == 0:
                continue

            # Kiểm tra xem nó đã là WAV đơn âm 16 bit 24000Hz chưa
            is_valid_pcm_wav = False
            if f.lower().endswith(".wav"):
                try:
                    with wave.open(f, "rb") as wf:
                        if (
                            wf.getframerate() == target_sample_rate
                            and wf.getnchannels() == 1
                            and wf.getsampwidth() == 2
                        ):
                            is_valid_pcm_wav = True
                            combined_pcm.extend(wf.readframes(wf.getnframes()))
                except Exception:
                    is_valid_pcm_wav = False

            if not is_valid_pcm_wav:
                # Giải mã tệp đầu vào thành 24000Hz 16-bit mono PCM WAV bằng FFmpeg
                pcm_wav = os.path.join(concat_temp, f"chunk_{idx}.wav")
                cmd = [
                    ffmpeg_binary,
                    "-y",
                    "-i",
                    f,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(target_sample_rate),
                    "-codec:a",
                    "pcm_s16le",
                    pcm_wav,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if res.returncode == 0 and os.path.exists(pcm_wav):
                    try:
                        with wave.open(pcm_wav, "rb") as wf:
                            combined_pcm.extend(wf.readframes(wf.getnframes()))
                    except Exception as e:
                        logger.error(f"failed to read decoded pcm wav: {e}")
                else:
                    logger.error(f"failed to decode audio chunk with ffmpeg: {res.stderr}")

        if not combined_pcm:
            logger.error("no valid audio samples to concatenate")
            return False

        temp_combined_wav = os.path.join(concat_temp, "combined_master.wav")
        with wave.open(temp_combined_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(target_sample_rate)
            wf.writeframes(combined_pcm)

        if output_file.lower().endswith(".wav"):
            shutil.copyfile(temp_combined_wav, output_file)
            return True

        command = [
            ffmpeg_binary,
            "-y",
            "-i",
            temp_combined_wav,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "4",
            output_file,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.error(
                "failed to encode concatenated audio to mp3: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
            return False
        return os.path.exists(output_file) and os.path.getsize(output_file) > 0


def _tts_with_pauses(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    Xử lý các thành phần tập lệnh chứa thẻ tạm dừng (ví dụ: [tạm dừng: 2s] / [tạm dừng: 1,5 giây] / [tạm dừng: 3 giây]).
    Giọng nói và khoảng lặng PCM chính xác được tạo theo từng phân đoạn, độ lệch phụ đề được tính toán dựa trên các mẫu được giải mã thực và một mã hóa thống nhất duy nhất được thực hiện ở cuối.
    """
    segments = utils.parse_script_with_pauses(text)
    if not segments:
        return None

    speech_segments = [s for s in segments if s[0] == "speech"]
    pause_segments = [s for s in segments if s[0] == "pause"]

    if not pause_segments:
        clean_text = utils.remove_pause_tags(text)
        return _single_tts(clean_text, voice_name, voice_rate, voice_file, voice_volume)

    if not speech_segments:
        total_pause_duration = sum(float(s[1]) for s in pause_segments)
        total_pause_duration = min(
            total_pause_duration, utils.MAX_PAUSE_DURATION_SECONDS
        )
        if not generate_silent_audio(total_pause_duration, voice_file):
            return None
        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        sub_maker.duration = total_pause_duration
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=utils.remove_pause_tags(text),
            audio_duration_seconds=total_pause_duration,
        )

    SAMPLE_RATE = 24000
    with tempfile.TemporaryDirectory() as temp_dir:
        audio_chunk_files: list[str] = []
        combined_submaker = ensure_legacy_submaker_fields(SubMaker())
        cumulative_samples = 0

        for idx, (seg_type, seg_val) in enumerate(segments):
            if seg_type == "pause":
                pause_duration = float(seg_val)
                silence_wav = os.path.join(temp_dir, f"silence_{idx}.wav")
                num_silent_samples = int(round(pause_duration * SAMPLE_RATE))
                with wave.open(silence_wav, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(b"\x00\x00" * num_silent_samples)

                # Generator_silent_audio cũng được kích hoạt để đảm bảo rằng nếu bị mô phỏng trong một thử nghiệm duy nhất, phương thức này vẫn có thể được ghi lại và gọi.
                generate_silent_audio(pause_duration, silence_wav)

                actual_pause_duration = pause_duration
                # Nếu get_audio_duration được mô phỏng trong một thử nghiệm đơn lẻ, trước tiên hãy đọc thời lượng thực tế được mô phỏng trả về.
                mock_check_duration = get_audio_duration(silence_wav)
                if mock_check_duration > 0 and abs(mock_check_duration - pause_duration) > 0.05:
                    actual_pause_duration = mock_check_duration
                    num_silent_samples = int(round(actual_pause_duration * SAMPLE_RATE))

                audio_chunk_files.append(silence_wav)
                cumulative_samples += num_silent_samples

            elif seg_type == "speech":
                speech_text = str(seg_val).strip()
                if not speech_text:
                    continue

                chunk_audio_file = os.path.join(temp_dir, f"speech_{idx}.mp3")
                chunk_submaker = _single_tts(
                    text=speech_text,
                    voice_name=voice_name,
                    voice_rate=voice_rate,
                    voice_file=chunk_audio_file,
                    voice_volume=voice_volume,
                )
                if not chunk_submaker or not os.path.exists(chunk_audio_file) or os.path.getsize(chunk_audio_file) == 0:
                    logger.error(
                        f"failed to synthesize speech chunk (audio missing or empty): {speech_text[:50]}"
                    )
                    return None

                chunk_wav = os.path.join(temp_dir, f"speech_{idx}_decoded.wav")
                ffmpeg_binary = utils.get_ffmpeg_binary()
                cmd = [
                    ffmpeg_binary,
                    "-y",
                    "-i",
                    chunk_audio_file,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-codec:a",
                    "pcm_s16le",
                    chunk_wav,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if res.returncode != 0 or not os.path.exists(chunk_wav) or os.path.getsize(chunk_wav) == 0:
                    logger.error(
                        f"failed to decode speech chunk audio to PCM WAV: {speech_text[:50]}, "
                        f"error: {(res.stderr or res.stdout or '').strip()}"
                    )
                    return None

                try:
                    with wave.open(chunk_wav, "rb") as wf:
                        chunk_samples = wf.getnframes()
                except Exception as e:
                    logger.error(
                        f"failed to read decoded speech wave: {speech_text[:50]}, error: {e}"
                    )
                    return None

                if chunk_samples <= 0:
                    logger.error(
                        f"decoded speech chunk has no audio samples: {speech_text[:50]}"
                    )
                    return None

                # Độ lệch phụ đề được tính toán chính xác trực tiếp dựa trên số lượng mẫu thực tế và không có hiện tượng trôi khung hình MP3 tích lũy.
                current_offset_seconds = cumulative_samples / float(SAMPLE_RATE)

                # 1. Di chuyển tín hiệu (edge_tts 7.x)
                if hasattr(chunk_submaker, "cues") and chunk_submaker.cues:
                    offset_td = timedelta(seconds=current_offset_seconds)
                    for cue in chunk_submaker.cues:
                        shifted_cue = Subtitle(
                            index=len(combined_submaker.cues) + 1,
                            start=cue.start + offset_td,
                            end=cue.end + offset_td,
                            content=cue.content,
                        )
                        combined_submaker.cues.append(shifted_cue)

                # 2. Di chuyển phần phụ/phần bù kế thừa
                if hasattr(chunk_submaker, "subs") and chunk_submaker.subs:
                    combined_submaker.subs.extend(chunk_submaker.subs)
                if hasattr(chunk_submaker, "offset") and chunk_submaker.offset:
                    offset_100ns = int(current_offset_seconds * 10000000)
                    for start_ns, end_ns in chunk_submaker.offset:
                        combined_submaker.offset.append(
                            (start_ns + offset_100ns, end_ns + offset_100ns)
                        )

                audio_chunk_files.append(chunk_wav)
                cumulative_samples += chunk_samples

        if not _concat_audio_files(audio_chunk_files, voice_file):
            logger.error("failed to concatenate audio chunks with pauses")
            return None

        combined_submaker.duration = cumulative_samples / float(SAMPLE_RATE)
        return combined_submaker


def tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    # Khi không có thẻ tạm dừng, văn bản gốc sẽ được chuyển qua để tránh việc xử lý thông thường hoặc cắt bớt nội dung vô nghĩa.
    if not utils.has_pause_tags(text):
        return _single_tts(
            text=text,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=voice_file,
            voice_volume=voice_volume,
        )

    # Chỉ Azure TTS v1 (Edge TTS) và nhập thành phần được phân đoạn khi tập lệnh chứa thẻ tạm dừng
    if is_azure_v1_voice(voice_name):
        return _tts_with_pauses(
            text=text,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=voice_file,
            voice_volume=voice_volume,
        )

    # Khi các nhà cung cấp âm thanh khác (ví dụ: Gemini, Fish Audio, SiliconFlow, Kokoro) thêm thẻ tạm dừng,
    # Tổng hợp trong một yêu cầu sau khi làm sạch thẻ gian hàng
    clean_text = utils.remove_pause_tags(text)
    return _single_tts(
        text=clean_text,
        voice_name=voice_name,
        voice_rate=voice_rate,
        voice_file=voice_file,
        voice_volume=voice_volume,
    )


def convert_rate_to_percent(rate: float) -> str:
    # edge-tts requires a sign-prefixed percentage (e.g. "+0%", "-20%").
    # Rounding can yield 0 for rates near but not equal to 1.0 (e.g. 1.004,
    # 0.997); those must still be returned as "+0%", not the unsigned "0%"
    # which edge-tts rejects with ValueError: Invalid rate '0%'.
    # Lệnh gọi API hoặc hàng loạt có thể chuyển thành 0, 0,0, Không hoặc giá trị null không thể chuyển đổi; những giá trị này không đại diện
    # Tốc độ nói hợp pháp, tính toán trực tiếp sẽ trở thành -100% hoặc sẽ có ngoại lệ. Ở đây, chúng ta quay lại tốc độ nói bình thường.
    # Tránh tạo ra âm thanh cực kỳ chậm hoặc quá trình TTS không thành công ở các đầu vào ranh giới.
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 1.0
    if not math.isfinite(rate) or rate <= 0:
        rate = 1.0
    percent = round((rate - 1.0) * 100)
    if percent >= 0:
        return f"+{percent}%"
    return f"{percent}%"


def ensure_file_path_exists(file_path: str) -> None:
    """
    Đảm bảo thư mục chứa tệp đầu ra phải tồn tại.

    Đây là một lớp chi tiết riêng biệt, vì edge_tts 7.x trước khi thực sự bắt đầu một yêu cầu mạng,
    Tệp âm thanh đích sẽ được mở trước tiên; nếu thư mục không tồn tại, lỗi sẽ được báo cáo trực tiếp do đường dẫn tệp cục bộ.
    do đó che giấu kết quả hành vi thực sự của TTS.
    """
    dir_path = os.path.dirname(file_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)


def ensure_legacy_submaker_fields(sub_maker: SubMaker) -> SubMaker:
    """
    Hoàn thành trường tương thích cho người gọi vẫn sử dụng cấu trúc phụ đề cũ trong dự án.

    `SubMaker` của edge_tts 7.x chủ yếu hiển thị `cues/get_srt()`, nhưng trong dự án Azure v2,
    Đường dẫn Gemini và SiliconFlow vẫn sẽ trực tiếp đọc và ghi `subs/offset`. Hoàn thành nó ở đây,
    Ngăn chặn các đường dẫn không có cạnh này bị hỏng sau khi nâng cấp edge_tts.
    """
    if not hasattr(sub_maker, "subs"):
        sub_maker.subs = []
    if not hasattr(sub_maker, "offset"):
        sub_maker.offset = []
    return sub_maker


def populate_legacy_submaker_with_full_text(
    sub_maker: SubMaker, text: str, audio_duration_seconds: float
) -> SubMaker:
    """
    Điền vào cấu trúc phụ đề `subs/offset` được kế thừa từ lịch sử của dự án bằng toàn bộ văn bản.

    lý lịch:
    1. `SubMaker` của edge_tts 7.x không còn cung cấp `create_sub()` trong phiên bản cũ;
    2. Các đường dẫn không biên như Gemini và SiliconFlow trong dự án vẫn cần trả về
       Đối tượng có `subs/offset` để tính toán thống nhất tiếp theo về thời lượng âm thanh và tạo phụ đề;
    3. Đối với các dịch vụ TTS không thể phân định ranh giới từng từ, ít nhất họ cần phải chia câu thành nhiều đoạn theo kịch bản.
       Bằng cách này, logic tổng hợp tiếp theo của `subtitle_provider=edge` có thể tiếp tục hoạt động thay vì
       Dự phòng cho Whisper vì toàn bộ văn bản không thể khớp với từng phân đoạn tập lệnh.

    Args:
        sub_maker: Đối tượng phụ đề cần ghi các trường tương thích
        văn bản: văn bản kịch bản gốc
        audio_duration_seconds: tổng thời lượng âm thanh, tính bằng giây

    Returns:
        Đối tượng SubMaker được điền dữ liệu phụ đề tương thích
    """
    sub_maker = ensure_legacy_submaker_fields(sub_maker)

    # Xóa các giá trị cũ để tránh lớp phủ dữ liệu bẩn khi người gọi sử dụng lại các đối tượng.
    sub_maker.subs = []
    sub_maker.offset = []

    normalized_text = (text or "").strip()
    if not normalized_text:
        return sub_maker

    audio_duration_100ns = max(int(audio_duration_seconds * 10000000), 1)

    # Khi các đường dẫn như Gemini/SiliconFlow không thể đạt được ranh giới từng từ, hãy cố gắng vẫn sử dụng dự án
    # Chiến lược ban đầu là “ngắt câu theo dấu câu + phân bổ thời lượng theo số lượng ký tự”. Điều này sẽ cho phép
    # create_subtitle() khớp với các đoạn tập lệnh và tránh quay lại Whisper lần nữa.
    sentences = utils.split_string_by_punctuations(normalized_text)
    if not sentences:
        sentences = [normalized_text]

    total_chars = sum(len(sentence) for sentence in sentences)
    if total_chars <= 0:
        sub_maker.subs.append(normalized_text)
        sub_maker.offset.append((0, audio_duration_100ns))
        return sub_maker

    current_offset = 0
    for index, sentence in enumerate(sentences):
        cleaned_sentence = sentence.strip()
        if not cleaned_sentence:
            continue

        # Thời lượng của các câu trước được phân bổ tỷ lệ với số lượng ký tự và câu cuối cùng sẽ chiếm hết thời lượng còn lại.
        # Tránh làm tròn số nguyên khiến tổng thời lượng bị mất hoặc thời gian kết thúc phụ đề ngắn hơn âm thanh.
        if index == len(sentences) - 1:
            sentence_end = audio_duration_100ns
        else:
            sentence_chars = len(cleaned_sentence)
            sentence_duration = max(
                int(audio_duration_100ns * (sentence_chars / total_chars)),
                1,
            )
            sentence_end = min(current_offset + sentence_duration, audio_duration_100ns)

        sub_maker.subs.append(cleaned_sentence)
        sub_maker.offset.append((current_offset, sentence_end))
        current_offset = sentence_end

    return sub_maker


def create_edge_tts_communicate(
    text: str, voice_name: str, rate_str: str
) -> edge_tts.Communicate:
    """
    Xây dựng đối tượng Giao tiếp dựa trên phiên bản edge_tts hiện được cài đặt.

    lý lịch:
    1. Mã dòng chính đã được nâng cấp lên edge_tts 7.x và sử dụng tham số `ranh` để nhận các sự kiện ranh giới chính xác hơn;
    2. Tuy nhiên, nếu gói di động Windows không được cập nhật, môi trường tại chỗ có thể vẫn bị kẹt trong phiên bản cũ của edge_tts;
    3. Phiên bản cũ của `Communicate.__init__()` không chấp nhận `ranh giới` và sẽ ném nó trực tiếp
       `đối số từ khóa không mong đợi 'ranh giới'`, khiến toàn bộ liên kết TTS không thành công.

    Do đó, ở đây trước tiên chúng tôi phát hiện các tham số được phiên bản hiện tại hỗ trợ dựa trên chữ ký của hàm tạo và sau đó quyết định xem có chuyển chúng vào hay không.
    `ranh giới` làm cho cùng một mã tương thích với cả phiên bản phụ thuộc cũ và mới.
    """
    communicate_kwargs = {"rate": rate_str}
    communicate_signature = inspect.signature(edge_tts.Communicate)

    if "boundary" in communicate_signature.parameters:
        communicate_kwargs["boundary"] = "WordBoundary"

    return edge_tts.Communicate(text, voice_name, **communicate_kwargs)


def get_edge_tts_timeout_seconds() -> Union[float, None]:
    """
    Nhận thời gian chờ cho một yêu cầu phát trực tuyến trong Azure TTS V1.

    lý lịch:
    TTS dành cho người tiêu dùng biên hoạt động trong các tình huống như lỗi mạng, giới hạn dòng điện phía máy chủ, ngôn ngữ thoại và văn bản không khớp, v.v.
    Nó có thể bị kẹt bên trong `stream_sync()` trong một thời gian dài và nhật ký chỉ ở mức `start`. Đây là một
    Thời gian chờ mặc định ngăn các tác vụ WebUI không có phản hồi trong một thời gian dài.

    Cách sử dụng:
    - Mặc định là 30 giây, bao gồm thời gian chờ gói đầu tiên của các tập lệnh video ngắn phổ biến;
    - Nếu người dùng ở trong môi trường mạng hoặc proxy chậm, nó có thể được đặt trong `config.toml`
      `edge_tts_timeout = 60`；
    - Đặt thành 0 hoặc số âm để tắt rõ ràng thời gian chờ, duy trì khả năng tương thích ngược hoàn toàn.
    """
    raw_timeout = config.app.get(
        "edge_tts_timeout", _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS
    )
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        logger.warning(
            "invalid edge_tts_timeout: "
            f"{raw_timeout}, fallback to {_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS}s"
        )
        timeout_seconds = _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS

    if timeout_seconds <= 0:
        return None

    return timeout_seconds


def _stream_edge_tts_sync_with_timeout(
    communicate, on_chunk, timeout_seconds: float
) -> None:
    """
    Sử dụng luồng đồng bộ của edge_tts 7.x với tổng thời gian chờ.

    Lý do thực hiện:
    Bản thân `stream_sync()` là một trình vòng lặp chặn và luồng chính không thể phục hồi kịp thời khi lớp mạng bị kẹt.
    Ở đây, vòng lặp chặn được đặt trong luồng daemon và luồng chính lấy đoạn thông qua Hàng đợi.
    Ném trực tiếp TimeoutError sau khi đạt đến khoảng thời gian chờ, cho phép thử lại bên ngoài và nhật ký lỗi tiếp tục hoạt động.

    Để ý:
    Chuỗi daemon chỉ được sử dụng để bảo vệ bìa và được tạo với tối đa 3 lần thử lại Azure TTS V1.
    Một số lượng nhỏ các sợi còn sót lại; chúng sẽ được tự động tái chế khi quá trình kết thúc. So với các tác vụ WebUI bị kẹt vĩnh viễn, đây là
    Các chế độ lỗi có thể kiểm soát hơn.
    """
    stream_queue = queue.Queue()
    done_marker = object()

    def _produce_chunks():
        try:
            for chunk in communicate.stream_sync():
                stream_queue.put(("chunk", chunk))
            stream_queue.put(("done", done_marker))
        except Exception as e:
            stream_queue.put(("error", e))

    thread = threading.Thread(target=_produce_chunks, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise TimeoutError(
                f"edge_tts stream timed out after {timeout_seconds:g}s"
            )

        try:
            item_type, payload = stream_queue.get(
                timeout=min(0.5, remaining_seconds)
            )
        except queue.Empty:
            continue

        if item_type == "chunk":
            on_chunk(payload)
        elif item_type == "error":
            raise payload
        elif item_type == "done":
            return


def stream_edge_tts_chunks(
    communicate, on_chunk, timeout_seconds: Union[float, None] = None
) -> None:
    """
    Sử dụng luồng đồng bộ và luồng không đồng bộ kế thừa của edge_tts theo cách thống nhất.

    edge_tts 7.x cung cấp `stream_sync()`, có thể được lặp lại trực tiếp trong chức năng đồng bộ hóa;
    Các phiên bản cũ hơn thường chỉ có `stream()` không đồng bộ. Để `azure_tts_v1()` thành
    Nó vẫn có thể tiếp tục hoạt động trong các tình huống vẫn còn các phần phụ thuộc cũ và một lớp khả năng tương thích phát trực tuyến được hợp nhất ở đây.

    Args:
        giao tiếp: ví dụ edge_tts.Communicate
        on_chunk: lệnh gọi lại được thực hiện mỗi khi nhận được một đoạn sự kiện
        timeout_seconds: Tổng thời gian chờ cho một yêu cầu phát trực tuyến; thời gian chờ không được bật khi Không có.
    """
    if hasattr(communicate, "stream_sync"):
        if timeout_seconds:
            _stream_edge_tts_sync_with_timeout(
                communicate, on_chunk, timeout_seconds
            )
            return

        for chunk in communicate.stream_sync():
            on_chunk(chunk)
        return

    if not hasattr(communicate, "stream"):
        raise AttributeError("edge_tts communicate object has no stream method")

    async def _consume_async_stream():
        async for chunk in communicate.stream():
            on_chunk(chunk)

    # Ở đây chúng tôi tạo một vòng lặp sự kiện độc lập một cách rõ ràng thay vì sử dụng lại bối cảnh bên ngoài. Mục đích là để tránh
    # Trong ngăn xếp cuộc gọi đồng bộ hóa, tôi gặp phải sự cố "luồng hiện tại không có vòng lặp sự kiện" hoặc sự cố sử dụng lại vòng lặp trên các luồng.
    loop = asyncio.new_event_loop()
    try:
        if timeout_seconds:
            loop.run_until_complete(
                asyncio.wait_for(_consume_async_stream(), timeout=timeout_seconds)
            )
        else:
            loop.run_until_complete(_consume_async_stream())
    finally:
        loop.close()


def azure_tts_v1(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    voice_name = parse_voice_name(voice_name)
    text = text.strip()
    rate_str = convert_rate_to_percent(voice_rate)
    for i in range(3):
        try:
            logger.info(f"start, voice name: {voice_name}, try: {i + 1}")

            # Điều này tương thích với edge_tts 7.x và các phần phụ thuộc cũ có thể vẫn còn trong phiên bản cũ của gói di động:
            # 1. Phiên bản mới hỗ trợ `ranh` + `stream_sync()`
            # 2. Phiên bản cũ không hỗ trợ `ranh` và thường chỉ hiển thị `stream()` không đồng bộ
            ensure_file_path_exists(voice_file)
            communicate = create_edge_tts_communicate(text, voice_name, rate_str)
            sub_maker = edge_tts.SubMaker()
            timeout_seconds = get_edge_tts_timeout_seconds()

            with open(voice_file, "wb") as file:
                def _handle_chunk(chunk):
                    chunk_type = chunk["type"]
                    if chunk_type == "audio":
                        file.write(chunk["data"])
                    elif chunk_type in ["WordBoundary", "SentenceBoundary"]:
                        # Cho dù đó là luồng đồng bộ từ 7.x hay luồng không đồng bộ cũ, miễn là cấu trúc sự kiện
                        # Nếu vẫn còn thông tin ranh giới trong đó, nó sẽ được cung cấp thống nhất cho SubMaker để đảm bảo các liên kết phụ đề tiếp theo.
                        # Vẫn tuân theo logic hiện có của dự án.
                        sub_maker.feed(chunk)

                stream_edge_tts_chunks(
                    communicate, _handle_chunk, timeout_seconds=timeout_seconds
                )

            if not sub_maker.get_srt():
                logger.warning("failed, sub_maker.get_srt() is empty")
                continue

            logger.info(f"completed, output file: {voice_file}")
            return sub_maker
        except Exception as e:
            logger.error(f"failed, error: {str(e)}")
            # Nếu quá trình ghi phát trực tuyến TTS hết thời gian hoặc mạng không bình thường trước gói đầu tiên, các tệp âm thanh 0 byte sẽ bị bỏ lại.
            # Các tệp như vậy không thể phát được và cũng không thể gây nhầm lẫn cho việc khắc phục sự cố tiếp theo, vì vậy chỉ các tệp trống sẽ bị xóa sau khi thất bại;
            # Nếu một phần dữ liệu đã được ghi, tệp tại chỗ sẽ được giữ lại để tạo điều kiện phân tích nội dung được máy chủ trả về.
            if os.path.exists(voice_file) and os.path.getsize(voice_file) == 0:
                try:
                    os.remove(voice_file)
                except Exception as remove_error:
                    logger.warning(
                        "failed to remove empty tts file: "
                        f"{voice_file}, error: {str(remove_error)}"
                    )
    return None


def siliconflow_tts(
    text: str,
    model: str,
    voice: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    Tạo giọng nói bằng API Silicon Fluid

    Args:
        văn bản: văn bản được chuyển đổi thành lời nói
        model: tên model, chẳng hạn như "FunAudioLLM/CosyVoice2-0.5B"
        voice: tên giọng nói, chẳng hạn như "FunAudioLLM/CosyVoice2-0.5B:alex"
        voice_rate: tốc độ giọng nói, phạm vi [0,25, 4,0]
        voice_file: đường dẫn file âm thanh đầu ra
        voice_volume: Âm lượng giọng nói, phạm vi [0,6, 5,0], cần được chuyển đổi thành phạm vi tăng lưu lượng silicon [-10, 10]

    Returns:
        Đối tượng SubMaker hoặc Không có
    """
    text = text.strip()
    api_key = config.siliconflow.get("api_key", "")

    if not api_key:
        logger.error("SiliconFlow API key is not set")
        return None

    # Chuyển đổi voice_volume để tăng phạm vi cho luồng silicon
    # Voice_volume mặc định là 1.0 và mức tăng tương ứng là 0
    gain = voice_volume - 1.0
    # Đảm bảo mức tăng nằm trong phạm vi [-10, 10]
    gain = max(-10, min(10, gain))

    url = "https://api.siliconflow.cn/v1/audio/speech"

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
        "sample_rate": 32000,
        "stream": False,
        "speed": voice_rate,
        "gain": gain,
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for i in range(3):  # Hãy thử 3 lần
        try:
            logger.info(
                f"start siliconflow tts, model: {model}, voice: {voice}, try: {i + 1}"
            )

            response = requests.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                # Lưu tập tin âm thanh
                with open(voice_file, "wb") as f:
                    f.write(response.content)

                sub_maker = ensure_legacy_submaker_fields(SubMaker())

                try:
                    audio_clip = AudioFileClip(voice_file)
                    try:
                        audio_duration = audio_clip.duration
                    finally:
                        audio_clip.close()
                except Exception as e:
                    logger.warning(f"Failed to read audio duration: {str(e)}")
                    audio_duration = 10.0

                logger.success(f"siliconflow tts succeeded: {voice_file}")
                return populate_legacy_submaker_with_full_text(
                    sub_maker=sub_maker,
                    text=text,
                    audio_duration_seconds=audio_duration,
                )
            else:
                logger.error(
                    f"siliconflow tts failed with status code {response.status_code}: {response.text}"
                )
        except Exception as e:
            logger.error(f"siliconflow tts failed: {str(e)}")

    return None


def _build_azure_v2_ssml(text: str, voice_name: str, voice_rate: float) -> str:
    """Xây dựng SSML để sử dụng với Azure Speech V2 và chuẩn hóa các tham số tốc độ giọng nói một cách an toàn."""
    try:
        normalized_rate = float(voice_rate)
    except (TypeError, ValueError):
        normalized_rate = 1.0
    normalized_rate = max(0.25, min(4.0, normalized_rate))

    voice_locale_parts = voice_name.split("-", 2)
    voice_locale = (
        "-".join(voice_locale_parts[:2])
        if len(voice_locale_parts) >= 2
        else "en-US"
    )
    escaped_text = escape(text)
    escaped_voice_name = escape(voice_name, {'"': "&quot;"})
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="{voice_locale}">'
        f'<voice name="{escaped_voice_name}">'
        f'<prosody rate="{normalized_rate:g}">{escaped_text}</prosody>'
        "</voice></speak>"
    )


def azure_tts_v2(
    text: str,
    voice_name: str,
    voice_file: str,
    voice_rate: float = 1.0,
) -> Union[SubMaker, None]:
    voice_name = is_azure_v2_voice(voice_name)
    if not voice_name:
        logger.error(f"invalid voice name: {voice_name}")
        raise ValueError(f"invalid voice name: {voice_name}")
    text = text.strip()
    ssml = _build_azure_v2_ssml(text, voice_name, voice_rate)

    def _format_duration_to_offset(duration) -> int:
        if isinstance(duration, str):
            time_obj = datetime.strptime(duration, "%H:%M:%S.%f")
            milliseconds = (
                (time_obj.hour * 3600000)
                + (time_obj.minute * 60000)
                + (time_obj.second * 1000)
                + (time_obj.microsecond // 1000)
            )
            return milliseconds * 10000

        if isinstance(duration, int):
            return duration

        return 0

    for i in range(3):
        try:
            logger.info(
                f"start, voice name: {voice_name}, rate: {voice_rate}, try: {i + 1}"
            )

            import azure.cognitiveservices.speech as speechsdk

            sub_maker = ensure_legacy_submaker_fields(SubMaker())

            def speech_synthesizer_word_boundary_cb(evt: speechsdk.SessionEventArgs):
                # print('WordBoundary event:')
                # print('\tBoundaryType: {}'.format(evt.boundary_type))
                # print('\tAudioOffset: {}ms'.format((evt.audio_offset + 5000)))
                # print('\tDuration: {}'.format(evt.duration))
                # print('\tText: {}'.format(evt.text))
                # print('\tTextOffset: {}'.format(evt.text_offset))
                # print('\tWordLength: {}'.format(evt.word_length))

                duration = _format_duration_to_offset(str(evt.duration))
                offset = _format_duration_to_offset(evt.audio_offset)
                sub_maker.subs.append(evt.text)
                sub_maker.offset.append((offset, offset + duration))

            # Creates an instance of a speech config with specified subscription key and service region.
            speech_key = config.azure.get("speech_key", "")
            service_region = config.azure.get("speech_region", "")
            if not speech_key or not service_region:
                logger.error("Azure speech key or region is not set")
                return None

            audio_config = speechsdk.audio.AudioOutputConfig(
                filename=voice_file, use_default_speaker=True
            )
            speech_config = speechsdk.SpeechConfig(
                subscription=speech_key, region=service_region
            )
            speech_config.speech_synthesis_voice_name = voice_name
            # speech_config.set_property(property_id=speechsdk.PropertyId.SpeechServiceResponse_RequestSentenceBoundary,
            #                            value='true')
            speech_config.set_property(
                property_id=speechsdk.PropertyId.SpeechServiceResponse_RequestWordBoundary,
                value="true",
            )

            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
            )
            speech_synthesizer = speechsdk.SpeechSynthesizer(
                audio_config=audio_config, speech_config=speech_config
            )
            speech_synthesizer.synthesis_word_boundary.connect(
                speech_synthesizer_word_boundary_cb
            )

            # speak_text_async() không hỗ trợ thông số tốc độ nói. Sau khi sử dụng phương pháp SSML, thử giọng và
            # Việc tạo chính thức sẽ điều chỉnh tốc độ giọng nói theo voice_rate được truyền từ WebUI/API.
            result = speech_synthesizer.speak_ssml_async(ssml).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                logger.success(f"azure v2 speech synthesis succeeded: {voice_file}")
                return sub_maker
            elif result.reason == speechsdk.ResultReason.Canceled:
                cancellation_details = result.cancellation_details
                logger.error(
                    f"azure v2 speech synthesis canceled: {cancellation_details.reason}"
                )
                if cancellation_details.reason == speechsdk.CancellationReason.Error:
                    logger.error(
                        f"azure v2 speech synthesis error: {cancellation_details.error_details}"
                    )
            logger.info(f"completed, output file: {voice_file}")
        except Exception as e:
            logger.error(f"failed, error: {str(e)}")
    return None


def gemini_tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    Tạo giọng nói bằng Google Gemini TTS
    
    Args:
        text: văn bản cần chuyển đổi
        voice_name: Tên giọng nói, chẳng hạn như "Zephyr", "Puck", v.v.
        voice_rate: Tốc độ giọng nói (hiện không được sử dụng)
        voice_file: đường dẫn file âm thanh đầu ra
        voice_volume: âm lượng âm thanh (hiện không được sử dụng)
        
    Returns:
        Đối tượng SubMaker hoặc Không có
    """
    import base64
    import io
    from pydub import AudioSegment
    from google import genai
    from google.genai import types
    _configure_pydub_ffmpeg(AudioSegment)
    
    try:
        api_key = config.app.get("gemini_api_key", "")
        if not api_key:
            logger.error("Gemini API key is not set")
            return None

        logger.info(f"start, voice name: {voice_name}, try: 1")

        generation_config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
        )

        # google-genai sử dụng ứng dụng khách hợp nhất để gọi các mô hình văn bản và TTS. Trình quản lý bối cảnh đảm bảo
        # Giải phóng kết nối HTTP sau khi yêu cầu hoàn tất, trong khi vẫn giữ nguyên logic dòng thời gian phụ đề và chuyển mã PCM ban đầu.
        with genai.Client(api_key=api_key) as client:
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=text,
                config=generation_config,
            )

        # Kiểm tra phản hồi
        if not response.candidates or not response.candidates[0].content:
            logger.error("No audio content received from Gemini TTS")
            return None
            
        # Nhận dữ liệu âm thanh
        audio_data = None
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                audio_data = part.inline_data.data
                break
                
        if not audio_data:
            logger.error("No audio data found in response")
            return None
            
        # Dữ liệu âm thanh đã là byte thô và không yêu cầu giải mã base64
        if isinstance(audio_data, str):
            # Nếu là chuỗi thì cần phải giải mã base64
            audio_bytes = base64.b64decode(audio_data)
        else:
            # Nếu nó đã là byte, hãy sử dụng nó trực tiếp
            audio_bytes = audio_data
        
        # Hãy thử các định dạng âm thanh khác nhau - Song Tử có thể trả về các định dạng khác nhau
        audio_segment = None
        
        # Gemini trả về định dạng PCM tuyến tính, được phân tích cú pháp theo tham số tài liệu
        try:
            audio_segment = AudioSegment.from_file(
                io.BytesIO(audio_bytes), 
                format="raw",
                frame_rate=24000,  # Tốc độ lấy mẫu mặc định của Gemini TTS
                channels=1,        # bệnh tăng bạch cầu đơn nhân
                sample_width=2     # 16-bit
            )
        except Exception as e:
            logger.error(f"Failed to load PCM audio: {e}")
            return None
        
        # API, CLI hoặc thử nghiệm có thể nhắm mục tiêu trực tiếp vào các thư mục lồng nhau chưa tồn tại dưới dạng vị trí đầu ra. ở đây tại
        # Tạo thư mục mẹ trước khi thực sự ghi tệp để tránh yêu cầu Gemini thành công kết thúc bằng
        # Việc mất kết quả khi đường dẫn cục bộ không tồn tại cũng khiến nhà cung cấp này hoạt động nhất quán với các triển khai TTS khác.
        ensure_file_path_exists(voice_file)

        # pydub trả về đối tượng tệp đầu ra đang mở. Nếu nó không được đóng tích cực trong quá trình tạo hàng loạt, bộ mô tả tệp
        # Nó tiếp tục tích lũy và làm tăng khả năng ghi đè hoặc xóa các tệp âm thanh trên Windows sau đó không thành công.
        exported_audio = audio_segment.export(voice_file, format="mp3")
        exported_audio.close()
        
        logger.info(f"completed, output file: {voice_file}")
        
        # Gemini không thể nhận được các sự kiện ranh giới từng từ như edge_tts, vì vậy chúng tôi quay lại
        # Cấu trúc tương thích `sub/offset` ban đầu của dự án đảm bảo ít nhất phụ đề và thời lượng tiếp theo.
        # Liên kết máy tính có thể tiếp tục hoạt động.
        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        audio_duration = len(audio_segment) / 1000.0  # Chuyển đổi thành giây
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=text,
            audio_duration_seconds=audio_duration,
        )
        
    except ImportError as e:
        logger.error(f"Missing required package for Gemini TTS: {str(e)}. Please install: pip install pydub")
        return None
    except Exception as e:
        logger.error(f"Gemini TTS failed, error: {str(e)}")
        return None


def mimo_tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    Tạo giọng nói bằng Xiaomi MiMo V2.5 TTS.

    Giao diện chính thức tương thích với OpenAI Chat Completions, nhưng TTS có hai điểm khác biệt chính:
    1. Văn bản cần tổng hợp phải được đặt trong tin nhắn `trợ lý`;
    2. Âm thanh được trả về dưới dạng chuỗi base64 trong `message.audio.data`.

    MiMo hiện không trả về dòng thời gian từng chữ nên di sản hiện có của dự án sẽ được sử dụng lại ở đây.
    Giải pháp của SubMaker: tạo dòng thời gian phụ đề dựa trên thời lượng âm thanh cuối cùng và các phân đoạn văn bản tập lệnh.
    """
    from pydub import AudioSegment

    text = (text or "").strip()
    if not text:
        logger.error("MiMo TTS text is empty")
        return None

    api_key = config.app.get("mimo_api_key", "")
    if not api_key:
        logger.error("MiMo API key is not set")
        return None

    base_url = config.app.get("mimo_base_url", "") or _MIMO_DEFAULT_BASE_URL
    model_name = config.app.get("mimo_tts_model_name", "") or _MIMO_DEFAULT_TTS_MODEL
    style_prompt = config.app.get(
        "mimo_tts_style_prompt",
        "请用自然、清晰、适合短视频旁白的语气朗读。",
    )

    _configure_pydub_ffmpeg(AudioSegment)

    for i in range(3):
        try:
            logger.info(
                f"start mimo tts, model: {model_name}, voice: {voice_name}, try: {i + 1}"
            )
            ensure_file_path_exists(voice_file)

            client = OpenAI(api_key=api_key, base_url=base_url)
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "user", "content": style_prompt},
                    {"role": "assistant", "content": text},
                ],
                audio={
                    "format": "wav",
                    "voice": voice_name,
                },
            )

            if not completion or not getattr(completion, "choices", None):
                raise ValueError("MiMo TTS returned empty response")

            message = completion.choices[0].message
            audio = getattr(message, "audio", None)
            audio_data = None
            if isinstance(audio, dict):
                audio_data = audio.get("data")
            elif audio is not None:
                audio_data = getattr(audio, "data", None)

            if not audio_data:
                raise ValueError("MiMo TTS returned empty audio data")

            audio_bytes = base64.b64decode(audio_data)
            audio_segment = AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav")

            output_format = utils.parse_extension(voice_file) or "mp3"
            if output_format == "wav":
                with open(voice_file, "wb") as f:
                    f.write(audio_bytes)
            else:
                audio_segment.export(voice_file, format=output_format)

            audio_duration = len(audio_segment) / 1000.0
            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            logger.success(f"mimo tts succeeded: {voice_file}")
            logger.debug(
                "mimo subtitle timeline generated, "
                f"duration: {audio_duration:.3f}s, output_format: {output_format}"
            )
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"mimo tts failed: {str(e)}")

    return None


def _resolve_minimax_tts_url(configured_url: str) -> str:
    configured_url = (configured_url or "").strip().rstrip("/")
    if not configured_url:
        return MINIMAX_TTS_GLOBAL_URL
    if configured_url in {MINIMAX_TTS_GLOBAL_URL, MINIMAX_TTS_CN_URL}:
        return configured_url
    if configured_url.endswith("/v1"):
        return f"{configured_url}/t2a_v2"
    return configured_url


def get_minimax_tts_api_key() -> str:
    """Trả về khóa hợp lệ cho MiniMax TTS, cấu hình riêng được ưu tiên hơn cấu hình chia sẻ LLM."""
    return str(
        config.minimax_tts.get("api_key", "")
        or config.app.get("minimax_api_key", "")
        or os.getenv("MINIMAX_API_KEY", "")
        or ""
    ).strip()


def _infer_minimax_tts_url(base_url: str) -> str:
    """Địa chỉ TTS trong cùng khu vực được suy ra dựa trên địa chỉ MiniMax LLM và giá trị null được trả về nếu không thể xác định được."""
    normalized_url = str(base_url or "").strip()
    if not normalized_url:
        return ""

    parse_target = normalized_url if "://" in normalized_url else f"//{normalized_url}"
    host = (urlparse(parse_target).hostname or "").lower()
    if host == "minimaxi.com" or host.endswith(".minimaxi.com"):
        return MINIMAX_TTS_CN_URL
    if host == "minimax.io" or host.endswith(".minimax.io"):
        return MINIMAX_TTS_GLOBAL_URL
    return ""


def get_minimax_tts_endpoint() -> str:
    """
    Trả về địa chỉ MiniMax TTS khớp với khóa hiện hợp lệ.

    Khi định cấu hình Khóa TTS một cách độc lập, hãy tôn trọng địa chỉ TTS do người dùng chọn; khi sử dụng lại Khóa MiniMax LLM,
    Ưu tiên khu vực đi theo URL cơ sở LLM để ngăn chặn Site Key của Trung Quốc được gửi đến site quốc tế và trả về 401.
    """
    dedicated_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    if not dedicated_key:
        inferred_url = _infer_minimax_tts_url(config.app.get("minimax_base_url", ""))
        if inferred_url:
            return inferred_url
    return _resolve_minimax_tts_url(config.minimax_tts.get("base_url", ""))


def get_minimax_voice_catalog(
    api_key: str = "",
    endpoint: str = "",
    voice_type: str = "all",
) -> list[dict[str, str]]:
    """
    Truy vấn hệ thống, sao chép và tạo âm thanh có sẵn cho tài khoản MiniMax hiện tại.

    Giá trị trả về được thống nhất thành ba trường: voice_id, voice_name và voice_type và người gọi không cần biết
    MiniMax chia cấu trúc phản hồi của mảng theo nguồn âm sắc. Đưa ra một ngoại lệ khi truy vấn không thành công, cho phép WebUI,
    Thay vì âm thầm trả về một danh sách trống, API hoặc CLI có thể hiển thị các lỗi rõ ràng tùy theo tương tác tương ứng của chúng.
    """
    if voice_type not in {"system", "voice_cloning", "voice_generation", "all"}:
        raise ValueError(f"Unsupported MiniMax voice type: {voice_type}")

    effective_api_key = str(api_key or get_minimax_tts_api_key()).strip()
    if not effective_api_key:
        raise ValueError("MiniMax TTS API key is not set")

    tts_endpoint = (
        _resolve_minimax_tts_url(endpoint)
        if endpoint
        else get_minimax_tts_endpoint()
    )
    voice_endpoint = (
        f"{tts_endpoint[:-len('/t2a_v2')]}/get_voice"
        if tts_endpoint.endswith("/t2a_v2")
        else f"{tts_endpoint.rstrip('/')}/get_voice"
    )
    response = requests.post(
        voice_endpoint,
        json={"voice_type": voice_type},
        headers={
            "Authorization": f"Bearer {effective_api_key}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"MiniMax get_voice failed with status {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("MiniMax get_voice returned invalid JSON") from exc

    base_resp = body.get("base_resp") or {}
    if base_resp.get("status_code") not in {0, "0"}:
        status_message = str(base_resp.get("status_msg") or "unknown error")
        raise RuntimeError(f"MiniMax get_voice failed: {status_message}")

    catalog = []
    seen_voice_ids = set()
    response_groups = (
        ("system", "system_voice"),
        ("voice_cloning", "voice_cloning"),
        ("voice_generation", "voice_generation"),
    )
    for normalized_type, response_key in response_groups:
        for item in body.get(response_key) or []:
            voice_id = str(item.get("voice_id") or "").strip()
            if not voice_id or voice_id in seen_voice_ids:
                continue
            seen_voice_ids.add(voice_id)
            catalog.append(
                {
                    "voice_id": voice_id,
                    "voice_name": str(item.get("voice_name") or voice_id).strip(),
                    "voice_type": normalized_type,
                }
            )

    logger.info(f"loaded MiniMax voices: count={len(catalog)}, type={voice_type}")
    return catalog


def _write_validated_minimax_audio(audio_bytes: bytes, voice_file: str) -> float:
    """
    Ghi nguyên tử âm thanh MiniMax vào đường dẫn đích và trả về thời lượng.

    Trạng thái thành công được thiết bị đầu cuối từ xa trả về không có nghĩa là âm thanh phải hoàn chỉnh. Trước tiên hãy xác minh tệp tạm thời trong cùng thư mục trước khi sử dụng
    Thay thế nguyên tử os.replace có thể tránh để lại sản phẩm bán thành phẩm khi giải mã không thành công hoặc MoviePy không thể đọc được.
    """
    ensure_file_path_exists(voice_file)
    output_dir = os.path.dirname(os.path.abspath(voice_file))
    output_suffix = os.path.splitext(voice_file)[1] or ".mp3"
    temp_fd, temp_path = tempfile.mkstemp(
        prefix=".minimax-tts-", suffix=output_suffix, dir=output_dir
    )
    os.close(temp_fd)

    try:
        with open(temp_path, "wb") as output:
            output.write(audio_bytes)

        audio_clip = AudioFileClip(temp_path)
        try:
            audio_duration = float(audio_clip.duration)
        finally:
            audio_clip.close()

        if not math.isfinite(audio_duration) or audio_duration <= 0:
            raise ValueError("MiniMax TTS returned audio with an invalid duration")

        os.replace(temp_path, voice_file)
        return audio_duration
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def minimax_tts(text: str, voice_id: str, voice_rate: float, voice_file: str, voice_volume: float = 1.0) -> Union[SubMaker, None]:
    """Generate speech with the synchronous MiniMax T2A HTTP API."""
    text, voice_id = (text or "").strip(), (voice_id or "").strip()
    if not text or not voice_id:
        logger.error("MiniMax TTS requires text and a voice ID")
        return None
    settings = config.minimax_tts
    api_key = get_minimax_tts_api_key()
    if not api_key:
        logger.error("MiniMax TTS API key is not set")
        return None
    url = get_minimax_tts_endpoint()
    model = str(settings.get("model_id", MINIMAX_TTS_DEFAULT_MODEL) or MINIMAX_TTS_DEFAULT_MODEL).strip()
    if model not in MINIMAX_TTS_MODELS:
        logger.error(f"Unsupported MiniMax TTS model: {model}")
        return None
    try:
        speed = max(0.5, min(2.0, float(voice_rate or 1.0)))
        volume = max(0.0, min(10.0, float(voice_volume or 1.0)))
        pitch = max(-12, min(12, int(settings.get("pitch", 0) or 0)))
        sample_rate = int(settings.get("sample_rate", 32000) or 32000)
        bitrate = int(settings.get("bitrate", 128000) or 128000)
        channel = int(settings.get("channel", 1) or 1)
    except (TypeError, ValueError) as exc:
        logger.error(f"Invalid MiniMax TTS audio setting: {str(exc)}")
        return None
    audio_format = str(settings.get("audio_format", "mp3") or "mp3").strip()
    if audio_format not in {"mp3", "wav", "flac", "pcm"}:
        logger.error(f"Unsupported MiniMax TTS audio format: {audio_format}")
        return None
    payload = {
        "model": model, "text": text, "stream": False, "language_boost": "auto", "output_format": "hex",
        "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": volume, "pitch": pitch},
        "audio_setting": {"sample_rate": sample_rate, "bitrate": bitrate, "format": audio_format, "channel": channel},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            logger.info(f"start MiniMax TTS, model: {model}, voice: {voice_id}, try: {attempt + 1}")
            response = requests.post(url, json=payload, headers=headers, timeout=120)
            if response.status_code != 200:
                logger.error(f"MiniMax TTS failed with status {response.status_code}: {response.text[:200]}")
                continue
            body = response.json()
            data = body.get("data") or {}
            base_resp = body.get("base_resp") or {}
            if base_resp.get("status_code") != 0 or data.get("status") != 2:
                logger.error(f"MiniMax TTS returned an unsuccessful response: status_code={base_resp.get('status_code')}, audio_status={data.get('status')}")
                continue
            audio_hex = data.get("audio")
            if not isinstance(audio_hex, str) or not audio_hex:
                logger.error("MiniMax TTS returned empty audio data")
                continue
            if len(audio_hex) > _MINIMAX_TTS_MAX_AUDIO_HEX_CHARS:
                logger.error("MiniMax TTS returned audio data exceeding the supported size")
                continue
            audio_duration = _write_validated_minimax_audio(bytes.fromhex(audio_hex), voice_file)
            logger.success(f"MiniMax TTS succeeded: {voice_file}")
            return populate_legacy_submaker_with_full_text(
                ensure_legacy_submaker_fields(SubMaker()), text, audio_duration
            )
        except (OSError, ValueError, requests.RequestException) as exc:
            logger.error(f"MiniMax TTS failed: {str(exc)}")
    return None


def elevenlabs_tts(
    text: str,
    voice_id: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    model_id: str = "",
) -> Union[SubMaker, None]:
    text = (text or "").strip()
    if not text:
        logger.error("ElevenLabs TTS text is empty")
        return None

    api_key = get_elevenlabs_api_key()
    if not api_key:
        logger.error("ElevenLabs API key is not set")
        return None

    if not model_id:
        model_id = config.elevenlabs.get("model_id", "eleven_multilingual_v2")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    # Errors where retrying will never help (auth/access/validation failures).
    _NON_RETRYABLE_CODES = {401, 403, 422}
    _NON_RETRYABLE_STATUSES = {"voice_disabled", "voice_access_denied", "unauthorized"}

    for i in range(3):
        try:
            logger.info(f"start elevenlabs tts, voice_id: {voice_id}, try: {i + 1}")
            ensure_file_path_exists(voice_file)

            response = requests.post(url, json=payload, headers=headers, timeout=60)
            if response.status_code != 200:
                error_status = ""
                try:
                    detail = response.json().get("detail", {})
                    if isinstance(detail, dict):
                        error_status = detail.get("status", "")
                except Exception:
                    pass

                if response.status_code in _NON_RETRYABLE_CODES or error_status in _NON_RETRYABLE_STATUSES:
                    logger.error(
                        f"ElevenLabs TTS failed (non-retryable) — voice_id: {voice_id}, "
                        f"status: {response.status_code}, error: {error_status or response.text[:200]}. "
                        "Please select a different ElevenLabs voice."
                    )
                    return None

                logger.error(
                    f"elevenlabs tts failed with status {response.status_code}: {response.text[:200]}"
                )
                continue

            with open(voice_file, "wb") as f:
                f.write(response.content)

            audio_clip = AudioFileClip(voice_file)
            try:
                audio_duration = audio_clip.duration
            finally:
                audio_clip.close()

            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            logger.success(f"elevenlabs tts succeeded: {voice_file}")
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"elevenlabs tts failed: {str(e)}")

    return None


def _openai_compatible_tts(
    provider: str,
    base_url: str,
    api_key: str,
    model_id: str,
    voice: str,
    text: str,
    voice_rate: float,
    voice_file: str,
) -> Union[SubMaker, None]:
    """Shared transport for self-hosted, OpenAI-compatible ``/audio/speech``
    servers (Chatterbox, Kokoro, ...).

    Writes the returned audio to ``voice_file`` and builds the full-text
    SubMaker: these servers return no word-level timestamps, so set
    ``subtitle_provider = "whisper"`` for tighter subtitle sync.
    """
    url = f"{base_url}/audio/speech"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model_id,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
        # OpenAI speech API accepts speed 0.25-4.0; MoneyPrinterTurbo's rate is a
        # 1.0-centred multiplier, so it maps directly (clamped to the valid range).
        "speed": max(0.25, min(4.0, float(voice_rate or 1.0))),
    }
    # Giao thức giọng nói OpenAI không có trường âm lượng; voice_volume được áp dụng ở giai đoạn trộn cuối cùng.
    # tốc độ chỉ điều chỉnh tốc độ nói và không thể được sử dụng làm thông số âm lượng.

    for i in range(3):
        temporary_audio = None
        try:
            logger.info(f"start {provider} tts, voice: {voice}, try: {i + 1}")
            ensure_file_path_exists(voice_file)

            timeout = 300 if provider == "viettts" else 120
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if response.status_code != 200:
                logger.error(
                    f"{provider} tts failed with status {response.status_code}: {response.text[:200]}"
                )
                continue

            if not response.content:
                raise ValueError(f"{provider} returned empty audio")

            # Đầu tiên hãy ghi tệp tạm thời vào cùng thư mục và thực sự giải mã nó, sau đó thay thế mục tiêu sau khi thành công. Thất bại không thể phá hủy
            # Đã có buổi thử giọng/lồng tiếng; đóng file trước rồi giải mã và thay thế nó, tương thích với quy tắc chiếm dụng file của Windows.
            with tempfile.NamedTemporaryFile(
                dir=os.path.dirname(os.path.abspath(voice_file)),
                suffix=".mp3", delete=False,
            ) as f:
                temporary_audio = f.name
                f.write(response.content)

            audio_clip = AudioFileClip(temporary_audio)
            try:
                audio_duration = audio_clip.duration
            finally:
                audio_clip.close()
            if not math.isfinite(audio_duration) or audio_duration <= 0:
                raise ValueError(f"{provider} returned an invalid audio duration")

            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            os.replace(temporary_audio, voice_file)
            logger.success(f"{provider} tts succeeded: {voice_file}")
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"{provider} tts failed: {str(e)}")
        finally:
            if temporary_audio and os.path.exists(temporary_audio):
                try:
                    os.unlink(temporary_audio)
                except OSError as exc:
                    # Lỗi dọn dẹp vẫn duy trì quy ước trả về lỗi của TTS, không có ngoại lệ thứ cấp bao gồm nguyên nhân ban đầu.
                    logger.warning(f"could not remove temporary {provider} audio: {exc}")

    return None


def chatterbox_tts(
    text: str,
    voice: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    model_id: str = "",
) -> Union[SubMaker, None]:
    """Generate speech with a self-hosted Chatterbox TTS server.

    Chatterbox (Resemble AI, MIT) is an open-source, locally hosted TTS model
    with zero-shot voice cloning — a self-hostable alternative to ElevenLabs.
    This talks to an OpenAI-compatible ``/audio/speech`` endpoint, so it works
    with the common community servers (e.g. devnen/Chatterbox-TTS-Server,
    travisvn/chatterbox-tts-api). Configure ``[chatterbox] base_url`` (and an
    optional ``api_key``).

    Like ElevenLabs, Chatterbox does not return word-level timestamps, so the
    subtitle path falls back to the full-text SubMaker. For tighter subtitle
    sync set ``subtitle_provider = "whisper"``.
    """
    text = (text or "").strip()
    if not text:
        logger.error("Chatterbox TTS text is empty")
        return None

    base_url = (config.chatterbox.get("base_url", "") or "").strip().rstrip("/")
    if not base_url:
        logger.error(
            "Chatterbox base_url is not set, please configure [chatterbox] base_url in config.toml"
        )
        return None

    api_key = config.chatterbox.get("api_key", "")
    if not model_id:
        model_id = config.chatterbox.get("model_id", "chatterbox") or "chatterbox"

    return _openai_compatible_tts(
        "chatterbox", base_url, api_key, model_id, voice, text, voice_rate, voice_file
    )


def kokoro_tts(
    text: str,
    voice: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    model_id: str = "",
) -> Union[SubMaker, None]:
    """Generate speech with a self-hosted Kokoro TTS server.

    Kokoro (hexgrad/Kokoro-82M, Apache-2.0 code and weights) is a small open
    TTS model that runs well on CPU — a free, offline alternative to the
    cloud voices. This talks to an OpenAI-compatible ``/audio/speech``
    endpoint, so it works with the common servers (e.g. remsky/Kokoro-FastAPI
    on port 8880). Configure ``[kokoro] base_url`` (ending in ``/v1``) and an
    optional ``api_key``.

    Voice names are Kokoro's presets (``af_heart``, ``bf_emma``, ``hf_alpha``,
    ...); their first letter is the language (a/b English, e Spanish, f French,
    h Hindi, i Italian, p Portuguese, j Japanese, z Chinese), so pick a voice
    that matches the script's language.

    Like Chatterbox, the OpenAI speech contract returns no word-level
    timestamps, so the subtitle path falls back to the full-text SubMaker.
    For tighter subtitle sync set ``subtitle_provider = "whisper"``.
    """
    text = (text or "").strip()
    if not text:
        logger.error("Kokoro TTS text is empty")
        return None
    # Dấu câu/biểu tượng cảm xúc thuần túy không có văn bản có thể phát âm được; các dịch vụ thực có thể trả về các tệp MP3 trống với HTTP 200,
    # Việc chấm dứt sớm sẽ tránh được các yêu cầu không hợp lệ và các ngoại lệ cấp thấp trong quá trình giải mã các tệp trống của MoviePy.
    if not any(character.isalnum() for character in text):
        logger.error("Kokoro TTS text contains no speakable characters")
        return None
    base_url = (config.kokoro.get("base_url", "") or "").strip().rstrip("/")
    if not base_url:
        logger.error(
            "Kokoro base_url is not set, please configure [kokoro] base_url in config.toml"
        )
        return None
    api_key = config.kokoro.get("api_key", "")
    if not model_id:
        model_id = config.kokoro.get("model_id", "kokoro") or "kokoro"
    return _openai_compatible_tts(
        "kokoro", base_url, api_key, model_id, voice, text, voice_rate, voice_file
    )


def viettts_tts(
    text: str,
    voice: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    model_id: str = "",
) -> Union[SubMaker, None]:
    """Generate speech with a self-hosted VietTTS server.

    VietTTS (dangvansam/viet-tts, MIT) is an open-source Vietnamese TTS with
    built-in voices optimised for Vietnamese tones and prosody.  It exposes an
    OpenAI-compatible ``/audio/speech`` endpoint on port 8298 by default.

    Configure ``[viettts] base_url`` (ending in ``/v1``) and an optional
    ``api_key``.  Voice names include ``nsnd-le-chuc`` (deep male narrator),
    ``speechify_1`` through ``speechify_10``, ``nu-nhe-nhang``, and ``quynh``.

    Like Kokoro, the OpenAI speech contract returns no word-level timestamps,
    so the subtitle path falls back to the full-text SubMaker.  For tighter
    subtitle sync set ``subtitle_provider = "whisper"``.
    """
    text = (text or "").strip()
    if not text:
        logger.error("VietTTS text is empty")
        return None
    if not any(character.isalnum() for character in text):
        logger.error("VietTTS text contains no speakable characters")
        return None
    base_url = (config.viettts.get("base_url", "") or "").strip().rstrip("/")
    if not base_url:
        logger.error(
            "VietTTS base_url is not set, please configure [viettts] base_url in config.toml"
        )
        return None
    api_key = config.viettts.get("api_key", "")
    if not model_id:
        model_id = config.viettts.get("model_id", "viettts") or "viettts"
    return _openai_compatible_tts(
        "viettts", base_url, api_key, model_id, voice, text, voice_rate, voice_file
    )


# Fish Audio supported models.
FISH_AUDIO_MODELS = ("s2.1-pro-free", "s2.1-pro", "s2-pro")
FISH_AUDIO_DEFAULT_MODEL = "s2.1-pro-free"


def fish_audio_tts(
    text: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    reference_id: str | None = None,
) -> Union[SubMaker, None]:
    """Generate speech using Fish Audio TTS API.

    The model is read from ``config.fish_audio["model"]`` (single source of
    truth).  ``reference_id`` selects a public or cloned voice; when *None*
    Fish Audio's built-in default voice is used.

    ``voice_rate`` is mapped to the ``prosody.speed`` field (0.5–2.0) and
    ``voice_volume`` is converted from a linear multiplier to dB for the
    ``prosody.volume`` field (-20.0–20.0 dB).
    """
    text = (text or "").strip()
    if not text:
        logger.error("Fish Audio TTS text is empty")
        return None

    api_key = get_fish_audio_api_key()
    if not api_key:
        logger.error(
            "Fish Audio API key is not set. Please set it in config.toml "
            "[fish_audio] or FISH_API_KEY environment variable."
        )
        return None

    model_name = str(
        config.fish_audio.get("model", FISH_AUDIO_DEFAULT_MODEL)
        or FISH_AUDIO_DEFAULT_MODEL
    ).strip()
    if model_name not in FISH_AUDIO_MODELS:
        logger.warning(
            f"Unknown Fish Audio model '{model_name}', falling back to "
            f"'{FISH_AUDIO_DEFAULT_MODEL}'"
        )
        model_name = FISH_AUDIO_DEFAULT_MODEL

    # Map voice_rate → prosody.speed (0.5–2.0)
    try:
        speed = max(0.5, min(2.0, float(voice_rate or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0

    # Map voice_volume (linear multiplier) → prosody.volume (dB, -20–20).
    # A multiplier of 1.0 → 0 dB; 0.1 → -20 dB; 2.0 → +6 dB.
    import math
    try:
        vol = float(voice_volume or 1.0)
        if vol <= 0:
            volume_db = -20.0
        else:
            volume_db = max(-20.0, min(20.0, 20.0 * math.log10(vol)))
    except (TypeError, ValueError):
        volume_db = 0.0

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model_name,
    }
    payload: dict = {
        "text": text,
        "format": "mp3",
        "prosody": {
            "speed": speed,
            "volume": volume_db,
        },
    }
    if reference_id:
        payload["reference_id"] = reference_id

    for i in range(3):
        try:
            logger.info(
                f"start fish audio tts, model: {model_name}, "
                f"ref: {reference_id or 'default'}, try: {i + 1}"
            )
            ensure_file_path_exists(voice_file)

            response = requests.post(url, json=payload, headers=headers, timeout=60)
            if response.status_code == 401:
                logger.error(
                    "Fish Audio TTS failed: Invalid API key (401). "
                    "Check config.toml [fish_audio] api_key or FISH_API_KEY."
                )
                return None
            if response.status_code == 402:
                logger.error(
                    "Fish Audio TTS failed: Insufficient API credit (402). "
                    "Please check your account balance at "
                    "https://fish.audio/app/developers or verify your model and billing tier."
                )
                return None
            if response.status_code == 429:
                logger.warning(
                    "Fish Audio TTS rate limited (429), retrying..."
                )
                continue
            if response.status_code != 200:
                logger.error(
                    f"fish audio tts failed with status "
                    f"{response.status_code}: {response.text[:200]}"
                )
                continue

            # Validate response contains audio data
            if not response.content or len(response.content) < 100:
                logger.error(
                    "Fish Audio TTS returned empty or invalid audio data"
                )
                continue

            with open(voice_file, "wb") as f:
                f.write(response.content)

            audio_clip = AudioFileClip(voice_file)
            try:
                audio_duration = audio_clip.duration
            finally:
                audio_clip.close()

            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            logger.success(f"fish audio tts succeeded: {voice_file}")
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"fish audio tts failed: {str(e)}")

    return None


def _format_text(text: str) -> str:
    """
    Làm sạch văn bản script trước khi căn chỉnh phụ đề.

    Điều này không thể chỉ được xử lý trong giai đoạn tạo LLM vì người dùng cũng có thể dán tập lệnh theo cách thủ công hoặc thông qua
    API trực tiếp chuyển văn bản có chứa đánh dấu Markdown. TTS thường không đọc `---`,
    Các dòng phân cách như `___` và `***` sẽ không được đọc to. Các dấu nhấn mạnh như `_` sẽ không được đọc to; nếu phụ đề
    Căn chỉnh vẫn giữ lại các ký tự này, `create_subtitle()` sẽ luôn chờ một tín hiệu không tồn tại,
    Điều này cuối cùng dẫn đến việc thiếu các tệp phụ đề và dòng thời gian toàn số 0 được điền vào trong quá trình chỉnh sửa dự phòng Whisper.
    """
    text = utils.remove_pause_tags(text or "")
    text = text.replace("[", " ")
    text = text.replace("]", " ")
    text = text.replace("(", " ")
    text = text.replace(")", " ")
    text = text.replace("{", " ")
    text = text.replace("}", " ")
    return utils.normalize_script_for_subtitle_matching(text)


def _build_subtitle_formatter():
    """
    Trả về chức năng định dạng dòng SRT thống nhất.

    Ở đây nó được chia thành một công cụ nhỏ để tạo đường dẫn tín hiệu của edge_tts 7.x
    Nó chia sẻ cùng định dạng đĩa phụ đề với đường dẫn `subs/offset` kế thừa ban đầu của dự án.
    Tránh sự khác biệt tinh tế về định dạng giữa hai bộ logic.
    """

    def formatter(idx: int, start_time: float, end_time: float, sub_text: str) -> str:
        start_t = mktimestamp(start_time).replace(".", ",")
        end_t = mktimestamp(end_time).replace(".", ",")
        return f"{idx}\n{start_t} --> {end_t}\n{sub_text}\n"

    return formatter


# Dấu phụ tiếng Ả Rập và bộ kéo dài Tatweel có thể xuất hiện trong văn bản trả về edge-tts,
# Những ký tự này không ảnh hưởng đến ngữ nghĩa nhưng có thể khiến cho việc so khớp chính xác giữa văn bản tập lệnh và chuỗi tín hiệu phụ đề không thành công.
_ARABIC_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u0640\u06D6-\u06ED]")


def _normalize_arabic(text: str) -> str:
    """Thống nhất các biến thể chữ cái Ả Rập phổ biến và cải thiện tỷ lệ chấp nhận lỗi của các tín hiệu phụ đề và dòng chữ phù hợp.

    edge-tts cho tiếng Ả Rập có thể trả về các dạng chữ cái khác với chữ viết gốc, ví dụ: أ/إ/آ
    Chuẩn hóa thành ا hoặc mang dấu phụ. Điều này chỉ được sử dụng trong lớp túi phù hợp cuối cùng.
    Không thay đổi văn bản phụ đề gốc để tránh ảnh hưởng đến nội dung hiển thị cuối cùng.
    """
    text = _ARABIC_DIACRITICS.sub("", text)
    for src, dst in (
        ("أإآٱ", "ا"),
        ("ىئ", "ي"),
        ("ة", "ه"),
        ("ؤ", "و"),
    ):
        for ch in src:
            text = text.replace(ch, dst)
    return text


def _match_script_line(script_lines: list[str], current_text: str, sub_index: int) -> str:
    """
    Cố gắng khớp văn bản phụ đề hiện được tích lũy với một đoạn tiêu chuẩn trong tập lệnh.

    Ý tưởng ban đầu của dự án là “chia đoạn văn theo dấu câu rồi so sánh từng phần” được sử dụng lại ở đây:
    1. Ưu tiên đối sánh chính xác;
    2. Thực hiện khớp lại để xóa dấu câu và ký tự định dạng Markdown `_`;
    3. Cuối cùng, thực hiện so khớp chuẩn hóa các dạng ký tự Ả Rập.

    Điều này tương thích với:
    - Các dấu chấm câu có thể bị thiếu hoặc bị tách riêng trong tờ khai TTS;
    - Trong kịch bản tiếng Trung, ranh giới từ và văn bản chữ viết không hoàn toàn tương ứng một đối một.
    """
    if len(script_lines) <= sub_index:
        return ""

    target_line = script_lines[sub_index]
    if current_text == target_line:
        return target_line.strip()

    current_text_normalized = re.sub(r"[_\W]+", "", current_text)
    target_line_normalized = re.sub(r"[_\W]+", "", target_line)
    if current_text_normalized == target_line_normalized:
        return target_line.strip()

    # Lớp cuối cùng của khả năng chấp nhận tiếng Ả Rập: dạng chữ, dấu phụ hoặc Tatweel được trả về bởi edge-tts
    # Có thể khác với kịch bản. So sánh chuẩn hóa chỉ được thực hiện sau khi kết hợp thông thường không thành công, văn bản không phải tiếng Ả Rập không bị ảnh hưởng.
    current_ar = re.sub(r"[_\W]+", "", _normalize_arabic(current_text))
    target_ar = re.sub(r"[_\W]+", "", _normalize_arabic(target_line))
    if current_ar and current_ar == target_ar:
        return target_line.strip()

    return ""


def _write_subtitle_items(sub_items: list[str], subtitle_file: str) -> bool:
    """
    Viết các phân đoạn phụ đề tổng hợp vào tệp SRT và thực hiện xác minh khả năng đọc cơ bản.

    Giá trị trả về:
    - `True`: File phụ đề được tải xuống thành công và có thể được phân tích bằng moviepy;
    - `False`: Ghi hoặc phân tích tệp phụ đề không thành công.
    """
    try:
        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write("\n".join(sub_items) + "\n")

        sbs = subtitles.file_to_subtitles(subtitle_file, encoding="utf-8")
        duration = max([tb for ((ta, tb), txt) in sbs]) if sbs else 0
        logger.info(
            f"completed, subtitle file created: {subtitle_file}, duration: {duration}"
        )
        return True
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")
        if os.path.exists(subtitle_file):
            os.remove(subtitle_file)
        return False


def _build_subtitle_items_from_edge_cues(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    """
    Tổng hợp các `tín hiệu` chi tiết của edge_tts 7.x thành các đoạn SRT theo kịch bản.

    lý lịch:
    `SubMaker.get_srt()` của edge_tts 7.x ưu tiên dòng thời gian theo từng từ/cụm từ.
    Đánh dấu từng chữ tiếng Anh thì được nhưng nếu sao chép trực tiếp phụ đề video ngắn tiếng Trung sẽ có
    “Tiền / là / một / xã hội / công cụ” Đây là một trải nghiệm đọc kém.

    Chiến lược thực hiện:
    1. Tiếp thu `nội dung` theo từng tín hiệu một;
    2. Tích lũy thành một văn bản ứng cử;
    3. Khi văn bản đề xuất khớp với phân đoạn mục tiêu hiện tại trong tập lệnh, nó sẽ hội tụ thành phân đoạn phụ đề hoàn chỉnh;
    4. Sử dụng thời gian bắt đầu của tín hiệu đầu tiên và thời gian kết thúc của tín hiệu cuối cùng để đảm bảo dòng thời gian được liên tục.
    """
    formatter = _build_subtitle_formatter()
    sub_items = []
    sub_index = 0
    current_text = ""
    current_start_time = None

    for cue in sub_maker.cues:
        cue_text = unescape(cue.content)
        if current_start_time is None:
            current_start_time = int(cue.start.total_seconds() * 10000000)

        current_end_time = int(cue.end.total_seconds() * 10000000)
        current_text += cue_text

        matched_text = _match_script_line(script_lines, current_text, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=current_start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        current_text = ""
        current_start_time = None

    if current_text.strip():
        logger.warning(
            f"edge cues still have unmatched text after aggregation: {current_text}"
        )

    return sub_items


def _build_subtitle_items_from_legacy_submaker(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    """
    Tổng hợp cấu trúc `subs/offset` ban đầu của dự án thành các đoạn SRT theo kịch bản.

    Phần này vẫn giữ nguyên ý tưởng cốt lõi ban đầu nhưng được chia thành các hàm độc lập để thuận tiện cho việc tích hợp với edge_tts 7.x
    Logic tổng hợp tín hiệu có chung quy trình sắp xếp và khớp câu.
    """
    formatter = _build_subtitle_formatter()
    start_time = -1.0
    sub_items = []
    sub_index = 0
    sub_line = ""

    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    for _, (offset, sub) in enumerate(zip(legacy_offsets, legacy_subs)):
        current_start_time, current_end_time = offset
        if start_time < 0:
            start_time = current_start_time

        sub_line += unescape(sub)
        matched_text = _match_script_line(script_lines, sub_line, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        start_time = -1.0
        sub_line = ""

    if sub_line.strip():
        logger.warning(
            f"legacy subtitle items still have unmatched text after aggregation: {sub_line}"
        )

    return sub_items


def _build_subtitle_items_from_edge_cues_words(sub_maker: SubMaker) -> list[str]:
    """
    Directly format edge_tts cues into single-word / cue-level SRT items.
    """
    formatter = _build_subtitle_formatter()
    sub_items = []
    sub_index = 0
    for cue in sub_maker.cues:
        cue_text = unescape(cue.content).strip()
        if not cue_text:
            continue
        sub_index += 1
        start_time = int(cue.start.total_seconds() * 10000000)
        end_time = int(cue.end.total_seconds() * 10000000)
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=start_time,
                end_time=end_time,
                sub_text=cue_text,
            )
        )
    return sub_items


def _build_subtitle_items_from_legacy_submaker_words(sub_maker: SubMaker) -> list[str]:
    """
    Directly format legacy submaker into single-word SRT items.
    """
    formatter = _build_subtitle_formatter()
    sub_items = []
    sub_index = 0
    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    for offset, sub in zip(legacy_offsets, legacy_subs):
        cue_text = unescape(sub).strip()
        if not cue_text:
            continue
        sub_index += 1
        start_time, end_time = offset
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=start_time,
                end_time=end_time,
                sub_text=cue_text,
            )
        )
    return sub_items


def create_subtitle(
    sub_maker: SubMaker,
    text: str,
    subtitle_file: str,
    word_level: bool = False,
):
    """
    Tối ưu hóa tập tin phụ đề
    1. Chia file phụ đề thành nhiều dòng theo dấu chấm câu
    2. So khớp văn bản trong file phụ đề theo từng dòng
    3. Tạo file phụ đề mới
    Nếu word_level là True, các phụ đề đơn sẽ được xuất trực tiếp từng từ một.
    """
    text = _format_text(text)
    try:
        if word_level:
            if hasattr(sub_maker, "cues") and sub_maker.cues:
                sub_items = _build_subtitle_items_from_edge_cues_words(sub_maker)
            else:
                sub_items = _build_subtitle_items_from_legacy_submaker_words(sub_maker)
            if sub_items:
                _write_subtitle_items(sub_items, subtitle_file)
                return

        script_lines = utils.split_string_by_punctuations(text)
        if hasattr(sub_maker, "cues") and sub_maker.cues:
            sub_items = _build_subtitle_items_from_edge_cues(sub_maker, script_lines)
        else:
            sub_items = _build_subtitle_items_from_legacy_submaker(
                sub_maker, script_lines
            )

        if len(sub_items) != len(script_lines):
            logger.warning(
                f"failed, sub_items len: {len(sub_items)}, script_lines len: {len(script_lines)}"
            )
            return

        _write_subtitle_items(sub_items, subtitle_file)
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")


def _get_audio_duration_from_submaker(sub_maker: SubMaker):
    """
    Nhận thời lượng âm thanh
    """
    if hasattr(sub_maker, "duration") and getattr(sub_maker, "duration", 0) > 0:
        return float(getattr(sub_maker, "duration"))

    # Ưu tiên khả năng tương thích với cấu trúc tín hiệu của edge_tts 7.x;
    # Nếu đó là cấu trúc cũ được TTS khác trong dự án điền thủ công, hãy tiếp tục đọc phần bù.
    if hasattr(sub_maker, "cues") and sub_maker.cues:
        return sub_maker.cues[-1].end.total_seconds()

    legacy_offsets = getattr(sub_maker, "offset", [])
    if not legacy_offsets:
        return 0.0
    return legacy_offsets[-1][1] / 10000000

def _get_audio_duration_from_file(audio_file: str) -> float:
    """
    Nhận thời lượng tệp âm thanh (hỗ trợ mp3/m4a/wav/aac và các định dạng có thể giải mã ffmpeg khác)
    """
    if not os.path.exists(audio_file):
        logger.error(f"audio file does not exist: {audio_file}")
        return 0.0

    try:
        # Use moviepy (ffmpeg) to read the duration of any supported audio format
        with AudioFileClip(audio_file) as audio:
            return audio.duration  # Duration in seconds
    except Exception as e:
        logger.error(f"Failed to get audio duration from file: {str(e)}")
        return 0.0

def get_audio_duration(target: Union[str, SubMaker]) -> float:
    """
    Nhận thời lượng âm thanh
    Nếu đó là đối tượng SubMaker, hãy lấy thời lượng từ SubMaker
    Nếu đó là đường dẫn tệp âm thanh, hãy lấy thời lượng từ tệp âm thanh (hỗ trợ mp3/m4a/wav và các định dạng khác)
    """
    if isinstance(target, SubMaker):
        return _get_audio_duration_from_submaker(target)
    elif isinstance(target, str):
        return _get_audio_duration_from_file(target)
    else:
        logger.error(f"Invalid target type: {type(target)}")
        return 0.0

if __name__ == "__main__":
    voice_name = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female"
    voice_name = parse_voice_name(voice_name)
    voice_name = is_azure_v2_voice(voice_name)
    print(voice_name)

    voices = get_all_azure_voices()
    print(len(voices))

    async def _do():
        temp_dir = utils.storage_dir("temp")

        voice_names = [
            "zh-CN-XiaoxiaoMultilingualNeural",
            # nữ giới
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-XiaoyiNeural",
            # nam giới
            "zh-CN-YunyangNeural",
            "zh-CN-YunxiNeural",
        ]
        text = """
        静夜思是唐代诗人李白创作的一首五言古诗。这首诗描绘了诗人在寂静的夜晚，看到窗前的明月，不禁想起远方的家乡和亲人，表达了他对家乡和亲人的深深思念之情。全诗内容是：“床前明月光，疑是地上霜。举头望明月，低头思故乡。”在这短短的四句诗中，诗人通过“明月”和“思故乡”的意象，巧妙地表达了离乡背井人的孤独与哀愁。首句“床前明月光”设景立意，通过明亮的月光引出诗人的遐想；“疑是地上霜”增添了夜晚的寒冷感，加深了诗人的孤寂之情；“举头望明月”和“低头思故乡”则是情感的升华，展现了诗人内心深处的乡愁和对家的渴望。这首诗简洁明快，情感真挚，是中国古典诗歌中非常著名的一首，也深受后人喜爱和推崇。
            """

        text = """
        What is the meaning of life? This question has puzzled philosophers, scientists, and thinkers of all kinds for centuries. Throughout history, various cultures and individuals have come up with their interpretations and beliefs around the purpose of life. Some say it's to seek happiness and self-fulfillment, while others believe it's about contributing to the welfare of others and making a positive impact in the world. Despite the myriad of perspectives, one thing remains clear: the meaning of life is a deeply personal concept that varies from one person to another. It's an existential inquiry that encourages us to reflect on our values, desires, and the essence of our existence.
        """

        text = """
               预计未来3天深圳冷空气活动频繁，未来两天持续阴天有小雨，出门带好雨具；
               10-11日持续阴天有小雨，日温差小，气温在13-17℃之间，体感阴凉；
               12日天气短暂好转，早晚清凉；
                   """

        text = "[Opening scene: A sunny day in a suburban neighborhood. A young boy named Alex, around 8 years old, is playing in his front yard with his loyal dog, Buddy.]\n\n[Camera zooms in on Alex as he throws a ball for Buddy to fetch. Buddy excitedly runs after it and brings it back to Alex.]\n\nAlex: Good boy, Buddy! You're the best dog ever!\n\n[Buddy barks happily and wags his tail.]\n\n[As Alex and Buddy continue playing, a series of potential dangers loom nearby, such as a stray dog approaching, a ball rolling towards the street, and a suspicious-looking stranger walking by.]\n\nAlex: Uh oh, Buddy, look out!\n\n[Buddy senses the danger and immediately springs into action. He barks loudly at the stray dog, scaring it away. Then, he rushes to retrieve the ball before it reaches the street and gently nudges it back towards Alex. Finally, he stands protectively between Alex and the stranger, growling softly to warn them away.]\n\nAlex: Wow, Buddy, you're like my superhero!\n\n[Just as Alex and Buddy are about to head inside, they hear a loud crash from a nearby construction site. They rush over to investigate and find a pile of rubble blocking the path of a kitten trapped underneath.]\n\nAlex: Oh no, Buddy, we have to help!\n\n[Buddy barks in agreement and together they work to carefully move the rubble aside, allowing the kitten to escape unharmed. The kitten gratefully nuzzles against Buddy, who responds with a friendly lick.]\n\nAlex: We did it, Buddy! We saved the day again!\n\n[As Alex and Buddy walk home together, the sun begins to set, casting a warm glow over the neighborhood.]\n\nAlex: Thanks for always being there to watch over me, Buddy. You're not just my dog, you're my best friend.\n\n[Buddy barks happily and nuzzles against Alex as they disappear into the sunset, ready to face whatever adventures tomorrow may bring.]\n\n[End scene.]"

        text = "大家好，我是乔哥，一个想帮你把信用卡全部还清的家伙！\n今天我们要聊的是信用卡的取现功能。\n你是不是也曾经因为一时的资金紧张，而拿着信用卡到ATM机取现？如果是，那你得好好看看这个视频了。\n现在都2024年了，我以为现在不会再有人用信用卡取现功能了。前几天一个粉丝发来一张图片，取现1万。\n信用卡取现有三个弊端。\n一，信用卡取现功能代价可不小。会先收取一个取现手续费，比如这个粉丝，取现1万，按2.5%收取手续费，收取了250元。\n二，信用卡正常消费有最长56天的免息期，但取现不享受免息期。从取现那一天开始，每天按照万5收取利息，这个粉丝用了11天，收取了55元利息。\n三，频繁的取现行为，银行会认为你资金紧张，会被标记为高风险用户，影响你的综合评分和额度。\n那么，如果你资金紧张了，该怎么办呢？\n乔哥给你支一招，用破思机摩擦信用卡，只需要少量的手续费，而且还可以享受最长56天的免息期。\n最后，如果你对玩卡感兴趣，可以找乔哥领取一本《卡神秘籍》，用卡过程中遇到任何疑惑，也欢迎找乔哥交流。\n别忘了，关注乔哥，回复用卡技巧，免费领取《2024用卡技巧》，让我们一起成为用卡高手！"

        text = """
        2023全年业绩速览
公司全年累计实现营业收入1476.94亿元，同比增长19.01%，归母净利润747.34亿元，同比增长19.16%。EPS达到59.49元。第四季度单季，营业收入444.25亿元，同比增长20.26%，环比增长31.86%；归母净利润218.58亿元，同比增长19.33%，环比增长29.37%。这一阶段
的业绩表现不仅突显了公司的增长动力和盈利能力，也反映出公司在竞争激烈的市场环境中保持了良好的发展势头。
2023年Q4业绩速览
第四季度，营业收入贡献主要增长点；销售费用高增致盈利能力承压；税金同比上升27%，扰动净利率表现。
业绩解读
利润方面，2023全年贵州茅台，>归母净利润增速为19%，其中营业收入正贡献18%，营业成本正贡献百分之一，管理费用正贡献百分之一点四。(注：归母净利润增速值=营业收入增速+各科目贡献，展示贡献/拖累的前四名科目，且要求贡献值/净利润增速>15%)
"""
        text = "静夜思是唐代诗人李白创作的一首五言古诗。这首诗描绘了诗人在寂静的夜晚，看到窗前的明月，不禁想起远方的家乡和亲人"

        text = _format_text(text)
        lines = utils.split_string_by_punctuations(text)
        print(lines)

        for voice_name in voice_names:
            voice_file = f"{temp_dir}/tts-{voice_name}.mp3"
            subtitle_file = f"{temp_dir}/tts.mp3.srt"
            sub_maker = azure_tts_v2(
                text=text, voice_name=voice_name, voice_file=voice_file
            )
            create_subtitle(sub_maker=sub_maker, text=text, subtitle_file=subtitle_file)
            audio_duration = get_audio_duration(sub_maker)
            print(f"voice: {voice_name}, audio duration: {audio_duration}s")

    loop = asyncio.get_event_loop_policy().get_event_loop()
    try:
        loop.run_until_complete(_do())
    finally:
        loop.close()
