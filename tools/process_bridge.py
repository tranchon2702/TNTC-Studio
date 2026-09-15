import os
import sys
import time
import re
import requests
import urllib.parse
import random

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from loguru import logger
from app.services import bridge, llm, material
from app.models.schema import VideoAspect
from app.utils import utils


def process_pending_bridge_request():
    state = bridge.get_bridge_state()
    if state.get("status") != "pending":
        print(f"No pending request. Current status: {state.get('status')}")
        return state

    req_id = state.get("id")
    subject = state.get("subject", "").strip()
    total_images = state.get("total_images", 5)

    logger.info(f"Processing bridge request {req_id}: subject={subject}")

    bridge.update_bridge_state(
        status="processing",
        progress=15,
        message=f"Antigravity đang viết kịch bản về: {subject[:40]}...",
    )

    try:
        # Step 1: Generate Script
        script = llm.generate_script(
            video_subject=subject,
            language="vi",
            paragraph_number=1,
        )
        if not script or script.startswith("Error:"):
            bridge.update_bridge_state(
                status="failed",
                progress=0,
                message=f"Không thể tạo kịch bản: {script}",
            )
            return

        bridge.update_bridge_state(
            progress=30,
            script=script,
            message="Đã viết xong kịch bản, đang bóc tách phân cảnh...",
        )

        # Step 2: Generate Terms
        terms = llm.generate_terms(
            video_subject=subject,
            video_script=script,
            amount=total_images,
            match_script_order=True,
        )
        if not terms or (isinstance(terms, list) and len(terms) == 0):
            terms = [subject]

        bridge.update_bridge_state(
            progress=40,
            terms=terms,
            total_images=len(terms),
            message=f"Bắt đầu vẽ {len(terms)} ảnh phân cảnh 9:16...",
        )

        # Step 3: Generate Images into storage/local_videos
        local_dir = utils.storage_dir("local_videos", create=True)
        images_info = []

        for idx, term in enumerate(terms):
            img_num = idx + 1
            cur_pct = 40 + int(55 * (img_num / len(terms)))
            bridge.update_bridge_state(
                progress=cur_pct,
                completed_images=idx,
                message=f"Đang vẽ ảnh {img_num}/{len(terms)}: {term[:30]}...",
            )

            prompt = f"digital illustration of {term}, cinematic lighting, 9:16 vertical composition, highly detailed, vibrant aesthetic, 8k"
            img_bytes = material._request_pollinations_image(prompt, VideoAspect.portrait)

            if img_bytes:
                clean_name = re.sub(r"[^a-zA-Z0-9]", "_", term)[:25]
                fname = f"{img_num:02d}_{clean_name}.png"
                fpath = os.path.join(local_dir, fname)
                with open(fpath, "wb") as f:
                    f.write(img_bytes)

                images_info.append(
                    {
                        "index": img_num,
                        "name": fname,
                        "path": fpath,
                        "term": term,
                    }
                )
                bridge.update_bridge_state(
                    images=images_info,
                    completed_images=img_num,
                )
                logger.success(f"Saved bridge image {img_num}: {fpath}")
            else:
                logger.warning(f"Failed to generate image for term: {term}")

        # Complete!
        bridge.update_bridge_state(
            status="completed",
            progress=100,
            message=f"Đã hoàn thành kịch bản và {len(images_info)} ảnh 9:16!",
            images=images_info,
            script=script,
            terms=terms,
        )
        logger.info(f"Bridge request {req_id} completed successfully!")

    except Exception as e:
        logger.error(f"Error processing bridge request: {e}")
        bridge.update_bridge_state(
            status="failed",
            progress=0,
            message=f"Lỗi: {str(e)}",
        )


if __name__ == "__main__":
    process_pending_bridge_request()
