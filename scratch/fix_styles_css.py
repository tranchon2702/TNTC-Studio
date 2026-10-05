import sys

replacements = [
    ('''/* Streamlit 默认让标签中的帮助区域占据剩余宽度，导致问号按钮贴到控件最右侧。
   帮助提示在语义上从属于标签文字，因此取消该区域的伸展，并用固定小间距让
   问号紧跟标签；没有 help 参数的标签不存在这个 div，不会受到影响。 */''',
     '''/* Streamlit mặc định để vùng trợ giúp trong nhãn chiếm toàn bộ chiều rộng còn lại, khiến nút dấu hỏi dính sát sang bên phải.
   Gợi ý trợ giúp về mặt ngữ nghĩa thuộc về văn bản nhãn, do đó hủy bỏ phần mở rộng này và dùng khoảng cách nhỏ cố định để
   dấu hỏi đi liền sau nhãn; nhãn không có tham số help sẽ không có div này và không bị ảnh hưởng. */'''),

    ('''/* 视频主题与大模型存在直接依赖，把配置入口放在字段标签旁。这里将 tertiary
   button 收敛为普通文字链接，避免它与下方“生成文案”主操作争夺视觉层级；
   text_area 仍保留折叠 label，因此屏幕阅读器可以继续读取字段名称。 */''',
     '''/* Chủ đề video phụ thuộc trực tiếp vào mô hình ngôn ngữ lớn (LLM), đặt lối vào cấu hình cạnh nhãn trường. Tại đây thu gọn tertiary
   button thành liên kết văn bản thông thường, tránh tranh chấp phân cấp thị giác với thao tác chính "Tạo kịch bản" bên dưới;
   text_area vẫn giữ nhãn thu gọn để trình đọc màn hình có thể tiếp tục đọc tên trường. */'''),

    ('''/* 自定义标签行和 text_area 是同一字段的两个子元素。覆盖 Streamlit 容器默认
   的纵向大间距，使它与原生“标签 + 输入框”控件保持相同的视觉节奏。 */''',
     '''/* Hàng nhãn tùy chỉnh và text_area là hai phần tử con của cùng một trường. Ghi đè khoảng cách dọc mặc định
   của container Streamlit để giữ cùng nhịp điệu thị giác với widget "nhãn + ô nhập liệu" gốc. */'''),

    ('''/* 配音方式位于四列布局中的窄面板。Streamlit 的 stretch 模式默认让单个分段
   按钮接近半行宽，三个选项会换成两行；这里仅对该控件改为三等分，并压缩
   水平内边距，保持其它 segmented control 的原生尺寸不变。 */''',
     '''/* Phương thức lồng tiếng nằm trong bảng hẹp của bố cục 4 cột. Chế độ stretch của Streamlit mặc định để một nút
   phân đoạn chiếm gần nửa hàng, 3 tùy chọn sẽ bị ngắt thành 2 hàng; tại đây chia đều widget này thành 3 phần và nén
   padding ngang, giữ nguyên kích thước gốc của các segmented control khác. */'''),

    ('''/* 顶部品牌区将项目名和弱化版本号放在同一基线。项目名称仍是页面唯一的 h1；
   版本号本身作为项目链接，避免增加独立图标而干扰标题的视觉层级。 */''',
     '''/* Khu vực thương hiệu trên cùng đặt tên dự án và số phiên bản làm mờ trên cùng đường cơ sở. Tên dự án vẫn là h1 duy nhất của trang;
   bản thân số phiên bản đóng vai trò là liên kết dự án, tránh thêm biểu tượng độc lập làm rối phân cấp thị giác của tiêu đề. */'''),

    ('''/* Streamlit 会在自定义 h1 内增加一层文本容器，实际的行内布局需要作用在
   这层容器上，才能稳定控制项目名和版本号的间距。 */''',
     '''/* Streamlit sẽ thêm một lớp container văn bản bên trong h1 tùy chỉnh, bố cục nội tuyến thực tế cần áp dụng lên
   lớp container này để kiểm soát ổn định khoảng cách giữa tên dự án và số phiên bản. */'''),

    ('''/* 新版本提醒只在确有正式版本更新时出现。使用文字链接而不是按钮或横幅，
   让它保持可发现性，同时不抢占项目标题和主要操作入口的视觉层级。 */''',
     '''/* Thông báo phiên bản mới chỉ xuất hiện khi thực sự có bản cập nhật chính thức. Sử dụng liên kết văn bản thay vì nút bấm hay banner
   giúp nó duy trì tính dễ phát hiện, đồng thời không lấn át phân cấp thị giác của tiêu đề dự án và các thao tác chính. */'''),

    ('''/* Streamlit 会自动给 h1 追加一个标题锚点。品牌区已有版本号项目链接，额外的
   标题锚点没有实际价值，因此只在这个标题内隐藏，避免窄屏出现多余图标。 */''',
     '''/* Streamlit sẽ tự động thêm anchor tiêu đề cho h1. Khu vực thương hiệu đã có liên kết dự án phiên bản, anchor tiêu đề bổ sung
   không có giá trị thực tế nên chỉ ẩn trong tiêu đề này, tránh xuất hiện biểu tượng thừa trên màn hình hẹp. */'''),

    ('''/* 顶部任务入口由 st.fragment 定时刷新，Streamlit 会为 Fragment 自动生成一个
   占满整行的 LayoutWrapper。这里只收缩该包装层，保证右侧操作区按内容紧凑排列。 */''',
     '''/* Lối vào tác vụ trên cùng được st.fragment làm mới định kỳ, Streamlit sẽ tự động tạo một LayoutWrapper chiếm toàn bộ hàng
   cho Fragment. Tại đây chỉ thu gọn lớp bọc này để đảm bảo khu vực thao tác bên phải sắp xếp gọn theo nội dung. */'''),

    ('''/* 高级设置属于内容展开入口，不是执行操作。保留 Streamlit 原生 expander 的
   语义和键盘交互，只在带 advanced_settings_ key 的局部容器内移除卡片边框，
   避免与紧邻的 AI 生成按钮形成两个连续大按钮的视觉干扰。 */''',
     '''/* Cài đặt nâng cao là lối vào mở rộng nội dung, không phải thao tác thực thi. Giữ lại ngữ nghĩa và tương tác bàn phím
   của expander gốc trong Streamlit, chỉ xóa viền thẻ trong container cục bộ có key advanced_settings_,
   tránh gây nhiễu thị giác khi tạo thành hai nút lớn liên tiếp cạnh nút tạo AI. */'''),

    ('''/* 展开后让标题和内容共享同一背景，视觉上形成连续区域；折叠状态仍保持原有的
   透明轻量入口，不额外占用页面层级。 */''',
     '''/* Sau khi mở rộng, tiêu đề và nội dung chia sẻ cùng một nền, tạo thành vùng liên tục về mặt thị giác; trạng thái thu gọn vẫn giữ
   lối vào trong suốt gọn nhẹ ban đầu, không chiếm thêm phân cấp của trang. */'''),

    ('''/* 键盘操作使用标题下划线作为焦点反馈，不再绘制任何外框，确保展开区域在视觉
   上完全无边框，同时仍能让键盘用户识别当前焦点。 */''',
     '''/* Thao tác bàn phím sử dụng gạch chân tiêu đề làm phản hồi tiêu điểm, không vẽ thêm khung viền nào, đảm bảo vùng mở rộng
   hoàn toàn không có viền về mặt thị giác, đồng thời người dùng bàn phím vẫn nhận diện được tiêu điểm hiện tại. */'''),

    ('''/* 内容区与展开标题无缝衔接，不使用任何边框，仅通过统一背景和内边距表达
   控件从属于高级设置，避免出现嵌套卡片或割裂的两段式结构。 */''',
     '''/* Vùng nội dung kết nối liền mạch với tiêu đề mở rộng, không dùng bất kỳ đường viền nào, chỉ thông qua nền và padding thống nhất
   để thể hiện các control thuộc về cài đặt nâng cao, tránh xuất hiện thẻ lồng nhau hoặc cấu trúc hai đoạn rời rạc. */'''),

    ('''/* 任务管理 popover 需要固定宽度，避免内容从空状态、列表状态切换时
   反复改变宽度造成页面抖动。宽度仍保留视口约束，兼容窄屏。 */''',
     '''/* Popover quản lý tác vụ cần chiều rộng cố định để tránh nội dung thay đổi độ rộng liên tục khi chuyển đổi giữa trạng thái rỗng
   và danh sách gây rung lắc trang. Chiều rộng vẫn giữ ràng buộc khung nhìn (viewport), tương thích với màn hình hẹp. */'''),

    ('''/* 任务管理列表保留 Streamlit 原生布局，只做轻量压缩，避免每一行过高。
   通过 task_row_ key 限定作用范围，避免影响其它配置面板的 container。 */''',
     '''/* Danh sách quản lý tác vụ giữ lại bố cục gốc của Streamlit, chỉ nén nhẹ để tránh mỗi hàng quá cao.
   Giới hạn phạm vi tác dụng thông qua key task_row_, tránh làm ảnh hưởng đến container của các bảng cấu hình khác. */'''),

    ('''/* 按钮文字视觉隐藏后，Streamlit 仍会保留图标与文字之间的默认间距，导致
   单独图标向左偏移。清除该间距并让内容层占满按钮，确保操作图标严格居中。 */''',
     '''/* Sau khi ẩn chữ của nút về mặt thị giác, Streamlit vẫn giữ khoảng cách mặc định giữa biểu tượng và chữ, dẫn đến
   biểu tượng đơn lẻ bị lệch sang trái. Xóa khoảng cách này và để lớp nội dung chiếm trọn nút, đảm bảo biểu tượng thao tác căn giữa tuyệt đối. */'''),

    ('''/* 行内操作保留可访问的按钮名称，但视觉上只显示图标。使用屏幕阅读器可见的
   隐藏方式，而不是 display:none，确保每个操作仍能被正确朗读。 */''',
     '''/* Thao tác trên hàng giữ lại tên nút hỗ trợ tiếp cận (a11y), nhưng về mặt thị giác chỉ hiển thị biểu tượng. Sử dụng phương thức ẩn
   mà trình đọc màn hình vẫn thấy được thay vì display:none, đảm bảo mỗi thao tác vẫn được đọc chính xác. */'''),

    ('''/* 中等宽度下将四个设置面板调整为 2×2。主内容区还会扣除页面边距，因此
   1200～1440px 的常见视口不足以稳定容纳四列英文标签；断点覆盖到 1500px，
   避免下拉内容和标题被裁切，真正的宽屏才恢复四列。 */''',
     '''/* Ở độ rộng trung bình, điều chỉnh 4 bảng cài đặt thành 2x2. Vùng nội dung chính còn trừ đi margin trang, do đó
   khung nhìn thông thường 1200~1440px không đủ để chứa ổn định 4 cột nhãn; breakpoint mở rộng đến 1500px,
   tránh nội dung dropdown và tiêu đề bị cắt gọt, màn hình thực sự rộng mới khôi phục 4 cột. */'''),

    ('''/* 移动端隐藏桌面表头，并将任务行重排为“主题 + 元信息 + 操作”紧凑结构。 */''',
     '''/* Giao diện di động ẩn tiêu đề bảng desktop và sắp xếp lại hàng tác vụ thành cấu trúc nhỏ gọn "Chủ đề + Siêu dữ liệu + Thao tác". */''')
]

path = 'webui/styles.css'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

for zh, vi in replacements:
    content = content.replace(zh.replace('\r\n', '\n'), vi)
    content = content.replace(zh.replace('\n', '\r\n'), vi.replace('\n', '\r\n'))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("styles.css completely translated!")
