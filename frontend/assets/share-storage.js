/** 访客侧本机密钥 / 会话存储（分享页与「我的分享」共用） */
const SHARE_SESSION_KEY = "minutes_share_session";
const SHARE_ACCESS_KEYS = "minutes_share_access_keys";
const SHARE_ACCESS_KEY_LEGACY = "minutes_share_access_key";

function loadAccessKeyList(extraShareToken) {
  const keys = [];
  const seen = new Set();
  const push = (raw) => {
    const value = String(raw || "").trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    keys.push(value);
  };

  try {
    const parsed = JSON.parse(localStorage.getItem(SHARE_ACCESS_KEYS) || "[]");
    if (Array.isArray(parsed)) parsed.forEach(push);
  } catch {
    /* ignore corrupt list */
  }

  const legacyPrefix = `${SHARE_ACCESS_KEY_LEGACY}:`;
  for (let i = 0; i < localStorage.length; i += 1) {
    const name = localStorage.key(i);
    if (!name || !name.startsWith(legacyPrefix)) continue;
    push(localStorage.getItem(name));
  }
  if (extraShareToken) {
    push(localStorage.getItem(`${SHARE_ACCESS_KEY_LEGACY}:${extraShareToken}`));
  }
  return keys;
}

function saveAccessKeyList(keys) {
  const cleaned = [];
  const seen = new Set();
  for (const raw of keys || []) {
    const value = String(raw || "").trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    cleaned.push(value);
  }
  localStorage.setItem(SHARE_ACCESS_KEYS, JSON.stringify(cleaned));
}

function rememberAccessKey(key, shareToken) {
  const value = (key || "").trim();
  if (!value) return;
  const next = loadAccessKeyList().filter((k) => k !== value);
  next.unshift(value);
  saveAccessKeyList(next);
  if (shareToken) {
    localStorage.removeItem(`${SHARE_ACCESS_KEY_LEGACY}:${shareToken}`);
  }
}

function forgetAccessKey(key, shareToken) {
  const value = (key || "").trim();
  if (!value) return;
  saveAccessKeyList(loadAccessKeyList().filter((k) => k !== value));
  if (shareToken) {
    localStorage.removeItem(`${SHARE_ACCESS_KEY_LEGACY}:${shareToken}`);
  }
}

/** 从本机会话键收集曾打开过的 share_token */
function loadKnownShareTokens() {
  const prefix = `${SHARE_SESSION_KEY}:`;
  const tokens = [];
  const seen = new Set();
  for (let i = 0; i < localStorage.length; i += 1) {
    const name = localStorage.key(i);
    if (!name || !name.startsWith(prefix)) continue;
    const token = name.slice(prefix.length).trim();
    if (!token || seen.has(token)) continue;
    seen.add(token);
    tokens.push(token);
  }
  return tokens;
}

function shareSessionStorageKey(shareToken) {
  return `${SHARE_SESSION_KEY}:${shareToken}`;
}
