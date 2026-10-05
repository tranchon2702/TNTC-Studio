import ast
import json
import re
import unittest
from pathlib import Path

from app.models.llm_provider import get_llm_provider
from app.utils import utils


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
LLM_PROVIDER_TIPS_PREFIX = "llm_provider_tips."
TTS_PROVIDER_TIPS_PREFIX = "tts_provider_tips."
SECONDARY_LOCALES = ("az", "de", "es", "fr", "id", "it", "ko", "pt", "ru", "tr", "vi")
PROVIDER_TIPS_PREFIXES = (
    LLM_PROVIDER_TIPS_PREFIX,
    TTS_PROVIDER_TIPS_PREFIX,
)
# Tên thương hiệu và mô tả dài về nhà cung cấp hợp tác xã chỉ được duy trì bằng tiếng Trung và tiếng Anh. Ngôn ngữ thứ cấp thống nhất trở lại tiếng Anh.
# Tránh trùng lặp chính xác cùng một tên thương hiệu mười lần và tránh những mô tả dài dòng chỉ cập nhật một phần ngôn ngữ sau này.
ENGLISH_FALLBACK_KEYS = frozenset(
    {
        "AI Video Quote Required",
        "AI Video Quote Retained For Retry",
        "AI Video Quote Estimate Incomplete",
        "AI Video Quote Summary",
        "AI Video Quote Summary Singular",
        "AI Video Model",
        "AI Video Model Reference Price",
        "AI Video Model List Load Failed",
        "AI Video Duration Basis Actual",
        "AI Video Duration Basis Estimated",
        "AI Video Material Coverage",
        "AI Video Scene Count",
        "Confirm AI Video Charge",
        "Confirm AI Video Charge Help",
        "Confirm AI Video Charge Required",
        "Custom API Endpoint",
        "API Platform",
        "llm_provider_endpoint_selector.moonshot",
        "llm_provider_endpoint_selector_help.moonshot",
        "llm_provider_endpoint.moonshot.china",
        "llm_provider_endpoint.moonshot.global",
        "llm_provider_authentication_error.moonshot",
        "Local LLM Script Generation",
        "llm_provider_label.apimart",
        "llm_provider_label.openrouter",
        "llm_provider_label.shengsuanyun",
        "LoomLoom Poll Retry Pending",
        "LoomLoom Poll Retry Warning",
        "Resume LoomLoom Status Check",
        "Refresh AI Video Models",
        "Retry AI Video Quote",
        "LoomLoom Quote Summary Singular",
        "LoomLoom Video Terms Reuse Help",
        "Metaso MiniMax H3",
        "Metaso MiniMax H3 Help",
        "Metaso MiniMax API Key",
        "Metaso MiniMax API Key Help",
        "Metaso MiniMax Base URL",
        "Metaso MiniMax Resolution",
        "Metaso MiniMax Resolution Help",
        "Metaso MiniMax Invalid Resolution",
        "Select Metaso MiniMax Resolution",
        "Please Enter the Metaso MiniMax API Key",
        "Metaso MiniMax Billing Notice",
        "Metaso MiniMax Billing Notice Uploaded Audio",
        "Metaso MiniMax Billing Notice Without Script",
        "Confirm Metaso MiniMax Charge",
        "Confirm Metaso MiniMax Charge Help",
        "Confirm Metaso MiniMax Charge Required",
        "Script Generation Method",
        "Script Generation Method Help",
        "Shengsuan Cloud AI Video",
        "Shengsuan Cloud AI Video Help",
        "Shengsuan Cloud API Key",
        "Shengsuan Cloud API Key Help",
        "Shengsuan Cloud API Key Link",
        "Shengsuan Cloud API Key Placeholder",
        "Shengsuan Cloud API Key Required",
        "Shengsuan Cloud API Key Reused",
        "Shengsuan Cloud Batch Script Generation",
        "Selected AI Video Model Unavailable",
        "Selected AI Video Ratio Unavailable",
        "Stop Tracking LoomLoom Run",
        "Stop Tracking LoomLoom Run Help",
        "Unavailable AI Video Model",
    }
)
FORMAT_PLACEHOLDER_PATTERN = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")
MARKDOWN_URL_PATTERN = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")


class _TrKeyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.keys = set()

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.keys.add(node.args[0].value)
        self.generic_visit(node)


def _load_translation(locale):
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data.get("Translation", {})


def _required_translation_keys(translations):
    """Trả về khóa phải được duy trì bằng ngôn ngữ cấp hai và mô tả dài về Nhà cung cấp sẽ chuyển sang tiếng Anh."""
    return {
        key
        for key in translations
        if key not in ENGLISH_FALLBACK_KEYS
        and not key.startswith(PROVIDER_TIPS_PREFIXES)
    }


def _format_placeholders(value):
    """Trích xuất các biến được định dạng trong thời gian chạy để tránh tên biến bị thiếu hoặc không chính xác trong bản dịch."""
    return set(FORMAT_PLACEHOLDER_PATTERN.findall(value))


