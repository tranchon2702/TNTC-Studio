# Ghi chú kiểm tra an toàn

Lần kiểm tra gần nhất: **2026-09-11**.

## Phạm vi đã kiểm tra

- Mã nguồn được lấy trực tiếp từ `https://github.com/harry0703/MoneyPrinterTurbo.git`.
- Commit local và `HEAD` của remote tại thời điểm kiểm tra cùng là
  `436b0e9cc830ef7639e33388917e389cf30d3307`.
- Microsoft Defender đang bật bảo vệ thời gian thực và có chữ ký cập nhật trong ngày.
- Defender Custom Scan toàn bộ `D:\Vietnamese-AI-Video-Studio`: không phát hiện
  mối đe dọa.
- Rà tĩnh các mẫu thực thi chuỗi lệnh, tải rồi chạy, Base64, shell và deserialize
  nguy hiểm. Script kiểm tra do bản cá nhân thêm đã được đổi sang gọi chương trình
  trực tiếp, không còn dùng `Invoke-Expression`.

Không công cụ nào có thể bảo đảm tuyệt đối một project là an toàn. Kết luận hiện
tại là **không có bằng chứng về virus/mã độc trong bản đã quét**, không phải bảo
đảm cho mọi commit hoặc model/plugin tải thêm trong tương lai.

## Cảnh báo dependency

`pip-audit` tìm thấy 59 advisory duy nhất trong 8 package của môi trường upstream:

- `aiohttp 3.14.0`
- `click 8.1.8`
- `cryptography 49.0.0`
- `gitpython 3.1.50`
- `pillow 11.3.0`
- `python-multipart 0.0.27`
- `setuptools 82.0.1`
- `starlette 1.2.1`

Đây là lỗ hổng đã biết trong thư viện, không phải kết quả phát hiện virus. Không
nâng phiên bản hàng loạt trước khi chạy lại toàn bộ test vì một số package là phụ
thuộc gián tiếp của Streamlit, FastAPI, Edge TTS, MoviePy và Faster Whisper.

## Cách chạy hiện tại

- Chỉ lắng nghe tại `127.0.0.1`; không mở cổng ra Internet.
- Chạy bằng `start-vietcreator.bat`, không chạy script lạ tải từ bình luận hoặc
  video hướng dẫn.
- Tự đăng video đang tắt và YouTube mặc định là `private`.
- Chỉ tải checkpoint/model từ nguồn chính thức; model và custom node là mã/dữ
  liệu bên thứ ba và phải quét lại sau khi thêm.

## Trước khi đưa lên VPS

Không public bản hiện tại trực tiếp. Cần nâng và kiểm thử dependency, đặt API key
bảo vệ ứng dụng, dùng HTTPS/reverse proxy, firewall, tài khoản riêng, giới hạn file
upload và không chạy container bằng quyền quản trị.
