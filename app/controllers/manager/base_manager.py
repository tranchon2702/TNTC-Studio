import threading
from typing import Any, Callable, Dict

from loguru import logger


class TaskQueueFullError(ValueError):
    pass


class TaskManager:
    def __init__(self, max_concurrent_tasks: int, max_queued_tasks: int = 100):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.max_queued_tasks = max_queued_tasks
        self.current_tasks = 0
        self.lock = threading.Lock()
        self.queue = self.create_queue()

    def create_queue(self):
        raise NotImplementedError()

    def add_task(self, func: Callable, *args: Any, **kwargs: Any):
        with self.lock:
            if self.current_tasks < self.max_concurrent_tasks:
                logger.info(
                    f"add task: {func.__name__}, current_tasks: {self.current_tasks}"
                )
                # Dự trữ hạn ngạch đồng thời trước khi chuỗi bắt đầu. Việc triển khai ban đầu tăng dần trong luồng cho các yêu cầu liên tục
                # Có thể thấy current_tasks=0 trước khi luồng con lấy được khóa, do đó phá vỡ sự tương tranh
                # giới hạn trên. Khi khởi động không thành công, hạn ngạch sẽ được khôi phục để các yêu cầu tiếp theo vẫn có thể được lên lịch bình thường.
                self.current_tasks += 1
                try:
                    self.execute_task(func, *args, **kwargs)
                except Exception:
                    self.current_tasks -= 1
                    raise
            else:
                queue_size = self.queue_size()
                # Hàng đợi sẽ chỉ được xếp hàng khi số lượng yêu cầu đồng thời đã đầy. Hàng đợi phải được giới hạn, nếu không giao diện ẩn danh có thể tồn tại
                # Xếp chồng các đối tượng nhiệm vụ và tham số yêu cầu, cuối cùng là hết bộ nhớ hoặc chi phí API của bên thứ ba nằm ngoài tầm kiểm soát.
                if queue_size >= self.max_queued_tasks:
                    logger.warning(
                        f"reject task: {func.__name__}, queue_size: {queue_size}, "
                        f"max_queued_tasks: {self.max_queued_tasks}"
                    )
                    raise TaskQueueFullError("task queue is full, please try again later")

                logger.info(
                    f"enqueue task: {func.__name__}, current_tasks: {self.current_tasks}, "
                    f"queue_size: {queue_size}"
                )
                self.enqueue({"func": func, "args": args, "kwargs": kwargs})

    def execute_task(self, func: Callable, *args: Any, **kwargs: Any):
        thread = threading.Thread(
            target=self.run_task, args=(func, *args), kwargs=kwargs
        )
        thread.start()

    def run_task(self, func: Callable, *args: Any, **kwargs: Any):
        try:
            func(*args, **kwargs)  # call the function here, passing *args and **kwargs.
        finally:
            self.task_done()

    def check_queue(self):
        with self.lock:
            if (
                self.current_tasks < self.max_concurrent_tasks
                and not self.is_queue_empty()
            ):
                task_info = self.dequeue()
                if task_info is None:
                    # dequeue() may skip and discard queue entries that no longer
                    # pass current validation (see RedisTaskManager.dequeue) and
                    # return None once nothing usable is left, even though
                    # is_queue_empty() was False a moment earlier.
                    return
                func = task_info["func"]
                args = task_info.get("args", ())
                kwargs = task_info.get("kwargs", {})
                # Duy trì thời gian đếm tương tự như các tác vụ được tạo trực tiếp để tránh các tác vụ vừa được xếp hàng đợi và chưa có trong chuỗi.
                # Trong quá trình đếm nội bộ, các yêu cầu mới sẽ bỏ qua hàng đợi và chiếm cùng một hạn mức đồng thời.
                self.current_tasks += 1
                try:
                    self.execute_task(func, *args, **kwargs)
                except Exception:
                    self.current_tasks -= 1
                    self.enqueue(task_info)
                    raise

    def task_done(self):
        with self.lock:
            self.current_tasks -= 1
        self.check_queue()

    def enqueue(self, task: Dict):
        raise NotImplementedError()

    def dequeue(self):
        raise NotImplementedError()

    def is_queue_empty(self):
        raise NotImplementedError()

    def queue_size(self):
        raise NotImplementedError()
