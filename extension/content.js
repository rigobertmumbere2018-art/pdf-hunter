(() => {
  const EXT = /\.(pdf|jpe?g|png|webp|gif|svg)(?:[?#].*)?$/i;
  const found = new Map();
  const add = (raw) => {
    if (!raw) return;
    try {
      const u = new URL(raw, location.href);
      if (!/^https?:$/i.test(u.protocol)) return;
      const clean = u.href.split('#')[0];
      if (EXT.test(clean)) found.set(clean, clean);
    } catch (_) {}
  };
  const scan = () => {
    document.querySelectorAll('img,source,video,audio,a,link,object,embed').forEach(el => {
      ['src','href','data-src','data-original','data-lazy-src','data-url','data-image-url','data-image','data-full','data-original-src'].forEach(a => add(el.getAttribute(a)));
      ['srcset','data-srcset'].forEach(a => {
        const v = el.getAttribute(a);
        if (v) v.split(',').forEach(x => add(x.trim().split(/\s+/)[0]));
      });
    });
    document.querySelectorAll('meta').forEach(m => {
      const n = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
      if (/image|thumbnail/.test(n)) add(m.getAttribute('content'));
    });
    document.querySelectorAll('[style]').forEach(el => {
      const s = el.getAttribute('style') || '';
      [...s.matchAll(/url\((['"]?)(.*?)\1\)/gi)].forEach(m => add(m[2]));
    });
    return [...found.values()];
  };
  scan();
  new MutationObserver(scan).observe(document.documentElement, {subtree:true, childList:true, attributes:true, attributeFilter:['src','srcset','data-src','data-original','style']});
  window.addEventListener('message', e => {
    if (e.data?.type === 'PDF_HUNTER_SCAN') {
      scan();
      window.postMessage({type:'PDF_HUNTER_RESULT', files:[...found.values()], page:location.href}, '*');
    }
  });
})();