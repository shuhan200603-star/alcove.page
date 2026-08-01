/* 墓碑 Service Worker
 *
 * 早先的版本在 /sw.js 注册过一个会缓存页面的 SW。光是替换掉 index.html
 * 并不能摆脱它——旧 SW 仍然注册在访客的浏览器里，会继续端出缓存的旧页面。
 *
 * 浏览器每次导航都会重新抓取这个路径并做字节比对，所以把该路径换成这份
 * 文件，就能让旧 SW 被替换掉：它清空全部缓存、注销自己，然后让还开着的
 * 页面重新导航一次，从而拿到真正的线上文件。
 *
 * 这份文件不缓存任何东西。等确信没有访客还挂着旧 SW 了，就可以连同
 * index.html 里那行注册一起删掉。
 */

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()){
      await caches.delete(key);
    }
    await self.registration.unregister();
    for (const client of await self.clients.matchAll({ type:'window' })){
      client.navigate(client.url).catch(() => {});
    }
  })());
});
