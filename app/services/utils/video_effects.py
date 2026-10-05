import numpy as np
from moviepy import Clip, ColorClip, CompositeVideoClip, vfx
from PIL import Image


# FadeIn
def fadein_transition(clip: Clip, t: float) -> Clip:
    return clip.with_effects([vfx.FadeIn(t)])


# FadeOut
def fadeout_transition(clip: Clip, t: float) -> Clip:
    return clip.with_effects([vfx.FadeOut(t)])


# SlideIn
def slidein_transition(clip: Clip, t: float, side: str) -> Clip:
    width, height = clip.size

    # SlideIn tích hợp của MoviePy không ổn định đối với các vật liệu toàn màn hình trong chuỗi xử lý hiện tại.
    # Sẽ xảy ra tình huống "việc chuyển đổi được áp dụng một cách hợp lý nhưng hầu như không có sự thay đổi nào trong hình ảnh".
    # Ở đây, nó được thay đổi thành nền đen rõ ràng + hoạt ảnh dịch chuyển để đảm bảo rằng hiệu ứng chuyển tiếp hiển thị và hành vi có thể kiểm soát được.
    def position(current_time: float):
        progress = min(max(current_time / max(t, 0.001), 0), 1)

        if side == "left":
            return (-width + width * progress, 0)
        if side == "right":
            return (width - width * progress, 0)
        if side == "top":
            return (0, -height + height * progress)
        if side == "bottom":
            return (0, height - height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# SlideOut
def slideout_transition(clip: Clip, t: float, side: str) -> Clip:
    width, height = clip.size
    transition_start = max(clip.duration - t, 0)

    # SlideOut cũng được thay đổi thành chuyển vị rõ ràng để đảm bảo phần cuối của clip có thể trượt ra khỏi màn hình một cách ổn định.
    def position(current_time: float):
        if current_time <= transition_start:
            return (0, 0)

        progress = min(
            max((current_time - transition_start) / max(t, 0.001), 0), 1
        )

        if side == "left":
            return (-width * progress, 0)
        if side == "right":
            return (width * progress, 0)
        if side == "top":
            return (0, -height * progress)
        if side == "bottom":
            return (0, height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# Việc giữ lại phạm vi thu phóng 20% ​​của thiết kế ban đầu mang lại cảm giác rõ ràng về chuyển động của Ken Burns ngay cả trong những đoạn clip ngắn khoảng ba giây.
# Độ ổn định của tỷ lệ được đảm bảo bằng cách lấy mẫu trung tâm pixel phụ bên dưới mà không che giấu hiện tượng nhấp nháy mã hóa video nguồn bằng cách giảm cường độ của hiệu ứng.
_ZOOM_MAX_SCALE = 1.2


def _zoom_frame(frame: np.ndarray, scale_factor: float) -> np.ndarray:
    """Sử dụng tính năng cắt trung tâm pixel phụ để đạt được hiệu ứng thu phóng ổn định và không có viền đen.

    Trước tiên, bạn không thể chuyển đổi chiều rộng và chiều cao cắt xén thành số nguyên: khi tỷ lệ thu phóng thay đổi liên tục, các ranh giới số nguyên sẽ nhảy ở các bước khác nhau
    và thay đổi pha lấy mẫu nửa pixel khi chuyển đổi kích thước chẵn lẻ, dẫn đến hiện tượng rung hình. Phép biến đổi EXTENT của Pillow
    có thể trực tiếp nhận các ranh giới dấu phẩy động và lấy mẫu pixel phụ hoàn chỉnh trên khung vẽ đầu ra cố định; ranh giới bên trái và bên phải, ranh giới trên và dưới
    luôn đối xứng xung quanh cùng một tâm dấu phẩy động, vì vậy nó phù hợp với những cảnh mà toàn bộ video tiếp tục thu phóng chậm.
    """
    if scale_factor <= 0:
        raise ValueError("scale_factor must be greater than zero")

    # Thu phóng 1x trực tiếp quay trở lại khung hình ban đầu để tránh việc lấy mẫu lại vô nghĩa gây ra hiện tượng mờ nhẹ ở khung hình đầu tiên.
    if abs(scale_factor - 1.0) < 1e-9:
        return frame

    height, width = frame.shape[:2]
    crop_width = width / scale_factor
    crop_height = height / scale_factor
    left = (width - crop_width) / 2
    top = (height - crop_height) / 2
    right = left + crop_width
    bottom = top + crop_height

    image = Image.fromarray(frame)
    transformed = image.transform(
        (width, height),
        Image.Transform.EXTENT,
        (left, top, right, bottom),
        # Việc thu phóng video liên tục chú ý nhiều hơn đến tính nhất quán của các khung liền kề. BICUBIC/LANCZOS Mặc dù khung hình đơn sắc nét hơn,
        # Tuy nhiên, kết cấu tần số cao dễ bị rung và nhấp nháy độ sáng khi đi qua lưới lấy mẫu; BILINEAR mềm hơn và
        # Một chút mất đi độ sắc nét có thể được đổi lấy một cái nhìn năng động hơn.
        resample=Image.Resampling.BILINEAR,
    )
    return np.asarray(transformed)


def zoomin_transition(clip: Clip, t: float) -> Clip:
    """Phóng to mượt mà từ bản gốc lên 1,2x trên toàn bộ clip."""
    # t được dành riêng tạm thời để duy trì chữ ký cuộc gọi thống nhất với các chức năng chuyển tiếp khác; việc thu phóng cần bao phủ toàn bộ clip,
    # Nếu không, hình ảnh sẽ đột ngột bị đóng băng sau khi thu phóng ngắn, điều này không phù hợp với các vật liệu tĩnh hoặc chuyển động thấp.
    _ = t
    duration = max(clip.duration, 0.001)

    def scale_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        scale_factor = 1 + (_ZOOM_MAX_SCALE - 1) * progress
        return _zoom_frame(get_frame(current_time), scale_factor)

    return clip.transform(scale_effect)


def zoomout_transition(clip: Clip, t: float) -> Clip:
    """Thu nhỏ mượt mà từ 1,2x về khung hình gốc trong suốt clip."""
    # Phù hợp với zoomin_transition, t chỉ được sử dụng để tương thích với giao diện gọi chuyển đổi hợp nhất.
    _ = t
    duration = max(clip.duration, 0.001)

    def scale_effect(get_frame, current_time: float):
        progress = min(max(current_time / duration, 0), 1)
        scale_factor = _ZOOM_MAX_SCALE - (_ZOOM_MAX_SCALE - 1) * progress
        return _zoom_frame(get_frame(current_time), scale_factor)

    return clip.transform(scale_effect)
