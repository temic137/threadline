// Copy buttons, and the hero command switching between macOS/Linux and Windows.
const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>';

document.querySelectorAll("[data-copy]").forEach((button) => {
  const icon = button.innerHTML;
  button.addEventListener("click", async () => {
    const text = button.closest("[data-command]").querySelector("code").textContent.trim();
    try { await navigator.clipboard.writeText(text); } catch { return; }
    button.innerHTML = CHECK;
    button.classList.add("copied");
    setTimeout(() => { button.innerHTML = icon; button.classList.remove("copied"); }, 1600);
  });
});

const hero = document.querySelector(".hero [data-mac]");
const label = document.querySelector("[data-os-label]");
const toggle = document.querySelector("[data-os-switch]");
function showOS(windows) {
  hero.textContent = windows ? hero.dataset.windows : hero.dataset.mac;
  hero.parentElement.querySelector(".prompt").textContent = windows ? ">" : "$";
  label.textContent = windows ? "For Windows, in PowerShell." : "For macOS and Linux.";
  toggle.textContent = windows ? "On a Mac or Linux?" : "On Windows?";
  toggle.dataset.windows = windows ? "1" : "";
}
toggle.addEventListener("click", () => showOS(!toggle.dataset.windows));
if (/Windows/i.test(navigator.userAgent)) showOS(true);
