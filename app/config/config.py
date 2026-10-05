import copy
import errno
import os
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager

import toml
from loguru import logger

from app import __version__

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
config_file = f"{root_dir}/config.toml"
_CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "kubepods", "libpod", "podman")
_DOCKER_HOST_GATEWAY_NAME = "host.docker.internal"
_config_save_lock = threading.RLock()
_pending_config_lock = threading.RLock()
_pending_config_updates = {}
_pending_config_save_requested = False
_pending_config_flush_scheduled = False
_MISSING = object()
_DELETE = object()
_UTF8_BOM = "\ufeff"


class _SynchronizedConfig(dict):
    """Giữ nguyên cách sử dụng dict và làm cho các thao tác ghi cấu hình thời gian chạy tuân theo cùng một khóa."""

    def __setitem__(self, key, value):
        # Streamlit sẽ ghi lại giá trị điều khiển hiện tại vào cấu hình mỗi khi toàn bộ trang được chạy lại. nhiệm vụ video đang giữ
        # Khi run_config_lock, nếu giá trị không thay đổi thì cách viết này không có tác dụng phụ và
        # Trang được làm mới không nên bị kẹt ở giữa biểu mẫu. Các ghi thực sự thay đổi cấu hình vẫn đi vào khóa dưới,
        # Do đó, bạn không thể chuyển đổi nhà cung cấp, khóa hoặc cài đặt chung khác khi đang tạo video.
        current = super().get(key, _MISSING)
        if current is not _MISSING and current == value:
            return
        with _config_save_lock:
            super().__setitem__(key, value)

    def __delitem__(self, key):
        with _config_save_lock:
            super().__delitem__(key)

    def clear(self):
        if not self:
            return
        with _config_save_lock:
            super().clear()

    def pop(self, key, default=_MISSING):
        # ``pop(key, default)`` cũng không thay đổi cấu hình khi key không tồn tại. Cách sử dụng WebUI
        # Cách viết này thể hiện "áp dụng chính sách mặc định", phải được phép hoàn thành trực tiếp khi làm mới.
        if key not in self:
            if default is _MISSING:
                raise KeyError(key)
            return default
        with _config_save_lock:
            if default is _MISSING:
                return super().pop(key)
            return super().pop(key, default)

    def setdefault(self, key, default=None):
        # Giống như __setitem__, đặt mặc định cho khóa hiện tại là thao tác chỉ đọc. Về sớm
        # Điều này cho phép làm mới trang chỉ đọc cấu hình mặc định để không bị ảnh hưởng bởi các khóa cấu hình tác vụ dài.
        current = super().get(key, _MISSING)
        if current is not _MISSING:
            return current
        with _config_save_lock:
            return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        if all(
            (current := dict.get(self, key, _MISSING)) is not _MISSING
            and current == value
            for key, value in changes.items()
        ):
            return
        with _config_save_lock:
            super().update(changes)


def _pending_update_key(config_section, key):
    """Tạo khóa để cập nhật cho các phân vùng cấu hình cố định trong quá trình."""
    return id(config_section), key


def update_config_nonblocking(config_section, key, value):
    """
    Cập nhật không chặn cấu hình thời gian chạy của WebUI.

    Quá trình tạo video sẽ giữ ``runtime_config_lock`` để đảm bảo rằng tác vụ tương tự sẽ không bị chuyển đổi trong khi thực thi
    Nhà cung cấp, khóa hoặc cấu hình giọng nói. Điều khiển Streamlit không thể đợi khóa tác vụ dài này khi nó thay đổi.
    Nếu không trình duyệt sẽ xuất hiện trạng thái đóng băng trang. Cập nhật ngay khi khóa không hoạt động; chỉ giữ lại từng mục cấu hình khi khóa bận
    Giá trị mới nhất được áp dụng thống nhất khi tác vụ hiện tại giải phóng khóa.

    Trả về True nghĩa là giá trị đã có hiệu lực, False nghĩa là nó đã vào hàng đợi cần cập nhật.
    """
    # Tất cả các bản cập nhật được đưa vào cùng một hàng đợi trước khi cố gắng lấy khóa cấu hình. Bằng cách này, nhiều trang có thể sửa đổi cùng một trang cùng một lúc.
    # Khi định cấu hình các mục, thứ tự ghi vào hàng đợi là thứ tự cuối cùng và sẽ không có luồng nào trước đó sau khi có được khóa.
    # Các giá trị đã được xếp hàng bởi các luồng mới hơn đã bị xóa nhầm.
    with _pending_config_lock:
        _pending_config_updates[_pending_update_key(config_section, key)] = (
            config_section,
            key,
            copy.deepcopy(value),
        )

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        # Người gọi thường sẽ yêu cầu lưu khi kết thúc lần chạy lại Streamlit này, nhưng không thể dựa vào bước này
        # Phải được thực hiện. Ví dụ: nếu trang có vấn đề bất thường ở giữa hoặc việc cập nhật xảy ra khi tác vụ thoát khỏi giai đoạn lưu thì vẫn cần phải
        # Có một luồng làm mới nền để đảm bảo rằng giá trị được xếp hàng cuối cùng có hiệu lực.
        _schedule_deferred_config_flush()
        return False

    try:
        _apply_pending_config_updates_locked()
        return config_section.get(key, _MISSING) == value
    finally:
        _config_save_lock.release()


