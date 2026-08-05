(function () {
  // 同域：页面与 API 都走当前 origin（本地由 serve.py 反代 /api，公网由 Tunnel path 分流）
  window.APP_CONFIG = {
    apiBase: `${location.origin}/api/v1`,
  };
})();
