"""
Dịch vụ Thử Đồ Ảo AI (Virtual Try-On) cho TNTC Studio.
Hỗ trợ:
- Chế độ Miễn Phí (100% Free): Kết nối tới Hugging Face Space yisol/IDM-VTON qua gradio_client.
- Chế độ Trả Phí Tốc Độ Cao: Hỗ trợ Replicate API (cuuupid/idm-vton) khi người dùng nhập API Token.
"""

import os
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple
from loguru import logger

from app.config import config

MODELS_DIR = Path("storage/models")
MODELS_DIR.mkdir(parents=True, exist_ok=True)


def run_tryon(
    person_image_path: str,
    garment_image_path: str,
    garment_description: str = "fashion clothing",
    provider: Optional[str] = None,
    api_token: Optional[str] = None,
    denoise_steps: int = 30,
) -> Tuple[bool, str, str]:
    """
    Thực hiện thử đồ ảo: Cho người mẫu mặc bộ trang phục được chỉ định.

    Args:
        person_image_path: Đường dẫn ảnh người mẫu (toàn thân hoặc bán thân).
        garment_image_path: Đường dẫn ảnh trang phục (váy, áo, đầm từ Shopee/TikTok).
        garment_description: Mô tả ngắn về trang phục (ví dụ: "summer dress", "croptop").
        provider: "free_hf" (mặc định) hoặc "replicate" (trả phí).
        api_token: Token API (nếu dùng chế độ trả phí).
        denoise_steps: Số bước khử nhiễu (mặc định 30).

    Returns:
        (success: bool, result_path_or_error: str, message: str)
    """
    if not os.path.isfile(person_image_path):
        return False, "", f"Không tìm thấy ảnh người mẫu: {person_image_path}"
    if not os.path.isfile(garment_image_path):
        return False, "", f"Không tìm thấy ảnh trang phục: {garment_image_path}"

    tryon_cfg = config.app.get("tryon", {})
    selected_provider = provider or tryon_cfg.get("provider", "free_hf")
    token = api_token or tryon_cfg.get("replicate_api_token", "")

    logger.info(
        f"Bắt đầu thử đồ ảo AI: provider={selected_provider}, "
        f"person={person_image_path}, garment={garment_image_path}"
    )

    if selected_provider == "replicate" and token:
        return _run_tryon_replicate(
            person_image_path=person_image_path,
            garment_image_path=garment_image_path,
            garment_description=garment_description,
            api_token=token,
            denoise_steps=denoise_steps,
        )

    # Mặc định: Chế độ Miễn Phí qua Hugging Face IDM-VTON
    return _run_tryon_free_hf(
        person_image_path=person_image_path,
        garment_image_path=garment_image_path,
        garment_description=garment_description,
        denoise_steps=denoise_steps,
    )


def _run_tryon_free_hf(
    person_image_path: str,
    garment_image_path: str,
    garment_description: str,
    denoise_steps: int = 30,
) -> Tuple[bool, str, str]:
    """Thử đồ ảo miễn phí qua Hugging Face IDM-VTON Space."""
    try:
        from gradio_client import Client, handle_file

        logger.info("Đang kết nối tới server IDM-VTON (Hugging Face Free GPU)...")
        client = Client("yisol/IDM-VTON")

        input_dict = {
            "background": handle_file(person_image_path),
            "layers": [],
            "composite": None,
        }

        logger.info("Đang xử lý ghép đồ AI... Quá trình có thể mất từ 30s - 60s trên server...")
        result = client.predict(
            dict=input_dict,
            garm_img=handle_file(garment_image_path),
            garment_des=garment_description,
            is_checked=True,
            is_checked_crop=False,
            denoise_steps=denoise_steps,
            seed=42,
            api_name="/tryon",
        )

        output_path = result[0] if isinstance(result, (list, tuple)) else result
        if not output_path or not os.path.isfile(output_path):
            return False, "", "Server không trả về kết quả ảnh hợp lệ."

        # Lưu ảnh vào thư mục models của TNTC Studio
        timestamp = int(time.time())
        dest_filename = f"tryon_result_{timestamp}.png"
        dest_path = MODELS_DIR / dest_filename
        shutil.copy2(output_path, dest_path)

        logger.success(f"Thử đồ thành công! Đã lưu ảnh vào: {dest_path}")
        return True, str(dest_path), "Thử đồ thành công (Chế độ Miễn Phí)!"

    except Exception as e:
        logger.error(f"Lỗi khi thử đồ qua Hugging Face: {e}")
        return False, "", f"Lỗi xử lý thử đồ: {str(e)}"


def _run_tryon_replicate(
    person_image_path: str,
    garment_image_path: str,
    garment_description: str,
    api_token: str,
    denoise_steps: int = 30,
) -> Tuple[bool, str, str]:
    """Thử đồ ảo tốc độ cao qua Replicate API."""
    import base64
    import requests

    try:
        logger.info("Đang gọi Replicate API (Chế độ trả phí tốc độ cao)...")

        def _to_data_uri(file_path: str) -> str:
            with open(file_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            ext = Path(file_path).suffix.lstrip(".").lower()
            mime = "image/png" if ext == "png" else "image/jpeg"
            return f"data:{mime};base64,{b64}"

        human_uri = _to_data_uri(person_image_path)
        garment_uri = _to_data_uri(garment_image_path)

        headers = {
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "version": "c871bb9b046616b6804422894fedfc77374b40fe07f32847a9e0ec360902e23f",
            "input": {
                "human_img": human_uri,
                "garm_img": garment_uri,
                "garment_des": garment_description,
                "denoise_steps": denoise_steps,
            },
        }

        resp = requests.post(
            "https://api.replicate.com/v1/predictions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            return False, "", f"Replicate API trả về lỗi: {resp.text}"

        prediction = resp.json()
        poll_url = prediction["urls"]["get"]

        # Chờ kết quả xử lý
        for _ in range(60):
            time.sleep(2)
            poll_resp = requests.get(poll_url, headers=headers, timeout=15)
            poll_data = poll_resp.json()
            status = poll_data.get("status")

            if status == "succeeded":
                output_url = poll_data.get("output")
                if isinstance(output_url, list):
                    output_url = output_url[0]

                # Tải ảnh về
                img_data = requests.get(output_url, timeout=30).content
                timestamp = int(time.time())
                dest_filename = f"tryon_replicate_{timestamp}.png"
                dest_path = MODELS_DIR / dest_filename
                with open(dest_path, "wb") as f:
                    f.write(img_data)

                logger.success(f"Thử đồ qua Replicate thành công: {dest_path}")
                return True, str(dest_path), "Thử đồ thành công (Replicate API)!"
            elif status in ("failed", "canceled"):
                err_msg = poll_data.get("error") or "Xử lý thất bại trên server"
                return False, "", f"Replicate lỗi: {err_msg}"

        return False, "", "Hết thời gian chờ phản hồi từ Replicate API."

    except Exception as e:
        logger.error(f"Lỗi khi gọi Replicate API: {e}")
        return False, "", f"Lỗi Replicate API: {str(e)}"
