import sys

replacements = [
    ('"""将片段播放速度归一化到 WebUI 支持的安全范围。"""', '"""Chuẩn hóa tốc độ phát của đoạn clip về phạm vi an toàn được WebUI hỗ trợ."""'),
    ('''    # NaN 会绕过普通的大小比较，并在 MoviePy 计算 duration 时传播；无穷值也不
    # 是合法用户输入。两者统一回退默认值，保证 API 和内部直接调用都不会生成
    # 无效时间线。零值和负值同样无法表示正常播放速度。''',
     '''    # NaN sẽ bỏ qua việc so sánh độ lớn thông thường và lan truyền khi MoviePy tính toán duration; giá trị vô cực cũng
    # không phải đầu vào hợp lệ của người dùng. Cả hai đều được fallback về giá trị mặc định, đảm bảo API và lệnh gọi trực tiếp nội bộ đều không sinh ra
    # timeline không hợp lệ. Giá trị 0 và giá trị âm cũng không thể đại diện cho tốc độ phát bình thường.'''),
    ('''    解析当前进程应该使用的 FFmpeg 可执行文件。

    增加原因：
    1. 视频编码、静音音频生成、pydub 音频转码都依赖 FFmpeg；
    2. Windows 便携包、Docker 和用户自定义安装目录经常出现 PATH 不一致；
    3. 集中解析可以让所有调用方使用同一套优先级，减少某条链路能跑、
       另一条链路找不到 FFmpeg 的现场问题。

    优先级：
    1. IMAGEIO_FFMPEG_EXE：MoviePy/imageio 约定的显式配置；
    2. 系统 PATH 中的 ffmpeg；
    3. imageio-ffmpeg 依赖提供的内置二进制；
    4. 字符串 "ffmpeg" 兜底，交给 subprocess 在运行时暴露更具体错误。''',
     '''    Phân giải tệp thực thi FFmpeg mà tiến trình hiện tại nên sử dụng.

    Lý do bổ sung:
    1. Mã hóa video, tạo âm thanh im lặng, chuyển mã âm thanh pydub đều phụ thuộc vào FFmpeg;
    2. Bản portable trên Windows, Docker và thư mục cài đặt tùy chỉnh của người dùng thường xuất hiện PATH không nhất quán;
    3. Phân giải tập trung cho phép tất cả các bên gọi sử dụng cùng một bộ ưu tiên, giảm thiểu sự cố thực tế khi một luồng chạy được
       nhưng luồng khác lại không tìm thấy FFmpeg.

    Độ ưu tiên:
    1. IMAGEIO_FFMPEG_EXE: Cấu hình rõ ràng theo quy ước của MoviePy/imageio;
    2. ffmpeg trong PATH của hệ thống;
    3. Binary tích hợp sẵn do dependency imageio-ffmpeg cung cấp;
    4. Chuỗi "ffmpeg" làm fallback dự phòng, giao cho subprocess bộc lộ lỗi cụ thể hơn lúc chạy.'''),
    ('''    在真正开始生成视频之前提前探测 FFmpeg 是否可用。

    增加原因：
    此前 FFmpeg 缺失/不可用只会在视频合成、静音音轨生成等环节里，以
    ``RuntimeError: No ffmpeg exe could be found`` 或 subprocess 报错的形式
    出现，用户往往要等到任务跑了大半才第一次看到这个报错，且报错本身
    不会指向任何解决办法。这里在共享任务流水线（app/services/task.py 的
    ``_run_pipeline``）里提前做一次探测，尽早给出可操作的英文提示（与项目
    里其他 logger.warning 的用语习惯保持一致），API、CLI、WebUI 都会经过
    这条流水线，因此三条路径能统一生效。

    仅做一次轻量的 ``-version`` 调用，不会触发下载或改变主流程；
    调用方需要把返回值当作硬性前置条件——项目锁定的 imageio-ffmpeg==0.6.0
    并不会在真正使用时自动补下载一个可用的二进制，因此检测失败必须让
    需要 FFmpeg 的阶段直接终止，而不是继续跑到视频合成才失败。''',
     '''    Thực hiện kiểm tra thăm dò trước xem FFmpeg có khả dụng không trước khi thực sự bắt đầu tạo video.

    Lý do bổ sung:
    Trước đây việc FFmpeg bị thiếu/không khả dụng chỉ xuất hiện trong các khâu ghép video, tạo track âm thanh im lặng,... dưới dạng
    ``RuntimeError: No ffmpeg exe could be found`` hoặc lỗi subprocess, người dùng thường phải đợi tác vụ chạy được phần lớn thời gian mới lần đầu thấy lỗi này,
    và bản thân lỗi cũng không chỉ ra bất kỳ cách giải quyết nào. Tại đây trong pipeline tác vụ dùng chung (``_run_pipeline`` của
    app/services/task.py) thăm dò trước một lần, đưa ra gợi ý sớm có thể thao tác (nhất quán với thói quen dùng từ của các logger.warning khác trong dự án).
    API, CLI, WebUI đều đi qua pipeline này nên cả ba nhánh đều có hiệu lực thống nhất.

    Chỉ thực hiện một lệnh gọi nhẹ ``-version``, không kích hoạt tải xuống hay thay đổi quy trình chính;
    Bên gọi cần coi giá trị trả về là điều kiện tiên quyết bắt buộc — imageio-ffmpeg==0.6.0 bị khóa của dự án
    sẽ không tự động tải bù một binary khả dụng khi thực sự dùng, do đó việc phát hiện thất bại bắt buộc phải làm cho
    giai đoạn cần FFmpeg dừng lại ngay lập tức, thay vì tiếp tục chạy đến bước ghép video mới báo lỗi.'''),
    ('''            # 英文数字里的千分位逗号不是断句符，例如 "1,000 years"。
            # Edge TTS 的 word boundary 通常会把这种数字整体作为连续内容返回；
            # 如果这里拆成 "1" 和 "000 years"，后续字幕聚合会无法匹配脚本原文，
            # 进而错误回退到 Whisper。''',
     '''            # Dấu phẩy ngăn cách hàng nghìn trong số tiếng Anh không phải dấu ngắt câu, ví dụ "1,000 years".
            # Boundary từ của Edge TTS thường trả về các con số dạng này dưới dạng nội dung liên tục;
            # Nếu tại đây tách thành "1" và "000 years", việc tổng hợp phụ đề sau đó sẽ không khớp được kịch bản gốc,
            # dẫn đến việc fallback sai lầm sang Whisper.'''),
    ('''# 匹配所有包含停顿关键词的标签（方括号或圆括号），无论其参数合法与否均匹配，
# 以确保非法标签（如 [pause: -2s]、[pause: nope]、[pause: 0s]）在合成前被彻底清除而不会泄漏给 TTS
# 停顿时长安全阈值（单位：秒）：
# 最小有效停顿为 0.1 秒（100ms），小于此值的请求会被校验并修正为 0.1s；
# 小于等于 0 秒或非法非数字的停顿标签会被判定为无效标签并直接移除，不朗读也不生成静音；
# 最大停顿上限为 10.0 秒，超过部分会被安全截断。''',
     '''# Khớp tất cả các thẻ chứa từ khóa tạm dừng (ngoặc vuông hoặc ngoặc tròn), bất kể tham số có hợp lệ hay không,
# để đảm bảo các thẻ không hợp lệ (như [pause: -2s], [pause: nope], [pause: 0s]) được xóa hoàn toàn trước khi tổng hợp và không bị rò rỉ cho TTS
# Ngưỡng an toàn thời lượng tạm dừng (đơn vị: giây):
# Tạm dừng hợp lệ tối thiểu là 0.1 giây (100ms), yêu cầu nhỏ hơn giá trị này sẽ được xác thực và sửa thành 0.1s;
# Thẻ tạm dừng nhỏ hơn hoặc bằng 0 giây hoặc không phải dạng số sẽ bị coi là thẻ không hợp lệ và bị loại bỏ trực tiếp, không đọc và không tạo khoảng lặng;
# Giới hạn tạm dừng tối đa là 10.0 giây, phần vượt quá sẽ được cắt ngắn an toàn.'''),
    ('"""检查文本中是否包含停顿/暂停标签。"""', '"""Kiểm tra xem văn bản có chứa thẻ tạm dừng/pause hay không."""'),
    ('''    移除脚本文本中的所有停顿/暂停标签（包括有效与无效标签）。

    在字幕分句、LLM关键词提取或作为发音文本传递给 TTS 时，必须将此类非发音标记清除，
    避免非法或未处理的标签被朗读或作为视觉搜索词。''',
     '''    Loại bỏ tất cả các thẻ tạm dừng/pause trong văn bản kịch bản (bao gồm cả thẻ hợp lệ và không hợp lệ).

    Khi tách câu phụ đề, trích xuất từ khóa LLM hoặc truyền văn bản phát âm cho TTS, bắt buộc phải xóa các dấu hiệu không phát âm này,
    tránh để các thẻ không hợp lệ hoặc chưa xử lý bị đọc to hoặc dùng làm từ khóa tìm kiếm hình ảnh.'''),
    ('    # 合并连续水平空格，保留换行', '    # Hợp nhất các khoảng trắng ngang liên tiếp, giữ lại dấu xuống dòng'),
    ('''    解析脚本中的文本与停顿标签。

    连续停顿标签会自动合并为一个停顿段；
    非法标签（如非数字参数、小于等于 0 的时长）会被直接移除并忽略，绝不会作为台词传给 TTS；
    小于 MIN_PAUSE_DURATION_SECONDS (0.1s/100ms) 的过小停顿会被校验并提升至 0.1s；
    超过 MAX_PAUSE_DURATION_SECONDS (10.0s) 的过长停顿会被限制在安全上限内。

    Returns:
        有序元组列表，形式为 [("speech", "文案"), ("pause", 2.0), ...]''',
     '''    Phân tích cú pháp văn bản và các thẻ tạm dừng trong kịch bản.

    Các thẻ tạm dừng liên tiếp sẽ tự động được hợp nhất thành một đoạn tạm dừng;
    Các thẻ không hợp lệ (như tham số không phải số, thời lượng nhỏ hơn hoặc bằng 0) sẽ bị loại bỏ trực tiếp và bỏ qua, tuyệt đối không truyền làm lời thoại cho TTS;
    Tạm dừng quá nhỏ dưới MIN_PAUSE_DURATION_SECONDS (0.1s/100ms) sẽ được kiểm tra và nâng lên 0.1s;
    Tạm dừng quá dài vượt quá MAX_PAUSE_DURATION_SECONDS (10.0s) sẽ được giới hạn trong ngưỡng an toàn tối đa.

    Returns:
        Danh sách tuple có thứ tự, có dạng [("speech", "kịch bản"), ("pause", 2.0), ...]'''),
    ('            # 未指定参数时，默认停顿 1.0 秒', '            # Khi không chỉ định tham số, mặc định tạm dừng 1.0 giây'),
    ('        # 连续出现的停顿标签合并为一个停顿段，避免生成碎片化静音文件', '        # Hợp nhất các thẻ tạm dừng xuất hiện liên tiếp thành một đoạn tạm dừng, tránh tạo ra các tệp im lặng phân mảnh'),
    ('''    清理字幕匹配前的脚本文本。

    用户可能手动输入 Markdown 分隔符、标题强调或 `_` 这类格式符号。
    这些字符通常不会出现在 TTS/Whisper 的识别结果里；如果继续参与
    字幕逐行匹配，脚本行数量会大于真实字幕行数量，最终可能补出
    `00:00:00,000 --> 00:00:00,000`，导致剪辑软件无法导入 SRT。''',
     '''    Làm sạch văn bản kịch bản trước khi so khớp phụ đề.

    Người dùng có thể nhập thủ công các dấu phân cách Markdown, nhấn mạnh tiêu đề hoặc các ký hiệu định dạng như `_`.
    Các ký tự này thường không xuất hiện trong kết quả nhận diện của TTS/Whisper; nếu tiếp tục tham gia
    so khớp từng dòng phụ đề, số dòng kịch bản sẽ nhiều hơn số dòng phụ đề thực tế, cuối cùng có thể bù ra
    `00:00:00,000 --> 00:00:00,000`, khiến phần mềm dựng phim không thể import được file SRT.'''),
    ('''        # Markdown 分隔符或强调符号单独成行时不会被 TTS 朗读，必须从
        # 脚本行里移除，避免字幕聚合卡在这类“不可发声”的目标行上。''',
     '''        # Dấu phân cách Markdown hoặc ký hiệu nhấn mạnh khi đứng riêng một dòng sẽ không được TTS đọc, bắt buộc phải xóa
        # khỏi dòng kịch bản, tránh việc tổng hợp phụ đề bị kẹt ở những dòng mục tiêu "không thể phát âm" này.'''),
    ('''    按“已保存设置、浏览器语言、默认语言”的优先级选择界面语言。

    浏览器通常返回带地区的 locale，例如 ``zh-CN``、``pt-BR``。语言文件使用
    ``zh``、``pt`` 这类基础代码，因此先尝试完整匹配，再回退到连字符前的语言
    代码。函数保持纯逻辑，避免把浏览器上下文和配置写入耦合到工具层，便于测试。''',
     '''    Chọn ngôn ngữ giao diện theo thứ tự ưu tiên "Cài đặt đã lưu, ngôn ngữ trình duyệt, ngôn ngữ mặc định".

    Trình duyệt thường trả về locale có mã vùng, ví dụ ``zh-CN``, ``pt-BR``. Tệp ngôn ngữ sử dụng
    các mã cơ bản như ``zh``, ``pt``, do đó trước tiên thử so khớp hoàn toàn, sau đó mới fallback về mã ngôn ngữ
    trước dấu gạch nối. Hàm duy trì logic thuần túy, tránh ghép ngữ cảnh trình duyệt và ghi cấu hình vào tầng tiện ích, thuận tiện cho việc kiểm thử.'''),
    ('''    # 正常项目始终包含英文；保留空语言集合兜底，避免损坏的语言目录让页面
    # 初始化直接抛异常，后续翻译函数会继续显示原始 key 以便诊断。''',
     '''    # Dự án bình thường luôn chứa tiếng Anh; giữ lại tập hợp ngôn ngữ rỗng làm dự phòng fallback, tránh việc thư mục ngôn ngữ bị hỏng khiến việc khởi tạo
    # trang ném ngoại lệ trực tiếp, hàm dịch sau đó sẽ tiếp tục hiển thị key ban đầu để tiện chẩn đoán.'''),
    ('''    # WebUI 每次交互都会触发 Streamlit 重新执行脚本，语言文件运行期不会变化，
    # 因此缓存解析结果，避免反复读取和解析所有 i18n JSON 文件。''',
     '''    # Mỗi tương tác trên WebUI đều kích hoạt Streamlit thực thi lại script, tệp ngôn ngữ không thay đổi trong lúc chạy,
    # do đó lưu cache kết quả phân tích cú pháp, tránh việc đọc và phân tích lặp đi lặp lại tất cả các tệp i18n JSON.''')
]

path = 'app/utils/utils.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

for zh, vi in replacements:
    content = content.replace(zh.replace('\r\n', '\n'), vi)
    content = content.replace(zh.replace('\n', '\r\n'), vi.replace('\n', '\r\n'))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("utils.py comments translated successfully!")
