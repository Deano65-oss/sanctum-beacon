/* First-party presentation and local request preparation. No network writes. */
(() => {
  'use strict';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let paused = false;
  try { paused = localStorage.getItem('sanctum-motion-paused') === 'true'; } catch (_) { /* Storage may be disabled. */ }
  const controls = [...document.querySelectorAll('[data-motion-toggle]')];
  const setMotion = () => {
    document.documentElement.classList.toggle('motion-paused', paused || reduced.matches);
    if (paused || reduced.matches) document.querySelectorAll('.v2-waiting').forEach(el => el.classList.remove('v2-waiting')); 
    controls.forEach(button => {
      button.disabled = reduced.matches;
      button.setAttribute('aria-pressed', String(paused || reduced.matches));
      button.textContent = reduced.matches ? 'Reduced motion enabled' : paused ? 'Resume motion' : 'Pause motion';
    });
  };
  controls.forEach(button => button.addEventListener('click', () => {
    paused = !paused;
    try { localStorage.setItem('sanctum-motion-paused', String(paused)); } catch (_) { /* Still apply in this page. */ }
    setMotion();
  }));
  reduced.addEventListener('change', setMotion);
  setMotion();
  if ('IntersectionObserver' in window && !reduced.matches && !paused) {
    document.documentElement.classList.add('v2-reveal-ready');
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) {entry.target.classList.remove('v2-waiting'); observer.unobserve(entry.target);}
    }), {rootMargin: '0px 0px 60px 0px', threshold: 0.04});
    document.querySelectorAll('[data-reveal]').forEach(element => {
      if (element.getBoundingClientRect().top > innerHeight) element.classList.add('v2-waiting');
      observer.observe(element);
    });
  }
  document.querySelectorAll('.v2-more').forEach(menu => {
    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape' && menu.open) {
        menu.open = false; menu.querySelector('summary').focus(); event.preventDefault();
      }
    });
    document.addEventListener('click', event => { if (!menu.contains(event.target)) menu.open = false; });
    menu.addEventListener('focusout', event => { if (!menu.contains(event.relatedTarget)) menu.open = false; });
  });
  document.querySelectorAll('[data-tilt]').forEach(object => {
    const surface = object.closest('.v2-product-art, .v2-product-showcase') || object;
    surface.addEventListener('pointermove', event => {
      if (paused || reduced.matches || event.pointerType === 'touch') return;
      const rect = surface.getBoundingClientRect();
      object.style.setProperty('--tilt-x', `${(event.clientX - rect.left) / rect.width * 22 - 11}deg`);
      object.style.setProperty('--tilt-y', `${12 - (event.clientY - rect.top) / rect.height * 18}deg`);
    }, {passive:true});
    surface.addEventListener('pointerleave', () => {
      object.style.removeProperty('--tilt-x'); object.style.removeProperty('--tilt-y');
    });
  });
  // Scroll-linked CSS 3D: one frame per event, no render loop or dependency.
  const scene = document.querySelector('[data-orbit-scene]');
  const landscape = document.querySelector('[data-alpine-world]');
  const stack = document.querySelector('[data-scroll-stack]');
  let framePending = false;
  const paintDepth = () => {
    framePending = false;
    const still = paused || reduced.matches;
    if (landscape) {
      const frame = landscape.parentElement;
      const journey = frame.parentElement;
      const distance = Math.max(1, journey.offsetHeight - frame.offsetHeight);
      const progress = still ? 0 : Math.max(0, Math.min(1, -journey.getBoundingClientRect().top / distance));
      landscape.style.setProperty('--camera', String(progress));
    }
    if (scene) {
      const hero = scene.closest('.v2-hero');
      const rect = hero.getBoundingClientRect();
      const p = still ? 0 : Math.max(0, Math.min(1, -rect.top / rect.height));
      scene.style.setProperty('--scene-turn', `${p * 95}deg`);
      scene.style.setProperty('--orbit-turn', `${-18 + p * 120}deg`);
      scene.style.setProperty('--scene-pitch', `${15 + p * 32}deg`);
      scene.style.setProperty('--scene-depth', `${p * 100}px`);
    }
    if (stack) {
      const rect = stack.getBoundingClientRect();
      const p = still ? 0 : Math.max(0, Math.min(1, (innerHeight - rect.top) / (innerHeight + rect.height)));
      stack.style.setProperty('--spread', `${p * 90}px`);
      stack.style.setProperty('--stack-turn', `${-18 + p * 36}deg`);
    }
  };
  const queueDepth = () => {
    if (framePending || (!scene && !stack && !landscape)) return;
    framePending = true;
    requestAnimationFrame(paintDepth);
  };
  addEventListener('scroll', queueDepth, {passive:true});
  addEventListener('resize', queueDepth, {passive:true});
  addEventListener('pageshow', queueDepth);
  reduced.addEventListener('change', queueDepth);
  controls.forEach(button => button.addEventListener('click', queueDepth));
  queueDepth();
  document.querySelectorAll('[data-request-builder]').forEach(section => {
    const form = section.querySelector('form');
    const brief = section.querySelector('textarea');
    const acknowledgement = section.querySelector('input[type=checkbox]');
    const output = section.querySelector('.v2-request-output');
    const status = section.querySelector('[role=status]');
    const download = section.querySelector('[data-download-request]');
    let prepared = null;
    brief.value = ''; acknowledgement.checked = false;
    const invalidate = () => {prepared = null; output.hidden = true; status.textContent = '';};
    form.addEventListener('input', invalidate);
    form.addEventListener('submit', event => {
      event.preventDefault(); invalidate();
      const request = brief.value.trim();
      if (request.length < 20 || request.length > 1500 || !acknowledgement.checked) {
        status.textContent = 'Describe the request in 20–1,500 characters and acknowledge the nonbinding scope.';
        return;
      }
      prepared = {
        client_id: `request-${crypto.randomUUID()}`,
        listing_id: section.dataset.listingId,
        listing_version: Number(section.dataset.listingVersion),
        request,
        acknowledge_non_binding: true,
        public_material_only: true
      };
      output.querySelector('pre').textContent = JSON.stringify(prepared, null, 2);
      output.hidden = false;
      status.textContent = 'Prepared locally. Nothing has been sent and no order has been accepted.';
    });
    download.addEventListener('click', () => {
      if (!prepared) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(prepared, null, 2) + '\n'], {type:'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = 'sanctum-request.json';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  });
})();
