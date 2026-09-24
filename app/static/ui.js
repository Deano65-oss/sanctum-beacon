/* Progressive disclosure only: all content remains available without JavaScript. */
(async () => {
  // Measure text after the locally served typefaces have settled.
  if (document.fonts) await document.fonts.ready;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let sequence = 0;
  const button = (label, target) => {
    const control = document.createElement('button');
    control.type = 'button';
    control.className = 'disclosure-toggle';
    control.textContent = label;
    control.setAttribute('aria-expanded', 'false');
    control.setAttribute('aria-controls', target);
    return control;
  };
  document.querySelectorAll('.post p:not(.fine):not(.post-meta), .document > .full-body').forEach(body => {
    if (body.querySelector('a, button, input, select, textarea')) return;
    const line = parseFloat(getComputedStyle(body).lineHeight) || 24;
    const preview = Math.ceil(line * 7);
    if (body.scrollHeight < preview + line * 3) return;
    body.id ||= `readable-content-${++sequence}`;
    const toggle = button('Show more', body.id);
    body.classList.add('disclosure-body', 'is-collapsed');
    body.style.maxHeight = `${preview}px`;
    body.after(toggle);
    toggle.addEventListener('click', () => {
      const opening = toggle.getAttribute('aria-expanded') !== 'true';
      body.style.maxHeight = `${body.getBoundingClientRect().height}px`;
      // Establish the current height before animating either direction.
      void body.offsetHeight;
      body.classList.toggle('is-collapsed', !opening);
      body.style.maxHeight = `${opening ? body.scrollHeight : preview}px`;
      toggle.setAttribute('aria-expanded', String(opening));
      toggle.textContent = opening ? 'Show less' : 'Show more';
      if (!opening && body.getBoundingClientRect().top < 0) {
        body.scrollIntoView({block: 'start', behavior: reduced.matches ? 'instant' : 'smooth'});
      }
      if (opening && reduced.matches) body.style.maxHeight = 'none';
    });
    body.addEventListener('transitionend', event => {
      if (event.propertyName === 'max-height' && toggle.getAttribute('aria-expanded') === 'true') {
        body.style.maxHeight = 'none';
      }
    });
  });
  // Limit long runs of cards without moving them out of their existing layout.
  const parents = new Set([...document.querySelectorAll('main .post')].map(post => post.parentElement));
  parents.forEach(parent => {
    let group = [];
    const finish = () => {
      if (group.length <= 4) { group = []; return; }
      const tail = group.slice(3);
      tail.forEach(post => { post.id ||= `readable-section-${++sequence}`; post.hidden = true; });
      const toggle = button(`Show ${tail.length} more`, tail.map(post => post.id).join(' '));
      group[group.length - 1].after(toggle);
      toggle.addEventListener('click', () => {
        const opening = toggle.getAttribute('aria-expanded') !== 'true';
        const before = toggle.getBoundingClientRect().top;
        tail.forEach((post, i) => {
          post.hidden = !opening;
          if (opening && !reduced.matches) post.animate(
            [{opacity: 0, transform: 'translateY(6px)'}, {opacity: 1, transform: 'translateY(0)'}],
            {duration: 260, delay: Math.min(i, 4) * 35, easing: 'ease-out'});
        });
        toggle.setAttribute('aria-expanded', String(opening));
        toggle.textContent = opening ? 'Show fewer' : `Show ${tail.length} more`;
        if (!opening) window.scrollBy({top: toggle.getBoundingClientRect().top - before, behavior: 'instant'});
      });
      group = [];
    };
    [...parent.children].forEach(child => { if (child.classList.contains('post')) group.push(child); else finish(); });
    finish();
  });
  // A shared reply link must remain reachable even inside a collapsed group.
  const revealLinkedReply = () => {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const target = id && document.getElementById(id);
    if (!target || !target.classList.contains('post') || !target.hidden) return;
    const control = [...document.querySelectorAll('.disclosure-toggle')].find(
      item => item.getAttribute('aria-controls').split(' ').includes(id));
    if (control) {
      control.click();
      target.scrollIntoView({block: 'start', behavior: 'instant'});
    }
  };
  revealLinkedReply();
  addEventListener('hashchange', revealLinkedReply);
})();
