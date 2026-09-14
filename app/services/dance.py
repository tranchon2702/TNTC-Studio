import os
import json
import shutil
import subprocess
from pathlib import Path
from loguru import logger

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


def get_video_info(video_path: str) -> dict:
    """Sử dụng ffprobe để lấy siêu dữ liệu video (duration, fps, kích thước)."""
    if not os.path.isfile(video_path):
        return {"error": "File không tồn tại", "duration": 0.0, "fps": 30.0, "width": 1080, "height": 1920}

    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        video_path,
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(res.stdout)
        stream = data.get("streams", [{}])[0]
        fmt = data.get("format", {})

        width = int(stream.get("width", 1080))
        height = int(stream.get("height", 1920))

        # Duration
        dur_str = stream.get("duration") or fmt.get("duration") or "0"
        duration = float(dur_str)

        # FPS
        fps_str = stream.get("r_frame_rate", "30/1")
        if "/" in fps_str:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 30.0
        else:
            fps = float(fps_str)

        return {
            "duration": round(duration, 2),
            "fps": round(fps, 2),
            "width": width,
            "height": height,
        }
    except Exception as e:
        logger.warning(f"ffprobe error for {video_path}: {e}")
        return {"duration": 0.0, "fps": 30.0, "width": 1080, "height": 1920}


