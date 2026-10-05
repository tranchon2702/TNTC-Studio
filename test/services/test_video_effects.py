import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from moviepy import ImageClip

from app.models.schema import VideoTransitionMode
from app.services.utils import video_effects


def _gradient_clip(width=64, height=48, duration=1.0):
    """Tạo các gradient không đồng nhất để đảm bảo có thể phát hiện được sự khác biệt về pixel trước và sau khi thu phóng một cách đáng tin cậy."""
    x = np.linspace(0, 255, width, dtype=np.uint8)
    y = np.linspace(0, 255, height, dtype=np.uint8)
    frame = np.stack(np.meshgrid(x, y), axis=-1).sum(axis=-1) % 256
    rgb = np.stack([frame] * 3, axis=-1).astype(np.uint8)
    return ImageClip(rgb).with_duration(duration)


def _detail_frame(width=128, height=96):
    """Tạo các khung RGB chứa chi tiết tần số cao để quan sát xem tỷ lệ pixel phụ có phản hồi liên tục hay không."""
    x = np.arange(width, dtype=np.int16)
    y = np.arange(height, dtype=np.int16)[:, None]
    return np.stack(
        (
            (x + y) % 256,
            (3 * x + y) % 256,
            (x + 3 * y) % 256,
        ),
        axis=-1,
    ).astype(np.uint8)


class TestFadeAndSlideTransitions(unittest.TestCase):
    def test_fade_transitions_apply_requested_duration(self):
        """Làm mờ dần và làm mờ dần phải chuyển khoảng thời gian mà người gọi chuyển sang hiệu ứng MoviePy như hiện tại."""
        clip = _gradient_clip()
        self.addCleanup(clip.close)

        fade_in = video_effects.fadein_transition(clip, 0.25)
        fade_out = video_effects.fadeout_transition(clip, 0.75)
        self.addCleanup(fade_in.close)
        self.addCleanup(fade_out.close)

        self.assertEqual(fade_in.duration, clip.duration)
        self.assertEqual(fade_out.duration, clip.duration)
        frame_difference = np.abs(
            fade_in.get_frame(0).astype(int) - clip.get_frame(0).astype(int)
        )
        self.assertGreater(
            frame_difference.max(),
            0,
        )
        np.testing.assert_allclose(
            fade_out.get_frame(0),
            clip.get_frame(0),
            atol=1,
        )

    def test_slidein_positions_cover_all_directions_and_unknown_side(self):
        """Hoạt ảnh trượt vào phải duy trì ổn định ở cả bốn hướng, vị trí cuối và phần dưới cùng của hướng không xác định."""
        clip = _gradient_clip(width=60, height=40, duration=2)
        self.addCleanup(clip.close)
        expected_starts = {
            "left": (-60, 0),
            "right": (60, 0),
            "top": (0, -40),
            "bottom": (0, 40),
            "unknown": (0, 0),
        }

        for side, expected_start in expected_starts.items():
            with self.subTest(side=side):
                transitioned = video_effects.slidein_transition(clip, 1, side)
                self.addCleanup(transitioned.close)
                moving_clip = transitioned.clips[1]
                self.assertEqual(moving_clip.pos(0), expected_start)
                self.assertEqual(moving_clip.pos(1), (0, 0))

    def test_slideout_positions_cover_timing_and_all_directions(self):
        """
        Thanh trượt sẽ bắt đầu di chuyển ở cuối clip; thông số bốn hướng, thời gian kết thúc trong quá khứ và thời lượng bằng không
        Tất cả cần phải được kẹp để tránh sự phân chia bằng 0 hoặc vật liệu bị lệch sớm.
        """
        clip = _gradient_clip(width=60, height=40, duration=2)
        self.addCleanup(clip.close)
        expected_ends = {
            "left": (-60, 0),
            "right": (60, 0),
            "top": (0, -40),
            "bottom": (0, 40),
            "unknown": (0, 0),
        }

        for side, expected_end in expected_ends.items():
            with self.subTest(side=side):
                transitioned = video_effects.slideout_transition(clip, 1, side)
                self.addCleanup(transitioned.close)
                moving_clip = transitioned.clips[1]
                self.assertEqual(moving_clip.pos(0.5), (0, 0))
                self.assertEqual(moving_clip.pos(2.5), expected_end)

        zero_duration = video_effects.slideout_transition(clip, 0, "right")
        self.addCleanup(zero_duration.close)
        self.assertEqual(zero_duration.clips[1].pos(2), (0, 0))
        self.assertEqual(zero_duration.clips[1].pos(2.1), (60, 0))


