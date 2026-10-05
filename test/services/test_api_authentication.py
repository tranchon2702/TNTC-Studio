import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config


class TestAPIAuthenticationHTTP(unittest.TestCase):
    """Xác minh xác thực tùy chọn của V1 API từ điểm vào ASGI thực tế, bao gồm hai nhóm định tuyến nghiệp vụ."""

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.client = TestClient(asgi.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_empty_key_preserves_existing_open_access(self):
        """Mặc định Key rỗng không yêu cầu tiêu đề yêu cầu, đảm bảo các client cũ và WebUI cục bộ tiếp tục hoạt động."""

        config.app["api_key"] = ""

        response = self.client.get("/api/v1/tasks")

        self.assertEqual(response.status_code, 200)

    def test_video_routes_require_matching_key_when_configured(self):
        """Định tuyến video sau khi bật bảo vệ phải từ chối thống nhất các Key bị thiếu và sai."""

        config.app["api_key"] = "video-secret"

        missing = self.client.get("/api/v1/tasks")
        wrong = self.client.get(
            "/api/v1/tasks",
            headers={"x-api-key": "wrong"},
        )
        accepted = self.client.get(
            "/api/v1/tasks",
            headers={"x-api-key": "video-secret"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_llm_routes_authenticate_before_request_validation(self):
        """Định tuyến LLM phải xác thực trước, yêu cầu chưa xác thực không được vào logic nghiệp vụ có thể phát sinh chi phí."""

        config.app["api_key"] = "llm-secret"

        # Mô hình yêu cầu cung cấp các giá trị mặc định, yêu cầu rỗng cũng có thể gọi LLM thực tế. Tại đây cô lập dịch vụ
        # bên ngoài và đối chiếu số lần gọi, vừa xác minh thứ tự xác thực, vừa tránh kiểm thử tiêu tốn API của người dùng.
        with patch(
            "app.controllers.v1.llm.llm.generate_script",
            return_value="mocked script",
        ) as generate_script:
            missing = self.client.post("/api/v1/scripts", json={})
            accepted = self.client.post(
                "/api/v1/scripts",
                json={},
                headers={"x-api-key": "llm-secret"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        generate_script.assert_called_once()

    def test_openapi_documents_api_key_header_for_v1_routes(self):
        """Swagger phải hiển thị x-api-key, tránh việc sau khi bật bảo vệ chỉ có thể đoán định dạng yêu cầu."""

        schema = self.client.get("/openapi.json").json()
        parameters = schema["paths"]["/api/v1/tasks"]["get"]["parameters"]

        self.assertTrue(
            any(
                parameter["in"] == "header" and parameter["name"] == "x-api-key"
                for parameter in parameters
            )
        )

    def test_duplicate_api_key_headers_are_rejected(self):
        """Việc giải thích thông tin xác thực trùng lặp có thể khác nhau tùy theo proxy, vì vậy phải từ chối bất kể thứ tự."""

        config.app["api_key"] = "video-secret"

        correct_first = self.client.get(
            "/api/v1/tasks",
            headers=[
                ("x-api-key", "video-secret"),
                ("x-api-key", "wrong"),
            ],
        )
        wrong_first = self.client.get(
            "/api/v1/tasks",
            headers=[
                ("x-api-key", "wrong"),
                ("x-api-key", "video-secret"),
            ],
        )

        self.assertEqual(correct_first.status_code, 401)
        self.assertEqual(wrong_first.status_code, 401)


if __name__ == "__main__":
    unittest.main()