def delete_config_nonblocking(config_section, key):
    """
    Xóa không chặn các mục cấu hình WebUI.

    "Sử dụng mặc định" thực sự yêu cầu xóa mục cấu hình thay vì viết một chuỗi trống. Cấu hình nghề nghiệp nhiệm vụ video
    Khi bị khóa, mục đích xóa sẽ ghi đè các bản cập nhật được xếp hàng đợi trước đó vào cùng một mục cấu hình và được thực thi sau khi tác vụ kết thúc.
    """
    with _pending_config_lock:
        _pending_config_updates[_pending_update_key(config_section, key)] = (
            config_section,
            key,
            _DELETE,
        )

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        _schedule_deferred_config_flush()
        return False

    try:
        _apply_pending_config_updates_locked()
        return key not in config_section
    finally:
        _config_save_lock.release()


def _apply_pending_config_updates_locked():
    """Áp dụng các giá trị cấu hình mới nhất do WebUI sắp xếp trong khi vẫn giữ khóa ghi cấu hình."""
    with _pending_config_lock:
        updates = list(_pending_config_updates.values())
        _pending_config_updates.clear()
        # Khóa cập nhật đang chờ xử lý tiếp tục được giữ trong khi áp dụng cấu hình. Do đó, luồng đọc ảnh chụp nhanh "giá trị hiện tại + giá trị sẽ được cập nhật"
        # Chỉ có thể nhìn thấy trạng thái hoàn chỉnh trước hoặc sau ứng dụng và chỉ các bộ sưu tập cấu hình được cập nhật một nửa sẽ không được đọc.
        for config_section, key, value in updates:
            if value is _DELETE:
                config_section.pop(key, None)
            else:
                config_section[key] = value
    return bool(updates)


def snapshot_config_with_pending(config_section):
    """
    Trả về ảnh chụp nhanh hợp lệ của phân vùng cấu hình, hợp nhất các bản cập nhật WebUI chưa được áp dụng.

    Không thể viết lại cấu hình chung khi tác vụ video bị khóa nhưng người dùng vẫn có thể chuẩn bị phần nội dung tiếp theo. Yêu cầu LLM
    Sau khi sử dụng snapshot này, Provider, model và key vừa chọn trong giao diện sẽ tham gia vào request mới, đồng thời
    Không thay đổi tác vụ video đang được thực hiện.
    """
    with _pending_config_lock:
        snapshot = dict(config_section)
        section_id = id(config_section)
        for (pending_section_id, key), (_, _, value) in _pending_config_updates.items():
            if pending_section_id != section_id:
                continue
            if value is _DELETE:
                snapshot.pop(key, None)
            else:
                snapshot[key] = copy.deepcopy(value)
    return snapshot


