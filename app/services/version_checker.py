"""Kiểm tra xem có phiên bản chính thức mới của MoneyPrinterTurbo không."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import requests
from loguru import logger
from packaging.version import InvalidVersion, Version


LATEST_RELEASE_API_URL: Final = (
    "https://api.github.com/repos/harry0703/MoneyPrinterTurbo/releases/latest"
)
LATEST_RELEASE_PAGE_URL: Final = (
    "https://github.com/harry0703/MoneyPrinterTurbo/releases/latest"
)
# Kiểm tra cập nhật chỉ là chức năng phụ trợ và các bất thường về mạng không thể làm chậm đáng kể WebUI cục bộ. Hạn chế riêng biệt về kết nối và đọc
# Khoảng thời gian chờ không chỉ cho phép GitHub hoàn thành phản hồi trong mạng bình thường mà còn tránh phải chờ đợi lâu trong môi trường ngoại tuyến.
RELEASE_CHECK_TIMEOUT: Final = (1.0, 2.0)
RELEASE_CHECK_HEADERS: Final = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "MoneyPrinterTurbo-Version-Checker",
}
UPDATE_CHECK_CACHE_TTL_SECONDS: Final = 12 * 60 * 60


def _parse_version(value: str) -> Version:
    """Tương thích với các thẻ ``v1.2.3`` thường được sử dụng của GitHub và được chuyển đổi thành các phiên bản tương đương."""
    normalized = str(value or "").strip()
    if normalized.lower().startswith("v"):
        normalized = normalized[1:]
    return Version(normalized)


def get_available_update(current_version: str) -> str | None:
    """
    Trả về phiên bản chính thức mới nhất cao hơn phiên bản hiện tại; trả về Không nếu không có cập nhật hoặc kiểm tra không thành công.

    Giao diện ``bản phát hành/mới nhất`` của GitHub tự động loại trừ các phiên bản nháp và bản phát hành trước, do đó không còn nữa
    Thực hiện lọc trạng thái phát hành nhiều lần. WebUI gọi hàm này ở chế độ nền thông qua ``AsyncUpdateChecker``;
    Khi mạng, định dạng phản hồi hoặc nhãn phiên bản không bình thường, chỉ nhật ký sẽ được ghi lại và hạ cấp xuống "Không hiển thị thông báo", điều này sẽ không ảnh hưởng
    Các chức năng cốt lõi như tạo video.
    """
    try:
        installed_version = _parse_version(current_version)
    except InvalidVersion:
        logger.warning(
            f"skip update check because current version is invalid: {current_version!r}"
        )
        return None

    try:
        response = requests.get(
            LATEST_RELEASE_API_URL,
            headers=RELEASE_CHECK_HEADERS,
            timeout=RELEASE_CHECK_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        # Lỗi kiểm tra cập nhật là những trường hợp ngoại lệ không cốt lõi có thể phục hồi được. Giữ lại các loại ngoại lệ và thông tin để tạo điều kiện cho các tác nhân định vị,
        # Điều chỉnh DNS, GitHub hoặc các vấn đề hỏng hóc trong phản hồi đồng thời tránh làm phiền người dùng thông thường trong WebUI.
        logger.debug(
            "GitHub release check failed: "
            f"error_type={type(exc).__name__}, error={exc}"
        )
        return None

    if not isinstance(payload, dict):
        logger.debug(
            "GitHub release check returned an invalid payload: "
            f"payload_type={type(payload).__name__}"
        )
        return None

    tag_name = payload.get("tag_name", "")
    try:
        latest_version = _parse_version(tag_name)
    except InvalidVersion:
        logger.warning(
            f"skip update notification because release tag is invalid: {tag_name!r}"
        )
        return None

    if latest_version <= installed_version:
        return None

    normalized_latest_version = str(latest_version)
    logger.info(
        "MoneyPrinterTurbo update available: "
        f"current={installed_version}, latest={normalized_latest_version}"
    )
    return normalized_latest_version


@dataclass(frozen=True)
class UpdateCheckSnapshot:
    """Trạng thái ngay lập tức của phiên bản nền sẽ kiểm tra xem WebUI có đọc không bị chặn hay không."""

    complete: bool
    available_version: str | None = None


class AsyncUpdateChecker:
    """
    Thực hiện kiểm tra phiên bản trong luồng nền và lưu vào bộ đệm các kết quả mới nhất.

    Streamlit sẽ thực thi tập lệnh trang ngay từ đầu sau bất kỳ tương tác điều khiển nào. Nếu truy cập trực tiếp vào khu vực tiêu đề
    GitHub, chặn toàn bộ trang khi mở lần đầu hoặc bộ đệm hết hạn. Ở đây yêu cầu mạng được đưa vào luồng daemon,
    Trang chỉ đọc ảnh chụp nhanh hiện tại; sau khi quá trình kiểm tra hoàn tất, kết quả sẽ được làm mới một lần bởi đoạn ngắn hạn của WebUI.

    Kết quả, cho dù đó là "Đã tìm thấy bản cập nhật" hay "Không có bản cập nhật/lỗi mạng" sẽ được lưu vào bộ đệm để tránh GitHub
    Khi không thể truy cập được, nó sẽ được yêu cầu lại mỗi lần chạy lại. Khóa chỉ bảo vệ trạng thái bộ nhớ và không bao bọc yêu cầu mạng, vì vậy
    Không chặn các phiên khác khỏi trạng thái kiểm tra đọc.
    """

    def __init__(
        self,
        check: Callable[[str], str | None] = get_available_update,
        ttl_seconds: float = UPDATE_CHECK_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._check = check
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._current_version: str | None = None
        self._available_version: str | None = None
        self._completed_at: float | None = None
        self._checking = False

    def poll(self, current_version: str) -> UpdateCheckSnapshot:
        """Quay lại kiểm tra ảnh chụp ngay lập tức; bắt đầu kiểm tra mới ở chế độ nền khi bộ đệm hết hạn."""
        normalized_current_version = str(current_version or "").strip()
        now = self._clock()

        with self._lock:
            cache_is_fresh = (
                self._current_version == normalized_current_version
                and self._completed_at is not None
                and now - self._completed_at < self._ttl_seconds
            )
            if cache_is_fresh:
                return UpdateCheckSnapshot(
                    complete=True,
                    available_version=self._available_version,
                )

            if (
                self._checking
                and self._current_version == normalized_current_version
            ):
                return UpdateCheckSnapshot(complete=False)

            # Khi phiên bản thay đổi hoặc bộ đệm hết hạn, kết quả cũ sẽ không tiếp tục được hiển thị. Xóa trạng thái trước rồi bắt đầu
            # Một chuỗi mới để người gọi có được ảnh chụp nhanh rõ ràng về tình trạng chờ xử lý trong quá trình kiểm tra.
            self._current_version = normalized_current_version
            self._available_version = None
            self._completed_at = None
            self._checking = True

            worker = threading.Thread(
                target=self._run_check,
                args=(normalized_current_version,),
                name="mpt-version-check",
                daemon=True,
            )
            worker.start()

        return UpdateCheckSnapshot(complete=False)

    def _run_check(self, current_version: str) -> None:
        try:
            available_version = self._check(current_version)
        except Exception:
            # get_available_update xử lý các ngoại lệ về dữ liệu và mạng dự kiến. Đây là chủ đề nền
            # Cuối cùng, để bảo vệ ranh giới, toàn bộ ngăn xếp phải được ghi lại để tránh tình trạng chờ xử lý vĩnh viễn sau ngoại lệ không mong muốn và chấm dứt im lặng.
            logger.exception(
                "unexpected error while checking for a MoneyPrinterTurbo update"
            )
            available_version = None

        with self._lock:
            # Trong một số ít trường hợp, phiên bản có thể thay đổi trong quá trình hoạt động. Các chủ đề cũ không được ghi đè lên các phiên bản trạng thái mới.
            if self._current_version != current_version:
                return
            self._available_version = available_version
            self._completed_at = self._clock()
            self._checking = False


_ASYNC_UPDATE_CHECKER = AsyncUpdateChecker()


def poll_available_update(current_version: str) -> UpdateCheckSnapshot:
    """Đọc trạng thái của trình kiểm tra lý lịch chung để tránh các yêu cầu lặp lại tới GitHub cho các phiên Streamlit khác nhau."""
    return _ASYNC_UPDATE_CHECKER.poll(current_version)
