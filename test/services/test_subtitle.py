import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Khi tệp thử nghiệm được chạy trực tiếp, gói ứng dụng cũng có thể được nhập từ thư mục gốc của kho.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import subtitle


class TestSubtitleService(unittest.TestCase):
    def test_file_to_subtitles_returns_empty_for_missing_input(self):
        """Cả đường dẫn trống và tệp không tồn tại đều phải trả về danh sách trống một cách an toàn."""
        self.assertEqual(subtitle.file_to_subtitles(""), [])
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_file = Path(tmp_dir) / "missing.srt"
            self.assertEqual(subtitle.file_to_subtitles(str(missing_file)), [])

    def test_levenshtein_distance_and_similarity_cover_common_boundaries(self):
        """
        Việc chỉnh sửa phụ đề phụ thuộc vào khoảng cách chỉnh sửa để chọn có tiếp tục ghép các phụ đề liền kề hay không, sao cho che mất chuỗi trống,
        Có bốn ranh giới: trao đổi tham số, bỏ qua trường hợp và sự khác biệt rõ ràng, để ngăn chặn việc hợp nhất nhầm sau khi điều chỉnh thuật toán.
        """
        self.assertEqual(subtitle.levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(subtitle.levenshtein_distance("a", "longer"), 6)
        self.assertEqual(subtitle.levenshtein_distance("hello", ""), 5)
        self.assertEqual(subtitle.similarity("Hello", "hello"), 1.0)
        self.assertLess(subtitle.similarity("hello", "world"), 0.5)

    def test_create_returns_empty_when_whisper_is_unavailable(self):
        """Nên bỏ qua các phần phụ thuộc Whisper tùy chọn nếu chưa được cài đặt, thay vì ném một ngoại lệ vào chuỗi tác vụ."""
        with patch.object(subtitle, "WhisperModel", None):
            self.assertEqual(subtitle.create("audio.mp3"), "")

    def test_create_returns_none_when_whisper_model_cannot_load(self):
        """Khi quá trình tải xuống hoặc khởi tạo mô hình không thành công, kết quả lỗi phải được trả về và lớp tác vụ được phép cập nhật trạng thái."""
        with patch.object(subtitle, "model", None), patch.object(
            subtitle,
            "WhisperModel",
            side_effect=RuntimeError("model unavailable"),
        ):
            self.assertIsNone(subtitle.create("audio.mp3"))

    def test_create_writes_punctuated_and_trailing_segments(self):
        """
        Sử dụng mô hình Whisper giả để ghi đè quá trình xử lý dấu thời gian từng từ mà không cần truy cập mạng hoặc tải mô hình thật.
        Một đoạn chứa cả dấu ngắt câu và văn bản không có dấu câu ở cuối, có thể xác minh hai đường dẫn viết quan trọng.
        """

        class _FakeWhisperModel:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs

            def transcribe(self, audio_file, **kwargs):
                words = [
                    SimpleNamespace(start=0.0, end=0.4, word="Hello"),
                    SimpleNamespace(start=0.4, end=0.9, word=" world."),
                    SimpleNamespace(start=1.0, end=1.5, word="Again"),
                ]
                segment = SimpleNamespace(
                    start=0.0,
                    end=1.8,
                    words=words,
                )
                info = SimpleNamespace(language="en", language_probability=0.99)
                return [segment], info

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "generated.srt"
            with patch.object(subtitle, "model", None), patch.object(
                subtitle,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                subtitle.create("audio.mp3", str(subtitle_file))

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello world", "Again"])

    def test_create_word_level_writes_each_whisper_word_with_its_timing(self):
        """Chế độ từng từ sẽ giữ nguyên từng từ của Whisper cũng như thời gian bắt đầu và kết thúc độc lập của nó."""
        transcribe_kwargs = {}

        class _FakeWhisperModel:
            def __init__(self, **_kwargs):
                pass

            def transcribe(self, _audio_file, **kwargs):
                transcribe_kwargs.update(kwargs)
                words = [
                    SimpleNamespace(start=0.1, end=0.4, word="Hello"),
                    SimpleNamespace(start=0.4, end=0.8, word=" world"),
                ]
                segment = SimpleNamespace(start=0.1, end=0.8, words=words)
                info = SimpleNamespace(language="en", language_probability=0.99)
                return [segment], info

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "word-level.srt"
            with patch.object(subtitle, "model", None), patch.object(
                subtitle,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                subtitle.create(
                    "audio.mp3",
                    str(subtitle_file),
                    word_level=True,
                )

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello", "world"])
        self.assertIs(transcribe_kwargs["word_timestamps"], True)
        self.assertIs(transcribe_kwargs["vad_filter"], True)
        self.assertIn("00:00:00,100 --> 00:00:00,400", items[0][1])
        self.assertIn("00:00:00,400 --> 00:00:00,800", items[1][1])

    def test_correct_ignores_markdown_separator_lines(self):
        """
        Giai đoạn chỉnh sửa dự phòng Whisper cũng phải bỏ qua các dòng script không được lồng tiếng như `---`.

        Nếu dấu phân cách Markdown được để lại ở đây, `true()` sẽ cho rằng tập lệnh có nhiều dòng hơn
        số dòng phụ đề và thêm `00:00:00,000 --> 00:00:00,000`. Phần mềm chỉnh sửa sẽ
        SRT được tạo được xác định là không được nhập.
        """
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "第一段\n\n"
            "2\n"
            "00:00:01,100 --> 00:00:02,000\n"
            "第二段\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="第一段\n---\n第二段",
            )

            corrected_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("第一段", corrected_srt)
        self.assertIn("第二段", corrected_srt)
        self.assertNotIn("---", corrected_srt)
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", corrected_srt)

    def test_correct_merges_adjacent_subtitles_for_one_script_sentence(self):
        """
        Whisper có thể chia một câu văn thành nhiều đoạn thời gian. Logic hiệu chỉnh sẽ hợp nhất các phạm vi thời gian và khôi phục
        Văn bản script gốc để tránh sự phân mảnh không cần thiết của phụ đề cuối cùng.
        """
        original_srt = (
            "1\n00:00:00,100 --> 00:00:01,000\nHello\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nworld\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(str(subtitle_file), "Hello world")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1], "00:00:00,100 --> 00:00:02,000")
        self.assertEqual(items[0][2], "Hello world")

    def test_correct_replaces_mismatch_and_appends_missing_script_line(self):
        """
        Nếu kết quả phiên âm hoàn toàn không nhất quán với chữ viết thì chữ viết đó vẫn chiếm ưu thế; không có câu thừa nào trong kịch bản có thể được sử dụng lại.
        Sử dụng trình giữ chỗ có thời gian bằng 0 rõ ràng khi sử dụng dòng thời gian để tránh mất văn bản và duy trì hành vi tương thích hiện có.
        """
        original_srt = "1\n00:00:00,100 --> 00:00:01,000\nWrong text\n\n"

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(str(subtitle_file), "Expected sentence. Extra sentence.")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(
            [item[2] for item in items],
            ["Expected sentence", "Extra sentence"],
        )
        self.assertEqual(items[1][1], "00:00:00,000 --> 00:00:00,000")

    def test_file_to_subtitles_keeps_last_block_without_trailing_newline(self):
        """
        The final subtitle must be parsed even when the SRT file does not end
        with a trailing blank line. Many tools omit it, and previously the last
        block was silently dropped because only a blank line flushed a block.
        """
        srt_without_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_without_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][2], "Hello")
        self.assertEqual(items[1][2], "World")

    def test_file_to_subtitles_parses_blocks_with_trailing_newline(self):
        """A normal SRT ending in a blank line still parses all blocks."""
        srt_with_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_with_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello", "World"])


if __name__ == "__main__":
    unittest.main()
