// TNTC Studio - Gemini Web Content Script
(function () {
  console.log("🎬 TNTC Studio Extension đã kích hoạt trên Gemini!");

  // 1. Tạo Floating Toolbar điều khiển nhanh
  function createFloatingToolbar() {
    if (document.getElementById("tntc-floating-panel")) return;

    const panel = document.createElement("div");
    panel.id = "tntc-floating-panel";
    panel.innerHTML = `
      <div class="tntc-header">
        <span class="tntc-title">🎬 TNTC Studio Helper</span>
        <button id="tntc-toggle-btn" title="Ẩn/Hiện">−</button>
      </div>
      <div class="tntc-body" id="tntc-panel-body">
        <div class="tntc-section-title">👗 Prompt Thời Trang TikTok:</div>
        
        <button class="tntc-prompt-btn" data-type="dress">
          💃 Mẫu Nữ Váy Body / Mặc Đồ Shopee
        </button>

        <button class="tntc-prompt-btn" data-type="dance">
          🎵 Mẫu Nhảy TikTok Gợi Cảm (Góc 9:16)
        </button>

        <button class="tntc-prompt-btn" data-type="lookbook">
          📸 Chụp Lookbook Chân Thực Siêu Nét
        </button>

        <div id="tntc-garment-preview" style="display:none;">
          <div class="tntc-section-title">Ảnh đồ mẫu từ Shopee:</div>
          <img id="tntc-garment-img" src="" style="max-width:100%; border-radius:6px; margin: 4px 0;" />
          <button id="tntc-use-garment-btn" class="tntc-action-btn">Dán kèm ảnh này vào chat</button>
        </div>

        <div class="tntc-footer">
          💡 Chọn prompt ➔ Gemini tự điền vào khung chat!
        </div>
      </div>
    `;

    document.body.appendChild(panel);

    // Xử lý thu gọn/mở rộng
    const toggleBtn = panel.querySelector("#tntc-toggle-btn");
    const body = panel.querySelector("#tntc-panel-body");
    toggleBtn.addEventListener("click", () => {
      if (body.style.display === "none") {
        body.style.display = "block";
        toggleBtn.innerText = "−";
      } else {
        body.style.display = "none";
        toggleBtn.innerText = "+";
      }
    });

    // Xử lý các nút prompt
    const prompts = {
      dress: "Tạo một bức ảnh chụp toàn thân tỉ lệ dọc 9:16 của một cô gái người mẫu Việt Nam 20 tuổi cực kỳ xinh đẹp và quyến rũ, đang mặc bộ trang phục váy thời trang nữ tính hot trend Shopee. Bối cảnh studio hiện đại ánh sáng tự nhiên dịu nhẹ, phong cách ảnh Lookbook thời trang chân thực, rõ chi tiết vải, không méo mó, chất lượng 8k, không watermark.",
      dance: "Tạo một bức ảnh chụp đứng toàn thân tỉ lệ dọc 9:16 của một hot girl Việt Nam xinh đẹp, dáng chuẩn đồng hồ cát, mặc trang phục thời trang trẻ trung năng động sẵn sàng nhảy vũ đạo TikTok. Chụp trong căn phòng hiện đại ấm cúng với đèn neon aesthetic, phong cách ảnh chụp sắc nét chân thực 8k, siêu thực.",
      lookbook: "Chụp ảnh thời trang lookbook toàn thân tỉ lệ 9:16 của nữ người mẫu Châu Á xinh đẹp, đường nét gương mặt thanh tú tự nhiên, biểu cảm cuốn hút, mặc trang phục thời trang thiết kế cao cấp, ánh sáng studio chuẩn nhiếp ảnh thương mại, độ chi tiết cực cao, photorealistic."
    };

    panel.querySelectorAll(".tntc-prompt-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const type = btn.getAttribute("data-type");
        const text = prompts[type];
        insertPromptIntoGemini(text);
      });
    });

    // Kiểm tra xem có ảnh đồ nào vừa gửi từ Shopee qua không
    chrome.storage.local.get(["pendingGarmentUrl"], (res) => {
      if (res.pendingGarmentUrl) {
        const preview = panel.querySelector("#tntc-garment-preview");
        const img = panel.querySelector("#tntc-garment-img");
        img.src = res.pendingGarmentUrl;
        preview.style.display = "block";

        panel.querySelector("#tntc-use-garment-btn").addEventListener("click", () => {
          insertPromptIntoGemini(
            `Đây là mẫu trang phục trong ảnh: hãy tạo ảnh người mẫu nữ Việt Nam 20 tuổi xinh đẹp mặc bộ đồ có thiết kế, màu sắc và kiểu dáng giống như bộ trang phục này. Chụp toàn thân 9:16 phong cách ảnh thật Lookbook.`
          );
          // Mở ảnh trong tab mới để kéo thả nếu cần
          window.open(res.pendingGarmentUrl, "_blank");
          chrome.storage.local.remove(["pendingGarmentUrl"]);
          preview.style.display = "none";
        });
      }
    });
  }

  // Điền prompt vào ô nhập của Gemini
  function insertPromptIntoGemini(text) {
    // Tìm khung soạn thảo rich text của Gemini
    const inputArea = document.querySelector('rich-textarea p, div[contenteditable="true"], textarea');
    if (inputArea) {
      inputArea.focus();
      // Chèn nội dung
      if (inputArea.tagName.toLowerCase() === 'textarea') {
        inputArea.value = text;
      } else {
        inputArea.innerHTML = text;
      }
      // Kích hoạt sự kiện input để nút gửi sáng lên
      inputArea.dispatchEvent(new Event('input', { bubbles: true }));
      showToast("✨ Đã điền câu lệnh vào khung chat! Hãy bấm Gửi.");
    } else {
      // Sao chép vào clipboard nếu không tìm thấy selector
      navigator.clipboard.writeText(text);
      showToast("📋 Đã copy Prompt vào Clipboard! Hãy dán (Ctrl+V) vào Gemini.");
    }
  }

  // 2. Tự động gắn nút "📥 Lưu vào TNTC Studio" trên các ảnh do Gemini tạo ra
  function attachDownloadButtonsToImages() {
    // Tìm các ảnh trong đoạn chat Gemini
    const images = document.querySelectorAll('img');
    images.forEach(img => {
      // Chỉ gắn nút cho ảnh lớn (tránh avatar hoặc icon nhỏ)
      if (img.naturalWidth > 200 || img.width > 200) {
        const parent = img.parentElement;
        if (!parent || parent.querySelector('.tntc-export-btn')) return;

        // Tạo container bọc nếu cần
        const btn = document.createElement("button");
        btn.className = "tntc-export-btn";
        btn.innerHTML = "📥 Chuyển vào TNTC Studio";
        btn.title = "Tải ảnh chất lượng cao về máy để làm video trên TNTC Studio";
        
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          const src = img.src || img.currentSrc;
          if (src) {
            chrome.runtime.sendMessage({
              action: "download_tntc_image",
              imageUrl: src,
              filename: `tntc_model_${Date.now()}.png`
            }, (res) => {
              showToast("✅ Đã tải ảnh về thư mục TNTC_Studio/models!");
            });
          }
        });

        // Đặt nút nổi góc trên ảnh
        if (getComputedStyle(parent).position === 'static') {
          parent.style.position = 'relative';
        }
        parent.appendChild(btn);
      }
    });
  }

  // Toast thông báo nhỏ xinh
  function showToast(msg) {
    let toast = document.getElementById("tntc-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "tntc-toast";
      document.body.appendChild(toast);
    }
    toast.innerText = msg;
    toast.className = "tntc-toast-show";
    setTimeout(() => {
      toast.className = "";
    }, 3000);
  }

  function checkPendingPrompt() {
    chrome.storage.local.get(["pendingPrompt"], (res) => {
      if (res.pendingPrompt) {
        insertPromptIntoGemini(res.pendingPrompt);
        chrome.storage.local.remove(["pendingPrompt"]);
      }
    });
  }

  // Khởi động
  window.addEventListener("load", () => {
    createFloatingToolbar();
    checkPendingPrompt();
    setInterval(attachDownloadButtonsToImages, 1500);
  });

  // Chạy ngay cả khi SPA navigate
  createFloatingToolbar();
  setTimeout(checkPendingPrompt, 1000);
  setInterval(attachDownloadButtonsToImages, 1500);

})();