class TestZoomTransitions(unittest.TestCase):
    def test_schema_has_zoom_members(self):
        self.assertEqual(VideoTransitionMode.zoom_in.value, "ZoomIn")
        self.assertEqual(VideoTransitionMode.zoom_out.value, "ZoomOut")

    def test_zoomin_preserves_geometry_and_zooms_over_time(self):
        clip = _gradient_clip()
        zoomed = video_effects.zoomin_transition(clip, 1)
        self.addCleanup(zoomed.close)
        self.addCleanup(clip.close)

        self.assertEqual(zoomed.size, clip.size)
        self.assertEqual(zoomed.duration, clip.duration)

        first = zoomed.get_frame(0)
        last = zoomed.get_frame(clip.duration - 0.01)
        original = clip.get_frame(0)

        self.assertEqual(first.shape, original.shape)
        self.assertEqual(first.dtype, np.uint8)
        # Độ phóng đại bắt đầu ở mức 1x, do đó khung hình đầu tiên phải nhất quán với khung hình gốc.
        np.testing.assert_allclose(first, original, atol=2)
        # Khung hình cuối cùng là từ vùng được cắt xén và phóng to ở giữa và phải khác biệt đáng kể so với ảnh gốc.
        self.assertGreater(np.abs(last.astype(int) - original.astype(int)).max(), 2)

    def test_zoomout_starts_zoomed_and_returns_to_source(self):
        clip = _gradient_clip()
        zoomed = video_effects.zoomout_transition(clip, 1)
        self.addCleanup(zoomed.close)
        self.addCleanup(clip.close)

        self.assertEqual(zoomed.size, clip.size)

        first = zoomed.get_frame(0)
        last = zoomed.get_frame(clip.duration)
        original = clip.get_frame(0)

        # Khung hình đầu tiên giảm là 1,2x và khung hình cuối quay lại chính xác tỷ lệ ban đầu.
        self.assertGreater(np.abs(first.astype(int) - original.astype(int)).max(), 2)
        np.testing.assert_allclose(last, original, atol=2)

    def test_zoom_frame_rejects_invalid_scale_factor(self):
        frame = np.zeros((8, 8, 3), dtype=np.uint8)

        with self.assertRaisesRegex(ValueError, "scale_factor"):
            video_effects._zoom_frame(frame, 0)

    def test_zoom_frame_responds_to_subpixel_scale_changes(self):
        frame = _detail_frame()

        first = video_effects._zoom_frame(frame, 1.1)
        second = video_effects._zoom_frame(frame, 1.1001)

        # Hai tỷ lệ này sẽ có cùng kích thước cắt xén trong thuật toán cắt số nguyên cũ, tạo ra cùng một khung hình,
        # Tiếp theo là bước nhảy đột ngột khi vượt qua một ranh giới số nguyên. Lấy mẫu pixel phụ sẽ đáp ứng với những thay đổi quy mô nhỏ như vậy.
        self.assertGreater(np.count_nonzero(first != second), 0)
        self.assertLessEqual(
            np.abs(first.astype(np.int16) - second.astype(np.int16)).max(),
            1,
        )

    def test_zoom_frame_keeps_center_stable_for_odd_resolution(self):
        width, height = 59, 75
        center_x, center_y = width // 2, height // 2
        x = np.arange(width, dtype=np.int16) - center_x
        y = np.arange(height, dtype=np.int16)[:, None] - center_y
        radial = np.clip(x**2 + y**2, 0, 255).astype(np.uint8)
        frame = np.stack((radial, radial, radial), axis=-1)

        zoomed = video_effects._zoom_frame(frame, 1.2)

        self.assertEqual(zoomed.shape, frame.shape)
        self.assertEqual(zoomed.dtype, frame.dtype)
        # Chiều rộng và chiều cao lẻ chỉ có một pixel trung tâm chính xác, pixel này không được trôi theo chiều ngang hoặc chiều dọc sau khi thu phóng.
        np.testing.assert_allclose(
            zoomed[center_y, center_x],
            frame[center_y, center_x],
            atol=1,
        )


if __name__ == "__main__":
    unittest.main()
