const header = document.querySelector("[data-elevate]");

function syncHeaderElevation() {
  if (!header) return;
  header.classList.toggle("is-elevated", window.scrollY > 8);
}

syncHeaderElevation();
window.addEventListener("scroll", syncHeaderElevation, { passive: true });

const carousel = document.querySelector("[data-carousel]");
if (carousel) {
  const slides = Array.from(carousel.querySelectorAll("[data-slide]"));
  const dots = Array.from(carousel.querySelectorAll("[data-dot]"));
  const rotateMs = 5000;
  let activeIndex = 0;
  let timer = null;

  function activate(next) {
    activeIndex = (next + slides.length) % slides.length;
    slides.forEach((slide, i) => {
      const isActive = i === activeIndex;
      slide.classList.toggle("is-active", isActive);
      if (isActive) {
        slide.removeAttribute("aria-hidden");
      } else {
        slide.setAttribute("aria-hidden", "true");
      }
    });
    dots.forEach((dot, i) => {
      const isActive = i === activeIndex;
      dot.classList.toggle("is-active", isActive);
      if (isActive) {
        dot.setAttribute("aria-current", "true");
      } else {
        dot.removeAttribute("aria-current");
      }
    });
  }

  function stopTimer() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function startTimer() {
    stopTimer();
    timer = window.setInterval(() => activate(activeIndex + 1), rotateMs);
  }

  dots.forEach((dot, i) => {
    dot.addEventListener("click", () => {
      activate(i);
      startTimer();
    });
  });

  carousel.addEventListener("mouseenter", stopTimer);
  carousel.addEventListener("mouseleave", startTimer);
  carousel.addEventListener("focusin", stopTimer);
  carousel.addEventListener("focusout", startTimer);

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  activate(0);
  if (!reduced.matches) startTimer();
  reduced.addEventListener("change", (e) => {
    if (e.matches) stopTimer();
    else startTimer();
  });
}
