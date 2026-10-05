import json
from typing import Dict

import redis
from loguru import logger
from pydantic import ValidationError

from app.controllers.manager.base_manager import TaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as tm

FUNC_MAP = {
    "start": tm.start,
    # 'start_test': tm.start_test
}


class RedisTaskManager(TaskManager):
    def __init__(
        self,
        max_concurrent_tasks: int,
        redis_url: str,
        max_queued_tasks: int = 100,
    ):
        self.redis_client = redis.Redis.from_url(redis_url)
        super().__init__(max_concurrent_tasks, max_queued_tasks=max_queued_tasks)

    def create_queue(self):
        return "task_queue"

    def enqueue(self, task: Dict):
        task_with_serializable_params = task.copy()
        # task.copy() chỉ sao chép từ điển ngoài cùng; nếu bạn trực tiếp viết lại các kwargs lồng nhau, người gọi sẽ
        # VideoParams được giữ lại được thay thế đồng bộ bằng dict. Nhật ký tiếp theo hoặc lần thử lại vẫn có thể đọc tác vụ ban đầu.
        # Do đó, các kwargs được sao chép riêng ở đây để đảm bảo không có tác dụng phụ ngoài ý muốn trong quá trình xê-ri hóa.
        task_kwargs = task.get("kwargs", {})
        task_with_serializable_params["kwargs"] = task_kwargs.copy()

        if "params" in task_kwargs and isinstance(task_kwargs["params"], VideoParams):
            task_with_serializable_params["kwargs"]["params"] = task_kwargs[
                "params"
            ].model_dump(warnings=False)

        # Chuyển đổi một đối tượng hàm thành tên của nó
        task_with_serializable_params["func"] = task["func"].__name__
        self.redis_client.rpush(self.queue, json.dumps(task_with_serializable_params))

    def dequeue(self):
        # Vòng lặp thay vì một cửa sổ bật lên duy nhất: một tác vụ có thể đáp ứng các quy tắc xác minh VideoParams hiện tại khi nó được đưa vào hàng đợi.
        # Nhưng bản thân các quy tắc xác thực đã được thắt chặt giữa các lần triển khai (ví dụ: ràng buộc ge=1 mới đã được thêm vào). lpop có tính hủy diệt
        # Hoạt động, một khi nó bật lên thì không thể đặt lại vào vị trí cũ; nếu bạn thấy rằng quá trình xác minh không thành công khi bạn xây dựng lại VideoParams,
        # Tác vụ này đã bị xóa vĩnh viễn khỏi hàng đợi và không thể giả vờ ở đó được nữa. Thay vì để ngoại lệ đi lên từ đây
        # Ném và phá hủy người giữ khóa của nhiệm vụ bị mất này. Tốt hơn hết bạn nên vứt nó tại chỗ và tiếp tục thử xếp hàng.
        # Mục tiếp theo ở đây là duy trì thỏa thuận "nhận một nhiệm vụ có sẵn hoặc hàng đợi thực sự trống".
        while True:
            task_json = self.redis_client.lpop(self.queue)
            if not task_json:
                return None

            task_info = json.loads(task_json)
            # Chuyển đổi tên hàm trở lại đối tượng hàm
            task_info["func"] = FUNC_MAP[task_info["func"]]

            if "params" in task_info["kwargs"] and isinstance(
                task_info["kwargs"]["params"], dict
            ):
                try:
                    task_info["kwargs"]["params"] = VideoParams(
                        **task_info["kwargs"]["params"]
                    )
                except ValidationError as e:
                    logger.error(
                        "dropping queued task with params that fail current "
                        f"VideoParams validation (queued under an older, more "
                        f"permissive schema, or corrupted): {e}"
                    )
                    # Bản ghi trạng thái nhiệm vụ được tạo trước khi xếp hàng đợi và mặc định là đang xử lý; giá như
                    # Loại bỏ mục hàng đợi này mà không chạm vào bản ghi trạng thái. API/WebUI sẽ luôn hiển thị rằng tác vụ đang được thực hiện.
                    # Chạy, không bao giờ biến thành thất bại. Sử dụng patch_task thay vì update_task,
                    # Bằng cách này, nếu người dùng đã xóa tác vụ, chúng tôi sẽ không tạo lại tác vụ đó.
                    task_id = task_info["kwargs"].get("task_id")
                    if task_id:
                        sm.state.patch_task(
                            task_id,
                            state=const.TASK_STATE_FAILED,
                            failed_stage="dequeue",
                            error=f"discarded stale queued task: {e}",
                        )
                    continue

            return task_info

    def is_queue_empty(self):
        return self.redis_client.llen(self.queue) == 0

    def queue_size(self):
        return self.redis_client.llen(self.queue)