def _flush_pending_config_locked(*, suppress_save_errors):
    """Áp dụng và lưu tất cả các cấu hình hiện đang chờ xử lý trong khi vẫn giữ khóa ghi cấu hình."""
    global _pending_config_save_requested

    updates_applied = _apply_pending_config_updates_locked()
    with _pending_config_lock:
        save_requested = _pending_config_save_requested
        _pending_config_save_requested = False

    if not updates_applied and not save_requested:
        return True

    try:
        save_config()
        return True
    except Exception as exc:
        # Cấu hình trong bộ nhớ đã được áp dụng thành công. Nếu lưu không thành công, chỉ có dấu lưu đang chờ xử lý sẽ được giữ lại. Nhiệm vụ video không nên
        # Việc sửa đổi không thành công vì tệp cấu hình tạm thời không thể ghi được; tương tác trang tiếp theo sẽ kích hoạt lưu lại.
        with _pending_config_lock:
            _pending_config_save_requested = True
        if not suppress_save_errors:
            raise
        logger.exception(f"failed to save deferred runtime config: {exc}")
        return False


def _run_deferred_config_flush():
    """Chờ các tác vụ dài để giải phóng các khóa cấu hình và xóa các bản cập nhật cấu hình tích lũy trong khoảng thời gian một cách đáng tin cậy."""
    global _pending_config_flush_scheduled

    while True:
        with _config_save_lock:
            flush_succeeded = _flush_pending_config_locked(
                suppress_save_errors=True
            )

        with _pending_config_lock:
            has_pending_work = bool(
                _pending_config_updates or _pending_config_save_requested
            )
            if not flush_succeeded or not has_pending_work:
                _pending_config_flush_scheduled = False
                return


def _schedule_deferred_config_flush():
    """Đảm bảo rằng có nhiều nhất một luồng nền đang chờ làm mới cấu hình cùng một lúc."""
    global _pending_config_flush_scheduled

    with _pending_config_lock:
        if _pending_config_flush_scheduled:
            return
        _pending_config_flush_scheduled = True

    threading.Thread(
        target=_run_deferred_config_flush,
        name="mpt-config-flush",
        daemon=True,
    ).start()


def try_save_config():
    """
    Cấu hình WebUI được lưu theo cách không chặn. Khi khóa bận, nó sẽ được lưu sau khi hoàn thành nhiệm vụ dài hiện tại.

    Các tập lệnh API, CLI và bảo trì thông thường vẫn có thể gọi ``save_config`` để có được ngữ nghĩa ghi chặn ban đầu;
    Chỉ Streamlit rerun mới sử dụng chức năng này để ngăn trang không phản hồi trong thời gian dài trong khi chờ tác vụ video.
    """
    global _pending_config_save_requested

    with _pending_config_lock:
        _pending_config_save_requested = True

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        _schedule_deferred_config_flush()
        return False

    try:
        return _flush_pending_config_locked(suppress_save_errors=False)
    finally:
        _config_save_lock.release()


@contextmanager
def runtime_config_lock():
    """
    Ngăn chặn các phiên WebUI khác ghi đè cấu hình trong quá trình thực hiện thao tác hoàn chỉnh dựa trên cấu hình chung.

    Dự án hiện tại liên kết địa chỉ loopback cục bộ theo mặc định và cấu hình vẫn là cấu hình toàn cầu một người dùng. Khóa nhẹ này chủ yếu
    Bảo vệ các hoạt động kéo dài như tạo và lắng nghe để ngăn tab khác chuyển đổi nhà cung cấp hoặc khóa ở giữa quá trình hoạt động.
    """
    with _config_save_lock:
        # Nếu luồng làm mới nền chưa được lên lịch khi thao tác ngắn trước đó giải phóng khóa thì tác vụ mới phải được đọc
        # Hàng đợi được áp dụng trước các cấu hình chung như nhà cung cấp và khóa. Bạn không thể tiếp tục sử dụng cấu hình cũ để thực thi toàn bộ quy trình.
        _flush_pending_config_locked(suppress_save_errors=True)
        try:
            yield
        finally:
            _flush_pending_config_locked(suppress_save_errors=True)


@contextmanager
def try_runtime_config_lock():
    """
    Cố gắng lấy khóa cấu hình thời gian chạy và trả về ngay lập tức nếu thành công.

    Thử giọng WebUI là một thao tác ngắn do người dùng chủ động kích hoạt và không nên đợi vài phút trong khi tác vụ video nền bị khóa.
    Người gọi có thể nhắc người dùng thử lại sau khi không lấy được khóa; sau khi lấy được khóa thành công, thời gian nghe vẫn có thể được đảm bảo.
    Cấu hình nhà cung cấp, khóa và mô hình sẽ không bị sửa đổi bởi các phiên khác.
    """
    acquired = _config_save_lock.acquire(blocking=False)
    try:
        if acquired:
            _flush_pending_config_locked(suppress_save_errors=True)
        yield acquired
    finally:
        if acquired:
            _flush_pending_config_locked(suppress_save_errors=True)
            _config_save_lock.release()