def download_tiktok_video(url: str, output_dir: str = "storage/dance") -> dict:
    """
    Tải video sạch không logo từ TikTok / Douyin / Shorts bằng yt-dlp.
    Đồng thời tự động trích xuất file audio.mp3.
    """
    if not yt_dlp:
        return {"status": "error", "message": "Thư viện yt-dlp chưa được cài đặt."}

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    timestamp = int(os.times().system * 1000)
    base_name = f"tiktok_{timestamp}"
    ydl_opts = {
        "outtmpl": str(out_path / f"{base_name}.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get("id", base_name)
            title = info.get("title", "TikTok Dance")
            duration = float(info.get("duration", 0.0))

            # Tìm file video đã tải về
            video_file = None
            for ext in ["mp4", "webm", "mkv"]:
                candidate = out_path / f"{base_name}.{ext}"
                if candidate.exists():
                    video_file = str(candidate)
                    break

            if not video_file:
                # Quét theo video_id
                for f in out_path.glob(f"*{video_id}*"):
                    if f.suffix in [".mp4", ".webm", ".mkv"]:
                        video_file = str(f)
                        break

            if not video_file:
                return {"status": "error", "message": "Không tìm thấy file video sau khi tải."}

            # Lấy thông số kỹ thuật chính xác bằng ffprobe
            v_info = get_video_info(video_file)
            if v_info["duration"] > 0:
                duration = v_info["duration"]

            # Trích xuất file âm thanh gốc (dùng cho nghe thử / tham khảo nhịp)
            audio_file = str(out_path / f"{base_name}_audio.mp3")
            ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
            cmd_audio = [
                ffmpeg_bin, "-y",
                "-i", video_file,
                "-vn",
                "-acodec", "libmp3lame",
                "-q:a", "2",
                audio_file,
            ]
            try:
                subprocess.run(cmd_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            except Exception as ae:
                logger.warning(f"Could not extract audio: {ae}")
                audio_file = ""

            return {
                "status": "success",
                "video_path": video_file,
                "audio_path": audio_file,
                "duration": duration,
                "fps": v_info["fps"],
                "width": v_info["width"],
                "height": v_info["height"],
                "title": title,
            }
    except Exception as e:
        logger.error(f"Lỗi tải video TikTok: {e}")
        return {"status": "error", "message": f"Không thể tải video từ link: {str(e)}"}


def trim_dance_video(
    video_path: str,
    start_sec: float,
    duration_sec: float,
    output_path: str,
    force_9_16: bool = True,
) -> tuple[bool, str]:
    """
    Cắt đoạn video nhảy mẫu với thời lượng mong muốn (ví dụ 5s, 7s, 10s)
    và chuẩn hóa về tỉ lệ 9:16 (1080x1920) ở 30fps.
    """
    if not os.path.isfile(video_path):
        return False, "File video gốc không tồn tại."

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    vf_filters = []
    if force_9_16:
        # Scale & Crop thành 9:16 (1080x1920)
        vf_filters.append("scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920")
    vf_filters.append("fps=30")
    vf_str = ",".join(vf_filters)

    # Thử render bằng NVIDIA NVENC trước để đạt tốc độ nhanh nhất
    cmd_nvenc = [
        ffmpeg_bin, "-y",
        "-ss", str(round(start_sec, 2)),
        "-i", video_path,
        "-t", str(round(duration_sec, 2)),
        "-vf", vf_str,
        "-c:v", "h264_nvenc",
        "-preset", "p5",
        "-b:v", "8M",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]

    try:
        subprocess.run(cmd_nvenc, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return True, output_path
    except Exception as e:
        logger.warning(f"NVENC trim failed, falling back to libx264: {e}")

    # Fallback CPU
    cmd_cpu = [
        ffmpeg_bin, "-y",
        "-ss", str(round(start_sec, 2)),
        "-i", video_path,
        "-t", str(round(duration_sec, 2)),
        "-vf", vf_str,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]
    try:
        subprocess.run(cmd_cpu, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return True, output_path
        return False, "Không thể tạo file cắt video."
    except Exception as e:
        return False, f"Lỗi cắt video: {str(e)}"


def enhance_video_60fps_gpu(
    input_video_path: str,
    output_video_path: str,
    target_fps: int = 60,
    sharpen: bool = True,
    ultra_smooth: bool = False,
) -> tuple[bool, str]:
    """
    Tận dụng GPU NVIDIA RTX (NVENC) để nâng cấp video:
    - Nội suy khung hình lên 60 FPS siêu mượt (Smooth motion).
    - Bộ lọc tăng độ nét thông minh (Unsharp Masking + Lanczos scaling) làm rõ từng sợi vải và da mặt.
    """
    if not os.path.isfile(input_video_path):
        return False, "File video đầu vào không tồn tại."

    os.makedirs(os.path.dirname(os.path.abspath(output_video_path)), exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    filters = []
    # 1. FPS enhancement
    if ultra_smooth:
        # Motion compensated interpolation
        filters.append(f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    else:
        filters.append(f"fps={target_fps}")

    # 2. 9:16 Scale & Crop
    filters.append("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920")

    # 3. Sharpening filter for textile & face clarity
    if sharpen:
        filters.append("unsharp=5:5:0.8:5:5:0.4")

    vf_str = ",".join(filters)

    # Cố gắng dùng NVIDIA NVENC Hardware Acceleration
    cmd_nvenc = [
        ffmpeg_bin, "-y",
        "-i", input_video_path,
        "-vf", vf_str,
        "-c:v", "h264_nvenc",
        "-preset", "p7",
        "-rc", "vbr",
        "-b:v", "15M",
        "-maxrate", "25M",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-an", # Video câm để người dùng ghép nhạc trực tiếp trên TikTok
        output_video_path,
    ]

    try:
        subprocess.run(cmd_nvenc, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.isfile(output_video_path) and os.path.getsize(output_video_path) > 0:
            return True, output_video_path
    except Exception as e:
        logger.warning(f"NVENC enhancement failed, trying CPU fallback: {e}")

    # Fallback CPU
    cmd_cpu = [
        ffmpeg_bin, "-y",
        "-i", input_video_path,
        "-vf", vf_str,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "17",
        "-pix_fmt", "yuv420p",
        "-an",
        output_video_path,
    ]
    try:
        subprocess.run(cmd_cpu, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.isfile(output_video_path) and os.path.getsize(output_video_path) > 0:
            return True, output_video_path
        return False, "Không thể xuất file nâng cấp."
    except Exception as e:
        return False, f"Lỗi nâng cấp video: {str(e)}"


def prepare_kling_package(
    person_image_path: str,
    motion_video_path: str,
    output_dir: str = "storage/dance/kling_package",
) -> tuple[bool, dict]:
    """
    Chuẩn hóa ảnh người mẫu và video nhảy thành gói chuẩn bị hoàn hảo cho Kling AI:
    - Ảnh người mẫu được crop/fit chuẩn 9:16 (1080x1920) tránh méo dáng.
    - Video nhảy được chuẩn hóa định dạng MP4 H.264.
    """
    if not (os.path.isfile(person_image_path) and os.path.isfile(motion_video_path)):
        return False, {"error": "Thiếu ảnh người mẫu hoặc video chuyển động."}

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    target_img = str(out_dir / "01_model_9x16.png")
    target_vid = str(out_dir / "02_motion_dance.mp4")

    # Chuẩn hóa ảnh người mẫu về 1080x1920
    cmd_img = [
        ffmpeg_bin, "-y",
        "-i", person_image_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        target_img,
    ]
    try:
        subprocess.run(cmd_img, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception:
        shutil.copy2(person_image_path, target_img)

    # Chuẩn hóa video chuyển động
    cmd_vid = [
        ffmpeg_bin, "-y",
        "-i", motion_video_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
        "-c:v", "h264_nvenc",
        "-b:v", "8M",
        target_vid,
    ]
    try:
        subprocess.run(cmd_vid, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception:
        shutil.copy2(motion_video_path, target_vid)

    v_info = get_video_info(target_vid)

    return True, {
        "folder": str(out_dir.resolve()),
        "model_image": target_img,
        "motion_video": target_vid,
        "duration": v_info.get("duration", 5.0),
        "fps": v_info.get("fps", 30.0),
    }


def generate_dance_video_api(
    person_image_path: str,
    motion_video_path: str,
    provider: str = "replicate",
    api_token: str = "",
    denoise_steps: int = 25,
    guidance_scale: float = 2.0,
    output_dir: str = "storage/dance",
) -> tuple[bool, str, str]:
    """
    Tạo video người mẫu AI nhảy trực tiếp 100% trong Tool qua API:
    - Provider 'replicate': Sử dụng mô hình MimicMotion (zsxkib/mimic-motion) tạo video nhảy từ ảnh người mẫu và video cử động.
    - Tự động tải video thành phẩm về máy.
    """
    if not (os.path.isfile(person_image_path) and os.path.isfile(motion_video_path)):
        return False, "", "Không tìm thấy file ảnh người mẫu hoặc video chuyển động."

    token = (api_token or "").strip()
    if not token:
        token = os.environ.get("REPLICATE_API_TOKEN", "")

    if not token:
        try:
            from app.config import config
            dance_cfg = getattr(config, "dance", {}) or {}
            if isinstance(dance_cfg, dict):
                token = dance_cfg.get("replicate_api_token", "")
            if not token:
                tryon_cfg = getattr(config, "tryon", {}) or {}
                if isinstance(tryon_cfg, dict):
                    token = tryon_cfg.get("replicate_api_token", "")
        except Exception:
            pass

    if not token:
        return False, "", "Vui lòng nhập Replicate API Token để kích hoạt chế độ tạo tự động trong tool."

    out_folder = Path(output_dir)
    out_folder.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    dest_path = str(out_folder / f"ai_rendered_dance_{timestamp}.mp4")

    if provider == "replicate":
        try:
            import replicate
            import requests

            client = replicate.Client(api_token=token)
            logger.info("Đang gửi yêu cầu tạo video nhảy đến Replicate AI (MimicMotion)...")

            with open(person_image_path, "rb") as img_f, open(motion_video_path, "rb") as vid_f:
                output = client.run(
                    "zsxkib/mimic-motion:2e7f79edf416dd7edd32dce85b7835be98eeb1d8eca2bebb2b8db8c996e5efad",
                    input={
                        "appearance_image": img_f,
                        "motion_video": vid_f,
                        "denoising_steps": denoise_steps,
                        "guidance_scale": guidance_scale,
                        "output_frames_per_second": 15,
                    },
                )

            # Output is an URL / FileOutput object
            video_url = str(output)
            logger.info(f"Replicate đã tạo video thành công: {video_url}")

            resp = requests.get(video_url, timeout=120, stream=True)
            if resp.status_code == 200:
                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                logger.success(f"Đã lưu video nhảy về: {dest_path}")
                return True, dest_path, "Tạo video nhảy thành công!"
            else:
                return False, "", f"Không thể tải video từ Replicate (HTTP {resp.status_code})"

        except Exception as e:
            logger.error(f"Lỗi tạo video qua Replicate API: {e}")
            return False, "", f"Lỗi Replicate API: {str(e)}"

    return False, "", f"Provider '{provider}' chưa được hỗ trợ."

