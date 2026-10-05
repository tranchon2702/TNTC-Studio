import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from time import perf_counter
from typing import List

from loguru import logger
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletion

from app.config import config
from app.models.llm_provider import DEFAULT_LLM_PROVIDER_ID, get_llm_provider
from app.utils import utils

_max_retries = 5
MIN_SCRIPT_PARAGRAPH_NUMBER = 1
MAX_SCRIPT_PARAGRAPH_NUMBER = 10
MAX_SCRIPT_PROMPT_LENGTH = 2000
MAX_SCRIPT_SYSTEM_PROMPT_LENGTH = 8000
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_URL_USERINFO_RE = re.compile(
    r"((?:https?|wss?)://)([^/\s?#@]*:[^/\s?#@]*@)", re.IGNORECASE
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password)=)([^&#\s]+)",
    re.IGNORECASE,
)

DEFAULT_SCRIPT_SYSTEM_PROMPT = """
# Role: Video Script Generator

## Goals:
Generate a script for a video, depending on the subject of the video.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point, don't start with unnecessary things like, "welcome to this video".
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject.
""".strip()

# Claude Code CLI sử dụng từ nhắc hệ thống của tác nhân mã hóa theo mặc định, bao gồm một số lượng lớn các ràng buộc và sao chép
# Không liên quan, nó sẽ khiến việc tạo tập lệnh và từ khóa đi chệch khỏi yêu cầu, vì vậy hãy thay thế chúng hoàn toàn khi gọi.
CLAUDE_CODE_SYSTEM_PROMPT = (
    "You are a concise copywriter. Follow the user's instructions and output "
    "format exactly, and output nothing else."
)
CLAUDE_CODE_DEFAULT_TIMEOUT = 300.0
# `--tools ""` đóng tất cả các công cụ tích hợp, `--safe-mode` đóng CLAUDE.md, kỹ năng, hook,
# Tất cả các tùy chỉnh ở cấp độ người dùng như plugin, MCP, v.v. trong khi vẫn đảm bảo tính năng xác thực, lựa chọn mô hình và các quyền hoạt động bình thường.
# Cả hai đều yêu cầu CLI mới hơn; phiên bản thấp hơn sẽ thoát với "tùy chọn không xác định" và thay đổi thành lời nhắc rõ ràng từ trang web gọi điện.
CLAUDE_CODE_MIN_CLI_VERSION = "2.1.260"
# Các biến môi trường này sẽ khiến CLI sử dụng Khóa API hoặc nhà cung cấp bên thứ ba (Bedrock, Vertex, Foundry,
# Mantle, Gateway, v.v.), do đó bỏ qua việc đăng nhập đăng ký và phát sinh thêm hóa đơn. Rất dễ bỏ sót các mục nếu bạn liệt kê từng mục một.
# Hơn nữa, CLI sẽ thêm các nhà cung cấp mới trong tương lai, vì vậy toàn bộ danh mục sẽ bị loại bỏ dựa trên tiền tố:
#   ANTHOPIC_* Khóa API, Mã thông báo xác thực, URL cơ sở, các điểm cuối và hồ sơ nhà cung cấp khác nhau
#   CLAUDE_CODE_USE_* chuyển đổi nhà cung cấp
#   CLAUDE_CODE_SKIP_*_AUTH chuyển sang bỏ qua xác thực nhà cung cấp
CLAUDE_CODE_CONFLICTING_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
CLAUDE_CODE_CONFLICTING_ENV_VARS = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
)
# Hai loại biến này không thể bị loại bỏ:
#   CLAUDE_CODE_OAUTH_TOKEN là phương thức xác thực đăng ký duy nhất trong vùng chứa (không khớp với tiền tố trên);
#   *_CONFIG_DIR chỉ chỉ ra vị trí lưu trữ thông tin xác thực. Nếu bị xóa, đăng ký đã đăng nhập sẽ không còn hiệu lực.
CLAUDE_CODE_PRESERVED_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_CONFIG_DIR",
    "CLAUDE_CONFIG_DIR",
)


def _is_conflicting_claude_code_env(name: str) -> bool:
    """Xác định xem biến môi trường có chuyển CLI từ đăng nhập đăng ký sang phương thức xác thực khác hay không."""
    if name in CLAUDE_CODE_PRESERVED_ENV_VARS:
        return False
    if name in CLAUDE_CODE_CONFLICTING_ENV_VARS:
        return True
    if name.startswith(CLAUDE_CODE_CONFLICTING_ENV_PREFIXES):
        return True
    return name.startswith("CLAUDE_CODE_SKIP_") and name.endswith("_AUTH")


def coerce_claude_code_timeout(value, config_key: str = "claude_code_timeout"):
    """
    Phân tích giá trị thời gian chờ trong cấu hình thành số giây hữu hạn dương.

    TOML có thể được viết là `claude_code_timeout = 300` (int/float) hoặc dưới dạng
    `"300"` (chuỗi), vì vậy `strip()` không thể được gọi trực tiếp. nan / inf sẽ làm
    `subprocess.run(timeout=...)` bị chặn vĩnh viễn và bị từ chối tại đây.
    """
    if value is None:
        return CLAUDE_CODE_DEFAULT_TIMEOUT

    if isinstance(value, bool):
        # bool là một lớp con của int, nhưng True Seconds rõ ràng không phải là cấu hình thời gian chờ mà người dùng mong muốn.
        raise ValueError(f"{config_key} must be a number of seconds, got {value!r}")

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return CLAUDE_CODE_DEFAULT_TIMEOUT
        try:
            seconds = float(text)
        except ValueError:
            raise ValueError(
                f"{config_key} must be a number of seconds, got {value!r}"
            ) from None
    elif isinstance(value, (int, float)):
        seconds = float(value)
    else:
        raise ValueError(f"{config_key} must be a number of seconds, got {value!r}")

    if not math.isfinite(seconds):
        raise ValueError(f"{config_key} must be a finite number, got {value!r}")
    if seconds <= 0:
        raise ValueError(f"{config_key} must be greater than 0, got {value!r}")
    return seconds


