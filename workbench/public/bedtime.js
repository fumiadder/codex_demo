"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    user: null, spaces: [], space: null, config: null, voices: [],
    stories: [], favorites: [], history: [], selected: null,
    library: "browse", category: "全部", scope: "local",
    spaceEpoch: 0, searchEpoch: 0, searchAbort: null, debounce: null,
    audioEpoch: 0, playing: false, utterance: null, audio: null,
    chunks: [], chunkIndex: 0, currentChar: 0, startedAt: 0,
    elapsed: 0, deadline: 0, urls: new Set(), voiceFile: null,
    cloneBusy: false, disposed: false, speechStartTimeout: null,
    voiceSampleEpoch: 0, voicePreviewURL: null,
  };
  let audioContext = null, speechGain = null, noiseGain = null, noiseSource = null;
  let toastTimeout = null, voicePoll = null;
  const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
  const desktopMedia = window.matchMedia("(min-width: 1024px)");
  const namespace = () => `zhixu:bedtime:stories:${state.user?.id}:${state.space?.id}`;
  const writable = () => !!state.space && state.space.role !== "viewer";
  const prefix = () => `/api/spaces/${encodeURIComponent(state.space.id)}/bedtime`;
  const node = (tag, className = "", content) => {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (content !== undefined) n.textContent = content;
    return n;
  };
  const action = (text, handler, className = "") => {
    const b = node("button", className, text);
    b.type = "button";
    b.addEventListener("click", handler);
    return b;
  };
  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }
  function safeStorage(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      if (value != null) throw new Error("设备存储不可用，请改用下载文本。");
    }
    return null;
  }
  function offlineStories() {
    try {
      const stories = JSON.parse(safeStorage(namespace()) || "[]");
      return Array.isArray(stories) ? stories.filter((s) => s && typeof s.id === "string" && typeof s.title === "string" && typeof s.text === "string").slice(0, 30) : [];
    } catch { return []; }
  }
  function clearOffline() {
    if (state.user && state.space) safeStorage(namespace(), null);
  }
  function signalSessionEvent(kind, oldSpace) {
    try {
      localStorage.setItem("zhixu:session-event", JSON.stringify({ kind, userId: state.user?.id, spaceId: oldSpace, at: Date.now() }));
    } catch { /* Storage unavailable does not prevent stopping this page. */ }
  }
  function loginReturnURL() {
    const params = new URLSearchParams();
    const current = new URLSearchParams(location.search);
    if (current.get("panel") === "voices") params.set("panel", "voices");
    const space = current.get("space");
    if (space && /^[A-Za-z0-9_-]{1,128}$/.test(space)) params.set("space", space);
    const query = params.toString();
    const path = `/bedtime.html${query ? `?${query}` : ""}`;
    return `/?next=${encodeURIComponent(path)}`;
  }
  async function shareVoiceLink(resultId) {
    const url = `${location.origin}/bedtime.html?panel=voices`;
    try {
      await navigator.clipboard.writeText(url);
      $(resultId).hidden = true;
      toast("音色上传链接已复制。打开链接后登录自己的账号即可选择录音。");
    } catch {
      const link = node("a", "", url);
      link.href = url;
      $(resultId).replaceChildren(node("span", "", "可复制这个链接："), link);
      $(resultId).hidden = false;
      toast("浏览器暂不支持自动复制，可选中显示的链接复制。");
    }
  }
  function applyTheme() {
    const preference = safeStorage("zhixu:bedtime:theme") || "auto";
    const hour = new Date().getHours();
    const night = preference === "night" || (preference === "auto" && (themeMedia.matches || hour >= 21 || hour < 7));
    document.documentElement.dataset.theme = night ? "night" : "day";
    const text = preference === "auto" ? "自动" : preference === "night" ? "夜间" : "日间";
    $("theme-toggle").setAttribute("aria-label", `护眼模式：${text}`);
    $("theme-toggle").title = `护眼模式：${text}，点击切换`;
    $("theme-toggle").textContent = night ? "☾" : "☼";
    document.querySelector('meta[name="theme-color"]').content = night ? "#192835" : "#e8eff4";
  }
  async function jsonRequest(url, method = "GET", body, signal) {
    const epoch = state.spaceEpoch;
    const expectedUser = state.user?.id;
    const options = { method, credentials: "same-origin", cache: "no-store", signal, headers: { Accept: "application/json" } };
    if (expectedUser) options.headers["X-Expected-User"] = expectedUser;
    if (method !== "GET") options.headers["X-Requested-With"] = "Workspace";
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try { response = await fetch(url, options); }
    catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("暂时无法连接。已保存的故事可在离线页阅读。");
    }
    let result;
    try { result = await response.json(); }
    catch { throw new Error("服务返回的内容不完整，请稍后重试。"); }
    if (epoch !== state.spaceEpoch || expectedUser !== state.user?.id) throw new DOMException("空间已切换", "AbortError");
    if (!response.ok) {
      if (response.status === 401) expireSession();
      if (method === "GET" && state.space && url.startsWith(prefix()) && [403, 404].includes(response.status)) {
        if (result.error === "空间不存在或无权访问" || response.status === 403) expireSession("这个空间已无法访问，请返回工作台。");
        else await verifyMembership();
      }
      const error = new Error(result.error || result.message || "暂时无法完成，请重试。");
      error.status = response.status;
      throw error;
    }
    return result;
  }
  async function verifyMembership() {
    if (!state.user || !state.space || state.disposed) return false;
    try {
      const result = await jsonRequest("/api/spaces");
      const member = result.spaces?.find((space) => space.id === state.space.id);
      if (!member) { expireSession("这个空间的访问权限已结束，请返回工作台。"); return false; }
      state.space.role = member.role;
      configureCapabilities();
      return true;
    } catch (error) {
      if (error.name !== "AbortError") expireSession("暂时无法确认空间访问权限，请返回工作台重新打开。");
      return false;
    }
  }
  const api = (path, method, body, signal) => jsonRequest(prefix() + path, method, body, signal);
  function expireSession(message = "请先登录知序，再打开晚安故事。") {
    if (state.disposed) return;
    stopPlayback(true);
    clearOffline();
    clearInterval(voicePoll);
    closeVoices();
    state.disposed = true;
    ++state.spaceEpoch;
    state.searchAbort?.abort();
    $("bedtime-app").hidden = true;
    $("reader-text").replaceChildren();
    $("story-list").replaceChildren();
    $("voice-library").replaceChildren();
    $("loading-screen").hidden = false;
    $("loading-screen").replaceChildren();
    const wrap = node("div");
    wrap.append(node("p", "", message));
    const link = node("a", "", "返回工作台登录");
    link.href = loginReturnURL();
    wrap.append(link);
    $("loading-screen").append(wrap);
    state.selected = null; state.voices = []; state.favorites = []; state.history = [];
  }
  function listStatus(message) { $("list-status").textContent = message; }
  function minutes(story) { return story.readMinutes || Math.max(1, Math.ceil(story.text.length / 250)); }
  function sourceLabel(story) {
    const label = typeof story.source?.label === "string" ? story.source.label : "故事文本";
    return story.isExcerpt ? `${label} · 文本片段` : label;
  }
  function renderStories(stories) {
    $("story-list").replaceChildren();
    if (!stories.length) {
      const empty = state.library === "offline" ? "保存一篇喜欢的故事，已打开的页面可离线阅读。" : state.library === "favorites" ? "打开故事，点一下收藏，留给下次的夜晚。" : state.library === "history" ? "开始朗读后，会在这里留下听过的故事。" : "还没找到这篇故事。换个关键词试试吧。";
      $("story-list").append(node("p", "empty-list", empty));
      return;
    }
    stories.forEach((story, index) => {
      const card = action("", () => openStory(story), "story-card");
      card.setAttribute("aria-label", `打开故事：${story.title}`);
      if (state.selected?.id === story.id) card.setAttribute("aria-current", "true");
      const art = node("span", `story-art${index % 3 === 1 ? " warm" : index % 3 === 2 ? " lavender" : ""}`, ["☾", "♧", "✧", "☁"][index % 4]);
      art.setAttribute("aria-hidden", "true");
      const info = node("div", "story-card-info");
      info.append(node("h3", "", story.title), node("p", "", story.text || story.excerpt || "打开故事，慢慢读。"), node("small", "", `${story.category || "晚安"} · 约 ${minutes(story)} 分钟 · ${sourceLabel(story)}`));
      card.append(art, info);
      $("story-list").append(card);
    });
  }
  function renderLibrary() {
    const labels = { browse: "给今晚的故事", favorites: "留给下次的夜晚", history: "曾经听过的故事", offline: "存在这台设备的故事" };
    $("list-heading").textContent = labels[state.library];
    document.querySelectorAll("[data-library]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.library === state.library)));
    $("clear-list").hidden = !["history", "offline"].includes(state.library);
    $("clear-list").disabled = state.library === "history" && !writable();
    $("clear-list").textContent = state.library === "history" ? "清空记录" : "删除离线缓存";
    if (state.library === "browse") renderStories(state.stories);
    else if (state.library === "favorites") renderStories(state.favorites);
    else if (state.library === "offline") {
      renderStories(offlineStories());
      listStatus("仅缓存主动保存的故事文本。已打开的页面可离线阅读，设备语音是否离线可用取决于系统；云端音色需要联网。退出或切换空间会删除当前缓存。");
    } else {
      const stories = state.history.map((entry) => {
        const cached = entry.story || [...state.stories, ...state.favorites, ...offlineStories()].find((s) => s.id === entry.storyId);
        return cached || { id: entry.storyId, title: entry.title, text: "", category: "晚安", source: { label: "播放记录" }, readMinutes: "—" };
      });
      renderStories(stories);
    }
  }
  async function search() {
    state.searchAbort?.abort();
    const abort = new AbortController();
    state.searchAbort = abort;
    const token = ++state.searchEpoch;
    const query = $("search-input").value.trim();
    state.library = "browse";
    renderLibrary();
    listStatus(state.scope === "web" ? "正在寻找联网故事文本…" : "正在寻找今晚的故事…");
    try {
      const params = new URLSearchParams({ q: query, category: state.category, scope: state.scope });
      const result = await api(`/search?${params}`, "GET", undefined, abort.signal);
      if (token !== state.searchEpoch || state.disposed) return;
      state.stories = Array.isArray(result.stories) ? result.stories : [];
      renderLibrary();
      listStatus(state.scope === "web" ? "联网来源按结果标注。全文能否提供取决于来源授权与可用性。" : `${state.stories.length} 篇知序原创故事 · 故事文本与音色独立`);
    } catch (error) {
      if (error.name === "AbortError" || token !== state.searchEpoch || state.disposed) return;
      state.stories = [];
      renderLibrary();
      listStatus(error.message);
    }
  }
  function scheduleSearch() {
    clearTimeout(state.debounce);
    // Invalidate immediately; an older response cannot replace the new input.
    ++state.searchEpoch;
    state.searchAbort?.abort();
    state.debounce = setTimeout(search, 220);
  }
  async function openStory(story) {
    const epoch = state.spaceEpoch;
    try {
      if (!story.text) {
        const result = await api(`/stories/${encodeURIComponent(story.id)}`);
        if (epoch !== state.spaceEpoch) return;
        story = result.story;
      }
      if (!story || typeof story.text !== "string" || !story.text.trim()) throw new Error("这个来源没有提供可朗读的正文，请选择其他故事。");
      stopPlayback(true);
      state.selected = story;
      state.currentChar = 0;
      $("reader-title").textContent = story.title;
      $("reader-source").textContent = sourceLabel(story);
      $("reader-text").replaceChildren();
      story.text.split(/\n+/).filter(Boolean).forEach((line) => $("reader-text").append(node("p", "", line)));
      $("reader").hidden = false;
      $("reader-placeholder").hidden = true;
      $("playing-title").textContent = story.title;
      $("play-toggle").disabled = false;
      $("play-status").textContent = "选好声音，再轻轻点播放";
      $("favorite-story").disabled = !writable();
      updateFavoriteButton();
      renderLibrary();
      updateProgress();
      if (!desktopMedia.matches) $("reader").scrollIntoView({ block: "start", behavior: "instant" });
    } catch (error) { if (error.name !== "AbortError" && !state.disposed) toast(error.message); }
  }
  function updateFavoriteButton() {
    const found = state.favorites.some((s) => s.id === state.selected?.id);
    $("favorite-story").textContent = found ? "♥ 已收藏" : "♡ 收藏";
    $("favorite-story").setAttribute("aria-pressed", String(found));
  }
  async function toggleFavorite() {
    if (!state.selected || !writable()) return;
    const selected = state.selected;
    const epoch = state.spaceEpoch;
    const exists = state.favorites.some((s) => s.id === selected.id);
    $("favorite-story").disabled = true;
    try {
      await api(`/favorites/${encodeURIComponent(selected.id)}`, exists ? "DELETE" : "PUT", exists ? undefined : { story: selected });
      const result = await api("/favorites");
      if (epoch !== state.spaceEpoch || state.disposed) return;
      state.favorites = result.favorites || [];
      updateFavoriteButton();
      if (state.library === "favorites") renderLibrary();
      toast(exists ? "已取消收藏" : "这篇故事，留给下次晚安");
    } catch (error) { if (error.name !== "AbortError" && !state.disposed) toast(error.message); }
    finally { $("favorite-story").disabled = !writable(); }
  }
  function cacheSelectedStory() {
    if (!state.selected) return;
    const stories = offlineStories().filter((s) => s.id !== state.selected.id);
    stories.unshift({ ...state.selected, savedAt: Date.now() });
    try {
      if (JSON.stringify(stories.slice(0, 30)).length > 1_500_000) throw new Error("离线空间已满，请先删除缓存，或下载文本。");
      safeStorage(namespace(), JSON.stringify(stories.slice(0, 30)));
      toast("故事文本已保存到本设备，可在已打开的离线页阅读");
    } catch (error) { toast(error.message); }
  }
  function downloadStory() {
    if (!state.selected) return;
    const story = state.selected;
    const blob = new Blob([`${story.title}\n${sourceLabel(story)}\n\n${story.text}`], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = node("a");
    a.href = url;
    a.download = `${story.title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 80)}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function systemVoices() {
    return window.speechSynthesis?.getVoices() || [];
  }
  function renderVoices() {
    const selected = $("voice-selector").value;
    $("voice-selector").replaceChildren();
    const defaultOption = node("option", "", "设备默认声音");
    defaultOption.value = "system:default";
    $("voice-selector").append(defaultOption);
    const actualVoices = systemVoices();
    actualVoices.forEach((voice, index) => {
      const option = node("option", "", `${voice.name} · ${voice.lang}${voice.localService ? " · 本机" : ""}`);
      option.value = `system:${index}`;
      $("voice-selector").append(option);
    });
    state.voices.forEach((voice) => {
      const option = node("option", "", `${voice.name}${voice.kind === "system" || voice.id.startsWith("cloud:") ? " · 云端" : " · 我的音色"}`);
      option.value = voice.id;
      option.disabled = voice.state !== "ready" || !state.config?.synthesis?.enabled || !writable();
      $("voice-selector").append(option);
    });
    if ([...$("voice-selector").options].some((option) => option.value === selected && !option.disabled)) $("voice-selector").value = selected;
    $("voice-library").replaceChildren();
    const clones = state.voices.filter((v) => !v.id.startsWith("cloud:") && v.kind !== "system");
    if (!clones.length) $("voice-library").append(node("p", "status-text", "还没有专属音色。设备自带声音可以直接朗读故事。"));
    clones.forEach((voice) => {
      const row = node("div", "voice-profile");
      const info = node("div");
      info.append(node("strong", "", voice.name), node("small", "", { ready: "已就绪", processing: "声音处理中", failed: "生成失败", pending: "等待服务处理" }[voice.state] || "等待处理"));
      row.append(info);
      if (voice.state === "ready" && state.config?.synthesis?.enabled && writable()) row.append(action("使用", () => {
        $("voice-selector").value = voice.id;
        switchVoice(); closeVoices();
      }));
      const remove = action("删除", async () => {
        if (!writable()) return;
        remove.disabled = true;
        try {
          await api(`/voices/${encodeURIComponent(voice.id)}`, "DELETE");
          if ($("voice-selector").value === voice.id) { $("voice-selector").value = "system:default"; stopPlayback(); }
          await refreshVoices();
          toast("音色已删除");
        } catch (error) { toast(error.message); remove.disabled = false; }
      });
      remove.disabled = !writable();
      row.append(remove); $("voice-library").append(row);
    });
  }
  async function refreshVoices() {
    const result = await api("/voices");
    state.voices = [...(result.systemVoices || []).map((voice) => ({ ...voice, state: "ready" })), ...(result.voices || [])];
    renderVoices();
  }
  function configureCapabilities() {
    const clone = state.config?.voiceClone?.enabled && writable();
    $("web-search").disabled = !state.config?.webSearch?.enabled;
    $("clone-entry-note").textContent = clone ? "上传 10–30 秒人声，创建专属声音" : "可先选择录音试听 · 专属音色服务待连接";
    const reason = !writable() ? "当前空间为只读。可以在本机试听录音、用设备声音听故事；创建音色需要编辑权限。" : !state.config?.voiceClone?.enabled ? "专属音色服务尚未连接。可以先选择录音在本机试听；素材不会提交到云端。管理员连接服务后，才能创建音色并联网朗读故事。" : `已配置云端音色服务${state.config.voiceClone.provider ? `：${state.config.voiceClone.provider}` : ""}。素材仅在你确认创建时提交给配置的服务，用于创建你的个人音色。`;
    $("clone-status").textContent = reason;
    $("voice-name").disabled = state.disposed;
    $("voice-file").disabled = state.disposed;
    $("voice-consent").disabled = !clone;
    updateCloneSubmit();
    if (!state.config?.webSearch?.enabled) $("web-search").title = "管理员配置联网搜索服务后启用";
    $("search-source-note").textContent = state.config?.webSearch?.enabled ? "原创故事库 · 可切换联网" : "原创故事库 · 联网服务待连接";
  }
  function openVoices() {
    if (state.disposed || $("voice-dialog").open) return;
    document.body.classList.add("dialog-open");
    $("voice-dialog").showModal();
    $("close-voices").focus({ preventScroll: true });
  }
  function closeVoices() {
    $("voice-dialog").close();
    document.body.classList.remove("dialog-open");
    clearVoiceSample();
    $("voice-name").value = "";
    $("voice-consent").checked = false;
    $("clone-error").textContent = "";
    updateCloneSubmit();
  }
  function updateCloneSubmit() {
    $("clone-submit").disabled = state.disposed || state.cloneBusy || !state.config?.voiceClone?.enabled || !writable() || !state.voiceFile || !$("voice-consent").checked;
  }
  function clearVoiceSample(resetFile = true) {
    ++state.voiceSampleEpoch;
    const player = $("voice-sample-player");
    player.pause();
    player.removeAttribute("src");
    player.load();
    if (state.voicePreviewURL) URL.revokeObjectURL(state.voicePreviewURL);
    state.voicePreviewURL = null;
    state.voiceFile = null;
    if (resetFile) $("voice-file").value = "";
    $("sample-preview").hidden = true;
    $("audio-duration-note").textContent = "清晰自然说话，无音乐、无其他人声。";
  }
  async function decodeVoiceFile(file) {
    if (!file) return null;
    if (file.size > 10 * 1024 * 1024) throw new Error("声音素材需小于 10 MB。");
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) throw new Error("当前浏览器无法检查声音，请使用近期版本的 Safari 或 Chrome。");
    const context = new Context();
    let buffer;
    try { buffer = await context.decodeAudioData(await file.arrayBuffer()); }
    catch { throw new Error("声音无法解码，请上传 WAV、MP3 或 M4A 人声音频。"); }
    finally { await context.close(); }
    const sampleRate = 24000;
    // Decoders can resample a 10-second WAV into 440999 frames at 44.1 kHz.
    // Validate the exact PCM frame count we will submit, not that tiny drift.
    const frames = Math.ceil(buffer.duration * sampleRate);
    if (frames < 10 * sampleRate || frames > 30 * sampleRate) throw new Error(`这段声音约 ${buffer.duration.toFixed(1)} 秒，请上传 10–30 秒人声。`);
    const offline = new OfflineAudioContext(1, frames, sampleRate);
    const source = offline.createBufferSource(); source.buffer = buffer; source.connect(offline.destination); source.start();
    const rendered = await offline.startRendering();
    const pcm = rendered.getChannelData(0);
    const wav = new ArrayBuffer(44 + pcm.length * 2), view = new DataView(wav);
    const write = (offset, text) => { [...text].forEach((c, index) => view.setUint8(offset + index, c.charCodeAt(0))); };
    write(0, "RIFF"); view.setUint32(4, wav.byteLength - 8, true); write(8, "WAVE"); write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, "data"); view.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) { const s = Math.max(-1, Math.min(1, pcm[i])); view.setInt16(44 + i * 2, s < 0 ? s * 32768 : s * 32767, true); }
    let binary = "";
    const bytes = new Uint8Array(wav);
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return { base64: btoa(binary), duration: frames / sampleRate, blob: new Blob([wav], { type: "audio/wav" }) };
  }
  async function createClone(event) {
    event.preventDefault();
    if (!writable() || !state.config?.voiceClone?.enabled || state.cloneBusy) return;
    if (!$("voice-consent").checked) return;
    state.cloneBusy = true;
    const epoch = state.spaceEpoch;
    const sampleEpoch = state.voiceSampleEpoch;
    $("clone-submit").disabled = true;
    $("clone-submit").textContent = "正在创建声音…";
    $("clone-error").textContent = "";
    try {
      const file = $("voice-file").files[0];
      const voiceFile = state.voiceFile || await decodeVoiceFile(file);
      if (!voiceFile) throw new Error("请先选择一段 10–30 秒声音。");
      if (epoch !== state.spaceEpoch) return;
      const result = await api("/voices", "POST", { name: $("voice-name").value.trim(), audioBase64: voiceFile.base64, mimeType: "audio/wav", consent: true });
      if (epoch !== state.spaceEpoch || state.disposed) return;
      await refreshVoices();
      if (epoch !== state.spaceEpoch || state.disposed || sampleEpoch !== state.voiceSampleEpoch) return;
      $("voice-name").value = ""; $("voice-consent").checked = false; clearVoiceSample();
      if (result.voice?.state === "ready" && state.voices.some((voice) => voice.id === result.voice.id)) {
        $("voice-selector").value = result.voice.id;
        switchVoice(); closeVoices();
        toast(state.selected ? "专属音色已创建并选中，点播放即可朗读当前故事" : "专属音色已创建并选中，选择一篇故事即可播放");
      } else toast("声音已提交，状态会在音色列表更新");
      clearInterval(voicePoll);
      if (state.voices.some((v) => ["processing", "pending"].includes(v.state))) voicePoll = setInterval(() => refreshVoices().catch(() => {}), 8000);
    } catch (error) { if (error.name !== "AbortError" && !state.disposed) $("clone-error").textContent = error.message; }
    finally {
      state.cloneBusy = false;
      updateCloneSubmit();
      $("clone-submit").textContent = "创建专属音色";
    }
  }
  function chunksFrom(text, startChar = 0) {
    const chunks = [];
    const remaining = text.slice(startChar);
    const segments = remaining.match(/[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+/g) || [remaining];
    let offset = startChar;
    // Small sentences make system speech volume fade effective between chunks.
    for (const segment of segments) {
      for (let start = 0; start < segment.length; start += 480) {
        const slice = segment.slice(start, start + 480);
        if (slice.trim()) chunks.push({ text: slice, offset });
        offset += slice.length;
      }
    }
    return chunks;
  }
  function effectiveVolume() {
    const base = Number($("speech-volume").value);
    if (!state.deadline || !$("fade-volume").checked) return base;
    return base * Math.min(1, Math.max(0, (state.deadline - Date.now()) / 60000));
  }
  function noiseVolume() {
    const base = Number($("noise-volume").value);
    if (!state.deadline || !$("fade-volume").checked) return base;
    return base * Math.min(1, Math.max(0, (state.deadline - Date.now()) / 60000));
  }
  async function ensureAudioContext() {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return null;
    if (!audioContext || audioContext.state === "closed") audioContext = new Context();
    if (audioContext.state === "suspended") await audioContext.resume();
    return audioContext;
  }
  function stopNoise() {
    try { noiseSource?.stop(); } catch { /* An already ended source is safe. */ }
    noiseSource?.disconnect(); noiseGain?.disconnect(); noiseSource = null; noiseGain = null;
  }
  async function startNoise() {
    stopNoise();
    if (!state.playing || $("noise-kind").value === "none") return;
    const epoch = state.audioEpoch;
    const context = await ensureAudioContext();
    if (!context || epoch !== state.audioEpoch || !state.playing) return;
    const kind = $("noise-kind").value;
    const length = context.sampleRate * 4;
    const buffer = context.createBuffer(1, length, context.sampleRate);
    const data = buffer.getChannelData(0);
    let brown = 0;
    for (let i = 0; i < length; i++) {
      const random = Math.random() * 2 - 1;
      brown = (brown + random * 0.035) / 1.035;
      if (kind === "rain") data[i] = random * 0.24 + brown * 0.5;
      else if (kind === "stream") data[i] = brown * 1.8 + random * 0.045 * (1 + Math.sin(i / context.sampleRate * 5));
      else data[i] = brown * 1.0 + (Math.random() < 0.001 ? random * 0.18 : 0);
      const edge = Math.min(1, i / 1800, (length - i - 1) / 1800);
      data[i] *= edge;
    }
    noiseSource = context.createBufferSource(); noiseSource.buffer = buffer; noiseSource.loop = true;
    noiseGain = context.createGain(); noiseGain.gain.value = noiseVolume();
    noiseSource.connect(noiseGain); noiseGain.connect(context.destination); noiseSource.start();
  }
  function stopPlayback(reset = false) {
    ++state.audioEpoch;
    state.playing = false;
    clearTimeout(state.speechStartTimeout);
    window.speechSynthesis?.cancel(); state.utterance = null;
    if (navigator.mediaSession) { navigator.mediaSession.metadata = null; navigator.mediaSession.playbackState = "none"; }
    if (state.audio) { state.audio.pause(); state.audio.removeAttribute("src"); state.audio.load(); state.audio = null; }
    speechGain?.disconnect(); speechGain = null;
    stopNoise();
    if (audioContext && audioContext.state === "running") audioContext.suspend().catch(() => {});
    for (const url of state.urls) URL.revokeObjectURL(url);
    state.urls.clear();
    if (reset) { state.currentChar = 0; state.elapsed = 0; state.startedAt = 0; }
    $("play-toggle").textContent = "▶";
    $("play-toggle").setAttribute("aria-label", "开始朗读");
  }
  async function recordHistory(position = 0) {
    if (!state.selected || !writable()) return;
    const story = state.selected;
    try {
      await api("/history", "POST", { story, storyId: story.id, title: story.title, voiceId: $("voice-selector").value, positionSeconds: Math.max(0, Math.round(position)) });
      const result = await api("/history");
      state.history = result.history || [];
      if (state.library === "history") renderLibrary();
    } catch { /* Reading remains available if a history update fails. */ }
  }
  function beginPlaybackUI() {
    $("play-toggle").textContent = "Ⅱ";
    $("play-toggle").setAttribute("aria-label", "暂停朗读");
    $("play-status").textContent = "正在轻声读给你听";
    state.startedAt = Date.now() - state.elapsed * 1000;
    if (navigator.mediaSession && window.MediaMetadata && state.selected) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: state.selected.title, artist: "知序晚安故事", album: state.space?.name || "" });
      navigator.mediaSession.playbackState = "playing";
    }
  }
  async function playCurrentChunk(epoch) {
    if (epoch !== state.audioEpoch || !state.playing) return;
    if (state.deadline && Date.now() >= state.deadline) { timerTick(); return; }
    const chunk = state.chunks[state.chunkIndex];
    if (!chunk) {
      const elapsed = state.elapsed;
      state.currentChar = state.selected?.text.length || 0;
      stopPlayback(); updateProgress();
      $("play-status").textContent = "故事读完了，愿你今晚安睡";
      recordHistory(elapsed);
      return;
    }
    state.currentChar = chunk.offset;
    const voiceId = $("voice-selector").value;
    if (voiceId.startsWith("system:")) {
      if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) throw new Error("当前浏览器没有朗读功能。可以阅读文本，或在支持系统语音的设备打开。");
      const utterance = new SpeechSynthesisUtterance(chunk.text);
      const index = voiceId.slice(7);
      if (index !== "default") utterance.voice = systemVoices()[Number(index)] || null;
      utterance.lang = utterance.voice?.lang || "zh-CN";
      utterance.rate = Number($("speech-rate").value);
      utterance.volume = effectiveVolume();
      state.utterance = utterance;
      utterance.onstart = () => {
        if (epoch !== state.audioEpoch) return;
        clearTimeout(state.speechStartTimeout);
        beginPlaybackUI();
        if (state.chunkIndex === 0) recordHistory(state.elapsed);
      };
      utterance.onboundary = (event) => {
        if (epoch !== state.audioEpoch) return;
        state.currentChar = chunk.offset + event.charIndex;
        updateProgress();
      };
      utterance.onend = () => {
        if (epoch !== state.audioEpoch || !state.playing) return;
        state.currentChar = chunk.offset + chunk.text.length;
        state.chunkIndex++;
        playCurrentChunk(epoch).catch((error) => playbackError(error, epoch));
      };
      utterance.onerror = (event) => {
        if (epoch !== state.audioEpoch || ["canceled", "interrupted"].includes(event.error)) return;
        playbackError(new Error("设备语音暂时无法播放，请切换实际可用的声音或阅读文本。"), epoch);
      };
      $("play-status").textContent = "正在唤起设备朗读…";
      window.speechSynthesis.speak(utterance);
      state.speechStartTimeout = setTimeout(() => {
        if (epoch === state.audioEpoch && state.utterance === utterance && !state.startedAt) playbackError(new Error("设备尚未提供可用的朗读声音，请切换其他声音或阅读文本。"), epoch);
      }, 8000);
      return;
    }
    if (!state.config?.synthesis?.enabled || !writable()) throw new Error("当前云端朗读尚不可用，请选择设备声音。");
    $("play-status").textContent = "正在准备这一段声音…";
    const result = await api("/synthesize", "POST", { text: chunk.text, voiceId, speed: Number($("speech-rate").value) });
    if (epoch !== state.audioEpoch || !state.playing) return;
    const url = new URL(result.audioUrl, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(`${prefix()}/`)) throw new Error("声音地址无法安全读取，请联系管理员检查音色服务。");
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-Expected-User": state.user.id } });
    if (epoch !== state.audioEpoch || !state.playing) return;
    if (response.status === 401 || response.status === 403) { expireSession("声音访问权限已失效，请返回工作台重新登录。"); return; }
    if (!response.ok) {
      if (response.status === 404) await verifyMembership();
      throw new Error("声音尚未准备好，请稍后再试。");
    }
    const blob = await response.blob();
    if (epoch !== state.audioEpoch || !state.playing) return;
    if (!blob.type.startsWith("audio/") && blob.type !== "application/octet-stream") throw new Error("声音响应格式异常，请重试。");
    const objectURL = URL.createObjectURL(blob); state.urls.add(objectURL);
    const audio = new Audio(objectURL); state.audio = audio;
    audio.playbackRate = Number($("speech-rate").value);
    const context = await ensureAudioContext();
    if (epoch !== state.audioEpoch || !state.playing) { URL.revokeObjectURL(objectURL); state.urls.delete(objectURL); return; }
    if (context) {
      const source = context.createMediaElementSource(audio);
      speechGain = context.createGain(); speechGain.gain.value = effectiveVolume(); source.connect(speechGain); speechGain.connect(context.destination);
    } else audio.volume = effectiveVolume();
    audio.onplaying = () => {
      if (epoch !== state.audioEpoch) return;
      beginPlaybackUI();
      if (state.chunkIndex === 0) recordHistory(state.elapsed);
    };
    audio.ontimeupdate = () => {
      if (epoch !== state.audioEpoch) return;
      if (Number.isFinite(audio.duration) && audio.duration > 0) state.currentChar = chunk.offset + Math.floor(chunk.text.length * audio.currentTime / audio.duration);
      updateProgress();
    };
    audio.onended = () => {
      URL.revokeObjectURL(objectURL); state.urls.delete(objectURL);
      if (epoch !== state.audioEpoch || !state.playing) return;
      speechGain?.disconnect(); speechGain = null;
      state.currentChar = chunk.offset + chunk.text.length;
      state.chunkIndex++;
      playCurrentChunk(epoch).catch((error) => playbackError(error, epoch));
    };
    audio.onerror = () => playbackError(new Error("声音无法播放，请重试或切换设备声音。"), epoch);
    await audio.play();
  }
  function playbackError(error, epoch) {
    if (epoch !== state.audioEpoch || state.disposed) return;
    stopPlayback();
    $("play-status").textContent = error.message;
    toast(error.message);
  }
  async function startPlayback() {
    if (!state.selected || state.disposed) return;
    if (state.deadline && Date.now() >= state.deadline) { timerTick(); return; }
    stopPlayback();
    const epoch = state.audioEpoch;
    state.playing = true;
    if (state.currentChar >= state.selected.text.length) { state.currentChar = 0; state.elapsed = 0; }
    state.chunks = chunksFrom(state.selected.text, state.currentChar);
    state.chunkIndex = 0;
    state.startedAt = 0;
    try {
      // Resume in the user's click before awaiting network calls (iOS policy).
      await ensureAudioContext();
      if (epoch !== state.audioEpoch || !state.playing) return;
      await startNoise();
      await playCurrentChunk(epoch);
    } catch (error) { playbackError(error, epoch); }
  }
  function switchVoice() {
    const wasPlaying = state.playing;
    stopPlayback();
    $("play-status").textContent = "声音已切换，故事文本保持不变";
    if (wasPlaying) startPlayback();
  }
  function formatTime(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }
  function updateProgress() {
    const text = state.selected?.text || "";
    $("story-progress").value = text.length ? Math.min(1, state.currentChar / text.length) : 0;
    $("elapsed-time").textContent = formatTime(state.elapsed);
    $("duration-time").textContent = text ? `约 ${minutes(state.selected)} 分` : "—";
  }
  function timerTick() {
    if (state.playing && state.startedAt) state.elapsed = (Date.now() - state.startedAt) / 1000;
    updateProgress();
    if (!state.deadline) return;
    const remaining = state.deadline - Date.now();
    if (remaining <= 0) {
      const elapsed = state.elapsed;
      state.deadline = 0;
      $("sleep-timer").value = "0";
      $("timer-summary").textContent = "定时已结束";
      stopPlayback();
      $("play-status").textContent = "定时已停止。晚安，愿你睡得安稳。";
      recordHistory(elapsed);
    } else {
      $("timer-summary").textContent = `${formatTime(remaining / 1000)} 后停止`;
      if (speechGain && audioContext) speechGain.gain.setTargetAtTime(effectiveVolume(), audioContext.currentTime, 0.1);
      else if (state.audio) state.audio.volume = effectiveVolume();
      if (noiseGain && audioContext) noiseGain.gain.setTargetAtTime(noiseVolume(), audioContext.currentTime, 0.1);
    }
  }
  async function loadSpace() {
    state.disposed = false;
    state.config = null;
    $("web-search").checked = false; state.scope = "local";
    state.library = "browse";
    state.stories = []; state.favorites = []; state.history = []; state.voices = [];
    $("reader").hidden = true; $("reader-text").replaceChildren();
    $("reader-placeholder").hidden = false;
    $("playing-title").textContent = "选一个故事，开始今晚的陪伴";
    $("play-toggle").disabled = true;
    $("account-context").textContent = `${state.user.name || "我的账户"}${!writable() ? " · 只读" : ""}`;
    configureCapabilities(); renderVoices();
    renderLibrary();
    const epoch = state.spaceEpoch;
    const results = await Promise.allSettled([api("/config"), api("/voices"), api("/favorites"), api("/history")]);
    if (epoch !== state.spaceEpoch || state.disposed) return;
    const [config, voices, favorites, history] = results;
    if (config.status === "fulfilled") state.config = config.value;
    if (voices.status === "fulfilled") state.voices = [...(voices.value.systemVoices || []).map((voice) => ({ ...voice, state: "ready" })), ...(voices.value.voices || [])];
    if (favorites.status === "fulfilled") state.favorites = favorites.value.favorites || [];
    if (history.status === "fulfilled") state.history = history.value.history || [];
    configureCapabilities(); renderVoices();
    const failure = results.find((result) => result.status === "rejected");
    if (failure) toast(failure.reason.message);
    await search();
  }
  async function init() {
    applyTheme();
    ["全部", "儿童", "治愈", "古风", "寓言", "晚安"].forEach((category) => {
      const b = action(category, () => {
        state.category = category;
        [...$("category-tabs").children].forEach((tab) => tab.setAttribute("aria-pressed", String(tab === b)));
        clearTimeout(state.debounce); search();
      });
      b.setAttribute("aria-pressed", String(category === state.category));
      $("category-tabs").append(b);
    });
    try {
      const result = await jsonRequest("/api/me");
      state.user = result.user;
      const resultSpaces = await jsonRequest("/api/spaces");
      state.spaces = resultSpaces.spaces || [];
      const requestedSpace = new URLSearchParams(location.search).get("space");
      const preferred = requestedSpace || sessionStorage.getItem(`zhixu:space:${state.user.id}`);
      const uploadEntry = !requestedSpace && new URLSearchParams(location.search).get("panel") === "voices";
      state.space = uploadEntry
        ? state.spaces.find((space) => space.role === "owner" && space.member_count === 1) || state.spaces.find((space) => space.role === "owner") || state.spaces.find((space) => space.role === "editor") || state.spaces[0]
        : state.spaces.find((space) => space.id === preferred) || (!requestedSpace ? state.spaces[0] : null);
      if (!state.space) { expireSession("当前账号无法访问这个空间，请返回工作台选择自己的空间。"); return; }
      for (const space of state.spaces) { const option = node("option", "", space.name); option.value = space.id; $("space-selector").append(option); }
      $("space-selector").value = state.space.id;
      sessionStorage.setItem(`zhixu:space:${state.user.id}`, state.space.id);
      $("bedtime-app").hidden = false; $("loading-screen").hidden = true;
      updatePlayerInset();
      await loadSpace();
      if (!state.disposed && new URLSearchParams(location.search).get("panel") === "voices") openVoices();
      try {
        const environment = await jsonRequest("/api/config");
        if (environment.testMode || environment.storagePersistence !== "persistent") {
          $("environment-note").textContent = environment.testMode ? "免费测试环境 · 请使用测试声音与资料，重启后云端记录可能清空。" : "存储状态未确认 · 暂不要上传重要的声音素材。";
          $("environment-note").hidden = false;
        }
      } catch { $("environment-note").textContent = "环境状态未确认 · 请使用测试资料。"; $("environment-note").hidden = false; }
    } catch (error) {
      if (!state.disposed) expireSession(error.status === 401 ? undefined : error.message);
    }
  }
  $("theme-toggle").addEventListener("click", () => {
    const preference = safeStorage("zhixu:bedtime:theme") || "auto";
    safeStorage("zhixu:bedtime:theme", { auto: "night", night: "day", day: "auto" }[preference]); applyTheme();
  });
  themeMedia.addEventListener("change", applyTheme);
  $("story-search").addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(state.debounce); search(); $("search-input").blur(); });
  $("search-input").addEventListener("input", scheduleSearch);
  $("web-search").addEventListener("change", () => { state.scope = $("web-search").checked ? "web" : "local"; clearTimeout(state.debounce); search(); });
  document.querySelectorAll("[data-library]").forEach((b) => b.addEventListener("click", () => {
    clearTimeout(state.debounce); ++state.searchEpoch; state.searchAbort?.abort();
    state.library = b.dataset.library; listStatus(""); renderLibrary();
  }));
  $("clear-list").addEventListener("click", async () => {
    if (state.library === "offline") { clearOffline(); renderLibrary(); toast("本设备的离线故事已删除"); }
    else if (state.library === "history" && writable()) {
      try { await api("/history", "DELETE"); state.history = []; renderLibrary(); toast("播放记录已清空"); }
      catch (error) { toast(error.message); }
    }
  });
  $("close-reader").addEventListener("click", () => { $("reader").hidden = true; $("reader-placeholder").hidden = false; });
  $("favorite-story").addEventListener("click", toggleFavorite);
  $("cache-story").addEventListener("click", cacheSelectedStory);
  $("download-story").addEventListener("click", downloadStory);
  $("copy-story").addEventListener("click", async () => {
    if (!state.selected) return;
    try { await navigator.clipboard.writeText(state.selected.text); toast("故事文本已复制"); }
    catch { toast("浏览器暂不支持复制，请下载文本或选中正文复制。"); }
  });
  for (const id of ["open-voices", "player-voices", "empty-reader-voices"]) $(id).addEventListener("click", openVoices);
  $("share-voice-link").addEventListener("click", () => shareVoiceLink("share-link-result"));
  $("dialog-share-link").addEventListener("click", () => shareVoiceLink("dialog-share-result"));
  $("close-voices").addEventListener("click", closeVoices);
  $("voice-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeVoices(); });
  $("voice-dialog").addEventListener("click", (event) => {
    if (event.target !== $("voice-dialog")) return;
    const rect = $("voice-dialog").getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeVoices();
  });
  $("voice-file").addEventListener("change", async () => {
    const file = $("voice-file").files[0];
    clearVoiceSample(false); $("clone-error").textContent = "";
    updateCloneSubmit();
    if (!file) return;
    $("audio-duration-note").textContent = "正在检查这段声音…";
    $("clone-submit").disabled = true;
    const epoch = state.spaceEpoch;
    const sampleEpoch = state.voiceSampleEpoch;
    try {
      const converted = await decodeVoiceFile(file);
      if (epoch !== state.spaceEpoch || sampleEpoch !== state.voiceSampleEpoch || state.disposed || $("voice-file").files[0] !== file || !$("voice-dialog").open) return;
      state.voiceFile = converted;
      state.voicePreviewURL = URL.createObjectURL(converted.blob);
      $("voice-sample-player").src = state.voicePreviewURL;
      $("sample-preview").hidden = false;
      $("audio-duration-note").textContent = `约 ${converted.duration.toFixed(1)} 秒 · 已在本机检查声音`;
      $("sample-preview-note").textContent = state.config?.voiceClone?.enabled && writable() ? "仅在当前页面试听。确认创建时，才将这段人声交给已配置的音色服务。" : "仅在当前页面试听；云端服务待连接，这段录音不会上传。";
    } catch (error) {
      if (epoch !== state.spaceEpoch || sampleEpoch !== state.voiceSampleEpoch || state.disposed || $("voice-file").files[0] !== file) return;
      $("clone-error").textContent = error.message;
      $("audio-duration-note").textContent = "请选择一段清晰的 10–30 秒人声。";
      $("voice-file").value = "";
    } finally {
      if (epoch === state.spaceEpoch && sampleEpoch === state.voiceSampleEpoch && !state.disposed) updateCloneSubmit();
    }
  });
  $("voice-consent").addEventListener("change", updateCloneSubmit);
  $("voice-sample-player").addEventListener("play", () => {
    if (state.playing) { stopPlayback(); $("play-status").textContent = "录音试听中，故事已暂停"; }
  });
  $("clone-form").addEventListener("submit", createClone);
  window.speechSynthesis?.addEventListener("voiceschanged", renderVoices);
  $("voice-selector").addEventListener("change", switchVoice);
  $("play-toggle").addEventListener("click", () => {
    if (state.playing) { const elapsed = state.elapsed; stopPlayback(); $("play-status").textContent = "已暂停，随时继续这一篇"; recordHistory(elapsed); }
    else startPlayback();
  });
  $("speech-rate").addEventListener("change", () => {
    if (state.audio) state.audio.playbackRate = Number($("speech-rate").value);
    else if (state.playing) { stopPlayback(); startPlayback(); }
  });
  $("sleep-timer").addEventListener("change", () => {
    state.deadline = Number($("sleep-timer").value) ? Date.now() + Number($("sleep-timer").value) * 60000 : 0;
    $("timer-summary").textContent = state.deadline ? `${$("sleep-timer").value} 分钟后停止` : "睡眠工具";
    timerTick();
  });
  $("speech-volume").addEventListener("input", () => {
    if (speechGain && audioContext) speechGain.gain.setTargetAtTime(effectiveVolume(), audioContext.currentTime, 0.1);
    else if (state.audio) state.audio.volume = effectiveVolume();
  });
  $("noise-kind").addEventListener("change", () => startNoise().catch(() => toast("当前设备暂时无法播放白噪音。")));
  $("noise-volume").addEventListener("input", () => { if (noiseGain && audioContext) noiseGain.gain.setTargetAtTime(noiseVolume(), audioContext.currentTime, 0.1); });
  $("sleep-controls").addEventListener("toggle", () => {
    $("player").classList.toggle("expanded", $("sleep-controls").open);
    updatePlayerInset();
  });
  function updatePlayerInset() {
    document.querySelector("main").style.paddingBottom = desktopMedia.matches ? "" : `${$("player").getBoundingClientRect().height + 55}px`;
  }
  desktopMedia.addEventListener("change", () => { $("sleep-controls").open = desktopMedia.matches; updatePlayerInset(); });
  window.addEventListener("resize", updatePlayerInset);
  $("sleep-controls").open = desktopMedia.matches;
  $("space-selector").addEventListener("change", async () => {
    const oldSpace = state.space.id;
    stopPlayback(true); clearOffline(); signalSessionEvent("spacechange", oldSpace);
    state.searchAbort?.abort(); ++state.spaceEpoch; ++state.searchEpoch;
    state.deadline = 0; $("sleep-timer").value = "0"; $("timer-summary").textContent = "睡眠工具";
    closeVoices(); clearInterval(voicePoll);
    state.selected = null;
    state.space = state.spaces.find((space) => space.id === $("space-selector").value);
    sessionStorage.setItem(`zhixu:space:${state.user.id}`, state.space.id);
    history.replaceState(null, "", `/bedtime.html?space=${encodeURIComponent(state.space.id)}`);
    await loadSpace();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== "zhixu:session-event" || !event.newValue) return;
    try {
      const data = JSON.parse(event.newValue);
      if (data.kind === "login" && data.userId !== state.user?.id) expireSession("账号已在另一个页面切换，请返回工作台重新打开故事。");
      else if (data.kind === "logout" && data.userId === state.user?.id) expireSession();
      else if (data.kind === "spacechange" && data.userId === state.user?.id && data.spaceId === state.space?.id) {
        stopPlayback(true); clearOffline(); closeVoices();
        toast("工作台已切换空间，当前声音已停止。请确认需要的空间。");
      }
    } catch { /* Session events are data only. */ }
  });
  document.addEventListener("visibilitychange", async () => {
    timerTick();
    if (document.hidden) return;
    if (!state.user || state.disposed) return;
    try { const me = await jsonRequest("/api/me"); if (me.user.id !== state.user.id) expireSession(); else await verifyMembership(); }
    catch (error) { if (error.status === 401) expireSession(); }
  });
  if (navigator.mediaSession) {
    for (const [actionName, handler] of [
      ["play", () => startPlayback()],
      ["pause", () => { stopPlayback(); $("play-status").textContent = "已暂停，随时继续这一篇"; }],
      ["stop", () => { stopPlayback(true); $("play-status").textContent = "已停止朗读"; }],
    ]) {
      try { navigator.mediaSession.setActionHandler(actionName, handler); } catch { /* Not all devices offer lock-screen controls. */ }
    }
  }
  setInterval(() => {
    if (state.user && !state.disposed && navigator.onLine) verifyMembership();
  }, 30000);
  window.addEventListener("pagehide", () => { stopPlayback(true); clearVoiceSample(); clearInterval(voicePoll); });
  setInterval(timerTick, 500);
  setInterval(applyTheme, 60000);
  // No application-wide API, vault, uploaded media, or generated audio cache.
  init();
})();