def is_running_in_container(
    dockerenv_path: str = "/.dockerenv",
    containerenv_path: str = "/run/.containerenv",
    cgroup_path: str = "/proc/1/cgroup",
) -> bool:
    """
    Xác định xem quy trình hiện tại có đang chạy trong vùng chứa hay không.

    Phán quyết này chủ yếu được sử dụng để lựa chọn địa chỉ mặc định của Ollama:
    - Khi chạy trên máy cục bộ thông thường, `localhost` trỏ tới chính máy của người dùng;
    - Trong Docker container, `localhost` trỏ tới chính container đó và truy cập vào máy chủ Ollama
      Thông thường bạn cần sử dụng `host.docker.internal`.

    Bạn không thể chỉ xác định liệu `/proc/1/cgroup` có tồn tại hay không, bởi vì Linux thông thường cũng sẽ có tệp này.
    Ở đây, True chỉ được trả về khi phát hiện thấy thẻ vùng chứa rõ ràng để tránh vô tình gây thương tích cho người dùng không sử dụng Docker Linux.
    Các tham số được dành riêng dưới dạng đường dẫn có thể chèn để tạo điều kiện thuận lợi cho việc kiểm tra đơn vị trên các môi trường hoạt động khác nhau.
    """
    if os.path.isfile(dockerenv_path) or os.path.isfile(containerenv_path):
        return True

    try:
        with open(cgroup_path, mode="r", encoding="utf-8") as fp:
            cgroup_content = fp.read().lower()
    except OSError:
        return False

    return any(marker in cgroup_content for marker in _CONTAINER_CGROUP_MARKERS)


def _can_resolve_hostname(hostname: str) -> bool:
    try:
        socket.gethostbyname(hostname)
    except OSError:
        return False
    return True


def _decode_linux_route_gateway(hex_gateway: str) -> str:
    # Cổng trong /proc/net/route là hệ thập lục phân nhỏ endian, ví dụ: 010011AC có nghĩa là
    # 172.17.0.1. Nó được phân tích cú pháp riêng ở đây để sử dụng khi Docker Linux gốc không có
    # bản ghi DNS hosting.docker.internal, nó cũng có thể cố gắng truy cập máy chủ trên cổng mặc định của vùng chứa.
    if len(hex_gateway) != 8:
        raise ValueError("invalid gateway length")

    octets = [
        str(int(hex_gateway[index : index + 2], 16)) for index in range(6, -1, -2)
    ]
    return ".".join(octets)


def get_container_default_gateway_ip(route_path: str = "/proc/net/route") -> str:
    """
    Đọc IP cổng mặc định trong vùng chứa Linux.

    Docker Desktop thường cung cấp `host.docker.internal`, nhưng Docker Linux gốc
    Tên DNS này không nhất thiết phải được cung cấp theo mặc định. Cổng mặc định thường có thể được sử dụng để truy cập các dịch vụ máy chủ.
    Địa chỉ bí mật; nếu Ollama của người dùng chỉ nghe 127.0.0.1 thì người dùng vẫn cần cho phép
    Ollama lắng nghe card mạng máy chủ hoặc định cấu hình `ollama_base_url` theo cách thủ công.
    """
    try:
        with open(route_path, mode="r", encoding="utf-8") as fp:
            route_lines = fp.readlines()
    except OSError:
        return ""

    for line in route_lines[1:]:
        fields = line.strip().split()
        if len(fields) < 3:
            continue

        destination = fields[1]
        gateway = fields[2]
        if destination != "00000000" or gateway == "00000000":
            continue

        try:
            return _decode_linux_route_gateway(gateway)
        except ValueError:
            logger.warning(f"invalid container gateway route entry: {line.strip()}")
            return ""

    return ""