def _resolve_provider_field_value(raw_value, default_value):
    """
    Chỉ dự phòng về mặc định của Sổ đăng ký khi "không được định cấu hình".

    Trước đây, việc sử dụng `raw hoặc default_value` sẽ coi các giá trị pháp lý như 0 và false là chưa được định cấu hình
    Đã thay thế: `claude_code_timeout = 0` được âm thầm thay đổi thành 300, trong khi `"0"` báo lỗi. giá trị mặc định
    Nó chỉ có hiệu lực khi Không có hoặc một chuỗi trống được sử dụng để việc xác minh cấu hình có thể nhất quán cho tất cả các phương pháp ghi.
    """
    if raw_value is None:
        return default_value
    if isinstance(raw_value, str) and not raw_value.strip():
        return default_value
    return raw_value


def build_claude_code_env(base_env=None):
    """
    Xây dựng môi trường quy trình con chỉ dựa vào thông tin đăng nhập đăng ký.

    Trả về (từ điển biến môi trường, danh sách tên biến bị loại trừ). Điều bị loại bỏ là phương thức xác thực hoặc nhà cung cấp sẽ được chuyển đổi.
    Biến `CLAUDE_CODE_OAUTH_TOKEN` phải được giữ lại: không có móc khóa nào trong vùng chứa, CLI chỉ có thể
    Dựa vào nó để hoàn thành xác thực đăng ký.
    """
    env = dict(os.environ if base_env is None else base_env)
    removed = sorted(name for name in env if _is_conflicting_claude_code_env(name))
    for name in removed:
        env.pop(name, None)
    return env, removed


def _normalize_text_response(content, llm_provider: str) -> str:
    # Các SDK LLM khác nhau có thể trả về Không có hoặc chuỗi trống trong các trường hợp bất thường hoặc bị chặn.
    # Ngay cả các đối tượng không phải chuỗi cũng được trả về. Kiểm tra toàn diện được thực hiện ở đây để tránh các cuộc gọi trực tiếp tiếp theo.
    # Các lỗi thuộc tính như `NoneType` được đưa ra khi sử dụng `.replace()`.
    if content is None:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    if not isinstance(content, str):
        raise TypeError(
            f"[{llm_provider}] returned non-text content: {type(content).__name__}"
        )

    # Các mô hình lý luận như MiniMax M3 và DeepSeek R1 có thể bao bọc lý luận nội bộ trong
    # Được trả về trong `<think>...</think>`. Tập lệnh video và từ khóa chỉ yêu cầu văn bản cuối cùng có thể đọc được,
    # Nếu lớp dịch vụ không được dọn dẹp một cách thống nhất, WebUI, phụ đề và lồng tiếng sẽ coi quá trình suy nghĩ dưới dạng văn bản.
    content = _THINK_BLOCK_RE.sub("", content)
    content = _UNCLOSED_THINK_BLOCK_RE.sub("", content).strip()
    if not content:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    # ``strip()`` trước đó đã xóa khoảng trắng ở đầu và cuối. Ở đây bạn phải giữ lại các ngắt dòng đơn và
    # Ngắt dòng đôi: Việc tạo tập lệnh dựa vào ngắt dòng đôi để phân biệt các đoạn văn và quá trình xử lý phụ đề cũng sẽ đọc bản sao của người dùng theo từng dòng.
    return content


def _sanitize_error_message(error: object) -> str:
    """
    Dọn dẹp các thông báo lỗi được trả về WebUI/API để tránh rò rỉ thông tin xác thực trong base_url tùy chỉnh.

    Một số SDK tương thích với OpenAI sẽ chèn URL yêu cầu vào thông tin ngoại lệ. Nếu người dùng muốn
    Cổng proxy được định cấu hình bằng `https://user:pass@example.com/v1` và trả về trực tiếp `str(e)`.
    Điều này sẽ hiển thị mật khẩu cho trang, người gọi API hoặc nhật ký tiếp theo. Chỉ có bản sao lỗi được xử lý ở đây và không bị thay đổi.
    Địa chỉ yêu cầu thực tế để tránh ảnh hưởng tới link gọi thông thường.
    """
    message = str(error)
    message = _URL_USERINFO_RE.sub(r"\1***:***@", message)
    message = _SENSITIVE_QUERY_RE.sub(r"\1***", message)
    return message


def _extract_chat_completion_text(response, llm_provider: str) -> str:
    # Giao diện tương thích OpenAI có thể không trả về lựa chọn nào hoặc
    # Hoặc các lựa chọn/tin nhắn/nội dung là một đối tượng phản hồi trống.
    # Việc xác minh cấu trúc được thực hiện thống nhất ở đây để tránh `NoneType không thể đăng ký`
    # Đây là loại lỗi truy cập thuộc tính cơ bản.
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"[{llm_provider}] returned empty choices")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise ValueError(f"[{llm_provider}] returned empty message")

    content = getattr(message, "content", None)
    return _normalize_text_response(content, llm_provider)


