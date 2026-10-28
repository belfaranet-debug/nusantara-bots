/* Firebase Cloud Messaging service worker for the own build.
 *
 * Background messages only. There are NO keys in this file: the
 * public Firebase web config (apiKey / projectId /
 * messagingSenderId / appId — public identifiers, not secrets)
 * is fetched from the server at /api/push/config (served from
 * the GROKBOT_FCM_WEB_CONFIG env var — see PUSH-SETUP.md). If
 * that endpoint is not wired yet, the worker simply does
 * nothing; it never fakes a registration.
 *
 * Firebase JS SDK, compat builds, version pinned:
 *   firebase-app-compat 10.12.2
 *   firebase-messaging-compat 10.12.2
 * (gstatic CDN, same version for both scripts.)
 */
importScripts(
  'https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js'
);
importScripts(
  'https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js'
);

let messaging = null;

async function initMessaging() {
  if (messaging) {
    return messaging;
  }
  let config = null;
  try {
    const res = await fetch('/api/push/config');
    if (!res.ok) {
      return null;
    }
    config = await res.json();
  } catch (err) {
    return null;
  }
  if (!config || !config.apiKey || !config.projectId
      || !config.messagingSenderId || !config.appId) {
    return null;
  }
  try {
    if (!firebase.apps.length) {
      firebase.initializeApp(config);
    }
    messaging = firebase.messaging();
  } catch (err) {
    return null;
  }
  return messaging;
}

self.addEventListener('install', (event) => {
  event.waitUntil(initMessaging());
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Background message -> show a notification. The payload the
// server sends carries title/body in `notification` and a
// `data.url` the click should open (deck, default "/").
initMessaging().then((msg) => {
  if (!msg) {
    return;
  }
  msg.onBackgroundMessage((payload) => {
    const notification = payload.notification || {};
    const data = payload.data || {};
    const title = notification.title || 'Grok Bot';
    const options = {
      body: notification.body || '',
      data: { url: data.url || '/' },
    };
    return self.registration.showNotification(title, options);
  });
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clients) => {
        for (const client of clients) {
          if ('focus' in client) {
            return client.focus();
          }
        }
        if (self.clients.openWindow) {
          return self.clients.openWindow(url);
        }
        return undefined;
      })
  );
});