def get_default_ollama_base_url() -> str:
    """
    Trả về base_url tương thích với OpenAI mặc định của Ollama.

    Người dùng sẽ không vào đây khi định cấu hình rõ ràng `ollama_base_url`; cái này chỉ xử lý "không được cấu hình"
    "Giá trị mặc định tốt nhất". Vùng chứa mặc định là máy chủ và máy cục bộ bình thường chạy tới localhost theo mặc định.
    """
    if not is_running_in_container():
        return "http://localhost:11434/v1"

    if _can_resolve_hostname(_DOCKER_HOST_GATEWAY_NAME):
        return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"

    gateway_ip = get_container_default_gateway_ip()
    if gateway_ip:
        logger.info(
            "host.docker.internal is not resolvable, fallback to container "
            f"default gateway for Ollama: {gateway_ip}"
        )
        return f"http://{gateway_ip}:11434/v1"

    logger.warning(
        "failed to resolve host.docker.internal and container default gateway; "
        "fallback to host.docker.internal for Ollama"
    )
    return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"


def _load_toml_config(config_path: str):
    """
    Tải TOML và tương thích với các BOM UTF-8 trùng lặp mà trình soạn thảo Windows có thể viết.

    ``utf-8-sig`` sẽ chỉ xóa BOM ở đầu tệp. Một số trình soạn thảo Windows hoặc
    Quá trình giải nén và lưu có thể ghi lại BOM khiến ký tự vô hình thứ hai nhập vào TOML
    Trình phân tích cú pháp báo lỗi ở dòng đầu tiên. Ở đây chỉ chuẩn hóa chỉ đọc được thực hiện sau khi phân tích cú pháp tiêu chuẩn không thành công,
    Không ghi lại file gốc để tránh vô tình ghi đè API Key mà người dùng đã điền.
    """
    try:
        return toml.load(config_path)
    except (toml.TomlDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "load config failed, retry with UTF-8 BOM compatibility: "
            f"path={config_path}, error={type(exc).__name__}: {exc}"
        )

    try:
        with open(config_path, mode="r", encoding="utf-8-sig") as fp:
            config_content = fp.read()

        normalized_content = config_content.lstrip(_UTF8_BOM)
        removed_bom_count = len(config_content) - len(normalized_content)
        if removed_bom_count:
            logger.warning(
                "removed repeated UTF-8 BOM characters while loading config: "
                f"path={config_path}, count={removed_bom_count}"
            )
        return toml.loads(normalized_content)
    except (toml.TomlDecodeError, UnicodeDecodeError) as exc:
        logger.error(
            "config file is not valid TOML after UTF-8 BOM normalization: "
            f"path={config_path}, error={type(exc).__name__}: {exc}"
        )
        raise


def load_config():
    # fix: IsADirectoryError: [Errno 21] Is a directory: '/MoneyPrinterTurbo/config.toml'
    if os.path.isdir(config_file):
        shutil.rmtree(config_file)

    if not os.path.isfile(config_file):
        example_file = f"{root_dir}/config.example.toml"
        if os.path.isfile(example_file):
            shutil.copyfile(example_file, config_file)
            logger.info("copy config.example.toml to config.toml")

    logger.info(f"load config from file: {config_file}")

    return _load_toml_config(config_file)