def _get_response_field(value, key: str):
    """Tương thích với việc đọc trường của các đối tượng phản hồi dict và SDK."""
    if isinstance(value, dict):
        return value.get(key)

    try:
        return value[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(value, key, None)


def _extract_qwen_generation_text(response) -> str:
    """
    Trích xuất văn bản từ phản hồi của DashScope Generation.

    Khi Qwen được gọi bằng `messages`, nó sẽ trả về cấu trúc trò chuyện:
    `output.choices[0].message.content`; chỉ có mẫu hoàn thành cũ sẽ được trả lại
    `đầu ra.text`. Cả hai đường dẫn ở đây đều tương thích để tránh `output.text` là Không có.
    Việc tiếp tục với `.replace()` sẽ gây ra một AttributionError không thể chẩn đoán được.
    """
    output = _get_response_field(response, "output")
    choices = _get_response_field(output, "choices") if output else None
    if choices is not None:
        if not choices:
            logger.warning("Qwen returned an empty choices list")
            raise ValueError("[qwen] returned empty choices")

        first_choice = choices[0]
        message = _get_response_field(first_choice, "message")
        content = _get_response_field(message, "content") if message else None
        if content is not None:
            return _normalize_text_response(content, "qwen")

    text = _get_response_field(output, "text") if output else None
    return _normalize_text_response(text, "qwen")


def _generate_response(prompt: str, app_config=None) -> str:
    try:
        # WebUI cho phép người dùng chuẩn bị bản sao tiếp theo trong quá trình tạo video. Người gọi có thể chuyển giao ngay lập tức
        # Ảnh chụp nhanh cấu hình để đảm bảo rằng tác vụ nền sẽ không kết thúc và các cấu hình mới sẽ được áp dụng trong quá trình thử lại yêu cầu mô hình.
        # Thay vào đó hãy chuyển sang Nhà cung cấp, URL cơ sở hoặc mô hình khác.
        runtime_app_config = app_config if app_config is not None else config.app
        llm_provider = str(
            runtime_app_config.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)
        ).lower()
        provider = get_llm_provider(llm_provider)
        if provider is None:
            raise ValueError(f"{llm_provider}: unsupported llm provider")

        logger.info(f"llm provider: {llm_provider}")
        api_key = runtime_app_config.get(provider.config_key("api_key"), "")
        configured_model = runtime_app_config.get(provider.config_key("model_name"), "")
        model_name = provider.resolve_model_name(configured_model)
        if configured_model and model_name != configured_model:
            logger.warning(
                f"{llm_provider} model '{configured_model}' is deprecated, "
                f"fallback to '{model_name}'"
            )
        configured_base_url = runtime_app_config.get(
            provider.config_key("base_url"), ""
        )
        base_url = provider.resolve_base_url(configured_base_url)
        if configured_base_url and configured_base_url.strip().rstrip("/") in {
            url.rstrip("/") for url in provider.deprecated_base_urls
        }:
            logger.warning(
                f"{llm_provider} base URL '{configured_base_url}' is deprecated, "
                f"fallback to '{base_url}'"
            )
        adapter = provider.adapter
        api_version = ""

        # Địa chỉ mặc định của Ollama phụ thuộc vào việc địa chỉ đó hiện có đang chạy trong vùng chứa hay không và không thể sử dụng làm sổ đăng ký tĩnh.
        # Các giá trị được lưu; Cơ quan đăng ký vẫn chịu trách nhiệm về mô hình và các quy tắc bắt buộc, đồng thời giải thích những khác biệt về môi trường thời gian chạy ở đây.
        if llm_provider == "ollama":
            api_key = "ollama"
            if not base_url:
                base_url = config.get_default_ollama_base_url()

        if adapter == "azure":
            api_version = runtime_app_config.get(
                provider.config_key("api_version"), "2024-02-15-preview"
            )

        extra_values = {
            field.config_suffix: _resolve_provider_field_value(
                runtime_app_config.get(provider.config_key(field.config_suffix)),
                field.default_value,
            )
            for field in provider.extra_fields
        }

        if provider.requires_api_key and not api_key:
            raise ValueError(
                f"{llm_provider}: api_key is not set, please set it in the config.toml file."
            )
        if provider.requires_model_name and not model_name:
            raise ValueError(
                f"{llm_provider}: model_name is not set, please set it in the config.toml file."
            )
        if provider.requires_base_url and not base_url:
            raise ValueError(
                f"{llm_provider}: base_url is not set, please set it in the config.toml file."
            )

        for field in provider.extra_fields:
            if field.required and not extra_values[field.config_suffix]:
                raise ValueError(
                    f"{llm_provider}: {field.config_suffix} is not set, "
                    "please set it in the config.toml file."
                )

        if adapter == "qwen":
            import dashscope
            from dashscope.api_entities.dashscope_response import GenerationResponse

            dashscope.api_key = api_key
            response = dashscope.Generation.call(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, GenerationResponse):
                    status_code = response.status_code
                    if status_code != 200:
                        raise Exception(
                            f'[{llm_provider}] returned an error response: "{response}"'
                        )

                    return _extract_qwen_generation_text(response)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}"'
                    )
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        if adapter == "gemini":
            from google import genai
            from google.genai import types

            http_options = types.HttpOptions(base_url=base_url) if base_url else None
            gemini_thinking = str(
                runtime_app_config.get("gemini_thinking_level", "high")
            ).strip().upper()
            thinking_config = None
            if gemini_thinking in ("HIGH", "MEDIUM", "LOW", "MINIMAL"):
                thinking_level = getattr(types.ThinkingLevel, gemini_thinking, types.ThinkingLevel.HIGH)
                thinking_config = types.ThinkingConfig(thinking_level=thinking_level)
            elif gemini_thinking in ("OFF", "DISABLE", "DISABLED", "0"):
                thinking_config = types.ThinkingConfig(thinking_budget=0)

            generation_config = types.GenerateContentConfig(
                temperature=0.5,
                top_p=1,
                top_k=1,
                max_output_tokens=2048,
                thinking_config=thinking_config,
                safety_settings=[
                    types.SafetySetting(
                        category="HARM_CATEGORY_HARASSMENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_HATE_SPEECH",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_DANGEROUS_CONTENT",
                        threshold="BLOCK_ONLY_HIGH",
                    ),
                ],
            )

            gemini_attempts = 3
            last_gemini_error = None
            generated_text = ""
            for g_idx in range(gemini_attempts):
                try:
                    # Phiên bản mới của google-genai hiển thị các dịch vụ mô hình thông qua Ứng dụng khách hợp nhất. quản lý bối cảnh
                    # Kết nối HTTP cơ bản sẽ bị đóng sau khi yêu cầu được hoàn thành để tránh tích lũy tài nguyên kết nối trong quá trình tạo thường xuyên.
                    with genai.Client(
                        api_key=api_key,
                        http_options=http_options,
                    ) as client:
                        response = client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=generation_config,
                        )
                    generated_text = response.text
                    break
                except (AttributeError, IndexError, ValueError) as e:
                    logger.warning(f"gemini returned invalid response content: {str(e)}")
                    raise ValueError(f"[{llm_provider}] returned invalid response content")
                except Exception as e:
                    last_gemini_error = e
                    err_text = str(e)
                    if any(code in err_text for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "DeadlineExceeded")) and g_idx < gemini_attempts - 1:
                        backoff = 2 * (g_idx + 1)
                        logger.warning(
                            f"gemini service spike ({err_text[:120]}), retrying in {backoff}s... (attempt {g_idx + 1}/{gemini_attempts})"
                        )
                        time.sleep(backoff)
                        continue
                    raise e

            if not generated_text and last_gemini_error:
                raise last_gemini_error

            return _normalize_text_response(generated_text, llm_provider)

        if adapter == "cloudflare_ai_gateway":
            account_id = extra_values["account_id"]
            gateway_id = extra_values["gateway_id"]
            # API REST AI Gateway được đề xuất hiện tại của Cloudflare tương thích với OpenAI SDK.
            # ID tài khoản được sử dụng để xây dựng điểm cuối hợp nhất và ID cổng được chọn thông qua tiêu đề yêu cầu; đây
            # Giao diện chuyên dụng /ai/run/{model} của Workers AI không còn được gọi nữa.
            client = OpenAI(
                api_key=api_key,
                base_url=(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
                ),
                default_headers={"cf-aig-gateway-id": gateway_id},
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "litellm":
            import litellm

            if not model_name:
                raise ValueError(
                    f"{llm_provider}: model_name is not set, please set it in the config.toml file."
                )

            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                drop_params=True,
            )

            if not response:
                raise ValueError(f"[{llm_provider}] returned empty response")
            if not getattr(response, "choices", None):
                raise ValueError(f"[{llm_provider}] returned empty response")

            return _extract_chat_completion_text(response, llm_provider)

        if adapter == "azure":
            # Azure OpenAI SDK sử dụng `azure_endpoint` và `api_version` để tạo địa chỉ yêu cầu riêng tư,
            # Bạn không thể tiếp tục sử dụng lại logic khởi tạo `base_url` tương thích OpenAI phổ biến bên dưới.
            # Tại đây, yêu cầu được hoàn thành trong nhánh Azure và được trả về ngay lập tức để tránh tình trạng dự phòng tiếp theo trên máy khách.
            # Ghi đè, khiến thông tin xác thực Azure được định cấu hình của người dùng vượt qua xác thực nhưng không được sử dụng cho yêu cầu thực tế.
            logger.info(f"requesting azure chat completion, model: {model_name}")
            client = AzureOpenAI(
                api_key=api_key,
                api_version=api_version,
                azure_endpoint=base_url,
            )
            response = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, ChatCompletion):
                    return _extract_chat_completion_text(response, llm_provider)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                        f"connection and try again."
                    )
            else:
                raise Exception(
                    f"[{llm_provider}] returned an empty response, please check your network connection and try again."
                )

        if adapter == "claude_code":
            # Đăng ký Claude (Pro/Max/Team) không cấp Khóa API và thông tin đăng nhập của họ chỉ có thể được cấp bởi
            # Khách hàng chính thức của Claude Code để bạn sử dụng. API Anthropic không được yêu cầu trực tiếp ở đây,
            # Thay vào đó, hãy gọi claude CLI đã đăng nhập cục bộ (`claude -p`) ở chế độ không đầu,
            # Việc xác thực được thực hiện bởi CLI và việc tạo tập lệnh chỉ sử dụng văn bản mà nó trả về.
            configured_cli = (extra_values.get("cli_path") or "").strip() or "claude"
            cli_path = shutil.which(configured_cli)
            if not cli_path and os.path.isfile(configured_cli):
                cli_path = configured_cli
            if not cli_path:
                raise ValueError(
                    f"{llm_provider}: claude CLI not found ('{configured_cli}'), "
                    f"install it in the runtime or set "
                    f"{provider.config_key('cli_path')} in the config.toml file."
                )

            try:
                timeout_seconds = coerce_claude_code_timeout(
                    extra_values.get("timeout"), provider.config_key("timeout")
                )
            except ValueError as timeout_error:
                raise ValueError(f"{llm_provider}: {timeout_error}") from None

            command = [
                cli_path,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--system-prompt",
                CLAUDE_CODE_SYSTEM_PROMPT,
                # Đóng tất cả các công cụ tích hợp và đảm bảo chỉ thực hiện việc tạo văn bản.
                "--tools",
                "",
                # Tắt tùy chỉnh cấp độ người dùng như CLAUDE.md, kỹ năng, hook, plugin, MCP, v.v.;
                # Việc xác thực và lựa chọn mô hình không bị ảnh hưởng (không thể sử dụng --bare, điều này sẽ tắt OAuth).
                "--safe-mode",
            ]
            # Khi tên mẫu được để trống, mẫu mặc định của CLI sẽ được sử dụng để tránh các ID mẫu được mã hóa cứng ở đây.
            # Hết hạn khi các mẫu có sẵn đăng ký thay đổi.
            if model_name:
                command += ["--model", model_name]

            cli_env, removed_env = build_claude_code_env()
            if removed_env:
                # Chỉ có tên biến được ghi lại chứ không phải giá trị để tránh ghi khóa vào nhật ký.
                logger.warning(
                    f"{llm_provider}: ignoring conflicting environment variables "
                    f"so the subscription login is used: {', '.join(removed_env)}"
                )

            logger.info(f"invoking claude cli, model: {model_name or 'cli default'}")
            # CLI sẽ đọc CLAUDE.md và cài đặt dự án trong thư mục làm việc, điều này sẽ gây ô nhiễm
            # Do đó, kết quả copywriting được cố định để thực thi trong một thư mục trống tạm thời.
            with tempfile.TemporaryDirectory() as work_dir:
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        cwd=work_dir,
                        env=cli_env,
                    )
                except subprocess.TimeoutExpired:
                    raise Exception(
                        f"[{llm_provider}] claude cli timed out after "
                        f"{timeout_seconds:.0f}s"
                    )

            # Các lỗi như không đăng nhập và sử dụng hết cũng sẽ trả về JSON (`is_error` là đúng,
            # `kết quả` có thể đọc được), nhưng mã thoát không phải là 0. Vì vậy, hãy phân tích thiết bị xuất chuẩn trước,
            # Dự phòng để thoát mã và stderr chỉ khi không có JSON.
            stdout = (completed.stdout or "").strip()
            try:
                payload = json.loads(stdout) if stdout else None
            except json.JSONDecodeError:
                payload = None

            if payload is None:
                detail = (completed.stderr or stdout or "").strip()
                if "unknown option" in detail.lower():
                    raise Exception(
                        f"[{llm_provider}] the installed claude CLI does not support "
                        f"the required isolation flags; upgrade to "
                        f"{CLAUDE_CODE_MIN_CLI_VERSION} or newer: {detail[:300]}"
                    )
                if completed.returncode != 0:
                    raise Exception(
                        f"[{llm_provider}] claude cli exited with code "
                        f"{completed.returncode}: {detail[:500]}"
                    )
                raise Exception(
                    f'[{llm_provider}] returned an invalid response: "{detail[:500]}"'
                )

            if payload.get("is_error") or completed.returncode != 0:
                reason = str(payload.get("result") or "").strip() or (
                    f"claude cli exited with code {completed.returncode}"
                )
                # Không thể thực thi tương tác/đăng nhập trong vùng chứa. Các phương thức xác thực có sẵn được cung cấp trực tiếp tại đây.
                if "login" in reason.lower():
                    reason += (
                        " (run `claude setup-token` on the host and pass the token "
                        "to the container as CLAUDE_CODE_OAUTH_TOKEN)"
                    )
                raise Exception(
                    f'[{llm_provider}] returned an error response: "{reason[:500]}"'
                )

            return _normalize_text_response(payload.get("result"), llm_provider)

        if adapter == "modelscope":
            content = ""
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"enable_thinking": False},
                stream=True,
            )
            if response:
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        content += delta.content

                if not content.strip():
                    raise ValueError("Empty content in stream response")

                return _normalize_text_response(content, llm_provider)
            else:
                raise Exception(f"[{llm_provider}] returned an empty response")

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

        response = client.chat.completions.create(
            model=model_name, messages=[{"role": "user", "content": prompt}]
        )
        if response:
            if isinstance(response, ChatCompletion):
                return _extract_chat_completion_text(response, llm_provider)
            else:
                raise Exception(
                    f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                    f"connection and try again."
                )
        else:
            raise Exception(
                f"[{llm_provider}] returned an empty response, please check your network connection and try again."
            )

    except Exception as e:
        return f"Error: {_sanitize_error_message(e)}"


