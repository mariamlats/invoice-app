/* Same-origin API requests carry a session-bound CSRF token, including uploads. */
async function apiFetch(input, options = {}) {
  const url = new URL(input, window.location.href);
  if (url.origin !== window.location.origin) {
    throw new Error('Cross-origin API requests are not allowed.');
  }
  const headers = new Headers(options.headers || {});
  const method = (options.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!token) throw new Error('Reload the page to refresh your session.');
    headers.set('X-CSRF-Token', token);
  }
  return fetch(url, { ...options, headers, credentials: 'same-origin' });
}
