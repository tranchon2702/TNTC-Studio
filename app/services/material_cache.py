"""Bộ nhớ đệm đĩa của kết quả tìm kiếm tài liệu trực tuyến."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from app.models.schema import MaterialInfo, VideoAspect
from app.utils import utils


MATERIAL_SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_FORMAT_VERSION = 2
_CACHE_CLEANUP_INTERVAL_SECONDS = 60 * 60
_CACHE_FILE_PATTERN = re.compile(r"^[0-9a-f]{64}\.json$")

# API cho phép nhiều tác vụ video được thực thi đồng thời theo mặc định. Một số lượng phân đoạn khóa cố định có thể được chia sẻ theo cùng điều kiện tìm kiếm
# Khóa đồng thời tránh tăng trưởng bộ nhớ liên tục do lưu vĩnh viễn Khóa theo từ khóa. Nó chỉ chịu trách nhiệm hợp nhất hiện tại
# Các yêu cầu đồng thời trong một quy trình; ghi chéo tiến trình vẫn được bảo vệ bởi các tệp tạm thời và os.replace để đảm bảo tính toàn vẹn.
_CACHE_LOCKS = tuple(threading.Lock() for _ in range(256))
_cleanup_state_lock = threading.Lock()
_last_cleanup_monotonic: float | None = None


def _safe_public_url(value) -> str | None:
    """Xóa tham số truy vấn và thông tin xác thực của người dùng khỏi URL trang công khai để tránh bộ nhớ đệm vô tình lưu mã thông báo."""
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


def _cached_source_info(item: MaterialInfo) -> dict | None:
    """
    Các nguồn thông tin có thể đặt được xây dựng theo danh sách trắng.

    Từ khóa tìm kiếm đã được bao gồm trong khóa bộ đệm và nội dung bộ đệm không còn được ghi bằng văn bản thuần túy nữa; khi đọc, nó được xác định bởi các tham số gọi
    hồi phục. URL tải xuống được lưu riêng bởi ``MaterialInfo.url``. Chỉ có trang tài liệu được phép xuất bản ở đây.
    Trang công khai của tác giả và nhận dạng doanh nghiệp ổn định ngăn chặn mọi trường mở rộng xâm nhập vào bộ nhớ đệm trên đĩa.
    """
    source = item.source_info
    if not isinstance(source, dict) or not source:
        return None

    cached: dict = {
        "provider": str(source.get("provider") or item.provider),
    }
    asset_id = source.get("asset_id")
    source_page = _safe_public_url(source.get("source_page"))
    if asset_id not in (None, ""):
        cached["asset_id"] = str(asset_id)
    if source_page:
        cached["source_page"] = source_page

    raw_creator = source.get("creator")
    if isinstance(raw_creator, dict):
        creator = {}
        creator_id = raw_creator.get("id")
        creator_name = raw_creator.get("name")
        creator_page = _safe_public_url(raw_creator.get("profile_page"))
        if creator_id not in (None, ""):
            creator["id"] = str(creator_id)
        if creator_name not in (None, ""):
            creator["name"] = str(creator_name)
        if creator_page:
            creator["profile_page"] = creator_page
        if creator:
            cached["creator"] = creator

    raw_rendition = source.get("rendition")
    if isinstance(raw_rendition, dict):
        rendition = {}
        for field in ("id", "width", "height"):
            value = raw_rendition.get(field)
            if value not in (None, ""):
                rendition[field] = str(value) if field == "id" else value
        if rendition:
            cached["rendition"] = rendition
    return cached


def _cache_dir() -> Path:
    """
    Trả về thư mục bộ đệm tìm kiếm tài liệu được chia sẻ bởi tất cả các cổng đang chạy.

    Bộ đệm phải được đặt trong ``storage`` chứ không phải trong phiên WebUI hoặc bộ nhớ xử lý để bộ đệm này hoạt động
    WebUI, API, CLI và các tác vụ sau khi Docker khởi động lại sẽ sử dụng lại các kết quả tương tự.
    """
    return Path(utils.storage_dir("cache_material_search", create=True))


def _cache_key(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
) -> str:
    """
    Tạo tên tệp ổn định dựa trên các thông số nghiệp vụ ảnh hưởng đến kết quả tìm kiếm.

    API Key chỉ chịu trách nhiệm xác thực và không ảnh hưởng đến kết quả tìm kiếm công khai nên không thể ghi cache key hoặc nội dung cache.
    Việc sử dụng SHA-256 sẽ ngăn từ khóa xuất hiện trực tiếp trong tên tệp trong khi vẫn giữ cố định độ dài đường dẫn.
    """
    aspect_value = getattr(video_aspect, "value", video_aspect)
    cache_key = json.dumps(
        {
            "provider": str(provider).strip().lower(),
            "search_term": str(search_term).strip(),
            "minimum_duration": int(minimum_duration),
            "video_aspect": str(aspect_value),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()


def _cache_path(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
) -> Path:
    digest = _cache_key(
        provider=provider,
        search_term=search_term,
        minimum_duration=minimum_duration,
        video_aspect=video_aspect,
    )
    return _cache_dir() / f"{digest}.json"


def get_material_search_cache_lock(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
) -> threading.Lock:
    """Trả về phân đoạn khóa đang xử lý tương ứng với điều kiện tìm kiếm hiện tại."""
    digest = _cache_key(
        provider=provider,
        search_term=search_term,
        minimum_duration=minimum_duration,
        video_aspect=video_aspect,
    )
    return _CACHE_LOCKS[int(digest[:8], 16) % len(_CACHE_LOCKS)]


def _remove_invalid_cache(cache_path: Path) -> None:
    """Xóa các tệp bộ đệm riêng lẻ đã hết hạn hoặc không thể phân tích cú pháp. Thất bại sẽ không ảnh hưởng đến quá trình tìm kiếm tài liệu chính."""
    try:
        cache_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            f"failed to remove invalid material search cache: "
            f"file={cache_path.name}, error={exc}"
        )


def load_material_search_cache(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
    *,
    now: float | None = None,
) -> list[MaterialInfo] | None:
    """
    Đọc kết quả tìm kiếm tài liệu vẫn có giá trị trong 24 giờ.

    ``None`` biểu thị thiếu bộ nhớ đệm và yêu cầu yêu cầu API từ xa; danh sách trống không được trả về dưới dạng bộ đệm hợp lệ.
    Tránh các lỗi mạng hoặc ngoại lệ ngược dòng bị lưu nhầm vào bộ nhớ đệm và tiếp tục chặn các tác vụ tiếp theo.
    """
    if str(provider).strip().lower() == "coverr":
        # Địa chỉ tải xuống của Coverr chứa JWT đã ký được liên kết với Khóa API. Nó chỉ được sử dụng cho yêu cầu hiện tại,
        # Không thể vào bộ đệm đĩa; khi truy vấn các điều kiện tương tự, bộ đệm có thể còn sót lại của phiên bản cũ sẽ bị xóa.
        try:
            _remove_invalid_cache(
                _cache_path(
                    provider=provider,
                    search_term=search_term,
                    minimum_duration=minimum_duration,
                    video_aspect=video_aspect,
                )
            )
        except Exception as exc:
            logger.warning(
                "failed to remove disabled Coverr material search cache: "
                f"error={type(exc).__name__}, detail={exc}"
            )
        return None

    try:
        cache_path = _cache_path(
            provider=provider,
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )
    except Exception as exc:
        # Các ngoại lệ như tạo thư mục bộ đệm và phân giải đường dẫn không thể chặn tìm kiếm tài liệu từ xa. Giữ ngoại lệ hoàn chỉnh ở đây
        # Nhập và thông tin để tạo điều kiện thuận lợi cho việc định vị quyền hoặc các vấn đề gắn kết, trong khi tiếp tục quá trình chính do thiếu bộ nhớ đệm.
        logger.warning(
            "failed to prepare material search cache: "
            f"operation=read, error={type(exc).__name__}, detail={exc}"
        )
        return None
    try:
        stat_result = cache_path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            f"failed to inspect material search cache: "
            f"file={cache_path.name}, error={exc}"
        )
        return None

    current_time = time.time() if now is None else now
    cache_age = current_time - stat_result.st_mtime
    # mtime có thể rơi vào tương lai sau khi thời gian hệ thống được khôi phục hoặc tệp được sao chép từ máy khác. Vào lúc này, không thể
    # Bộ nhớ đệm xử lý dữ liệu như mới trong thời gian dài và sẽ đáng tin cậy hơn khi trực tiếp vô hiệu hóa và yêu cầu lại đầu cuối từ xa.
    if cache_age < 0 or cache_age >= MATERIAL_SEARCH_CACHE_TTL_SECONDS:
        _remove_invalid_cache(cache_path)
        return None

    try:
        with cache_path.open("r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)

        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CACHE_FORMAT_VERSION
            or not isinstance(payload.get("items"), list)
            or not payload["items"]
        ):
            raise ValueError("invalid cache payload")

        items = []
        for raw_item in payload["items"]:
            if not isinstance(raw_item, dict):
                raise ValueError("invalid material item")
            item_provider = raw_item.get("provider")
            item_url = raw_item.get("url")
            item_duration = raw_item.get("duration")
            source_info = raw_item.get("source_info")
            if (
                not isinstance(item_provider, str)
                or not item_provider
                or not isinstance(item_url, str)
                or not item_url
                or isinstance(item_duration, bool)
                or not isinstance(item_duration, (int, float))
                or item_duration <= 0
                or not isinstance(source_info, dict)
                or not source_info
            ):
                raise ValueError("invalid material fields")
            source_info = dict(source_info)
            source_info["search_term"] = search_term
            items.append(
                MaterialInfo(
                    provider=item_provider,
                    url=item_url,
                    duration=int(item_duration),
                    source_info=source_info,
                )
            )
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            f"failed to load material search cache: file={cache_path.name}, error={exc}"
        )
        _remove_invalid_cache(cache_path)
        return None

    logger.info(
        f"material search cache hit: provider={provider}, "
        f"term={search_term!r}, items={len(items)}"
    )
    return items


def save_material_search_cache(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
    items: Iterable[MaterialInfo],
) -> bool:
    """
    Atomic lưu kết quả tìm kiếm vật liệu không trống thành công.

    Nhiều tác vụ có thể tìm kiếm cùng một từ khóa cùng một lúc. Trước tiên hãy ghi tệp tạm thời duy nhất vào cùng thư mục, sau đó chuyển
    ``os.replace`` được phát hành để đảm bảo rằng quá trình đọc sẽ chỉ nhìn thấy tệp cũ hoàn chỉnh hoặc tệp mới hoàn chỉnh;
    Ngay cả khi hai quá trình ghi hoàn tất cùng lúc thì nội dung cuối cùng vẫn là kết quả hợp pháp tương ứng với cùng một cache key.
    """
    if str(provider).strip().lower() == "coverr":
        return False

    temp_path = None
    try:
        serialized_items = []
        for item in items:
            source_info = _cached_source_info(item)
            if not item.url or item.duration <= 0 or not source_info:
                continue
            serialized_items.append(
                {
                    "provider": item.provider,
                    "url": item.url,
                    "duration": int(item.duration),
                    "source_info": source_info,
                }
            )
        if not serialized_items:
            return False

        cache_path = _cache_path(
            provider=provider,
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )
        cleanup_expired_material_search_cache()
        payload = {
            "version": _CACHE_FORMAT_VERSION,
            "items": serialized_items,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=f".{cache_path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                payload,
                temp_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, cache_path)
        return True
    except Exception as exc:
        logger.warning(
            "failed to save material search cache: "
            f"error={type(exc).__name__}, detail={exc}"
        )
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def cleanup_expired_material_search_cache(
    *,
    now: float | None = None,
    force: bool = False,
) -> int:
    """
    Dọn dẹp tần suất thấp các bộ nhớ đệm tìm kiếm đã hết hạn mà chưa được truy vấn lại.

    Đường dẫn ghi thông thường sẽ quét thư mục nhiều nhất một lần mỗi giờ để tránh việc truyền tải thư mục tuyến tính cho mỗi lần tìm kiếm;
    ``force`` chỉ nên được gọi để kiểm tra hoặc bảo trì rõ ràng. Chỉ xóa các tệp JSON có tên SHA-256, không xóa
    Chạm vào các tập tin khác mà người dùng đã đặt trong thư mục.
    """
    global _last_cleanup_monotonic

    monotonic_now = time.monotonic()
    with _cleanup_state_lock:
        if (
            not force
            and _last_cleanup_monotonic is not None
            and monotonic_now - _last_cleanup_monotonic
            < _CACHE_CLEANUP_INTERVAL_SECONDS
        ):
            return 0
        _last_cleanup_monotonic = monotonic_now

    try:
        cache_dir = _cache_dir()
        entries = os.scandir(cache_dir)
    except Exception as exc:
        logger.warning(
            "failed to scan material search cache: "
            f"error={type(exc).__name__}, detail={exc}"
        )
        return 0

    current_time = time.time() if now is None else now
    deleted_count = 0
    failed_count = 0
    with entries:
        for entry in entries:
            if not _CACHE_FILE_PATTERN.fullmatch(entry.name):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                cache_age = current_time - entry.stat(follow_symlinks=False).st_mtime
                if 0 <= cache_age < MATERIAL_SEARCH_CACHE_TTL_SECONDS:
                    continue
                os.unlink(entry.path)
                deleted_count += 1
            except OSError as exc:
                failed_count += 1
                logger.warning(
                    "failed to delete material search cache file: "
                    f"file={entry.name}, error={exc}"
                )

    if deleted_count or failed_count:
        logger.info(
            "finished cleaning material search cache: "
            f"deleted={deleted_count}, failed={failed_count}"
        )
    return deleted_count