def test_connection() -> tuple[bool, str, float]:
    """
    Sử dụng cấu hình Nhà cung cấp hiện tại để bắt đầu một yêu cầu tối thiểu nhằm xác minh xem liên kết được tạo thực tế có khả dụng hay không.

    Kiểm tra kết nối trực tiếp sử dụng lại `_generate_response()`, do đó, nó sẽ bao gồm Khóa API, URL cơ sở,
    Tên mẫu và các trường dành riêng cho Nhà cung cấp nhưng sẽ không nhập logic thử lại do tập lệnh tạo ra và sẽ không được gửi.
    Chủ đề video hoặc bản sao của người dùng. Giá trị trả về là trạng thái thành công, thông tin lỗi và thời gian yêu cầu.
    """
    started_at = perf_counter()
    response = _generate_response(prompt="Reply with exactly: OK")
    elapsed = perf_counter() - started_at

    if not response:
        error_message = "LLM returned an empty response"
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    if response.startswith("Error:"):
        error_message = response.removeprefix("Error:").strip()
        logger.warning(f"llm connection test failed: {error_message}")
        return False, error_message, elapsed

    logger.info(f"llm connection test succeeded, elapsed: {elapsed:.2f}s")
    return True, "", elapsed


def _limit_script_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # Lớp API đã sử dụng Pydantic để xác minh độ dài; chúng tôi tiếp tục che nó ở đây để bảo vệ.
    # Khi WebUI hoặc các dịch vụ nội bộ gọi trực tiếp generate_script, các từ nhắc quá dài sẽ không được gửi tới mô hình.
    # Tránh các trường hợp ngoại lệ về chi phí mã thông báo và lỗi yêu cầu.
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _normalize_script_paragraph_number(paragraph_number: int | None) -> int:
    try:
        value = int(paragraph_number or MIN_SCRIPT_PARAGRAPH_NUMBER)
    except (TypeError, ValueError):
        value = MIN_SCRIPT_PARAGRAPH_NUMBER

    if value < MIN_SCRIPT_PARAGRAPH_NUMBER or value > MAX_SCRIPT_PARAGRAPH_NUMBER:
        # Cả WebUI và API sẽ giới hạn phạm vi; các cuộc gọi nội bộ được xử lý ở đây để tránh mở rộng trực tiếp các tham số bất thường.
        # LLM tạo ra chi phí hoặc tạo ra kết quả trống rỗng.
        logger.warning(
            f"script paragraph_number is out of range and will be clamped: {value}"
        )
        return max(MIN_SCRIPT_PARAGRAPH_NUMBER, min(value, MAX_SCRIPT_PARAGRAPH_NUMBER))

    return value


