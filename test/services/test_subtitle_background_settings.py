import json
from pathlib import Path
import unittest

import numpy as np

from app.models.schema import SubtitleRequest, VideoParams
from app.services import video


class TestSubtitleBackgroundSettings(unittest.TestCase):
    def test_subtitle_background_is_disabled_by_default(self):
        """Cả tác vụ mới lẫn giao diện phụ đề độc lập đều không hiển thị nền phụ đề khi người dùng không chỉ định."""
        video_params = VideoParams(video_subject="default subtitle background")
        subtitle_request = SubtitleRequest(video_script="default subtitle background")

        self.assertFalse(video_params.text_background_color)
        self.assertFalse(subtitle_request.text_background_color)

    def test_all_locales_include_subtitle_background_labels(self):
        """
        Sau khi thêm nút chuyển nền phụ đề và bộ chọn màu vào WebUI, tất cả các ngôn ngữ hiện có phải bao gồm ngôn ngữ tương ứng
        Phím dịch để ngăn một số giao diện ngôn ngữ hiển thị trực tiếp các phím nội bộ tiếng Anh.
        """
        i18n_dir = Path(__file__).parent.parent.parent / "webui" / "i18n"
        required_keys = {
            "Enable Subtitle Background",
            "Subtitle Background Color",
            "Subtitle Colors Are Indistinguishable",
            "Subtitle Font Does Not Support Text",
            "No Voice",
        }

        for locale_file in i18n_dir.glob("*.json"):
            with self.subTest(locale=locale_file.name):
                data = json.loads(locale_file.read_text(encoding="utf-8"))
                translations = data.get("Translation", {})
                missing_keys = required_keys - translations.keys()

                self.assertEqual(missing_keys, set())

    def test_video_params_accepts_disabled_and_colored_subtitle_background(self):
        """
        Tùy thuộc vào công tắc, giao diện người dùng sẽ chuyển Sai hoặc chuỗi màu sang phần phụ trợ. Điều này vẫn xác nhận lược đồ
        Việc chấp nhận cả hai giá trị sẽ ngăn các phần phụ thuộc tiếp theo hoặc các điều chỉnh loại phá vỡ hợp đồng của WebUI với logic tổng hợp.
        """
        base_params = {
            "video_subject": "subtitle background smoke",
        }

        disabled_params = VideoParams(
            **base_params,
            text_background_color=False,
        )
        colored_params = VideoParams(
            **base_params,
            text_background_color="#123456",
        )

        self.assertFalse(disabled_params.text_background_color)
        self.assertEqual(colored_params.text_background_color, "#123456")

    def test_visible_text_position_centers_actual_mask_bounds(self):
        """
        Canvas của TextClip sẽ chứa chiều cao dòng phông chữ và khoảng trắng cơ bản. Căn giữa trực tiếp canvas sẽ tạo ra
        Phụ đề trông thấp hơn ở nền. Ở đây, mặt nạ giả được sử dụng để mô phỏng "pixel văn bản hiển thị"
        Trong trường hợp nửa dưới của canvas", trình trợ giúp xác thực sẽ tính toán lại y dựa trên vùng hiển thị thực.
        """

        class FakeMask:
            def get_frame(self, _):
                mask = np.zeros((46, 100), dtype=float)
                mask[12:46, 10:90] = 1.0
                return mask

        class FakeTextClip:
            w = 100
            h = 46
            mask = FakeMask()

        x, y = video._get_visible_center_position(
            FakeTextClip(), container_width=100, container_height=93
        )

        self.assertEqual(x, 0)
        # Chiều cao pixel hiển thị là 34px, chiều cao trên và dưới khoảng 29px trong vùng chứa 93px;
        # Vì phần trên cùng của mặt nạ bắt đầu ở 12px nên bản thân TextClip cần phải di chuyển lên tới 18px.
        self.assertEqual(y, 18)

    def test_detects_indistinguishable_subtitle_colors(self):
        invisible_params = VideoParams(
            video_subject="subtitle color validation",
            text_fore_color="#000000",
            text_background_color="#000000",
            stroke_color="#000000",
            stroke_width=1.5,
        )
        different_outline_params = VideoParams(
            video_subject="subtitle color validation",
            text_fore_color="#000000",
            text_background_color="#000000",
            stroke_color="#FFFFFF",
            stroke_width=1.5,
        )
        background_disabled_params = VideoParams(
            video_subject="subtitle color validation",
            text_fore_color="#000000",
            text_background_color=False,
            stroke_color="#000000",
            stroke_width=1.5,
        )

        self.assertTrue(
            video.subtitle_colors_are_indistinguishable(invisible_params)
        )
        self.assertTrue(
            video.subtitle_colors_are_indistinguishable(different_outline_params)
        )
        self.assertFalse(
            video.subtitle_colors_are_indistinguishable(background_disabled_params)
        )

    def test_detects_font_without_chinese_glyphs(self):
        fonts_dir = (
            Path(__file__).parent.parent.parent / "resource" / "fonts"
        )

        self.assertFalse(
            video.subtitle_font_supports_text(
                str(fonts_dir / "BeVietnamPro-Bold.ttf"), "人工智能改变生活"
            )
        )
        self.assertTrue(
            video.subtitle_font_supports_text(
                str(fonts_dir / "MicrosoftYaHeiBold.ttc"), "人工智能改变生活"
            )
        )
        self.assertTrue(
            video.subtitle_font_supports_text(
                str(fonts_dir / "BeVietnamPro-Bold.ttf"), "Artificial intelligence"
            )
        )

    def test_wrap_text_keeps_closing_punctuation_with_text(self):
        """
        Khi các câu tiếng Trung dài được bao bọc bởi các ký tự, các dấu chấm câu đóng như dấu chấm không thể chiếm một dòng, nếu không thì nền phụ đề
        Nó sẽ được giữ bởi một điểm nhỏ duy nhất. Tình huống ranh giới của các câu tiếng Trung dài với phông chữ lớn được tái hiện ở đây.
        """
        font_path = (
            Path(__file__).parent.parent.parent
            / "resource"
            / "fonts"
            / "MicrosoftYaHeiBold.ttc"
        )

        wrapped_text, _ = video.wrap_text(
            "如果你调整字号，中文笔画也不能被黑色背景遮挡。",
            max_width=1642,
            font=str(font_path),
            fontsize=72,
        )

        self.assertNotIn("\n。", wrapped_text)
        self.assertIn("挡。", wrapped_text)
