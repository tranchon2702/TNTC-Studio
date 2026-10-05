import os


def resolve_path_within_directory(
    base_dir: str,
    unsafe_path: str,
    *,
    require_file: bool = True,
) -> str:
    # Đường dẫn người dùng truyền vào có thể là tên tệp, đường dẫn tương đối, đường dẫn tuyệt đối hoặc chứa `../`.
    # Tại đây đường dẫn được phân giải thống nhất thành đường dẫn thực tế (realpath) và dùng commonpath để xác định xem nó có còn nằm trong thư mục được phép hay không.
    # Cách này đáng tin cậy hơn so với việc so khớp chuỗi tiền tố đơn thuần, xử lý được cả liên kết tượng trưng (symlink), dấu phân cách trùng lặp, đường dẫn tương đối,
    # phù hợp cho các thư mục trong danh sách trắng như thư mục tải lên, thư mục tài nguyên, thư mục sản phẩm tác vụ.
    if not unsafe_path:
        raise ValueError("empty path is not allowed")

    base_dir_real = os.path.realpath(base_dir)
    candidate_path = unsafe_path
    if not os.path.isabs(candidate_path):
        candidate_path = os.path.join(base_dir_real, candidate_path)

    resolved_path = os.path.realpath(candidate_path)
    try:
        common_path = os.path.commonpath([base_dir_real, resolved_path])
    except ValueError as exc:
        # Các ký tự ổ đĩa khác nhau trong Windows sẽ kích hoạt ValueError. Những đường dẫn như vậy không được thuộc các thư mục được phép.
        raise ValueError("path is outside the allowed directory") from exc

    if common_path != base_dir_real:
        raise ValueError("path is outside the allowed directory")

    if require_file and not os.path.isfile(resolved_path):
        raise ValueError("file does not exist")

    return resolved_path
