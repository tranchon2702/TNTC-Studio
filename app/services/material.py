import base64
import io
import math
import os
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, List
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image, UnidentifiedImageError

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import (
    material_cache,
    metaso_minimax,
    ofox,
    task_artifacts,
    video,
    volcengine_seedance,
)
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


class _OpenAIImageDecodeError(ValueError):
    """Cho biết rằng các byte được giao diện tương thích trả về không thể được giải mã thành hình ảnh và không bao gồm lỗi ghi tệp cục bộ."""


def _safe_public_url(value: Any) -> str | None:
    """
    Chỉ giữ lại các địa chỉ trang HTTP(S) có thể xem công khai và xóa các tham số truy vấn cũng như thông tin xác thực.

    Địa chỉ tải xuống tài liệu có thể mang Khóa API, JWT đã ký hoặc mã thông báo tạm thời. Danh sách nhiệm vụ chỉ yêu cầu
    Để giúp người dùng quay lại trang tài liệu công khai của nhà cung cấp, không nên lưu các tham số xác thực; URL ở dạng thông tin người dùng
    Đồng thời từ chối và tránh những nội dung như ``https://user:pass@example.com``.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _creator_info(value: Any) -> dict[str, str] | None:
    """Trích xuất các trường công khai thống nhất từ ​​cấu trúc tác giả từ các nhà cung cấp khác nhau."""
    if isinstance(value, str) and value.strip():
        return {"name": value.strip()}
    if not isinstance(value, dict):
        return None

    creator: dict[str, str] = {}
    creator_id = value.get("id")
    creator_name = value.get("name") or value.get("username")
    creator_page = _safe_public_url(
        value.get("url") or value.get("profile_url") or value.get("profile_page")
    )
    if creator_id is not None:
        creator["id"] = str(creator_id)
    if creator_name:
        creator["name"] = str(creator_name)
    if creator_page:
        creator["profile_page"] = creator_page
    return creator or None


def _material_source_record(item: MaterialInfo, local_path: str) -> dict[str, Any]:
    """
    Tạo bản ghi xuất xứ nhẹ cho nội dung được tải xuống thành công.

    ``source_info`` có thể đến từ bộ đệm hoặc thậm chí từ ``MaterialInfo`` được xây dựng bên ngoài, vì vậy
    Không thể viết như cũ được. Nó được cơ cấu lại theo danh sách trắng và chỉ giữ lại trang công khai, logo doanh nghiệp và kích thước.
    Và chỉ ghi lại tên tệp cục bộ để ngăn thư mục người dùng hoặc đường dẫn gắn Docker vào tệp tác vụ.
    """
    source = item.source_info if isinstance(item.source_info, dict) else {}
    record: dict[str, Any] = {
        "provider": str(item.provider or source.get("provider") or ""),
        "local_file": Path(local_path).name,
        "duration": int(item.duration),
    }

    search_term = source.get("search_term")
    asset_id = source.get("asset_id")
    source_page = _safe_public_url(source.get("source_page"))
    if isinstance(search_term, str) and search_term.strip():
        record["search_term"] = search_term.strip()
    if asset_id not in (None, ""):
        record["asset_id"] = str(asset_id)
    if source_page:
        record["source_page"] = source_page

    creator = _creator_info(source.get("creator"))
    if creator:
        record["creator"] = creator

    raw_rendition = source.get("rendition")
    if isinstance(raw_rendition, dict):
        rendition = {}
        for field in ("id", "width", "height"):
            value = raw_rendition.get(field)
            if value not in (None, ""):
                rendition[field] = str(value) if field == "id" else value
        if rendition:
            record["rendition"] = rendition
    return record


def _persist_material_sources(
    task_id: str,
    material_sources: list[dict[str, Any]],
) -> None:
    """
    Thêm các nguồn tài liệu hiện đã tải xuống thành công vào danh sách nhiệm vụ.

    Ghi tác vụ là một khả năng phụ trợ và không thể thay đổi giá trị trả về của chức năng tải xuống video, cũng như không thể do lỗi ghi đĩa.
    Làm gián đoạn quá trình sản xuất phim chính. ``patch_script_data`` sẽ chịu trách nhiệm thay thế nguyên tử và ghi nhật ký ngoại lệ; chỉ ở đây
    Sau khi thành công, số lượng được ghi lại để tạo điều kiện xác nhận xem thông tin truy xuất nguồn gốc của nhiệm vụ đã được đặt hay chưa.
    """
    try:
        saved = task_artifacts.patch_script_data(
            task_id,
            material_sources=material_sources,
        )
        if saved:
            logger.info(
                f"saved material source records: "
                f"task_id={task_id}, count={len(material_sources)}"
            )
    except Exception as exc:
        # Bản thân task_artifacts đã được thiết kế để giảm thiểu lỗi và sự cô lập cuối cùng vẫn được giữ lại ở đây.
        # Ngăn chặn các điều chỉnh triển khai trong tương lai hoặc ngoại lệ phân tích cú pháp thư mục vô tình ảnh hưởng đến giá trị trả về tải xuống tài liệu.
        logger.warning(
            "failed to persist material source records: "
            f"task_id={task_id}, error={type(exc).__name__}, detail={exc}"
        )


def _get_tls_verify() -> bool:
    # Xác minh chứng chỉ TLS được bật theo mặc định để ngăn quá trình tìm kiếm và tải xuống tài liệu khỏi bị người trung gian giả mạo.
    # Chỉ trong những trường hợp được yêu cầu rõ ràng như đại lý công ty và chứng chỉ tự ký, người dùng mới được phép vượt qua
    # Cài đặt rõ ràng `tls_verify = false` trong `config.toml` tạm thời bị tắt.
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\n"
            f"Please set it in the config.toml file: {config.config_file}\n"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def _redact_secret(message: str, secret: str) -> str:
    """
    Giảm thiểu tối đa văn bản bất thường sẽ được ghi vào nhật ký.

    Ngoại lệ kết nối cho các yêu cầu có thể chứa URL yêu cầu đầy đủ, trong khi Khóa API Pixabay được truy vấn bởi
    Truyền tham số. Ở đây, giá trị ban đầu và giá trị được mã hóa URL được thay thế cùng lúc, không chỉ giữ lại thông tin lỗi mạng để khắc phục sự cố,
    Điều này cũng ngăn khóa nhập vào tệp nhật ký.
    """
    safe_message = str(message)
    if not secret:
        return safe_message

    safe_message = safe_message.replace(secret, "***")
    encoded_secret = quote_plus(secret)
    if encoded_secret != secret:
        safe_message = safe_message.replace(encoded_secret, "***")
    return safe_message


def _redact_request_error(error: Exception, *secrets: str) -> str:
    """
    Lưu giữ thông tin khắc phục sự cố về các điểm bất thường của mạng trong khi xóa khóa API và thông tin xác thực proxy.

    Chỉ ghi nhật ký trực tiếp loại ngoại lệ sẽ làm mất ngữ cảnh chính như DNS, chứng chỉ, thời gian chờ, v.v. Ghi nhật ký trực tiếp ngoại lệ ban đầu
    Cũng có thể lặp lại URL yêu cầu đầy đủ. Lối vào thống nhất cho phép ba nhà cung cấp vật liệu sử dụng các quy tắc giải mẫn cảm giống nhau.
    """
    safe_message = str(error)
    for secret in secrets:
        safe_message = _redact_secret(safe_message, str(secret or ""))
    for proxy_url in config.proxy.values():
        safe_message = _redact_secret(safe_message, str(proxy_url))
    return safe_message


def _is_cloudflare_challenge(response: requests.Response) -> bool:
    """
    Nhận biết Thử thách HTML do Cloudflare trả về thay vì coi nó là JSON JSON.

    Cloudflare thường đặt `cf-minigated: challenge`; một số triển khai chỉ trả về `cf-mitigated: challenge` với
    "Chỉ một lát" hoặc HTML nền tảng thử thách, do đó duy trì các đặc điểm nội dung.
    Nội dung phản hồi chỉ được đánh giá trong bộ nhớ và không được ghi vào nhật ký để tránh ghi lại các phần lớn HTML vô giá trị.
    """
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True

    content_type = str(headers.get("content-type", "")).lower()
    if "text/html" not in content_type:
        return False

    body = str(getattr(response, "text", "")).lower()
    return "just a moment" in body or "/cdn-cgi/challenge-platform/" in body


def _matches_video_aspect(
    width: Any,
    height: Any,
    video_aspect: VideoAspect,
    *,
    is_vertical: Any = None,
) -> bool:
    """
    Xác định xem vật liệu từ xa có cùng hướng với màn hình mục tiêu hay không.

    Các trường phản hồi của Pexels, Pixabay và Coverr không đồng nhất, vì vậy trước tiên hãy sử dụng chiều rộng và chiều cao để đưa ra phán đoán đáng tin cậy;
    Coverr sử dụng giá trị Boolean ``is_vertical`` rõ ràng khi một số phản hồi lịch sử bị thiếu thứ nguyên.
    Các vật liệu không thể xác nhận hướng sẽ được bỏ qua trực tiếp để tránh các tác vụ màn hình dọc bị trộn lẫn với các vật liệu màn hình ngang và gây ra viền đen trong phim cuối cùng.
    """
    aspect = VideoAspect(video_aspect)
    try:
        normalized_width = int(float(width))
        normalized_height = int(float(height))
    except (TypeError, ValueError):
        normalized_width = 0
        normalized_height = 0

    if normalized_width > 0 and normalized_height > 0:
        if aspect == VideoAspect.portrait:
            return normalized_height > normalized_width
        if aspect == VideoAspect.landscape:
            return normalized_width > normalized_height
        return normalized_width == normalized_height

    if isinstance(is_vertical, bool) and aspect != VideoAspect.square:
        return is_vertical == (aspect == VideoAspect.portrait)
    return False


def _filter_materials_by_aspect(
    items: List[MaterialInfo],
    video_aspect: VideoAspect,
) -> List[MaterialInfo]:
    """
    Xác minh lại hướng của kết quả được lưu trong bộ nhớ đệm.

    Bộ đệm tìm kiếm tài liệu được giữ lại tối đa 24 giờ và bộ đệm được ghi trước khi nâng cấp có thể chứa tài liệu có hướng không khớp.
    Việc lọc mục nhập bộ nhớ đệm hợp nhất cho phép bản sửa lỗi có hiệu lực ngay lập tức và cũng bảo vệ khỏi các nhà cung cấp bên thứ ba hoặc bộ nhớ đệm cũ
    Sàng lọc ở xa bị bỏ sót. Các mục cũ có kích thước hiển thị không thể đọc được sẽ được coi là không hợp lệ và bị bỏ qua.
    """
    aspect = VideoAspect(video_aspect)
    if aspect == VideoAspect.square:
        # Pixabay và Coverr hiếm khi cung cấp cảnh vuông gốc. Đầu ra vuông tuân theo hành vi hiện có,
        # Tiếp nhận các ứng viên có sẵn và chuyển sang khâu tổng hợp video để cắt xén tránh trường hợp không còn nguyên liệu cho nhiệm vụ 1:1 sau khi nâng cấp.
        return list(items)

    filtered_items = []
    for item in items:
        source_info = item.source_info if isinstance(item.source_info, dict) else {}
        rendition = source_info.get("rendition")
        rendition = rendition if isinstance(rendition, dict) else {}
        if _matches_video_aspect(
            rendition.get("width"),
            rendition.get("height"),
            aspect,
        ):
            filtered_items.append(item)
    return filtered_items


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/v1/videos/search?{urlencode(params)}"
    logger.info(f"searching videos on pexels: term={search_term!r}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error("pexels video search returned an unsupported response")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if (
                    _matches_video_aspect(w, h, aspect)
                    and w == video_width
                    and h == video_height
                ):
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    item.source_info = {
                        "provider": "pexels",
                        "search_term": search_term,
                        "asset_id": (
                            str(v.get("id")) if v.get("id") is not None else None
                        ),
                        "source_page": _safe_public_url(v.get("url")),
                        "creator": _creator_info(v.get("user")),
                        "rendition": {
                            "id": (
                                str(video.get("id"))
                                if video.get("id") is not None
                                else None
                            ),
                            "width": w,
                            "height": h,
                        },
                    }
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(
            "pexels video search failed: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        )

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(
        f"searching videos on pixabay: term={search_term!r}, "
        f"proxy_enabled={bool(config.proxy)}"
    )

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        status_code = int(getattr(r, "status_code", 200))
        headers = getattr(r, "headers", {}) or {}
        content_type = str(headers.get("content-type", ""))
        retry_after = headers.get("retry-after")
        cf_ray = headers.get("cf-ray")

        if _is_cloudflare_challenge(r):
            logger.error(
                "pixabay search was blocked by a Cloudflare challenge: "
                f"status={status_code}, cf_ray={cf_ray or 'unknown'}. "
                "Check the server network or proxy, or use Pexels/Coverr instead."
            )
            return []

        if status_code == 429:
            logger.error(
                "pixabay API rate limit exceeded: "
                f"status=429, retry_after={retry_after or 'unknown'}"
            )
            return []

        if status_code >= 400:
            logger.error(
                "pixabay search request failed: "
                f"status={status_code}, content_type={content_type or 'unknown'}"
            )
            return []

        try:
            response = r.json()
        except ValueError:
            logger.error(
                "pixabay returned an unexpected non-JSON response: "
                f"status={status_code}, content_type={content_type or 'unknown'}"
            )
            return []

        video_items = []
        if "hits" not in response:
            logger.error("pixabay video search returned an unsupported response")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                try:
                    w = int(video["width"])
                    h = int(video["height"])
                except (KeyError, TypeError, ValueError):
                    continue
                # Pixabay hiếm khi trả về video vuông gốc; Đầu ra 1:1 tiếp tục chấp nhận độ phân giải thỏa mãn
                # các ứng cử viên và được cắt tỉa ở giai đoạn tổng hợp. Màn hình ngang và dọc phải phù hợp chặt chẽ với hướng mục tiêu.
                orientation_matches = aspect == VideoAspect.square or (
                    _matches_video_aspect(w, h, aspect)
                )
                if orientation_matches and w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    item.source_info = {
                        "provider": "pixabay",
                        "search_term": search_term,
                        "asset_id": (
                            str(v.get("id")) if v.get("id") is not None else None
                        ),
                        "source_page": _safe_public_url(v.get("pageURL")),
                        "creator": _creator_info(
                            {
                                "id": v.get("user_id"),
                                "name": v.get("user"),
                            }
                        ),
                        "rendition": {
                            "id": video_type,
                            "width": w,
                            "height": video.get("height"),
                        },
                    }
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        error_message = _redact_request_error(e, api_key)
        logger.error(
            "pixabay search request failed: "
            f"error={type(e).__name__}, detail={error_message}"
        )

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - Xác thực: Ủy quyền: Bearer <api_key>
      - Điểm cuối tìm kiếm: GET /video?query=..., cấu trúc phản hồi {"hits": [...], ...}
      - Thêm ?urls=true để trả về trực tiếp liên kết trực tiếp mp4 trong phản hồi tìm kiếm
      - URL được ký JWT (bound API key, không có thời gian hết hạn)
      - Coverr hỗ trợ lọc các vật liệu màn hình ngang và dọc thông qua filter=is_vertical:true/false;
        Sau khi phản hồi được trả về, xác minh cục bộ vẫn được thực hiện dựa trên max_width/max_height hoặc is_vertical.
      - Trường thời lượng tồn tại ở cả dạng số và dạng chuỗi và hàm này chấp nhận cả hai.

    Hàm này sử dụng trường urls.mp4_download làm địa chỉ tải xuống - theo tài liệu chính thức của Coverr
    (https://api.coverr.co/docs/videos/#download-a-video),
    Bản thân URL GET được Coverr coi là sự kiện tải xuống hợp pháp và được đưa vào số liệu thống kê.
    Không cần phải gọi PATCH /videos/:id/stats/downloads nữa.
    """
    aspect = VideoAspect(video_aspect)
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 20,
        "urls": "true",
        "sort": "popular",
    }
    # Tính năng lọc phía máy chủ có thể trả về trực tiếp các tài liệu mục tiêu từ các kết quả tìm kiếm hoàn chỉnh, tránh việc phải tìm nạp các kết quả phổ biến trước rồi mới đến.
    # Lọc cục bộ dẫn đến các ứng cử viên chân dung trống. Vật liệu hình vuông không tương ứng với các điều kiện Boolean và tiếp tục dựa vào xác minh chiều rộng và chiều cao cục bộ.
    if aspect == VideoAspect.portrait:
        params["filter"] = "is_vertical:true"
    elif aspect == VideoAspect.landscape:
        params["filter"] = "is_vertical:false"
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching videos on coverr: term={search_term!r}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or "hits" not in response:
            logger.error("coverr video search returned an unsupported response")
            return video_items

        for v in response["hits"]:
            # thời lượng có thể là số (11,625) hoặc chuỗi ("10,500000") trong các phản hồi khác nhau
            try:
                duration = int(float(v.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue
            if aspect != VideoAspect.square and not _matches_video_aspect(
                v.get("max_width"),
                v.get("max_height"),
                aspect,
                is_vertical=v.get("is_vertical"),
            ):
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            item.source_info = {
                "provider": "coverr",
                "search_term": search_term,
                "asset_id": str(video_id),
                "source_page": _safe_public_url(v.get("canonical_url") or v.get("url")),
                "creator": _creator_info(v.get("creator") or v.get("author")),
                "rendition": {
                    "id": "mp4_download",
                    "width": v.get("max_width"),
                    "height": v.get("max_height"),
                },
            }
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(
            "coverr video search failed: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        )

    return []


# WaveSpeed ​​​​AI (https://wavespeed.ai) sử dụng mô hình video Wensheng để trực tiếp tạo tài liệu dựa trên từ khóa của tập lệnh.
# Chia sẻ cấu trúc kết quả MaterialInfo cũng như các quá trình tải xuống và chỉnh sửa tiếp theo với ba nguồn tài liệu có sẵn.
WAVESPEED_API_BASE_URL = "https://api.wavespeed.ai/api/v3"
WAVESPEED_DEFAULT_T2V_MODEL = "bytedance/seedance-2.0-fast/text-to-video"
WAVESPEED_POLL_INTERVAL_SECONDS = 2.0
WAVESPEED_RUN_TIMEOUT_SECONDS = 600.0
# Mô hình mặc định byteance/seedance-2.0-fast/text-to-video chỉ chấp nhận 4-15 giây; vượt quá
# Các yêu cầu về phạm vi sẽ bị API trực tiếp từ chối. Độ dài đoạn mặc định của WebUI là 3 giây, vì vậy nó phải được gửi trước
# Trước khi hội tụ đến phạm vi hỗ trợ mô hình, thời lượng vượt quá sẽ bị cắt bỏ bởi quá trình chỉnh sửa hiện có theo thời lượng của clip.
WAVESPEED_MIN_DURATION_SECONDS = 4
WAVESPEED_MAX_DURATION_SECONDS = 15
# Ba trạng thái lỗi có ngữ nghĩa khác nhau (lỗi mô hình/hủy người dùng/hết thời gian chờ nền tảng), nhưng chúng đều có ý nghĩa đối với quy trình vật chất
# Từ khóa này không có sản phẩm nên sẽ được coi là kết quả trống và được chuyển lên lớp trên để bỏ qua đoạn và tiếp tục tạo ra nó.
WAVESPEED_FAILURE_STATUSES = frozenset({"failed", "cancelled", "timeout"})
# Giữ nguyên cỡ nòng như nút Python SDK/n8n chính thức của WaveSpeed: 429 và 5xx là tạm thời
# Thất bại đáng để thử lại thời gian chờ hạn chế; 4xx là lỗi máy khách rõ ràng và nhanh chóng bị lỗi.
WAVESPEED_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# Số lần thất bại tạm thời liên tiếp được phép trong một cuộc thăm dò. Một GET không may mắn không thể khiến một tác vụ đã được tính phí bị ngắt kết nối.
WAVESPEED_MAX_POLL_RETRIES = 5
# Cơ sở lùi tuyến tính, lần thử lại thứ n đợi cơ sở * n giây.
WAVESPEED_RETRY_BASE_SECONDS = 1.0
# Số lần thử lại cho cùng một địa chỉ chữ ký khi tải xuống sản phẩm không thành công. Các tài liệu đã được tạo ra cho một khoản phí. Ưu tiên thử lại bản gốc.
# Địa chỉ, bạn không thể gửi lại tác vụ tạo phải trả phí chỉ vì hiện tượng giật hình khi tải xuống.
WAVESPEED_MAX_DOWNLOAD_RETRIES = 2


class WaveSpeedUnconfirmedTaskError(RuntimeError):
    """
    Nhiệm vụ xây dựng phải trả phí đã được gửi nhưng trạng thái cuối cùng không thể được xác nhận cục bộ.

    Loại ngoại lệ này hoàn toàn không tương đương với "tác vụ thất bại và có thể được lặp lại": tác vụ từ xa có thể vẫn đang chạy hoặc có thể có
    Đã hoàn thành và thanh toán. Quá trình quan trọng phải dừng ở đây và sẽ không có nhiệm vụ trả phí mới nào được gửi cho các từ khóa tiếp theo.
    Và để lại id dự đoán đã gửi trong nhật ký để truy xuất thủ công.
    """

    def __init__(self, message: str, prediction_id: str = ""):
        super().__init__(message)
        self.prediction_id = prediction_id


def _wavespeed_status_code(response: Any) -> int:
    """Đọc mã trạng thái phản hồi; xử lý nó là 200 khi trường này bị thiếu trong đối tượng ngoại lệ hoặc đối tượng kiểm tra kép."""
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _is_wavespeed_retryable_error(error: Exception) -> bool:
    """
    Xác định xem ngoại lệ bỏ phiếu có đáng để thử lại hay không.

    Các bất thường của mạng như kết nối và hết thời gian chờ không có mã trạng thái và được xử lý như lỗi tạm thời; phản hồi với mã trạng thái chỉ
    429 và 5xx, nhất quán với bộ thử lại của SDK chính thức.
    """
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    response = getattr(error, "response", None)
    if response is not None:
        return _wavespeed_status_code(response) in WAVESPEED_RETRYABLE_STATUS_CODES
    return False


def _wavespeed_duration_bounds() -> tuple[int, int]:
    """
    Trả về khoảng thời gian tạo (giây) được mô hình hiện tại hỗ trợ.

    Khoảng thời gian mặc định tương ứng với mô hình Seedance mặc định; khi người dùng chuyển sang các mẫu video Wensheng khác, họ có thể
    Đồng bộ hóa khoảng thời gian điều chỉnh trong cấu hình. Mọi cấu hình bất thường sẽ quay về giá trị mặc định và đảm bảo min <= max,
    Tránh biến thông tin đầu vào của người dùng thành một yêu cầu từ xa không thành công.
    """

    def read_bound(key: str, fallback: int) -> int:
        try:
            value = int(config.app.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value >= 1 else fallback

    min_duration = read_bound("wavespeed_min_duration", WAVESPEED_MIN_DURATION_SECONDS)
    max_duration = read_bound("wavespeed_max_duration", WAVESPEED_MAX_DURATION_SECONDS)
    return min_duration, max(max_duration, min_duration)


def generate_videos_wavespeed(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Sử dụng mô hình video WaveSpeed ​​​​Vincent để tạo một đoạn phim cho từ khóa kịch bản.

    Duy trì cùng một quy ước về lỗi chữ ký và danh sách trống như search_videos_* của nguồn chứng khoán,
    Cho phép kết nối trực tiếp với quy trình tính toán thời lượng và tải xuống chung của ``download_videos``.
    ``thời lượng_tối thiểu`` trong ngữ cảnh tạo là thời lượng phân đoạn mục tiêu tính bằng giây.
    """
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("wavespeed_api_keys")
    model_id = (
        str(
            config.app.get("wavespeed_text_to_video_model", "")
            or WAVESPEED_DEFAULT_T2V_MODEL
        )
        .strip()
        .strip("/")
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    requested_duration = max(int(minimum_duration), 1)
    min_duration, max_duration = _wavespeed_duration_bounds()
    duration = min(max(requested_duration, min_duration), max_duration)
    if duration != requested_duration:
        # Việc tạo dài hơn yêu cầu sẽ không ảnh hưởng tới phim cuối cùng: quá trình biên tập vẫn được cắt bớt theo thời lượng của clip; tạo ra lâu hơn yêu cầu
        # Tình huống ngắn hơn chỉ xảy ra khi yêu cầu vượt quá giới hạn trên của mô hình và lúc này nó chỉ có thể hội tụ đến giới hạn trên.
        logger.info(
            f"wavespeed clip duration clamped to model-supported range: "
            f"requested={requested_duration}s, using={duration}s "
            f"(supported {min_duration}-{max_duration}s)"
        )
    payload = {
        "prompt": search_term,
        "aspect_ratio": aspect.value,
        "duration": duration,
    }
    logger.info(
        f"generating video on wavespeed: model={model_id}, "
        f"term={search_term!r}, duration={duration}s"
    )

    # Việc gửi POST sẽ không bao giờ tự động thử lại: yêu cầu có thể đã tạo một tác vụ phải trả phí ở đầu từ xa và việc gửi lại sẽ gây ra
    # Tạo lặp lại và khấu trừ lặp lại (phù hợp với chính sách gửi của SDK chính thức).
    try:
        submit_response = requests.post(
            f"{WAVESPEED_API_BASE_URL}/{model_id}",
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
    except Exception as e:
        # Không nhận được phản hồi không có nghĩa là tác vụ chưa được tạo. Trạng thái không xác định tại thời điểm này và toàn bộ thế hệ phải bị chấm dứt
        # xử lý thay vì tiếp tục gửi nhiệm vụ trả phí mới cho từ khóa tiếp theo.
        raise WaveSpeedUnconfirmedTaskError(
            "wavespeed submission did not return a response, the task may "
            "already exist remotely: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        ) from e

    submit_status = _wavespeed_status_code(submit_response)
    if submit_status >= 500:
        # 5xx có thể xảy ra sau khi tác vụ được tạo và không thể xác định liệu tác vụ đó đã được lập hóa đơn hay chưa.
        raise WaveSpeedUnconfirmedTaskError(
            f"wavespeed submission failed with HTTP {submit_status}, "
            "the task may already exist remotely"
        )
    try:
        submit_body = submit_response.json()
    except Exception as e:
        raise WaveSpeedUnconfirmedTaskError(
            "wavespeed submission returned an unreadable response, the task "
            f"may already exist remotely: error={type(e).__name__}"
        ) from e

    submit_data = submit_body.get("data") if isinstance(submit_body, dict) else None
    if not isinstance(submit_body, dict) or submit_body.get("code") != 200:
        # 4xx và mã lỗi kinh doanh là những sự từ chối rõ ràng. Không có tác vụ nào được tạo ở đầu xa nên không có sự trùng lặp.
        # Rủi ro thanh toán, trả lại kết quả trống và tiếp tục theo thỏa thuận nguồn nguyên liệu hiện có.
        logger.error(
            "wavespeed video generation request rejected: "
            f"http_status={submit_status}, "
            f"code={submit_body.get('code') if isinstance(submit_body, dict) else None}, "
            f"detail={_redact_secret(str((submit_body or {}).get('message') or ''), api_key)}"
        )
        return []
    prediction_id = (
        str(submit_data.get("id") or "") if isinstance(submit_data, dict) else ""
    )
    if not prediction_id:
        # Việc gửi đã được chấp nhận nhưng không nhận được ID: nhiệm vụ có thể đã tồn tại nhưng không thể theo dõi và không thể tiếp tục đơn hàng.
        raise WaveSpeedUnconfirmedTaskError(
            "wavespeed accepted the submission without returning a prediction id"
        )
    # Nếu nhiệm vụ tạo được gửi thành công, việc thanh toán từ xa sẽ có tác dụng phụ. ID nhiệm vụ của bản ghi nhật ký sẽ được nhập đầu tiên.
    # Ngay cả khi cuộc bỏ phiếu tiếp theo không thành công, người dùng vẫn có thể truy xuất sản phẩm trong bảng điều khiển WaveSpeed ​​bằng ID.
    logger.info(f"wavespeed prediction created: id={prediction_id}")

    result_data = _wait_for_wavespeed_prediction(
        prediction_id=prediction_id,
        headers=headers,
        api_key=api_key,
    )
    if result_data is None:
        return []

    try:
        video_items = []
        outputs = result_data.get("outputs")
        for output in outputs if isinstance(outputs, list) else []:
            # URL sản phẩm là địa chỉ tải xuống tạm thời đã ký và phải được giữ lại toàn bộ (không thể loại bỏ các tham số truy vấn).
            # số), do đó source_info không được ghi và chỉ được sử dụng cho các lần tải xuống ngay sau đó.
            if not isinstance(output, str) or not output.startswith(
                ("http://", "https://")
            ):
                continue
            item = MaterialInfo()
            item.provider = "wavespeed"
            item.url = output
            item.duration = duration
            item.source_info = {
                "provider": "wavespeed",
                "search_term": search_term,
                "asset_id": prediction_id,
                "rendition": {
                    "id": None,
                    "width": video_width,
                    "height": video_height,
                },
            }
            video_items.append(item)
        if not video_items:
            logger.error(
                "wavespeed prediction completed without downloadable outputs: "
                f"id={prediction_id}"
            )
        return video_items
    except Exception as e:
        # Sản phẩm đã được tạo và lập hóa đơn và ngoại lệ ở đây chỉ có thể đến từ phân tích cú pháp cục bộ. Ghi xong nhấn vào kết quả trống
        # Quay trở lại, cho phép lớp trên bỏ qua phân đoạn, nhưng bản thân trạng thái nhiệm vụ được xác định và các phân đoạn tiếp theo có thể được tiếp tục.
        logger.error(
            "wavespeed output parsing failed: "
            f"id={prediction_id}, error={type(e).__name__}, "
            f"detail={_redact_request_error(e, api_key)}"
        )

    return []


def _wait_for_wavespeed_prediction(
    *,
    prediction_id: str,
    headers: dict,
    api_key: str,
) -> dict | None:
    """
    Thăm dò cùng một id dự đoán cho đến khi xuất hiện kết quả được xác nhận.

    Trả về dữ liệu `` đã hoàn thành``; đầu từ xa rõ ràng không thành công (không thành công/hủy/hết thời gian chờ)
    Trả về Không có nghĩa là tác vụ đã kết thúc và có thể tiếp tục với các đoạn tiếp theo một cách an toàn. Nút lỗi tạm thời
    Độ trễ tuyến tính thử lại cùng một ID và không bao giờ gửi lại nhiệm vụ; bị ném khi trạng thái không thể được xác nhận
    :class:`WaveSpeedUnconfirmedTaskError`, người gọi sẽ chấm dứt toàn bộ quá trình tạo.
    """
    deadline = time.monotonic() + WAVESPEED_RUN_TIMEOUT_SECONDS
    consecutive_failures = 0
    while True:
        try:
            response = requests.get(
                f"{WAVESPEED_API_BASE_URL}/predictions/{prediction_id}/result",
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(30, 60),
            )
            status_code = _wavespeed_status_code(response)
            if status_code in WAVESPEED_RETRYABLE_STATUS_CODES:
                raise requests.exceptions.HTTPError(
                    f"HTTP {status_code}", response=response
                )
            result_body = response.json()
            result_data = (
                result_body.get("data") if isinstance(result_body, dict) else None
            )
            if not isinstance(result_body, dict) or result_body.get("code") != 200:
                # Khi bỏ phiếu bị từ chối rõ ràng (ví dụ: 4xx), trạng thái nhiệm vụ vẫn không xác định: nhiệm vụ đã được gửi,
                # Chỉ là không thể tìm thấy kết quả cục bộ và bạn không thể tiếp tục gửi các nhiệm vụ trả phí mới.
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction status is unknown: "
                    f"http_status={status_code}, "
                    f"code={result_body.get('code') if isinstance(result_body, dict) else None}, "
                    f"detail={_redact_secret(str((result_body or {}).get('message') or ''), api_key)}",
                    prediction_id=prediction_id,
                )
            if not isinstance(result_data, dict):
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction result payload is malformed",
                    prediction_id=prediction_id,
                )
        except WaveSpeedUnconfirmedTaskError:
            raise
        except Exception as e:
            if not _is_wavespeed_retryable_error(e):
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction polling failed and the task state is "
                    f"unknown: error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, api_key)}",
                    prediction_id=prediction_id,
                ) from e
            consecutive_failures += 1
            if consecutive_failures > WAVESPEED_MAX_POLL_RETRIES:
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction polling failed after "
                    f"{WAVESPEED_MAX_POLL_RETRIES + 1} attempts, the task may "
                    "still be running remotely: "
                    f"error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, api_key)}",
                    prediction_id=prediction_id,
                ) from e
            delay = WAVESPEED_RETRY_BASE_SECONDS * consecutive_failures
            logger.warning(
                "wavespeed prediction polling hit a transient error, retry the "
                f"same task: id={prediction_id}, "
                f"attempt={consecutive_failures}/{WAVESPEED_MAX_POLL_RETRIES}, "
                f"error={type(e).__name__}, retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        # Số lượng được đặt lại khi nhận được phản hồi hợp lệ và hạn ngạch thử lại chỉ được sử dụng nếu có lỗi liên tiếp.
        consecutive_failures = 0
        status = str(result_data.get("status") or "")
        if status == "completed":
            return result_data
        if status in WAVESPEED_FAILURE_STATUSES:
            logger.error(
                "wavespeed prediction did not produce a video: "
                f"id={prediction_id}, status={status}, "
                f"detail={_redact_secret(str(result_data.get('error') or ''), api_key)}"
            )
            return None
        if time.monotonic() > deadline:
            # Tác vụ từ xa vẫn đang được thực thi và trạng thái cuối cùng không thể được xác nhận cục bộ nên các đơn hàng phải dừng lại.
            raise WaveSpeedUnconfirmedTaskError(
                f"wavespeed prediction is still {status or 'pending'} after "
                f"{WAVESPEED_RUN_TIMEOUT_SECONDS:.0f}s of local waiting",
                prediction_id=prediction_id,
            )
        time.sleep(WAVESPEED_POLL_INTERVAL_SECONDS)


def _save_generated_video_with_retry(
    video_url: str, save_dir: str, provider: str
) -> str:
    """
    Tải xuống sản phẩm đã được thanh toán. Nếu không thành công, trước tiên hãy thử lại với cùng một địa chỉ.

    Chi phí để tạo lại tác vụ từ xa là phải trả lại, vì vậy việc tải jitter trước tiên phải được thực hiện tại địa chỉ ban đầu.
    Thực hiện một số lần thử lại có giới hạn và chỉ từ bỏ phân đoạn khi số lần thử lại đã hết.
    """
    for attempt in range(WAVESPEED_MAX_DOWNLOAD_RETRIES + 1):
        try:
            saved_video_path = save_video(video_url=video_url, save_dir=save_dir)
            if saved_video_path:
                return saved_video_path
            failure_detail = "empty result"
        except Exception as e:
            failure_detail = (
                f"error={type(e).__name__}, "
                f"detail={_redact_request_error(e, video_url)}"
            )
        if attempt >= WAVESPEED_MAX_DOWNLOAD_RETRIES:
            break
        delay = WAVESPEED_RETRY_BASE_SECONDS * (attempt + 1)
        logger.warning(
            "failed to download generated video, retry the same url: "
            f"provider={provider}, "
            f"attempt={attempt + 1}/{WAVESPEED_MAX_DOWNLOAD_RETRIES}, "
            f"{failure_detail}, retry_in={delay:.1f}s"
        )
        time.sleep(delay)
    logger.error(
        "failed to download generated video after "
        f"{WAVESPEED_MAX_DOWNLOAD_RETRIES + 1} attempts: "
        f"provider={provider}, {failure_detail}"
    )
    return ""


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


# OpenAI tương thích với biểu đồ thế hệ (Số phát hành #1274) thông qua giao thức /images/thế hệ làm từ khóa tập lệnh
# Tạo tài liệu hình ảnh, có thể được trỏ tới cổng ComfyUI/SD cục bộ hoặc được sử dụng để chuyển giao thức OpenAI khác nhau
# Phục vụ. Hình ảnh được tạo ngay lập tức được hiển thị thành một đoạn mp4 "được phóng to từ từ" có cùng kiểu với tài liệu cục bộ, rất hữu ích cho việc tải xuống
# Quá trình chỉnh sửa hoàn toàn minh bạch.
OPENAI_IMAGE_ENDPOINT_PATH = "images/generations"
# Giao diện hình ảnh chính thức của OpenAI chỉ chấp nhận kích thước do model chỉ định và không thể truyền trực tiếp độ phân giải video (chẳng hạn như 1080x1920).
# Khi openai_image_size được để trống, các giá trị mặc định tương thích sau đây sẽ được sử dụng theo khung; cổng cục bộ có thể được cấu hình rõ ràng để ghi đè.
OPENAI_IMAGE_DEFAULT_SIZES = {
    VideoAspect.portrait: "1024x1536",
    VideoAspect.landscape: "1536x1024",
    VideoAspect.square: "1024x1024",
}
# Giữ nguyên cỡ thử lại như WaveSpeed: 429 và 5xx là những lỗi tạm thời và yêu cầu một số lần thử lại có giới hạn.
OPENAI_IMAGE_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# 401/403 có nghĩa là khóa hiện tại bị từ chối rõ ràng. get_api_key xoay khóa mỗi lần nó được gọi, nhiều cấu hình được định cấu hình
# Chìa khóa sẽ được tự động thay đổi khi thử lại; khi chỉ có một chìa khóa, nó sẽ thất bại nhanh chóng và sẽ không có những lần thử lại vô nghĩa.
OPENAI_IMAGE_KEY_ERROR_STATUS_CODES = frozenset({401, 403})
OPENAI_IMAGE_MAX_ATTEMPTS = 3
# Đầu ra nối tiếp + độ trễ tuyến tính, tương thích với cửa sổ khôi phục giới hạn hiện tại chung của các dịch vụ chuyển tuyến.
OPENAI_IMAGE_RETRY_BACKOFF_SECONDS = (5, 15, 30)
# Giao diện tạo đồng bộ có thể mất hàng chục giây để trả về hình ảnh và thời gian chờ đọc sẽ có đủ biên độ.
OPENAI_IMAGE_REQUEST_TIMEOUT = (30, 300)
# Thử tải xuống lại sau khi ảnh đã được tính phí trên cơ sở từng ảnh: Ưu tiên thử lại địa chỉ ban đầu thay vì tạo lại cùng một ảnh.
OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS = 3
OPENAI_IMAGE_DOWNLOAD_BACKOFF_SECONDS = 2


def is_openai_image_enabled(app_config: dict | None = None) -> bool:
    """
    Xác định xem nguồn tài liệu hình ảnh Vincent tương thích với OpenAI đã hoàn thành cấu hình tối thiểu hay chưa.

    Khóa API được phép để trống: Cổng ComfyUI/SD hoàn toàn cục bộ thường không yêu cầu xác thực khi nó trống
    Yêu cầu không có tiêu đề ủy quyền. Để kiểm tra trước nhiệm vụ và tín dụng WebUI, LLM và TTS được sử dụng
    Chặn trước các tác vụ có cấu hình bị thiếu.
    """
    app_config = config.app if app_config is None else app_config
    return bool(
        str(app_config.get("openai_image_base_url", "") or "").strip()
        and str(app_config.get("openai_image_model", "") or "").strip()
    )


def _openai_image_endpoint() -> tuple[str, str]:
    """
    Đọc điểm cuối và tên mô hình của sơ đồ Vincent, đồng thời đưa ra lỗi kèm theo hướng dẫn cấu hình khi bị thiếu.
    """
    base_url = (
        str(config.app.get("openai_image_base_url", "") or "").strip().rstrip("/")
    )
    model = str(config.app.get("openai_image_model", "") or "").strip()
    if not base_url:
        raise ValueError(
            "\n\n##### openai_image_base_url is not set #####\n\n"
            f"Please set it in the config.toml file: {config.config_file}\n"
        )
    if not model:
        raise ValueError(
            "\n\n##### openai_image_model is not set #####\n\n"
            f"Please set it in the config.toml file: {config.config_file}\n"
        )
    return f"{base_url}/{OPENAI_IMAGE_ENDPOINT_PATH}", model


def _openai_image_size(video_aspect: VideoAspect) -> str:
    """
    Phân tích kích thước hình ảnh được yêu cầu.

    Giao diện OpenAI chính thức chỉ chấp nhận kích thước do kiểu máy chỉ định (chẳng hạn như 1024x1536) và truyền trực tiếp độ phân giải video.
    (chẳng hạn như 1080x1920) sẽ trả về 400. Theo mặc định, kích thước tương thích được lấy theo khung; ``openai_image_size``
    Phần ghi đè có thể được định cấu hình rõ ràng để sử dụng bởi các cổng gốc (chẳng hạn như SD WebUI) hỗ trợ mọi độ phân giải.
    """
    configured = str(config.app.get("openai_image_size", "") or "").strip()
    if configured:
        return configured
    return OPENAI_IMAGE_DEFAULT_SIZES.get(VideoAspect(video_aspect), "1024x1024")


def _openai_image_prompt(search_term: str, character_prompt: str = "") -> str:
    """
    Gói các từ khóa của tập lệnh vào các từ gợi ý cuối cùng. Hỗ trợ character_prompt tùy chọn để đảm bảo tính nhất quán của ký tự.
    """
    term = f"{character_prompt}, {search_term}" if character_prompt else search_term
    template = str(config.app.get("openai_image_prompt_template", "") or "").strip()
    if not template or "{term}" not in template:
        return term
    try:
        return template.replace("{term}", term)
    except Exception:
        return term


def _response_json_safely(response: Any) -> Any:
    """Đọc JSON phản hồi; trả về Không có nếu phản hồi kiểm tra kép hoặc ngoại lệ không phân tích được."""
    try:
        return response.json()
    except Exception:
        return None


def _openai_image_response_message(body: Any) -> str:
    """
    Trích xuất các mô tả lỗi mà con người có thể đọc được từ các phản hồi tương thích với OpenAI.

    Định dạng chuẩn là ``{"error": {"message": ...}}`` và các dịch vụ chuyển tuyến thường thoái hóa thành
    ``{"message": ...}`` hoặc đưa ra một chuỗi trực tiếp. Nếu không thể lấy được, một chuỗi trống sẽ được trả về, được xác định bởi người gọi
    Xác định xem có quay lại phần thân phản hồi hay không.
    """
    if not isinstance(body, dict):
        return str(body or "")[:300]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")[:300]
    if error is not None:
        return str(error)[:300]
    return str(body.get("message") or "")[:300]


def _openai_image_http_failure(response: Any, status: int, api_key: str) -> str:
    """Sắp xếp các phản hồi lỗi HTTP thành mô tả có thể đọc được trong nhật ký đã được giải mẫn cảm."""
    message = _openai_image_response_message(_response_json_safely(response))
    if not message:
        message = str(getattr(response, "text", "") or "")[:300]
    return f"HTTP {status}: {_redact_secret(message, api_key)}"


def _openai_image_download_bytes(
    image_url: str,
    api_key: str,
) -> tuple[bytes | None, str]:
    """
    Tải xuống URL tạm thời của hình ảnh được tạo.

    Hình ảnh được tính phí trên cơ sở mỗi hình ảnh. Khi tải xuống không thành công, ưu tiên thử lại địa chỉ ban đầu thay vì quay lại tạo lại.
    Tránh trả tiền hai lần cho cùng một bức tranh.
    """
    failure_detail = "no download attempt was made"
    for attempt in range(1, OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            response = requests.get(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/115.0.0.0 Safari/537.36"
                },
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(30, 120),
            )
            if response.status_code == 200 and response.content:
                return response.content, ""
            failure_detail = f"HTTP {response.status_code} while downloading image"
        except Exception as e:
            failure_detail = (
                f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
            )
        if attempt < OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS:
            logger.warning(
                "generated image download failed, retrying the same url: "
                f"attempt={attempt}/{OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS}, "
                f"{failure_detail}"
            )
            time.sleep(OPENAI_IMAGE_DOWNLOAD_BACKOFF_SECONDS)
    return None, failure_detail


def _parse_openai_image_response(
    response: Any,
    api_key: str,
) -> tuple[bytes | None, str]:
    """
    Phân tích phản hồi /images/thế hệ và truy xuất dữ liệu hình ảnh url hoặc b64_json.

    Nếu lỗi phân tích cú pháp thuộc về sự từ chối kinh doanh rõ ràng (chẳng hạn như chính sách nội dung) hoặc định dạng phản hồi bất thường, nó sẽ được trả về trực tiếp.
    Mô tả lỗi, thử lại mà không chờ đợi - gửi lại cùng một yêu cầu sẽ chỉ nhận được kết quả tương tự.
    """
    body = _response_json_safely(response)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        return None, _redact_secret(_openai_image_response_message(body), api_key)

    entry = data[0]
    if not isinstance(entry, dict):
        return None, "invalid image data entry"

    b64_payload = entry.get("b64_json")
    if b64_payload:
        try:
            return base64.b64decode(b64_payload), ""
        except Exception as e:
            return None, f"invalid b64_json payload: {type(e).__name__}"

    image_url = entry.get("url")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        return _openai_image_download_bytes(image_url, api_key)

    return None, "image response has neither url nor b64_json"


def _request_openai_image(endpoint: str, payload: dict) -> tuple[bytes | None, str]:
    """
    Gọi giao diện /hình ảnh/thế hệ tương thích với OpenAI với các lần thử lại và xoay phím.

    429/5xx Thử lại theo thời gian lùi lỗi tạm thời; 401/403 Chỉ thử lại khi có nhiều phím được định cấu hình
    (Sử dụng cơ chế xoay của get_api_key để thay đổi key); 4xx còn lại bị từ chối rõ ràng và nhanh chóng thất bại.
    Để cấp trên bỏ qua từ khóa này.

    Bảo mật thanh toán: Thời gian chờ đọc và gián đoạn kết nối của POST được coi là trạng thái "chưa được xác nhận" - máy chủ có thể có
    Tạo và tính phí, nhưng phản hồi không được trả lại. Việc tự động gửi lại có thể gây ra sự lặp lại và sao chép.
    Đang thanh toán nên không có lần thử lại nào được thực hiện. Chỉ hết thời gian chờ của giai đoạn kết nối (ConnectTimeout, yêu cầu OK không
    Đã gửi đến máy chủ) để xác nhận rằng không có tác vụ tạo nào được tạo và bạn có thể thử lại một cách an toàn.

    Khóa API được phép để trống: Cổng ComfyUI/SD hoàn toàn cục bộ thường không yêu cầu xác thực khi nó trống
    Không có tiêu đề ủy quyền nào được gửi.
    """
    api_keys = config.app.get("openai_image_api_keys")
    if isinstance(api_keys, (list, tuple)):
        configured_keys = [k for k in api_keys if str(k or "").strip()]
    elif str(api_keys or "").strip():
        configured_keys = [api_keys]
    else:
        configured_keys = []

    failure_detail = "no request attempt was made"
    for attempt in range(1, OPENAI_IMAGE_MAX_ATTEMPTS + 1):
        api_key = get_api_key("openai_image_api_keys") if configured_keys else ""
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        retryable = False
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=OPENAI_IMAGE_REQUEST_TIMEOUT,
            )
        except requests.exceptions.ConnectTimeout as e:
            # Hết thời gian chờ của giai đoạn kết nối: Yêu cầu được xác định là không được gửi đến máy chủ và tác vụ tạo không được tạo.
            # Sẽ an toàn để thử lại.
            failure_detail = (
                f"connect timeout: detail={_redact_request_error(e, api_key)}"
            )
            retryable = True
        except Exception as e:
            # Hết thời gian chờ đọc/gián đoạn kết nối, v.v. ở trạng thái "chưa được xác nhận": máy chủ có thể đã chấp nhận và trừ phí.
            # Việc gửi lại tự động có thể dẫn đến việc tạo lặp lại và thanh toán lặp lại và từ khóa sẽ bị lớp trên bỏ qua.
            failure_detail = (
                f"unconfirmed request error (no retry to avoid double billing): "
                f"{type(e).__name__}, detail={_redact_request_error(e, api_key)}"
            )
        else:
            status = int(getattr(response, "status_code", 200) or 200)
            if status in OPENAI_IMAGE_KEY_ERROR_STATUS_CODES:
                failure_detail = _openai_image_http_failure(response, status, api_key)
                # Chỉ trong cấu hình nhiều phím, việc thử lại mới có thể xoay sang các phím có sẵn.
                retryable = len(configured_keys) > 1
            elif status in OPENAI_IMAGE_RETRYABLE_STATUS_CODES:
                failure_detail = _openai_image_http_failure(response, status, api_key)
                retryable = True
            elif status >= 400:
                return None, _openai_image_http_failure(response, status, api_key)
            else:
                image_bytes, parse_error = _parse_openai_image_response(
                    response, api_key
                )
                if image_bytes is not None:
                    return image_bytes, ""
                failure_detail = parse_error

        if retryable and attempt < OPENAI_IMAGE_MAX_ATTEMPTS:
            backoff_seconds = OPENAI_IMAGE_RETRY_BACKOFF_SECONDS[
                min(attempt - 1, len(OPENAI_IMAGE_RETRY_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "openai image request failed, retrying: "
                f"attempt={attempt}/{OPENAI_IMAGE_MAX_ATTEMPTS}, "
                f"next_retry_in={backoff_seconds}s, detail={failure_detail}"
            )
            time.sleep(backoff_seconds)
            continue
        return None, failure_detail

    return None, failure_detail


def _save_openai_image_file(
    image_bytes: bytes,
    save_dir: str,
) -> tuple[str, int, int]:
    """
    Chuẩn hóa kết quả được tạo thành PNG và trả về (đường dẫn, chiều rộng, chiều cao).

    Chuyển đổi hợp nhất sang PNG có thể tránh được hai loại vấn đề: dịch vụ chuyển trả về WebP/JPEG nhưng không đáng tin cậy
    tiện ích mở rộng và hình ảnh mang siêu dữ liệu bất thường khiến MoviePy không thể phân tích cú pháp (với các tài liệu cục bộ
    Để đáp ứng logic thanh lọc, việc tiêu chuẩn hóa được hoàn thành trong giai đoạn sắp xếp).
    """
    if not save_dir:
        save_dir = utils.storage_dir("cache_images", create=True)
    elif not os.path.isdir(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    image_path = os.path.join(save_dir, f"openai-image-{uuid.uuid4().hex[:12]}.png")

    # Lỗi giải mã hình ảnh có thể bị hạ cấp thành "bỏ qua từ khóa hiện tại", nhưng quyền thư mục, dung lượng ổ đĩa và tệp
    # Lỗi ghi phải tiếp tục được đưa ra, nếu không, vòng lặp tạo theo yêu cầu sẽ tiếp tục tạo tệp khi không thể lưu cục bộ.
    # Nhiệm vụ được trả tiền tiếp theo. Image.open chỉ đọc byte bộ nhớ nên OSError ở đây thuộc định dạng
    # Công nhận không thành công; OSError của image.load tương ứng với dữ liệu hình ảnh bị cắt bớt hoặc bị hỏng.
    try:
        image = Image.open(io.BytesIO(image_bytes))
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise _OpenAIImageDecodeError(f"{type(exc).__name__}: {exc}") from exc

    with image:
        try:
            image.load()
        except (OSError, SyntaxError, ValueError) as exc:
            raise _OpenAIImageDecodeError(f"{type(exc).__name__}: {exc}") from exc
        if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            image = image.convert("RGB")
        # lưu không được đặt trong vùng bảo vệ ngoại lệ giải mã: lỗi ghi cho biết môi trường vận hành tiếp tục không khả dụng và sẽ
        # Chấm dứt toàn bộ nhiệm vụ để ngăn các từ khóa tiếp theo tiếp tục tạo ra những hình ảnh trả phí không thể đưa ra thị trường.
        image.save(image_path, format="PNG")
        width, height = image.size
    return image_path, width, height


def _request_pollinations_image(prompt: str, video_aspect: VideoAspect) -> bytes | None:
    """Tự động sinh ảnh AI chất lượng cao miễn phí qua Pollinations (Flux) khi endpoint OpenAI/Google bị lỗi hoặc không có key."""
    try:
        import urllib.parse
        encoded_prompt = urllib.parse.quote(prompt[:400])
        aspect_val = VideoAspect(video_aspect)
        width, height = (768, 1344) if aspect_val == VideoAspect.portrait else (1344, 768)
        seed = random.randint(1, 9999999)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=true&seed={seed}"
        logger.info(f"generating image via Pollinations AI (Flux): prompt={prompt[:70]!r}...")
        resp = requests.get(url, timeout=45, proxies=config.proxy, verify=_get_tls_verify())
        if resp.status_code == 200 and resp.content and len(resp.content) > 1000:
            return resp.content
        logger.warning(f"pollinations returned non-image response: status={resp.status_code}")
    except Exception as e:
        logger.warning(f"pollinations image request failed: {e}")
    return None


def generate_images_openai(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    save_dir: str = "",
    character_prompt: str = "",
) -> List[MaterialInfo]:
    """
    Sử dụng giao diện hình ảnh Vincent tương thích OpenAI để tạo hình ảnh cho từ khóa tập lệnh và lưu cục bộ.
    """
    aspect = VideoAspect(video_aspect)
    clip_duration = max(int(minimum_duration), 1)
    endpoint, model = _openai_image_endpoint()
    image_size = _openai_image_size(aspect)
    image_prompt = _openai_image_prompt(search_term, character_prompt=character_prompt)
    payload = {
        "model": model,
        "prompt": image_prompt,
        "n": 1,
        "size": image_size,
    }
    logger.info(
        f"generating image via openai-compatible endpoint: model={model}, "
        f"term={search_term!r}, size={image_size}"
    )
    image_bytes, failure_detail = _request_openai_image(endpoint, payload)
    if image_bytes is None:
        logger.warning(
            f"primary image provider failed ({failure_detail}), attempting fallback via Pollinations AI: term={search_term!r}"
        )
        image_bytes = _request_pollinations_image(image_prompt, aspect)

    if image_bytes is None:
        logger.error(
            f"all image generation providers failed: term={search_term!r}, "
            f"detail={failure_detail}"
        )
        return []

    try:
        image_path, width, height = _save_openai_image_file(image_bytes, save_dir)
    except _OpenAIImageDecodeError as e:
        # Lớp tương thích có thể trả về 200 nhưng nội dung không phải là hình ảnh (chẳng hạn như trang lỗi HTML được ngụy trang dưới dạng JSON,
        # Trang nhắc hạ cấp cổng). Những hình ảnh không thể giải mã được thuộc về “Thế hệ đã thất bại”, theo nguồn tư liệu
        # Nó được đồng ý trả về một danh sách trống để cho phép lớp trên bỏ qua từ khóa và tiếp tục, thay vì để ngoại lệ làm gián đoạn toàn bộ nhiệm vụ.
        logger.error(
            "openai image response is not a decodable image, skipping term: "
            f"term={search_term!r}, error={type(e).__name__}, detail={e}"
        )
        return []
    item = MaterialInfo()
    item.provider = "openai_image"
    item.url = image_path
    item.duration = clip_duration
    item.source_info = {
        "provider": "openai_image",
        "search_term": search_term,
        "rendition": {
            "id": None,
            "width": width,
            "height": height,
        },
    }
    return [item]


def _render_openai_image_video(image_path: str, clip_duration: int) -> str:
    """
    Kết xuất hình ảnh được tạo thành một đoạn mp4 và sử dụng lại quy trình "hình ảnh → đoạn động" của tài liệu cục bộ.

    Nếu kết xuất không thành công, một chuỗi trống sẽ được trả về theo quy ước nguồn vật liệu và người gọi sẽ bỏ qua hình ảnh và tiếp tục.
    """
    try:
        return video.render_image_zoom_video(image_path, clip_duration)
    except Exception as e:
        logger.error(
            "failed to render generated image as a video clip: "
            f"image={image_path}, error={type(e).__name__}, detail={e}"
        )
        return ""


def _download_videos_openai_image_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
    character_prompt: str = "",
) -> List[str]:
    """
    Tạo từng tài liệu hình ảnh Vincent tương thích với OpenAI theo trình tự các đoạn tập lệnh và dừng ngay lập tức khi đạt đến tổng thời gian yêu cầu.
    """
    if not material_directory:
        material_directory = utils.task_dir(task_id)

    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0

    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError):
        required_duration = 0.0
    if required_duration <= 0:
        logger.warning(
            "skip openai image generation because required audio duration is "
            f"not positive: duration={audio_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    for search_term in search_terms:
        items = generate_images_openai(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
            save_dir=material_directory,
            character_prompt=character_prompt,
        )
        for item in items:
            video_file = _render_openai_image_video(item.url, max_clip_duration)
            if not video_file:
                continue
            logger.info(f"image material rendered: {video_file}")
            video_paths.append(video_file)
            try:
                material_sources.append(_material_source_record(item, video_file))
            except Exception as source_error:
                # Nhất quán với nguồn khoảng không quảng cáo: không thể tạo và hiển thị ngoại lệ bản ghi nguồn có tính phí.
                # Tài liệu được coi là lỗi và không thể chặn quá trình tạo video.
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=openai_image, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(max_clip_duration, item.duration)
            # Tương tự như WaveSpeed, sử dụng >= để đánh giá: nếu bạn chỉ nhận đủ, bạn sẽ phải trả thêm một khoản phí nếu tạo một khoản phí khác.
            # Hai phán đoán bên trong và bên ngoài phải duy trì cùng một ngữ nghĩa.
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated image materials cover the required duration, stop "
                f"generating more images: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and rendered {len(video_paths)} image materials")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _search_videos_with_cache(
    provider: str,
    search_videos: Callable[..., List[MaterialInfo]],
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> List[MaterialInfo]:
    """
    Xử lý thống nhất bộ nhớ đệm tìm kiếm 24 giờ cho ba nguồn trực tuyến.

    Bộ đệm chỉ bao bọc API tìm kiếm và không thay đổi logic tải xuống và sao chép video tiếp theo. Không viết khi đầu xa trả về danh sách trống
    Bộ nhớ đệm, vì giao diện nhà cung cấp hiện tại sử dụng danh sách trống để thể hiện cả "không có kết quả" và "yêu cầu không thành công";
    Trước khi cả hai được chia thành các loại kết quả rõ ràng, tốt hơn là bạn nên thử lại lần sau thay vì lưu trữ các lỗi tạm thời trong một ngày.
    """
    cache_args = {
        "provider": provider,
        "search_term": search_term,
        "minimum_duration": minimum_duration,
        "video_aspect": video_aspect,
    }

    def load_cache_safely() -> List[MaterialInfo] | None:
        try:
            return material_cache.load_material_search_cache(**cache_args)
        except Exception as exc:
            # Bộ nhớ đệm là một tối ưu hóa tùy chọn. Mọi ngoại lệ triển khai bộ nhớ đệm đều phải được coi là thiếu sót và không thể bị chặn.
            # Tìm kiếm từ xa thông thường từ Pexels, Pixabay hoặc Coverr.
            logger.warning(
                "material search cache read failed, continue with remote search: "
                f"provider={provider}, error={type(exc).__name__}, detail={exc}"
            )
            return None

    def load_matching_cache() -> tuple[List[MaterialInfo] | None, int]:
        cached_items = load_cache_safely()
        if cached_items is None:
            return None, 0

        filtered_cached_items = _filter_materials_by_aspect(
            cached_items,
            video_aspect,
        )
        ignored_count = len(cached_items) - len(filtered_cached_items)
        if ignored_count:
            # Bộ đệm phiên bản cũ hơn có thể chứa tài liệu từ các hướng khác. Làm mới ngay cả khi vẫn còn một vài mục có sẵn
            # Bộ ứng cử viên hoàn chỉnh, nếu không, cùng một loạt video nhỏ sẽ được sử dụng lặp đi lặp lại trong thời gian hiệu lực của bộ nhớ đệm.
            return None, ignored_count
        return filtered_cached_items, 0

    cached_items, ignored_count = load_matching_cache()
    if cached_items is not None:
        return cached_items
    if ignored_count:
        logger.info(
            "material search cache contains mismatched orientations, "
            f"refresh from provider: provider={provider}, term={search_term!r}, "
            f"ignored={ignored_count}"
        )

    cache_lock = material_cache.get_material_search_cache_lock(**cache_args)
    with cache_lock:
        # Đợi các chuỗi có cùng điều kiện tìm kiếm hoàn tất trước khi đọc lại để tránh nhiều tác vụ API được lưu vào bộ nhớ đệm lần đầu tiên.
        # Khi xảy ra lỗi, đầu từ xa sẽ được yêu cầu cùng lúc, giảm khả năng kích hoạt giới hạn dòng điện giao diện của bên thứ ba và kích hoạt kiểm soát rủi ro.
        cached_items, _ = load_matching_cache()
        if cached_items is not None:
            return cached_items

        items = search_videos(
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )
        # Nhà cung cấp thường sẽ viết từ khóa hiện tại, nhưng việc kiểm tra sẽ nhân đôi, tiện ích mở rộng của bên thứ ba hoặc triển khai cũ có thể
        # Thiếu hoặc mang giá trị sai. Quá trình đọc được lưu vào bộ đệm sẽ khôi phục trường dựa trên khóa bộ đệm, do đó kết quả từ xa cũng là
        # Việc chỉnh sửa mục nhập tương tự đảm bảo rằng các bản ghi nguồn tác vụ truy cập bộ nhớ đệm và tìm kiếm đầu tiên là nhất quán.
        for item in items:
            if isinstance(item.source_info, dict):
                item.source_info = dict(item.source_info)
                item.source_info["search_term"] = search_term
        if items:
            try:
                material_cache.save_material_search_cache(
                    **cache_args,
                    items=items,
                )
            except Exception as exc:
                logger.warning(
                    "material search cache write failed, use remote results: "
                    f"provider={provider}, error={type(exc).__name__}, detail={exc}"
                )
        return items


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
    character_prompt: str = "",
) -> List[str]:
    provider = "pexels"
    remote_search_videos = search_videos_pexels
    if source == "pixabay":
        provider = "pixabay"
        remote_search_videos = search_videos_pixabay
    elif source == "coverr":
        provider = "coverr"
        remote_search_videos = search_videos_coverr

    def search_videos(
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect,
    ) -> List[MaterialInfo]:
        return _search_videos_with_cache(
            provider=provider,
            search_videos=remote_search_videos,
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if source == "wavespeed":
        return _download_videos_wavespeed_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "volcengine_seedance":
        return _download_videos_seedance_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "ofox":
        return _download_videos_ofox_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "metaso_minimax":
        return _download_videos_metaso_minimax_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "openai_image":
        return _download_videos_openai_image_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
            character_prompt=character_prompt,
        )

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )

    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []
    material_sources: list[dict[str, Any]] = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            source_info = item.source_info if isinstance(item.source_info, dict) else {}
            logger.info(
                f"downloading {item.provider} video: "
                f"asset_id={source_info.get('asset_id') or 'unknown'}"
            )
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                try:
                    material_sources.append(
                        _material_source_record(item, saved_video_path)
                    )
                except Exception as source_error:
                    # Nếu bản ghi nguồn không bình thường thì tài liệu được tải xuống thành công không thể được coi là lỗi tải xuống chứ chưa nói đến
                    # Chặn việc tạo video; giữ lại các nhà cung cấp và các loại bất thường cho việc định vị tiếp theo.
                    logger.warning(
                        "failed to prepare material source record: "
                        f"provider={item.provider}, "
                        f"error={type(source_error).__name__}, detail={source_error}"
                    )
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(
                "failed to download material video: "
                f"provider={item.provider}, error={type(e).__name__}, "
                f"detail={_redact_request_error(e, item.url)}"
            )
    logger.success(f"downloaded {len(video_paths)} videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_wavespeed_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    Tạo từng phần vật liệu WaveSpeed ​​​​theo thứ tự các đoạn tập lệnh và dừng ngay lập tức khi đạt đến tổng thời lượng yêu cầu.

    Mỗi từ khóa tương ứng một cách tự nhiên với một đoạn tập lệnh và bạn trả tiền khi tạo nó: Tạo nó đầy đủ trước rồi chọn nó.
    Trả tiền cho những cảnh quay chưa sử dụng. Mỗi khi một đoạn văn được tạo ở đây, nó sẽ được tải xuống ngay lập tức và thời hạn hiệu lực được tích lũy (với kho lưu trữ
    Quá trình này giống nhau, bị giới hạn bởi thời lượng clip) và thế hệ mới sẽ không còn được kích hoạt sau khi thời lượng lồng tiếng tích lũy vượt quá thời lượng yêu cầu.
    hỏi. Nếu một đoạn văn không đạt, hãy bỏ qua và tiếp tục sang đoạn tiếp theo theo quy ước về nguồn tài liệu hiện có.
    """
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = generate_videos_wavespeed(
                search_term=search_term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
            )
        except WaveSpeedUnconfirmedTaskError as e:
            # Trạng thái của các tác vụ phải trả phí đã gửi không xác định: đầu từ xa có thể vẫn đang chạy hoặc có thể đã được hoàn thành và lập hóa đơn.
            # Việc tiếp tục đặt hàng cho các từ khóa tiếp theo sẽ gây ra việc tạo lặp lại và bị khấu trừ nhiều lần, vì vậy hãy dừng việc này ngay tại chỗ.
            # Và để lại id dự đoán trong nhật ký để truy xuất sản phẩm theo cách thủ công trên bảng điều khiển.
            logger.error(
                "stop submitting new wavespeed tasks, the last submitted task "
                f"is unconfirmed: prediction_id={e.prediction_id or 'unknown'}, "
                f"detail={e}"
            )
            break
        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "wavespeed"
            )
            if not saved_video_path:
                continue
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                # Phù hợp với nguồn hàng tồn kho: bản ghi nguồn không bình thường và không thể tạo và tải xuống thành công với một khoản phí.
                # Tài liệu được coi là lỗi và không thể chặn quá trình tạo video.
                logger.warning(
                    "failed to prepare material source record: "
                    f"provider={item.provider}, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(max_clip_duration, item.duration)
            # Sử dụng >= để phán đoán: khi thời gian tích lũy bằng đúng với thời gian yêu cầu là đủ và sẽ được tái tạo.
            # Trả thêm một khoản phí. Hai phán đoán bên trong và bên ngoài phải duy trì cùng một ngữ nghĩa.
            if total_duration >= audio_duration:
                break
        if total_duration >= audio_duration:
            logger.info(
                "generated materials cover the required duration, stop "
                f"generating more clips: generated={total_duration:.1f}s, "
                f"required={audio_duration:.1f}s"
            )
            break
    logger.success(f"generated and downloaded {len(video_paths)} videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_seedance_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """Tạo các tài liệu Ark Seedance một cách tuần tự và ngừng thanh toán cho các đơn đặt hàng ngay sau khi kết thúc thời gian lồng tiếng."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    # Các vòng tạo trả phí trước tiên phải xác minh hai khoảng thời gian kiểm soát số vòng lặp. NaN/Infinity sẽ tạo ra
    # ``total_duration >= audio_duration`` sẽ không bao giờ giữ nguyên và thời lượng clip không tích cực sẽ khiến
    # Giá trị tích lũy không thể tăng lên và cả hai đều có thể tạo ra các tác vụ phải trả phí vô ích cho tất cả từ khóa.
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance audio duration must be a finite number"
        ) from exc
    if not math.isfinite(required_duration):
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance audio duration must be a finite number"
        )
    if required_duration <= 0:
        logger.warning(
            "skip Seedance paid generation because required audio duration is "
            f"not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance clip duration must be a positive integer"
        ) from exc
    if clip_duration <= 0:
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance clip duration must be a positive integer"
        )

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = volcengine_seedance.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except volcengine_seedance.VolcEngineSeedanceUnconfirmedTaskError as exc:
            # Nhiệm vụ trả phí từ xa vẫn có thể thành công. Dừng ngay việc đặt hàng và giữ lại ID nhiệm vụ cho tiện
            # Sau đó người dùng xác nhận hoặc truy xuất kết quả trên bảng điều khiển Ark.
            logger.error(
                "stop submitting new Seedance tasks because the last paid task "
                f"is unconfirmed: task_id={exc.task_id or 'unknown'}, detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except volcengine_seedance.VolcEngineSeedanceError as exc:
            logger.error(f"Seedance generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "volcengine_seedance"
            )
            if not saved_video_path:
                # Nhiệm vụ từ xa đã được hoàn thành và phí đã được tạo. Khi tải xuống cục bộ không thành công, ID tác vụ từ xa phải được
                # Đưa lại trạng thái tác vụ để người dùng có thể vào bảng điều khiển Ark để lấy kết quả. Ném thẳng vào đây
                # Lỗi đặc biệt, đồng thời ngăn các từ khóa tiếp theo tiếp tục tạo các tác vụ phải trả phí mới.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise volcengine_seedance.VolcEngineSeedanceDownloadError(
                    "Seedance generated a paid video but the result could not be "
                    f"downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=volcengine_seedance, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(clip_duration, item.duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated Seedance materials cover the required duration; stop "
                f"submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(
        f"generated and downloaded {len(video_paths)} Volcano Engine Seedance videos"
    )
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_ofox_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """Tạo tài liệu OFox một cách tuần tự và ngừng thanh toán cho các đơn đặt hàng ngay sau khi kết thúc thời gian lồng tiếng."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    # Các vòng tạo trả phí trước tiên phải xác minh hai khoảng thời gian kiểm soát số vòng lặp. NaN/Infinity sẽ tạo ra
    # ``total_duration >= audio_duration`` sẽ không bao giờ giữ nguyên và thời lượng clip không tích cực sẽ khiến
    # Giá trị tích lũy không thể tăng lên và cả hai đều có thể tạo ra các tác vụ phải trả phí vô ích cho tất cả từ khóa.
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise ofox.OFoxError("OFox audio duration must be a finite number") from exc
    if not math.isfinite(required_duration):
        raise ofox.OFoxError("OFox audio duration must be a finite number")
    if required_duration <= 0:
        logger.warning(
            "skip OFox paid generation because required audio duration is "
            f"not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ofox.OFoxError("OFox clip duration must be a positive integer") from exc
    if clip_duration <= 0:
        raise ofox.OFoxError("OFox clip duration must be a positive integer")

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = ofox.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except ofox.OFoxUnconfirmedTaskError as exc:
            # Nhiệm vụ trả phí từ xa vẫn có thể thành công. Dừng ngay việc đặt hàng và giữ lại ID nhiệm vụ cho tiện
            # Sau đó, người dùng xác nhận hoặc truy xuất kết quả trong bảng điều khiển OOX.
            logger.error(
                "stop submitting new OFox tasks because the last paid task "
                f"is unconfirmed: task_id={exc.task_id or 'unknown'}, detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except ofox.OFoxError as exc:
            logger.error(f"OFox generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        # Khi một từ khóa không được đánh giá rõ ràng bởi đầu cuối từ xa (chẳng hạn như kích hoạt xem xét nội dung), một danh sách trống sẽ được trả về: nhiệm vụ đã được hoàn thành
        # Kết thúc, không cần chần chừ thanh toán, bỏ qua phân đoạn này và tiếp tục tạo các từ khóa tiếp theo.
        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "ofox"
            )
            if not saved_video_path:
                # Nhiệm vụ từ xa đã được hoàn thành và phí đã được tạo. Khi tải xuống cục bộ không thành công, ID tác vụ từ xa phải được
                # Đưa lại trạng thái tác vụ để người dùng có thể vào bảng điều khiển OOX lấy kết quả. Ném thẳng vào đây
                # Lỗi đặc biệt, đồng thời ngăn các từ khóa tiếp theo tiếp tục tạo các tác vụ phải trả phí mới.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise ofox.OFoxDownloadError(
                    "OFox generated a paid video but the result could not be "
                    f"downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=ofox, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(clip_duration, item.duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated OFox materials cover the required duration; stop "
                f"submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and downloaded {len(video_paths)} OFox videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_metaso_minimax_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """Tài liệu MiniMax của Secret Tower được tạo tuần tự và đơn hàng thanh toán sẽ dừng ngay sau khi hết thời gian lồng tiếng."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    # Thời gian tối thiểu do đầu từ xa tạo ra là 4 giây, nhưng phân đoạn cục bộ vẫn được cắt bớt và tích lũy theo thời lượng của phân đoạn người dùng. Trước vòng lặp xác nhận
    # Kiểm soát các tham số để tránh các số NaN, Infinity hoặc không dương sẽ không bao giờ thỏa mãn điều kiện dừng, do đó
    # Tất cả các từ khóa được gửi dưới dạng nhiệm vụ phải trả tiền.
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax audio duration must be a finite number"
        ) from exc
    if not math.isfinite(required_duration):
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax audio duration must be a finite number"
        )
    if required_duration <= 0:
        logger.warning(
            "skip Metaso MiniMax paid generation because required audio duration "
            f"is not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax clip duration must be a positive integer"
        ) from exc
    if clip_duration <= 0:
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax clip duration must be a positive integer"
        )

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = metaso_minimax.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except metaso_minimax.MetasoMiniMaxUnconfirmedTaskError as exc:
            # Khi không xác định được yêu cầu hoặc trạng thái bỏ phiếu, tác vụ từ xa vẫn có thể thành công và bị tính phí. Dừng toàn bộ
            # Tạo vòng lặp để ngăn không cho đặt thứ tự từ khóa tiếp theo và cung cấp ID tác vụ cho dịch vụ tác vụ để lưu trữ.
            logger.error(
                "stop submitting new Metaso MiniMax tasks because the last paid "
                f"task is unconfirmed: task_id={exc.task_id or 'unknown'}, "
                f"detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except metaso_minimax.MetasoMiniMaxError as exc:
            logger.error(f"Metaso MiniMax generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "metaso_minimax"
            )
            if not saved_video_path:
                # Nếu việc tạo thành công, phí sẽ phát sinh. Nếu tải xuống không thành công thì không thể tạo tác vụ mới để thay thế.
                # Đưa ra một lỗi chuyên dụng mang ID từ xa để sử dụng theo trạng thái tác vụ và khôi phục thủ công.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise metaso_minimax.MetasoMiniMaxDownloadError(
                    "Metaso MiniMax generated a paid video but the result could "
                    f"not be downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=metaso_minimax, error={type(source_error).__name__}, "
                    f"detail={source_error}"
                )

            # Việc tạo phim cục bộ chỉ sử dụng độ dài phân đoạn do người dùng chọn; ngay cả khi H3 được tạo ra do hạn chế về thời lượng tối thiểu
            # Để có được tài liệu dài hơn, phần chưa sử dụng không thể được tính vào thời gian hiển thị và các cảnh ít cần thiết hơn sẽ được tạo ra.
            total_duration += min(clip_duration, item.duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated Metaso MiniMax materials cover the required duration; "
                f"stop submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and downloaded {len(video_paths)} Metaso MiniMax videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    Tải tài liệu theo thứ tự copywriting script.

    Logic tải xuống mặc định sẽ hợp nhất tất cả các tài liệu đề xuất từ ​​khóa thành một danh sách lớn; nếu là người đầu tiên
    Từ khóa trả về nhiều kết quả và nội dung của từ khóa này có thể được sử dụng trong lần tải xuống cuối cùng.
    Chủ đề kịch bản không thể được lên lịch trên dòng thời gian. Ở đây chúng tôi nhóm theo từ khóa và sau đó thăm dò lượt tải xuống:
    Vòng 1 lấy ứng viên thứ nhất cho mỗi từ khóa và vòng 2 lấy ứng cử viên thứ 2 cho mỗi từ khóa.
    Bằng cách này, không cần viết lại công cụ tổng hợp video, hãy cố gắng đảm bảo rằng thứ tự của các tài liệu càng gần với thứ tự của bản sao chép càng tốt.
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0
    candidate_index = 0
    while candidate_groups and total_duration <= audio_duration:
        has_candidate = False
        for search_term, term_items in candidate_groups:
            if candidate_index >= len(term_items):
                continue

            has_candidate = True
            item = term_items[candidate_index]
            try:
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                logger.info(
                    f"downloading ordered {item.provider} video for {search_term!r}: "
                    f"asset_id={source_info.get('asset_id') or 'unknown'}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    try:
                        material_sources.append(
                            _material_source_record(item, saved_video_path)
                        )
                    except Exception as source_error:
                        logger.warning(
                            "failed to prepare ordered material source record: "
                            f"provider={item.provider}, "
                            f"error={type(source_error).__name__}, "
                            f"detail={source_error}"
                        )
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    "failed to download ordered material video: "
                    f"provider={item.provider}, error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, item.url)}"
                )

        if not has_candidate:
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
