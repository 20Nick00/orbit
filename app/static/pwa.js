function orbitNotify(message) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    toast.setAttribute('role', 'alert');
    document.body.append(toast);
  }
  toast.textContent = message;
  toast.classList.add('on');
  setTimeout(() => toast.classList.remove('on'), 6000);
}
document.addEventListener('htmx:responseError', () => orbitNotify('That did not save. Reload the page and try again.'));
document.addEventListener('htmx:sendError', () => orbitNotify('Orbit could not be reached, so that did not save.'));

/* Service workers require HTTPS or localhost. Unsupported contexts stay ordinary pages. */
if ('serviceWorker' in navigator && window.isSecureContext) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {});
  });
}
