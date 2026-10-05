import uvicorn
from loguru import logger

from app.config import config

if __name__ == "__main__":
    logger.info(
        "start server, docs: http://127.0.0.1:" + str(config.listen_port) + "/docs"
    )
    # Tính năng phát hiện FFmpeg đã được chuyển sang đường dẫn tác vụ được chia sẻ trong app/services/task.py, vì vậy
    # Ba đường dẫn API, CLI và WebUI có thể được đề cập thống nhất và sẽ không được kiểm tra riêng ở đây.
    uvicorn.run(
        app="app.asgi:app",
        host=config.listen_host,
        port=config.listen_port,
        reload=config.reload_debug,
        log_level="warning",
    )
