import json
import os
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import voice


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
LOCALES = ("de", "en", "es", "id", "pt", "ru", "tr", "vi", "zh")

# Mỗi nhà cung cấp dịch vụ chỉ duy trì một lối vào chính thức. Chatterbox là dịch vụ tự lưu trữ và không có Key thống nhất
# Nhận nền tảng, nên liên kết đến hướng dẫn cấu hình của các dịch vụ tương thích thực sự được sử dụng để tránh nhầm lẫn cho người dùng đăng ký tài khoản bên thứ ba.
TTS_API_KEY_LABELS = {
    "Speech Key": "portal.azure.com",
    "SiliconFlow API Key": "cloud.siliconflow.cn/account/ak",
    "Gemini API Key": "aistudio.google.com/app/apikey",
    "MiMo API Key": "mimo.mi.com/docs/",
    "MiniMax TTS API Key": "platform.minimaxi.com",
    "ElevenLabs API Key": "elevenlabs.io/app/settings/api-keys",
    "Chatterbox API Key": "github.com/travisvn/chatterbox-tts-api",
}

TTS_PROVIDER_WIDGETS = {
    "azure-tts-v2": ("azure_speech_key_input", "Speech Key"),
    "siliconflow": ("siliconflow_api_key_input", "SiliconFlow API Key"),
    "gemini-tts": ("gemini_tts_api_key_input", "Gemini API Key"),
    "mimo-tts": ("mimo_tts_api_key_input", "MiMo API Key"),
    "minimax-tts": ("minimax_tts_api_key_input", "MiniMax TTS API Key"),
    "elevenlabs": ("elevenlabs_api_key_input", "ElevenLabs API Key"),
    "chatterbox": ("chatterbox_api_key_input", "Chatterbox API Key"),
}


def _load_translation(locale: str) -> dict:
    """Đọc trực tiếp tệp ngôn ngữ, đảm bảo rằng các xác nhận bao gồm các thẻ Markdown cuối cùng mà người dùng thực sự nhìn thấy."""
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data["Translation"]


def _widget_by_key(elements, key: str):
    """Các nhãn điều khiển được sắp xếp hợp lý sẽ được dịch và sử dụng các khóa kinh doanh ổn định để xác định vị trí các hộp đầu vào thực sự."""
    return next(
        item
        for item in elements
        if str(getattr(item, "key", "")) == key
        or str(getattr(item, "key", "")).startswith(f"{key}_")
    )


def test_all_tts_api_key_labels_include_an_official_configuration_link():
    """Tất cả các ngôn ngữ nên giữ lại tên nhà cung cấp dịch vụ và mục nhập có thể nhấp để tránh mất liên kết trong quá trình dịch."""
    for locale in LOCALES:
        translations = _load_translation(locale)
        for label_key, expected_host in TTS_API_KEY_LABELS.items():
            label = translations[label_key]
            assert expected_host in label, f"{locale}: {label_key}"
            assert "](" in label, f"{locale}: {label_key}"


