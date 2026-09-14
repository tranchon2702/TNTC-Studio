document.addEventListener("DOMContentLoaded", () => {
  const outfitDesc = document.getElementById("outfit-desc");
  const modelStyle = document.getElementById("model-style");
  const btnOpenGemini = document.getElementById("btn-open-gemini");
  const btnOpenStudio = document.getElementById("btn-open-studio");

  btnOpenGemini.addEventListener("click", () => {
    const outfit = outfitDesc.value.trim() || "váy thời trang nữ tính hot trend Shopee";
    const styleVal = modelStyle.value;

    let styleDesc = "cực kỳ xinh đẹp và quyến rũ, dáng chuẩn đồng hồ cát";
    if (styleVal === "cute") styleDesc = "dễ thương, năng động, phong cách idol Hàn/Việt";
    if (styleVal === "lookbook") styleDesc = "thần thái người mẫu thời trang quốc tế, biểu cảm tự nhiên cao cấp";
    if (styleVal === "streetwear") styleDesc = "cá tính, năng động hiện đại, phong cách đường phố trendy";

    const fullPrompt = `Tạo một bức ảnh chụp toàn thân tỉ lệ dọc 9:16 của một cô gái người mẫu Việt Nam 20 tuổi ${styleDesc}, đang mặc trang phục: ${outfit}. Chụp tại studio thời trang hiện đại với ánh sáng tự nhiên dịu nhẹ, phong cách Lookbook ảnh thật chân thực, độ chi tiết cao, không méo mó, 8k, không watermark.`;

    // Lưu prompt vào storage để content script trên Gemini tự chèn vào
    chrome.storage.local.set({ pendingPrompt: fullPrompt }, () => {
      // Sao chép sẵn vào clipboard để dự phòng
      navigator.clipboard.writeText(fullPrompt).catch(() => {});

      // Mở hoặc chuyển tab sang Gemini
      chrome.tabs.create({ url: "https://gemini.google.com/app" });
    });
  });

  btnOpenStudio.addEventListener("click", () => {
    chrome.tabs.create({ url: "http://127.0.0.1:8501" });
  });
});