def build_script_prompt(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )

    # Tách riêng "quy tắc tạo tập lệnh" và "bối cảnh thời gian chạy". Điều này cho phép người dùng nâng cao ghi đè mặc định
    # lời nhắc của hệ thống và sẽ không bỏ lỡ các thông số về chủ đề video, ngôn ngữ và số đoạn văn phải có mỗi khi tạo.
    prompt = custom_system_prompt or DEFAULT_SCRIPT_SYSTEM_PROMPT
    prompt += f"""

# Initialization:
- video subject: {video_subject}
- number of paragraphs: {paragraph_number}
""".rstrip()
    if language:
        prompt += f"\n- language: {language}"
    if video_script_prompt:
        prompt += f"""

# Additional User Requirements:
{video_script_prompt}
""".rstrip()

    return prompt


def generate_script(
    video_subject: str,
    language: str = "",
    paragraph_number: int = 1,
    video_script_prompt: str = "",
    custom_system_prompt: str = "",
    app_config=None,
) -> str:
    paragraph_number = _normalize_script_paragraph_number(paragraph_number)
    video_script_prompt = _limit_script_text(
        video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt"
    )
    custom_system_prompt = _limit_script_text(
        custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt"
    )
    prompt = build_script_prompt(
        video_subject=video_subject,
        language=language,
        paragraph_number=paragraph_number,
        video_script_prompt=video_script_prompt,
        custom_system_prompt=custom_system_prompt,
    )
    final_script = ""
    logger.info(
        "generating video script: "
        f"subject={video_subject}, paragraph_number={paragraph_number}, "
        f"has_custom_prompt={bool(video_script_prompt.strip())}, "
        f"has_custom_system_prompt={bool(custom_system_prompt.strip())}"
    )

    def format_response(response):
        # Clean the script
        # Remove asterisks, hashes
        response = response.replace("*", "")
        response = response.replace("#", "")

        # Remove markdown syntax.  Use non-greedy .*? so each bracket/paren
        # group is removed independently; the greedy form would eat all text
        # between the first opener and the last closer on the same line.
        response = re.sub(r"\[.*?\]", "", response)
        response = re.sub(r"\(.*?\)", "", response)

        # Split the script into paragraphs
        paragraphs = response.split("\n\n")

        # Select the specified number of paragraphs
        # selected_paragraphs = paragraphs[:paragraph_number]

        # Join the selected paragraphs into a single string
        return "\n\n".join(paragraphs)

    for i in range(_max_retries):
        try:
            if app_config is None:
                response = _generate_response(prompt=prompt)
            else:
                response = _generate_response(prompt=prompt, app_config=app_config)
            if response and not response.startswith("Error:"):
                final_script = format_response(response)
                # Some upstream providers may return quota errors as plain text.
                if final_script and "当日额度已消耗完" in final_script:
                    raise ValueError(final_script)

                if final_script:
                    break
            else:
                err_detail = response if response else "empty response"
                logger.warning(f"generation attempt {i + 1} failed: {err_detail}")
                time.sleep(1.5 * (i + 1))
        except Exception as e:
            logger.error(f"failed to generate script: {e}")
            time.sleep(1.5 * (i + 1))

        if i < _max_retries - 1:
            logger.warning(f"retrying video script generation... attempt {i + 2}")
    if "Error: " in final_script:
        logger.error(f"failed to generate video script: {final_script}")
    else:
        logger.success(f"completed: \n{final_script}")
    return final_script.strip()


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding markdown code fence from an LLM response.

    Non-OpenAI providers (Claude, Gemini, …) frequently wrap JSON output in a
    ```json … ``` fence even when asked to return raw JSON. Removing it lets the
    first json.loads() succeed instead of falling through to the regex recovery
    path (and spuriously logging a warning). Mirrors the DOTALL handling already
    used in _parse_social_metadata().
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def generate_terms(
    video_subject: str,
    video_script: str,
    amount: int = 5,
    match_script_order: bool = False,
    app_config=None,
    character_prompt: str = "",
) -> List[str]:
    video_script = utils.remove_pause_tags(video_script or "").strip()
    if match_script_order:
        goal = (
            f"Generate {amount} chronological stock-video search terms that follow "
            "the order of topics in the video script."
        )
        ordering_rule = (
            "6. keep the terms in the same order as the script narration; "
            "earlier terms must describe earlier visual moments."
        )
        # Trong chế độ từ khóa được sắp xếp, số lượng ví dụ phải phù hợp với số lượng để tránh mô hình bị cố định.
        # 4 ví dụ trên gây hiểu lầm, dẫn đến bản sao dài chỉ trả lại một số lượng nhỏ từ khóa, ảnh hưởng đến phạm vi nội dung.
        example_terms = [
            "opening visual topic",
            *[f"script visual topic {index}" for index in range(2, max(amount, 1))],
            "final visual topic",
        ]
        output_example = json.dumps(example_terms[:amount], ensure_ascii=False)
    else:
        goal = (
            f"Generate {amount} search terms for stock videos, depending on the "
            "subject of a video."
        )
        ordering_rule = ""
        output_example = (
            '["search term 1", "search term 2", "search term 3",'
            '"search term 4", "search term 5"]'
        )

    character_rule = ""
    if character_prompt:
        character_rule = (
            f"7. The video features a consistent main character: '{character_prompt}'. "
            f"Every visual search term must feature this exact character in that scene's action or setting."
        )

    prompt = f"""
# Role: Video Search Terms Generator

## Goals:
{goal}

## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. you must only return the json-array of strings. you must not return anything else. you must not return the script.
4. the search terms must be related to the subject of the video.
5. reply with english search terms only.
{ordering_rule}
{character_rule}

## Output Example:
{output_example}

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}

Please note that you must use English for generating video search terms; Chinese is not accepted.
""".strip()

    logger.info(f"subject: {video_subject}, match_script_order: {match_script_order}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        try:
            if app_config is None:
                response = _generate_response(prompt)
            else:
                response = _generate_response(prompt, app_config=app_config)
            if response.startswith("Error: "):
                logger.warning(
                    f"attempt {i + 1}/{_max_retries} to generate terms returned error: {response}"
                )
                if i < _max_retries - 1:
                    time.sleep(2 * (i + 1))
                    continue
                else:
                    logger.error(f"failed to generate video terms after {_max_retries} attempts: {response}")
                    return []
            search_terms = json.loads(_strip_code_fence(response))
            if not isinstance(search_terms, list) or not all(
                isinstance(term, str) for term in search_terms
            ):
                logger.error("response is not a list of strings.")
                continue

        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            if response:
                match = re.search(r"\[.*]", response, re.DOTALL)
                if match:
                    try:
                        search_terms = json.loads(match.group())
                    except Exception as e:
                        # Quá trình thử lại được giữ lại ở đây, nhưng JSON không chuẩn được LLM trả về phải được ghi lại.
                        # Nếu không, việc khắc phục sự cố tiếp theo sẽ không thể xác định được nếu cụm từ tìm kiếm trống.
                        # Đây có phải là vấn đề về định dạng mô hình hay vấn đề logic phân tích cú pháp không?
                        logger.warning(f"failed to generate video terms: {str(e)}")

        if search_terms and len(search_terms) > 0:
            break
        if i < _max_retries - 1:
            logger.warning(f"failed to generate video terms, trying again... {i + 1}")

    logger.success(f"completed: \n{search_terms}")
    return search_terms


# =============================================================================
# Social publishing metadata
#
# Tạo tiêu đề, chú thích và thẻ bắt đầu bằng # thường được sử dụng khi xuất bản lên nền tảng video ngắn dựa trên chủ đề và tập lệnh video.
# Khả năng này chỉ sử dụng lại nhà cung cấp LLM hiện có, không kết nối với bất kỳ dịch vụ xuất bản bên ngoài nào và không ảnh hưởng đến liên kết tạo video chính.
# =============================================================================

# Các nền tảng khác nhau có các tùy chọn khác nhau về độ dài bản sao và số lượng hashtag. Giới hạn trên thận trọng được sử dụng ở đây để tránh trả về mô hình
# Nếu nội dung quá dài, người gọi sẽ phải cắt nội dung đó hai lần.
SOCIAL_PLATFORMS = {
    "tiktok": {"title_max": 100, "caption_max": 2200, "hashtag_count": 5},
    "youtube_shorts": {"title_max": 100, "caption_max": 5000, "hashtag_count": 3},
    "instagram_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 8},
    "facebook_reels": {"title_max": 125, "caption_max": 2200, "hashtag_count": 5},
}
DEFAULT_SOCIAL_PLATFORM = "tiktok"
DEFAULT_SOCIAL_LANGUAGE = "auto"
MAX_SOCIAL_SUBJECT_LENGTH = 500
MAX_SOCIAL_SCRIPT_LENGTH = 8000
MAX_SOCIAL_LANGUAGE_LENGTH = 64

SOCIAL_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "instagram_reels": "Instagram Reels",
    "facebook_reels": "Facebook Reels",
}

