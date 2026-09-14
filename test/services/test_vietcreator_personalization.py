import json
import tomllib
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]


def test_personal_defaults_are_vietnamese_local_and_private():
    config = tomllib.loads(
        (ROOT_DIR / "config.example.toml").read_text(encoding="utf-8")
    )

    assert config["project_name"] == "VietCreator Studio"
    assert config["listen_host"] == "127.0.0.1"
    assert config["app"]["video_source"] == "local"
    assert config["app"]["llm_provider"] == "ollama"
    assert config["app"]["max_concurrent_tasks"] == 1
    assert config["app"]["video_codec"] == "h264_nvenc"
    assert config["app"]["upload_post_enabled"] is False
    assert config["app"]["upload_post_auto_upload"] is False
    assert config["app"]["upload_post_youtube_privacy_status"] == "private"
    assert config["ui"]["language"] == "vi"
    assert config["ui"]["video_language"] == "vi-VN"
    assert config["ui"]["video_aspect_local"] == "9:16"
    assert config["ui"]["video_transition_mode"] == "None"
    assert config["ui"]["voice_name"] == "vi-VN-HoaiMyNeural-Female"
    assert config["ui"]["font_name"] == "BeVietnamPro-Bold.ttf"


def test_vietnamese_locale_covers_main_creation_flow():
    locale = json.loads(
        (ROOT_DIR / "webui" / "i18n" / "vi.json").read_text(encoding="utf-8")
    )["Translation"]

    for key in (
        "Video Script Settings",
        "Video Subject",
        "Video Source",
        "Audio Settings",
        "Subtitle Settings",
        "Generate Video",
    ):
        assert key in locale
        assert locale[key].strip()


def test_webui_brand_uses_project_configuration():
    source = (ROOT_DIR / "webui" / "Main.py").read_text(encoding="utf-8")

    assert 'PROJECT_NAME = str(config.project_name or "MoneyPrinterTurbo").strip()' in source
    assert '<span class="mpt-brand__name">{html.escape(PROJECT_NAME)}</span>' in source
