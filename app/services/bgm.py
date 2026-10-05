import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from loguru import logger

from app.utils import file_security, utils


# Streamlit cho phép tải lên các tệp lớn hơn theo mặc định nhưng nhạc nền thường chỉ có kích thước vài MB. Đặt rõ ràng ở đây
# Giới hạn trên ở phía máy chủ ngăn API hoặc WebUI ghi hoàn toàn các tệp cực lớn vào đĩa và ảnh hưởng đến các tác vụ video trong cùng một quy trình.
MAX_BGM_UPLOAD_BYTES = 30 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_INTERNAL_UPLOAD_PREFIX = ".bgm-upload-"
_WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
# Cuối cùng, MoviePy giải mã nhạc nền thông qua FFmpeg, do đó không cần có giới hạn nhân tạo đối với MP3. Chỉ mở ở đây
# Tiện ích mở rộng âm thanh chính thống và rõ ràng về mặt ngữ nghĩa để tránh tải nhầm các vùng chứa video như MP4 làm nhạc nền.
# Bộ dữ liệu này cũng đóng vai trò là nguồn dữ liệu duy nhất cho kiểm soát tải lên WebUI, do đó sẽ không có sự mâu thuẫn giữa mặt trước và mặt sau khi thêm hoặc xóa các định dạng sau này.
SUPPORTED_BGM_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
)


class BgmUploadError(ValueError):
    """Cho biết tệp đã tải lên không đáp ứng các yêu cầu về bảo mật hoặc định dạng đối với nhạc nền."""


class BgmServiceError(RuntimeError):
    """Cho biết lỗi thực thi phía máy chủ, chẳng hạn như FFmpeg hoặc hệ thống tệp không khả dụng."""