# Nhãn tổng hợp chung khi LLM không có sẵn. Điều này cố ý không bị ràng buộc với một quốc gia hoặc ngôn ngữ nhất định để đảm bảo rằng API
# Các cấu trúc có sẵn có thể được trả về cho các kịch bản khác nhau như tiếng Trung, tiếng Anh và tiếng Việt.
DEFAULT_SOCIAL_HASHTAGS = [
    "#shorts",
    "#viral",
    "#trending",
    "#fyp",
    "#video",
    "#reels",
    "#creator",
    "#content",
]


def _resolve_social_platform(platform: str | None) -> str:
    value = (platform or "").strip().lower()
    return value if value in SOCIAL_PLATFORMS else DEFAULT_SOCIAL_PLATFORM


def _normalize_social_language(language: str | None) -> str:
    value = (language or DEFAULT_SOCIAL_LANGUAGE).strip()
    if len(value) > MAX_SOCIAL_LANGUAGE_LENGTH:
        logger.warning(
            "social metadata language is too long and will be truncated to "
            f"{MAX_SOCIAL_LANGUAGE_LENGTH} characters."
        )
        value = value[:MAX_SOCIAL_LANGUAGE_LENGTH]
    return value or DEFAULT_SOCIAL_LANGUAGE


def _limit_social_text(text: str | None, max_length: int, field_name: str) -> str:
    value = (text or "").strip()
    if len(value) <= max_length:
        return value

    # Lớp API sẽ giới hạn độ dài; tiếp tục đề cập đến vấn đề này ở đây là để bảo vệ các cuộc gọi nội bộ hoặc WebUI trong tương lai
    # Khi gọi trực tiếp, nội dung quá dài sẽ không được gửi đến mô hình để tránh sự bất thường về chi phí mã thông báo.
    logger.warning(
        f"{field_name} is too long and will be truncated to {max_length} characters."
    )
    return value[:max_length]


