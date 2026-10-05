import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from loguru import logger
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import bgm, elevenlabs_music, sonilo, voice


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
TEST_LOCALES = ("en", "zh")


def _valid_wav_bytes() -> bytes:
    """Tạo một WAV tiêu chuẩn rất ngắn, tránh việc kiểm tra phải dựa vào âm thanh bên ngoài hoặc các tệp ghi hệ thống từ kho lưu trữ."""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


class TestWebuiBackgroundMusic(unittest.TestCase):
    @staticmethod
    def _translation(locale, key):
        """Đọc bản sao dự kiến ​​bằng ngôn ngữ kiểm tra để tránh những xác nhận phụ thuộc vào một ngôn ngữ trình bày nhất định."""
        locale_data = json.loads(
            (I18N_DIR / f"{locale}.json").read_text(encoding="utf-8")
        )
        return locale_data["Translation"][key]

    def _widget_by_key(self, elements, key_prefix):
        """Tìm điều khiển thông qua khóa doanh nghiệp ổn định và vẫn có thể đạt được điều khiển tương tự sau khi nhãn hiển thị được dịch."""
        widget = next(
            (
                item
                for item in elements
                if str(getattr(item, "key", "")) == key_prefix
                or str(getattr(item, "key", "")).startswith(f"{key_prefix}_")
            ),
            None,
        )
        self.assertIsNotNone(widget, f"widget not found: {key_prefix}")
        return widget

    def _open_custom_bgm_panel(self, locale):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        # CI không có ngôn ngữ bản địa được lưu trong config.toml. Ghi đè rõ ràng ngôn ngữ phiên, cả hai
        # Việc tái tạo giá trị mặc định bằng tiếng Anh của CI cũng có thể bảo vệ giao diện tiếng Trung thường được các nhà phát triển sử dụng không quay trở lại.
        app.session_state["ui_language"] = locale
        app.run()
        source_select = self._widget_by_key(app.selectbox, "bgm_type_select")
        # Các tùy chọn thực sự của stable_selectbox là các giá trị doanh nghiệp và bản sao hiển thị sẽ thay đổi theo ngôn ngữ.
        source_select.set_value("custom").run()
        return app

    def _open_sonilo_bgm_panel(self, locale):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = locale
        app.run()
        source_select = self._widget_by_key(app.selectbox, "bgm_type_select")
        source_select.set_value("sonilo").run()
        return app

    def _open_elevenlabs_bgm_panel(self, locale):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = locale
        app.run()
        source_select = self._widget_by_key(app.selectbox, "bgm_type_select")
        source_select.set_value("elevenlabs").run()
        return app

    def _open_preset_bgm_panel(self, locale):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = locale
        app.run()
        source_select = self._widget_by_key(app.selectbox, "bgm_type_select")
        source_select.set_value("preset").run()
        return app

    def _uploader(self, app):
        return self._widget_by_key(app.file_uploader, "custom_bgm_uploader")

    def _volume_select(self, app):
        return self._widget_by_key(app.selectbox, "bgm_volume_select")

    def test_preset_song_selection_is_previewed_and_persisted(self):
        """Trình phát phải được cập nhật ngay sau khi chuyển đổi các bài hát cài sẵn và cấu hình tên tệp ổn định phải được giữ lại."""
        with tempfile.TemporaryDirectory() as temp_dir:
            first_song = Path(temp_dir) / "first.wav"
            second_song = Path(temp_dir) / "second.wav"
            first_song.write_bytes(_valid_wav_bytes())
            second_song.write_bytes(_valid_wav_bytes())
            test_ui = dict(
                config.ui,
                language="en",
                bgm_type="random",
                preset_song=first_song.name,
            )

            with (
                patch.object(config, "ui", test_ui),
                patch.object(config, "try_save_config", return_value=True),
                patch.object(
                    bgm,
                    "list_builtin_bgm_files",
                    return_value=[str(first_song), str(second_song)],
                ),
            ):
                app = self._open_preset_bgm_panel("en")
                preset_select = self._widget_by_key(
                    app.selectbox, "preset_song_select"
                )
                self.assertEqual(preset_select.value, first_song.name)
                self.assertEqual(len(app.get("audio")), 1)

                preset_select.set_value(second_song.name).run()

            updated_select = self._widget_by_key(
                app.selectbox, "preset_song_select"
            )
            self.assertEqual(updated_select.value, second_song.name)
            self.assertEqual(test_ui["preset_song"], second_song.name)
            self.assertEqual(len(app.get("audio")), 1)
            self.assertEqual([str(item.value) for item in app.exception], [])

    def test_empty_preset_song_list_shows_localized_warning(self):
        """Lời nhắc về ngôn ngữ hiện tại sẽ được đưa ra khi không có bài hát nào, thay vì hiển thị hộp chọn không hợp lệ."""
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                test_ui = dict(config.ui, language=locale, bgm_type="random")
                with (
                    patch.object(config, "ui", test_ui),
                    patch.object(config, "try_save_config", return_value=True),
                    patch.object(bgm, "list_builtin_bgm_files", return_value=[]),
                ):
                    app = self._open_preset_bgm_panel(locale)

                self.assertTrue(
                    any(
                        item.value
                        == self._translation(locale, "No Background Music Available")
                        for item in app.warning
                    )
                )
                self.assertFalse(
                    any(
                        str(getattr(item, "key", "")).startswith(
                            "preset_song_select"
                        )
                        for item in app.selectbox
                    )
                )
                self.assertEqual([str(item.value) for item in app.exception], [])

    def test_task_restore_selects_the_original_preset_song(self):
        """Khi khôi phục các tác vụ lịch sử, chúng không thể bị ghi đè bởi các bài hát cài sẵn đã lưu trên toàn cầu."""
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_song = Path(temp_dir) / "saved.wav"
            restored_song = Path(temp_dir) / "restored.wav"
            saved_song.write_bytes(_valid_wav_bytes())
            restored_song.write_bytes(_valid_wav_bytes())
            test_ui = dict(
                config.ui,
                language="en",
                bgm_type="random",
                preset_song=saved_song.name,
            )

            with (
                patch.object(config, "ui", test_ui),
                patch.object(config, "try_save_config", return_value=True),
                patch.object(
                    bgm,
                    "list_builtin_bgm_files",
                    return_value=[str(saved_song), str(restored_song)],
                ),
            ):
                app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
                app.session_state["ui_language"] = "en"
                app.session_state["task_restore_payload"] = {
                    "task_id": "preset-bgm-restore-test",
                    "params": {
                        "bgm_type": "preset",
                        "bgm_file": str(restored_song),
                    },
                }
                app.run()

            preset_select = self._widget_by_key(
                app.selectbox, "preset_song_select"
            )
            self.assertEqual(preset_select.value, restored_song.name)
            self.assertEqual(test_ui["preset_song"], restored_song.name)
            self.assertEqual(len(app.get("audio")), 1)
            self.assertEqual([str(item.value) for item in app.exception], [])

    def test_missing_preset_song_does_not_interrupt_the_page(self):
        """Lời nhắc sẽ được hiển thị khi một tệp trở nên không hợp lệ sau khi liệt kê và không thể tạo trình phát bị hỏng."""
        missing_song = Path(tempfile.gettempdir()) / "mpt-missing-preset.wav"
        missing_song.unlink(missing_ok=True)
        test_ui = dict(config.ui, language="en", bgm_type="random")

        with (
            patch.object(config, "ui", test_ui),
            patch.object(config, "try_save_config", return_value=True),
            patch.object(
                bgm,
                "list_builtin_bgm_files",
                return_value=[str(missing_song)],
            ),
        ):
            app = self._open_preset_bgm_panel("en")

        self.assertIn(
            self._translation("en", "Background Music Preview Failed"),
            [item.value for item in app.warning],
        )
        self.assertEqual(len(app.get("audio")), 0)
        self.assertEqual([str(item.value) for item in app.exception], [])

    def test_invalid_audio_shows_error_without_ready_state_or_player(self):
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                app = self._open_custom_bgm_panel(locale)
                with patch.object(logger, "warning") as warning:
                    self._uploader(app).set_value(
                        (
                            "invalid.m4a",
                            b"not-a-decodable-audio-file",
                            "audio/mp4",
                        )
                    ).run()
                    # Kích hoạt điều chỉnh âm lượng Streamlit chạy lại khi các tệp bất hợp pháp bị bỏ lại trong kiểm soát tải lên.
                    # Các lần truy cập bộ nhớ đệm chỉ có thể vẽ lại lỗi và không thể xác minh nhiều lần hoặc ghi lại cảnh báo nhiều lần.
                    self._volume_select(app).set_value(0.4).run()

                rejection_logs = [
                    call
                    for call in warning.call_args_list
                    if "WebUI background music validation rejected" in str(call)
                ]
                self.assertEqual([str(item.value) for item in app.exception], [])
                self.assertEqual(
                    [item.value for item in app.error],
                    [self._translation(locale, "Invalid Background Music")],
                )
                self.assertFalse(
                    any("invalid.m4a" in item.value for item in app.info)
                )
                self.assertEqual(len(app.get("audio")), 0)
                self.assertEqual(len(rejection_logs), 1)

    def test_valid_audio_shows_ready_state_and_reuses_validation_cache(self):
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                app = self._open_custom_bgm_panel(locale)
                self._uploader(app).set_value(
                    ("valid.wav", _valid_wav_bytes(), "audio/wav")
                ).run()

                # Sau khi lần xác minh đầu tiên thành công, hãy thay đổi chức năng dịch vụ thành lỗi rõ ràng; nếu âm lượng chạy lại
                # Nếu FFmpeg được gọi lại do nhầm lẫn, AppTest sẽ nhận được AssertionError.
                with patch.object(
                    bgm,
                    "validate_bgm_upload",
                    side_effect=AssertionError(
                        "validation repeated during rerun"
                    ),
                ):
                    self._volume_select(app).set_value(0.4).run()

                self.assertEqual([str(item.value) for item in app.exception], [])
                self.assertEqual([item.value for item in app.error], [])
                self.assertEqual(
                    [item.value for item in app.info if "valid.wav" in item.value],
                    [
                        f"{self._translation(locale, 'Background Music Ready')}: "
                        "valid.wav"
                    ],
                )
                self.assertEqual(len(app.get("audio")), 1)

    def test_zero_volume_defers_custom_upload_validation_until_enabled(self):
        """0 vẫn giữ lựa chọn tải lên nhưng phải đợi cho đến khi bật lại BGM trước khi xác minh và xem trước."""
        app = self._open_custom_bgm_panel("en")
        self._volume_select(app).set_value(0.0).run()

        with patch.object(bgm, "validate_bgm_upload") as validation:
            self._uploader(app).set_value(
                ("deferred.wav", _valid_wav_bytes(), "audio/wav")
            ).run()

        validation.assert_not_called()
        self.assertEqual([str(item.value) for item in app.exception], [])
        self.assertEqual([item.value for item in app.error], [])
        self.assertFalse(any("deferred.wav" in item.value for item in app.info))
        self.assertEqual(len(app.get("audio")), 0)

        # Tệp vẫn còn trong phiên Streamlit. Sau khi người dùng tăng âm lượng, quá trình chạy lại tương tự sẽ tự động được thực hiện
        # Quá trình xác minh hoàn tất và trình phát được hiển thị mà không cần chọn lại tệp.
        with patch.object(bgm, "validate_bgm_upload") as validation:
            self._volume_select(app).set_value(0.2).run()

        validation.assert_called_once()
        self.assertEqual([str(item.value) for item in app.exception], [])
        self.assertEqual([item.value for item in app.error], [])
        self.assertTrue(any("deferred.wav" in item.value for item in app.info))
        self.assertEqual(len(app.get("audio")), 1)

    def test_service_failure_is_not_reported_as_invalid_user_audio(self):
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                app = self._open_custom_bgm_panel(locale)
                with patch.object(
                    bgm,
                    "validate_bgm_upload",
                    side_effect=bgm.BgmServiceError("FFmpeg unavailable"),
                ):
                    self._uploader(app).set_value(
                        ("valid.wav", _valid_wav_bytes(), "audio/wav")
                    ).run()

                self.assertEqual([str(item.value) for item in app.exception], [])
                self.assertEqual(
                    [item.value for item in app.error],
                    [
                        self._translation(
                            locale, "Background Music Validation Failed"
                        )
                    ],
                )
                self.assertEqual(len(app.get("audio")), 0)

    def test_sonilo_source_shows_masked_prefilled_key_and_optional_prompt(self):
        """Sau khi chọn Sonilo, Khóa cục bộ sẽ được điền lại và chế độ hiển thị mật khẩu phải được duy trì."""
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                test_config = dict(config.app, sonilo_api_key="saved-test-key")
                with (
                    patch.object(config, "app", test_config),
                    patch.object(config, "save_config"),
                ):
                    app = self._open_sonilo_bgm_panel(locale)

                api_key_input = self._widget_by_key(
                    app.text_input, "sonilo_api_key_input"
                )
                prompt_input = self._widget_by_key(
                    app.text_input, "sonilo_bgm_prompt_input"
                )
                self.assertEqual(api_key_input.value, "saved-test-key")
                self.assertEqual(
                    api_key_input.label,
                    self._translation(locale, "Sonilo API Key"),
                )
                self.assertIn("platform.sonilo.com", api_key_input.label)
                # Element.type của AppTest đại diện cho loại điều khiển (text_input); chế độ mật khẩu
                # Được lưu trong bảng liệt kê protobuf cơ bản, trường này phải được kiểm tra để xác minh kết xuất thực.
                self.assertEqual(
                    api_key_input.proto.type, api_key_input.proto.PASSWORD
                )
                self.assertFalse(getattr(api_key_input.proto, "help", ""))
                self.assertEqual(prompt_input.value, "")
                self.assertEqual([str(item.value) for item in app.exception], [])

    def test_sonilo_connection_button_reports_success(self):
        test_config = dict(config.app, sonilo_api_key="saved-test-key")
        with (
            patch.object(config, "app", test_config),
            patch.object(config, "save_config"),
            patch.object(sonilo, "test_connection", return_value={}) as connection,
        ):
            app = self._open_sonilo_bgm_panel("en")
            button = self._widget_by_key(
                app.button, "test_sonilo_connection_button"
            )
            button.click().run()

        connection.assert_called_once_with()
        self.assertIn(
            self._translation("en", "Sonilo Connection Test Succeeded"),
            [item.value for item in app.success],
        )

    def test_zero_volume_does_not_require_sonilo_key(self):
        """WebUI không được tiếp tục hiển thị cảnh báo yêu cầu Khóa API khi âm lượng Sonilo bằng 0."""
        test_config = dict(config.app, sonilo_api_key="")
        # Âm lượng BGM hiện là tùy chọn ưa thích của người dùng. Được đưa ra rõ ràng cho bài kiểm tra này
        # Các điều kiện ban đầu khác 0 để ngăn các giá trị mặc định được lưu bởi các phiên AppTest khác ảnh hưởng đến các xác nhận trước.
        test_ui = dict(config.ui, bgm_volume=0.2)
        required_warning = self._translation("en", "Sonilo API Key Required")
        with (
            patch.object(config, "app", test_config),
            patch.object(config, "ui", test_ui),
            patch.object(config, "save_config"),
            patch.object(sonilo, "is_enabled", return_value=False),
        ):
            app = self._open_sonilo_bgm_panel("en")
            self.assertIn(required_warning, [item.value for item in app.warning])
            self._volume_select(app).set_value(0.0).run()

        self.assertNotIn(required_warning, [item.value for item in app.warning])
        self.assertEqual([str(item.value) for item in app.exception], [])

    def test_elevenlabs_source_reuses_masked_tts_key_and_shows_prompt(self):
        """Nhạc phim và TTS phải chia sẻ Khóa và duy trì mục nhập mật khẩu cũng như cấu hình mô hình âm nhạc độc lập."""
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                test_config = dict(
                    config.elevenlabs,
                    api_key="saved-elevenlabs-key",
                    model_id="eleven_multilingual_v2",
                    music_model_id="music_v2",
                )
                with (
                    patch.object(config, "elevenlabs", test_config),
                    patch.object(config, "save_config"),
                ):
                    app = self._open_elevenlabs_bgm_panel(locale)

                api_key_input = self._widget_by_key(
                    app.text_input, "elevenlabs_api_key_input"
                )
                prompt_input = self._widget_by_key(
                    app.text_input, "elevenlabs_music_prompt_input"
                )
                self.assertEqual(api_key_input.value, "saved-elevenlabs-key")
                self.assertEqual(
                    api_key_input.label,
                    self._translation(locale, "ElevenLabs Music API Key"),
                )
                self.assertIn(
                    "elevenlabs.io/app/settings/api-keys",
                    api_key_input.label,
                )
                self.assertEqual(
                    api_key_input.proto.type, api_key_input.proto.PASSWORD
                )
                self.assertFalse(getattr(api_key_input.proto, "help", ""))
                self.assertEqual(prompt_input.value, "")
                self.assertEqual(
                    test_config["model_id"], "eleven_multilingual_v2"
                )
                self.assertEqual([str(item.value) for item in app.exception], [])

    def test_elevenlabs_tts_and_music_share_one_api_key_widget(self):
        """Khi lồng tiếng và nhạc phim được bật cùng lúc, chỉ có thể tồn tại một trạng thái Khóa và không thể ghi đè bằng giá trị cũ sau khi sửa đổi."""
        test_config = dict(config.elevenlabs, api_key="key-A")
        test_ui = dict(config.ui, voice_mode="tts")
        with (
            patch.object(config, "elevenlabs", test_config),
            patch.object(config, "ui", test_ui),
            patch.object(config, "save_config"),
            patch.object(voice, "get_elevenlabs_voices", return_value=[]),
        ):
            app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
            app.session_state["ui_language"] = "en"
            app.run()
            self._widget_by_key(
                app.selectbox, "tts_server_select"
            ).set_value("elevenlabs").run()
            self._widget_by_key(
                app.selectbox, "bgm_type_select"
            ).set_value("elevenlabs").run()

            shared_inputs = [
                item
                for item in app.text_input
                if str(getattr(item, "key", "")).startswith(
                    "elevenlabs_api_key_input"
                )
            ]
            self.assertEqual(len(shared_inputs), 1)
            self.assertEqual(shared_inputs[0].value, "key-A")
            self.assertFalse(
                any(
                    str(getattr(item, "key", "")).startswith(
                        "elevenlabs_music_api_key_input"
                    )
                    for item in app.text_input
                )
            )

            shared_inputs[0].set_value("key-B").run()
            updated_input = self._widget_by_key(
                app.text_input, "elevenlabs_api_key_input"
            )
            self.assertEqual(updated_input.value, "key-B")
            self.assertEqual(test_config["api_key"], "key-B")

        self.assertEqual([str(item.value) for item in app.exception], [])

    def test_elevenlabs_connection_button_reports_success(self):
        test_config = dict(config.elevenlabs, api_key="saved-test-key")
        with (
            patch.object(config, "elevenlabs", test_config),
            patch.object(config, "save_config"),
            patch.object(
                elevenlabs_music, "test_connection", return_value={}
            ) as connection,
        ):
            app = self._open_elevenlabs_bgm_panel("en")
            button = self._widget_by_key(
                app.button, "test_elevenlabs_music_connection_button"
            )
            button.click().run()

        connection.assert_called_once_with()
        self.assertIn(
            self._translation(
                "en", "ElevenLabs Connection Test Succeeded"
            ),
            [item.value for item in app.success],
        )

    def test_elevenlabs_connection_reports_paid_plan_requirement(self):
        """Lỗi ở bậc miễn phí nên sử dụng ngôn ngữ tự nhiên của giao diện hiện tại thay vì hiển thị trực tiếp các ngoại lệ bằng tiếng Anh."""
        for locale in TEST_LOCALES:
            with self.subTest(locale=locale):
                test_config = dict(
                    config.elevenlabs, api_key="saved-test-key"
                )
                with (
                    patch.object(config, "elevenlabs", test_config),
                    patch.object(config, "save_config"),
                    patch.object(
                        elevenlabs_music,
                        "test_connection",
                        side_effect=(
                            elevenlabs_music.ElevenLabsPaidPlanRequiredError(
                                "paid plan required"
                            )
                        ),
                    ),
                ):
                    app = self._open_elevenlabs_bgm_panel(locale)
                    button = self._widget_by_key(
                        app.button,
                        "test_elevenlabs_music_connection_button",
                    )
                    button.click().run()

                self.assertIn(
                    self._translation(
                        locale, "ElevenLabs Paid Plan Required"
                    ),
                    [item.value for item in app.error],
                )

    def test_zero_volume_does_not_require_elevenlabs_key(self):
        """ElevenLabs cũng không nên yêu cầu Key hoặc gọi các dịch vụ trả phí khi âm lượng ở mức 0."""
        test_config = dict(config.elevenlabs, api_key="")
        required_warning = self._translation(
            "en", "ElevenLabs API Key Required"
        )
        with (
            patch.object(config, "elevenlabs", test_config),
            patch.object(config, "save_config"),
            patch.object(
                elevenlabs_music, "is_enabled", return_value=False
            ),
        ):
            app = self._open_elevenlabs_bgm_panel("en")
            self.assertIn(required_warning, [item.value for item in app.warning])
            self._volume_select(app).set_value(0.0).run()

        self.assertNotIn(required_warning, [item.value for item in app.warning])
        self.assertEqual([str(item.value) for item in app.exception], [])


if __name__ == "__main__":
    unittest.main()
