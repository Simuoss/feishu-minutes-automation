if (!requireAdminPage()) throw new Error("redirecting to login");

if (!isSuperAdminView() || !getSuperJwt()) {
  setAdminViewMode("user");
  location.replace("/");
}

let voiceprints = [];

function msToClock(ms) {
  const total = Math.max(0, Math.round(Number(ms) || 0) / 1000);
  const m = Math.floor(total / 60);
  const s = Math.floor(total % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function talkText(ms) {
  const minutes = Math.round((Number(ms) || 0) / 60000);
  return minutes >= 1 ? `${minutes} 分钟` : "不足 1 分钟";
}

function sampleHtml(sample) {
  const range = `${msToClock(sample.start_ms)}–${msToClock(sample.end_ms)}`;
  if (!sample.audio_url) {
    return `<li class="voiceprint-sample">
      <span class="voiceprint-sample-range">${escapeHtml(range)}</span>
      <span class="voiceprint-sample-missing">音频已不在，无法试听</span>
    </li>`;
  }
  // 用媒体片段定位到这段发言，省得为每个样本单独存一个音频文件
  const src = `${sample.audio_url}#t=${(sample.start_ms / 1000).toFixed(1)},${(
    sample.end_ms / 1000
  ).toFixed(1)}`;
  return `<li class="voiceprint-sample">
    <span class="voiceprint-sample-range">${escapeHtml(range)}</span>
    <audio controls preload="none" src="${escapeHtml(src)}"></audio>
  </li>`;
}

function meetingHtml(item) {
  const title = item.title || item.minute_token;
  const score = item.match_score ? `　匹配 ${item.match_score.toFixed(2)}` : "";
  return `<li>
    <a href="/meeting.html?token=${encodeURIComponent(
      item.minute_token
    )}&owner_user_id=${item.owner_user_id}">${escapeHtml(title)}</a>
    <span class="voiceprint-meeting-meta">${escapeHtml(
      item.local_label
    )}　${escapeHtml(talkText(item.talk_ms))}${escapeHtml(score)}</span>
  </li>`;
}

function cardHtml(person) {
  const others = voiceprints
    .filter((p) => p.id !== person.id)
    .map(
      (p) =>
        `<option value="${p.id}">${escapeHtml(
          p.display_name || `未命名 #${p.id}`
        )}</option>`
    )
    .join("");
  return `<article class="voiceprint-card" data-id="${person.id}">
    <header class="voiceprint-card-head">
      <div class="voiceprint-name">
        <input type="text" class="voiceprint-name-input" data-field="display_name"
          maxlength="64" placeholder="未命名（如：张三）"
          value="${escapeHtml(person.display_name || "")}" />
        <button type="button" class="btn btn-sm btn-primary" data-action="rename">
          <i class="ri ri-save-line" aria-hidden="true"></i><span class="btn-label">保存</span>
        </button>
      </div>
      <div class="voiceprint-meta">
        #${person.id}　${person.meeting_count} 场会议　${person.sample_count} 段样本　
        更新于 ${escapeHtml(person.updated_at_text || "—")}
      </div>
    </header>

    <div class="voiceprint-body">
      <div class="voiceprint-col">
        <h4>试听样本</h4>
        <ul class="voiceprint-samples">${
          (person.samples || []).map(sampleHtml).join("") ||
          "<li class='voiceprint-sample-missing'>暂无样本</li>"
        }</ul>
      </div>
      <div class="voiceprint-col">
        <h4>出现的会议</h4>
        <ul class="voiceprint-meetings">${
          (person.meetings || []).map(meetingHtml).join("") || "<li>—</li>"
        }</ul>
      </div>
    </div>

    <footer class="voiceprint-card-foot">
      <label class="voiceprint-merge">
        <span>并入</span>
        <select data-field="merge_target"><option value="">选择目标人物</option>${others}</select>
      </label>
      <button type="button" class="btn btn-sm" data-action="merge">合并</button>
      <button type="button" class="btn btn-sm btn-danger" data-action="delete">删除</button>
    </footer>
  </article>`;
}

async function loadVoiceprints() {
  const status = $("#voiceprints-status");
  const list = $("#voiceprints-list");
  const empty = $("#voiceprints-empty");
  setThinkingStatus(status);
  try {
    const res = await apiFetch("/admin/voiceprints");
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    const data = await res.json();
    voiceprints = data.items || [];
    const named = voiceprints.filter((p) => p.named).length;
    status.textContent = `共 ${voiceprints.length} 位人物，已命名 ${named} 位`;
    if (!voiceprints.length) {
      list.innerHTML = "";
      empty?.classList.remove("hidden");
      return;
    }
    empty?.classList.add("hidden");
    list.innerHTML = voiceprints.map(cardHtml).join("");
  } catch (e) {
    status.textContent = `加载失败：${e.message || e}`;
    list.innerHTML = "";
  }
}

async function renamePerson(id, card) {
  const status = $("#voiceprints-status");
  const name = card.querySelector('[data-field="display_name"]')?.value || "";
  setThinkingStatus(status);
  try {
    const res = await apiFetch(`/admin/voiceprints/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ display_name: name }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    status.textContent = name.trim()
      ? `已命名为「${name.trim()}」，相关转写已即时生效`
      : "已清空命名，转写将回到「说话人N」";
    await loadVoiceprints();
  } catch (e) {
    status.textContent = `保存失败：${e.message || e}`;
  }
}

async function mergeInto(sourceId, card) {
  const status = $("#voiceprints-status");
  const targetId = card.querySelector('[data-field="merge_target"]')?.value;
  if (!targetId) {
    status.textContent = "请先选择要并入的目标人物";
    return;
  }
  if (!confirm("合并后来源人物会作废，其样本与会议都改挂到目标人物，确定吗？")) return;
  setThinkingStatus(status);
  try {
    const res = await apiFetch(`/admin/voiceprints/${targetId}/merge`, {
      method: "POST",
      body: JSON.stringify({ source_ids: [Number(sourceId)] }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    status.textContent = "已合并";
    await loadVoiceprints();
  } catch (e) {
    status.textContent = `合并失败：${e.message || e}`;
  }
}

async function deletePerson(id) {
  const status = $("#voiceprints-status");
  if (!confirm("删除后引用它的会议会退回「说话人N」，确定吗？")) return;
  setThinkingStatus(status);
  try {
    const res = await apiFetch(`/admin/voiceprints/${id}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    status.textContent = "已删除";
    await loadVoiceprints();
  } catch (e) {
    status.textContent = `删除失败：${e.message || e}`;
  }
}

$("#refresh-voiceprints-btn")?.addEventListener("click", loadVoiceprints);
$("#voiceprints-list")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const card = btn.closest(".voiceprint-card");
  const id = card?.dataset.id;
  if (!id) return;
  const action = btn.getAttribute("data-action");
  if (action === "rename") renamePerson(id, card);
  if (action === "merge") mergeInto(id, card);
  if (action === "delete") deletePerson(id);
});

bindAdminNav();
checkAuth();
loadVoiceprints();
