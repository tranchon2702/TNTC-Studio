#!/usr/bin/env sh

# If you could not download the model from the official site, you can use the mirror site.
# Just remove the comment of the following line .
# Nếu bạn không thể tải xuống mô hình từ trang web chính thức, bạn có thể sử dụng trang web nhân bản.
# Chỉ cần xóa nhận xét ở dòng bên dưới.

# export HF_ENDPOINT=https://hf-mirror.com

CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$CURRENT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 0.0.0.0 chỉ có thể có nghĩa là "nghe trên tất cả các card mạng" và không phù hợp làm địa chỉ truy cập trình duyệt.
# Khi mở http://0.0.0.0:8501 trong trình duyệt chạy macOS/Linux, nó có thể đi qua proxy hoặc cổng.
# Cuối cùng 502 xuất hiện. Liên kết và mở 127.0.0.1 theo mặc định, phù hợp với các tập lệnh khởi động Windows.
MPT_WEBUI_HOST="${MPT_WEBUI_HOST:-127.0.0.1}"
MPT_WEBUI_PORT="${MPT_WEBUI_PORT:-8501}"

if [ -x "$CURRENT_DIR/.venv/bin/python" ]; then
  set -- "$CURRENT_DIR/.venv/bin/python" -m streamlit
  find_port() { find_available_port "$CURRENT_DIR/.venv/bin/python"; }
elif command -v uv >/dev/null 2>&1; then
  set -- uv run streamlit
  find_port() { find_available_port uv run python; }
elif command -v streamlit >/dev/null 2>&1; then
  echo "***** Warning: using streamlit from PATH. If dependencies fail, run 'uv sync --frozen' first. *****"
  set -- streamlit
  find_port() { find_available_port python3; }
else
  echo "***** Neither project Python, uv, nor streamlit was found. Please install dependencies first. *****"
  exit 1
fi

find_available_port() {
  WEBUI_HOST="$MPT_WEBUI_HOST" WEBUI_PORT="$MPT_WEBUI_PORT" "$@" - <<'PY' 2>/dev/null
import os
import socket
import sys

host = os.environ.get("WEBUI_HOST", "127.0.0.1")
preferred = int(os.environ.get("WEBUI_PORT", "8501"))
candidates = [preferred] + [port for port in range(8502, 8600) if port != preferred]

for port in candidates:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            continue
        print(port)
        sys.exit(0)

sys.exit(1)
PY
}

# Sử dụng Python để phát hiện cổng nhằm tránh dựa vào sự khác biệt về lsof/nc trên các bản phân phối macOS/Linux khác nhau.
SELECTED_WEBUI_PORT=$(find_port)

if [ -z "$SELECTED_WEBUI_PORT" ]; then
  echo "***** No available WebUI port found in 8501-8599 for $MPT_WEBUI_HOST. *****"
  exit 1
fi

if [ "$SELECTED_WEBUI_PORT" != "$MPT_WEBUI_PORT" ]; then
  echo "***** Port $MPT_WEBUI_PORT is unavailable, using $SELECTED_WEBUI_PORT instead. *****"
fi

MPT_WEBUI_PORT="$SELECTED_WEBUI_PORT"

echo "***** WebUI address: http://$MPT_WEBUI_HOST:$MPT_WEBUI_PORT *****"
"$@" run "$CURRENT_DIR/webui/Main.py" \
  --server.address="$MPT_WEBUI_HOST" \
  --server.port="$MPT_WEBUI_PORT" \
  --browser.serverAddress="$MPT_WEBUI_HOST" \
  --browser.gatherUsageStats=False \
  --client.toolbarMode=minimal \
  --logger.hideWelcomeMessage=True \
  --server.showEmailPrompt=False \
  --server.enableCORS=True
