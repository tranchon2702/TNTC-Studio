"""
Upload-Post API integration for cross-posting videos to TikTok, Instagram and YouTube Shorts.

Docs: https://docs.upload-post.com
"""
import os
from typing import Optional

import requests
from loguru import logger
from app.config import config


class UploadPostService:
    API_BASE = "https://api.upload-post.com"

    @property
    def api_key(self) -> str:
        return config.app.get("upload_post_api_key", "")

    @property
    def username(self) -> str:
        return config.app.get("upload_post_username", "")

    @property
    def enabled(self) -> bool:
        return config.app.get("upload_post_enabled", False)

    @property
    def platforms(self) -> list:
        return config.app.get("upload_post_platforms", ["tiktok", "instagram"])

    @property
    def auto_upload(self) -> bool:
        return config.app.get("upload_post_auto_upload", False)

    @property
    def youtube_privacy_status(self) -> str:
        return config.app.get("upload_post_youtube_privacy_status", "public")

    @property
    def youtube_made_for_kids(self) -> bool:
        # Khi không được định cấu hình, các video tải lên YouTube tiếp theo được tuyên bố rõ ràng là không dành cho trẻ em; nội dung dành cho trẻ em cần phải được đặt thành true.
        # Điều này khác với phiên bản cũ là bỏ qua trường xác nhận nhưng không sửa đổi video đã tải lên.
        # Hãy xác minh nghiêm ngặt loại trước khi tải lên để tránh báo cáo sai đối tượng bằng cách coi chuỗi TOML là "false" làm giá trị thực.
        return config.app.get("upload_post_youtube_made_for_kids", False)

    def is_configured(self) -> bool:
        return bool(self.api_key and self.username and self.enabled)

    def upload_video(
        self,
        video_path: str,
        title: str,
        platforms: Optional[list] = None,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        youtube_extra: Optional[dict] = None,
    ) -> dict:
        if not self.is_configured():
            logger.warning("Upload-Post is not configured. Skipping cross-post.")
            return {"success": False, "error": "Upload-Post not configured"}

        if platforms is None:
            platforms = self.platforms

        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return {"success": False, "error": f"Video file not found: {video_path}"}

        has_youtube = any(p.startswith("youtube") for p in platforms)
        if has_youtube:
            # Ảnh chụp nhanh khai báo khi tác vụ nền được chuyển vào hàng đợi; cấu hình hiện tại chỉ được đọc khi dịch vụ được gọi trực tiếp.
            made_for_kids = (youtube_extra or {}).get(
                "selfDeclaredMadeForKids", self.youtube_made_for_kids
            )
            if not isinstance(made_for_kids, bool):
                error = "YouTube made-for-kids setting must be a boolean"
                logger.error(error)
                return {"success": False, "error": error}

        logger.info(f"Cross-posting video to {', '.join(platforms)} via Upload-Post...")

        try:
            with open(video_path, 'rb') as video_file:
                files = {'video': video_file}

                data = [
                    ('user', self.username),
                    ('title', title[:2200]),
                    ('privacy_level', privacy_level),
                ]

                for platform in platforms:
                    data.append(('platform[]', platform))

                if has_youtube:
                    # biểu mẫu nhiều phần sử dụng chuỗi boolean chữ thường và không thể dựa vào siêu dữ liệu LLM
                    # tồn tại; vượt qua tuyên bố về đối tượng của người dùng một cách rõ ràng bất cứ khi nào đăng lên YouTube.
                    data.append(('selfDeclaredMadeForKids', str(made_for_kids).lower()))
                    logger.info(f"YouTube audience declaration: made_for_kids={made_for_kids}")

                if youtube_extra and has_youtube:
                    if "youtube_title" in youtube_extra:
                        data.append(('youtube_title', youtube_extra["youtube_title"][:100]))
                    if "youtube_description" in youtube_extra:
                        data.append(('youtube_description', youtube_extra["youtube_description"]))
                    for tag in youtube_extra.get("tags", []):
                        data.append(('tags[]', tag))
                    data.append(('privacyStatus', youtube_extra.get("privacyStatus", "public")))
                    data.append(('containsSyntheticMedia', "true"))

                headers = {'Authorization': f'Apikey {self.api_key}'}

                response = requests.post(
                    f"{self.API_BASE}/api/upload",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=300,
                )

                response.raise_for_status()
                result = response.json()

                if result.get('success'):
                    logger.info(f"✅ Video cross-posted successfully! Request ID: {result.get('request_id')}")
                else:
                    logger.warning(f"Cross-post failed: {result.get('message', 'Unknown error')}")

                return result

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to cross-post video: {str(e)}")
            return {"success": False, "error": str(e)}

    def check_status(self, request_id: str) -> dict:
        """
        Check the status of an upload request.

        Args:
            request_id (str): The request ID from upload

        Returns:
            dict: Status information
        """
        try:
            headers = {
                'Authorization': f'Apikey {self.api_key}'
            }

            response = requests.get(
                f"{self.API_BASE}/api/uploadposts/status",
                params={'request_id': request_id},
                headers=headers,
                timeout=30
            )

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to check status: {str(e)}")
            return {"success": False, "error": str(e)}


# Singleton instance
upload_post_service = UploadPostService()


def cross_post_video(
    video_path: str,
    title: str,
    platforms: Optional[list] = None,
    youtube_extra: Optional[dict] = None,
) -> dict:
    return upload_post_service.upload_video(video_path, title, platforms, youtube_extra=youtube_extra)
