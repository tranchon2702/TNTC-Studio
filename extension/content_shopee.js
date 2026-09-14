// Content script on Shopee / TikTok Shop
(function () {
  console.log("👗 TNTC Studio Helper: Sẵn sàng bắt ảnh đồ mẫu từ Shopee/TikTok!");

  // Cho phép lưu nhanh ảnh sản phẩm khi rê chuột
  document.addEventListener("contextmenu", (e) => {
    let target = e.target;
    let imgUrl = null;

    if (target.tagName === "IMG") {
      imgUrl = target.src;
    } else {
      const img = target.querySelector("img");
      if (img) imgUrl = img.src;
      else {
        // Kiểm tra background-image
        const bg = window.getComputedStyle(target).backgroundImage;
        if (bg && bg.startsWith('url("')) {
          imgUrl = bg.slice(5, -2);
        }
      }
    }

    if (imgUrl) {
      chrome.storage.local.set({ pendingGarmentUrl: imgUrl });
    }
  }, true);
})();
