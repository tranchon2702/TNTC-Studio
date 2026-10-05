# -*- coding: utf-8 -*-
import base64
import io
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from app.config import config
from app.services import material


def _png_bytes(width=64, height=96, color=(120, 40, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _image_response(payload, status_code=200):
    return SimpleNamespace(json=lambda: payload, status_code=status_code)


def _download_response(content, status_code=200):
    return SimpleNamespace(status_code=status_code, content=content)


class TestOpenAIImageProvider(unittest.TestCase):
    """
    Nguồn tài liệu hình ảnh Vincent tương thích với OpenAI. Phù hợp với các thử nghiệm nguồn vật liệu khác, tất cả đều sử dụng unittest.mock
    Thay thế các yêu cầu và time.sleep, CI không dựa vào mạng thực, khóa API thực và thanh toán thực.
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        # Xác nhận cần được thực hiện sau khi lệnh tạo trả về và thư mục tạm thời không thể bị hủy trước bằng khối with.
        # Do đó hãy sử dụng mkdtemp + addCleanup để quản lý vòng đời.
        self.save_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)
        config.app["openai_image_base_url"] = "https://img.example.com/v1"
        config.app["openai_image_api_keys"] = ["sk-test-key"]
        config.app["openai_image_model"] = "test-image-model"
        # Các mẫu từ nhắc nhở và kích thước tùy chỉnh bị tắt theo mặc định và các trường hợp sử dụng cần đề cập sẽ tự cấu hình để tránh các nhà phát triển
        # Các cài đặt trong config.toml cục bộ ảnh hưởng đến các xác nhận cho các trường hợp hành vi mặc định.
        config.app.pop("openai_image_prompt_template", None)
        config.app.pop("openai_image_size", None)
        config.app.pop("tls_verify", None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    @staticmethod
    def _generated_item(term, image_path, duration=5):
        item = material.MaterialInfo()
        item.provider = "openai_image"
        item.url = image_path
        item.duration = duration
        item.source_info = {
            "provider": "openai_image",
            "search_term": term,
            "rendition": {"id": None, "width": 736, "height": 1312},
        }
        return item

    # ------------------------------------------------------------------
    # con đường dẫn đến thành công
    # ------------------------------------------------------------------

    def test_generate_images_openai_with_b64_json_response(self):
        """
        Phản hồi b64_json phải được giải mã thành PNG hợp pháp và được ghi vào bản trình diễn theo kích thước hình ảnh thực.
        (Tương thích với trường hợp kích thước được dịch vụ chuyển trả về không nhất quán với yêu cầu), thời lượng ghi lại thời lượng của đoạn mục tiêu.
        """
        image_data = _png_bytes(width=736, height=1312)
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            results = material.generate_images_openai(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
                save_dir=self.save_dir,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "openai_image")
        self.assertEqual(item.duration, 5)
        # Kích thước yêu cầu theo kích thước tương thích chính thức của OpenAI, không sử dụng trực tiếp độ phân giải video
        self.assertEqual(
            post.call_args.args[0],
            "https://img.example.com/v1/images/generations",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "test-image-model",
                "prompt": "sunrise over mountains",
                "n": 1,
                "size": "1024x1536",
            },
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-test-key",
        )
        # Tệp bị rơi là tệp PNG có thể giải mã được
        self.assertTrue(item.url.endswith(".png"))
        self.assertTrue(os.path.isfile(item.url))
        with Image.open(item.url) as saved:
            self.assertEqual(saved.size, (736, 1312))
        # hiển thị ghi lại kích thước thật của hình ảnh và không dựa vào các tham số yêu cầu
        self.assertEqual(
            item.source_info["rendition"],
            {"id": None, "width": 736, "height": 1312},
        )
        self.assertEqual(item.source_info["search_term"], "sunrise over mountains")

    def test_generate_images_openai_with_url_response(self):
        """Phản hồi url phải tải xuống ngay địa chỉ tạm thời và đặt nó vào đĩa."""
        response = _image_response(
            {"data": [{"url": "https://cdn.example.com/generated/abc.png?sig=1"}]}
        )
        download = _download_response(_png_bytes(width=200, height=300))

        with (
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.requests.get", return_value=download) as get,
        ):
            results = material.generate_images_openai(
                "city at night", minimum_duration=3, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertTrue(os.path.isfile(results[0].url))
        # URL tạm thời được tải xuống nguyên trạng và không thể xóa các tham số truy vấn chữ ký.
        self.assertEqual(
            get.call_args.args[0],
            "https://cdn.example.com/generated/abc.png?sig=1",
        )

    def test_generate_images_openai_skips_b64_json_with_invalid_image(self):
        """
        Lớp tương thích trả về 200 nhưng nội dung không phải là hình ảnh có thể giải mã được (chẳng hạn như trang lỗi HTML được ngụy trang dưới dạng JSON)
        Khi đó, một danh sách trống phải được trả về theo thỏa thuận nguồn nguyên liệu để cho phép tầng trên bỏ qua từ khóa thay vì cho phép giải mã.
        Ngoại lệ làm gián đoạn toàn bộ nhiệm vụ.
        """
        fake_content = b"<html><body>gateway degraded</body></html>"
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(fake_content).decode("ascii")}]}
        )

        with patch("app.services.material.requests.post", return_value=response):
            results = material.generate_images_openai(
                "sunrise over mountains", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generate_images_openai_skips_url_download_with_invalid_content(self):
        """URL tạm thời cũng có đường dẫn bỏ qua khi tải xuống nội dung không phải hình ảnh 200."""
        response = _image_response(
            {"data": [{"url": "https://cdn.example.com/generated/abc.png?sig=1"}]}
        )
        download = _download_response(b"\x89PNG\r\n\x1a\nnot-really-a-png")

        with (
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.requests.get", return_value=download),
        ):
            results = material.generate_images_openai(
                "city at night", minimum_duration=3, save_dir=self.save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generate_images_openai_propagates_image_write_failure(self):
        """
        Tác vụ phải bị gián đoạn khi hình ảnh đã được giải mã thành công nhưng việc ghi PNG không thành công. Loại hư hỏng này thường tồn tại
        Nó ảnh hưởng đến các từ khóa tiếp theo. Nếu xác định nhầm rằng nội dung của một trang là bất thường và vẫn tiếp tục, yêu cầu thanh toán không thể thực hiện được sẽ được tạo.
        """
        response = _image_response(
            {
                "data": [
                    {"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}
                ]
            }
        )

        with (
            patch("app.services.material.requests.post", return_value=response),
            patch.object(
                Image.Image,
                "save",
                side_effect=OSError("no space left on device"),
            ),
            self.assertRaisesRegex(OSError, "no space left on device"),
        ):
            material.generate_images_openai(
                "city at night", minimum_duration=3, save_dir=self.save_dir
            )

        self.assertEqual(os.listdir(self.save_dir), [])

    # ------------------------------------------------------------------
    # Thử lại thời gian chờ và xoay khóa
    # ------------------------------------------------------------------

    def test_generate_images_openai_retries_429_with_backoff(self):
        """429 là giới hạn hiện tại tạm thời. Bạn phải lùi lại và thử lại thay vì dừng nhiệm vụ."""
        image_data = _png_bytes()
        responses = [
            _image_response({"error": {"message": "rate limited"}}, status_code=429),
            _image_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "ocean waves", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)
        # Bạn phải chờ đợi tuyến tính trước khi thử lại lần đầu tiên và giao diện từ xa không thể được lấp đầy ngay lập tức.
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(
            sleep.call_args.args[0],
            material.OPENAI_IMAGE_RETRY_BACKOFF_SECONDS[0],
        )

    def test_generate_images_openai_rotates_key_on_401(self):
        """
        401 có nghĩa là khóa hiện tại bị từ chối. Khi nhiều khóa được định cấu hình, thử lại phải sử dụng get_api_key
        Cơ chế xoay chuyển sang phím tiếp theo thay vì liên tục sử dụng cùng một phím bị từ chối.
        """
        config.app["openai_image_api_keys"] = ["sk-bad-key", "sk-good-key"]
        image_data = _png_bytes()

        responses = [
            _image_response({"error": {"message": "unauthorized"}}, status_code=401),
            _image_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_openai(
                "forest fog", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)
        used_keys = [
            call.kwargs["headers"]["Authorization"] for call in post.call_args_list
        ]
        # Hai yêu cầu liên tiếp phải sử dụng các khóa khác nhau và đều xuất phát từ danh sách cấu hình
        self.assertNotEqual(used_keys[0], used_keys[1])
        for auth in used_keys:
            self.assertIn(auth.replace("Bearer ", ""), ["sk-bad-key", "sk-good-key"])

    def test_generate_images_openai_fails_fast_on_401_with_single_key(self):
        """Khi chỉ có một khóa, việc thử lại 401 là vô nghĩa và nó phải nhanh chóng thất bại và trả về kết quả trống."""
        response = _image_response(
            {"error": {"message": "unauthorized"}}, status_code=401
        )

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "desert dunes", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_generate_images_openai_returns_empty_after_retries_exhausted(self):
        """
        Sau khi hết số lần thử lại, một danh sách trống sẽ được trả về theo thỏa thuận nguồn nguyên liệu và từ khóa sẽ được chuyển lên lớp trên để bỏ qua; và đơn hàng sẽ không bị hủy bỏ.
        Mọi tập tin còn sót lại.
        """
        response = _image_response(
            {"error": {"message": "rate limited"}}, status_code=429
        )

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "storm clouds", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(post.call_count, material.OPENAI_IMAGE_MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, material.OPENAI_IMAGE_MAX_ATTEMPTS - 1)
        # Không có tập tin dư nào được tạo ra
        self.assertEqual(os.listdir(self.save_dir), [])

    def test_generate_images_openai_redacts_api_key_in_failure_detail(self):
        """Chi tiết lỗi không thể được ghi bằng văn bản rõ ràng của khóa API vào nhật ký."""
        config.app["openai_image_api_keys"] = ["sk-secret-123"]
        response = _image_response(
            {"error": {"message": "invalid key sk-secret-123 provided"}},
            status_code=401,
        )

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.logger") as logger,
        ):
            results = material.generate_images_openai(
                "redacted term", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(results, [])
        logged = [str(call) for call in logger.error.call_args_list]
        self.assertTrue(logged)
        for message in logged:
            self.assertNotIn("sk-secret-123", message)

    def test_generate_images_openai_retries_generated_image_download(self):
        """
        Hình ảnh đã được tính phí trên cơ sở mỗi hình ảnh. URL tương tự phải được thử lại để tải xuống jitter và không thể khôi phục lại để tái tạo.
        Hình ảnh tương tự dẫn đến việc thanh toán gấp đôi.
        """
        response = _image_response(
            {"data": [{"url": "https://cdn.example.com/generated/x.png"}]}
        )
        downloads = [
            _download_response(b"", status_code=502),
            _download_response(_png_bytes()),
        ]

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", side_effect=downloads) as get,
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_openai(
                "aurora", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(len(results), 1)
        # Quá trình tải xuống đã được thử lại tại cùng một địa chỉ và không kích hoạt thế hệ thanh toán thứ hai.
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertEqual(call.args[0], "https://cdn.example.com/generated/x.png")

    def test_generate_images_openai_returns_empty_on_rejected_request(self):
        """Việc từ chối kinh doanh (chẳng hạn như chính sách nội dung) trả về kết quả trống và thử lại mà không cần chờ đợi."""
        response = _image_response(
            {"error": {"message": "content policy violation"}}, status_code=400
        )

        with (
            tempfile.TemporaryDirectory() as save_dir,
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_openai(
                "blocked term", minimum_duration=5, save_dir=save_dir
            )

        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    # ------------------------------------------------------------------
    # Công tắc cấu hình
    # ------------------------------------------------------------------

    def test_is_openai_image_enabled_requires_full_configuration(self):
        """
        base_url và mô hình là bắt buộc; Khóa API được phép để trống - hoàn toàn cục bộ
        Cổng ComfyUI/SD thường không yêu cầu xác thực.
        """
        self.assertTrue(material.is_openai_image_enabled())

        config.app["openai_image_base_url"] = ""
        self.assertFalse(material.is_openai_image_enabled())
        config.app["openai_image_base_url"] = "https://img.example.com/v1"

        # Cổng không cần xác thực cục bộ: ngay cả khi không có khóa, nó vẫn được coi là đã bật
        config.app["openai_image_api_keys"] = []
        self.assertTrue(material.is_openai_image_enabled())
        config.app["openai_image_api_keys"] = ["sk-test-key"]

        config.app["openai_image_model"] = ""
        self.assertFalse(material.is_openai_image_enabled())

    def test_generate_images_openai_sends_no_authorization_without_key(self):
        """
        Khi khóa API không được định cấu hình, nó phải được tạo như bình thường và yêu cầu không bao gồm tiêu đề Cấp phép.
        Để sử dụng bởi các cổng ComfyUI/SD cục bộ không cần xác thực.
        """
        config.app["openai_image_api_keys"] = []
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
        ):
            results = material.generate_images_openai(
                "local gateway", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_generate_images_openai_retries_connect_timeout(self):
        """
        Thời gian chờ trong giai đoạn kết nối cho biết yêu cầu chưa được gửi đến máy chủ và tác vụ kế toán không thể được tạo.
        Cho phép thử lại thời gian chờ.
        """
        image_data = _png_bytes()
        responses = [
            requests.exceptions.ConnectTimeout("connect timed out"),
            _image_response(
                {"data": [{"b64_json": base64.b64encode(image_data).decode("ascii")}]}
            ),
        ]

        with (
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_openai(
                "connect timeout term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)

    def test_generate_images_openai_does_not_retry_unconfirmed_errors(self):
        """
        Hết thời gian chờ đọc/gián đoạn kết nối thuộc về trạng thái "chưa được xác nhận": máy chủ có thể đã tạo và trừ phí, nhưng
        Không có phản hồi nào được trả lại. Việc gửi lại tự động sẽ gây ra việc tạo lặp lại và thanh toán nhiều lần và phải được thực hiện trực tiếp
        Nếu thất bại, từ khóa sẽ bị lớp trên bỏ qua.
        """
        for error in (
            requests.exceptions.ReadTimeout("read timed out"),
            requests.exceptions.ConnectionError("connection dropped"),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    patch(
                        "app.services.material.requests.post", side_effect=error
                    ) as post,
                    patch("app.services.material.time.sleep") as sleep,
                ):
                    results = material.generate_images_openai(
                        "unconfirmed term", minimum_duration=5, save_dir=self.save_dir
                    )

                self.assertEqual(results, [])
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()

    def test_generate_images_openai_size_defaults_and_override(self):
        """
        Theo mặc định, kích thước tương thích OpenAI chính thức được lấy theo khung hình (dọc 1024x1536 /
        phong cảnh 1536x1024); được che phủ hoàn toàn sau khi định cấu hình openai_image_size,
        Để sử dụng bởi các cổng địa phương hỗ trợ bất kỳ độ phân giải nào.
        """
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as default_post:
            material.generate_images_openai(
                "landscape term",
                minimum_duration=5,
                video_aspect=material.VideoAspect.landscape,
                save_dir=self.save_dir,
            )
        # Màn hình ngang mặc định có kích thước tương thích chính thức với OpenAI
        self.assertEqual(default_post.call_args.kwargs["json"]["size"], "1536x1024")

        config.app["openai_image_size"] = "1080x1920"
        self.addCleanup(config.app.pop, "openai_image_size", None)

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "custom size term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(
            post.call_args.kwargs["json"]["size"],
            "1080x1920",
        )

    def test_generate_images_openai_raises_without_base_url(self):
        """Khi gọi trực tiếp và base_url không được định cấu hình, phải đưa ra lỗi với hướng dẫn cấu hình."""
        config.app["openai_image_base_url"] = ""
        with self.assertRaises(ValueError):
            material.generate_images_openai("term", minimum_duration=5)

    # ------------------------------------------------------------------
    # mẫu lời nhắc
    # ------------------------------------------------------------------

    def test_generate_images_openai_applies_prompt_template(self):
        """
        Khi mẫu chứa phần giữ chỗ {term} được định cấu hình, lời nhắc được yêu cầu phải là kết quả thay thế mẫu.
        Các sửa đổi kiểu bổ sung thống nhất cải thiện sự phù hợp của hình ảnh và văn bản.
        """
        config.app["openai_image_prompt_template"] = (
            "cinematic photo of {term}, photorealistic, high detail"
        )
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "晨光中的玻璃杯", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(
            post.call_args.kwargs["json"]["prompt"],
            "cinematic photo of 晨光中的玻璃杯, photorealistic, high detail",
        )

    def test_generate_images_openai_sends_raw_term_without_template(self):
        """Khi mẫu không được định cấu hình, lời nhắc phải là văn bản gốc của từ khóa và hành vi nhất quán với phiên bản cũ."""
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "raw term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(post.call_args.kwargs["json"]["prompt"], "raw term")

    def test_generate_images_openai_falls_back_when_template_lacks_placeholder(self):
        """Khi mẫu không chứa trình giữ chỗ {term} thì không thể chèn từ khóa và phải trả lại văn bản gốc."""
        config.app["openai_image_prompt_template"] = "no placeholder here"
        response = _image_response(
            {"data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]}
        )

        with patch(
            "app.services.material.requests.post", return_value=response
        ) as post:
            material.generate_images_openai(
                "fallback term", minimum_duration=5, save_dir=self.save_dir
            )

        self.assertEqual(post.call_args.kwargs["json"]["prompt"], "fallback term")

    # ------------------------------------------------------------------
    # download_videos phân phối và tạo theo yêu cầu
    # ------------------------------------------------------------------

    def test_download_videos_openai_image_generates_on_demand_and_stops(self):
        """
        Hình ảnh của Vincent được tính phí trên cơ sở mỗi bức ảnh và không thể được tạo cho tất cả các từ khóa trước rồi mới chọn. Vật liệu phải theo yêu cầu từng cái một
        Tạo, sau khi thời lượng hiệu quả tích lũy (giới hạn theo thời lượng phân đoạn) đạt đến thời lượng lồng tiếng cần thiết, các từ khóa tiếp theo
        Không có yêu cầu thanh toán nào được kích hoạt nữa.
        """
        generated = {
            "term-1": [self._generated_item("term-1", "/tmp/img-1.png")],
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        def fake_render(image_path, clip_duration):
            return f"{image_path}.mp4"

        with (
            patch(
                "app.services.material.generate_images_openai",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                side_effect=fake_render,
            ) as render,
        ):
            result = material.download_videos(
                task_id="test-openai-image-lazy",
                search_terms=["term-1", "term-2", "term-3"],
                source="openai_image",
                audio_duration=8,
                max_clip_duration=5,
            )

        # 5s + 5s > 8s, từ khóa thứ ba không thể tạo yêu cầu tạo trả phí nữa
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call.kwargs["search_term"] for call in generate.call_args_list],
            ["term-1", "term-2"],
        )
        # Mỗi hình ảnh được hiển thị thành một clip mp4 và không được tính vào thời lượng.
        self.assertEqual(render.call_count, 2)
        self.assertEqual(result, ["/tmp/img-1.png.mp4", "/tmp/img-2.png.mp4"])

    def test_download_videos_openai_image_continues_after_invalid_image(self):
        """
        Khi không thể giải mã được phản hồi giao diện tương thích đầu tiên, chỉ có từ khóa tương ứng bị bỏ qua và hình ảnh pháp lý tiếp theo vẫn có thể được
        Hoàn tất việc sắp xếp và hiển thị, đồng thời xác minh rằng bản sửa lỗi bao gồm chuỗi cuộc gọi tạo theo yêu cầu thực sự chứ không chỉ một chức năng duy nhất.
        """
        invalid_response = _image_response(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(
                            b"<html>gateway degraded</html>"
                        ).decode("ascii")
                    }
                ]
            }
        )
        valid_response = _image_response(
            {
                "data": [
                    {"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}
                ]
            }
        )
        config.app["material_directory"] = self.save_dir

        with (
            patch(
                "app.services.material.requests.post",
                side_effect=[invalid_response, valid_response],
            ) as post,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/rendered-openai-image.mp4",
            ) as render,
            patch("app.services.material._persist_material_sources"),
        ):
            result = material.download_videos(
                task_id="test-openai-image-invalid-then-valid",
                search_terms=["invalid term", "valid term"],
                source="openai_image",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(result, ["/tmp/rendered-openai-image.mp4"])

    def test_download_videos_openai_image_stops_when_duration_exactly_covered(self):
        """Hồi quy biên: Chỉ cần đủ thời gian là đủ, phán đoán dừng phải là >= thay vì >."""
        generated = {
            "term-1": [self._generated_item("term-1", "/tmp/img-1.png")],
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_images_openai",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/rendered.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-openai-image-exact",
                search_terms=["term-1", "term-2", "term-3"],
                source="openai_image",
                audio_duration=10,
                max_clip_duration=5,
            )

        # 5s + 5s == 10s, được bao phủ chính xác, không được tạo đoạn thứ 3
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(len(result), 2)

    def test_download_videos_openai_image_bypasses_search_cache(self):
        """
        Kết quả được tạo là các tệp hình ảnh một lần và không tham gia vào bộ đệm tìm kiếm 24 giờ - bộ đệm sẽ tạo ra sự khác biệt
        Nhiệm vụ là lặp đi lặp lại cùng một hình ảnh. download_videos phải truy cập trực tiếp vào nhánh xây dựng theo yêu cầu.
        """
        with (
            patch(
                "app.services.material.generate_images_openai",
                return_value=[self._generated_item("sunrise", "/tmp/img-1.png")],
            ) as generate,
            patch("app.services.material._search_videos_with_cache") as cached_search,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/img-1.png.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-openai-image-cache-bypass",
                search_terms=["sunrise"],
                source="openai_image",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 1)
        cached_search.assert_not_called()
        self.assertEqual(result, ["/tmp/img-1.png.mp4"])

    def test_download_videos_openai_image_skips_failed_segment_and_continues(self):
        """
        Nếu việc tạo tờ rơi không thành công (kết quả trống) hoặc hiển thị không thành công, hãy bỏ qua từ khóa này và tiếp tục tạo các đoạn tiếp theo.
        Vật liệu thành công được trả lại như bình thường.
        """
        generated = {
            "term-1": [],  # Xây dựng không thành công
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        def fake_render(image_path, clip_duration):
            if "img-2" in image_path:
                return ""  # kết xuất thuật ngữ 2 không thành công
            return f"{image_path}.mp4"

        with (
            patch(
                "app.services.material.generate_images_openai",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                side_effect=fake_render,
            ),
        ):
            result = material.download_videos(
                task_id="test-openai-image-skip",
                search_terms=["term-1", "term-2", "term-3"],
                source="openai_image",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 3)
        # Việc hiển thị thuật ngữ-2 không thành công và bị bỏ qua. Chỉ những đoạn của học kỳ 3 mới lọt vào bộ phim cuối cùng.
        self.assertEqual(result, ["/tmp/img-3.png.mp4"])

    def test_download_videos_openai_image_skips_generation_without_audio(self):
        """Nếu thời lượng lồng tiếng không dương, bạn sẽ trở về tay không và bạn sẽ không trả tiền cho mỗi bức ảnh cho những nhiệm vụ bất khả thi."""
        with patch("app.services.material.generate_images_openai") as generate:
            result = material.download_videos(
                task_id="test-openai-image-no-audio",
                search_terms=["term-1"],
                source="openai_image",
                audio_duration=0,
                max_clip_duration=5,
            )

        generate.assert_not_called()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