def _social_language_instruction(language: str | None) -> str:
    language = _normalize_social_language(language)
    if language.lower() == DEFAULT_SOCIAL_LANGUAGE:
        return (
            "Use the same language as the video subject and script. If the subject "
            "and script use different languages, prefer the script language."
        )

    return f'Write "title" and "caption" in this language: {language}.'


def _clamp_text(text, max_length: int) -> str:
    value = ("" if text is None else str(text)).strip()
    if max_length and len(value) > max_length:
        return value[:max_length].rstrip()
    return value


def _normalize_hashtags(raw, count: int) -> List[str]:
    """
    Thống nhất các hashtag được LLM trả về thành định dạng `#tag`.

    LLM có thể trả về chuỗi, mảng, cụm từ có dấu cách, thẻ lặp lại hoặc nội dung chứa dấu câu.
    Việc dọn dẹp tập trung ở đây có thể làm cho cấu trúc phản hồi giao diện ổn định và tránh các thẻ trống và nhãn trống khi nền tảng được phát hành.
    Thẻ trùng lặp hoặc thẻ bắt đầu bằng # không theo định dạng chung.
    """
    if isinstance(raw, str):
        candidates = re.split(r"[\s,]+", raw)
    elif isinstance(raw, (list, tuple)):
        # Mỗi mục trong mảng được coi là một nhãn hoàn chỉnh, do đó "du lich" trở thành
        # "#dulich" thay vì chia thành hai thẻ.
        candidates = [str(entry) for entry in raw]
    else:
        candidates = []

    seen = set()
    result: List[str] = []
    for item in candidates:
        tag = re.sub(r"[^\w]", "", item, flags=re.UNICODE)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(f"#{tag}")
        if count and len(result) >= count:
            break
    return result


