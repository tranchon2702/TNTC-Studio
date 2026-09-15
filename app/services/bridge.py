import json
import os
import time
from typing import Any, Dict, List, Optional
from loguru import logger
from app.utils import utils

BRIDGE_FILE = os.path.join(utils.storage_dir(), "antigravity_bridge.json")


def get_bridge_file_path() -> str:
    os.makedirs(os.path.dirname(BRIDGE_FILE), exist_ok=True)
    return BRIDGE_FILE


def get_default_bridge_state() -> Dict[str, Any]:
    return {
        "id": "",
        "subject": "",
        "status": "idle",  # idle, pending, processing, completed, failed
        "progress": 0,
        "message": "Sẵn sàng nhận yêu cầu",
        "script": "",
        "terms": [],
        "total_images": 5,
        "completed_images": 0,
        "images": [],
        "created_at": 0,
        "updated_at": 0,
    }


def get_bridge_state() -> Dict[str, Any]:
    fpath = get_bridge_file_path()
    if not os.path.exists(fpath):
        return get_default_bridge_state()
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"failed to read bridge state: {e}")
    return get_default_bridge_state()


def create_bridge_request(subject: str, total_images: int = 5) -> Dict[str, Any]:
    req_id = f"req_{int(time.time())}"
    state = {
        "id": req_id,
        "subject": subject.strip(),
        "status": "pending",
        "progress": 5,
        "message": "Đã gửi yêu cầu! Đang chờ Antigravity nhận lệnh...",
        "script": "",
        "terms": [],
        "total_images": total_images,
        "completed_images": 0,
        "images": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _save_bridge_state(state)
    logger.info(f"created new antigravity bridge request: id={req_id}, subject={subject}")
    return state


def update_bridge_state(**kwargs) -> Dict[str, Any]:
    state = get_bridge_state()
    state.update(kwargs)
    state["updated_at"] = time.time()
    _save_bridge_state(state)
    return state


def _save_bridge_state(state: Dict[str, Any]):
    fpath = get_bridge_file_path()
    try:
        temp_path = f"{fpath}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, fpath)
    except Exception as e:
        logger.error(f"failed to save bridge state: {e}")


def reset_bridge_state() -> Dict[str, Any]:
    state = get_default_bridge_state()
    _save_bridge_state(state)
    return state
