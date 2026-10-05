import sys

pairs_bgm = [
    ('"""通用开关必须覆盖当前来源和未来提供商，不能再写提供商特判。"""', '"""Công tắc chung phải bao quát các nguồn hiện tại và nhà cung cấp tương lai, không được viết phán đoán riêng lẻ theo từng nhà cung cấp nữa."""'),
    ('WebUI 与 API 共用同一格式白名单；这里覆盖大小写和常见容器，避免未来\n        # 修改上传控件时只支持界面展示、服务层却拒绝文件。', 'WebUI và API dùng chung một danh sách trắng định dạng; tại đây bao quát cả chữ hoa/thường và các container phổ biến, tránh việc sau này\n        # sửa widget tải lên thì chỉ hỗ trợ hiển thị giao diện nhưng tầng dịch vụ lại từ chối tệp.'),
    ('服务会把文件指针恢复到开头，同一个 UploadedFile 仍可供 Streamlit\n            # 试听或后续 rerun 使用，不会因为保存操作变成空文件。', 'Dịch vụ sẽ khôi phục con trỏ tệp về đầu, cùng một UploadedFile vẫn có thể dùng cho Streamlit\n            # nghe thử hoặc rerun tiếp theo, không bị biến thành tệp rỗng do thao tác lưu.'),
    ('WebUI 预检只允许短暂使用临时文件，用户尚未点击生成时不能把文件\n            # 留在持久化 BGM 目录，也不能改变后续试听所需的文件指针。', 'Kiểm tra trước của WebUI chỉ cho phép dùng tạm tệp thời gian ngắn, khi người dùng chưa nhấn tạo thì không được để tệp\n            # lại trong thư mục BGM lâu dài, cũng không được làm thay đổi con trỏ tệp cần cho việc nghe thử sau đó.'),
    ('失败的预检同样不能在持久化目录留下临时音频，避免随后被随机 BGM\n            # 枚举逻辑选中，也避免长期运行时逐步堆积无效文件。', 'Kiểm tra trước thất bại cũng không được để lại âm thanh tạm thời trong thư mục lưu trữ lâu dài, tránh việc sau đó bị logic liệt kê\n            # BGM ngẫu nhiên chọn trúng, cũng như tránh tích tụ dần các tệp không hợp lệ khi chạy lâu dài.'),
    ('校验失败发生在原子替换之前，已有用户文件不能被损坏。', 'Xác thực thất bại diễn ra trước khi thay thế nguyên tử, các tệp người dùng hiện có không được phép bị hư hỏng.'),
]

path = 'test/services/test_bgm.py'
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

for zh, vi in pairs_bgm:
    c = c.replace(zh, vi)

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

print("test_bgm.py updated successfully!")