def save_config():
    """
    Tiết kiệm nguyên tử cấu hình thời gian chạy.

    Các phiên khác nhau của Streamlit có thể kích hoạt lưu cấu hình vào những thời điểm tương tự. Khi ghi đè trực tiếp config.toml,
    Một luồng khác có thể đọc nội dung TOML chỉ được viết một phần. Tuần tự hóa khóa đăng nhập lại trong quá trình được sử dụng ở đây
    Lưu, trước tiên ghi vào tệp tạm thời trong cùng thư mục, sau đó thay thế tệp đích thông qua os.replace.

    Gắn kết liên kết tệp đơn Docker Desktop sẽ sử dụng chính config.toml làm điểm gắn kết.
    Nhân Linux không cho phép thay thế điểm gắn kết thông qua đổi tên/thay thế, do đó EBUSY được trả về.
    Trong trường hợp này, tệp chỉ có thể được ghi đè tại chỗ trong khóa; các trường hợp ngoại lệ khác vẫn được đưa ra để tránh che đậy các quyền, ổ đĩa
    Hoặc đường dẫn sai.

    Điều này vẫn giữ lại ngữ nghĩa cấu hình toàn cầu một người dùng hiện có của dự án mà không đưa vào hệ thống cấu hình nhiều người dùng phức tạp bổ sung;
    Chủ yếu được sử dụng để tránh làm hỏng các tệp cấu hình trong các trang có nhiều tab hoặc chạy lại nhanh.
    """
    with _config_save_lock:
        config_to_save = dict(_cfg)
        config_to_save["app"] = dict(app)
        config_to_save["azure"] = dict(azure)
        config_to_save["siliconflow"] = dict(siliconflow)
        config_to_save["minimax_tts"] = dict(minimax_tts)
        config_to_save["elevenlabs"] = dict(elevenlabs)
        config_to_save["chatterbox"] = dict(chatterbox)
        config_to_save["kokoro"] = dict(kokoro)
        config_to_save["viettts"] = dict(viettts)
        config_to_save["fish_audio"] = dict(fish_audio)
        config_to_save["ui"] = dict(ui)
        config_to_save["tryon"] = dict(tryon)
        config_to_save["dance"] = dict(dance)
        serialized_config = toml.dumps(config_to_save)

        # Lưu sẽ được gọi khi kết thúc quá trình chạy lại hoàn chỉnh của WebUI. Quay lại trực tiếp khi nội dung không thay đổi để tránh mỗi lần
        # Nhấp vào điều khiển bình thường sẽ gây ra lỗi ghi đĩa và fsync.
        try:
            with open(config_file, mode="r", encoding="utf-8") as f:
                if f.read() == serialized_config:
                    _cfg.clear()
                    _cfg.update(config_to_save)
                    return
        except (OSError, UnicodeError):
            pass

        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".config-",
                suffix=".toml.tmp",
                dir=root_dir,
            )
            with os.fdopen(fd, mode="w", encoding="utf-8") as f:
                f.write(serialized_config)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(temp_path, config_file)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise

                logger.warning(
                    "atomic config replacement is unavailable for the mounted "
                    f"file, fallback to in-place write: {config_file}"
                )
                with open(config_file, mode="w", encoding="utf-8") as f:
                    f.write(serialized_config)
                    f.flush()
                    os.fsync(f.fileno())
            _cfg.clear()
            _cfg.update(config_to_save)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)


_cfg = load_config()
app = _SynchronizedConfig(_cfg.get("app", {}))
whisper = _cfg.get("whisper", {})
proxy = _cfg.get("proxy", {})
azure = _SynchronizedConfig(_cfg.get("azure", {}))
siliconflow = _SynchronizedConfig(_cfg.get("siliconflow", {}))
minimax_tts = _SynchronizedConfig(_cfg.get("minimax_tts", {}))
elevenlabs = _SynchronizedConfig(_cfg.get("elevenlabs", {}))
chatterbox = _SynchronizedConfig(_cfg.get("chatterbox", {}))
kokoro = _SynchronizedConfig(_cfg.get("kokoro", {}))
viettts = _SynchronizedConfig(_cfg.get("viettts", {}))
fish_audio = _SynchronizedConfig(_cfg.get("fish_audio", {}))
ui = _SynchronizedConfig(
    _cfg.get(
        "ui",
        {
            "hide_log": False,
        },
    )
)
tryon = _SynchronizedConfig(_cfg.get("tryon", {}))
dance = _SynchronizedConfig(_cfg.get("dance", {}))

hostname = socket.gethostname()

log_level = _cfg.get("log_level", "DEBUG")
listen_host = _cfg.get("listen_host", "0.0.0.0")
listen_port = _cfg.get("listen_port", 8080)
project_name = _cfg.get("project_name", "MoneyPrinterTurbo")
project_description = _cfg.get(
    "project_description",
    "<a href='https://github.com/harry0703/MoneyPrinterTurbo'>https://github.com/harry0703/MoneyPrinterTurbo</a>",
)
project_version = _cfg.get("project_version", __version__)
reload_debug = False

app["redis_host"] = os.getenv(
    "MPT_APP_REDIS_HOST",
    os.getenv("REDIS_HOST", app.get("redis_host", "localhost")),
)

ffmpeg_path = app.get("ffmpeg_path", "")
if ffmpeg_path and os.path.isfile(ffmpeg_path):
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_path

logger.info(f"{project_name} v{project_version}")