def test_tts_provider_inputs_render_the_standardized_labels():
    """Trên thực tế, hãy chuyển đổi từng Nhà cung cấp TTS và xác nhận rằng hộp nhập liệu không bỏ qua thẻ dịch hợp nhất."""
    test_ui = dict(
        config.ui,
        voice_mode="tts",
        tts_server="azure-tts-v1",
        voice_name="",
    )
    translations = _load_translation("zh")

    with (
        patch.object(config, "ui", test_ui),
        patch.object(config, "save_config"),
        patch.object(voice, "get_all_azure_voices", return_value=[]),
        patch.object(voice, "get_siliconflow_voices", return_value=[]),
        patch.object(voice, "get_gemini_voices", return_value=[]),
        patch.object(voice, "get_mimo_voices", return_value=[]),
        patch.object(voice, "get_elevenlabs_voices", return_value=[]),
        patch.object(voice, "get_chatterbox_voices", return_value=[]),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "zh"
        app.run()

        for provider, (widget_key, label_key) in TTS_PROVIDER_WIDGETS.items():
            provider_select = _widget_by_key(app.selectbox, "tts_server_select")
            provider_select.set_value(provider).run()

            api_key_input = _widget_by_key(app.text_input, widget_key)
            assert api_key_input.label == translations[label_key]
            assert api_key_input.proto.type == api_key_input.proto.PASSWORD
            assert not getattr(api_key_input.proto, "help", "")

    assert [str(item.value) for item in app.exception] == []


def test_elevenlabs_reconnect_restores_saved_key_before_loading_voices():
    """
    Sau khi khởi động lại dịch vụ, trình duyệt có thể phát lại trạng thái mật khẩu trống; WebUI sẽ giữ lại cấu hình và sử dụng nó trong lần chạy lại hiện tại
    Tải âm thanh bằng Khóa đã lưu, thay vì chỉ tránh viết phím trống và tiếp tục yêu cầu dịch vụ bằng Khóa trống.
    """
    test_config = dict(config.elevenlabs, api_key="saved-key")
    test_ui = dict(
        config.ui,
        voice_mode="tts",
        tts_server="elevenlabs",
        voice_name="",
    )

    with (
        patch.object(config, "elevenlabs", test_config),
        patch.object(config, "ui", test_ui),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(voice, "get_elevenlabs_voices", return_value=[]) as get_voices,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        app.session_state["elevenlabs_api_key_input"] = ""
        app.run()

    assert test_config["api_key"] == "saved-key"
    assert app.session_state["elevenlabs_api_key_input"] == "saved-key"
    assert get_voices.call_count >= 1
    assert all(call.args == ("saved-key",) for call in get_voices.call_args_list)
    assert [str(item.value) for item in app.exception] == []


def test_elevenlabs_environment_key_is_used_without_persisting_it():
    """Các biến môi trường có thể điều khiển việc tải âm thanh nhưng WebUI không thể tự động sao chép vào config.toml."""
    test_config = dict(config.elevenlabs, api_key="")
    test_ui = dict(
        config.ui,
        voice_mode="tts",
        tts_server="elevenlabs",
        voice_name="",
    )

    with (
        patch.object(config, "elevenlabs", test_config),
        patch.object(config, "ui", test_ui),
        patch.object(config, "try_save_config", return_value=True),
        patch.dict(os.environ, {"ELEVENLABS_API_KEY": "env-key"}),
        patch.object(voice, "get_elevenlabs_voices", return_value=[]) as get_voices,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        app.run()

    assert test_config["api_key"] == ""
    assert app.session_state["elevenlabs_api_key_input"] == "env-key"
    assert get_voices.call_count >= 1
    assert all(call.args == ("env-key",) for call in get_voices.call_args_list)
    assert [str(item.value) for item in app.exception] == []


def test_minimax_reconnect_restores_saved_tts_key():
    """Trạng thái trống sau khi trình duyệt kết nối lại không thể xóa Khóa TTS MiniMax đã lưu."""
    test_config = dict(config.minimax_tts, api_key="saved-tts-key", base_url=voice.MINIMAX_TTS_GLOBAL_URL)
    test_ui = dict(config.ui, voice_mode="tts", tts_server="minimax-tts", voice_name="")

    with (
        patch.object(config, "minimax_tts", test_config),
        patch.object(config, "ui", test_ui),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        app.session_state["minimax_tts_api_key_input"] = ""
        app.run()

    assert test_config["api_key"] == "saved-tts-key"
    assert app.session_state["minimax_tts_api_key_input"] == "saved-tts-key"
    assert [str(item.value) for item in app.exception] == []


def test_minimax_shared_llm_key_is_not_duplicated_in_tts_config():
    """Khóa LLM được chia sẻ sẽ tự động khớp với vùng nhưng không thể sao chép vào cấu hình dành riêng cho TTS."""
    test_config = dict(config.minimax_tts, api_key="", base_url="")
    test_app_config = dict(
        config.app,
        minimax_api_key="shared-cn-key",
        minimax_base_url="https://api.minimaxi.com/v1",
    )
    test_ui = dict(config.ui, voice_mode="tts", tts_server="minimax-tts", voice_name="")

    with (
        patch.object(config, "minimax_tts", test_config),
        patch.object(config, "app", test_app_config),
        patch.object(config, "ui", test_ui),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        app.run()

    api_key_input = _widget_by_key(app.text_input, "minimax_tts_api_key_input")
    endpoint_select = _widget_by_key(app.selectbox, "minimax_tts_endpoint_select")
    assert api_key_input.value == "shared-cn-key"
    assert test_config["api_key"] == ""
    assert endpoint_select.value == voice.MINIMAX_TTS_CN_URL
    assert endpoint_select.disabled
    assert [str(item.value) for item in app.exception] == []


def test_minimax_voice_selector_accepts_a_custom_voice_id():
    """Bộ chọn giọng nói phổ quát MiniMax sẽ kích hoạt khả năng nhập ID giọng nói ngoài danh sách."""
    test_config = dict(
        config.minimax_tts,
        api_key="test-key",
        base_url=voice.MINIMAX_TTS_GLOBAL_URL,
        voice_id="old-voice",
    )
    test_ui = dict(
        config.ui,
        voice_mode="tts",
        tts_server="minimax-tts",
        voice_name="minimax:old-voice",
    )

    with (
        patch.object(config, "minimax_tts", test_config),
        patch.object(config, "ui", test_ui),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        app.run()
        voice_select = _widget_by_key(
            app.selectbox,
            "speech_synthesis_select_minimax-tts",
        )

    assert voice_select.proto.accept_new_options
    assert voice_select.value == "minimax:old-voice"
    assert [str(item.value) for item in app.exception] == []


def test_minimax_voices_load_only_on_demand_and_sync_the_selected_voice():
    """Danh sách âm chỉ tải sau khi người dùng nhấp vào nó và kết quả lựa chọn sẽ được đồng bộ hóa với cấu hình và điều khiển âm chung."""
    test_config = dict(
        config.minimax_tts,
        api_key="test-key",
        base_url=voice.MINIMAX_TTS_CN_URL,
        voice_id="old-voice",
    )
    test_ui = dict(
        config.ui,
        voice_mode="tts",
        tts_server="minimax-tts",
        voice_name="minimax:old-voice",
    )
    catalog = [
        {
            "voice_id": "Chinese (Mandarin)_News_Anchor",
            "voice_name": "新闻女声",
            "voice_type": "system",
        },
        {
            "voice_id": "English_expressive_narrator",
            "voice_name": "Expressive Narrator",
            "voice_type": "system",
        },
    ]

    with (
        patch.object(config, "minimax_tts", test_config),
        patch.object(config, "ui", test_ui),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(
            voice,
            "get_minimax_voice_catalog",
            return_value=catalog,
        ) as get_catalog,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "zh"
        app.run()

        # Chạy lại trang thông thường không thể chủ động sử dụng API MiniMax; nó chỉ có thể được truy vấn bằng cách nhấp vào nút.
        get_catalog.assert_not_called()
        _widget_by_key(app.button, "load_minimax_voices_button").click().run()
        get_catalog.assert_called_once_with(
            api_key="test-key",
            endpoint=voice.MINIMAX_TTS_CN_URL,
            voice_type="all",
        )

        voice_select = _widget_by_key(
            app.selectbox,
            "speech_synthesis_select_minimax-tts",
        )
        voice_select.set_value("minimax:Chinese (Mandarin)_News_Anchor").run()

        assert test_config["voice_id"] == "Chinese (Mandarin)_News_Anchor"
        assert voice_select.value == "minimax:Chinese (Mandarin)_News_Anchor"

    voice_select = _widget_by_key(app.selectbox, "speech_synthesis_select_minimax-tts")
    assert voice_select.proto.accept_new_options
    assert test_config["voice_id"] == "Chinese (Mandarin)_News_Anchor"
    assert test_ui["voice_name"] == "minimax:Chinese (Mandarin)_News_Anchor"
    assert voice_select.value == "minimax:Chinese (Mandarin)_News_Anchor"
    assert get_catalog.call_count == 1
    assert not any(item.label == "MiniMax TTS Voice ID" for item in app.text_input)
    assert not any(item.label == "MiniMax Voice Catalog" for item in app.selectbox)
    assert [str(item.value) for item in app.exception] == []
