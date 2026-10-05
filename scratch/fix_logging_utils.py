import sys

pairs_logging = [
    ('''# Loguru 启动时默认终端 handler 的 ID 为 0。WebUI 重新加载时只能替换这个
# 基础终端输出，不能调用 logger.remove() 清空全部 handler，否则正在运行任务
# 用于收集 WebUI 日志的临时 sink 也会被删除。''',
     '''# Khi Loguru khởi động, handler terminal mặc định có ID là 0. Khi WebUI tải lại chỉ được thay thế
# đầu ra terminal cơ bản này, không được gọi logger.remove() để xóa toàn bộ handler, nếu không các sink
# tạm thời thu thập nhật ký WebUI của các tác vụ đang chạy cũng sẽ bị xóa bỏ.'''),

    ('''    把绝对路径缩短为 ``./`` 开头、始终使用正斜杠的项目相对路径。

    Windows 上项目可能通过映射网络盘或 ``subst`` 盘启动。此时调用栈里的路径
    仍是 ``X:\\MoneyPrinterTurbo\\...``，而 ``PROJECT_ROOT`` 经 ``realpath``
    解析后落在 ``C:\\...``，``os.path.relpath`` 会直接抛出 ``ValueError``。
    格式化函数抛错会被 loguru 捕获并丢弃整条记录，终端和 WebUI 日志面板会
    同时变空，因此这里必须兜底返回原始路径。项目目录之外的文件同理：把
    ``./`` 拼到 ``..`` 回溯路径上只会得到更难读的结果。''',
     '''    Rút ngắn đường dẫn tuyệt đối thành đường dẫn tương đối của dự án bắt đầu bằng ``./`` và luôn dùng dấu gạch chéo thuận.

    Trên Windows, dự án có thể khởi động qua ổ đĩa mạng ánh xạ hoặc ổ đĩa ``subst``. Khi đó đường dẫn trong ngăn xếp gọi
    vẫn là ``X:\\MoneyPrinterTurbo\\...``, trong khi ``PROJECT_ROOT`` qua ``realpath``
    phân giải lại thành ``C:\\...``, khiến ``os.path.relpath`` ném thẳng ``ValueError``.
    Nếu hàm định dạng ném lỗi, loguru sẽ bắt lấy và hủy bỏ toàn bộ bản ghi, bảng nhật ký trên terminal và WebUI sẽ
    đồng thời bị trống, vì vậy ở đây bắt buộc phải fallback trả về đường dẫn gốc. Tương tự với các tệp ngoài thư mục dự án:
    việc ghép ``./`` vào đường dẫn truy vết ngược ``..`` sẽ chỉ tạo ra kết quả khó đọc hơn.'''),

    ('''    # Windows 的 relpath 返回反斜杠分隔的路径，直接拼接会得到 ``./app\\utils``
    # 这种混合分隔符的输出，与其它平台的日志不一致。''',
     '''    # Hàm relpath của Windows trả về đường dẫn ngăn cách bằng dấu gạch chéo ngược, nếu ghép trực tiếp sẽ thành ``./app\\utils``
    # kiểu phân cách hỗn hợp này không nhất quán với nhật ký trên các nền tảng khác.'''),

    ('''    统一格式化终端与 WebUI 日志。

    Loguru 会把同一条记录交给多个 sink。第一个 sink 可能已经将绝对路径转换
    为项目相对路径，因此这里同时兼容绝对路径和 ``./`` 开头的已格式化路径。
    WebUI sink 会关闭颜色，但时间、级别、调用位置和消息内容与终端保持一致。''',
     '''    Định dạng thống nhất nhật ký terminal và WebUI.

    Loguru sẽ chuyển cùng một bản ghi cho nhiều sink. Sink đầu tiên có thể đã chuyển đổi đường dẫn tuyệt đối
    thành đường dẫn tương đối của dự án, vì vậy tại đây tương thích đồng thời cả đường dẫn tuyệt đối và đường dẫn đã định dạng bắt đầu bằng ``./``.
    Sink WebUI sẽ tắt màu sắc, nhưng thời gian, cấp độ, vị trí gọi và nội dung thông báo vẫn giữ nhất quán với terminal.'''),

    ('''    # 日志消息有时会包含任务文件的绝对路径。统一缩短为项目相对路径，可以
    # 避免 WebUI 和终端因初始化入口不同而展示两套内容。''',
     '''    # Thông điệp nhật ký đôi khi chứa đường dẫn tuyệt đối của tệp tác vụ. Việc rút ngắn thống nhất thành đường dẫn tương đối của dự án có thể
    # tránh việc WebUI và terminal hiển thị hai nội dung khác nhau do điểm khởi tạo khác nhau.'''),

    ('''    安全替换进程级终端日志 handler，并保留任务专用 handler。

    Streamlit 在代码热重载或缓存失效时可能重新执行日志初始化。这里只按已记录
    的 handler ID 精确移除旧终端输出，因此不会中断后台任务正在写入的 WebUI
    日志。锁用于保护多个浏览器会话同时初始化时的 ID 更新。''',
     '''    Thay thế an toàn handler nhật ký terminal ở cấp tiến trình, đồng thời giữ lại handler dành riêng cho tác vụ.

    Streamlit khi hot reload mã nguồn hoặc hết hạn cache có thể thực thi lại việc khởi tạo nhật ký. Tại đây chỉ xóa chính xác
    đầu ra terminal cũ theo handler ID đã ghi nhận, do đó sẽ không làm gián đoạn nhật ký WebUI đang được ghi bởi các tác vụ nền.
    Khóa (lock) dùng để bảo vệ việc cập nhật ID khi nhiều phiên trình duyệt khởi tạo đồng thời.'''),

    ('''                # 测试或外部入口可能已经移除该 handler。继续创建新的终端输出，
                # 不需要影响其它仍有效的日志 sink。''',
     '''                # Kiểm thử hoặc điểm vào bên ngoài có thể đã gỡ bỏ handler này. Tiếp tục tạo đầu ra terminal mới
                # mà không làm ảnh hưởng đến các sink nhật ký khác vẫn đang hoạt động.''')
]

path = 'app/utils/logging_utils.py'
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

for zh, vi in pairs_logging:
    c = c.replace(zh.replace('\r\n', '\n'), vi)
    c = c.replace(zh.replace('\n', '\r\n'), vi.replace('\n', '\r\n'))

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

print("logging_utils.py updated successfully!")
