import sys, re

pairs = [
    ('环境变量中的空格和尾随逗号不应破坏合法来源匹配。', 'Khoảng trắng và dấu phẩy ở cuối trong biến môi trường không được làm hỏng việc so khớp nguồn gốc hợp lệ.'),
    ('未配置白名单时，第三方网页不能读取响应或通过预检。', 'Khi chưa cấu hình danh sách trắng, trang web bên thứ ba không thể đọc phản hồi hoặc vượt qua preflight.'),
    ('同源浏览器和不发送 Origin 的服务端客户端必须继续正常访问。', 'Trình duyệt cùng nguồn gốc và client máy chủ không gửi Origin phải tiếp tục truy cập bình thường.'),
    ('独立网页前端显式配置后可以访问，其他来源仍必须被拒绝。', 'Frontend web độc lập sau khi cấu hình rõ ràng có thể truy cập, các nguồn gốc khác vẫn phải bị từ chối.'),
    ('精确白名单应支持远程网页访问本机或局域网 API 的额外预检。', 'Danh sách trắng chính xác phải hỗ trợ preflight bổ sung khi trang web từ xa truy cập API máy cục bộ hoặc mạng LAN.'),
    ('显式通配符保留兼容能力，但不得再次形成反射 Origin 的组合。', 'Ký tự đại diện rõ ràng giữ lại khả năng tương thích, nhưng không được tạo lại tổ hợp Origin phản xạ.'),
    ('无需预检的 multipart 请求也必须在进入上传处理函数前返回 403。', 'Yêu cầu multipart không cần preflight cũng phải trả về 403 trước khi vào hàm xử lý tải lên.'),
]

path = 'test/services/test_asgi_cors.py'
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

for zh, vi in pairs:
    c = c.replace(zh, vi)

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)

print("test_asgi_cors.py updated successfully!")
