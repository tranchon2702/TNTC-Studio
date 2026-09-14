# VietCreator Studio

VietCreator Studio là bản cá nhân hóa từ MoneyPrinterTurbo để tạo video ngắn
tiếng Việt cho YouTube Shorts và TikTok. Dự án giữ nguyên thông báo giấy phép MIT
của mã nguồn nền và mặc định không tự đăng công khai.

## Chạy ứng dụng

Xem [ghi chú kiểm tra an toàn](SECURITY-NOTES-VI.md) trước khi thêm model/plugin
hoặc đưa ứng dụng lên VPS.

1. Chạy `check-vietcreator.ps1` để kiểm tra máy.
2. Nhấp đúp `start-vietcreator.bat`.
3. Trình duyệt mở `http://127.0.0.1:8501`.

Máy đã có `uv` và FFmpeg thì lần đầu chỉ cần chờ môi trường Python được cài.

## Tạo video đầu tiên không cần API key

1. Chuẩn bị 3-6 ảnh hoặc video dọc mà bạn sở hữu quyền sử dụng.
2. Trong **Cài đặt kịch bản**, nhập chủ đề và dán kịch bản tiếng Việt hoàn chỉnh.
3. Trong **Cài đặt video**, giữ nguồn **Tệp cục bộ** rồi tải vật liệu lên.
4. Giữ giọng `vi-VN-HoaiMyNeural` để thử nhanh. Giọng này dùng Edge TTS, cần
   Internet nhưng không cần API key.
5. Xem lại phụ đề, nguồn hình, âm lượng và nhấn tạo video.
6. Thành phẩm nằm trong `storage/tasks/<task-id>/final-1.mp4`.

Mặc định hiện tại là video dọc 9:16, ghép theo thứ tự, font Be Vietnam Pro,
phụ đề nền bo góc, không có nhạc nền và xuất bằng NVIDIA NVENC. Nếu máy không hỗ
trợ NVENC ở thời điểm chạy, pipeline tự quay về bộ mã hóa CPU.

## Dùng AI viết kịch bản trên máy

Ollama là thành phần tùy chọn và chưa được cài tự động. Sau khi cài Ollama từ
trang chính thức, chạy:

```powershell
ollama pull qwen2.5:3b
```

Cấu hình mặc định đã chọn provider `ollama` và model `qwen2.5:3b`. Với RTX 3050
4 GB, chỉ nên chạy một công đoạn AI nặng tại một thời điểm.

## Sinh ảnh trên GPU

Lát đầu tiên dùng ảnh/video cục bộ để có quy trình ổn định ngay. Upstream đã có
nguồn ảnh qua API tương thích OpenAI, nhưng ComfyUI nguyên bản không cung cấp trực
tiếp endpoint đó. Cần thêm adapter ComfyUI riêng và workflow Stable Diffusion 1.5
ở lát tiếp theo; không nên dùng SDXL hoặc model video lớn làm mặc định trên 4 GB
VRAM.

## Xuất bản và kiếm tiền

- Tự đăng đang tắt; quyền riêng tư YouTube mặc định là `private`.
- Luôn kiểm tra video trước khi chuyển sang `public`.
- Chỉ dùng hình, video, nhạc, font, model và giọng có quyền thương mại phù hợp.
- Không clone giọng hoặc khuôn mặt người khác khi chưa có sự cho phép.
- Mỗi video cần kịch bản, nhận định và cách dựng riêng; tránh sản xuất hàng loạt
  các video gần như giống nhau.
- Gắn nhãn nội dung AI khi nền tảng yêu cầu, nhất là hình/giọng chân thực.

## Các lớp chính

- `webui/Main.py`: giao diện và cấu hình tác vụ.
- `app/services/task.py`: điều phối toàn bộ pipeline.
- `app/services/llm.py`: viết kịch bản và sinh từ khóa.
- `app/services/voice.py`: giọng đọc và timestamp phụ đề.
- `app/services/material.py`: nguồn ảnh/video.
- `app/services/video.py`: ghép hình, âm thanh, phụ đề và xuất MP4.
- `config.toml`: cấu hình riêng có thể chứa khóa; file này bị Git bỏ qua.

## Trạng thái hiện tại

- Đã tạo project riêng và cài dependency khóa theo upstream.
- Đã Việt hóa giao diện chính và các hướng dẫn local quan trọng.
- Đã đặt preset an toàn cho video dọc tiếng Việt và RTX 3050 4 GB.
- Chưa cài Ollama, ComfyUI hoặc model vì đây là phần mềm/model cấp hệ thống có
  dung lượng lớn và cần người dùng chọn rõ trước khi tải.
- Chưa cấu hình OAuth YouTube hoặc kết nối TikTok.
