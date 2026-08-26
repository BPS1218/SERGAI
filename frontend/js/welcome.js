import { CONFIG } from "./config.js";

const $ = (id) => document.getElementById(id);
let pendingGoogle = null;

function decodeJwt(token) {
  const base64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(atob(base64));
}

function startGoogleLogin() {
  const redirectUri = location.origin + "/";
  window.location.href =
    "https://accounts.google.com/o/oauth2/v2/auth" +
    "?client_id=" +
    encodeURIComponent(CONFIG.googleClientId) +
    "&redirect_uri=" +
    encodeURIComponent(redirectUri) +
    "&response_type=token" +
    "&scope=" +
    encodeURIComponent("openid email profile") +
    "&prompt=select_account";
}

function hideLoginLoading() {
  const el = $("loginLoading");
  if (el) el.classList.add("hidden");
}

async function handleAfterGoogle() {
  const saved = JSON.parse(localStorage.getItem("sergai_user") || "null");

  // Cek localStorage dulu (cepat)
  if (saved && saved.email === pendingGoogle.email && saved.unit) {
    localStorage.setItem(
      "sergai_user",
      JSON.stringify({ ...pendingGoogle, unit: saved.unit }),
    );
    location.href = "/chat";
    return;
  }

  // Fallback: cek di Google Sheets
  try {
    const res = await fetch(CONFIG.googleScriptUrl, {
      method: "POST",
      body: JSON.stringify({ action: "get_user", email: pendingGoogle.email }),
    });
    const data = await res.json();
    if (data.ok && data.unit) {
      const user = { ...pendingGoogle, unit: data.unit };
      localStorage.setItem("sergai_user", JSON.stringify(user));
      location.href = "/chat";
      return;
    }
  } catch (e) {
    console.warn("Sheet check failed:", e);
  }

  // Belum terdaftar → tampilkan modal
  $("muName").textContent = (pendingGoogle.name || "").split(" ")[0];
  if (pendingGoogle.picture) $("muPhoto").src = pendingGoogle.picture;
  hideLoginLoading();
  $("unitModal").classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", () => {
  // 1) Tangani token kembalian dari Google
  const hash = location.hash.substring(1);
  if (hash) {
    const params = new URLSearchParams(hash);
    const idToken = params.get("id_token");
    const accessToken = params.get("access_token");
    history.replaceState(null, "", location.pathname);

    if (idToken) {
      try {
        pendingGoogle = decodeJwt(idToken);
        handleAfterGoogle(); // tetap sama
      } catch (e) {
        console.warn(e);
      }
    } else if (accessToken) {
      fetch("https://www.googleapis.com/oauth2/v3/userinfo", {
        headers: { Authorization: "Bearer " + accessToken },
      })
        .then((r) => r.json())
        .then((d) => {
          pendingGoogle = {
            email: d.email,
            name: d.name,
            picture: d.picture || "",
          };
          handleAfterGoogle(); // tetap sama
        })
        .catch((e) => console.warn(e));
    }
  }

  const saved = JSON.parse(localStorage.getItem("sergai_user") || "null");

  // ✅ Warm-up Google Apps Script agar riwayat di /chat lebih cepat
  const savedWarm = JSON.parse(localStorage.getItem("sergai_user") || "null");
  if (savedWarm && savedWarm.email) {
    fetch(CONFIG.googleScriptUrl, {
      method: "POST",
      body: JSON.stringify({ action: "get_user", email: savedWarm.email }),
    }).catch(() => {});
  }

  // 2) Tombol Mulai Chat (hero)
  $("startChatBtn").addEventListener("click", () => {
    if (saved && saved.email) location.href = "/chat";
    else startGoogleLogin();
  });

  // 3) Tombol Chat (navbar)
  const navBtn = $("navStartChatBtn");
  if (navBtn) {
    navBtn.addEventListener("click", (e) => {
      e.preventDefault();
      if (saved && saved.email) location.href = "/chat";
      else startGoogleLogin();
    });
  }

  // 4) Simpan unit (login pertama)
  $("muSave").addEventListener("click", () => {
    const unit = $("muUnit").value;
    if (!unit) {
      alert("Silakan pilih unit kerja terlebih dahulu");
      return;
    }
    const user = {
      ...pendingGoogle,
      unit,
      registeredAt: new Date().toISOString(),
    };
    localStorage.setItem("sergai_user", JSON.stringify(user));
    fetch(CONFIG.googleScriptUrl, {
      method: "POST",
      body: JSON.stringify({
        action: "register_user",
        email: user.email,
        name: user.name,
        picture: user.picture,
        unit: user.unit,
      }),
    }).catch(() => {});
    location.href = "/chat";
  });

  // 5) Navbar scroll + scrollspy
  const nav = document.querySelector(".w-nav");
  const sectionIds = ["beranda", "tentang", "penggunaan", "faq"];
  const menuLinks = document.querySelectorAll(".w-menu a");

  function onScroll() {
    if (window.scrollY > 80) nav.classList.add("scrolled");
    else nav.classList.remove("scrolled");

    const pos = window.scrollY + 140;
    let current = "beranda";
    sectionIds.forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.offsetTop <= pos) current = id;
    });
    menuLinks.forEach((a) => {
      a.classList.toggle("active", a.getAttribute("href") === "#" + current);
    });
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // 6) Burger menu (mobile)
  const burger = $("wBurger");
  const wNav = document.querySelector(".w-nav");

  const setBurgerIcon = (open) => {
    const icon = burger.querySelector("i");
    if (icon) icon.className = open ? "fas fa-times" : "fas fa-bars";
  };

  burger.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = wNav.classList.toggle("menu-open");
    setBurgerIcon(open);
  });

  // Klik link menu → tutup dropdown
  document.querySelectorAll(".w-menu a").forEach((a) => {
    a.addEventListener("click", () => {
      wNav.classList.remove("menu-open");
      setBurgerIcon(false);
    });
  });

  // Klik di luar navbar → tutup dropdown
  document.addEventListener("click", (e) => {
    if (wNav.classList.contains("menu-open") && !wNav.contains(e.target)) {
      wNav.classList.remove("menu-open");
      setBurgerIcon(false);
    }
  });
});