def _markdown_urls(value):
    """Trích xuất các mục tiêu liên kết Markdown, cho phép dịch văn bản liên kết nhưng không thay đổi địa chỉ."""
    return set(MARKDOWN_URL_PATTERN.findall(value))


class TestWebuiI18n(unittest.TestCase):
    def test_saved_ui_language_takes_priority_over_browser_locale(self):
        language = utils.resolve_ui_language(
            saved_language="de",
            browser_locale="zh-CN",
            supported_languages=["zh", "en", "de"],
        )

        self.assertEqual(language, "de")

    def test_browser_locale_is_normalized_to_supported_base_language(self):
        self.assertEqual(
            utils.resolve_ui_language("", "zh-CN", ["zh", "en"]),
            "zh",
        )
        self.assertEqual(
            utils.resolve_ui_language(None, "pt_BR", ["en", "pt"]),
            "pt",
        )

    def test_unsupported_browser_locale_falls_back_to_english(self):
        language = utils.resolve_ui_language(
            saved_language="",
            browser_locale="fr-FR",
            supported_languages=["zh", "en"],
        )

        self.assertEqual(language, "en")

    def test_english_locale_covers_static_webui_labels(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        en_keys = set(_load_translation("en"))

        self.assertEqual(sorted(visitor.keys - en_keys), [])

    def test_shengsuanyun_provider_tips_keep_registration_and_model_links(self):
        """Cổng hợp tác và thư mục mẫu thuộc về cấu hình sản phẩm, để tránh vô tình xóa liên kết theo dõi khi thay đổi bản sao sau này."""
        expected_urls = {
            "https://www.shengsuanyun.com/?from=CH_XUQ4OTSK",
            "https://global.modelmesh.info/model",
        }

        for locale in ("zh", "en"):
            with self.subTest(locale=locale):
                tips = _load_translation(locale)["llm_provider_tips.shengsuanyun"]
                provider = get_llm_provider("shengsuanyun")
                rendered = tips.format(
                    api_key_url=provider.effective_api_key_url(),
                    default_base_url=provider.effective_default_base_url,
                    default_model=provider.default_model,
                )
                self.assertEqual(_markdown_urls(rendered), expected_urls)

    def test_metaso_api_key_label_keeps_mpt_referral_link(self):
        """Lối vào mua lại Secret Tower Key phải giữ lại các tham số theo dõi MPT để tránh lỗi liên kết chuyển đổi tài trợ."""
        expected_url = "https://metaso.cn/minimax-h3/?s=MPT"

        for locale in ("zh", "en"):
            with self.subTest(locale=locale):
                label = _load_translation(locale)["Metaso MiniMax API Key"]
                self.assertEqual(_markdown_urls(label), {expected_url})

    def test_secondary_locales_cover_english_locale(self):
        en_translations = _load_translation("en")
        required_en_keys = _required_translation_keys(en_translations)

        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(sorted(required_en_keys - locale_keys), [])

    def test_secondary_locales_do_not_duplicate_provider_tips(self):
        # Mô tả dài về cấu hình của nhà cung cấp chỉ được duy trì bằng tiếng Trung và tiếng Anh và quay lại tiếng Anh khi chạy bằng các ngôn ngữ khác.
        # Việc sao chép các khóa này bị cấm để tránh nội dung bán dịch sẽ không được duy trì liên tục.
        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                duplicated_keys = sorted(
                    key for key in locale_keys if key.startswith(PROVIDER_TIPS_PREFIXES)
                )
                self.assertEqual(duplicated_keys, [])

    def test_secondary_locales_do_not_duplicate_english_fallback_keys(self):
        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(sorted(ENGLISH_FALLBACK_KEYS & locale_keys), [])

    def test_secondary_locales_cover_static_webui_labels(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(
                    sorted(visitor.keys - locale_keys - ENGLISH_FALLBACK_KEYS),
                    [],
                )

    def test_secondary_locales_preserve_format_placeholders(self):
        en_translations = _load_translation("en")

        for locale in SECONDARY_LOCALES:
            locale_translations = _load_translation(locale)
            for key in _required_translation_keys(en_translations):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _format_placeholders(locale_translations[key]),
                        _format_placeholders(en_translations[key]),
                    )

    def test_secondary_locales_preserve_markdown_urls(self):
        en_translations = _load_translation("en")

        for locale in SECONDARY_LOCALES:
            locale_translations = _load_translation(locale)
            for key in _required_translation_keys(en_translations):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _markdown_urls(locale_translations[key]),
                        _markdown_urls(en_translations[key]),
                    )

    def test_script_language_options_include_russian(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        support_locales = None

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "support_locales"
                for target in node.targets
            ):
                support_locales = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(support_locales)
        self.assertIn("ru-RU", support_locales)
