<div align="center">

# 🎬 TNTC Studio

### Hệ Thống Tự Động Hóa Sản Xuất Video Ngắn Cho YouTube Shorts & TikTok

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![AI-Model](https://img.shields.io/badge/Gemini-3.8_Flash_High-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://aistudio.google.com/)
[![TTS](https://img.shields.io/badge/Voice-Edge_TTS_Vietnamese-0078D7?style=for-the-badge&logo=microsoft)](https://github.com/rany2/edge-tts)
[![Hardware](https://img.shields.io/badge/Render-NVIDIA_NVENC_RTX-76B900?style=for-the-badge&logo=nvidia&logoColor=white)](https://developer.nvidia.com/video-encode-decode-gpu-support-matrix)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

*Dự án cá nhân hóa của **[tranchon2702](https://github.com/tranchon2702)** — Tối ưu cho quy trình sáng tạo nội dung chất lượng cao, bền vững và tự động.*

</div>

---

## 🌟 Điểm Nổi Bật

* 🧠 **Kịch Bản Tư Duy Sâu (Gemini 3.8 Flash - Thinking High)**:
  * Không tạo kịch bản hời hợt hay spam nội dung.
  * Tích hợp chế độ tư duy sâu (**Thinking Level: HIGH**) của Google Gemini 3.8 giúp kết nối các luận điểm logic, đào sâu bản chất vấn đề.
  * Tự động bổ sung lời kết thúc cuốn hút (**Call-To-Action**) kích thích khán giả comment và follow kênh.

* 🎨 **Hình Ảnh & Video AI Nhất Quán**:
  * Tích hợp **Google Imagen 3** (`imagen-3.0-generate-002`) cho hình ảnh đồng nhất phong cách (Dark Moody Illustration / Cinematic).
  * Hỗ trợ tìm kiếm footage bản quyền từ **Pexels**, Pixabay hoặc dùng kho video tự quay cục bộ.

* 🗣️ **Giọng Đọc Tiếng Việt & Quốc Tế Chuẩn**:
  * Lồng tiếng mượt mà với Microsoft Edge TTS (`vi-VN-HoaiMyNeural`, `vi-VN-NamMinhNeural`).
  * Hỗ trợ đa ngôn ngữ (Tiếng Anh, Tiếng Việt) cho cả kênh Global và kênh nội địa.

* 📝 **Phụ Đề Động Chuẩn Typography Tiếng Việt**:
  * Tự động nhận diện nhịp nói (Whisper/Edge timeline) khớp từng từ.
  * Đã nhúng sẵn bộ font chuẩn tiếng Việt không bao giờ lỗi dấu (`BeVietnamPro-Bold.ttf`).
  * Hiệu ứng chữ nổi bật, màu sắc tùy biến theo chủ đề video.

* 🎵 **Nhạc Nền (BGM) Tự Động**:
  * Tự động trộn nhạc nền ngẫu nhiên từ kho 29 bản nhạc chất lượng cao có sẵn trong hệ thống.
  * Tự động căn chỉnh âm lượng giọng nói nổi bật hơn nhạc nền.

* 👗 **Thử Đồ Ảo AI (Virtual Try-On cho Shopee & TikTok Affiliate)**:
  * Tích hợp công nghệ **IDM-VTON** thử đồ ảo trực tiếp trên WebUI.
  * Hỗ trợ 2 chế độ: **Free 100%** (qua Cloud GPU Hugging Face ZeroGPU) và **Replicate API** siêu tốc.
  * Tự động mặc quần áo, váy đầm từ Shopee/TikTok lên người mẫu AI xinh đẹp mà vẫn giữ nguyên khuôn mặt và dáng vóc.

* 🧩 **Chrome / Edge Extension (Gemini AI Studio & Shopee Downloader)**:
  * Tiện ích mở rộng nằm tại thư mục `extension/`.
  * Hỗ trợ tải ảnh sản phẩm sạch chuẩn HD từ Shopee/TikTok Shop chỉ với 1 click.
  * Tạo prompt tiếng Việt chuẩn fashion model cho Gemini Web để tự sinh mẫu ảnh theo ý muốn.

* ⚡ **Hiệu Năng & Bảo Mật Tuyệt Đối (Local 100%)**:
  * Tận dụng card đồ họa rời NVIDIA RTX (NVENC `h264_nvenc`) giúp render video 1080x1920 60fps cực nhanh.
  * Chạy hoàn toàn cục bộ trên máy tính cá nhân. File cấu hình API (`config.toml`) và video xuất bản (`storage/`) được bảo vệ nghiêm ngặt, không bao giờ đẩy lên mạng.

---

## 🚀 Khởi Động Nhanh

### 1. Bật ứng dụng bằng 1 click:
Click đúp vào file:
👉 **`start-studio.bat`**  
*(Ứng dụng sẽ tự động kiểm tra môi trường Python và mở giao diện Web tại `http://127.0.0.1:8501`)*

Hoặc chạy lệnh qua PowerShell:
```powershell
.\start-studio.ps1
```

Hoặc qua dòng lệnh tiêu chuẩn:
```bash
uv run streamlit run webui/Main.py --server.address 127.0.0.1 --server.port 8501
```

---

## ⚙️ Cấu Hình Mô Hình AI (`config.toml`)

Hệ thống đã được cấu hình tối ưu sẵn tại file `config.toml`:

```toml
project_name = "TNTC Studio"

[app]
# Nguồn LLM tạo kịch bản
llm_provider = "gemini"
gemini_api_key = "YOUR_GEMINI_API_KEY" # Lấy tại https://aistudio.google.com/app/apikey
gemini_model_name = "gemini-3.8-flash"
gemini_thinking_level = "high"   # Bật chế độ suy nghĩ sâu

# Nguồn tạo hình ảnh AI
video_source = "openai_image"
openai_image_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
openai_image_model = "imagen-3.0-generate-002"
openai_image_prompt_template = "digital illustration of {term}, dark moody atmosphere, cinematic lighting, clean art style, vibrant colors, vertical 9:16 composition, no text no watermark"

# Nhạc nền & Giọng đọc
bgm_type = "random"
edge_tts_timeout = 120
video_codec = "h264_nvenc"       # Render bằng GPU RTX
```

---

## 📂 Thư Mục Video Đầu Ra

Sau khi bấm **Tạo Video**, toàn bộ thành phẩm được lưu tự động tại:

```text
storage/
└── tasks/
    └── <mã-nhiệm-vụ>/
        ├── final-1.mp4    <-- Video hoàn chỉnh xuất bản (1080x1920)
        ├── audio.mp3      <-- File giọng đọc thuyết minh
        ├── subtitle.srt   <-- File phụ đề rời
        └── script.json    <-- Toàn bộ kịch bản và từ khóa
```

> **Mẹo**: Trên bảng quản lý tác vụ của WebUI, anh chỉ cần bấm vào nút **📁 (Mở thư mục)** là cửa sổ Windows File Explorer sẽ bật ra ngay thư mục chứa video `final-1.mp4`!

---

## 🎯 Định Hướng Kênh Đang Xây Dựng

1. 🌌 **Kênh Khoa Học Vũ Trụ & Bí Ẩn** (Tiếng Anh - Global): Kịch bản ly kỳ, phong cách hình ảnh tối huyền bí, tập trung thị trường US/UK.
2. 🕵️‍♂️ **Kênh Tri Thức & Giải Mã** (Tiếng Việt): Chủ đề tâm lý học, sự thật ít người biết, các vụ án bí ẩn, lịch sử kỳ thú.

---

## 📜 Giấy Phép & Bản Quyền

* Dự án được cá nhân hóa và phát triển bởi **[tranchon2702](https://github.com/tranchon2702)**.
* Kế thừa và phát triển từ mã nguồn mở MoneyPrinterTurbo theo giấy phép **MIT License**.
