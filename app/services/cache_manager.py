"""Dịch vụ thống kê, xem trước và dọn dẹp bộ nhớ đệm của tài liệu video."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterator

from loguru import logger

from app.utils import utils


# Tài liệu trực tuyến sử dụng MD5 của URL làm tên tệp ổn định. Quản lý bộ đệm chỉ chấp nhận định dạng đặt tên này để tránh
# Video, tệp mô tả hoặc các tệp doanh nghiệp khác mà người dùng đưa nhầm vào thư mục sẽ bị xóa dưới dạng bộ đệm.
_VIDEO_CACHE_FILE_PATTERN = re.compile(r"^vid-[0-9a-f]{32}\.mp4$")
_SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class VideoCacheStats:
    """Kết quả thống kê nhẹ cho các thư mục bộ đệm, chỉ chứa siêu dữ liệu hệ thống tệp."""

    file_count: int = 0
    total_size: int = 0
    oldest_mtime: float | None = None
    newest_mtime: float | None = None


@dataclass(frozen=True)
class VideoCacheCleanupResult:
    """Kết quả thực hiện việc dọn dẹp cho phép việc xóa một phần tệp không thành công."""

    deleted_count: int = 0
    deleted_size: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class _VideoCacheEntry:
    """Thông tin tệp nhỏ nhất được lưu trong giai đoạn quét để tránh mở hoặc phân tích video trong quá trình dọn dẹp."""

    path: str
    name: str
    size: int
    mtime: float


def video_cache_dir() -> str:
    """Trả về thư mục bộ đệm video mặc định để quản lý dự án."""

    return os.path.realpath(utils.storage_dir("cache_videos"))


def _iter_video_cache_entries() -> Iterator[_VideoCacheEntry]:
    """
    Quét tuần tự cấp độ đầu tiên của thư mục bộ đệm mặc định.

    Mục đích của việc sử dụng ``os.scandir`` là để sử dụng lại siêu dữ liệu được trả về khi truyền tải thư mục khi bộ đệm đạt tới hàng chục nghìn tệp.
    Tránh truy vấn lại các loại tệp sau ``Path.iterdir``. Không có đệ quy, không có video mở và không có cuộc gọi
    FFmpeg, do đó thời gian tiêu thụ chủ yếu liên quan tuyến tính với số lượng tệp chứ không liên quan đến tổng dung lượng video.
    """

    cache_dir = video_cache_dir()
    try:
        entries = os.scandir(cache_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            f"failed to scan video cache directory: path={cache_dir}, error={exc}"
        )
        return

    with entries:
        for entry in entries:
            if not _VIDEO_CACHE_FILE_PATTERN.fullmatch(entry.name):
                continue

            try:
                # Các liên kết tượng trưng không được tuân theo để đảm bảo logic dọn dẹp không vượt qua ranh giới thư mục bộ nhớ đệm mặc định.
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                logger.warning(
                    f"failed to inspect video cache file: file={entry.name}, error={exc}"
                )
                continue

            yield _VideoCacheEntry(
                path=entry.path,
                name=entry.name,
                size=stat_result.st_size,
                mtime=stat_result.st_mtime,
            )


def _is_cleanup_candidate(
    entry: _VideoCacheEntry,
    max_age_days: int | None,
    now: float,
) -> bool:
    if max_age_days is None:
        return True
    return entry.mtime < now - max_age_days * _SECONDS_PER_DAY


def _validate_max_age_days(max_age_days: int | None) -> None:
    """Các tham số dọn dẹp không hợp lệ sẽ bị từ chối một cách đáng tin cậy ngay cả khi thư mục bộ nhớ đệm trống."""
    if max_age_days is None:
        return
    if (
        isinstance(max_age_days, bool)
        or not isinstance(max_age_days, int)
        or max_age_days <= 0
    ):
        raise ValueError("max_age_days must be a positive integer or None")


def get_video_cache_stats(max_age_days: int | None = None) -> VideoCacheStats:
    """
    Đếm tất cả các bộ nhớ đệm hoặc xem trước các bộ nhớ đệm có thể xóa được có thời gian sửa đổi cũ hơn một số ngày được chỉ định.

    ``max_age_days=None`` có nghĩa là lưu tất cả vào bộ nhớ đệm. Quá trình thống kê chỉ đọc kích thước và thời gian sửa đổi của mục nhập thư mục.
    Nội dung video không được đọc nên ngay cả khi tổng dung lượng bộ nhớ đệm lớn thì nó cũng không phát sinh I/O tỷ lệ thuận với dung lượng.
    """

    _validate_max_age_days(max_age_days)
    now = time.time()
    file_count = 0
    total_size = 0
    oldest_mtime = None
    newest_mtime = None

    for entry in _iter_video_cache_entries():
        if not _is_cleanup_candidate(entry, max_age_days, now):
            continue
        file_count += 1
        total_size += entry.size
        oldest_mtime = (
            entry.mtime if oldest_mtime is None else min(oldest_mtime, entry.mtime)
        )
        newest_mtime = (
            entry.mtime if newest_mtime is None else max(newest_mtime, entry.mtime)
        )

    return VideoCacheStats(
        file_count=file_count,
        total_size=total_size,
        oldest_mtime=oldest_mtime,
        newest_mtime=newest_mtime,
    )


def clean_video_cache(max_age_days: int | None = None) -> VideoCacheCleanupResult:
    """
    Xóa bộ đệm video mặc định và trả về kết quả tổng hợp có thể hiển thị cho người dùng.

    Có thể có một khoảng thời gian dài giữa việc xem trước trang và nhấp chuột thực tế để dọn dẹp, vì vậy bạn phải quét lại và đánh giá khi thực hiện.
    Danh sách ứng viên cũ không thể được sử dụng lại. Việc xóa áp dụng khả năng chịu lỗi theo từng tệp: ghi lại khi một tệp bị chiếm hoặc không có đủ quyền
    Cảnh báo và tiếp tục tránh một tệp bất thường trong số hàng trăm tệp khiến toàn bộ quá trình dọn dẹp không thành công.
    """

    _validate_max_age_days(max_age_days)
    now = time.time()
    logger.info(
        f"start cleaning video cache: max_age_days={max_age_days}"
    )

    candidate_count = 0
    candidate_size = 0
    deleted_count = 0
    deleted_size = 0
    failed_count = 0
    cache_dir = video_cache_dir()

    # Xóa trong khi quét mà không lưu toàn bộ danh sách ứng viên vào bộ nhớ. Ngay cả khi thư mục tăng lên hàng trăm nghìn tệp,
    # Bộ nhớ bổ sung trong quá trình dọn dẹp vẫn không đổi; sử dụng hợp nhất ngay trong quá trình thực thi để tránh quá trình dọn dẹp mất nhiều thời gian.
    # Thời gian giới hạn liên tục thay đổi tạo ra một lượng ứng viên không thể đoán trước.
    for entry in _iter_video_cache_entries():
        if not _is_cleanup_candidate(entry, max_age_days, now):
            continue
        candidate_count += 1
        candidate_size += entry.size
        try:
            # entry.path xuất phát từ scandir cấp đầu tiên của thư mục mặc định; xác minh lại thư mục mẹ và tổng trước khi xóa
            # Tên tệp để ngăn chặn việc vô tình mở rộng phạm vi có thể xóa khi sửa đổi logic quét trong tương lai.
            if (
                os.path.realpath(os.path.dirname(entry.path)) != cache_dir
                or not _VIDEO_CACHE_FILE_PATTERN.fullmatch(entry.name)
                or os.path.islink(entry.path)
            ):
                raise ValueError("cache file is outside the managed directory")
            os.unlink(entry.path)
            deleted_count += 1
            deleted_size += entry.size
        except (OSError, ValueError) as exc:
            failed_count += 1
            logger.warning(
                f"failed to delete video cache file: file={entry.name}, error={exc}"
            )

    logger.info(
        "finished cleaning video cache: "
        f"candidates={candidate_count}, candidate_bytes={candidate_size}, "
        f"deleted={deleted_count}, deleted_bytes={deleted_size}, failed={failed_count}"
    )
    return VideoCacheCleanupResult(
        deleted_count=deleted_count,
        deleted_size=deleted_size,
        failed_count=failed_count,
    )
