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
  const scene = document.querySelector('[data-orbit-scene]');
  let framePending = false;
  if (scene) addEventListener('scroll', () => {
    if (framePending || paused || reduced.matches || scrollY > 1100) return;
    framePending = true;
    requestAnimationFrame(() => {
      scene.style.setProperty('--scene-turn', `${Math.min(scrollY, 800) * .025}deg`);
      scene.style.setProperty('--scene-y', `${Math.min(scrollY, 800) * .035}px`);
      framePending = false;
    });
  }, {passive:true});
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
