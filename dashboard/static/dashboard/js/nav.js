(function () {
  const toggle = document.getElementById("nav-toggle");
  const links = document.getElementById("nav-links");
  if (!toggle || !links) return;

  function closeNav() {
    links.classList.remove("open");
    document.body.classList.remove("sr-nav-open");
    toggle.setAttribute("aria-expanded", "false");
  }

  function openNav() {
    links.classList.add("open");
    document.body.classList.add("sr-nav-open");
    toggle.setAttribute("aria-expanded", "true");
  }

  toggle.addEventListener("click", function (e) {
    e.stopPropagation();
    if (links.classList.contains("open")) {
      closeNav();
    } else {
      openNav();
    }
  });

  // tapping a link on mobile should close the menu, not leave it open
  // underneath the next page
  links.querySelectorAll("a").forEach((a) => a.addEventListener("click", closeNav));

  // tapping outside the open menu closes it
  document.addEventListener("click", function (e) {
    if (!links.classList.contains("open")) return;
    if (links.contains(e.target) || toggle.contains(e.target)) return;
    closeNav();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeNav();
  });

  // if the viewport grows back past the mobile breakpoint, make sure we
  // don't leave the menu stuck "open" (which the desktop CSS ignores, but
  // best to reset the state cleanly)
  window.addEventListener("resize", function () {
    if (window.innerWidth > 780) closeNav();
  });
})();
