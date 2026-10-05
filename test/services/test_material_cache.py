import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.models.schema import MaterialInfo, VideoAspect
from app.services import material, material_cache


class TestMaterialSearchCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir_patch = patch(
            "app.services.material_cache.utils.storage_dir",
            return_value=self.temp_dir.name,
        )
        self.cache_dir_patch.start()

    def tearDown(self):
        self.cache_dir_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _item(url: str = "https://example.com/video.mp4") -> MaterialInfo:
        return MaterialInfo(
            provider="pixabay",
            url=url,
            duration=12,
            source_info={
                "provider": "pixabay",
                "search_term": "nature",
                "asset_id": "123",
                "source_page": "https://pixabay.com/videos/example-123/",
                "creator": {
                    "id": "456",
                    "name": "Creator",
                    "profile_page": "https://pixabay.com/users/creator-456/",
                },
                "rendition": {
                    "id": "large",
                    "width": 1080,
                    "height": 1920,
                },
            },
        )

    def _cache_path(self) -> Path:
        return material_cache._cache_path(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

    def test_cache_round_trip_preserves_material_fields(self):
        """
        Bộ đệm đĩa phải có khả năng khôi phục tất cả các trường được MaterialInfo yêu cầu trong các quy trình, nó không thể chỉ lưu trữ URL
        Sau đó, nhà cung cấp hoặc thời lượng bị mất, dẫn đến những thay đổi trong hành vi tính toán thời lượng và tải xuống tiếp theo.
        """
        saved = material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[self._item()],
        )
        loaded = material_cache.load_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

        self.assertTrue(saved)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].provider, "pixabay")
        self.assertEqual(loaded[0].url, "https://example.com/video.mp4")
        self.assertEqual(loaded[0].duration, 12)
        self.assertEqual(loaded[0].source_info["search_term"], "nature")
        self.assertEqual(loaded[0].source_info["asset_id"], "123")
        self.assertEqual(
            loaded[0].source_info["source_page"],
            "https://pixabay.com/videos/example-123/",
        )
        self.assertEqual(
            loaded[0].source_info["creator"]["profile_page"],
            "https://pixabay.com/users/creator-456/",
        )

    def test_expired_cache_is_removed_and_treated_as_miss(self):
        """
        Pixabay yêu cầu sử dụng lại kết quả tìm kiếm trong tối đa 24 giờ. Các tập tin hết hạn phải hết hạn và xóa ngay lập tức,
        Điều này ngăn các URL tài liệu cũ được sử dụng lại vô hạn và ngăn thư mục bộ đệm liên tục tích lũy JSON không hợp lệ.
        """
        material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[self._item()],
        )
        cache_path = self._cache_path()
        now = 2_000_000_000.0
        expired_mtime = now - material_cache.MATERIAL_SEARCH_CACHE_TTL_SECONDS - 1
        os.utime(cache_path, (expired_mtime, expired_mtime))

        loaded = material_cache.load_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            now=now,
        )

        self.assertIsNone(loaded)
        self.assertFalse(cache_path.exists())

    def test_future_dated_cache_is_removed_and_treated_as_miss(self):
        """Khi thời gian của hệ thống không bình thường, các dấu thời gian trong tương lai không được phép bỏ qua khoảng thời gian hiệu lực 24 giờ."""
        material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[self._item()],
        )
        cache_path = self._cache_path()
        now = 2_000_000_000.0
        future_mtime = now + 60
        os.utime(cache_path, (future_mtime, future_mtime))

        loaded = material_cache.load_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            now=now,
        )

        self.assertIsNone(loaded)
        self.assertFalse(cache_path.exists())

    def test_corrupted_cache_is_removed_without_breaking_search(self):
        """
        Quá trình thoát bất thường, lỗi ổ đĩa hoặc người dùng sửa đổi thủ công có thể để lại các tệp bị hỏng. Đọc thất bại sẽ quay trở lại
        Tìm kiếm trang web từ xa và dọn sạch các tập tin xấu. Đừng để bộ đệm chặn vĩnh viễn việc tạo vật liệu.
        """
        cache_path = self._cache_path()
        cache_path.write_text("{invalid-json", encoding="utf-8")

        with patch("app.services.material_cache.logger.warning") as warning:
            loaded = material_cache.load_material_search_cache(
                provider="pixabay",
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertIsNone(loaded)
        self.assertFalse(cache_path.exists())
        self.assertTrue(warning.called)

    def test_empty_results_are_not_cached(self):
        """
        Giao diện nhà cung cấp hiện tại sử dụng [] để biểu thị cả không có kết quả và yêu cầu không thành công. Bộ nhớ đệm một danh sách trống sẽ
        Cloudflare chặn hoặc ngừng hoạt động mạng tạm thời trong 24 giờ, do đó, chỉ những kết quả thành công không có giá trị mới có thể được lưu vào bộ đệm.
        """
        saved = material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[],
        )

        self.assertFalse(saved)
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])

    def test_cache_file_does_not_contain_search_parameters_or_credentials(self):
        """
        Tên tệp bộ đệm sử dụng bản tóm tắt và nội dung chỉ lưu trường tài liệu. Ngay cả khi người dùng chia sẻ thư mục lưu trữ,
        Từ khóa, Khóa API hoặc các cấu hình yêu cầu khác cũng sẽ không xuất hiện trong tệp.
        """
        item = self._item()
        item.source_info["source_page"] += "?token=drop"
        item.source_info["creator"]["profile_page"] += "?key=drop"
        material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="private search term",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[item],
        )
        cache_files = list(Path(self.temp_dir.name).glob("*.json"))

        self.assertEqual(len(cache_files), 1)
        self.assertNotIn("private search term", cache_files[0].name)
        raw_payload = cache_files[0].read_text(encoding="utf-8")
        payload = json.loads(raw_payload)
        self.assertEqual(set(payload), {"version", "items"})
        self.assertNotIn("private search term", raw_payload)
        self.assertNotIn("token=drop", raw_payload)

    def test_coverr_signed_urls_are_never_cached(self):
        """Địa chỉ tải xuống Coverr chứa JWT đã ký và không thể đi vào bộ đệm đĩa dài hạn."""
        item = self._item(
            "https://storage.coverr.co/video/download?token=signed-jwt"
        )
        item.provider = "coverr"
        item.source_info["provider"] = "coverr"

        saved = material_cache.save_material_search_cache(
            provider="coverr",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[item],
        )

        self.assertFalse(saved)
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.json")), [])

    def test_coverr_cache_load_removes_legacy_signed_url(self):
        """Khi truy cập Coverr, bạn nên dọn sạch bộ nhớ đệm địa chỉ tải xuống chữ ký mà các phiên bản cũ hơn có thể để lại."""
        cache_path = material_cache._cache_path(
            provider="coverr",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )
        cache_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "provider": "coverr",
                            "url": "https://storage.coverr.co/video?token=signed-jwt",
                            "duration": 12,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        loaded = material_cache.load_material_search_cache(
            provider="coverr",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

        self.assertIsNone(loaded)
        self.assertFalse(cache_path.exists())

    def test_version_one_cache_is_invalidated(self):
        """Bộ nhớ đệm cũ thiếu thông tin nguồn và phải được truy vấn lại sau khi nâng cấp mà không tạo ra các bản ghi tác vụ chưa hoàn chỉnh."""
        cache_path = self._cache_path()
        cache_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "provider": "pixabay",
                            "url": "https://example.com/old.mp4",
                            "duration": 12,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        loaded = material_cache.load_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

        self.assertIsNone(loaded)
        self.assertFalse(cache_path.exists())

    def test_cache_key_separates_provider_duration_and_aspect(self):
        """
        Nguồn tài liệu, thời lượng tối thiểu và kích thước khung hình đều sẽ thay đổi kết quả tìm kiếm từ xa. Mọi thay đổi tham số phải
        Sử dụng bộ nhớ đệm độc lập để tránh trả lại tài liệu không đáp ứng yêu cầu của tác vụ hiện tại cho quy trình tạo video.
        """
        base_path = material_cache._cache_path(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )
        paths = {
            base_path,
            material_cache._cache_path(
                provider="pexels",
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            ),
            material_cache._cache_path(
                provider="pixabay",
                search_term="nature",
                minimum_duration=10,
                video_aspect=VideoAspect.portrait,
            ),
            material_cache._cache_path(
                provider="pixabay",
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.landscape,
            ),
        }

        self.assertEqual(len(paths), 4)

    def test_search_wrapper_reuses_cache_across_calls(self):
        """
        Lần đầu tiên tìm kiếm từ xa được gọi và bộ đệm được ghi, lần thứ hai, các tham số tương tự phải được sử dụng để trực tiếp sử dụng lại kết quả đĩa.
        Đây là hành vi cốt lõi nhằm giảm xác suất xảy ra các lệnh gọi API Pixabay và kích hoạt kiểm soát rủi ro Cloudflare.
        """
        remote_search = Mock(return_value=[self._item()])

        first = material._search_videos_with_cache(
            provider="pixabay",
            search_videos=remote_search,
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )
        second = material._search_videos_with_cache(
            provider="pixabay",
            search_videos=remote_search,
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

        self.assertEqual(remote_search.call_count, 1)
        self.assertEqual(first, second)

    def test_search_wrapper_refreshes_mixed_orientation_cache(self):
        """
        Bộ đệm trước khi nâng cấp có thể chứa các tài liệu từ các hướng khác. Chỉ trả lại một số lượng nhỏ các mục đã lọc sẽ làm giảm tài liệu
        Tính đa dạng, vì vậy khi tìm thấy sự không phù hợp ở bất kỳ hướng nào, toàn bộ tập hợp ứng cử viên sẽ được yêu cầu lại và thay thế.
        """
        portrait_item = self._item("https://example.com/old-portrait.mp4")
        landscape_item = self._item("https://example.com/old-landscape.mp4")
        landscape_item.source_info["rendition"] = {
            "id": "large",
            "width": 1920,
            "height": 1080,
        }
        material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
            items=[portrait_item, landscape_item],
        )

        refreshed_item = self._item("https://example.com/refreshed-portrait.mp4")
        remote_search = Mock(return_value=[refreshed_item])
        results = material._search_videos_with_cache(
            provider="pixabay",
            search_videos=remote_search,
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )

        self.assertEqual(remote_search.call_count, 1)
        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/refreshed-portrait.mp4"],
        )
        cached_items = material_cache.load_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.portrait,
        )
        self.assertEqual(
            [item.url for item in cached_items],
            ["https://example.com/refreshed-portrait.mp4"],
        )

    def test_square_search_reuses_crop_compatible_cache(self):
        """Tác vụ hình vuông sẽ tiếp tục sử dụng lại bộ đệm vật liệu có thể cắt bớt và không thể liên tục yêu cầu đầu từ xa do các hướng ban đầu khác nhau."""
        landscape_item = self._item("https://example.com/landscape.mp4")
        landscape_item.source_info["rendition"] = {
            "id": "large",
            "width": 1920,
            "height": 1080,
        }
        material_cache.save_material_search_cache(
            provider="pixabay",
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.square,
            items=[landscape_item],
        )
        remote_search = Mock(return_value=[])

        results = material._search_videos_with_cache(
            provider="pixabay",
            search_videos=remote_search,
            search_term="nature",
            minimum_duration=5,
            video_aspect=VideoAspect.square,
        )

        self.assertEqual(remote_search.call_count, 0)
        self.assertEqual(
            [item.url for item in results],
            ["https://example.com/landscape.mp4"],
        )

    def test_search_wrapper_retries_after_empty_result(self):
        """Các kết quả trống không được lưu vào bộ nhớ đệm và cuộc gọi tiếp theo vẫn phải truy cập vào đầu từ xa để có thể tự động thử lại sau khi khắc phục lỗi tạm thời."""
        remote_search = Mock(return_value=[])

        for _ in range(2):
            results = material._search_videos_with_cache(
                provider="pixabay",
                search_videos=remote_search,
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )
            self.assertEqual(results, [])

        self.assertEqual(remote_search.call_count, 2)

    def test_cache_read_failure_falls_back_to_remote_search(self):
        """Các ngoại lệ đọc bộ đệm chỉ có thể bị hạ cấp xuống mức bỏ lỡ và không thể chặn tìm kiếm tài liệu từ xa."""
        remote_items = [self._item()]
        remote_search = Mock(return_value=remote_items)

        with patch.object(
            material_cache,
            "load_material_search_cache",
            side_effect=RuntimeError("cache read failed"),
        ), patch.object(material_cache.logger, "warning") as warning:
            results = material._search_videos_with_cache(
                provider="pixabay",
                search_videos=remote_search,
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(results, remote_items)
        self.assertEqual(remote_search.call_count, 1)
        self.assertTrue(warning.called)

    def test_cache_write_failure_keeps_remote_results(self):
        """Sau khi tìm kiếm từ xa thành công, các tài liệu có sẵn phải tiếp tục được trả về ngay cả khi việc ghi vào bộ đệm không thành công."""
        remote_items = [self._item()]
        remote_search = Mock(return_value=remote_items)

        with patch.object(
            material_cache,
            "load_material_search_cache",
            return_value=None,
        ), patch.object(
            material_cache,
            "save_material_search_cache",
            side_effect=RuntimeError("cache write failed"),
        ), patch.object(material_cache.logger, "warning") as warning:
            results = material._search_videos_with_cache(
                provider="pixabay",
                search_videos=remote_search,
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
            )

        self.assertEqual(results, remote_items)
        self.assertEqual(remote_search.call_count, 1)
        self.assertTrue(warning.called)

    def test_invalid_cache_item_does_not_raise(self):
        """Các đối tượng vật chất ngoại lệ không được cho phép ghi vào bộ đệm tùy chọn để làm gián đoạn luồng chính của người gọi."""
        with patch.object(material_cache.logger, "warning") as warning:
            saved = material_cache.save_material_search_cache(
                provider="pixabay",
                search_term="nature",
                minimum_duration=5,
                video_aspect=VideoAspect.portrait,
                items=[None],
            )

        self.assertFalse(saved)
        self.assertTrue(warning.called)

    def test_concurrent_identical_searches_share_remote_request(self):
        """
        Dịch vụ API cho phép nhiều tác vụ chạy đồng thời. Khi tìm kiếm các điều kiện tương tự lần đầu tiên, luồng đến sau sẽ đợi luồng đầu tiên
        Ghi vào bộ đệm thay vì sử dụng lại tín dụng giao diện của bên thứ ba.
        """
        remote_started = threading.Event()
        allow_remote_finish = threading.Event()
        remote_call_lock = threading.Lock()
        remote_call_count = 0
        results = []

        def remote_search(**_kwargs):
            nonlocal remote_call_count
            with remote_call_lock:
                remote_call_count += 1
            remote_started.set()
            self.assertTrue(allow_remote_finish.wait(timeout=2))
            return [self._item()]

        def run_search():
            results.append(
                material._search_videos_with_cache(
                    provider="pixabay",
                    search_videos=remote_search,
                    search_term="shared nature",
                    minimum_duration=5,
                    video_aspect=VideoAspect.portrait,
                )
            )

        first_thread = threading.Thread(target=run_search)
        second_thread = threading.Thread(target=run_search)
        first_thread.start()
        self.assertTrue(remote_started.wait(timeout=2))
        second_thread.start()
        # Dành thời gian cho luồng thứ hai vào khu vực chờ khóa bộ đệm để đảm bảo rằng quá trình kiểm tra bao gồm các lỗi đồng thời thực sự.
        time.sleep(0.05)
        allow_remote_finish.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(remote_call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_cleanup_removes_expired_entries_only(self):
        """Việc dọn dẹp tần suất thấp chỉ xóa bộ đệm đã hết hạn và không ảnh hưởng đến bộ đệm hợp lệ hoặc các tệp người dùng khác."""
        stale_path = self._cache_path()
        stale_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "provider": "pixabay",
                            "url": "https://example.com/stale.mp4",
                            "duration": 12,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fresh_path = material_cache._cache_path(
            provider="pexels",
            search_term="fresh",
            minimum_duration=5,
            video_aspect=VideoAspect.landscape,
        )
        fresh_path.write_text("{}", encoding="utf-8")
        unrelated_path = Path(self.temp_dir.name) / "notes.json"
        unrelated_path.write_text("keep", encoding="utf-8")

        now = 2_000_000_000.0
        stale_mtime = now - material_cache.MATERIAL_SEARCH_CACHE_TTL_SECONDS - 1
        os.utime(stale_path, (stale_mtime, stale_mtime))
        os.utime(fresh_path, (now - 60, now - 60))

        deleted = material_cache.cleanup_expired_material_search_cache(
            now=now,
            force=True,
        )

        self.assertEqual(deleted, 1)
        self.assertFalse(stale_path.exists())
        self.assertTrue(fresh_path.exists())
        self.assertTrue(unrelated_path.exists())


if __name__ == "__main__":
    unittest.main()