def build_social_metadata_prompt(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> str:
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    platform = _resolve_social_platform(platform)
    spec = SOCIAL_PLATFORMS[platform]
    label = SOCIAL_PLATFORM_LABELS.get(platform, platform)
    language_instruction = _social_language_instruction(language)

    prompt = f"""
# Role: Short-Video Social Media Copywriter

## Goal
Write engaging publishing metadata for a short video that will be posted on {label}.

## Constraints
1. Respond ONLY with a single valid minified JSON object. No markdown, no code fences, no commentary.
2. The JSON must contain exactly these keys: "title", "caption", "hashtags".
3. "title": a catchy hook, at most {spec["title_max"]} characters.
4. "caption": an engaging description that ends with a call to action, at most {spec["caption_max"]} characters. Do not put hashtags inside the caption.
5. "hashtags": a JSON array of exactly {spec["hashtag_count"]} strings. Each must start with "#", contain no spaces, and be relevant to the topic and to {label}.
6. {language_instruction}

## Output Example
{{"title":"...","caption":"...","hashtags":["#example","#video"]}}

## Context
### Video Subject
{video_subject}

### Video Script
{video_script}
""".strip()
    return prompt


def _parse_social_metadata(response: str, platform: str) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]

    data = None
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception:
        # Một số mô hình sẽ bao bọc văn bản mô tả hoặc hàng rào đánh dấu trong JSON.
        # Trình gọi API chỉ cần cấu trúc ổn định, vì vậy ở đây chúng tôi cố gắng trích xuất đối tượng JSON đầu tiên.
        match = re.search(r"\{.*\}", response or "", re.DOTALL)
        if match:
            data = json.loads(match.group())

    if not isinstance(data, dict):
        raise ValueError("social metadata response is not a JSON object")

    title = _clamp_text(data.get("title", ""), spec["title_max"])
    caption = _clamp_text(data.get("caption", ""), spec["caption_max"])
    hashtags = _normalize_hashtags(data.get("hashtags", []), spec["hashtag_count"])

    if not title and not caption:
        raise ValueError("social metadata response is missing both title and caption")

    return {"title": title, "caption": caption, "hashtags": hashtags}


def _fallback_social_metadata(
    video_subject: str, video_script: str, platform: str
) -> dict:
    spec = SOCIAL_PLATFORMS[_resolve_social_platform(platform)]
    subject = (video_subject or "").strip()
    script = (video_script or "").strip()

    title = subject
    if not title and script:
        # Khi không có chủ đề, hãy sử dụng câu đầu tiên của tập lệnh để tạo tiêu đề nhằm tránh giao diện trả về tiêu đề trống.
        title = re.split(r"(?<=[.!?。！？])\s+", script)[0]

    return {
        "title": _clamp_text(title, spec["title_max"]),
        "caption": _clamp_text(script or subject, spec["caption_max"]),
        "hashtags": _normalize_hashtags(DEFAULT_SOCIAL_HASHTAGS, spec["hashtag_count"]),
    }


def generate_social_metadata(
    video_subject: str,
    video_script: str = "",
    language: str = DEFAULT_SOCIAL_LANGUAGE,
    platform: str = DEFAULT_SOCIAL_PLATFORM,
) -> dict:
    """
    Tạo siêu dữ liệu copywriting xuất bản video ngắn.

    Cấu trúc trả về được cố định thành `{"title": str, "caption": str, "hashtags": List[str]}`.
    Nếu LLM không khả dụng hoặc trả về định dạng bất thường, nó sẽ bị hạ cấp xuống kết quả phỏng đoán chung để đảm bảo rằng API
    Người gọi luôn nhận được cấu trúc dữ liệu có thể được hiển thị và chỉnh sửa trước khi xuất bản.
    """
    platform = _resolve_social_platform(platform)
    language = _normalize_social_language(language)
    video_subject = _limit_social_text(
        video_subject, MAX_SOCIAL_SUBJECT_LENGTH, "video_subject"
    )
    video_script = _limit_social_text(
        video_script, MAX_SOCIAL_SCRIPT_LENGTH, "video_script"
    )
    prompt = build_social_metadata_prompt(
        video_subject=video_subject,
        video_script=video_script,
        language=language,
        platform=platform,
    )
    logger.info(f"generating social metadata: platform={platform}, language={language}")

    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and "Error: " in response:
                logger.error(f"failed to generate social metadata: {response}")
                break
            metadata = _parse_social_metadata(response, platform)
            logger.success(f"completed: \n{metadata}")
            return metadata
        except Exception as e:
            logger.warning(f"failed to parse social metadata: {str(e)}")

        if i < _max_retries - 1:
            logger.warning(
                f"failed to generate social metadata, trying again... {i + 1}"
            )

    logger.warning("falling back to heuristic social metadata")
    return _fallback_social_metadata(video_subject, video_script, platform)


def extract_character_profile_from_image(image_path: str, app_config=None) -> tuple[bool, str]:
    """
    Sử dụng Gemini Vision để phân tích ảnh người mẫu và trích xuất
    đoạn mô tả nhận dạng nhân vật (Character Anchor Prompt) đồng nhất.
    """
    if not os.path.isfile(image_path):
        return False, "File ảnh không tồn tại."

    runtime_app_config = app_config if app_config is not None else config.app
    api_key = str(runtime_app_config.get("gemini_api_key", "") or "").strip()
    if not api_key:
        return False, "Chưa cấu hình gemini_api_key trong config.toml."

    try:
        from google import genai
        from PIL import Image

        client = genai.Client(api_key=api_key)
        img = Image.open(image_path)
        prompt = (
            "Analyze this person in detail for visual consistency in AI image generation. "
            "Identify: age, gender, ethnicity, facial features (eyes, nose, lips), "
            "hairstyle and color, skin tone, body build, and default clothing style. "
            "Output ONLY a single concise, high-detail English visual description (max 35 words) "
            "that can be appended to AI image prompts to reproduce this exact character consistently. "
            "Example format: 'young 22-year-old Vietnamese female model, slender, delicate oval face, "
            "long wavy dark brown hair, warm eyes, elegant chic outfit'."
        )
        for model in ["gemini-3.6-flash", "gemini-2.5-flash"]:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[img, prompt],
                )
                text = response.text.strip().strip('"').strip("'")
                if text:
                    return True, text
            except Exception as me:
                logger.warning(f"Vision model {model} attempt failed: {me}")
                continue

        return False, "Không nhận được phản hồi mô tả từ Gemini Vision."
    except Exception as e:
        logger.error(f"Lỗi trích xuất nhận dạng nhân vật: {e}")
        return False, f"Lỗi: {str(e)}"



if __name__ == "__main__":
    video_subject = "生命的意义是什么"
    script = generate_script(
        video_subject=video_subject, language="zh-CN", paragraph_number=1
    )
    print("######################")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print(search_terms)
