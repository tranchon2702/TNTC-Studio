// Background service worker for TNTC Studio Extension

chrome.runtime.onInstalled.addListener(() => {
  // Tạo menu chuột phải trên bất kỳ ảnh sản phẩm nào
  chrome.contextMenus.create({
    id: "tntc_send_to_gemini",
    title: "👗 Gửi ảnh mẫu đồ này sang Gemini (TNTC Studio)",
    contexts: ["image"]
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "tntc_send_to_gemini" && info.srcUrl) {
    // Lưu link ảnh vào storage và mở Gemini
    chrome.storage.local.set({ pendingGarmentUrl: info.srcUrl }, () => {
      chrome.tabs.create({ url: "https://gemini.google.com/app" });
    });
  }
});

// Lắng nghe yêu cầu tải ảnh từ content_gemini
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "download_tntc_image") {
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = `TNTC_Studio/models/${request.filename || `model_${timestamp}.png`}`;

    chrome.downloads.download({
      url: request.imageUrl,
      filename: filename,
      saveAs: false
    }, (downloadId) => {
      sendResponse({ success: true, downloadId });
    });
    return true; // async response
  }
});