def should_use_bgm(bgm_type: str | None, bgm_volume: float | None) -> bool:
    """
    Thống nhất xác định xem tác vụ hiện tại có yêu cầu xử lý bất kỳ nhạc nền nào không.

    Quy tắc này không liên quan gì đến nguồn cụ thể: khi không có nguồn nào được chọn, âm lượng không hợp lệ hoặc âm lượng không lớn hơn 0, ngẫu nhiên,
    Các nhà cung cấp tùy chỉnh, Sonilo và tương lai phải bỏ qua quá trình phân tích cú pháp tệp, tạo bên ngoài và trộn lần cuối.
    Việc đặt nó trong dịch vụ BGM phổ quát sẽ tránh trùng lặp một bộ đánh giá 0 khối lượng cho mỗi nhà cung cấp bổ sung.
    """
    if not str(bgm_type or "").strip():
        return False
    try:
        normalized_volume = float(bgm_volume or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(normalized_volume) and normalized_volume > 0


def uploaded_bgm_dir(create: bool = True) -> str:
    """
    Trả về thư mục liên tục chứa nhạc nền của người dùng.

    Các bài hát cài sẵn thuộc về tài nguyên mã và tiếp tục được đặt trong tài nguyên/bài hát; nội dung do người dùng tải lên thuộc về dữ liệu thời gian chạy.
    Nó phải được đặt dưới bộ lưu trữ gắn kết của Docker. Nó có thể được giữ lại sau khi container được xây dựng lại và sẽ không gây ô nhiễm không gian làm việc Git.
    """
    return utils.storage_dir("bgm", create=create)


def _remove_staged_file(file_path: str) -> None:
    """Hãy cố gắng hết sức để xóa các tệp tạm thời được tải lên mà không ghi đè lên ngoại lệ ban đầu đang được người gọi xử lý."""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        # Các tệp tạm thời sử dụng tiền tố dành riêng và sẽ không đưa vào danh sách BGM; không dọn dẹp sẽ không gây ra "âm thanh bất hợp pháp"
        # Đợi ngoại lệ ban đầu chính xác hơn được khắc phục, nhưng phải để lại đường dẫn và lỗi hệ thống để hoạt động và bảo trì xác định.
        logger.warning(
            f"failed to remove staged background music: path={file_path}, "
            f"error={str(exc)}"
        )


def sanitize_upload_filename(filename: str) -> str:
    """Trích xuất tên tệp âm thanh có thể được hiển thị trên các nền tảng và từ chối các tên bất hợp pháp cũng như tiện ích mở rộng không được hỗ trợ."""
    safe_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name) > 255
        or any(ord(character) < 32 for character in safe_name)
        or any(character in _WINDOWS_INVALID_FILENAME_CHARS for character in safe_name)
        or safe_name.lower().startswith(_INTERNAL_UPLOAD_PREFIX)
    ):
        raise BgmUploadError("invalid background music filename")

    # Windows sẽ nhận dạng đoạn đầu tiên trước phần mở rộng là tên thiết bị, chẳng hạn như CON.mp3 và LPT1.wav.
    # Không thể tạo như một tập tin bình thường. Ngay cả khi máy chủ sử dụng UUID, việc từ chối sớm những tên đó có thể
    # Đảm bảo rằng hành vi nhập API nhất quán trên các nền tảng khác nhau.
    windows_basename = safe_name.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        raise BgmUploadError("invalid background music filename")
    if Path(safe_name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
        supported_formats = ", ".join(
            extension.removeprefix(".").upper()
            for extension in SUPPORTED_BGM_EXTENSIONS
        )
        raise BgmUploadError(
            f"unsupported background music format; supported formats: {supported_formats}"
        )
    return safe_name


def _validate_audio(file_path: str, timeout_seconds: int = 30) -> None:
    """
    Chỉ sử dụng FFmpeg hiện được định cấu hình cho dự án để xác minh rằng tệp chứa luồng âm thanh có thể giải mã hoàn toàn.

    Dự án cho phép imageio-ffmpeg cung cấp FFmpeg di động. Phương pháp cài đặt này không đảm bảo sự tồn tại đồng thời.
    Do đó, FFprobe không thể thêm các phụ thuộc nhị phân độc lập. `-map 0:a:0` sẽ thất bại nếu không có luồng âm thanh,
    `-xerror` sẽ đẩy lỗi giải mã thành lỗi; giải mã hoàn toàn cũng có thể vô tình chặn các tệp được mã hóa hoặc dữ liệu ngẫu nhiên
    Đánh giá sai về việc nhấn tiêu đề khung âm thanh. Tệp có thể chứa các luồng bổ sung như ảnh bìa album nhưng chỉ luồng âm thanh đầu tiên mới được xác minh.
    """
    try:
        decoded = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                file_path,
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BgmServiceError("FFmpeg background music validation timed out") from exc
    except OSError as exc:
        raise BgmServiceError("failed to run FFmpeg for background music validation") from exc
    if decoded.returncode != 0:
        raise BgmUploadError("uploaded file must contain a decodable audio stream")


def validate_audio_file(file_path: str, timeout_seconds: int = 120) -> None:
    """
    Xác minh rằng các tệp âm thanh trên đĩa có thể được Project FFmpeg giải mã hoàn toàn.

    Tải lên preflight thường chỉ mất 30 giây; Các bản nhạc do Sonilo tạo có thể dài tới 6 phút, vì vậy chúng có sẵn bên ngoài
    Sử dụng lại mục nhập với thời gian chờ có thể điều chỉnh. Dịch vụ này chỉ dựa trên FFmpeg và không yêu cầu cài đặt thêm FFprobe trên hệ thống.
    """
    if not os.path.isfile(file_path) or os.path.getsize(file_path) <= 0:
        raise BgmUploadError("background music file is empty or missing")
    _validate_audio(file_path, timeout_seconds=timeout_seconds)


def _stage_bgm_upload(filename: str, source: BinaryIO) -> tuple[str, str, int]:
    """
    Ghi luồng tải lên vào một tệp tạm thời trong cùng thư mục và trả về tên tệp an toàn, đường dẫn tạm thời và số byte.

    Quá trình tải lên trước và lưu giữ cuối cùng của WebUI phải sử dụng chính xác các lần đọc, giới hạn kích thước và tên tệp giống nhau
    Quy tắc, nếu không có thể có sự phân chia trạng thái nơi giao diện hiển thị có sẵn nhưng bị máy chủ từ chối sau khi nhấp để tạo.
    Các tệp tạm thời sẽ bị người gọi xóa hoặc thay thế nguyên tử sau khi hoàn tất việc thăm dò âm thanh.
    """
    safe_name = sanitize_upload_filename(filename)
    try:
        target_dir = uploaded_bgm_dir(create=True)
    except OSError as exc:
        raise BgmServiceError("failed to prepare background music storage") from exc
    temp_path = ""
    total_bytes = 0

    try:
        try:
            source.seek(0)
        except (AttributeError, OSError) as exc:
            raise BgmUploadError("background music upload is not seekable") from exc

        # Việc giữ tiện ích mở rộng ban đầu cho phép FFmpeg chọn tiện ích mở rộng chính xác cho các định dạng như AAC mà không cần tiêu đề vùng chứa.
        # bộ giải mã; các tập tin tạm thời vẫn được đặt trong thư mục đích để đảm bảo rằng thao tác os.replace cuối cùng là nguyên tử.
        descriptor, temp_path = tempfile.mkstemp(
            prefix=_INTERNAL_UPLOAD_PREFIX,
            suffix=Path(safe_name).suffix.lower(),
            dir=target_dir,
        )
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise BgmUploadError("background music upload must be binary")
                total_bytes += len(chunk)
                if total_bytes > MAX_BGM_UPLOAD_BYTES:
                    raise BgmUploadError("background music file exceeds the 30 MB limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if total_bytes == 0:
            raise BgmUploadError("background music file is empty")
        return safe_name, temp_path, total_bytes
    except Exception as exc:
        _remove_staged_file(temp_path)
        if isinstance(exc, BgmUploadError):
            raise
        if isinstance(exc, OSError):
            raise BgmServiceError("failed to stage background music upload") from exc
        raise
    finally:
        # Streamlit cũng cần sử dụng cùng một tệp đã tải lên để nghe trình duyệt; khôi phục con trỏ tập tin có thể
        # Tránh việc người chơi đọc nội dung trống hoặc lưu lần cuối sau khi xác minh.
        try:
            source.seek(0)
        except (AttributeError, OSError):
            pass


def validate_bgm_upload(filename: str, source: BinaryIO) -> str:
    """Xác thực hoàn toàn âm thanh đã tải lên nhưng không lưu giữ nó, được sử dụng cho ánh sáng trước WebUI trước khi hiển thị "Sẵn sàng"."""
    safe_name, temp_path, total_bytes = _stage_bgm_upload(filename, source)
    try:
        _validate_audio(temp_path)
        logger.debug(
            f"background music upload validated: name={safe_name}, "
            f"size={total_bytes} bytes"
        )
        return safe_name
    finally:
        _remove_staged_file(temp_path)


def save_bgm_upload(filename: str, source: BinaryIO) -> str:
    """
    Lưu nhạc nền của người dùng theo các phương pháp thay thế nguyên tử, có giới hạn và chia nhỏ.

    Các tình huống sử dụng bao gồm FastAPI UploadFile và Streamlit AddedFile, cả hai đều cung cấp tệp nhị phân
    Giao diện tập tin. Trước tiên hãy ghi tệp tạm thời vào cùng thư mục và xác minh nó, sau đó sử dụng os.replace để sao chép nó vào đĩa một cách nguyên tử, điều này có thể tránh được
    Tải lên đồng thời hoặc quá trình bị gián đoạn sẽ để lại một nửa tệp âm thanh, điều này cũng sẽ khiến các tệp tải lên có cùng tên nhận được các khóa lưu trữ UUID khác nhau.
    Do đó, các tác vụ được xếp hàng hoặc đang chạy luôn tham chiếu đến tệp bất biến gốc.
    """
    safe_name, temp_path, total_bytes = _stage_bgm_upload(filename, source)
    stored_name = f"{uuid4().hex}{Path(safe_name).suffix.lower()}"
    target_path = os.path.join(os.path.dirname(temp_path), stored_name)

    try:
        _validate_audio(temp_path)
        try:
            os.replace(temp_path, target_path)
        except OSError as exc:
            raise BgmServiceError("failed to persist background music upload") from exc
        temp_path = ""
        logger.info(
            f"background music uploaded: original_name={safe_name}, "
            f"stored_name={stored_name}, size={total_bytes} bytes"
        )
        return stored_name
    finally:
        _remove_staged_file(temp_path)


def _list_bgm_files(directories: tuple[str, ...]) -> list[str]:
    """Liệt kê các tệp nhạc nền an toàn và được hỗ trợ theo mức độ ưu tiên của thư mục."""
    files_by_name: dict[str, str] = {}
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory), key=str.lower):
            # Tải lên preflight và lưu lần cuối sẽ nhanh chóng tạo các tệp trong cùng một thư mục. Mặc dù hồ sơ tạm thời có tính pháp lý
            # Phần mở rộng âm thanh vẫn chưa được xác minh và không thể chọn trước trong danh sách BGM ngẫu nhiên.
            if name.startswith(_INTERNAL_UPLOAD_PREFIX):
                continue
            if Path(name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
                continue
            file_path = os.path.join(directory, name)
            try:
                # Kết quả liệt kê cũng cần được xác minh bằng đường dẫn thực. Nếu không kẻ tấn công có thể đặt vào một thư mục được phép
                # Một liên kết tượng trưng âm thanh trỏ đến một tệp bên ngoài và đưa nó tới MoviePy bằng đường dẫn BGM ngẫu nhiên.
                resolved_path = file_security.resolve_path_within_directory(
                    directory, file_path
                )
            except ValueError as exc:
                logger.warning(
                    f"skip unsafe background music file: name={name}, error={str(exc)}"
                )
                continue
            files_by_name[name] = resolved_path
    return [files_by_name[name] for name in sorted(files_by_name, key=str.lower)]


def list_builtin_bgm_files() -> list[str]:
    """
    Liệt kê nhạc nền tích hợp được phân phối cùng với dự án.

    "Bài hát cài sẵn" của WebUI và thiết lập nhập và xuất đặt trước chỉ sử dụng danh sách này, đảm bảo tên tệp đã lưu
    Có thể khôi phục trên thiết bị khác sử dụng cùng phiên bản; các tập tin do người dùng tải lên vẫn được Custom Music quản lý.
    """
    return _list_bgm_files((utils.song_dir(),))


def list_bgm_files() -> list[str]:
    """Liệt kê nhạc nền có sẵn do người dùng tải lên và tích hợp sẵn. Nếu trùng tên thì file tải lên sẽ được sử dụng trước."""
    return _list_bgm_files((utils.song_dir(), uploaded_bgm_dir(create=True)))


def resolve_builtin_bgm_file(unsafe_path: str) -> str:
    """Phân tích nhạc nền tích hợp theo tên tệp và từ chối đường dẫn, tệp không xác định và tệp do người dùng tải lên."""
    if not unsafe_path:
        raise ValueError("background music filename is required")

    filename = str(unsafe_path)
    if filename != os.path.basename(filename):
        raise ValueError("preset background music must use a filename")

    files_by_name = {
        os.path.basename(file_path): file_path for file_path in list_builtin_bgm_files()
    }
    if filename not in files_by_name:
        raise ValueError("preset background music is not available")
    return files_by_name[filename]


def resolve_bgm_file(unsafe_path: str) -> str:
    """
    Phân tích BGM trong thư mục tải lên của người dùng và thư mục bài hát tích hợp, đồng thời từ chối các đường dẫn bên ngoài hai danh sách trắng.

    Tên tệp chạm vào thư mục người dùng trước tiên, trong khi vẫn giữ lại `output000.mp3`, đường dẫn danh sách trắng tuyệt đối và
    `./resource/songs/output000.mp3` và các cách sử dụng cũ khác. Các tệp mới tải lên sử dụng UUID. Trong hoàn cảnh bình thường
    Sẽ không có tên trùng lặp với các bài hát cài sẵn hoặc nội dung tải lên lịch sử.
    """
    if (
        not unsafe_path
        or Path(unsafe_path).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS
    ):
        raise ValueError("unsupported background music path")

    candidates = [unsafe_path]
    if not os.path.isabs(unsafe_path):
        candidates.append(os.path.join(utils.root_dir(), unsafe_path))

    last_error = ValueError("background music file does not exist")
    for directory in (uploaded_bgm_dir(create=True), utils.song_dir()):
        for candidate in candidates:
            try:
                return file_security.resolve_path_within_directory(directory, candidate)
            except ValueError as exc:
                last_error = exc
    raise ValueError(str(last_error)) from last_error
