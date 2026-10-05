import asyncio
import base64
import os
import shutil
import unittest
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.utils import utils
from app.services import voice as vs
from app.services import task as task_service
from pydub import AudioSegment

temp_dir = utils.storage_dir("temp")

text_en = """
What is the meaning of life? 
This question has puzzled philosophers, scientists, and thinkers of all kinds for centuries. 
Throughout history, various cultures and individuals have come up with their interpretations and beliefs around the purpose of life. 
Some say it's to seek happiness and self-fulfillment, while others believe it's about contributing to the welfare of others and making a positive impact in the world. 
Despite the myriad of perspectives, one thing remains clear: the meaning of life is a deeply personal concept that varies from one person to another. 
It's an existential inquiry that encourages us to reflect on our values, desires, and the essence of our existence.
"""

text_zh = """
预计未来3天深圳冷空气活动频繁，未来两天持续阴天有小雨，出门带好雨具；
10-11日持续阴天有小雨，日温差小，气温在13-17℃之间，体感阴凉；
12日天气短暂好转，早晚清凉；
"""

voice_rate=1.0
voice_volume=1.0
RUN_INTEGRATION_TESTS = os.environ.get("MPT_RUN_INTEGRATION_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}
                    
class TestVoiceService(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
    
    def tearDown(self):
        self.loop.close()

    def test_get_all_azure_voices(self):
        voices = vs.get_all_azure_voices()
        # Dữ liệu đã được di chuyển từ chuỗi nội tuyến sang azure_voices.json để đảm bảo dữ liệu vẫn tải hoàn toàn
        self.assertEqual(len(voices), 331)
        # Kết quả phải ở định dạng "Tên-Giới tính" và được sắp xếp
        self.assertEqual(voices, sorted(voices))
        for v in voices:
            self.assertTrue(v.endswith("-Male") or v.endswith("-Female"))

    def test_get_all_azure_voices_filtered(self):
        filtered = vs.get_all_azure_voices(filter_locals=["zh-CN", "en-US"])
        self.assertTrue(len(filtered) > 0)
        self.assertTrue(
            all(v.startswith(("zh-CN", "en-US")) for v in filtered)
        )

    def test_get_gemini_voices_matches_documented_catalog(self):
        voices = vs.get_gemini_voices()

        self.assertEqual(len(voices), 30)
        self.assertEqual(
            [name for name, _style in vs.GEMINI_TTS_VOICES],
            [
                "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
                "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
                "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome",
                "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam",
                "Schedar", "Gacrux", "Pulcherrima", "Achird",
                "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager",
                "Sulafat",
            ],
        )
        self.assertIn("gemini:Achernar-Soft", voices)
        self.assertIn("gemini:Sulafat-Warm", voices)
        self.assertFalse(any("Atlas" in voice for voice in voices))

    def test_parse_gemini_voice_name_supports_new_and_legacy_labels(self):
        self.assertEqual(
            vs.parse_gemini_voice_name("gemini:Achernar-Soft"), "Achernar"
        )
        self.assertEqual(vs.parse_gemini_voice_name("gemini:Charon-Male"), "Charon")
        self.assertEqual(vs.parse_gemini_voice_name("Charon-Male"), "")

    def test_no_voice_tts_generates_silent_audio_and_subtitle_timeline(self):
        """
        Chế độ không lồng tiếng không gọi bất kỳ nhà cung cấp TTS bên ngoài nào và chỉ tạo ra âm thanh im lặng dưới dạng trình giữ chỗ dòng thời gian.
        Ở đây mô phỏng FFmpeg để xác minh rằng các tham số yêu cầu, tệp đầu ra và cấu trúc phụ đề kế thừa đều tuân thủ các yêu cầu tiếp theo
        Kỳ vọng về các liên kết sáng tác video.
        """

        def fake_run(command, capture_output, text, check):
            self.assertEqual(command[0], "/tmp/fake-ffmpeg")
            self.assertIn("anullsrc=r=44100:cl=mono", command)
            Path(command[-1]).write_bytes(b"fake-silent-mp3")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.utils,
            "get_ffmpeg_binary",
            return_value="/tmp/fake-ffmpeg",
        ), patch.object(vs.subprocess, "run", side_effect=fake_run):
            voice_file = str(Path(tmp_dir) / "silent.mp3")
            sub_maker = vs.tts(
                text="第一句话。Second sentence.",
                voice_name=vs.NO_VOICE_NAME,
                voice_rate=1.0,
                voice_file=voice_file,
            )

            self.assertEqual(Path(voice_file).read_bytes(), b"fake-silent-mp3")

        self.assertIsNotNone(sub_maker)
        self.assertEqual(getattr(sub_maker, "subs", []), ["第一句话", "Second sentence"])
        self.assertEqual(len(getattr(sub_maker, "offset", [])), 2)
        self.assertGreater(vs.get_audio_duration(sub_maker), 0)

    def test_get_audio_duration_accepts_non_mp3_files(self):
        """
        Âm thanh tùy chỉnh (custom_audio_file) thường ở các định dạng không phải mp3 như m4a/wav/aac.
        get_audio_duration không nên báo cáo "Loại mục tiêu không hợp lệ" và trả về 0 chỉ vì tiện ích mở rộng không phải là .mp3.
        Thay vào đó, hãy để moviepy(ffmpeg) đọc thời lượng thực.
        """
        for path in ("custom-audio.m4a", "voice.wav", "clip.aac"):
            with patch.object(vs.os.path, "exists", return_value=True), \
                    patch.object(vs, "AudioFileClip") as mock_afc:
                mock_afc.return_value.__enter__.return_value.duration = 28.89
                self.assertEqual(vs.get_audio_duration(path), 28.89)
                mock_afc.assert_called_once_with(path)

    def test_get_audio_duration_missing_file_returns_zero(self):
        """Trả về 0 một cách an toàn nếu tệp âm thanh không tồn tại, thay vì đưa ra ngoại lệ hoặc không đọc được."""
        with patch.object(vs.os.path, "exists", return_value=False):
            self.assertEqual(vs.get_audio_duration("does-not-exist.m4a"), 0.0)

    def test_no_voice_alias_none_is_supported_temporarily(self):
        """
        Tương thích với không có trọng điểm nào được sử dụng trong PR #981 để tránh một số ít người dùng gọi trực tiếp API
        Không hợp lệ ngay sau khi nâng cấp. Giao diện người dùng mới và mã mới vẫn sử dụng thống nhất không có giọng nói.
        """
        self.assertTrue(vs.is_no_voice("none"))
        self.assertTrue(vs.is_no_voice(vs.NO_VOICE_NAME))
        self.assertFalse(vs.is_no_voice(""))

    def test_no_voice_duration_estimates_non_ascii_languages(self):
        """
        Không lồng tiếng, không có âm thanh TTS thực, thời gian đọc chỉ có thể ước tính dựa trên văn bản kịch bản. tiếng Nga, tiếng Ả Rập,
        Văn bản không phải ASCII như tiếng Nhật Kana và tiếng Hàn cũng phải được đưa vào ước tính và tất cả không thể dài tối thiểu 3 giây.
        """
        russian_text = (
            "Это длинный тестовый сценарий без озвучки. "
            "Он должен получить достаточно времени для чтения субтитров."
        )
        arabic_text = "هذا اختبار طويل بدون تعليق صوتي، ويجب أن يحصل على وقت كاف لقراءة الترجمة."

        self.assertGreater(vs.estimate_no_voice_duration(russian_text), 8.0)
        self.assertGreater(vs.estimate_no_voice_duration(arabic_text), 8.0)

    def test_generate_silent_audio_rejects_missing_output_file(self):
        """
        Ngay cả khi quá trình FFmpeg trả về thành công, hãy đảm bảo tệp đầu ra thực sự tồn tại và không trống. Bằng cách này bạn có thể
        Sự hội tụ bất thường được bộc lộ trong giai đoạn TTS thay vì bị trì hoãn cho đến giai đoạn tổng hợp video tiếp theo.
        """
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.utils,
            "get_ffmpeg_binary",
            return_value="/tmp/fake-ffmpeg",
        ), patch.object(
            vs.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            voice_file = str(Path(tmp_dir) / "missing-silent.mp3")

            self.assertFalse(vs.generate_silent_audio(3.0, voice_file))

    def test_empty_voice_name_does_not_enable_no_voice_mode(self):
        """
        Giọng trống thường có nghĩa là thiếu cấu hình hoặc thông số giao diện sai và không thể tự động chuyển sang chế độ không lồng tiếng.
        Ngược lại, nếu người dùng điền sai cấu hình TTS, họ cũng sẽ nhận được video tắt tiếng "thành công" và chi phí định vị sẽ cao hơn.
        """
        sentinel = object()

        with patch.object(vs, "azure_tts_v1", return_value=sentinel) as azure_tts_v1:
            result = vs.tts(
                text="empty voice should still use the default TTS path",
                voice_name="",
                voice_rate=1.0,
                voice_file="/tmp/empty-voice.mp3",
            )

        self.assertIs(result, sentinel)
        azure_tts_v1.assert_called_once()

    @unittest.skipUnless(
        RUN_INTEGRATION_TESTS,
        "MPT_RUN_INTEGRATION_TESTS not set",
    )
    def test_siliconflow(self):
        # Khóa API của SiliconFlow tồn tại trong [siliconflow].api_key và mã thời gian chạy cũng từ
        # config.siliconflow đọc; nguồn cấu hình tương tự phải được sử dụng ở đây để tránh thông tin đăng nhập cấu hình không chính xác
        # Các bài kiểm tra vẫn đang bị bỏ qua do nhầm lẫn.
        if not vs.config.siliconflow.get("api_key"):
            self.skipTest("siliconflow_api_key is not configured")

        voice_name = "siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex-Male"
        voice_name = vs.parse_voice_name(voice_name)
        
        async def _do():
            parts = voice_name.split(":")
            if len(parts) >= 3:
                model = parts[1]
                # Xóa hậu tố giới tính, chẳng hạn như "alex-Male" -> "alex"
                voice_with_gender = parts[2]
                voice = voice_with_gender.split("-")[0]
                # Xây dựng các tham số giọng nói hoàn chỉnh theo định dạng "model:voice"
                full_voice = f"{model}:{voice}"
                voice_file = f"{temp_dir}/tts-siliconflow-{voice}.mp3"
                subtitle_file = f"{temp_dir}/tts-siliconflow-{voice}.srt"
                sub_maker = vs.siliconflow_tts(
                    text=text_zh, model=model, voice=full_voice, voice_file=voice_file, voice_rate=voice_rate, voice_volume=voice_volume
                )
                if not sub_maker:
                    self.fail("siliconflow tts failed")
                vs.create_subtitle(sub_maker=sub_maker, text=text_zh, subtitle_file=subtitle_file)
                audio_duration = vs.get_audio_duration(sub_maker)
                print(f"voice: {voice_name}, audio duration: {audio_duration}s")
            else:
                self.fail("siliconflow invalid voice name")

        self.loop.run_until_complete(_do())
    
    @unittest.skipUnless(
        RUN_INTEGRATION_TESTS,
        "MPT_RUN_INTEGRATION_TESTS not set",
    )
    def test_azure_tts_v1(self):
        voice_name = "zh-CN-XiaoyiNeural-Female"
        voice_name = vs.parse_voice_name(voice_name)
        print(voice_name)
        
        voice_file = f"{temp_dir}/tts-azure-v1-{voice_name}.mp3"
        subtitle_file = f"{temp_dir}/tts-azure-v1-{voice_name}.srt"
        sub_maker = vs.azure_tts_v1(
            text=text_zh, voice_name=voice_name, voice_file=voice_file, voice_rate=voice_rate
        )
        if not sub_maker:
            self.fail("azure tts v1 failed")
        vs.create_subtitle(sub_maker=sub_maker, text=text_zh, subtitle_file=subtitle_file)
        audio_duration = vs.get_audio_duration(sub_maker)
        print(f"voice: {voice_name}, audio duration: {audio_duration}s")

    def test_azure_tts_v1_supports_legacy_edge_tts_without_boundary(self):
        """
        Xác minh rằng Azure TTS V1 tiếp tục hoạt động với các phần phụ thuộc edge_tts cũ còn lại.

        Kịch bản hồi quy này tương ứng với thực tế là sau khi cập nhật gói di động Windows không thành công, môi trường tại chỗ vẫn ở phiên bản cũ.
        Trong trường hợp edge_tts:
        1. `Communicate.__init__()` không chấp nhận `ranh`
        2. Chỉ có `stream()` không đồng bộ, không có `stream_sync()`
        """

        class _LegacyCommunicate:
            def __init__(self, text, voice, rate="+0%"):
                self.text = text
                self.voice = voice
                self.rate = rate

            async def stream(self):
                yield {"type": "audio", "data": b"legacy-audio"}
                yield {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 10000000,
                    "text": "legacy",
                }

        class _FakeSubMaker:
            def __init__(self):
                self.events = []

            def feed(self, chunk):
                self.events.append(chunk)

            def get_srt(self):
                if not self.events:
                    return ""
                return "1\n00:00:00,000 --> 00:00:01,000\nlegacy\n"

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.edge_tts, "Communicate", _LegacyCommunicate
        ), patch.object(vs.edge_tts, "SubMaker", _FakeSubMaker):
            voice_file = str(Path(tmp_dir) / "legacy-edge-tts.mp3")
            sub_maker = vs.azure_tts_v1(
                text="legacy edge tts compatibility",
                voice_name="zh-CN-XiaoyiNeural-Female",
                voice_file=voice_file,
                voice_rate=1.0,
            )

            self.assertIsNotNone(sub_maker)
            self.assertEqual(Path(voice_file).read_bytes(), b"legacy-audio")
            self.assertEqual(len(sub_maker.events), 1)
            self.assertEqual(sub_maker.events[0]["type"], "WordBoundary")

    def test_azure_tts_v1_times_out_hanging_stream_sync(self):
        """
        Đã xác minh rằng Azure TTS V1 nhanh chóng bị lỗi khi luồng đồng bộ hóa edge_tts bị kẹt.

        Trong cảnh thực, khi mạng không bình thường, máy chủ bị điều tiết hoặc ngôn ngữ giọng nói không khớp với văn bản,
        `stream_sync()` có thể không quay lại trong một thời gian dài, khiến tác vụ WebUI dừng ở
        `bắt đầu, tên giọng nói...`. Ở đây chúng tôi sử dụng luồng giả bị chặn để tái tạo cảnh,
        Việc xác nhận bảo vệ thời gian chờ sẽ khiến hàm chấm dứt và trả về Không.
        """

        class _HangingCommunicate:
            def __init__(self, text, voice, rate="+0%", boundary=None):
                self.text = text
                self.voice = voice
                self.rate = rate
                self.boundary = boundary

            def stream_sync(self):
                time.sleep(10)
                yield {"type": "audio", "data": b"unreachable"}

        class _FakeSubMaker:
            def feed(self, chunk):
                return None

            def get_srt(self):
                return ""

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.edge_tts, "Communicate", _HangingCommunicate
        ), patch.object(vs.edge_tts, "SubMaker", _FakeSubMaker), patch.object(
            vs.config,
            "app",
            dict(vs.config.app, edge_tts_timeout=0.05),
        ):
            voice_file = Path(tmp_dir) / "hanging-edge-tts.mp3"
            started_at = time.monotonic()
            sub_maker = vs.azure_tts_v1(
                text="帮我生成一个花开花落的视频",
                voice_name="en-AU-NatashaNeural-Female",
                voice_file=str(voice_file),
                voice_rate=1.0,
            )
            elapsed = time.monotonic() - started_at
            self.assertFalse(voice_file.exists())

        self.assertIsNone(sub_maker)
        self.assertLess(elapsed, 2)

    @unittest.skipUnless(
        RUN_INTEGRATION_TESTS,
        "MPT_RUN_INTEGRATION_TESTS not set",
    )
    def test_azure_tts_v2(self):
        if not vs.config.azure.get("speech_key") or not vs.config.azure.get("speech_region"):
            self.skipTest("Azure speech key or region is not configured")

        voice_name = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female"
        voice_name = vs.parse_voice_name(voice_name)
        print(voice_name)

        async def _do():
            voice_file = f"{temp_dir}/tts-azure-v2-{voice_name}.mp3"
            subtitle_file = f"{temp_dir}/tts-azure-v2-{voice_name}.srt"
            sub_maker = vs.azure_tts_v2(
                text=text_zh,
                voice_name=voice_name,
                voice_file=voice_file,
                voice_rate=1.0,
            )
            if not sub_maker:
                self.fail("azure tts v2 failed")
            vs.create_subtitle(sub_maker=sub_maker, text=text_zh, subtitle_file=subtitle_file)
            audio_duration = vs.get_audio_duration(sub_maker)
            print(f"voice: {voice_name}, audio duration: {audio_duration}s")

        self.loop.run_until_complete(_do())

    def test_azure_tts_v2_ssml_applies_rate_and_escapes_text(self):
        """Azure V2 phải áp dụng tốc độ giọng nói qua SSML và tránh việc bản sao của người dùng làm hỏng XML."""
        ssml = vs._build_azure_v2_ssml(
            text='A < B & "quoted"',
            voice_name="zh-CN-XiaoxiaoMultilingualNeural",
            voice_rate=1.8,
        )

        self.assertIn('xml:lang="zh-CN"', ssml)
        self.assertIn('rate="1.8"', ssml)
        self.assertIn("A &lt; B &amp; \"quoted\"", ssml)

    def test_tts_forwards_rate_to_azure_v2(self):
        """Mục nhập TTS hợp nhất không thể mất voice_rate khi phân phối Azure V2."""
        voice_name = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female"
        with patch.object(vs, "azure_tts_v2", return_value=object()) as mock_tts:
            result = vs.tts(
                text="语速测试",
                voice_name=voice_name,
                voice_rate=1.8,
                voice_file="/tmp/azure-v2-rate.mp3",
            )

        self.assertIsNotNone(result)
        mock_tts.assert_called_once_with(
            "语速测试",
            voice_name,
            "/tmp/azure-v2-rate.mp3",
            voice_rate=1.8,
        )

    def test_tts_strips_gemini_style_metadata_before_dispatch(self):
        """Mô tả kiểu chính thức cho hộp thả xuống Gemini không thể là một phần của API voice_name."""
        sentinel = object()

        with patch.object(vs, "gemini_tts", return_value=sentinel) as gemini_tts:
            result = vs.tts(
                text="Test the updated voice catalog.",
                voice_name="gemini:Achernar-Soft",
                voice_rate=1.0,
                voice_file="/tmp/gemini-achernar.mp3",
                voice_volume=1.0,
            )

        self.assertIs(result, sentinel)
        gemini_tts.assert_called_once_with(
            "Test the updated voice catalog.",
            "Achernar",
            1.0,
            "/tmp/gemini-achernar.mp3",
            1.0,
        )

    def test_gemini_tts_uses_google_genai_and_compatible_submaker_fields(self):
        """
        Đã xác minh rằng Gemini TTS vẫn trả về cấu trúc phụ đề tương thích với dự án trong môi trường edge_tts 7.x,
        Và có thể được sử dụng trực tiếp bởi liên kết tạo phụ đề của `subtitle_provider=edge`,
        Tránh quay lại Whisper lần nữa. Đồng thời sử dụng thư mục đầu ra lồng nhau không tồn tại, ghi đè API hoặc
        Có một trường hợp khó khăn khi CLI gọi trực tiếp dịch vụ mà không tạo trước thư mục tác vụ.
        """

        class _InlineData:
            def __init__(self, data):
                self.data = data

        class _Part:
            def __init__(self, data):
                self.inline_data = _InlineData(data)

        class _Content:
            def __init__(self, data):
                self.parts = [_Part(data)]

        class _Candidate:
            def __init__(self, data):
                self.content = _Content(data)

        class _Response:
            def __init__(self, data):
                self.candidates = [_Candidate(data)]

        captured = {}

        class _FakeModels:
            def generate_content(self, **kwargs):
                captured.update(kwargs)
                tone = (
                    AudioSegment.silent(duration=1800)
                    .set_frame_rate(24000)
                    .set_channels(1)
                    .set_sample_width(2)
                )
                return _Response(tone.raw_data)

        class _FakeClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.models = _FakeModels()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                captured["closed"] = True

        temp_root = Path(tempfile.mkdtemp(prefix="gemini-tts-output-"))
        self.addCleanup(shutil.rmtree, temp_root, True)
        output_dir = temp_root / "nested" / "audio"
        voice_file = str(output_dir / "tts-gemini-Zephyr.mp3")
        subtitle_file = str(output_dir / "tts-gemini-Zephyr.srt")
        text = "Gemini subtitle generation should work now. Testing multiple lines."

        self.assertFalse(output_dir.exists())

        with patch("google.genai.Client", _FakeClient), patch.object(
            vs.config,
            "app",
            dict(vs.config.app, gemini_api_key="test-key"),
        ):
            sub_maker = vs.gemini_tts(
                text=text,
                voice_name="Zephyr",
                voice_rate=1.0,
                voice_file=voice_file,
            )

        self.assertIsNotNone(sub_maker)
        self.assertTrue(Path(voice_file).is_file())
        self.assertEqual(
            getattr(sub_maker, "subs", []),
            ["Gemini subtitle generation should work now", "Testing multiple lines"],
        )
        self.assertEqual(len(getattr(sub_maker, "offset", [])), 2)
        self.assertEqual(sub_maker.offset[0][0], 0)
        self.assertLess(sub_maker.offset[0][1], sub_maker.offset[1][1])
        self.assertEqual(captured["client_kwargs"], {"api_key": "test-key"})
        self.assertEqual(captured["model"], "gemini-2.5-flash-preview-tts")
        self.assertEqual(captured["contents"], text)
        self.assertEqual(captured["config"].response_modalities, ["AUDIO"])
        voice_config = captured["config"].speech_config.voice_config
        self.assertEqual(
            voice_config.prebuilt_voice_config.voice_name,
            "Zephyr",
        )
        self.assertTrue(captured["closed"])

        vs.create_subtitle(sub_maker=sub_maker, text=text, subtitle_file=subtitle_file)
        subtitle_content = Path(subtitle_file).read_text(encoding="utf-8")
        self.assertIn("Gemini subtitle generation should work now", subtitle_content)
        self.assertIn("Testing multiple lines", subtitle_content)

    def test_mimo_tts_uses_openai_compatible_audio_response(self):
        """
        Xác minh rằng Xiaomi MiMo TTS có thể sử dụng cấu trúc phản hồi âm thanh tương thích với OpenAI.

        Ở đây, ứng dụng khách OpenAI giả và AudioSegment giả được sử dụng để bao phủ mạng thực và ffmpeg.
        Xác nhận rằng mã thời gian chạy sẽ đưa văn bản được tổng hợp vào tin nhắn trợ lý và đặt văn bản được trả về
        Xuất âm thanh WAV base64 sang tệp âm thanh để sử dụng sau này trong dự án.
        """

        class _FakeAudio:
            def __init__(self):
                self.data = base64.b64encode(b"RIFF-fake-wav").decode("utf-8")

        class _FakeMessage:
            def __init__(self):
                self.audio = _FakeAudio()

        class _FakeChoice:
            def __init__(self):
                self.message = _FakeMessage()

        class _FakeCompletion:
            def __init__(self):
                self.choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return _FakeCompletion()

        class _FakeAudioSegment:
            def __len__(self):
                return 1800

            def export(self, output_file, format):
                Path(output_file).write_bytes(b"fake-mp3")

        fake_completions = _FakeCompletions()
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=fake_completions)
        )

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs,
            "OpenAI",
            return_value=fake_client,
        ) as openai_client, patch(
            "pydub.AudioSegment.from_file",
            return_value=_FakeAudioSegment(),
        ), patch.object(
            vs.config,
            "app",
            dict(
                vs.config.app,
                mimo_api_key="mimo-key",
                mimo_base_url="https://api.xiaomimimo.com/v1",
                mimo_tts_model_name="mimo-v2.5-tts",
                mimo_tts_style_prompt="用清晰的中文旁白朗读。",
            ),
        ):
            voice_file = str(Path(tmp_dir) / "mimo-tts.mp3")
            sub_maker = vs.mimo_tts(
                text="小米语音合成测试。第二句话。",
                voice_name="冰糖",
                voice_rate=1.0,
                voice_file=voice_file,
                voice_volume=1.0,
            )
            generated_audio = Path(voice_file).read_bytes()

        openai_client.assert_called_once_with(
            api_key="mimo-key",
            base_url="https://api.xiaomimimo.com/v1",
        )
        self.assertEqual(fake_completions.kwargs["model"], "mimo-v2.5-tts")
        self.assertEqual(
            fake_completions.kwargs["messages"],
            [
                {"role": "user", "content": "用清晰的中文旁白朗读。"},
                {"role": "assistant", "content": "小米语音合成测试。第二句话。"},
            ],
        )
        self.assertEqual(
            fake_completions.kwargs["audio"],
            {"format": "wav", "voice": "冰糖"},
        )
        self.assertEqual(generated_audio, b"fake-mp3")
        self.assertIsNotNone(sub_maker)
        self.assertEqual(getattr(sub_maker, "subs", []), ["小米语音合成测试", "第二句话"])
        self.assertEqual(len(getattr(sub_maker, "offset", [])), 2)

    def test_minimax_tts_uses_regional_endpoint_and_hex_audio(self):
        class _Response:
            status_code, text = 200, ""

            @staticmethod
            def json():
                return {"data": {"audio": b"audio".hex(), "status": 2}, "base_resp": {"status_code": 0}}

        class _Clip:
            duration = 2.5

            def close(self):
                pass

        captured = {}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return _Response()

        settings = {
            "api_key": "test-key", "base_url": vs.MINIMAX_TTS_CN_URL,
            "model_id": "speech-2.8-turbo", "voice_id": "male-qn-qingse",
            "sample_rate": 32000, "bitrate": 128000, "audio_format": "mp3", "channel": 1,
        }
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.config, "minimax_tts", settings
        ), patch.object(vs.requests, "post", side_effect=_post), patch.object(
            vs, "AudioFileClip", return_value=_Clip()
        ):
            voice_file = str(Path(tmp_dir) / "minimax.mp3")
            result = vs.minimax_tts("Speech test.", "male-qn-qingse", 1.2, voice_file, 1.5)
            self.assertEqual(Path(voice_file).read_bytes(), b"audio")

        self.assertIsNotNone(result)
        self.assertEqual(captured["url"], "https://api.minimaxi.com/v1/t2a_v2")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(captured["json"]["model"], "speech-2.8-turbo")
        self.assertEqual(captured["json"]["text"], "Speech test.")
        self.assertEqual(captured["json"]["voice_setting"]["voice_id"], "male-qn-qingse")
        self.assertEqual(captured["json"]["audio_setting"]["format"], "mp3")

    def test_minimax_tts_reuses_cn_llm_key_and_endpoint(self):
        """Khi TTS không được định cấu hình riêng biệt, thông tin xác thực và địa chỉ MiniMax LLM trong cùng khu vực sẽ được sử dụng lại."""
        class _Response:
            status_code, text = 200, ""

            @staticmethod
            def json():
                return {"data": {"audio": b"audio".hex(), "status": 2}, "base_resp": {"status_code": 0}}

        class _Clip:
            duration = 1.25

            def close(self):
                pass

        captured = {}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, headers=headers)
            return _Response()

        settings = {
            "api_key": "", "base_url": vs.MINIMAX_TTS_GLOBAL_URL,
            "model_id": vs.MINIMAX_TTS_DEFAULT_MODEL, "audio_format": "mp3",
        }
        app_settings = {
            "minimax_api_key": "shared-cn-key",
            "minimax_base_url": "https://api.minimaxi.com/v1",
        }
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.config, "minimax_tts", settings
        ), patch.object(vs.config, "app", app_settings), patch.object(
            vs.requests, "post", side_effect=_post
        ), patch.object(vs, "AudioFileClip", return_value=_Clip()):
            voice_file = str(Path(tmp_dir) / "minimax.mp3")
            result = vs.minimax_tts("测试。", "male-qn-qingse", 1.0, voice_file)

        self.assertIsNotNone(result)
        self.assertEqual(captured["url"], vs.MINIMAX_TTS_CN_URL)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer shared-cn-key")

    def test_get_minimax_voice_catalog_normalizes_all_voice_types(self):
        """Truy vấn giọng nói phải thống nhất cấu trúc phản hồi giữa các nguồn và bỏ qua ID giọng nói trùng lặp hoặc trống."""

        class _Response:
            status_code, text = 200, ""

            @staticmethod
            def json():
                return {
                    "system_voice": [
                        {"voice_id": "system-1", "voice_name": "系统音色"},
                        {"voice_id": "", "voice_name": "无效音色"},
                    ],
                    "voice_cloning": [
                        {"voice_id": "clone-1", "voice_name": "我的克隆音色"},
                        {"voice_id": "system-1", "voice_name": "重复音色"},
                    ],
                    "voice_generation": [{"voice_id": "generated-1"}],
                    "base_resp": {"status_code": "0"},
                }

        with patch.object(vs.requests, "post", return_value=_Response()) as post:
            catalog = vs.get_minimax_voice_catalog(
                api_key="test-key",
                endpoint=vs.MINIMAX_TTS_CN_URL,
            )

        post.assert_called_once_with(
            "https://api.minimaxi.com/v1/get_voice",
            json={"voice_type": "all"},
            headers={
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        self.assertEqual(
            catalog,
            [
                {
                    "voice_id": "system-1",
                    "voice_name": "系统音色",
                    "voice_type": "system",
                },
                {
                    "voice_id": "clone-1",
                    "voice_name": "我的克隆音色",
                    "voice_type": "voice_cloning",
                },
                {
                    "voice_id": "generated-1",
                    "voice_name": "generated-1",
                    "voice_type": "voice_generation",
                },
            ],
        )

    def test_get_minimax_voice_catalog_exposes_provider_error(self):
        """Các lỗi kinh doanh từ xa phải được trình bày rõ ràng và không thể ngụy trang vì tài khoản không có sẵn âm thanh."""

        class _Response:
            status_code, text = 200, ""

            @staticmethod
            def json():
                return {
                    "base_resp": {
                        "status_code": 1004,
                        "status_msg": "invalid api key",
                    }
                }

        with patch.object(vs.requests, "post", return_value=_Response()):
            with self.assertRaisesRegex(RuntimeError, "invalid api key"):
                vs.get_minimax_voice_catalog(api_key="invalid-key")

    def test_minimax_tts_does_not_leave_invalid_audio_output(self):
        """Các tệp hiện có không được ghi đè hoặc để lại các tệp tạm thời khi không thể phân tích cú pháp âm thanh phản hồi."""
        class _Response:
            status_code, text = 200, ""

            @staticmethod
            def json():
                return {"data": {"audio": b"invalid-audio".hex(), "status": 2}, "base_resp": {"status_code": 0}}

        settings = {
            "api_key": "test-key", "base_url": vs.MINIMAX_TTS_GLOBAL_URL,
            "model_id": vs.MINIMAX_TTS_DEFAULT_MODEL, "audio_format": "mp3",
        }
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.config, "minimax_tts", settings
        ), patch.object(vs.requests, "post", return_value=_Response()), patch.object(
            vs, "AudioFileClip", side_effect=OSError("invalid audio")
        ):
            voice_path = Path(tmp_dir) / "minimax.mp3"
            voice_path.write_bytes(b"existing-audio")
            result = vs.minimax_tts("Speech test.", "English_expressive_narrator", 1.0, str(voice_path))

            self.assertIsNone(result)
            self.assertEqual(voice_path.read_bytes(), b"existing-audio")
            self.assertEqual([path.name for path in Path(tmp_dir).iterdir()], ["minimax.mp3"])

    def test_minimax_voice_helpers_and_dispatch(self):
        with patch.object(vs.config, "minimax_tts", {"voice_id": "narrator"}):
            self.assertEqual(vs.get_minimax_voices(), ["minimax:narrator"])
        self.assertEqual(vs.get_minimax_voices("custom-voice"), ["minimax:custom-voice"])
        self.assertTrue(vs.is_minimax_voice("minimax:narrator"))
        sentinel = object()
        with patch.object(vs, "minimax_tts", return_value=sentinel) as implementation:
            result = vs.tts("test", "minimax:narrator", 1.0, "voice.mp3", 1.0)
        self.assertIs(result, sentinel)
        implementation.assert_called_once_with("test", "narrator", 1.0, "voice.mp3", 1.0)

    def test_chatterbox_voice_helpers(self):
        """is_chatterbox_voice / get_chatterbox_voices basics and normalisation."""
        self.assertTrue(vs.is_chatterbox_voice("chatterbox:default-Female"))
        self.assertFalse(vs.is_chatterbox_voice("elevenlabs:abc:Rachel"))
        self.assertFalse(vs.is_chatterbox_voice(""))
        self.assertFalse(vs.is_chatterbox_voice(None))

        # list entries are normalised to the chatterbox:<name> dispatcher format,
        # and entries that are already prefixed are left untouched
        with patch.object(
            vs.config,
            "chatterbox",
            {"voices": ["narrator-Male", "chatterbox:host"]},
        ):
            self.assertEqual(
                vs.get_chatterbox_voices(),
                ["chatterbox:narrator-Male", "chatterbox:host"],
            )

        # a comma-separated string is also accepted (TOML-friendly)
        with patch.object(vs.config, "chatterbox", {"voices": "alpha, beta ,"}):
            self.assertEqual(
                vs.get_chatterbox_voices(),
                ["chatterbox:alpha", "chatterbox:beta"],
            )

        # with nothing configured the dropdown still gets a usable default
        with patch.object(vs.config, "chatterbox", {}):
            self.assertEqual(vs.get_chatterbox_voices(), ["chatterbox:default-Female"])

    def test_chatterbox_tts_posts_to_openai_compatible_endpoint(self):
        """Success path: POST /audio/speech, write audio, return legacy SubMaker."""

        class _FakeResponse:
            status_code = 200
            content = b"RIFF-fake-wav"
            text = ""

        class _FakeClip:
            duration = 3.5

            def close(self):
                pass

        captured = {}

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.config,
            "chatterbox",
            {
                "base_url": "http://localhost:4123/v1/",
                "api_key": "secret",
                "model_id": "chatterbox",
            },
        ), patch.object(
            vs.requests, "post", side_effect=_fake_post
        ) as post, patch.object(
            vs, "AudioFileClip", return_value=_FakeClip()
        ):
            voice_file = str(Path(tmp_dir) / "chatterbox.mp3")
            sub_maker = vs.chatterbox_tts(
                text="Hello world. Second sentence.",
                voice="default",
                voice_file=voice_file,
                voice_rate=1.2,
                voice_volume=1.0,
            )
            generated_audio = Path(voice_file).read_bytes()

        post.assert_called_once()
        # trailing slash on base_url is stripped before appending /audio/speech
        self.assertEqual(captured["url"], "http://localhost:4123/v1/audio/speech")
        self.assertEqual(captured["json"]["model"], "chatterbox")
        self.assertEqual(captured["json"]["voice"], "default")
        self.assertEqual(captured["json"]["input"], "Hello world. Second sentence.")
        self.assertAlmostEqual(captured["json"]["speed"], 1.2)
        # api_key is forwarded as a bearer token
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer secret")
        # volume is intentionally not part of the OpenAI speech payload
        self.assertNotIn("volume", captured["json"])
        self.assertEqual(generated_audio, b"RIFF-fake-wav")
        self.assertIsNotNone(sub_maker)
        self.assertTrue(getattr(sub_maker, "subs", []))

    def test_chatterbox_tts_requires_base_url(self):
        """Missing base_url short-circuits without any network call."""
        with patch.object(
            vs.config, "chatterbox", {"base_url": ""}
        ), patch.object(vs.requests, "post") as post:
            result = vs.chatterbox_tts(
                text="hi", voice="default", voice_file="unused.mp3"
            )
        self.assertIsNone(result)
        post.assert_not_called()

    def test_chatterbox_tts_returns_none_on_http_error(self):
        """A non-200 response is retried up to 3 times, then fails to None."""

        class _FakeResponse:
            status_code = 500
            content = b""
            text = "boom"

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.config, "chatterbox", {"base_url": "http://localhost:4123/v1"}
        ), patch.object(
            vs.requests, "post", return_value=_FakeResponse()
        ) as post:
            voice_file = str(Path(tmp_dir) / "chatterbox.mp3")
            result = vs.chatterbox_tts(
                text="hi", voice="default", voice_file=voice_file
            )
        self.assertIsNone(result)
        self.assertEqual(post.call_count, 3)

    def _make_broken_clip_class(self, close_calls: list):
        """Return a clip class whose .duration raises and whose .close() records calls."""

        class _BrokenClip:
            @property
            def duration(self):
                raise RuntimeError("FFmpeg probe failed")

            def close(self):
                close_calls.append(True)

        return _BrokenClip

    def test_elevenlabs_tts_audio_clip_closed_on_duration_error(self):
        """AudioFileClip.close() must be called even when reading .duration raises."""
        close_calls: list = []
        BrokenClip = self._make_broken_clip_class(close_calls)

        class _OkResponse:
            status_code = 200
            content = b"fake-mp3"
            text = ""

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out = f.name
        try:
            with (
                patch.object(
                    vs.config,
                    "elevenlabs",
                    {"api_key": "test-key", "model_id": "eleven_multilingual_v2"},
                ),
                patch.object(vs.requests, "post", return_value=_OkResponse()),
                patch.object(vs, "AudioFileClip", side_effect=lambda _: BrokenClip()),
            ):
                result = vs.elevenlabs_tts("Hello world.", "voice-id", out)
        finally:
            if os.path.exists(out):
                os.remove(out)

        self.assertIsNone(result)
        self.assertTrue(close_calls, "AudioFileClip.close() was never called")

    def test_chatterbox_tts_audio_clip_closed_on_duration_error(self):
        """AudioFileClip.close() must be called even when reading .duration raises."""
        close_calls: list = []
        BrokenClip = self._make_broken_clip_class(close_calls)

        class _OkResponse:
            status_code = 200
            content = b"fake-mp3"
            text = ""

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out = f.name
        try:
            with (
                patch.object(
                    vs.config,
                    "chatterbox",
                    {"base_url": "http://localhost:4123", "api_key": "", "model_id": "chatterbox"},
                ),
                patch.object(vs.requests, "post", return_value=_OkResponse()),
                patch.object(vs, "AudioFileClip", side_effect=lambda _: BrokenClip()),
            ):
                result = vs.chatterbox_tts("Hello world.", "default", out)
        finally:
            if os.path.exists(out):
                os.remove(out)

        self.assertIsNone(result)
        self.assertTrue(close_calls, "AudioFileClip.close() was never called")

    def test_fish_audio_tts_audio_clip_closed_on_duration_error(self):
        """AudioFileClip.close() must be called even when reading .duration raises."""
        close_calls: list = []
        BrokenClip = self._make_broken_clip_class(close_calls)

        class _OkResponse:
            status_code = 200
            content = b"x" * 200
            text = ""

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out = f.name
        try:
            with (
                patch.object(
                    vs.config,
                    "fish_audio",
                    {"api_key": "test-key", "model": "s2.1-pro-free"},
                ),
                patch.object(vs.requests, "post", return_value=_OkResponse()),
                patch.object(vs, "AudioFileClip", side_effect=lambda _: BrokenClip()),
            ):
                result = vs.fish_audio_tts("Hello world.", out)
        finally:
            if os.path.exists(out):
                os.remove(out)

        self.assertIsNone(result)
        self.assertTrue(close_calls, "AudioFileClip.close() was never called")

    def test_generate_subtitle_keeps_edge_provider_for_gemini_legacy_submaker(self):
        """
        Xác minh rằng cấu trúc phụ đề cũ được Gemini TTS trả về có thể được xuất trực tiếp theo nhà cung cấp biên
        SRT, sẽ không quay lại Whisper do trận đấu thất bại.
        """
        script = "Gemini subtitle generation should work now. Testing multiple lines."
        sub_maker = vs.populate_legacy_submaker_with_full_text(
            vs.ensure_legacy_submaker_fields(vs.SubMaker()),
            script,
            2.4,
        )

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            task_service.config,
            "app",
            dict(task_service.config.app, subtitle_provider="edge"),
        ), patch("app.services.subtitle.create") as whisper_create, patch(
            "app.utils.utils.task_dir",
            lambda tid="": str(Path(tmp_dir) / tid) if tid else str(Path(tmp_dir)),
        ):
            task_id = "gemini-subtitle-edge-task"
            Path(tmp_dir, task_id).mkdir(parents=True, exist_ok=True)
            subtitle_path = task_service.generate_subtitle(
                task_id=task_id,
                params=type("Params", (), {"subtitle_enabled": True})(),
                video_script=script,
                sub_maker=sub_maker,
                audio_file="",
            )

            self.assertTrue(subtitle_path.endswith("subtitle.srt"))
            self.assertTrue(Path(subtitle_path).exists())
            self.assertFalse(whisper_create.called)
            subtitle_content = Path(subtitle_path).read_text(encoding="utf-8")
            self.assertIn("Gemini subtitle generation should work now", subtitle_content)
            self.assertIn("Testing multiple lines", subtitle_content)

    def test_script_split_keeps_thousand_separator_comma(self):
        """
        Edge TTS sẽ trả về "1.000 năm" dưới dạng văn bản liên tục. Khi ngắt câu trong chữ viết, bạn không thể
        Dấu phẩy tiếng Anh giữa các số được dùng làm ranh giới câu, nếu không việc gộp phụ đề sẽ xuất hiện issue #894
        Số lượng mục con trong đó ít hơn script_lines và lỗi xảy ra với Whisper.
        """
        text = (
            "It takes about 1,000 years for a single drop of water to finish "
            "the whole trip!"
        )

        self.assertEqual(
            utils.split_string_by_punctuations(text),
            [
                (
                    "It takes about 1,000 years for a single drop of water to finish "
                    "the whole trip"
                )
            ],
        )

    def test_edge_cue_aggregation_handles_thousand_separator_comma(self):
        """
        Tái tạo dạng chính của vấn đề #894: câu cuối cùng trong Edge Cues được trả về dưới dạng văn bản liên tục,
        Chứa `1.000 năm`. Việc phân đoạn tập lệnh phải nhất quán với kết quả tổng hợp tín hiệu và không thể
        Chia thành hai phụ đề.
        """
        text = (
            "The ocean isn't just sitting stil, it moves around the world like a massive "
            "amusement park ride! Cold water at the North and South Poles sinks to the "
            "bottom because it is heavy and salty. At the same time, warm water from the "
            "sunny equator flows along the top to take its place. This creates a giant "
            "underwater conveyor belt that travels all the way around the Earth. It takes "
            "about 1,000 years for a single drop of water to finish the whole trip!"
        )
        script_lines = utils.split_string_by_punctuations(text)
        cues = []
        for index, line in enumerate(script_lines):
            # Nội dung gợi ý của Edge thường không có dấu cách và bố cục dấu câu trong tập lệnh. Các khoảng trống được loại bỏ ở đây.
            # để mô phỏng các kịch bản kết hợp chặt chẽ hơn.
            cues.append(
                SimpleNamespace(
                    content=line.replace(" ", ""),
                    start=timedelta(seconds=index),
                    end=timedelta(seconds=index + 0.8),
                )
            )
        sub_maker = SimpleNamespace(cues=cues)

        sub_items = vs._build_subtitle_items_from_edge_cues(sub_maker, script_lines)

        self.assertEqual(len(sub_items), len(script_lines))
        self.assertIn("1,000 years", sub_items[-1])

    def test_script_split_supports_arabic_punctuation(self):
        """
        Chữ viết Ả Rập thường sử dụng ، ؛ ؟ như dấu câu tự nhiên. Những điều này phải được xác định trong giai đoạn phân đoạn câu
        Dấu câu, nếu không thì ranh giới tạm dừng và ranh giới dòng tập lệnh của tín hiệu edge-tts sẽ bị căn chỉnh sai.
        """
        text = "مرحبا بالعالم، كيف حالك؟ هذا اختبار؛ يعمل بشكل جيد."

        self.assertEqual(
            utils.split_string_by_punctuations(text),
            [
                "مرحبا بالعالم",
                "كيف حالك",
                "هذا اختبار",
                "يعمل بشكل جيد",
            ],
        )

    def test_match_script_line_normalizes_arabic_letter_forms(self):
        """
        edge-tts có thể bình thường hóa các dạng chữ cái khác nhau trong tiếng Ả Rập hoặc trả về dấu phụ,
        Văn bản gợi ý của Tatweel. Các kết quả trùng khớp phải có khả năng chấp nhận được lỗi nhưng phụ đề cuối cùng phải giữ lại bản sao tập lệnh gốc.
        """
        script_lines = ["أهلاً وسهلاً بك في المدرسة"]

        matched = vs._match_script_line(
            script_lines,
            "اهلا وسهلا بك في المدرسه",
            0,
        )

        self.assertEqual(matched, script_lines[0])

    def test_edge_cue_aggregation_handles_arabic_variant_forms(self):
        """
        Con đường cốt lõi để tái tạo sự thất bại của phụ đề tiếng Ả Rập: chữ viết chứa các dạng chữ cái như أ/ة, dấu hiệu cạnh
        Khi trả về các dạng chuẩn hóa như ا/ه, việc tổng hợp vẫn phải tạo ra phụ đề đầy đủ để tránh quay lại Whisper.
        """
        text = "أهلاً وسهلاً بك في المدرسة؟ هذا اختبار رائع، شكراً لك."
        script_lines = utils.split_string_by_punctuations(text)
        cue_texts = [
            "اهلا وسهلا بك في المدرسه",
            "هذا اختبار رائع",
            "شكرا لك",
        ]
        sub_maker = SimpleNamespace(
            cues=[
                SimpleNamespace(
                    content=cue_text,
                    start=timedelta(seconds=index),
                    end=timedelta(seconds=index + 0.8),
                )
                for index, cue_text in enumerate(cue_texts)
            ]
        )

        sub_items = vs._build_subtitle_items_from_edge_cues(sub_maker, script_lines)

        self.assertEqual(len(sub_items), len(script_lines))
        self.assertIn("أهلاً وسهلاً بك في المدرسة", sub_items[0])
        self.assertIn("شكراً لك", sub_items[-1])

    def test_create_subtitle_ignores_markdown_separator_lines(self):
        """
        Các tập lệnh hướng dẫn sử dụng có thể chứa các dấu phân cách Markdown, chẳng hạn như `---`. TTS không đọc to
        Các dòng ký hiệu, tổng hợp phụ đề này không nên coi chúng là các dòng phụ đề đích, nếu không thì phần tiếp theo sẽ
        Phụ đề sẽ bị kẹt và rơi trở lại Whisper.
        """
        text = "第一段\n---\n第二段"
        sub_maker = SimpleNamespace(
            cues=[
                SimpleNamespace(
                    content="第一段",
                    start=timedelta(seconds=0),
                    end=timedelta(seconds=0.8),
                ),
                SimpleNamespace(
                    content="第二段",
                    start=timedelta(seconds=1),
                    end=timedelta(seconds=1.8),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            vs.create_subtitle(
                sub_maker=sub_maker,
                text=text,
                subtitle_file=str(subtitle_file),
            )

            subtitle_content = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("第一段", subtitle_content)
        self.assertIn("第二段", subtitle_content)
        self.assertNotIn("---", subtitle_content)
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", subtitle_content)

    def test_create_subtitle_word_level_preserves_edge_cue_timing(self):
        """Các tín hiệu chi tiết của Edge TTS phải được viết theo từng mục và không thể tổng hợp bằng dấu câu nữa."""
        sub_maker = SimpleNamespace(
            cues=[
                SimpleNamespace(
                    content="人工智能",
                    start=timedelta(seconds=0.1),
                    end=timedelta(seconds=0.8),
                ),
                SimpleNamespace(
                    content="正在",
                    start=timedelta(seconds=0.9),
                    end=timedelta(seconds=1.2),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "word-level.srt"
            vs.create_subtitle(
                sub_maker=sub_maker,
                text="人工智能正在发展。",
                subtitle_file=str(subtitle_file),
                word_level=True,
            )
            subtitle_content = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("00:00:00,100 --> 00:00:00,800", subtitle_content)
        self.assertIn("人工智能", subtitle_content)
        self.assertIn("00:00:00,900 --> 00:00:01,200", subtitle_content)
        self.assertIn("正在", subtitle_content)

    def test_create_subtitle_word_level_falls_back_to_provider_granularity(self):
        """
        Các phiên bản cũ hơn của SubMaker không có tín hiệu sẽ giữ nguyên độ chi tiết về thời gian ban đầu được dịch vụ giọng nói trả về.

        Các dịch vụ như ElevenLabs, Fish Audio, v.v. chỉ có thể trả về các cụm từ hoặc toàn bộ dòng thời gian của câu. Không thể vào lúc này
        Chia đều theo ký tự và độ chính xác giả từng chữ, nếu không phụ đề sẽ dần trôi xa khỏi lời nói thật.
        """
        sub_maker = SimpleNamespace(
            cues=[],
            subs=["Hello world"],
            # Các phiên bản cũ hơn của phần bù của SubMaker đã sử dụng dấu thời gian số nguyên theo đơn vị 100 nano giây.
            offset=[(2_000_000, 11_000_000)],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "word-level-fallback.srt"
            vs.create_subtitle(
                sub_maker=sub_maker,
                text="Hello world",
                subtitle_file=str(subtitle_file),
                word_level=True,
            )
            subtitle_content = subtitle_file.read_text(encoding="utf-8")

        self.assertEqual(subtitle_content.count(" --> "), 1)
        self.assertIn("00:00:00,200 --> 00:00:01,100", subtitle_content)
        self.assertIn("Hello world", subtitle_content)

    def test_create_subtitle_ignores_markdown_underscore_marks(self):
        """
        `_` thường được người dùng sử dụng làm dấu nhấn mạnh Markdown, nhưng tín hiệu được TTS trả về thường không chứa
        các ký tự định dạng này. Nên bỏ qua `_` khi khớp để tránh tạo ra phụ đề trống hoặc quay lại Whisper.
        """
        text = "这是_a_测试。"
        sub_maker = SimpleNamespace(
            cues=[
                SimpleNamespace(
                    content="这是a测试",
                    start=timedelta(seconds=0),
                    end=timedelta(seconds=0.8),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            vs.create_subtitle(
                sub_maker=sub_maker,
                text=text,
                subtitle_file=str(subtitle_file),
            )

            subtitle_content = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("这是a测试", subtitle_content)
        self.assertNotIn("这是_a_测试", subtitle_content)
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", subtitle_content)

    def test_convert_rate_to_percent_signs_zero_rate(self):
        # Rates near but not exactly 1.0 round to 0 percent. edge-tts rejects
        # an unsigned "0%" (ValueError: Invalid rate '0%'), so the helper must
        # emit a sign-prefixed "+0%". Regression test for that crash.
        self.assertEqual(vs.convert_rate_to_percent(1.0), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(1.004), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(0.997), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(1.5), "+50%")
        self.assertEqual(vs.convert_rate_to_percent(0.8), "-20%")

    def test_convert_rate_to_percent_invalid_values_default_to_normal(self):
        # API và tập lệnh bó có thể chuyển các chuỗi trống dưới dạng 0, Không có hoặc chuỗi trống; những điều này không nên
        # edge-tts nhận -100% hoặc kích hoạt một ngoại lệ và thay vào đó xử lý nó ở tốc độ giọng nói bình thường.
        self.assertEqual(vs.convert_rate_to_percent(0), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(0.0), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(None), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(""), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(float("nan")), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(float("inf")), "+0%")


def _write_test_wav(filepath: str, duration_seconds: float = 1.0, sample_rate: int = 24000) -> str:
    import wave
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(round(duration_seconds * sample_rate))
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * num_samples)
    return filepath


class TestElevenLabsVoice(unittest.TestCase):

    def test_is_elevenlabs_voice_true(self):
        self.assertTrue(vs.is_elevenlabs_voice("elevenlabs:pNInz6obpgDQGcFmaJgB:Adam"))

    def test_is_elevenlabs_voice_false_azure(self):
        self.assertFalse(vs.is_elevenlabs_voice("zh-CN-XiaoxiaoNeural-Female"))

    def test_is_elevenlabs_voice_false_siliconflow(self):
        self.assertFalse(vs.is_elevenlabs_voice("siliconflow:model:voice-Male"))

    def test_is_elevenlabs_voice_empty(self):
        self.assertFalse(vs.is_elevenlabs_voice(""))

    def test_is_elevenlabs_voice_none(self):
        self.assertFalse(vs.is_elevenlabs_voice(None))

    def test_get_elevenlabs_voices_empty_api_key(self):
        result = vs.get_elevenlabs_voices("")
        self.assertEqual(result, [])

    @patch("app.services.voice.requests.get")
    def test_get_elevenlabs_voices_success(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "voices": [
                {"voice_id": "abc123", "name": "Adam"},
                {"voice_id": "def456", "name": "Rachel"},
            ]
        }
        result = vs.get_elevenlabs_voices("fake-api-key")
        self.assertEqual(result, [
            "elevenlabs:abc123:Adam",
            "elevenlabs:def456:Rachel",
        ])
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        self.assertIn("xi-api-key", call_kwargs.kwargs.get("headers", {}))

    @patch("app.services.voice.requests.get")
    def test_get_elevenlabs_voices_http_error(self, mock_get):
        mock_get.return_value.status_code = 401
        mock_get.return_value.text = "Unauthorized"
        result = vs.get_elevenlabs_voices("bad-key")
        self.assertEqual(result, [])

    @patch("app.services.voice.requests.get")
    def test_get_elevenlabs_voices_network_error(self, mock_get):
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("timeout")
        result = vs.get_elevenlabs_voices("fake-key")
        self.assertEqual(result, [])

    @patch("app.services.voice.requests.post")
    @patch("app.services.voice.AudioFileClip")
    @patch("app.services.voice.config")
    def test_elevenlabs_tts_success(self, mock_config, mock_clip_cls, mock_post):
        mock_config.elevenlabs.get.return_value = "fake-api-key"
        mock_post.return_value.status_code = 200
        mock_post.return_value.content = b"fake-mp3-bytes"
        mock_clip_cls.return_value.duration = 3.0
        mock_clip_cls.return_value.close = lambda: None

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out_path = f.name

        try:
            result = vs.elevenlabs_tts("Hello world", "abc123", out_path)
            self.assertIsNotNone(result)
            self.assertTrue(hasattr(result, "subs"))
            self.assertTrue(hasattr(result, "offset"))
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)

    @patch("app.services.voice.config")
    def test_elevenlabs_tts_no_api_key(self, mock_config):
        mock_config.elevenlabs.get.return_value = ""
        # Phân tích cú pháp chính bao gồm dự phòng biến môi trường và các thử nghiệm phải xóa rõ ràng môi trường máy chủ để tránh các máy phát triển hoặc CI
        # Thay đổi điều kiện kiểm tra "không được định cấu hình" sau khi cài đặt chính xác ELEVENLABS_API_KEY.
        with patch.dict(os.environ, {}, clear=True):
            result = vs.elevenlabs_tts("Hello", "abc123", "/tmp/test.mp3")
        self.assertIsNone(result)

    @patch("app.services.voice.config")
    def test_elevenlabs_tts_empty_text(self, mock_config):
        mock_config.elevenlabs.get.return_value = "fake-key"
        result = vs.elevenlabs_tts("  ", "abc123", "/tmp/test.mp3")
        self.assertIsNone(result)

    def test_elevenlabs_api_key_prefers_config(self):
        with (
            patch.object(vs.config, "elevenlabs", {"api_key": "config-key"}),
            patch.dict(os.environ, {"ELEVENLABS_API_KEY": "env-key"}),
        ):
            self.assertEqual(vs.get_elevenlabs_api_key(), "config-key")

    def test_elevenlabs_api_key_falls_back_to_environment(self):
        with (
            patch.object(vs.config, "elevenlabs", {"api_key": ""}),
            patch.dict(os.environ, {"ELEVENLABS_API_KEY": " env-key "}),
        ):
            self.assertEqual(vs.get_elevenlabs_api_key(), "env-key")

    def test_elevenlabs_api_key_matches_music_service(self):
        """TTS và nhạc phim chia sẻ cùng một cấu hình tài khoản và hai liên kết được tạo phải giải quyết cùng một Khóa."""
        from app.services import elevenlabs_music

        for configured_key, env_key in (("config-key", "env-key"), ("", "env-key")):
            with self.subTest(configured_key=configured_key):
                with (
                    patch.object(
                        vs.config, "elevenlabs", {"api_key": configured_key}
                    ),
                    patch.object(
                        elevenlabs_music.config,
                        "elevenlabs",
                        {"api_key": configured_key},
                    ),
                    patch.dict(os.environ, {"ELEVENLABS_API_KEY": env_key}),
                ):
                    self.assertEqual(
                        vs.get_elevenlabs_api_key(),
                        elevenlabs_music.get_api_key(),
                    )


    def test_siliconflow_subtitle_spans_full_audio_duration(self):
        """Last subtitle entry must end at the actual audio end, not truncated early.

        The old ad-hoc loop applied integer division independently to every
        sentence, so accumulated truncation meant the final subtitle always
        ended a few units before the real audio end. Every other TTS provider
        already delegates to populate_legacy_submaker_with_full_text, which
        anchors the last entry to the full duration; this test verifies
        siliconflow_tts now does the same.
        """
        audio_duration_seconds = 7.3
        expected_end_100ns = int(audio_duration_seconds * 10_000_000)

        fake_response = SimpleNamespace(status_code=200, content=b"fake-mp3")
        fake_clip = SimpleNamespace(
            duration=audio_duration_seconds, close=lambda: None
        )

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(vs.requests, "post", return_value=fake_response),
            patch.object(vs, "AudioFileClip", return_value=fake_clip),
            patch.object(vs.config, "siliconflow", {"api_key": "test-key"}),
        ):
            voice_file = str(Path(tmp_dir) / "test.mp3")
            sub_maker = vs.siliconflow_tts(
                text=(
                    "First sentence. Second sentence. "
                    "Third sentence. Fourth sentence."
                ),
                model="FunAudioLLM/CosyVoice2-0.5B",
                voice="FunAudioLLM/CosyVoice2-0.5B:alex",
                voice_rate=1.0,
                voice_file=voice_file,
            )

        self.assertIsNotNone(sub_maker)
        offsets = getattr(sub_maker, "offset", [])
        self.assertGreater(
            len(offsets), 1, "multi-sentence text must produce multiple subtitles"
        )
        last_end = offsets[-1][1]
        self.assertEqual(
            last_end,
            expected_end_100ns,
            f"last subtitle end ({last_end}) must equal the full audio duration "
            f"({expected_end_100ns} units = {audio_duration_seconds}s)",
        )

    def test_pause_tag_detection_and_parsing(self):
        """Kiểm tra khả năng phát hiện, phân tích cú pháp và làm sạch các thẻ tạm dừng đa ngôn ngữ."""
        sample_script = (
            "Hola a todos. [pausa: 2s] "
            "Welcome back. [pause: 1.5s] "
            "今天天气很好。[停顿: 3秒] "
            "잠시 멈춤 [일시중지: 500ms] "
            "Final sentence."
        )

        self.assertTrue(utils.has_pause_tags(sample_script))
        self.assertFalse(utils.has_pause_tags("Plain script with no pause tags."))

        segments = utils.parse_script_with_pauses(sample_script)
        self.assertEqual(len(segments), 9)
        self.assertEqual(segments[0], ("speech", "Hola a todos."))
        self.assertEqual(segments[1], ("pause", 2.0))
        self.assertEqual(segments[2], ("speech", "Welcome back."))
        self.assertEqual(segments[3], ("pause", 1.5))
        self.assertEqual(segments[4], ("speech", "今天天气很好。"))
        self.assertEqual(segments[5], ("pause", 3.0))
        self.assertEqual(segments[6], ("speech", "잠시 멈춤"))
        self.assertEqual(segments[7], ("pause", 0.5))
        self.assertEqual(segments[8], ("speech", "Final sentence."))

        cleaned = utils.remove_pause_tags(sample_script)
        self.assertNotIn("[pausa", cleaned)
        self.assertNotIn("[pause", cleaned)
        self.assertNotIn("[停顿", cleaned)
        self.assertNotIn("[일시중지", cleaned)

        normalized = utils.normalize_script_for_subtitle_matching(sample_script)
        self.assertNotIn("[pausa", normalized)
        self.assertIn("Hola a todos", normalized)
        self.assertIn("Final sentence", normalized)

        # Flexible syntax tests: without colon, with parentheses, and with default duration
        flexible_script = "Intro. [pausa 1s] Middle. (pausa: 2s) Next. [pause] End."
        self.assertTrue(utils.has_pause_tags(flexible_script))
        flex_segments = utils.parse_script_with_pauses(flexible_script)
        self.assertEqual(len(flex_segments), 7)
        self.assertEqual(flex_segments[0], ("speech", "Intro."))
        self.assertEqual(flex_segments[1], ("pause", 1.0))
        self.assertEqual(flex_segments[2], ("speech", "Middle."))
        self.assertEqual(flex_segments[3], ("pause", 2.0))
        self.assertEqual(flex_segments[4], ("speech", "Next."))
        self.assertEqual(flex_segments[5], ("pause", 1.0))
        self.assertEqual(flex_segments[6], ("speech", "End."))

        flex_cleaned = utils.remove_pause_tags(flexible_script)
        self.assertNotIn("[pausa", flex_cleaned)
        self.assertNotIn("(pausa", flex_cleaned)
        self.assertNotIn("[pause", flex_cleaned)

    def test_tts_with_pauses_shifts_submaker_timeline(self):
        """Dòng thời gian và nối âm thanh của SubMaker được bù đắp chính xác khi thử nghiệm bao gồm các thẻ tạm dừng."""
        from edge_tts.srt_composer import Subtitle

        fake_sub1 = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        fake_sub1.cues = [
            Subtitle(1, timedelta(seconds=0.1), timedelta(seconds=1.5), "Segment 1"),
        ]
        fake_sub1.subs = ["Segment 1"]
        fake_sub1.offset = [(1000000, 15000000)]

        fake_sub2 = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        fake_sub2.cues = [
            Subtitle(1, timedelta(seconds=0.2), timedelta(seconds=1.8), "Segment 2"),
        ]
        fake_sub2.subs = ["Segment 2"]
        fake_sub2.offset = [(2000000, 18000000)]

        def fake_single_tts(text, voice_name, voice_rate, voice_file, voice_volume=1.0):
            if "Segment 1" in text:
                _write_test_wav(voice_file, 1.5)
                return fake_sub1
            _write_test_wav(voice_file, 1.8)
            return fake_sub2

        def fake_silence(duration, voice_file):
            _write_test_wav(voice_file, duration)
            return True

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(vs, "_single_tts", side_effect=fake_single_tts) as mock_single_tts,
            patch.object(vs, "generate_silent_audio", side_effect=fake_silence) as mock_silence,
            patch.object(vs, "_concat_audio_files", return_value=True) as mock_concat,
        ):
            out_file = str(Path(tmp_dir) / "combined.mp3")
            script = "Segment 1. [pausa: 2s] Segment 2."
            result_submaker = vs.tts(
                text=script,
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file=out_file,
            )

            self.assertIsNotNone(result_submaker)
            self.assertEqual(mock_single_tts.call_count, 2)
            mock_silence.assert_called_once()
            mock_concat.assert_called_once()

            # Xác minh rằng đoạn tín hiệu thứ hai được bù 1,5 giây + 2,0 giây = 3,5 giây
            self.assertEqual(len(result_submaker.cues), 2)
            self.assertAlmostEqual(result_submaker.cues[0].start.total_seconds(), 0.1, places=2)
            self.assertAlmostEqual(result_submaker.cues[0].end.total_seconds(), 1.5, places=2)
            self.assertAlmostEqual(result_submaker.cues[1].start.total_seconds(), 3.5 + 0.2, places=2)
            self.assertAlmostEqual(result_submaker.cues[1].end.total_seconds(), 3.5 + 1.8, places=2)

            # Xác minh rằng phần bù kế thừa cũng được bù chính xác
            self.assertEqual(len(result_submaker.offset), 2)
            self.assertEqual(result_submaker.offset[0], (1000000, 15000000))
            expected_ns_offset = int(3.5 * 10000000)
            self.assertEqual(
                result_submaker.offset[1],
                (2000000 + expected_ns_offset, 18000000 + expected_ns_offset),
            )


    def test_pause_invalid_and_excessive_durations(self):
        """Khoảng thời gian thử nghiệm không hợp lệ (<= 0 giây) sẽ bị bỏ qua và khoảng thời gian cực dài bị giới hạn ở giới hạn tối đa."""
        # 1. Thời lượng không hợp lệ hoặc bằng 0: không được coi là phân đoạn tạm dừng
        zero_script = "Hello [pause: 0s] world. [pause: -2s] Bye."
        segments = utils.parse_script_with_pauses(zero_script)
        speech_only = [s for s in segments if s[0] == "speech"]
        pause_only = [s for s in segments if s[0] == "pause"]
        self.assertEqual(len(pause_only), 0)
        self.assertTrue(any("Hello" in s[1] for s in speech_only))
        self.assertTrue(any("world" in s[1] for s in speech_only))

        # 2. Tạm dừng quá lâu: bị kẹp hơn MAX_PAUSE_DUration_SECONDS
        long_script = "Hello [pause: 99s] world."
        segments_long = utils.parse_script_with_pauses(long_script)
        pauses = [s for s in segments_long if s[0] == "pause"]
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][1], utils.MAX_PAUSE_DURATION_SECONDS)

    def test_pause_consecutive_merging(self):
        """Các thẻ tạm dừng kiểm tra liên tục được tự động hợp nhất thành một phân đoạn tạm dừng và tổng thời lượng được bảo vệ bởi giới hạn trên."""
        # Hai lần tạm dừng liên tiếp gộp lại thành 1s + 2s = 3s
        script = "First part. [pause: 1s] [pause: 2s] Second part."
        segments = utils.parse_script_with_pauses(script)
        pauses = [s for s in segments if s[0] == "pause"]
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][1], 3.0)

        # Khi nhiều lần tạm dừng vượt quá giới hạn tối đa, chúng sẽ bị cắt bớt ở MAX_PAUSE_DUration_SECONDS sau khi hợp nhất.
        over_script = "Start. [pause: 7s] [pause: 8s] End."
        over_segments = utils.parse_script_with_pauses(over_script)
        over_pauses = [s for s in over_segments if s[0] == "pause"]
        self.assertEqual(len(over_pauses), 1)
        self.assertEqual(over_pauses[0][1], utils.MAX_PAUSE_DURATION_SECONDS)

    def test_pause_leading_and_trailing(self):
        """Kiểm tra độ lệch của đường đua và dòng thời gian cho phần đầu và phần cuối."""
        from edge_tts.srt_composer import Subtitle

        fake_sub = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        fake_sub.cues = [
            Subtitle(1, timedelta(seconds=0.1), timedelta(seconds=1.2), "Hello"),
        ]
        fake_sub.subs = ["Hello"]
        fake_sub.offset = [(1000000, 12000000)]

        def fake_single_tts(text, voice_name, voice_rate, voice_file, voice_volume=1.0):
            _write_test_wav(voice_file, 1.2)
            return fake_sub

        def fake_silence(duration, voice_file):
            _write_test_wav(voice_file, duration)
            return True

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(vs, "_single_tts", side_effect=fake_single_tts),
            patch.object(vs, "generate_silent_audio", side_effect=fake_silence) as mock_silence,
            patch.object(vs, "_concat_audio_files", return_value=True),
        ):
            # Leading pause: [pause: 1.5s] Hello
            out_file = str(Path(tmp_dir) / "leading.mp3")
            result = vs.tts(
                text="[pause: 1.5s] Hello",
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file=out_file,
            )
            self.assertIsNotNone(result)
            mock_silence.assert_called_with(1.5, mock_silence.call_args[0][1])
            # Tín hiệu phụ đề phải bắt đầu ở 1,5 giây + 0,1 giây = 1,6 giây
            self.assertAlmostEqual(result.cues[0].start.total_seconds(), 1.6, places=2)

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(vs, "_single_tts", side_effect=fake_single_tts),
            patch.object(vs, "generate_silent_audio", side_effect=fake_silence) as mock_silence,
            patch.object(vs, "_concat_audio_files", return_value=True),
        ):
            # Trailing pause: Hello [pause: 2s]
            out_file = str(Path(tmp_dir) / "trailing.mp3")
            result = vs.tts(
                text="Hello [pause: 2s]",
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file=out_file,
            )
            self.assertIsNotNone(result)
            mock_silence.assert_called_with(2.0, mock_silence.call_args[0][1])
            # Phụ đề giọng nói phải được giữ nguyên ở vị trí ban đầu và không được di chuyển sau khi kết thúc khoảng lặng.
            self.assertAlmostEqual(result.cues[0].start.total_seconds(), 0.1, places=2)
            self.assertAlmostEqual(result.cues[0].end.total_seconds(), 1.2, places=2)

    def test_pause_script_with_only_pauses(self):
        """Tập lệnh thử nghiệm chỉ chứa tính năng tạo im lặng thuần túy và an toàn khi tạm dừng thẻ."""
        def fake_silence(duration, voice_file):
            _write_test_wav(voice_file, duration)
            return True

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(vs, "generate_silent_audio", side_effect=fake_silence) as mock_silence,
            patch.object(vs, "_single_tts") as mock_single_tts,
        ):
            out_file = str(Path(tmp_dir) / "only_pauses.mp3")
            result = vs.tts(
                text="[pause: 2.5s]",
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file=out_file,
            )
            self.assertIsNotNone(result)
            mock_single_tts.assert_not_called()
            mock_silence.assert_called_once_with(2.5, out_file)
            self.assertEqual(vs.get_audio_duration(result), 2.5)

            # Việc tạo phụ đề phải xử lý an toàn các tập lệnh không có dòng mà không đưa ra ngoại lệ
            srt_path = str(Path(tmp_dir) / "only_pauses.srt")
            vs.create_subtitle(result, "[pause: 2.5s]", srt_path, word_level=False)
            vs.create_subtitle(result, "[pause: 2.5s]", srt_path, word_level=True)

    def test_subtitle_sync_sentence_mode_with_pauses(self):
        """Dòng thời gian của phụ đề chứa thẻ tạm dừng ở chế độ câu kiểm tra (câu) được đồng bộ hoàn toàn với nội dung."""
        from edge_tts.srt_composer import Subtitle

        fake_sub = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        # Đoạn đầu tiên là 0,1s - 1,5s, đoạn thứ hai là sau khoảng dừng 2s (3,5s - 5,0s)
        fake_sub.cues = [
            Subtitle(1, timedelta(seconds=0.1), timedelta(seconds=0.7), "Primera"),
            Subtitle(2, timedelta(seconds=0.8), timedelta(seconds=1.5), "frase."),
            Subtitle(3, timedelta(seconds=3.6), timedelta(seconds=4.2), "Segunda"),
            Subtitle(4, timedelta(seconds=4.3), timedelta(seconds=5.0), "frase."),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            srt_file = str(Path(tmp_dir) / "sentence_mode.srt")
            raw_script = "Primera frase. [pausa: 2s] Segunda frase."
            vs.create_subtitle(
                sub_maker=fake_sub,
                text=raw_script,
                subtitle_file=srt_file,
                word_level=False,
            )

            self.assertTrue(os.path.exists(srt_file))
            with open(srt_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("Primera frase", content)
            self.assertIn("Segunda frase", content)
            self.assertNotIn("[pausa", content)
            # Câu đầu tiên bắt đầu ở 0,1 giây và câu thứ hai bắt đầu ở 3,6 giây (phản ánh khoảng dừng 2 giây)
            self.assertIn("00:00:00,100 --> 00:00:01,500", content)
            self.assertIn("00:00:03,600 --> 00:00:05,000", content)

    def test_subtitle_sync_word_mode_with_pauses(self):
        """Đã thử nghiệm dòng thời gian phụ đề với thẻ tạm dừng ở chế độ một từ (word_by_word) để được đồng bộ hóa hoàn toàn với nội dung."""
        from edge_tts.srt_composer import Subtitle

        fake_sub = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        fake_sub.cues = [
            Subtitle(1, timedelta(seconds=0.1), timedelta(seconds=0.5), "Hello"),
            Subtitle(2, timedelta(seconds=0.6), timedelta(seconds=1.2), "world."),
            Subtitle(3, timedelta(seconds=3.3), timedelta(seconds=3.8), "Good"),
            Subtitle(4, timedelta(seconds=3.9), timedelta(seconds=4.5), "morning."),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            srt_file = str(Path(tmp_dir) / "word_mode.srt")
            raw_script = "Hello world. [pause: 2s] Good morning."
            vs.create_subtitle(
                sub_maker=fake_sub,
                text=raw_script,
                subtitle_file=srt_file,
                word_level=True,
            )

            self.assertTrue(os.path.exists(srt_file))
            with open(srt_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("Hello", content)
            self.assertIn("world.", content)
            self.assertIn("Good", content)
            self.assertIn("morning.", content)
            # Mục nhập thứ hai được bù chính xác thành 3,3 giây và 3,9 giây
            self.assertIn("00:00:00,100 --> 00:00:00,500", content)
            self.assertIn("00:00:03,300 --> 00:00:03,800", content)
            self.assertIn("00:00:03,900 --> 00:00:04,500", content)

    def test_tts_without_pauses_calls_single_tts_directly(self):
        """Khi xác minh rằng thẻ tạm dừng không được bao gồm, _single_tts được gọi trực tiếp và hành vi cũng như hiệu suất ban đầu hoàn toàn không thay đổi."""
        with (
            patch.object(vs, "_single_tts", return_value="normal_submaker") as mock_single_tts,
            patch.object(vs, "_tts_with_pauses") as mock_pauses,
        ):
            res = vs.tts(
                text="This is regular text without any pauses.",
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file="any.mp3",
            )
            self.assertEqual(res, "normal_submaker")
            mock_single_tts.assert_called_once()
            mock_pauses.assert_not_called()

    def test_pause_invalid_tags_rejected_and_cleaned(self):
        """Xác minh rằng các thẻ không hợp lệ (chẳng hạn như [tạm dừng: -2s], [tạm dừng: không], [tạm dừng: 0s]) đã được lọc hoàn toàn, không được nói cũng như không bị im lặng."""
        # 1. utils.remove_pause_tags xóa hoàn toàn tất cả các thẻ bất hợp pháp
        dirty_text = "Hello [pause: 0s] world. [pause: -2s] [pause: nope] Bye."
        cleaned = utils.remove_pause_tags(dirty_text)
        self.assertNotIn("[pause", cleaned)
        self.assertNotIn("-2s", cleaned)
        self.assertNotIn("nope", cleaned)
        self.assertIn("Hello world.", cleaned)
        self.assertIn("Bye.", cleaned)

        # 2. Parse_script_with_pauses bỏ qua các thẻ không hợp lệ và không đưa các thẻ này vào bản sao
        segments = utils.parse_script_with_pauses(dirty_text)
        pauses = [s for s in segments if s[0] == "pause"]
        speech = [s for s in segments if s[0] == "speech"]
        self.assertEqual(len(pauses), 0)
        self.assertEqual(len(speech), 3)
        for _, text in speech:
            self.assertNotIn("[pause", text)
            self.assertNotIn("-2s", text)
            self.assertNotIn("nope", text)

        # 3. Khi tập lệnh là tất cả các thẻ không hợp lệ, tts sẽ quay trở lại _single_tts, chuyển bản sao đã được làm sạch thay vì văn bản bẩn ban đầu
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(vs, "_single_tts", return_value="submaker_ok") as mock_single,
        ):
            out_file = str(Path(tmp_dir) / "cleaned.mp3")
            res = vs.tts(
                text="Hello [pause: 0s] world [pause: -2s] [pause: nope]",
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file=out_file,
            )
            self.assertEqual(res, "submaker_ok")
            mock_single.assert_called_once()
            called_text = mock_single.call_args[1].get("text") or mock_single.call_args[0][0]
            self.assertNotIn("[pause", called_text)
            self.assertNotIn("-2s", called_text)
            self.assertNotIn("nope", called_text)
            self.assertIn("Hello world", called_text)

    def test_pause_minimum_duration_validation(self):
        """Xác minh rằng các khoảng dừng nhỏ (ví dụ: 1 mili giây) đã được kiểm tra và giới hạn ở ngưỡng hợp lệ thấp nhất MIN_PAUSE_DUration_SECONDS (0,1 giây/100 mili giây)."""
        script = "Start [pause: 1ms] End"
        segments = utils.parse_script_with_pauses(script)
        pauses = [s for s in segments if s[0] == "pause"]
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0][1], utils.MIN_PAUSE_DURATION_SECONDS)
        self.assertEqual(utils.MIN_PAUSE_DURATION_SECONDS, 0.1)

    def test_tts_provider_limitation_to_azure_v1(self):
        """Đã xác minh rằng chỉ Azure TTS v1 (Edge TTS) mới đi vào liên kết được phân đoạn, Gemini/Fish Audio/SiliconFlow/Kokoro vẫn là một yêu cầu duy nhất."""
        # 1. Đánh giá của nhà cung cấp âm thanh
        self.assertTrue(vs.is_azure_v1_voice("zh-CN-XiaoxiaoNeural"))
        self.assertTrue(vs.is_azure_v1_voice("es-ES-AlvaroNeural"))
        self.assertFalse(vs.is_azure_v1_voice("gemini:Puck-Male"))
        self.assertFalse(vs.is_azure_v1_voice("fish_audio:default"))
        self.assertFalse(vs.is_azure_v1_voice("siliconflow:fishaudio/fish-speech-1.5:alex-Male"))
        self.assertFalse(vs.is_azure_v1_voice("kokoro:af_bella"))
        self.assertFalse(vs.is_azure_v1_voice("elevenlabs:voice-id:voice-name"))

        # 2. Khi các tập lệnh của nhà cung cấp khác chứa thẻ tạm dừng, trước tiên họ phải xóa thẻ và gọi một tổng hợp duy nhất mà không gọi _tts_with_pauses
        non_azure_voices = [
            "gemini:Puck-Male",
            "fish_audio:default",
            "siliconflow:fishaudio/fish-speech-1.5:alex-Male",
            "kokoro:af_bella",
        ]
        script_with_pause = "Part 1. [pause: 2s] Part 2."

        for voice in non_azure_voices:
            with (
                patch.object(vs, "_single_tts", return_value="mock_sub") as mock_single,
                patch.object(vs, "_tts_with_pauses") as mock_split,
            ):
                res = vs.tts(
                    text=script_with_pause,
                    voice_name=voice,
                    voice_rate=1.0,
                    voice_file="dummy.mp3",
                )
                self.assertEqual(res, "mock_sub")
                mock_split.assert_not_called()
                mock_single.assert_called_once()
                called_text = mock_single.call_args[1].get("text") or mock_single.call_args[0][0]
                self.assertNotIn("[pause", called_text)
                self.assertIn("Part 1. Part 2.", called_text)

    def test_real_multi_segment_concatenation_no_drift(self):
        """Thử nghiệm ghép nối nhiều đoạn âm thanh thực: âm thanh 12 giây 1 và 11 giây tạm dừng 0,5 giây, xác minh rằng độ lệch phụ đề hoàn toàn giống với mẫu được giải mã thực, không bị trôi tích lũy."""
        import wave
        import struct
        import math
        import subprocess
        from edge_tts.srt_composer import Subtitle

        sr = 24000
        # Tạo WAV PCM 16-bit đơn sắc hình sin 1 giây tiêu chuẩn
        speech_pcm = bytearray()
        for i in range(sr):
            val = int(32767.0 * 0.3 * math.sin(2.0 * math.pi * 440.0 * i / sr))
            speech_pcm.extend(struct.pack("<h", val))

        def make_fake_speech_submaker(idx):
            sub = vs.ensure_legacy_submaker_fields(vs.SubMaker())
            # Tín hiệu của mỗi dòng trong phân đoạn riêng của nó nằm trong khoảng từ 0,0 giây đến 1,0 giây
            sub.cues = [
                Subtitle(1, timedelta(seconds=0.0), timedelta(seconds=1.0), f"Word_{idx}"),
            ]
            sub.subs = [f"Word_{idx}"]
            sub.offset = [(0, 10000000)]
            sub.duration = 1.0
            return sub

        with tempfile.TemporaryDirectory() as tmp_dir:
            def real_single_tts_wav(text, voice_name, voice_rate, voice_file, voice_volume=1.0):
                # Viết dữ liệu âm thanh WAV thực 1 giây
                with wave.open(voice_file, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframes(speech_pcm)
                idx = int(text.split()[-1]) if text.split()[-1].isdigit() else 0
                return make_fake_speech_submaker(idx)

            # Xây dựng một đoạn script có 12 dòng và 11 lần tạm dừng 0,5 giây
            script_parts = []
            for i in range(12):
                script_parts.append(f"Word {i}")
                if i < 11:
                    script_parts.append("[pause: 0.5s]")
            full_script = " ".join(script_parts)

            out_mp3 = str(Path(tmp_dir) / "output.mp3")

            # Chạy các liên kết được phân đoạn thực mà không cần giả lập _concat_audio_files và get_audio_duration
            with patch.object(vs, "_single_tts", side_effect=real_single_tts_wav):
                result_submaker = vs._tts_with_pauses(
                    text=full_script,
                    voice_name="zh-CN-XiaoxiaoNeural",
                    voice_rate=1.0,
                    voice_file=out_mp3,
                )

            self.assertIsNotNone(result_submaker)
            self.assertTrue(os.path.exists(out_mp3))
            self.assertGreater(os.path.getsize(out_mp3), 0)

            # Xác minh số đầu mối phụ đề là 12
            self.assertEqual(len(result_submaker.cues), 12)

            # Xác minh đoạn 12 (dòng cuối cùng):
            # Chúng tôi đã trải qua 11 bài phát biểu 1,0 giây + 11 lần tạm dừng 0,5 giây = 11,0 giây + 5,5 giây = 16,50 giây
            last_cue = result_submaker.cues[-1]
            # Khẳng định chặt chẽ: thời gian xuất phát phải là 16h50 và không được trôi về 17,71s!
            self.assertAlmostEqual(last_cue.start.total_seconds(), 16.50, places=2)
            self.assertAlmostEqual(last_cue.end.total_seconds(), 17.50, places=2)

            # Âm thanh MP3 đầu ra được giải mã thực tế, xác minh rằng tổng thời lượng mẫu sau khi giải mã là 17,50 giây
            decoded_wav = str(Path(tmp_dir) / "decoded.wav")
            ffmpeg_binary = utils.get_ffmpeg_binary()
            subprocess.run(
                [ffmpeg_binary, "-y", "-i", out_mp3, decoded_wav],
                capture_output=True,
                check=True,
            )
            with wave.open(decoded_wav, "rb") as wf:
                total_frames = wf.getnframes()
                total_sr = wf.getframerate()
                decoded_duration = total_frames / float(total_sr)

            # Xác minh rằng thời lượng giải mã cuối cùng giống hệt với thời lượng cuối của phụ đề (17,50 giây)
            self.assertAlmostEqual(decoded_duration, 17.50, delta=0.06)

    def test_tts_with_pauses_fails_on_empty_chunk_audio(self):
        """Kiểm tra hồi quy: Khi quá trình tổng hợp đoạn giọng nói tạo ra một tệp trống (0 byte) hoặc tệp bị thiếu, _tts_with_pauses không báo cáo được lỗi và trả về Không có. Nó không được quay lại để tạo ra lỗi mặt nạ im lặng."""
        from edge_tts.srt_composer import Subtitle

        fake_sub = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        fake_sub.cues = [
            Subtitle(1, timedelta(seconds=0.0), timedelta(seconds=1.0), "Hello"),
        ]

        def fake_single_tts_empty(text, voice_name, voice_rate, voice_file, voice_volume=1.0):
            Path(voice_file).touch()  # 0-byte empty file
            return fake_sub

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = str(Path(tmp_dir) / "out.mp3")
            with patch.object(vs, "_single_tts", side_effect=fake_single_tts_empty):
                result = vs.tts(
                    text="Hello [pause: 1s] World",
                    voice_name="zh-CN-XiaoxiaoNeural",
                    voice_rate=1.0,
                    voice_file=out_file,
                )
            self.assertIsNone(result)

    def test_tts_with_pauses_fails_on_corrupted_chunk_audio(self):
        """Kiểm tra hồi quy: Khi âm thanh của clip lời nói bị hỏng và không thể giải mã thành PCM, _tts_with_pauses phải báo lỗi và trả về None thay vì tiếp tục thực hiện với chế độ im lặng thay vì tường thuật."""
        from edge_tts.srt_composer import Subtitle

        fake_sub = vs.ensure_legacy_submaker_fields(vs.SubMaker())
        fake_sub.cues = [
            Subtitle(1, timedelta(seconds=0.0), timedelta(seconds=1.0), "Hello"),
        ]

        def fake_single_tts_corrupted(text, voice_name, voice_rate, voice_file, voice_volume=1.0):
            with open(voice_file, "wb") as f:
                f.write(b"NOT_A_VALID_AUDIO_FILE_DATA_CORRUPTED_1234567890")
            return fake_sub

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = str(Path(tmp_dir) / "out.mp3")
            with patch.object(vs, "_single_tts", side_effect=fake_single_tts_corrupted):
                result = vs.tts(
                    text="Hello [pause: 1s] World",
                    voice_name="zh-CN-XiaoxiaoNeural",
                    voice_rate=1.0,
                    voice_file=out_file,
                )
            self.assertIsNone(result)

    def test_tts_passes_original_text_unchanged_without_pauses(self):
        """Khi kiểm tra các thẻ không tạm dừng, tts chuyển trực tiếp văn bản gốc tới _single_tts mà không thực hiện thay thế hoặc làm sạch thường xuyên."""
        original_text = "  Leading and trailing spaces, [regular bracket] and punctuation!  \nNew line here.  "
        with patch.object(vs, "_single_tts", return_value="dummy_submaker") as mock_single:
            result = vs.tts(
                text=original_text,
                voice_name="zh-CN-XiaoxiaoNeural",
                voice_rate=1.0,
                voice_file="out.mp3",
            )
            self.assertEqual(result, "dummy_submaker")
            mock_single.assert_called_once()
            called_text = mock_single.call_args[1].get("text") or mock_single.call_args[0][0]
            self.assertEqual(called_text, original_text)


if __name__ == "__main__":
    # python -m unittest test.services.test_voice.TestVoiceService.test_azure_tts_v1
    # python -m unittest test.services.test_voice.TestVoiceService.test_azure_tts_v2
    unittest.main()
